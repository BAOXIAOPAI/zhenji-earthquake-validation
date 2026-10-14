"""Build a clearly-labelled LEGAL-UNKNOWN UI fixture (free, no model, no network).

The accepted real chain has no accepted legal-unknown run, so the UI needs a
labelled fixture to show that state. This copies the real accepted first round
into a fixture runlog, applies a "exclude the only station" change through the
PRODUCTION prepare/feedback path, and validates a hand-written claim that the
production validator accepts. Nothing here is a model run; the fixture records say
so, and the normal product runlog is never touched.

Run:
  .venv/bin/python -B delivery/finalization-2026-10-12/fixtures/make_legal_unknown_fixture.py
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_RUNLOG = ROOT / "delivery/finalization-2026-10-12/fixtures/runlog"
SOURCE = ROOT / "main/runlog/r-e2e8419bf299785d"
os.environ.update(RUNLOG_DIR=str(FIXTURE_RUNLOG), EQ_CACHE_DIR=str(ROOT / "main/cache"),
                  EQ_STRICT_OFFLINE="1", MPLCONFIGDIR=str(ROOT / ".cache/matplotlib"))
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import run_agent_arm as agent  # noqa: E402
import runlog  # noqa: E402


def main():
    FIXTURE_RUNLOG.mkdir(parents=True, exist_ok=True)
    parent_id = "r-e2e8419bf299785d"
    shutil.copytree(SOURCE, FIXTURE_RUNLOG / parent_id, dirs_exist_ok=True)
    parent_state = json.loads((FIXTURE_RUNLOG / parent_id / "mcp_state.json").read_text())
    cid = parent_state["acquired"][0]["candidate_id"]

    prepared = prepare_agent.prepare_followup(
        parent_id, "排除唯一参与台站后复核",
        change={"kind": "exclude", "candidate_id": cid},
        analysis_level="quality_only")
    run_id = prepared["run_id"]
    folder = runlog.run_dir(run_id)
    task = json.loads((folder / "task.json").read_text())
    state = json.loads((folder / "mcp_state.json").read_text())
    requested = state["requested_conditions"]
    verdict = core.verdict_for_question(core.active_records(state["acquired"]),
                                        requested["question_id"], None, task)
    claim = {
        "conclusion": verdict["verdict"], "field": requested["required_field"],
        "condition_id": requested["condition_id"], "scope_candidate_ids": [],
        "evidence_refs": [], "stop_reason_code": "scope_exhausted",
        "limitations": ["fixture: 唯一参与台站被用户排除后，当前范围为空，"
                        "结论为当前证据下的正确未知。"],
    }
    validation = agent.validate(run_id, task.get("demo_case_ref") or "", task,
                                state.get("acquired") or [],
                                {"answer": json.dumps(claim, ensure_ascii=False)},
                                requested["question_id"])
    (folder / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2, default=str))
    (folder / "model_response.json").write_text(json.dumps(
        {"RUN": run_id, "status": "fixture", "fixture": True,
         "note": "手工主张 + 生产 validate；不是模型运行",
         "answer": json.dumps(claim, ensure_ascii=False)}, ensure_ascii=False, indent=2))
    runlog.outcome(run_id, "succeeded", stations_acquired=len(state["acquired"]),
                   model_cost_known=False)
    # Label the fixture honestly at the run level too.
    run_meta = json.loads((folder / "run.json").read_text())
    run_meta.update({"fixture": True,
                     "fixture_note": "UI 状态 fixture：手工主张经生产 validate，"
                                     "不是真实模型运行"})
    (folder / "run.json").write_text(json.dumps(run_meta, ensure_ascii=False, indent=2))
    print(json.dumps({"fixture_run_id": run_id, "parent": parent_id,
                      "accepted": validation["delivered"]["model_claim_accepted"],
                      "outcome": validation["finish_audit"]["outcome"],
                      "stop_basis": validation["finish_audit"]["stop_basis"],
                      "scope": validation["scope_report"]},
                     ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()