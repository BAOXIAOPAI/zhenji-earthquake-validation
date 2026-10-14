"""One paid AGH smoke test on a free-input task (no preset demo case).

This is the last step of the second review's P0-1 acceptance condition: the
no-paid interface checks pass, so exactly one real AGH session is run against a
task the user wrote. It exercises the fixed free-input acquisition path
(`demo_case_ref=None`), the question-specific answer fields and the validator.

It is deliberately small: one task, one session, one window. The task is frozen
before any network or model action, and the run is recorded under its own run id.

Usage:
  python main/smoke_free_input.py ["<question>"] [--window-start ... --window-end ...]
                                  [--region orcas] [--band 1 12]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import run_agent_arm  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402

DEFAULT = {
    "window_start": "2025-03-03T13:02:20",
    "window_end": "2025-03-03T13:04:20",
    "region": "orcas",
    "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
    "band": [1.0, 12.0],
}


def _args(argv):
    options = dict(DEFAULT)
    rest = [a for a in argv if not a.startswith("--")]
    for i, token in enumerate(argv):
        if token == "--window-start":
            options["window_start"] = argv[i + 1]
        elif token == "--window-end":
            options["window_end"] = argv[i + 1]
        elif token == "--region":
            options["region"] = argv[i + 1]
        elif token == "--band":
            options["band"] = [float(argv[i + 1]), float(argv[i + 2])]
    if rest:
        options["question"] = " ".join(rest)
    return options


def main():
    options = _args(sys.argv[1:])
    core.fdsn.set_cache(core.CACHE)
    raw = {"window_start": options["window_start"],
           "window_end": options["window_end"],
           "question": options["question"], "region": options["region"]}
    parsed = task_input.build(raw)
    if not parsed.get("ready"):
        raise SystemExit(f"task not ready: {parsed.get('missing')}")
    spec = dict(parsed["structured"])
    spec["target_band_hz"] = list(options["band"])
    frozen = taskmod.task_from_spec(spec, user_question=raw["question"])
    taskmod.freeze(frozen)
    print(json.dumps({"task_id": frozen["task_id"],
                      "window": [frozen["window_start"], frozen["window_end"]],
                      "region": frozen["region"], "band": frozen["target_band_hz"],
                      "goal": frozen["method_goal"],
                      "question": frozen["user_question"]},
                     ensure_ascii=False, indent=2), flush=True)

    row = run_agent_arm.run_user_task(frozen, raw["question"])
    summary = {
        "run_id": row["run_id"], "task_id": row["task_id"],
        "question_id": row["question_id"], "status": row["status"],
        "failure_kind": row["failure_kind"],
        "stations_acquired": row["stations_acquired"],
        "requests_to_verdict": row["requests_to_verdict"],
        "http_attempts": row["http_attempts"],
        "cache_hits": row["cache_hits"],
        "model_cost_known": row["model_cost_known"],
        "model_cost_cny": row["model_cost_cny"],
        "acquisition": row["acquisition"],
        "model_claim": row["model_claim"],
        "delivered": row["delivered"],
        "agreement": row["agreement"],
        "stations": row["stations"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
    # A smoke test that did not actually obtain a waveform did not pass, whatever
    # the session said. Exit non-zero so a caller cannot mistake it for success.
    if row["status"] != "succeeded" or row["stations_acquired"] < 1:
        raise SystemExit("smoke test did not acquire a station")


if __name__ == "__main__":
    main()