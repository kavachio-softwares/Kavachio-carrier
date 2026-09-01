"""
pipeline_log.py
───────────────
A single human-readable file recording WHY the pipeline made each batching
decision, and what size it chose.

The console already prints what happened; what it does not give you is the
decision trail in one place, in order, with the numbers behind each choice. That
matters here because nearly every cost/quality question about this pipeline is
really the question "why did that stage split into N calls?" — and the answer is
different each time (output budget, a detected dilution, a dropped intent, a
cache hit).

One line per decision, aligned so it reads as a table:

  15:02:11 | run=a3f2 | CALL2 | ATTEMPT   | 55 clause(s) in ONE call | est answer 7,500 of 45,875 tok (16%)
  15:02:52 | run=a3f2 | CALL2 | DILUTED   | clauses [26, 25] rule-bearing in a small batch | single call rejected them
  15:02:52 | run=a3f2 | CALL2 | FALLBACK  | 55 item(s) -> 5 batch(es) of 12 | spot-check failed

Fail-open: every function swallows its own errors. A logging problem must never
be able to affect an upload.

Location: KAVACHIO_DECISION_LOG (default ./pipeline_decisions.log).
Disable:  KAVACHIO_DECISION_LOG=0
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

_LOCK = threading.Lock()
_RUN = uuid.uuid4().hex[:4]      # distinguishes concurrent uploads in one file
# {run_id: {stage: seconds}} — wall-clock per stage, accumulated so a stage entered
# more than once (a fallback re-entering Call 2) reports its TOTAL, not its last visit.
_TIMES: dict = {}
_STARTED: dict = {}


def _path():
    p = os.getenv("KAVACHIO_DECISION_LOG", "pipeline_decisions.log")
    return None if p == "0" else p


def new_run(label=""):
    """Start a new run id and write a header. Called once per contract upload."""
    global _RUN
    _RUN = uuid.uuid4().hex[:4]
    _write_raw("")
    _write_raw("=" * 108)
    _write_raw(f"RUN {_RUN}  started {datetime.now():%Y-%m-%d %H:%M:%S}"
               + (f"  |  {label}" if label else ""))
    _write_raw("=" * 108)
    _TIMES[_RUN] = {}
    _STARTED[_RUN] = time.time()
    return _RUN


@contextmanager
def stage(name):
    """Time one pipeline stage. Re-entrant — a stage entered twice accumulates.

    Wall-clock deliberately: the question these numbers answer is "where did the
    7 minutes go", and for this pipeline the answer is nearly always "waiting on a
    model call". Time spent blocked is the thing worth seeing.
    """
    t0 = time.time()
    try:
        yield
    finally:
        try:
            d = _TIMES.setdefault(_RUN, {})
            d[name] = d.get(name, 0.0) + (time.time() - t0)
        except Exception:
            pass


def finish_run():
    """Write the per-stage timing summary. Called once when a contract finishes."""
    try:
        times = _TIMES.pop(_RUN, {}) or {}
        total = time.time() - _STARTED.pop(_RUN, time.time())
        if not times:
            return
        _write_raw("-" * 108)
        _write_raw(f"TIMING run={_RUN}   total {total:.1f}s ({total/60:.1f} min)")
        # Slowest first — the top line is where the time actually went.
        for name, secs in sorted(times.items(), key=lambda kv: -kv[1]):
            share = 100 * secs / total if total else 0
            _write_raw(f"   {name:<26}{secs:>8.1f}s {share:>5.1f}%  {'#' * int(share / 2.5)}")
        _write_raw(f"   {'(other / overlap)':<26}{total - sum(times.values()):>8.1f}s")
        _write_raw("-" * 108)
    except Exception:
        pass


def log(stage, event, what, why=""):
    """One decision.

    stage  CALL1 | CALL2 | CALL3 | CACHE | TEMPLATE   — which pipeline step
    event  ATTEMPT | OK | SPOTCHECK | DILUTED | FALLBACK | DROPPED | HIT | MISS
    what   the concrete outcome, with numbers
    why    the reason the decision was taken, with the numbers behind it
    """
    line = (f"{datetime.now():%H:%M:%S} | run={_RUN} | {str(stage):<8} | "
            f"{str(event):<9} | {what}")
    if why:
        line += f" | {why}"
    _write_raw(line)


def _write_raw(line):
    p = _path()
    if not p:
        return
    try:
        with _LOCK:
            with open(p, "a") as f:
                f.write(line + "\n")
    except Exception:
        pass          # logging must never break an upload
