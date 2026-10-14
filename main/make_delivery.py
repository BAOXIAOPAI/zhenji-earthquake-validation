"""Consolidate every arm into delivery/next/RESULTS.json.

Reads the frozen rule-arm run and both agent variants, recomputes correctness
against the independent reference, and emits one machine-readable record.
Nothing here calls a model or the network.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
from run_experiment import correct_for_question, correct_binary  # noqa: E402

OUT = ROOT / "main/out"
DELIVERY = ROOT / "delivery/next"


def load(name):
    path = OUT / name
    return json.loads(path.read_text()) if path.exists() else None


def ledger_cost_by_run():
    """Per-run model cost upper bound, keyed by the unique budget run id."""
    path = ROOT / "runs/model-budget-ledger.json"
    if not path.exists():
        return {}
    entries = json.loads(path.read_text())
    entries = entries.get("entries", entries) if isinstance(entries, dict) else entries
    cost = defaultdict(float)
    for e in entries:
        cost[e.get("case_id")] += (e.get("cost_upper_cny") or e.get("reserved_cny") or 0.0)
    return cost


def agent_rows(name, verifications, costs=None):
    data = load(name)
    if not data:
        return []
    costs = costs if costs is not None else ledger_cost_by_run()
    variant = name.replace("agent_results_", "").replace(".json", "")
    rows = []
    for row in data["results"]:
        case = core.case_by_id(row["case_id"])
        exec_path = OUT / f"agent_execution_{variant}_{row['case_id']}.json"
        run_id, elapsed = None, None
        if exec_path.exists():
            record = json.loads(exec_path.read_text())
            run_id = record.get("RUN_ID")
            elapsed = record.get("elapsed_ms")
        if run_id:
            cost = round(costs.get(run_id, 0.0), 6)
            cost_basis = "per_run"
        else:
            # Runs made before RUN_ID was recorded: fall back to every ledger entry
            # for this case. This aggregates all runs of the case, which is the
            # honest reading and is labelled as such.
            prefix = "main-" + row["case_id"]
            cost = round(sum(v for k, v in costs.items() if k.startswith(prefix)), 6)
            cost_basis = "all_runs_of_case"
        if "error" in row:
            rows.append({**row, "correct": False, "model_cost_cny": cost,
                         "cost_basis": cost_basis, "elapsed_ms": elapsed})
            continue
        rows.append({**row, "model_cost_cny": cost, "cost_basis": cost_basis,
                     "elapsed_ms": elapsed,
                     "correct": correct_for_question(
            case, row, verifications[row["case_id"]]),
            "coherence_correct": correct_binary(
                case, row, verifications[row["case_id"]]),
            "split": case["split"]})
    return rows


def stats(rows):
    ok = [r for r in rows if r.get("correct")]
    reqs = sorted(r["requests_to_verdict"] for r in ok
                  if r.get("requests_to_verdict") is not None)
    return {"runs": len(rows), "correct": len(ok),
            "median_requests_correct": reqs[len(reqs) // 2] if reqs else None,
            "mean_requests_correct": round(sum(reqs) / len(reqs), 3) if reqs else None,
            "wrong_cases": [r["case_id"] for r in rows if not r.get("correct")]}


def main():
    frozen = load("RESULTS.json")
    verifications = frozen["verifications"]
    results = frozen["results"]
    for row in results:
        case = core.case_by_id(row["case_id"])
        row["correct"] = correct_for_question(case, row, verifications[row["case_id"]])
        row["coherence_correct"] = correct_binary(case, row, verifications[row["case_id"]])
        # The four deterministic arms never call a model: cost is zero by construction.
        row["model_cost_cny"] = 0.0
        row["model_requests"] = 0

    arms = defaultdict(list)
    for row in results:
        arms[row["arm"]].append(row)

    costs = ledger_cost_by_run()
    agent_open = agent_rows("agent_results_open.json", verifications, costs)
    agent_stated = agent_rows("agent_results_stated.json", verifications, costs)
    payload = {
        "generated_by": "main/make_delivery.py",
        "provider": "deepseek", "agent_model": "deepseek-flash",
        "harness": "vendored agnes-harness (AGH)",
        "criterion": {
            "primary": "答案是否回答了用户实际提出的问题（PROTOCOL §3, amendment-01）",
            "secondary": "仅按一致传播判据（PROTOCOL §2 原始 verdict）",
            "primary_metric": "到达正确判决所消耗的外部请求数（每次台站获取 = 2 次请求）",
        },
        "reference": {
            "source": "USGS ComCat fdsnws/event/1",
            "role": "评价器专用，任何一组都看不到",
            "limitation": "对小事件有探测下限，见 FAILURES.md F-5",
        },
        "cases": {c["case_id"]: {"kind": c["kind"], "split": c["split"],
                                 "question_id": c["question_id"],
                                 "question": core.QUESTIONS[c["question_id"]],
                                 "injected": bool(c.get("injected"))}
                  for c in core.CASES},
        "arms": {
            name: {"description": desc, "all_cases": stats(rows),
                   "development": stats([r for r in rows if r["split"] == "development"]),
                   "locked": stats([r for r in rows if r["split"] == "locked"])}
            for name, desc, rows in (
                ("full_evidence", "取满 6 个最近的可用台站",
                 arms["full_evidence"]),
                ("fixed_pipeline", "固定取 3 个最近的可用台站",
                 arms["fixed_pipeline"]),
                ("adaptive_rule", "按一致传播判据决定何时停止（不知道用户问了什么）",
                 arms["adaptive_rule"]),
                ("adaptive_question", "按用户实际问题的判据决定何时停止（问题类型由人工给定）",
                 arms["adaptive_question"]),
            )
        },
        "agent_variants": {
            "adaptive_agent_open": {
                "description": "AGH 真实会话；自由文本问题；不告知验收判据",
                "all_cases": stats(agent_open),
                "development": stats([r for r in agent_open if r.get("split") == "development"]),
                "locked": stats([r for r in agent_open if r.get("split") == "locked"]),
                "rows": agent_open,
                "model_cost_cny": round(sum(r.get("model_cost_cny") or 0 for r in agent_open), 6),
            },
            "adaptive_agent_stated": {
                "description": "AGH 真实会话；自由文本问题；明确告知 ≥3 台站的验收判据（仅在开发集上验证）",
                "all_cases": stats(agent_stated),
                "rows": agent_stated,
                "model_cost_cny": round(sum(r.get("model_cost_cny") or 0 for r in agent_stated), 6),
            },
        },
        "rule_arm_rows": results,
    }
    DELIVERY.mkdir(parents=True, exist_ok=True)
    (DELIVERY / "RESULTS.json").write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, default=str))
    for name, block in payload["arms"].items():
        print(f"{name:20s} all={block['all_cases']['correct']}/{block['all_cases']['runs']} "
              f"median={block['all_cases']['median_requests_correct']}")
    for name, block in payload["agent_variants"].items():
        print(f"{name:20s} all={block['all_cases']['correct']}/{block['all_cases']['runs']} "
              f"median={block['all_cases']['median_requests_correct']} "
              f"wrong={block['all_cases']['wrong_cases']}")
    print(f"\nwrote {DELIVERY / 'RESULTS.json'}")


if __name__ == "__main__":
    main()