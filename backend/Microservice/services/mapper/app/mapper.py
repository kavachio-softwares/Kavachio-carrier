"""LLM mapping engine (generate_mapping_multi + Gemini helpers). Owned by
mapper-service. Pure utils come from the shared mapping_utils module."""
from __future__ import annotations

from mapping_utils import *  # noqa: F401,F403  (pure helpers + constants)
import mapping_utils as _mu  # ensure every name (incl. underscored) is available
globals().update({k: v for k, v in vars(_mu).items() if not k.startswith('__')})

SUCCESS_THRESHOLD = 0.65

LIKELY_THRESHOLD = 0.45

TOP_N_CANDIDATES = 10

def _build_candidates_prompt(
    headers: list[str],
    samples: dict[str, list[str]],
    pins: dict[str, str],
    mappable_model: dict,
) -> str:
    """Single unified prompt: per source column, return TOP_N_CANDIDATES
    ranked canonical fields with confidence + reason. Also carries the
    role-prefix / composite / attribute / pin hints that used to live on
    the per-canonical prompt — so the spec we derive from these candidates
    is as accurate as the previous two-call pipeline."""
    role_hint = (
        "\nROLE-PREFIXED FIELDS: When the same logical field is filled by\n"
        "different real-world parties, the data model exposes role-prefixed\n"
        "variants:\n"
        "  - Agency*/Broker* headers → *_agency variant (agency_legal_name,\n"
        "    agency_address_line1, …).\n"
        "  - Insured* headers → *_insured variant (insured_legal_name, …).\n"
        "  - Company* headers referring to the writing carrier → *_carrier\n"
        "    if available, else the base field.\n"
        "Never silently drop agency/broker columns.\n"
    )
    composite_hint = (
        "\nCOMPOSITE FIELDS: legal_name is often split across FirstName +\n"
        "LastName columns. List the canonical (legal_name / legal_name_insured\n"
        "/ legal_name_agency) as a HIGH-confidence candidate on BOTH source\n"
        "columns; the apply layer combines them.\n"
    )
    attributes_hint = (
        "\nNEVER invent canonical field keys. If no field in the provided\n"
        "data model is a reasonable match for a source column, return ONLY\n"
        "low-confidence (<0.30) candidates drawn from the existing model —\n"
        "do not fabricate keys such as `attribute_value_*`, `extra_*`, or\n"
        "anything else not in the canonical list.\n"
    )
    join_hint = (
        "\nJOIN KEYS (policy_number, program_name, tenant_name,\n"
        "external_policy_number): if the same column meaning appears in\n"
        "multiple sheets (POL/UNT/PRM), pick the same canonical key as the\n"
        "top candidate in each sheet — do NOT suffix.\n"
    )
    pins_hint = ""
    if pins:
        pins_hint = (
            "\nPINNED MAPPINGS (heuristic — must be the TOP candidate with\n"
            "confidence 1.0 for the listed source column):\n"
            + json.dumps({src: canon for canon, src in pins.items()}, indent=2)
            + "\n"
        )

    sample_block = json.dumps(
        {h: samples.get(h, [])[:MAX_SAMPLES] for h in headers},
        indent=2, default=str,
    )
    return (
        "You map insurance bordereaux (BDX) Excel COLUMN HEADERS to a\n"
        "canonical data model. Headers are qualified 'SheetName :: ColumnHeader'.\n"
        f"\nFor EACH source column, return the TOP {TOP_N_CANDIDATES} canonical\n"
        "fields ranked by goodness of fit. Use BOTH the header name and the\n"
        "sample values to decide.\n"
        "\nConfidence scale (0-1): >=0.85 unambiguous; 0.65-0.85 strong;\n"
        "0.45-0.65 likely; 0.20-0.45 weak; 0.0 no fit. Reserve high\n"
        f"confidence carefully. Always return exactly {TOP_N_CANDIDATES}\n"
        "items per column — pad with weak candidates if needed.\n"
        + role_hint + composite_hint + attributes_hint + join_hint + pins_hint
        + "\nUse canonical field KEYS exactly as they appear in the data model.\n"
        "Output MUST be COMPACT JSON — NO explanations, NO reason fields,\n"
        "NO whitespace between tokens. Two keys only per candidate object:\n"
        '`c` (canonical field key) and `s` (confidence score 0-1).\n'
        "\nReturn EXACTLY this shape, nothing else:\n"
        '  { "Sheet :: Column": [\n'
        '      {"c":"policy_number","s":0.95},\n'
        '      {"c":"external_policy_number","s":0.42},\n'
        "      …\n"
        "    ],\n"
        "    … (one entry per source column) }\n\n"
        f"Canonical data model (BDX-mappable fields):\n{json.dumps(mappable_model)}\n\n"
        f"Source columns with sample values:\n{sample_block}\n"
    )

