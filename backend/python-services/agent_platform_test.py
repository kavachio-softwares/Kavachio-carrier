"""
agent_platform_test.py — STANDALONE manual test of Kavachio's AI call on
Google Gemini Enterprise Agent Platform (formerly Vertex AI), US region.

Mirrors contract_upload_services/gemini_service.call_gemini():
  * same signature  (prompt, label, temperature, seed, response_schema, model,
                     max_output_tokens, thinking_budget, cache_split)
  * same retry/backoff on transient errors
  * same pre-flight token ceiling check (OversizeError)
  * same explicit context caching (cache_split)
  * same MAX_TOKENS truncation guard, JSON validation, degenerate-output check
  * same token usage logging

It does NOT import or change any app code. Edit the CONFIG block, then run:

    cd backend/python-services
    ./venv/bin/python agent_platform_test.py            # run all tests
    ./venv/bin/python agent_platform_test.py basic      # run one test

Auth (pick one):
  * Local Mac: `gcloud auth application-default login` and leave
    CREDENTIALS_FILE = None   (uses Application Default Credentials)
  * Service-account / Workload-Identity JSON: set CREDENTIALS_FILE to its path.
"""

import os
import re
import sys
import json
import time
import random
import hashlib
import threading

from google import genai
from google.genai import types


# ═════════════════════════════════════════════════════════════════════════════
# CONFIG — edit these for your manual test
# ═════════════════════════════════════════════════════════════════════════════

# True  = Agent Platform (formerly Vertex AI)  ← what we are migrating to
# False = direct Gemini Developer API with an API key (today's behaviour), for
#         side-by-side comparison
USE_ENTERPRISE = True

# --- Agent Platform settings ---
GCP_PROJECT = "kollab-476811"
GCP_LOCATION = "us"            # US multi-region. Verified working for 3.5 Flash /
                               # 3.5 Flash-Lite. NOTE: "us-central1" returns 404
                               # for these models; avoid "global" (no US guarantee).
# Service-account key JSON (server-style auth). Relative paths resolve from this
# file's folder. Set to None to fall back to your gcloud login (ADC) instead.
CREDENTIALS_FILE = "kollab-476811-e847abe0ca5c-gcp-credetials.json"

# --- Direct Gemini API settings (only used when USE_ENTERPRISE = False) ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")   # or paste a key here for testing

# --- Models (same roles as gemini_service.py) ---
EXTRACTION_MODEL = "gemini-3.5-flash"        # was gemini-2.5-flash (retired)
STAGE_B_MODEL = "gemini-3.5-flash"
SMALL_MODEL = "gemini-3.5-flash-lite"        # was gemini-2.5-flash-lite (retired)

DETERMINISTIC_SEED = 7
HTTP_TIMEOUT_MS = 600_000                    # exporter uses 600 s

# --- Retry / backoff (same defaults as gemini_service.py) ---
LLM_MAX_RETRIES = 4
BACKOFF_BASE = 1.5
BACKOFF_CAP = 60.0

# --- Context cache (same defaults as gemini_service.py) ---
CTX_CACHE_ON = True
CTX_CACHE_TTL = "600s"
CTX_CACHE_MIN_TOKENS = 4096

# --- Token ceilings ---
DEFAULT_INPUT_CEIL = 900_000
DEFAULT_OUTPUT_CEIL = 65_536
MODEL_CEILINGS = {
    "gemini-3.5-flash": {"input": 1_000_000, "output": 65_536},
    "gemini-3.5-flash-lite": {"input": 1_000_000, "output": 65_536},
}

# ═════════════════════════════════════════════════════════════════════════════
# CLIENT
# ═════════════════════════════════════════════════════════════════════════════

def make_client(timeout_ms=HTTP_TIMEOUT_MS):
    """Build the genai client for Agent Platform or the direct Gemini API."""
    http_options = types.HttpOptions(timeout=timeout_ms) if timeout_ms else None

    if USE_ENTERPRISE:
        credentials = None
        creds_desc = "ADC (gcloud login)"
        if CREDENTIALS_FILE:
            # Load the service-account key explicitly so the SDK uses exactly this
            # identity (never silently falls back to your personal gcloud login).
            from google.oauth2 import service_account
            path = CREDENTIALS_FILE
            if not os.path.isabs(path):
                path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
            if not os.path.exists(path):
                raise SystemExit(f"CREDENTIALS_FILE not found: {path}")
            credentials = service_account.Credentials.from_service_account_file(
                path, scopes=["https://www.googleapis.com/auth/cloud-platform"])
            creds_desc = f"service account {credentials.service_account_email}"
        print(f"[client] Agent Platform  project={GCP_PROJECT}  location={GCP_LOCATION}  "
              f"creds={creds_desc}")
        return genai.Client(
            enterprise=True,
            project=GCP_PROJECT,
            location=GCP_LOCATION,
            credentials=credentials,
            http_options=http_options,
        )

    # if not GEMINI_API_KEY:
    #     raise SystemExit("USE_ENTERPRISE=False but GEMINI_API_KEY is empty.")
    # print("[client] Direct Gemini Developer API (API key)")
    # return genai.Client(enterprise=True, project=GCP_PROJECT, location="us-central1")

    # return genai.Client(api_key=GEMINI_API_KEY, http_options=http_options)
    # client = genai.Client(vertexai=True, project="your-gcp-project-id", location="us-central1")



