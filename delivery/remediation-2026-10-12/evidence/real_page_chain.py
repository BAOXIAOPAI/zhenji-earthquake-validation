"""One real first-round + one real feedback chain, driven through the REAL page API.

This starts the actual `main/server.py` handler in-process (the same HTTP contract
the browser's app.js uses) and posts:
  1. POST /api/review      mode=agent, a user task (explicit station CHIMA)
  2. POST /api/followup    engine=agent, a diagnostic-grid change on that child

Only the real model/AGH path runs; nothing is mocked. The report records the run
ids, parent/child link, actual profile/driver, conditions, real tool arguments, the
raw model answer and validation, cost, and the native AGH tool call/result pair.

Run from the project root:
  .venv/bin/python -B delivery/remediation-2026-10-12/evidence/real_page_chain.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(os.environ.get("EQ_CHAIN_OUT")
           or Path(__file__).with_suffix(".json"))
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import runlog  # noqa: E402
# Loaded by path: `core` puts prototype/ ahead of main/ for its own legacy imports,
# and prototype/server.py is an unrelated module.
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("product_server",
                                               ROOT / "main" / "server.py")
servermod = importlib.util.module_from_spec(_spec)
sys.modules["product_server"] = servermod
_spec.loader.exec_module(servermod)

FIRST_TASK = {
    "window_start": "2023-12-24T10:00:00Z",
    "window_end": "2023-12-24T10:02:00Z",
    "stations": ["CHIMA"],
    "question": "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
    "target_band_hz": [1.0, 12.0],
    "method_goal": "three_component_pick",
}
FEEDBACK_CHANGE = {"kind": "stability_bands", "target_band_hz": [3.0, 12.0]}
FEEDBACK_INSTRUCTION = "把到时诊断网格收紧到 3–12 Hz，不要新增取数"


def post(base, path, payload, timeout=60):
    request = urllib.request.Request(
        base + path, method="POST", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-EQ-Request": "1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        raise RuntimeError(f"POST {path} -> HTTP {exc.code}: {detail}") from exc


def get(base, path, timeout=60):
    with urllib.request.urlopen(base + path, timeout=timeout) as response:
        return json.loads(response.read())


def wait_for(base, track_id, *, budget_s=900):
    deadline = time.time() + budget_s
    while time.time() < deadline:
        data = get(base, f"/api/review/{track_id}")
        if data.get("status") != "running":
            return data
        time.sleep(1.0)
    return {"status": "timeout", "error": f"not settled within {budget_s}s"}


def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def run_evidence(run_id):
    folder = runlog.run_dir(run_id)
    validation = load(folder / "validation.json") or {}
    state = load(folder / "mcp_state.json") or {}
    config = load(folder / "agent_config.json") or {}
    tool_calls = []
    tool_path = folder / "tool_calls.jsonl"
    if tool_path.exists():
        for line in tool_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("phase") == "start":
                tool_calls.append({"tool": entry.get("tool"),
                                   "arguments": entry.get("arguments")})
    native = load(folder / "native_agh_evidence.json") or {}
    return {
        "run_id": run_id,
        "agent_config": config,
        "requested_conditions": state.get("requested_conditions"),
        "actual_conditions": state.get("requested_conditions"),
        "attempted_candidate_ids_episode": state.get("attempted_candidate_ids_episode"),
        "container_exclusions": state.get("exclusions"),
        "tool_calls": tool_calls,
        "delivered": validation.get("delivered"),
        "acceptance_checks": validation.get("acceptance_checks"),
        "evidence_ref_report": validation.get("evidence_ref_report"),
        "scope_report": validation.get("scope_report"),
        "finish_audit": {k: (validation.get("finish_audit") or {}).get(k) for k in
                         ("outcome", "can_finish", "stop_basis", "stop_bases",
                          "unmet")},
        "model_claim": validation.get("model_claim"),
        "model_raw_answer": load(folder / "model_response.json"),
        "cost": load(folder / "cost.json"),
        "cleanup": load(folder / "cleanup_status.json"),
        "native_agh_evidence": {
            "agh_session_id": native.get("agh_session_id"),
            "native_tool_pair": native.get("native_tool_pair"),
            "native_sessions_db": native.get("native_sessions_db"),
            "mcp_tool_pair": native.get("mcp_tool_pair")},
    }


def main():
    feedback_only = os.environ.get("EQ_CHAIN_FEEDBACK_ONLY")
    ledger = load(ROOT / "runs/model-budget-ledger.json") or {}
    entries = ledger.get("entries") or []
    total = sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                for e in entries)
    report = {"schema": "real-page-chain/v1",
              "entry_point": "main/server.py /api/review + /api/followup (page contract)",
              "mocked": False,
              "budget_before": {"entries": len(entries),
                                "cumulative_upper_cny": round(total, 6),
                                "guard_limit_cny": 20},
              "first_round_task_spec": FIRST_TASK,
              "feedback_change": FEEDBACK_CHANGE,
              "feedback_instruction": FEEDBACK_INSTRUCTION}

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), servermod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        if feedback_only:
            # Feedback-only attempt on an existing real parent (used for an honest
            # retry; the previous attempt's outcome stays in its own report).
            first_id = feedback_only
            report["first_run_id"] = first_id
            report["first_evidence"] = run_evidence(first_id)
            report["first_accepted"] = bool(
                (load(runlog.run_dir(first_id) / "validation.json") or {})
                .get("delivered", {}).get("model_claim_accepted"))
            fb_started = post(base, "/api/followup",
                              {"parent_run_id": first_id, "engine": "agent",
                               "change": FEEDBACK_CHANGE,
                               "instruction": FEEDBACK_INSTRUCTION})
            report["feedback_start"] = fb_started
            fb_settled = wait_for(base, fb_started["run_id"])
            report["feedback_settled_status"] = fb_settled.get("status")
            fb_result = fb_settled.get("result") or {}
            report["feedback_result_status"] = fb_result.get("status")
            report["feedback_run_id"] = fb_result.get("run_id")
            report["feedback_parent_run_id"] = fb_result.get("parent_run_id")
            report["feedback_failure_kind"] = fb_result.get("failure_kind")
            second_id = fb_result.get("run_id")
            if second_id:
                report["feedback_evidence"] = run_evidence(second_id)
            report["feedback_accepted"] = bool((fb_result.get("delivered") or {})
                                               .get("model_claim_accepted"))
            report["chain_ok"] = bool(report["first_accepted"]
                                      and report["feedback_accepted"])
            ledger_after = load(ROOT / "runs/model-budget-ledger.json") or {}
            report["budget_after"] = {"entries": len(
                ledger_after.get("entries") or []), "cumulative_upper_cny": round(sum(
                    e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                    for e in (ledger_after.get("entries") or [])), 6)}
            return report
        started = post(base, "/api/review",
                       {"mode": "agent", "task": FIRST_TASK})
        report["first_start"] = started
        settled = wait_for(base, started["run_id"])
        report["first_settled_status"] = settled.get("status")
        result = settled.get("result") or {}
        report["first_result_status"] = result.get("status")
        report["first_run_id"] = result.get("run_id")
        report["first_failure_kind"] = result.get("failure_kind")
        first_id = result.get("run_id")
        if not first_id or result.get("status") != "succeeded":
            report["stopped_reason"] = "first round did not succeed"
            return report
        report["first_evidence"] = run_evidence(first_id)
        first_accepted = bool((result.get("delivered") or {})
                              .get("model_claim_accepted"))
        report["first_accepted"] = first_accepted
        force_feedback = os.environ.get("EQ_CHAIN_FORCE_FEEDBACK") == "1"
        if not first_accepted and not force_feedback:
            report["stopped_reason"] = "first round not accepted; not spending on feedback"
            return report

        fb_started = post(base, "/api/followup",
                          {"parent_run_id": first_id, "engine": "agent",
                           "change": FEEDBACK_CHANGE,
                           "instruction": FEEDBACK_INSTRUCTION})
        report["feedback_start"] = fb_started
        fb_settled = wait_for(base, fb_started["run_id"])
        report["feedback_settled_status"] = fb_settled.get("status")
        fb_result = fb_settled.get("result") or {}
        report["feedback_result_status"] = fb_result.get("status")
        report["feedback_run_id"] = fb_result.get("run_id")
        report["feedback_parent_run_id"] = fb_result.get("parent_run_id")
        report["feedback_failure_kind"] = fb_result.get("failure_kind")
        second_id = fb_result.get("run_id")
        if second_id:
            report["feedback_evidence"] = run_evidence(second_id)
        report["feedback_accepted"] = bool((fb_result.get("delivered") or {})
                                           .get("model_claim_accepted"))
        report["chain_ok"] = bool(report["first_accepted"]
                                  and report["feedback_accepted"]
                                  and result.get("status") == "succeeded"
                                  and fb_result.get("status") == "succeeded")
        ledger_after = load(ROOT / "runs/model-budget-ledger.json") or {}
        entries_after = ledger_after.get("entries") or []
        report["budget_after"] = {
            "entries": len(entries_after),
            "cumulative_upper_cny": round(sum(
                e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                for e in entries_after), 6)}
        return report
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    outcome = main()
    OUT.write_text(json.dumps(outcome, indent=2, ensure_ascii=False, default=str))
    print(json.dumps(outcome, indent=2, ensure_ascii=False, default=str))