_MAX_OUTPUT_TOKENS = 65536  # Flash 2.5 ceiling

_FALLBACK_BATCH = 30        # only used if the single call truncates

def _is_acceptable_canonical(cf: str) -> bool:
    """Whitelist: canonical keys that actually exist in the data model,
    plus role-prefixed variants. The attribute_value_* / unit_attribute_*
    catch-all family is INTENTIONALLY rejected — if the LLM can't find a
    real model field, the column stays unmapped for the user to handle.

    Also intentionally rejected: bare `policy_attributes` columns
    (`attribute_value`, `attribute_key`, `scope`, `value_type`). They only
    make sense as a triple — picking one of them in isolation creates a row
    that violates the NOT NULL constraints on the other two.
    """
    BARE_ATTR_KEYS = {"attribute_value", "attribute_key", "scope", "value_type"}
    if cf in BARE_ATTR_KEYS:
        return False
    if cf in CANONICAL_FIELDS:
        return True
    base = re.sub(r"_(insured|agency|carrier)$", "", cf)
    return base in CANONICAL_FIELDS

def _parse_candidates_response(
    raw: dict, valid_headers: set[str],
) -> dict[str, list[dict]]:
    """Accept the compact `{c, s}` shape AND the verbose
    `{canonical, confidence, reason}` shape for forward/backward compat."""
    out: dict[str, list[dict]] = {}
    for src, lst in (raw or {}).items():
        if src not in valid_headers or not isinstance(lst, list):
            continue
        cleaned: list[dict] = []
        seen: set[str] = set()
        for item in lst:
            if not isinstance(item, dict):
                continue
            cf = item.get("c") or item.get("canonical")
            if not isinstance(cf, str) or not _is_acceptable_canonical(cf):
                continue
            if cf in seen:
                continue
            seen.add(cf)
            raw_conf = item.get("s") if "s" in item else item.get("confidence", 0.0)
            try:
                conf = float(raw_conf or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            cleaned.append({
                "canonical": cf,
                "confidence": max(0.0, min(1.0, conf)),
                "reason": (item.get("reason") or "")[:140],
            })
            if len(cleaned) >= TOP_N_CANDIDATES:
                break
        if cleaned:
            cleaned.sort(key=lambda c: c["confidence"], reverse=True)
            out[src] = cleaned
    return out

def _gemini_candidates_call(
    client, headers: list[str], samples: dict[str, list[str]],
    pins: dict[str, str], mappable_model: dict,
) -> dict[str, list[dict]] | None:
    """One Gemini round trip. Returns parsed candidates dict, or None on
    failure / truncation (caller may retry in smaller chunks)."""
    prompt = _build_candidates_prompt(headers, samples, pins, mappable_model)
    log.info("Calling Gemini for top-%d candidates over %d headers (prompt=%d chars)…",
             TOP_N_CANDIDATES, len(headers), len(prompt))
    t0 = time.time()
    try:
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={
                "response_mime_type": "application/json",
                "max_output_tokens": _MAX_OUTPUT_TOKENS,
            },
        )
    except Exception as e:
        log.error("Gemini candidates call failed: %s", e)
        return None

    # Diagnostics: finish_reason tells us if Gemini stopped early (MAX_TOKENS,
    # SAFETY, RECITATION, OTHER…) so a truncated JSON can be distinguished
    # from a model bug. usage_metadata gives token accounting.
    finish_reason: Any = "?"
    safety_ratings: Any = None
    cands = getattr(resp, "candidates", None) or []
    if cands:
        finish_reason = getattr(cands[0], "finish_reason", "?")
        safety_ratings = getattr(cands[0], "safety_ratings", None)
    usage = getattr(resp, "usage_metadata", None)

    text = (resp.text or "").strip()
    log.info(
        "Gemini responded in %.2fs (%d chars) finish=%s usage=%s",
        time.time() - t0, len(text), finish_reason, usage,
    )

    raw = _lenient_json_loads(text)
    if raw is None:
        # Persist the raw payload so we can post-mortem WHY recovery failed.
        try:
            import tempfile
            fd, dump = tempfile.mkstemp(
                prefix="mapper_unparseable_", suffix=".json", dir="/tmp",
            )
            with os.fdopen(fd, "w") as f:
                f.write(text)
            log.error(
                "Could not recover any JSON. finish=%s len=%d. Dumped raw "
                "response to %s. First 200 chars: %r",
                finish_reason, len(text), dump, text[:200],
            )
        except Exception:
            log.error(
                "Could not recover any JSON. finish=%s len=%d. First 200 "
                "chars: %r", finish_reason, len(text), text[:200],
            )
        if safety_ratings:
            log.error("Safety ratings on the failed response: %s", safety_ratings)
        return None

    parsed = _parse_candidates_response(raw, set(headers))
    log.info("Parsed %d/%d source columns with at least one candidate",
             len(parsed), len(headers))
    return parsed