client = make_client()


# ═════════════════════════════════════════════════════════════════════════════
# TOKEN BUDGETING (same logic as gemini_service.py)
# ═════════════════════════════════════════════════════════════════════════════

class OversizeError(Exception):
    def __init__(self, message, input_tokens=None, ceiling=None):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.ceiling = ceiling


def model_ceilings(model=None):
    c = MODEL_CEILINGS.get(model or EXTRACTION_MODEL)
    if c:
        return c["input"], c["output"]
    return DEFAULT_INPUT_CEIL, DEFAULT_OUTPUT_CEIL


def estimate_tokens(prompt, model=None):
    text = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
    return max(1, len(text) // 4)


def clamp_max_output_tokens(requested, model=None):
    if requested is None:
        return None
    _in, out_ceil = model_ceilings(model)
    return min(int(requested), out_ceil)


def should_chunk(prompt, max_output_tokens=0, thinking_budget=0, model=None):
    in_ceil, _out = model_ceilings(model)
    est = estimate_tokens(prompt, model) + int(max_output_tokens or 0) + int(thinking_budget or 0)
    return est > in_ceil, est, in_ceil


# ═════════════════════════════════════════════════════════════════════════════
# RETRY (same logic as gemini_service.py, limiter omitted)
# ═════════════════════════════════════════════════════════════════════════════

def _is_transient(exc) -> bool:
    s = f"{type(exc).__name__} {exc}".lower()
    needles = (
        "429", "resource_exhausted", "rate limit", "quota",
        "500", "503", "504", "unavailable", "internal error",
        "deadline", "timeout", "timed out",
        "connection reset", "connection aborted", "broken pipe",
        "temporarily", "overloaded", "try again",
    )
    return any(n in s for n in needles)


def _retry_after_seconds(exc):
    m = re.search(r"retry[-\s]?after['\":\s]+(\d+)", f"{exc}".lower())
    return int(m.group(1)) if m else None


def _generate_with_retry(kwargs, label):
    attempt = 0
    while True:
        try:
            return client.models.generate_content(**kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt >= LLM_MAX_RETRIES:
                raise
            ra = _retry_after_seconds(exc)
            backoff = ra if ra is not None else min(BACKOFF_CAP, BACKOFF_BASE ** attempt)
            backoff += random.uniform(0, min(1.0, backoff))
            print(f"[{label}] transient error (attempt {attempt + 1}/{LLM_MAX_RETRIES}): "
                  f"{type(exc).__name__}: {str(exc)[:160]} — retrying in {backoff:.1f}s")
            time.sleep(backoff)
            attempt += 1


# ═════════════════════════════════════════════════════════════════════════════
# CONTEXT CACHE (same logic as gemini_service.py)
# ═════════════════════════════════════════════════════════════════════════════

_ctx_caches: dict = {}
_ctx_lock = threading.Lock()


def context_cache_for(prefix: str, model: str):
    if not CTX_CACHE_ON or not prefix:
        return None
    if estimate_tokens(prefix, model) < CTX_CACHE_MIN_TOKENS:
        return None
    key = (model, hashlib.sha256(prefix.encode("utf-8")).hexdigest())
    with _ctx_lock:
        if key in _ctx_caches:
            return _ctx_caches[key]
    try:
        cache = client.caches.create(
            model=model,
            config=types.CreateCachedContentConfig(contents=[prefix], ttl=CTX_CACHE_TTL),
        )
        name = getattr(cache, "name", None)
    except Exception as exc:
        print(f"[ctx-cache] not created ({type(exc).__name__}: {str(exc)[:200]}) "
              f"— sending full prompt")
        name = None
    with _ctx_lock:
        _ctx_caches[key] = name
    if name:
        print(f"[ctx-cache] created {name} "
              f"(~{estimate_tokens(prefix, model):,} tok, ttl {CTX_CACHE_TTL})")
    return name


def release_context_caches():
    with _ctx_lock:
        names = [n for n in _ctx_caches.values() if n]
        _ctx_caches.clear()
    for n in names:
        try:
            client.caches.delete(name=n)
            print(f"[ctx-cache] released {n}")
        except Exception as exc:
            print(f"[ctx-cache] release failed for {n}: {exc}")


# ═════════════════════════════════════════════════════════════════════════════
# RESPONSE HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def clean_gemini_response(text: str):
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)
    return text.strip()


def _is_degenerate(text: str) -> bool:
    s = (text or "").strip()
    if len(s) < 50:
        return False
    from collections import Counter
    _ch, n = Counter(s).most_common(1)[0]
    return n / len(s) >= 0.95


# ═════════════════════════════════════════════════════════════════════════════
# call_gemini — same signature and behaviour as gemini_service.call_gemini
# ═════════════════════════════════════════════════════════════════════════════

def call_gemini(prompt, label="LLM", temperature=None, seed=None,
                response_schema=None, model=None, max_output_tokens=None,
                thinking_budget=None, cache_split=None):
    _model = model or EXTRACTION_MODEL
    print(f"\n[{label}] Calling {_model} ...")

    # Pre-flight token budget
    max_output_tokens = clamp_max_output_tokens(max_output_tokens, _model)
    over, est_input, in_ceil = should_chunk(
        prompt, max_output_tokens or 0, thinking_budget or 0, _model)
    if over:
        raise OversizeError(
            f"[{label}] request ~{est_input} tokens exceeds input ceiling "
            f"{in_ceil} for {_model}; split before sending.",
            input_tokens=est_input, ceiling=in_ceil)

    # Context cache: split fixed prefix from variable payload
    _cache_name = None
    _contents = prompt
    if cache_split and CTX_CACHE_ON:
        _cut = prompt.rfind(cache_split)
        if _cut > 0:
            _prefix, _suffix = prompt[:_cut], prompt[_cut:]
            _cache_name = context_cache_for(_prefix, _model)
            if _cache_name:
                _contents = _suffix

    kwargs = {"model": _model, "contents": _contents}

    config = {}
    if temperature is not None:
        config["temperature"] = temperature
    if seed is not None:
        config["seed"] = seed
    if max_output_tokens is not None:
        config["max_output_tokens"] = max_output_tokens
    if response_schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_schema"] = response_schema
    if thinking_budget is not None:
        config["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)
    if _cache_name is not None:
        config["cached_content"] = _cache_name
    if config:
        kwargs["config"] = config

    t0 = time.time()
    try:
        response = _generate_with_retry(kwargs, label)
    except Exception as exc:
        if response_schema is not None:
            print(f"[{label}] response_schema rejected ({exc}); "
                  f"retrying without structured output.")
            config.pop("response_mime_type", None)
            config.pop("response_schema", None)
            if _cache_name is not None:
                config["cached_content"] = _cache_name
            if config:
                kwargs["config"] = config
            else:
                kwargs.pop("config", None)
            response = _generate_with_retry(kwargs, label)
        else:
            raise
    elapsed = time.time() - t0

    # Token usage
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        print(
            f"[{label}] TOKENS  input={getattr(usage, 'prompt_token_count', None)}  "
            f"processing(thoughts)={getattr(usage, 'thoughts_token_count', None)}  "
            f"output={getattr(usage, 'candidates_token_count', None)}  "
            f"cached={getattr(usage, 'cached_content_token_count', None)}  "
            f"total={getattr(usage, 'total_token_count', None)}  "
            f"time={elapsed:.1f}s"
        )

    # Truncation detection
    _cands = getattr(response, "candidates", None) or []
    _finish = str(getattr(_cands[0], "finish_reason", "")) if _cands else ""
    if "MAX_TOKENS" in _finish:
        raise Exception(f"[{label}] hit MAX_TOKENS (answer truncated) — caller should split")

    cleaned = clean_gemini_response(getattr(response, "text", ""))

    print(f"\n[{label}] RAW RESPONSE:")
    print("=" * 80)
    print(cleaned[:400])
    print("=" * 80)

    if not cleaned:
        raise Exception("Gemini returned empty response")

    try:
        json.loads(cleaned)
    except Exception as e:
        if _is_degenerate(cleaned):
            print(f"\n[{label}] DEGENERATE OUTPUT: {len(cleaned)} chars, ~all "
                  f"'{cleaned.strip()[:1]}'")
            raise Exception("Gemini returned degenerate (repeated-character) output")
        print("\nINVALID JSON FROM GEMINI:")
        print(cleaned[:1000])
        raise Exception(f"Gemini returned invalid JSON: {str(e)}")

    return cleaned


# ═════════════════════════════════════════════════════════════════════════════
# MANUAL TESTS — edit prompts / add your own real prompts here
# ═════════════════════════════════════════════════════════════════════════════

SAMPLE_CONTRACT = """
Clause 4.2: The Supplier shall deliver all goods within 30 days of the purchase
order date. Late delivery attracts a penalty of 2% of the order value per week,
capped at 10%. Clause 7.1: Invoices must be submitted in INR and paid within 45
days. Clause 9.3: Unit price shall not exceed the rate card price by more than 5%.
"""

RULES_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "rules": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "clause_id": {"type": "STRING"},
                    "rule": {"type": "STRING"},
                    "field": {"type": "STRING"},
                    "operator": {"type": "STRING"},
                    "value": {"type": "STRING"},
                },
                "required": ["clause_id", "rule"],
            },
        }
    },
    "required": ["rules"],
}


