"""Scripted, no-pay scenarios driven through the production MCP tool layer.

Used by `main/controlled_driver.mjs` so `run_agent_arm._execute` can be
exercised end to end (real subprocess, real tools, real validator, real
outcome record) without a paid model call — the review-4 guide §2.2 requires
the recovery/terminal-state regressions to go through the production
execution path, not helper-level checks.

Scenarios (each writes `model_response.json` into the run folder, exactly the
record `_execute` reads for a local-record driver):

  acquire_error_unrecovered
      every acquisition raises inside the tool; nothing valid is acquired and
      the driver exits normally -> terminal case 1 (failed / tool_failure).
  error_then_ok_same_target
      the first acquisition of the target candidate is forced to raise once,
      the retry succeeds from the seeded cache -> terminal case 2 (succeeded,
      error history kept, recovery proven by event order).
  ok_then_error_incomplete
      an acquisition completes as a data fact (dead proxy: no metadata), then
      a later forced key error is never recovered and the field stays
      unsettled -> terminal case 3 (failed; never "其后恢复").
  independent_evidence_ok
      acquiring candidate A is forced to fail and is never retried, candidate
      B succeeds from the cache and settles the asked field -> terminal case 4
      (succeeded; completion and the unrecovered exception reported
      separately).

Everything is offline: successes are served from the cache the calling test
seeds; failures are forced in-process. No network, no model, no AGH session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))
sys.path.insert(0, str(ROOT / "prototype"))

# `main/core.py` puts `prototype/` ahead of `main/` on sys.path, and prototype
# ships its own `mcp_server.py`; select the current production MCP explicitly.
sys.path.insert(0, str(ROOT / "main"))
import mcp_server  # noqa: E402

import core  # noqa: E402


def _force_once(tool_module, attribute, exception):
    """Force `tool_module.attribute` to raise once, then restore the original."""
    original = getattr(tool_module, attribute)
    state = {"raised": False}

    def wrapper(*args, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            raise exception
        return original(*args, **kwargs)

    setattr(tool_module, attribute, wrapper)
    return lambda: setattr(tool_module, attribute, original)


def _answer_from_validator(mcp, state):
    """The honest answer for the record: what the validator itself will deliver."""
    task = mcp._task_from_state(state)
    question_id = core.classify_question(state.get("user_question"))
    verdict = core.verdict_for_question(core.active_records(state["acquired"]),
                                        question_id, None, task)
    return json.dumps({"conclusion": verdict["verdict"],
                       "stop_reason": "controlled driver (no model called)",
                       "stations_used": len(state["acquired"])})


def main():
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    run_id = sys.argv[1]
    scenario = sys.argv[2] if len(sys.argv) > 2 else "acquire_error_unrecovered"
    mcp_server.bind(run_id)
    mcp = mcp_server
    state = mcp._load()
    eligible = list((state.get("discovery") or {}).get("eligible") or [])
    if not eligible:
        print(json.dumps({"fatal": "no candidates in run state"}))
        sys.exit(1)

    fatal = None
    # Candidate roles by id suffix — the seeded test runs provide a cached
    # healthy station (`cand-ok`) and a station whose requests fail at the data
    # level (`cand-dead`: dead proxy, so the tool succeeds with no data).
    def _pick(*suffixes, fallback):
        for suffix in suffixes:
            for candidate in eligible:
                if str(candidate["candidate_id"]).endswith(suffix):
                    return candidate
        return fallback(eligible)

    ok_candidate = _pick("ok", fallback=lambda e: e[0])
    dead_candidate = _pick("dead", fallback=lambda e: e[-1])
    if scenario == "acquire_error_unrecovered":
        # Case 1: the interface itself fails; nothing is acquired.
        original = mcp.core.acquire
        mcp.core.acquire = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("controlled interface failure: acquisition layer"))
        try:
            mcp.acquire_station(ok_candidate["candidate_id"])
        finally:
            mcp.core.acquire = original
    elif scenario == "error_then_ok_same_target":
        # Case 2: fail once on the healthy candidate, then retry the SAME one.
        target = ok_candidate["candidate_id"]
        restore = _force_once(mcp.core, "acquire",
                              RuntimeError("controlled interface failure: once"))
        try:
            mcp.acquire_station(target)
        finally:
            restore()
        mcp.acquire_station(target)
    elif scenario == "ok_then_error_incomplete":
        # Case 3: a data-level no-data fact first (tool ok), then an interface
        # failure that is never recovered while the field stays unsettled.
        mcp.acquire_station(dead_candidate["candidate_id"])
        original = mcp.core.acquire
        mcp.core.acquire = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("controlled interface failure: after the ok"))
        try:
            mcp.acquire_station(ok_candidate["candidate_id"])
        finally:
            mcp.core.acquire = original
    elif scenario == "independent_evidence_ok":
        # Case 4: acquiring the dead candidate is forced to fail and is never
        # retried; the healthy candidate settles the field independently.
        original = mcp.core.acquire
        mcp.core.acquire = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("controlled interface failure: candidate A"))
        try:
            mcp.acquire_station(dead_candidate["candidate_id"])
        finally:
            mcp.core.acquire = original
        mcp.acquire_station(ok_candidate["candidate_id"])
    else:
        print(json.dumps({"fatal": f"unknown scenario: {scenario}"}))
        sys.exit(2)

    state = mcp._load()
    record = {
        "RUN": run_id, "RUN_ID": run_id, "variant": "controlled",
        "stated": False, "model": "controlled (no model)",
        "sessionId": None, "fatal": fatal, "failure_kind": fatal,
        "status": "failed" if fatal else "succeeded",
        "answer": _answer_from_validator(mcp, state),
        "transcript": [], "execution": None, "elapsed_ms": 0,
        "scenario": scenario,
    }
    run_folder = Path(mcp.FOLDER)
    (run_folder / "model_response.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps({"RUN": run_id, "scenario": scenario,
                      "status": record["status"]}))


if __name__ == "__main__":
    main()
