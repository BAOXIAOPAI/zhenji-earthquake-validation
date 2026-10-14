"""One paid AGH feedback flow: exclude the only acquired station, then recover.

This is the third review's 门槛 B §2.2 acceptance step. The existing run
`r-0016509070584443` stays as the validator's correct-rejection sample; this script
adds a *new*, small, real flow:

  1. a free-input task whose question says the first round may take just one station
     while the task still allows acquiring other candidates;
  2. a real AGH session for that first round;
  3. a real AGH *feedback* session that is told to drop the first round's station and
     is allowed to acquire replacements, re-check the method conditions and answer —
     or to terminate explicitly with a reason and a next step.

Both rounds are recorded as their own runs with task/run/parent, the raw tool trace,
the before/after fields, the requests and the cost, so the interface closure can be
checked rather than asserted. Execution success (`status`) and task completion
(`task_completed`) are reported separately, and a wrong model claim is still refused
by the validator — nothing here relaxes it for the sake of a demo.

Usage:
  .venv/bin/python main/smoke_feedback_recovery.py [--cap-cny 0.5] \\
      [--window-start 2023-03-01T11:34:43 --window-end 2023-03-01T11:36:43] \\
      [--region orcas] [--band 1 12] [--out <path>.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import runlog  # noqa: E402
import run_agent_arm  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402

QUESTION = ("先用离中心最近的一个台站做初步判断（第一轮只取一个台站；"
            "本任务允许后续再取同一范围内其他台站），告诉我："
            "这段观测能不能用于 1–12 Hz 的 P/S 拾取？")

DEFAULT = {
    "window_start": "2023-03-01T11:34:43",
    "window_end": "2023-03-01T11:36:43",
    "region": "orcas",
    "band": [1.0, 12.0],
    "question": QUESTION,
    "cap_cny": 0.5,
    "out": str(ROOT / "delivery/review-3-2026-10-10/evidence/"
                      "agh-feedback-recovery.json"),
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
        elif token == "--cap-cny":
            options["cap_cny"] = float(argv[i + 1])
        elif token == "--out":
            options["out"] = argv[i + 1]
    if rest:
        options["question"] = " ".join(rest)
    return options


def _ledger_upper() -> float:
    """The ledger's own upper bound, computed the same way the server reports it."""
    path = runlog.LEDGER
    if not path.exists():
        return 0.0
    entries = json.loads(path.read_text()).get("entries") or []
    return round(sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                     for e in entries), 6)


def _ledger_for_runs(run_ids) -> float:
    """What these runs actually cost, summed from the ledger entries themselves."""
    path = runlog.LEDGER
    if not path.exists():
        return 0.0
    wanted = set(run_ids)
    return sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
               for e in (json.loads(path.read_text()).get("entries") or [])
               if e.get("case_id") in wanted)


def _ledger_size() -> int:
    path = runlog.LEDGER
    if not path.exists():
        return 0
    return len(json.loads(path.read_text()).get("entries") or [])


def _trace_summary(run_id: str, limit: int = 60) -> dict:
    folder = runlog.run_dir(run_id)
    path = folder / "tool_calls.jsonl"
    calls = []
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            calls.append({"tool": entry.get("tool"), "phase": entry.get("phase"),
                          "at": entry.get("at")})
    tools = {}
    for call in calls:
        tools[call["tool"]] = tools.get(call["tool"], 0) + 1
    return {"tool_calls": len(calls), "by_tool": tools,
            "first_calls": calls[:limit]}


