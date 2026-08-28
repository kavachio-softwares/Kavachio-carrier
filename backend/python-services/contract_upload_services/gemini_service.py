import re
import os
import json
import time
import random
import threading
from collections import deque
from contextlib import contextmanager
from google import genai
from google.genai import types


# API key from the environment (GEMINI_API_KEY / GOOGLE_API_KEY). The previous
# hardcoded key must be rotated; the literal fallback is kept ONLY so existing
# setups don't break before the env var is configured — remove it once rotated.
# SECURITY: when the literal fallback is used we log a loud warning so it can't be
# forgotten. Set GEMINI_API_KEY in the environment/.env and rotate the exposed key.
_API_KEY = (
    os.getenv("GEMINI_API_KEY")
)
if not (os.getenv("GEMINI_API_KEY")):
    print("[gemini_service] SECURITY WARNING: using the hardcoded fallback API key. "
          "Set GEMINI_API_KEY in the environment and rotate the exposed key.")
client = genai.Client(api_key=_API_KEY)


# ─────────────────────────────────────────────────────────────────────────────
# AI GATEWAY — the single front door every call_gemini invocation goes through.
# Adds three cross-cutting concerns without touching any call site:
#   1. Retry with exponential backoff + jitter on transient / rate-limit errors.
#   2. A shared concurrency cap + tokens-per-minute / requests-per-minute limiter
#      so running many contracts at once can never overwhelm the AI quota.
#   3. Token estimation + a per-model ceiling so callers can size batches by
#      TOKENS (should_chunk) and detect oversize BEFORE truncation (OversizeError).
# Every knob defaults to "behaves like today": retries recover failures only
# (success path unchanged), and the limiter is OFF unless limits are configured.
# ─────────────────────────────────────────────────────────────────────────────

# Retry / backoff
LLM_MAX_RETRIES   = int(os.getenv("KAVACHIO_LLM_MAX_RETRIES", "4"))
_BACKOFF_BASE     = float(os.getenv("KAVACHIO_LLM_BACKOFF_BASE", "1.5"))
_BACKOFF_CAP      = float(os.getenv("KAVACHIO_LLM_BACKOFF_CAP", "60"))

# Shared limiter (0 / unset = OFF, so single-contract usage is untouched)
_MAX_CONCURRENCY  = int(os.getenv("KAVACHIO_LLM_MAX_CONCURRENCY", "0"))
_RPM              = int(os.getenv("KAVACHIO_LLM_RPM", "0"))
_TPM              = int(os.getenv("KAVACHIO_LLM_TOKENS_PER_MIN", "0"))

# Token budgeting. Generous defaults so nothing wrongly trips; tune per model.
_COUNT_TOKENS     = os.getenv("KAVACHIO_LLM_COUNT_TOKENS", "0") == "1"
_DEFAULT_INPUT_CEIL  = int(os.getenv("KAVACHIO_LLM_INPUT_CEILING", "900000"))
_DEFAULT_OUTPUT_CEIL = int(os.getenv("KAVACHIO_LLM_OUTPUT_CEILING", "65536"))
# Per-model overrides may be added here; falls back to the defaults above.
_MODEL_CEILINGS = {
    "gemini-2.5-flash": {"input": 1_000_000, "output": 65536},
    "gemini-2.5-flash-lite": {"input": 1_000_000, "output": 65536},
    "gemini-3.5-flash": {"input": 1_000_000, "output": 65536},
}


class OversizeError(Exception):
    """Raised before a call when the estimated input + requested output exceeds
    the model's ceiling, so the caller can split the work instead of blindly
    sending a request that would truncate."""
    def __init__(self, message, input_tokens=None, ceiling=None):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.ceiling = ceiling


def model_ceilings(model=None):
    m = model or EXTRACTION_MODEL
    c = _MODEL_CEILINGS.get(m)
    if c:
        return c["input"], c["output"]
    return _DEFAULT_INPUT_CEIL, _DEFAULT_OUTPUT_CEIL