def _call_llm_candidates(
    headers: list[str],
    samples: dict[str, list[str]],
    pins: dict[str, str] | None = None,
) -> dict[str, list[dict]]:
    """Ask Gemini for ranked top-N candidates per source column.

    Strategy: ONE call covers all source columns. Only if that response
    truncates or fails to parse do we retry in chunks of `_FALLBACK_BATCH`.
    Returns `{ "Sheet :: Column": [{canonical, confidence, reason}, ...] }`.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or not headers:
        if not api_key:
            log.warning("No GEMINI_API_KEY — returning empty candidates.")
        return {}

    from google import genai
    client = genai.Client(api_key=api_key)
    mappable_model = {k: DATA_MODEL[k] for k in MAPPABLE_FIELDS}

    parsed = _gemini_candidates_call(
        client, headers, samples, pins or {}, mappable_model,
    )
    if parsed is not None:
        log.info("Got candidates for %d/%d source columns (single call)",
                 len(parsed), len(headers))
        return parsed

    # Fallback: chunk only when the single call hits a problem (truncation,
    # parse failure, transient error). This is intentionally rare.
    log.warning("Single call failed/truncated — falling back to chunked calls of %d.",
                _FALLBACK_BATCH)
    out: dict[str, list[dict]] = {}
    for i in range(0, len(headers), _FALLBACK_BATCH):
        chunk = headers[i : i + _FALLBACK_BATCH]
        chunk_samples = {h: samples.get(h, []) for h in chunk}
        chunk_pins = {c: s for c, s in (pins or {}).items() if s in chunk}
        chunk_parsed = _gemini_candidates_call(
            client, chunk, chunk_samples, chunk_pins, mappable_model,
        )
        if chunk_parsed:
            out.update(chunk_parsed)
    log.info("Got candidates for %d/%d source columns (chunked)", len(out), len(headers))
    return out

def _derive_llm_mapping(
    candidates_by_source: dict[str, list[dict]],
) -> dict[str, dict[str, Any]]:
    """Invert per-source candidates into the canonical→source structure that
    `_bucketize` and `apply_spec_multi` already consume.

    Rules:
      - Each source's #1 candidate with confidence ≥ LIKELY_THRESHOLD becomes
        that source's vote.
      - If a join key (policy_number, program_name, …) is voted by sources in
        multiple sheets, emit per-sheet canonical keys with the `__<sheet>`
        suffix that `_call_llm` used to produce (e.g. `policy_number__unt`).
      - If two or more sources in the SAME sheet vote for the same canonical,
        treat them as a composite source (list); confidence = mean.
    """
    votes: dict[str, list[tuple[str, float]]] = {}
    for src, cands in (candidates_by_source or {}).items():
        if not cands:
            continue
        top = cands[0]
        canon = top.get("canonical")
        try:
            conf = float(top.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if not canon or conf < LIKELY_THRESHOLD:
            continue
        votes.setdefault(canon, []).append((src, conf))

    out: dict[str, dict[str, Any]] = {}

    def _sheet_token(src: str) -> str:
        sheet = src.split(SHEET_SEP, 1)[0]
        return re.sub(r"[^a-z0-9]+", "_", sheet.lower()).strip("_") or "sheet"

    for canon, vlist in votes.items():
        sheets_seen = {v[0].split(SHEET_SEP, 1)[0] for v in vlist}

        if canon in JOIN_KEY_FIELDS and len(sheets_seen) > 1:
            # One entry per (canonical, sheet) so each sheet's apply_spec can
            # find its own column.
            for src, conf in vlist:
                tok = _sheet_token(src)
                # First sheet keeps the bare canonical key (matches legacy);
                # subsequent sheets get the suffix.
                if canon not in out:
                    out[canon] = {"source": src, "confidence": conf}
                else:
                    out[f"{canon}__{tok}"] = {"source": src, "confidence": conf}
            continue

        if len(vlist) > 1:
            srcs = [s for s, _ in vlist]
            avg = sum(c for _, c in vlist) / len(vlist)
            out[canon] = {"source": srcs, "confidence": avg}
        else:
            src, conf = vlist[0]
            out[canon] = {"source": src, "confidence": conf}

    return out

def _bucketize(headers: list[str], llm_result: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Convert LLM result into the canonical response shape, per-sheet aware."""
    # spec is now grouped per sheet, since one canonical field may have one
    # source per sheet (the join-key case).
    spec_by_sheet: dict[str, dict[str, Any]] = {}
    source_to_mappings: dict[str, list[dict[str, Any]]] = {}

    for raw_field, payload in llm_result.items():
        # Strip __sheet suffix and find which sheet it applies to.
        if "__" in raw_field:
            field, _, sheet_suffix = raw_field.partition("__")
        else:
            field = raw_field
            sheet_suffix = None

        src = payload["source"]
        conf = payload["confidence"]
        # Determine sheet(s) from the source string(s)
        srcs = src if isinstance(src, list) else [src]
        sheets = sorted({s.split(SHEET_SEP, 1)[0] for s in srcs})
        # Place into spec per-sheet
        for sheet in sheets:
            spec_by_sheet.setdefault(sheet, {})[field] = src
        # Track bindings for bucket display
        for one_src in srcs:
            source_to_mappings.setdefault(one_src, []).append(
                {"canonical": field, "score": round(conf, 3)}
            )

    successful, likely, unsuccessful = [], [], []
    for src in headers:
        bindings = source_to_mappings.get(src, [])
        if not bindings:
            unsuccessful.append({"source": src, "canonicals": [], "score": 0.0})
            continue
        bindings.sort(key=lambda b: -b["score"])
        best = bindings[0]
        entry = {
            "source": src,
            "canonical": best["canonical"],
            "canonicals": [b["canonical"] for b in bindings],
            "bindings": bindings,
            "score": best["score"],
        }
        if best["score"] >= SUCCESS_THRESHOLD:
            successful.append(entry)
        elif best["score"] >= LIKELY_THRESHOLD:
            likely.append(entry)
        else:
            unsuccessful.append(entry)

    flat_spec = {f: s for sheet_spec in spec_by_sheet.values() for f, s in sheet_spec.items()}
    canonical_unmapped = sorted(CANONICAL_FIELDS - set(flat_spec.keys()))

    return {
        "spec": flat_spec,                 # legacy flat view (one source per canonical)
        "spec_by_sheet": spec_by_sheet,    # NEW: per-sheet, supports join keys
        "successful": successful,
        "likely": likely,
        "unsuccessful": unsuccessful,
        "canonical_unmapped": canonical_unmapped,
    }

