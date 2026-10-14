"""Run the frozen comparison across all arms and cases, then write RESULTS.json.

Evaluator-only material (USGS ComCat) is consulted here, after every arm has
finished, and is never visible to any arm or tool.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "explore/probes"))
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import b_coherence as B  # noqa: E402

ARMS = {
    "full_evidence": core.arm_full_evidence,
    "fixed_pipeline": core.arm_fixed_pipeline,
    "adaptive_rule": core.arm_adaptive_rule,
    "adaptive_question": core.arm_adaptive_question,
}
OUT = ROOT / "main/out"


def fresh_state(case, arm, index):
    return {"run_id": f"{case['case_id']}__{arm}__r{index}", "case_id": case["case_id"],
            "arm": arm, "records": [], "requests": 0, "bytes": 0,
            "inject_first": bool(case.get("injected"))}


def run_case(case, discovery, arm_name):
    state = fresh_state(case, arm_name, 0)
    (core.RUNS / state["run_id"]).mkdir(parents=True, exist_ok=True)
    ARMS[arm_name](case, discovery, state)
    verdict = core.verdict_for_question(
        state["records"], case["question_id"], state.get("observed_candidate"))
    answer = core.answered_field(verdict, case["question_id"])
    return {
        "case_id": case["case_id"], "arm": arm_name,
        "kind": case["kind"], "split": case["split"],
        "question_id": case["question_id"],
        "n_eligible_available": discovery["n_eligible"],
        "stations_acquired": len(state["records"]),
        "requests_to_verdict": state["requests"],
        "bytes": state["bytes"],
        "verdict": verdict["verdict"], "reason": verdict["reason"],
        "coincidence": verdict["coincidence"],
        "eligible_stations": verdict["eligible_stations"],
        "integrity_flag": verdict["integrity_flag"],
        "answered": answer,
        "records": [{k: v for k, v in r.items()
                     if k not in ("waveform_url", "metadata_url")}
                    for r in state["records"]],
        "evidence_dir": str((core.RUNS / state["run_id"]).relative_to(ROOT)),
    }


def verify(case):
    """Independent reference, evaluator only."""
    spec = {"start": core._iso_plus(case["start"], -30),
            "end": core._iso_plus(case["start"], core.WINDOW_S + 30),
            "center_lat": case["region"]["center_lat"],
            "center_lon": case["region"]["center_lon"]}
    result = B.comcat_query(spec, radius_km=120)
    catalogued = result.get("queried") and result.get("count", 0) > 0
    return {"comcat": result, "catalogued_event": bool(catalogued)}


def expected_verdict(case, verification):
    """Environmental label for the exact-label criterion (PROTOCOL §3, pre-amendment-01)."""
    if case["kind"] == "sparse":
        return "not_decidable"
    if case["kind"] == "artifact":
        return "instrument_artifact"
    if verification["catalogued_event"]:
        return "coherent_multistation"
    return "single_station_only"


def correct_binary(case, row, verification):
    """Coherence-only criterion (PROTOCOL amendment-01), ignoring the asked question."""
    verdict = row.get("verdict")
    if case["kind"] == "sparse":
        return verdict == "not_decidable"
    if case["kind"] == "artifact":
        return verdict == "instrument_artifact"
    return (verdict == "coherent_multistation") == verification["catalogued_event"]


def correct_for_question(case, row, verification):
    """Did the run answer the question it was actually asked?

    Q_instrument: the injected dead channel must be reported as the explanation.
    Q_usable:     an observation that cannot support the 1-12 Hz band must be
                  reported as unusable, not silently treated as analysable.
    Q_propagating: the only decisive error is concluding shared propagation when
                  the independent catalogue has no event (or the reverse).
    """
    verdict = row.get("verdict")
    question = case["question_id"]
    if question == "Q_instrument":
        return verdict == "instrument_artifact"
    if question == "Q_usable":
        return verdict == "not_decidable"
    return (verdict == "coherent_multistation") == verification["catalogued_event"]


def main():
    core.fdsn.set_cache(core.CACHE)
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1] if len(sys.argv) > 1 else None
    cases = [c for c in core.CASES if only in (None, c["case_id"])]
    results, verifications = [], {}
    for case in cases:
        discovery = core.discover(case)
        verification = verify(case)
        verifications[case["case_id"]] = {
            "comcat": verification["comcat"],
            "catalogued_event": verification["catalogued_event"],
            "expected_verdict": expected_verdict(case, verification),
        }
        print(f"{case['case_id']:26s} eligible={discovery['n_eligible']:3d} "
              f"catalogued={verification['catalogued_event']}", flush=True)
        for arm_name in ARMS:
            try:
                row = run_case(case, discovery, arm_name)
            except Exception as exc:  # noqa: BLE001
                row = {"case_id": case["case_id"], "arm": arm_name,
                       "error": f"{type(exc).__name__}: {exc}"[:300]}
            row["expected_verdict"] = verifications[case["case_id"]]["expected_verdict"]
            row["verdict_matches_reference"] = row.get("verdict") == row["expected_verdict"]
            row["answers_question_correctly"] = correct_for_question(
                case, row, verifications[case["case_id"]])
            row["coherence_correct"] = correct_binary(
                case, row, verifications[case["case_id"]])
            row["answer_answers_question"] = bool((row.get("answered") or {}).get("answered"))
            results.append(row)
            print(f"   {arm_name:18s} verdict={row.get('verdict'):22s} "
                  f"req={row.get('requests_to_verdict'):>2} "
                  f"asked={row.get('answers_question_correctly')} "
                  f"coherence={row.get('coherence_correct')}", flush=True)
    (OUT / "RESULTS.json").write_text(json.dumps(
        {"arms": list(ARMS), "cases": core.CASES, "questions": core.QUESTIONS,
         "verifications": verifications, "results": results},
        ensure_ascii=False, indent=2, default=str))
    summarise(results, verifications)


def summarise(results, verifications):
    for criterion, label in (
            ("answers_question_correctly", "已回答用户实际问题"),
            ("coherence_correct", "仅按一致传播判据")):
        print(f"\n=== 主指标：到达正确判决所需的请求数 —— {label} ===")
        by_arm = defaultdict(list)
        for row in results:
            by_arm[row["arm"]].append(row)
        print(f"{'arm':18s} {'正确/总':>8s} {'中位请求':>9s} {'平均请求':>9s} {'错误':>5s}")
        for arm, rows in sorted(by_arm.items()):
            correct = [r for r in rows if r.get(criterion)]
            reqs = sorted(r["requests_to_verdict"] for r in correct
                          if r.get("requests_to_verdict") is not None)
            med = reqs[len(reqs) // 2] if reqs else None
            avg = (sum(reqs) / len(reqs)) if reqs else None
            wrong = [r["case_id"] for r in rows if not r.get(criterion)]
            print(f"{arm:18s} {len(correct):>4d}/{len(rows):<3d} {str(med):>9s} "
                  f"{('%.2f' % avg) if avg else 'n/a':>9s} {len(wrong):>5d}")
            if wrong:
                print(f"   错误案例: {wrong}")
    print("\n=== 逐案例（请求数） ===")
    for case_id in verifications:
        cat = verifications[case_id]["catalogued_event"]
        print(f"{case_id:26s} 目录事件={str(cat):5s} " + " ".join(
            f"{r['arm']}={r.get('verdict')}({r.get('requests_to_verdict')})"
            for r in results if r["case_id"] == case_id))


if __name__ == "__main__":
    main()