def test_basic():
    """Plain JSON via prompt only, default model (EXTRACTION_MODEL)."""
    call_gemini(
        'Return ONLY JSON: {"status": "ok", "model_check": true}',
        label="basic", temperature=0, seed=DETERMINISTIC_SEED, thinking_budget=0)


def test_schema():
    """Structured output with response_schema — like Stage B / extraction."""
    out = call_gemini(
        "Extract each validation rule from this contract.\n" + SAMPLE_CONTRACT,
        label="schema", temperature=0, seed=DETERMINISTIC_SEED,
        response_schema=RULES_SCHEMA, max_output_tokens=8192, thinking_budget=1024)
    print(f"[schema] rules extracted: {len(json.loads(out)['rules'])}")


def test_stage_b():
    """Stage B model with a large thinking budget (like stage_b_synthesizer)."""
    call_gemini(
        "Extract each validation rule from this contract as JSON.\n" + SAMPLE_CONTRACT,
        label="stage_b", model=STAGE_B_MODEL, temperature=0, seed=DETERMINISTIC_SEED,
        response_schema=RULES_SCHEMA, max_output_tokens=65536, thinking_budget=16384)


def test_small():
    """SMALL_MODEL yes/no check (like variation_admit / missing_columns)."""
    call_gemini(
        'Are "Purchase Order No" and "PO Number" the same field? '
        'Return ONLY JSON: {"same": true|false}',
        label="small", model=SMALL_MODEL, temperature=0, seed=DETERMINISTIC_SEED,
        max_output_tokens=256, thinking_budget=0)