def _run_facts(run_id: str) -> dict:
    folder = runlog.run_dir(run_id)
    read = lambda name: (json.loads((folder / name).read_text())  # noqa: E731
                         if (folder / name).exists() else {})
    task = read("task.json")
    state = read("mcp_state.json")
    # An AGH run records its judgement in validation.json (validator_verdict); a
    # deterministic run writes verdict.json. Accept either, and say which was used.
    verdict = read("verdict.json")
    validation = read("validation.json")
    validated = validation.get("validator_verdict") or {}
    delivered = validation.get("delivered") or {}
    if not verdict:
        verdict = validated
    outcome = read("result.json")
    cost = read("cost.json")
    return {
        "run_id": run_id,
        "parent_run_id": (read("run.json") or {}).get("parent_run_id"),
        "task_id": task.get("task_id"),
        "user_question": task.get("user_question"),
        "acceptance": task.get("acceptance"),
        "allowed_actions": task.get("allowed_actions"),
        "target_band_hz": task.get("target_band_hz"),
        "method_goal": task.get("method_goal"),
        "acquired": [{"station": r.get("station"),
                      "candidate_id": r.get("candidate_id"),
                      "distance_km": r.get("distance_km"),
                      "excluded": bool(r.get("excluded")),
                      "status": r.get("status")} for r in (state.get("acquired") or [])],
        "exclusions": state.get("exclusions"),
        "verdict": verdict.get("verdict"),
        "field_answered": verdict.get("field_answered"),
        # The deterministic path records `answered` as a dict; the AGH path records
        # it as a bool and puts the value in `delivered`. Accept both.
        "answered_value": (
            verdict["answered"].get("value") if isinstance(verdict.get("answered"), dict)
            else delivered.get("field_value", delivered.get("value"))),
        "statement_zh": verdict.get("statement_zh") or delivered.get("statement_zh"),
        "task_completed": delivered.get("task_completed"),
        "validation_file": "verdict.json" if (folder / "verdict.json").exists()
                           else ("validation.json" if validation else "none"),
        "status": outcome.get("status"),
        "failure_kind": outcome.get("failure_kind"),
        "failure_detail": outcome.get("failure_detail"),
        "cost": cost,
        "trace": _trace_summary(run_id),
    }


def rebuild(path: Path) -> dict:
    """Re-derive the report from the runs that were already paid for.

    The paid sessions are the evidence; this only re-reads their run directories,
    so a reporting fix never needs a second model call.
    """
    record = json.loads(path.read_text())
    for rnd in ("round1", "round2"):
        run_id = record[rnd]["run_id"]
        record[rnd]["facts"] = _run_facts(run_id)
        delivered = (json.loads((runlog.run_dir(run_id) / "validation.json").read_text())
                     .get("delivered") if (runlog.run_dir(run_id) / "validation.json").exists()
                     else {}) or {}
        record[rnd]["task_completed"] = delivered.get("task_completed")
        record[rnd]["task_completed_note"] = delivered.get("task_completed_note")
        outcome = json.loads((runlog.run_dir(run_id) / "result.json").read_text()) \
            if (runlog.run_dir(run_id) / "result.json").exists() else {}
        record[rnd]["status"] = outcome.get("status")
        record[rnd]["failure_kind"] = outcome.get("failure_kind")
        record[rnd]["failure_detail"] = outcome.get("failure_detail")
    station = record["excluded_station"]
    acquired2 = record["round2"]["facts"]["acquired"]
    refilled = sorted({r["station"] for r in acquired2
                       if not r["excluded"] and r["station"] != station})
    excluded2 = sorted({r["station"] for r in acquired2 if r["excluded"]})
    record["round2_refilled_stations"] = refilled
    record["round2_excluded_stations"] = excluded2
    record["round2_verdict"] = record["round2"]["facts"]["verdict"]
    record["round2_answered_value"] = record["round2"]["facts"]["answered_value"]
    if station in excluded2 and refilled:
        record["outcome"] = "recovered_and_answered"
    elif station in excluded2:
        record["outcome"] = "excluded_and_terminated_without_refill"
    else:
        record["outcome"] = "station_not_excluded"
    record["checks"] = {
        "round2_is_a_new_run": record["round2"]["run_id"] != record["round1"]["run_id"],
        "parent_is_round1": record["round2"]["facts"]["parent_run_id"]
                            == record["round1"]["run_id"],
        "the_station_is_excluded_in_round2": station in excluded2,
        "round1_record_untouched": bool(
            [r for r in (json.loads((runlog.run_dir(record["round1"]["run_id"])
                                    / "mcp_state.json").read_text()).get("acquired") or [])
             if not r.get("excluded")]),
        "status_and_task_completion_reported_separately":
            "status" in record["round2"] and "task_completed" in record["round2"],
        "requests_and_cost_recorded": bool(record["round2"]["facts"]["cost"]),
    }
    record["ledger_upper_cny_after"] = _ledger_upper()
    # The ledger delta is derived from the ledger's own entries for these two runs,
    # so it stays exact even when the "before" field was written by an older build.
    spent = _ledger_for_runs([record["round1"]["run_id"], record["round2"]["run_id"]])
    record["ledger_delta_cny"] = round(spent, 6)
    record["ledger_upper_cny_before"] = round(record["ledger_upper_cny_after"] - spent, 6)
    record["ledger_entries"] = _ledger_size()
    record["total_model_cost_cny"] = round(spent, 6)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({"outcome": record["outcome"], "checks": record["checks"],
                      "refilled": refilled, "excluded": excluded2,
                      "round2_verdict": record["round2_verdict"],
                      "round2_answered": record["round2_answered_value"],
                      "total_model_cost_cny": record.get("total_model_cost_cny"),
                      "ledger_delta_cny": record["ledger_delta_cny"]},
                     ensure_ascii=False, indent=2), flush=True)
    print(f"rebuilt {path}", flush=True)
    return record