def estimate_tokens(prompt, model=None):
    """Best-effort input-token estimate. Cheap chars/4 heuristic by default;
    uses the API's count_tokens only when KAVACHIO_LLM_COUNT_TOKENS=1 (it costs a
    round-trip and can itself be rate-limited). Never raises."""
    text = prompt if isinstance(prompt, str) else json.dumps(prompt, default=str)
    if _COUNT_TOKENS:
        try:
            r = client.models.count_tokens(model=model or EXTRACTION_MODEL, contents=text)
            n = getattr(r, "total_tokens", None)
            if n:
                return int(n)
        except Exception:
            pass  # fall through to heuristic
    return max(1, len(text) // 4)


def clamp_max_output_tokens(requested, model=None):
    """Clamp a requested output budget to the model's real output ceiling."""
    _in, out_ceil = model_ceilings(model)
    if requested is None:
        return None
    return min(int(requested), out_ceil)


def should_chunk(prompt, max_output_tokens=0, thinking_budget=0, model=None):
    """True when input + requested output + thinking would exceed the model input
    ceiling — i.e. the caller should split before sending. Cheap and safe."""
    in_ceil, _out = model_ceilings(model)
    est = estimate_tokens(prompt, model) + int(max_output_tokens or 0) + int(thinking_budget or 0)
    return est > in_ceil, est, in_ceil


# ── Output-side budgeting ────────────────────────────────────────────────────
# The input ceiling (1M) and the output ceiling (65,536) differ by 15x, so in
# practice a call NEVER dies on input — it dies because the answer did not fit.
# should_chunk() above compares against the INPUT ceiling and so cannot see that
# failure at all; would_truncate() below is the guard for it.
# The answer/data multiple is NOT one number across the pipeline. Measured over 82
# real calls it is a property of the STAGE — of what that prompt turns data into —
# and the stages differ by 5x, so each call site passes its own `ratio`:
#     Stage 1 extraction  1.89-2.08  contract prose in, one JSON clause out per rule
#     Stage 2 intents     1.46-2.41  clauses in, verdict + intent out per clause
#     Stage 3 mapping     0.06-0.82  bulky intent objects in, compact IR out
# Each stage sets its ratio ABOVE its own observed max (see the *_OUTPUT_RATIO
# constants beside each call site). OUTPUT_RATIO below is only the DEFAULT for
# callers that pass nothing, and is kept above the highest per-stage figure.
#
# Do NOT fold thinking tokens into a ratio: would_truncate adds thinking_budget as a
# separate term, so a ratio derived from (answer + thinking) counts thinking twice.
# That is what made the old global 2.0 roughly 5x too eager to chunk on Stage 3.
OUTPUT_RATIO  = float(os.getenv("KAVACHIO_LLM_OUTPUT_RATIO", "2.5"))
OUTPUT_SAFETY = float(os.getenv("KAVACHIO_LLM_OUTPUT_SAFETY", "0.7"))


def would_truncate(data_text, thinking_budget=0, model=None, ratio=None):
    """True when the answer for this much DATA would not fit in the output budget.

    Pass ONLY the data block — the contract text, the clause list, the intent list —
    never the whole prompt: a 39k-character instruction block costs input tokens but
    produces no output, so including it would make every call look oversize.

    `thinking_budget` is subtracted from the same pot: on Gemini 2.5/3.x the model's
    thinking tokens are drawn from max_output_tokens, so a 16k thinking budget leaves
    only ~49k for the answer. Because it is added HERE, `ratio` must describe the
    ANSWER ALONE — never answer-plus-thinking, or thinking is counted twice.

    `ratio` — this stage's measured answer/data multiple (see OUTPUT_RATIO above).
    Omit it only where no per-stage figure has been measured.

    Returns (over, est_total_output_tokens, limit)."""
    _in_ceil, out_ceil = model_ceilings(model)
    r = OUTPUT_RATIO if ratio is None else float(ratio)
    est_out = estimate_tokens(data_text, model) * r
    total   = est_out + int(thinking_budget or 0)
    limit   = out_ceil * OUTPUT_SAFETY
    return total > limit, int(total), int(limit)


def plan_token_batches(items, text_of, max_input_tokens=None, model=None,
                       reserve_output=0, hard_max_items=None):
    """Split `items` into batches whose combined estimated input tokens stay under
    the ceiling (minus a reserve for output). `text_of(item)` returns the item's
    prompt-contributing text. Always yields at least one item per batch so a
    single oversized item still makes progress (the call site handles its own
    truncation recovery). Deterministic: preserves input order."""
    in_ceil, _out = model_ceilings(model)
    budget = (max_input_tokens or in_ceil) - int(reserve_output or 0)
    budget = max(budget, 1)
    batches, cur, cur_tok = [], [], 0
    for it in items:
        t = max(1, len(text_of(it)) // 4)
        over_tokens = cur and (cur_tok + t) > budget
        over_count = hard_max_items and len(cur) >= hard_max_items
        if over_tokens or over_count:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(it)
        cur_tok += t
    if cur:
        batches.append(cur)
    return batches


# ── Shared limiter internals ────────────────────────────────────────────────
_sema = threading.BoundedSemaphore(_MAX_CONCURRENCY) if _MAX_CONCURRENCY > 0 else None


class _RateLimiter:
    """Thread-safe rolling-60s window limiter for requests-per-minute and
    tokens-per-minute. No-op when both limits are 0."""
    def __init__(self, rpm, tpm):
        self.rpm, self.tpm = rpm, tpm
        self._lock = threading.Lock()
        self._calls = deque()          # request timestamps
        self._tokens = deque()         # (timestamp, tokens)

    def acquire(self, est_tokens):
        if not self.rpm and not self.tpm:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                cutoff = now - 60.0
                while self._calls and self._calls[0] < cutoff:
                    self._calls.popleft()
                while self._tokens and self._tokens[0][0] < cutoff:
                    self._tokens.popleft()
                ok_rpm = (not self.rpm) or len(self._calls) < self.rpm
                cur_tok = sum(t for _, t in self._tokens)
                ok_tpm = (not self.tpm) or (cur_tok + est_tokens) <= self.tpm
                if ok_rpm and ok_tpm:
                    self._calls.append(now)
                    self._tokens.append((now, est_tokens))
                    return
                # sleep until the oldest entry ages out of the window
                waits = []
                if not ok_rpm and self._calls:
                    waits.append(self._calls[0] + 60.0 - now)
                if not ok_tpm and self._tokens:
                    waits.append(self._tokens[0][0] + 60.0 - now)
                sleep = min([w for w in waits if w > 0] or [0.1])
            time.sleep(min(max(sleep, 0.05), 2.0))


_rate_limiter = _RateLimiter(_RPM, _TPM)


@contextmanager
def _limited(est_tokens):
    """Acquire the shared concurrency slot + rate budget for one call."""
    _rate_limiter.acquire(est_tokens)
    if _sema is not None:
        _sema.acquire()
        try:
            yield
        finally:
            _sema.release()
    else:
        yield


def _is_transient(exc) -> bool:
    """True for errors worth retrying: quota/rate-limit, service unavailable,
    timeouts, and transient network resets. Content errors (bad JSON, schema
    rejection) are NOT transient and are handled by the caller as before."""
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
    """Extract a server-suggested Retry-After (seconds) if present, else None."""
    m = re.search(r"retry[-\s]?after['\":\s]+(\d+)", f"{exc}".lower())
    return int(m.group(1)) if m else None


def invoke_with_retry(kwargs, label="LLM", est_tokens=None, gen_client=None):
    """PUBLIC gateway entry for callers that build their own kwargs / need their
    own client and JSON recovery (e.g. exporter). Runs the network call through
    the shared concurrency + rate limiter and transient-error backoff, and
    returns the raw response object. Content/JSON handling stays with the caller."""
    if est_tokens is None:
        est_tokens = estimate_tokens(kwargs.get("contents", ""), kwargs.get("model"))
    return _generate_with_retry(kwargs, label, est_tokens, gen_client=gen_client)


def _generate_with_retry(kwargs, label, est_tokens, gen_client=None):
    """Call the model with the shared limiter + transient-error backoff. Only the
    network invocation is retried here; JSON/content validation stays in the
    caller so its existing chunked-retry semantics are preserved."""
    gc = gen_client or client
    attempt = 0
    while True:
        try:
            with _limited(est_tokens):
                return gc.models.generate_content(**kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt >= LLM_MAX_RETRIES:
                raise
            ra = _retry_after_seconds(exc)
            backoff = ra if ra is not None else min(_BACKOFF_CAP, _BACKOFF_BASE ** attempt)
            backoff += random.uniform(0, min(1.0, backoff))  # full-ish jitter
            print(f"[{label}] transient error (attempt {attempt + 1}/{LLM_MAX_RETRIES}): "
                  f"{type(exc).__name__}: {str(exc)[:160]} — retrying in {backoff:.1f}s")
            time.sleep(backoff)
            attempt += 1

# Models are config, not literals. The IR/compiler stay model-agnostic so a swap
# or bake-off (Gemini 3.5 Flash, Claude Haiku 4.5) is a one-line env change.
#   EXTRACTION_MODEL — the default for every call (Pipeline 1 extraction = call
#                      #1, Stage A classification = call #2). Stays 2.5 Flash.
#   STAGE_B_MODEL    — the Stage B rule-extraction call (the old AJV/Custom calls
#                      #3 & #4, now one IR call). Uses 3.5 Flash for stronger
#                      structured extraction.
#   SMALL_MODEL      — a cheap, low-latency, single-question check made from
#                      inside a REQUEST HANDLER, where a person is waiting on the
#                      response (admitting one spelling a tenant_admin typed).
#                      Deliberately the smallest model that can answer a closed
#                      yes/no about two short strings; the deterministic gates
#                      around it do the load-bearing work.
# Determinism comes from the compiler + temp 0 / seed, not from the model.
EXTRACTION_MODEL = os.getenv("KAVACHIO_EXTRACTION_MODEL", "gemini-2.5-flash")
STAGE_B_MODEL = os.getenv("KAVACHIO_STAGEB_MODEL", "gemini-3.5-flash")
SMALL_MODEL = os.getenv("KAVACHIO_SMALL_MODEL", "gemini-2.5-flash-lite")
DETERMINISTIC_SEED = int(os.getenv("KAVACHIO_LLM_SEED", "7"))

def clean_gemini_response(text: str):

    if not text:
        return ""

    text = text.strip()

    # Remove markdown fences
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```", "", text)

    return text.strip()


def _is_degenerate(text: str) -> bool:
    """True when the response is a single-character repetition loop (e.g. '0000…') —
    a known 2.5-flash failure mode under a large 'thinking' budget. Used to log a
    short summary instead of dumping the whole garbage blob."""
    s = (text or "").strip()
    if len(s) < 50:
        return False
    from collections import Counter
    _ch, n = Counter(s).most_common(1)[0]
    return n / len(s) >= 0.95


def call_gemini(prompt, label="LLM", temperature=None, seed=None,
                response_schema=None, model=None, max_output_tokens=None,
                thinking_budget=None):

    _model = model or EXTRACTION_MODEL
    print(f"\n[{label}] Calling Gemini...")

    # ── Pre-flight token budget ──────────────────────────────────────────
    # Estimate input size and clamp the requested output to the model ceiling so
    # we never *ask* for more than the model can return. If input + output +
    # thinking would exceed the input ceiling, raise OversizeError so the caller
    # can split the work rather than send a request that would truncate.
    max_output_tokens = clamp_max_output_tokens(max_output_tokens, _model)
    over, est_input, in_ceil = should_chunk(
        prompt, max_output_tokens or 0, thinking_budget or 0, _model)
    if over:
        raise OversizeError(
            f"[{label}] request ~{est_input} tokens exceeds input ceiling "
            f"{in_ceil} for {_model}; split before sending.",
            input_tokens=est_input, ceiling=in_ceil)

    kwargs = {"model": _model, "contents": prompt}

    # Build the generation config. temperature=0 + a fixed seed make decoding
    # reproducible; response_schema constrains the output to the IR shape so the
    # structure is enforced by the API rather than coaxed by prose;
    # max_output_tokens raises the ceiling so large batched JSON isn't truncated.
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
    # On Gemini 2.5/3.x the "thinking" tokens are drawn from the SAME budget as
    # max_output_tokens — runaway thinking (e.g. 31k thoughts) starves the actual
    # output and truncates the JSON mid-string, so most rules are silently lost; a
    # large thinking budget is also the trigger for the rare repeat-loop ('0000…')
    # output. Capping (or thinking_budget=0 to disable) guarantees room for output.
    if thinking_budget is not None:
        config["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget
        )
    if config:
        kwargs["config"] = config

    try:
        # Routes through the shared gateway: concurrency + rate limiter and
        # transient-error backoff. Content/JSON validation stays below so the
        # callers' existing chunked-retry behaviour is unchanged.
        response = _generate_with_retry(kwargs, label, est_input)
    except OversizeError:
        raise
    except Exception as exc:
        # If the SDK/model rejects the structured-output config (older SDK or a
        # model without response_schema support), retry once without it so the
        # call degrades to prompt-enforced JSON instead of hard-failing.
        if response_schema is not None:
            print(f"[{label}] response_schema rejected ({exc}); "
                  f"retrying without structured output.")
            config.pop("response_mime_type", None)
            config.pop("response_schema", None)
            kwargs["config"] = config or None
            if not config:
                kwargs.pop("config", None)
            response = _generate_with_retry(kwargs, label, est_input)
        else:
            raise

    # ── Token usage (input / processing / output) ────────────────────────
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        input_tokens   = getattr(usage, "prompt_token_count", None)
        output_tokens  = getattr(usage, "candidates_token_count", None)
        thought_tokens = getattr(usage, "thoughts_token_count", None)
        cached_tokens  = getattr(usage, "cached_content_token_count", None)
        total_tokens   = getattr(usage, "total_token_count", None)
        print(
            f"[{label}] TOKENS  input={input_tokens}  "
            f"processing(thoughts)={thought_tokens}  "
            f"output={output_tokens}  cached={cached_tokens}  "
            f"total={total_tokens}"
        )

    # ── Truncation detection ─────────────────────────────────────────────
    # A truncated answer sometimes still parses as valid JSON — the model closes
    # its open braces on the way out — so the json.loads() check at the bottom of
    # this function cannot be the only guard. finish_reason is the reliable
    # signal. Raising here makes the CALLER's chunked fallback engage instead of
    # silently accepting a partial answer and losing the rules in the tail.
    # (mapper.py and exporter.py already check this; the shared gateway did not.)
    _cands = getattr(response, "candidates", None) or []
    _finish = str(getattr(_cands[0], "finish_reason", "")) if _cands else ""
    if "MAX_TOKENS" in _finish:
        raise Exception(
            f"[{label}] hit MAX_TOKENS (answer truncated) — caller should split")

    text = getattr(response, "text", "")

    cleaned = clean_gemini_response(text)

    print(f"\n[{label}] RAW RESPONSE:")
    print("=" * 80)
    print(cleaned[:400])
    print("=" * 80)

    if not cleaned:
        raise Exception("Gemini returned empty response")

    # Validate JSON before returning
    try:
        json.loads(cleaned)
    except Exception as e:
        if _is_degenerate(cleaned):
            # Model looped on a single character (e.g. '0000…'). Log a SHORT
            # summary instead of dumping the whole blob; the caller will retry.
            print(f"\n[{label}] DEGENERATE OUTPUT: {len(cleaned)} chars, ~all "
                  f"'{cleaned.strip()[:1]}' (thinking-loop). Retrying in chunks.")
            raise Exception("Gemini returned degenerate (repeated-character) output")
        print("\nINVALID JSON FROM GEMINI:")
        print(cleaned[:1000])
        raise Exception(f"Gemini returned invalid JSON: {str(e)}")

    return cleaned