def generate_mapping_multi(sheets: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Map headers across ALL sheets in a single LLM call, per-sheet aware."""
    log.info("=== generate_mapping_multi: %d sheets ===", len(sheets))
    qualified_headers: list[str] = []
    samples: dict[str, list[str]] = {}
    for sheet_name, df in sheets.items():
        for col in df.columns:
            q = qualify(str(sheet_name), str(col))
            qualified_headers.append(q)
            vals = df[col].dropna().astype(str).head(MAX_SAMPLES).tolist()
            samples[q] = vals

    log.info("Total qualified headers: %d", len(qualified_headers))

    # Heuristic pre-pass — pins guaranteed mappings.
    pins: dict[str, str] = {}
    for h in qualified_headers:
        canonical = _heuristic_pin(h, samples.get(h, []))
        if canonical:
            pins[canonical] = h
    if pins:
        log.info("Pre-pinned %d obvious mappings via heuristics: %s", len(pins), list(pins))

    # Cross-tenant column cache pre-pass. Any source column that's already
    # been mapped (by anyone, any tenant) with similar samples is served
    # straight from cache. Only the cache MISSES go to the LLM.
    cached_candidates: dict[str, list[dict]] = {}
    cache_miss_headers: list[str] = []
    try:
        from db import SessionLocal
        with SessionLocal() as s:
            for h in qualified_headers:
                if h in pins.values():
                    # Heuristic pin always wins — still LLM-call it so the
                    # candidates list has alternatives for the UI picker.
                    cache_miss_headers.append(h)
                    continue
                sheet, _, col = h.partition(SHEET_SEP)
                fp = sample_fingerprint(samples.get(h) or [])
                hit = cache_lookup(s, sheet, col, fp)
                if hit:
                    canon, conf = hit
                    cached_candidates[h] = [{
                        "canonical": canon,
                        "confidence": conf,
                        "reason": "cache hit",
                    }]
                else:
                    cache_miss_headers.append(h)
    except Exception as e:
        log.warning("Cache lookup skipped: %s", e)
        cache_miss_headers = list(qualified_headers)

    log.info("Cache: %d/%d source columns served from cache (LLM will see %d)",
             len(cached_candidates), len(qualified_headers), len(cache_miss_headers))

    # ONE Gemini call covers only the cache-miss columns. If every column
    # is already cached, we skip the LLM entirely.
    if cache_miss_headers:
        miss_samples = {h: samples.get(h, []) for h in cache_miss_headers}
        miss_pins = {c: s for c, s in pins.items() if s in cache_miss_headers}
        llm_candidates = _call_llm_candidates(cache_miss_headers, miss_samples, miss_pins)
    else:
        log.info("All %d headers cache-hit — skipping LLM entirely.",
                 len(qualified_headers))
        llm_candidates = {}

    candidates_by_source: dict[str, list[dict]] = {**cached_candidates, **llm_candidates}
    # Inject heuristic pins as forced top candidates so they're always #1
    # even if the model ranked something else higher.
    for canonical, src in pins.items():
        existing = candidates_by_source.get(src, [])
        existing = [c for c in existing if c.get("canonical") != canonical]
        candidates_by_source[src] = (
            [{"canonical": canonical, "confidence": 1.0,
              "reason": "Heuristic-pinned"}] + existing
        )[:TOP_N_CANDIDATES]

    llm = _derive_llm_mapping(candidates_by_source)
    # Force-merge pins (defensive — derivation above should cover them).
    for canonical, src in pins.items():
        if canonical not in llm:
            llm[canonical] = {"source": src, "confidence": 1.0}

    out = _bucketize(qualified_headers, llm)

    # NOTE: the `attribute_value_*` policy_attributes catch-all was removed —
    # if a column has no real match in the canonical model we leave it
    # unmapped so the user explicitly handles it.

    out["samples"] = samples
    out["sheets"] = list(sheets.keys())
    out["candidates_by_source"] = candidates_by_source

    # Write fresh mappings back to the cache so the NEXT upload benefits.
    # Only write LLM-derived hits (cached_candidates already came from cache).
    try:
        from db import SessionLocal
        with SessionLocal() as s:
            for h, cands in llm_candidates.items():
                if not cands:
                    continue
                top = cands[0]
                if not top.get("canonical"):
                    continue
                if float(top.get("confidence", 0)) < LIKELY_THRESHOLD:
                    continue
                sheet, _, col = h.partition(SHEET_SEP)
                fp = sample_fingerprint(samples.get(h) or [])
                cache_store(s, sheet, col, fp,
                            top["canonical"], top["confidence"],
                            source="llm")
            s.commit()
    except Exception as e:
        log.warning("Cache write skipped: %s", e)

    log.info(
        "=== generate_mapping_multi done: success=%d likely=%d weak=%d ===",
        len(out["successful"]), len(out["likely"]), len(out["unsuccessful"]),
    )
    return out