def test_cache():
    """Context cache with cache_split — two calls share one cached prefix."""
    big_instructions = ("SYSTEM: You are a contract rule extractor. "
                        "Follow every instruction exactly. " * 1200)
    try:
        for i, q in enumerate(["Clause A: pay within 30 days.",
                               "Clause B: deliver within 10 days."], 1):
            prompt = (big_instructions +
                      '\nUSER:\nReturn ONLY JSON {"clause": "...", "days": N} for: ' + q)
            call_gemini(prompt, label=f"cache-{i}", temperature=0,
                        seed=DETERMINISTIC_SEED, thinking_budget=0,
                        cache_split="\nUSER:\n")
    finally:
        release_context_caches()


def test_count_tokens():
    r = client.models.count_tokens(model=EXTRACTION_MODEL, contents=SAMPLE_CONTRACT)
    print(f"\n[count_tokens] {EXTRACTION_MODEL}: {r.total_tokens} tokens")


TESTS = {
    "basic": test_basic,
    "schema": test_schema,
    "stage_b": test_stage_b,
    "small": test_small,
    "cache": test_cache,
    "count_tokens": test_count_tokens,
}


if __name__ == "__main__":
    selected = sys.argv[1:] or list(TESTS)
    results = {}
    for name in selected:
        fn = TESTS.get(name)
        if not fn:
            print(f"Unknown test '{name}'. Choose from: {', '.join(TESTS)}")
            continue
        print(f"\n{'#' * 80}\n# TEST: {name}\n{'#' * 80}")
        try:
            fn()
            results[name] = "PASS"
        except Exception as exc:
            results[name] = f"FAIL — {type(exc).__name__}: {str(exc)[:200]}"

    print(f"\n{'=' * 80}\nSUMMARY\n{'=' * 80}")
    for name, res in results.items():
        print(f"  {name:<14} {res}")