def main():
    options = _args(sys.argv[1:])
    core.fdsn.set_cache(core.CACHE)
    if "--rebuild" in sys.argv:
        rebuild(Path(options["out"]))
        return
    before_upper = _ledger_upper()

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
    print(json.dumps({"round": "task", "task_id": frozen["task_id"],
                      "window": [frozen["window_start"], frozen["window_end"]],
                      "region": (frozen.get("region") or {}).get("name"),
                      "band": frozen["target_band_hz"],
                      "goal": frozen["method_goal"],
                      "question": frozen["user_question"]},
                     ensure_ascii=False, indent=2), flush=True)

    record = {"question": frozen["user_question"],
              "window": [frozen["window_start"], frozen["window_end"]],
              "region": (frozen.get("region") or {}).get("name"),
              "task_id": frozen["task_id"],
              "ledger_upper_cny_before": before_upper,
              "cap_cny": options["cap_cny"]}

    # ---- round 1: the first real AGH session ------------------------------
    print("\n=== 第一轮：真实 AGH 会话 ===", flush=True)
    row1 = run_agent_arm.run_user_task(frozen, raw["question"])
    run1 = row1["run_id"]
    state1 = json.loads((runlog.run_dir(run1) / "mcp_state.json").read_text())
    acquired = [r for r in (state1.get("acquired") or []) if not r.get("excluded")]
    record["round1"] = {
        "run_id": run1, "status": row1.get("status"),
        "failure_kind": row1.get("failure_kind"),
        "task_completed": row1.get("delivered", {}).get("task_completed"),
        "task_completed_note": row1.get("delivered", {}).get("task_completed_note"),
        "stations_acquired": row1.get("stations_acquired"),
        "http_attempts": row1.get("http_attempts"),
        "cache_hits": row1.get("cache_hits"),
        "model_cost_cny": row1.get("model_cost_cny"),
        "model_cost_known": row1.get("model_cost_known"),
        "acquisition": row1.get("acquisition"),
        "delivered": row1.get("delivered"),
        "facts": _run_facts(run1),
    }
    print(json.dumps({k: v for k, v in record["round1"].items() if k != "facts"},
                     ensure_ascii=False, indent=2, default=str), flush=True)

    if not acquired:
        record["outcome"] = "round1_acquired_nothing"
        record["note"] = ("第一轮没有取得任何观测，无法构造“排除唯一已取台站”的反馈轮。"
                          "这本身是一次真实终止，但不是 §2.2 要验的情形。")
        _write(options["out"], record)
        raise SystemExit("round 1 acquired no station; nothing to exclude")

    # The station to drop: the nearest one the first round actually took. When the
    # session took exactly one, this is that one — the guide's case.
    def sort_key(r):
        d = r.get("distance_km")
        return (d if d is not None else 9e9, r.get("station") or "")
    target = sorted(acquired, key=sort_key)[0]
    station = target.get("station")
    record["excluded_station"] = station
    record["round1_took_only_one"] = len(acquired) == 1

    if len(acquired) == 1:
        instruction = (
            f"排除台站 {station}（第一轮唯一取到的台站）：把它从判断里去掉，"
            f"然后在本任务允许的范围内补取其他台站并重新给出方法适用性的结论；"
            f"补取之后仍然无法判断，就明确说明终止原因和下一步。不要再取 {station}。")
    else:
        instruction = (
            f"排除台站 {station}（第一轮离中心最近、最先取到的台站）：把它从判断里去掉；"
            f"如果需要，可以补取同一范围内其他允许的台站并重新给出结论；"
            f"无法判断就明确说明终止原因和下一步。不要再取 {station}。")
    record["followup_instruction"] = instruction

    spent = (row1.get("model_cost_cny") or 0.0)
    if spent > options["cap_cny"]:
        record["outcome"] = "stopped_by_cap"
        record["note"] = f"第一轮已用 {spent:.6f} 元，超过本次上限 {options['cap_cny']} 元，未跑反馈轮。"
        _write(options["out"], record)
        raise SystemExit("cost cap reached after round 1")

    # ---- round 2: the real AGH feedback session ---------------------------
    print("\n=== 第二轮：真实 AGH 反馈会话 ===", flush=True)
    row2 = run_agent_arm.run_followup(run1, instruction)
    run2 = row2.get("run_id")
    if not run2:
        record["outcome"] = "followup_did_not_produce_a_run"
        record["round2_error"] = row2
        _write(options["out"], record)
        raise SystemExit(f"follow-up produced no run: {row2}")
    record["round2"] = {
        "run_id": run2, "parent_run_id": row2.get("parent_run_id"),
        "status": row2.get("status"), "failure_kind": row2.get("failure_kind"),
        "interface_blocked": row2.get("interface_blocked"),
        "failure_detail": row2.get("failure_detail"),
        "task_completed": row2.get("delivered", {}).get("task_completed"),
        "task_completed_note": row2.get("delivered", {}).get("task_completed_note"),
        "stations_acquired": row2.get("stations_acquired"),
        "requests_to_verdict": row2.get("requests_to_verdict"),
        "http_attempts": row2.get("http_attempts"),
        "cache_hits": row2.get("cache_hits"),
        "model_cost_cny": row2.get("model_cost_cny"),
        "model_cost_known": row2.get("model_cost_known"),
        "acquisition": row2.get("acquisition"),
        "delivered": row2.get("delivered"),
        "facts": _run_facts(run2),
    }
    print(json.dumps({k: v for k, v in record["round2"].items() if k != "facts"},
                     ensure_ascii=False, indent=2, default=str), flush=True)

    # ---- what actually happened, stated without spin ----------------------
    facts2 = record["round2"]["facts"]
    acquired2 = facts2["acquired"]
    refilled = sorted({r["station"] for r in acquired2
                       if not r["excluded"] and r["station"] != station})
    excluded2 = sorted({r["station"] for r in acquired2 if r["excluded"]})
    record["round2_refilled_stations"] = refilled
    record["round2_excluded_stations"] = excluded2
    record["round2_verdict"] = facts2["verdict"]
    record["round2_answered_value"] = facts2["answered_value"]
    record["total_model_cost_cny"] = round(
        (row1.get("model_cost_cny") or 0.0) + (row2.get("model_cost_cny") or 0.0), 6)
    record["ledger_upper_cny_after"] = _ledger_upper()
    record["ledger_delta_cny"] = round(
        record["ledger_upper_cny_after"] - before_upper, 6)

    if station in excluded2 and refilled:
        record["outcome"] = "recovered_and_answered"
    elif station in excluded2:
        record["outcome"] = "excluded_and_terminated_without_refill"
    else:
        record["outcome"] = "station_not_excluded"

    checks = {
        "round2_is_a_new_run": run2 != run1,
        "parent_is_round1": record["round2"]["parent_run_id"] == run1,
        "the_station_is_excluded_in_round2": station in excluded2,
        "round1_record_untouched": bool(
            [r for r in (json.loads((runlog.run_dir(run1) / "mcp_state.json")
                                    .read_text()).get("acquired") or [])
             if not r.get("excluded")]),
        "status_and_task_completion_reported_separately":
            "status" in record["round2"] and "task_completed" in record["round2"],
        "requests_and_cost_recorded": bool(facts2["cost"]),
    }
    record["checks"] = checks
    print("\n=== 判定 ===", flush=True)
    print(json.dumps({"outcome": record["outcome"], "checks": checks,
                      "refilled": refilled, "excluded": excluded2,
                      "round2_verdict": record["round2_verdict"],
                      "round2_answered": record["round2_answered_value"],
                      "total_model_cost_cny": record["total_model_cost_cny"],
                      "ledger_delta_cny": record["ledger_delta_cny"]},
                     ensure_ascii=False, indent=2), flush=True)
    _write(options["out"], record)


def _write(path, record):
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    print(f"\nwrote {out}", flush=True)


if __name__ == "__main__":
    main()