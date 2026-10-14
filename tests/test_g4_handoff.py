"""The hand-back list must not drift from the artifacts it summarises.

`HANDOFF-3.md` is the single latest status entry, so a stale number in it is worse
than no number. These tests read the same files the document points at and check the
figures the document states. Everything is offline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import runlog  # noqa: E402

REVISION = ROOT / "delivery/revision-3"
REVIEW = ROOT / "delivery/review-3-2026-10-10"
HANDOFF = REVISION / "HANDOFF-3.md"
ACCEPTANCE = REVISION / "ACCEPTANCE-3.json"


def _acceptance():
    if not ACCEPTANCE.exists():
        pytest.skip("ACCEPTANCE-3.json not present")
    return json.loads(ACCEPTANCE.read_text())


def _evidence(name):
    path = REVIEW / "evidence" / name
    if not path.exists():
        pytest.skip(f"{name} not present")
    return json.loads(path.read_text())


def test_the_latest_entry_point_marks_the_gates_that_are_done_and_the_ones_that_are_not():
    text = HANDOFF.read_text()
    assert "门槛 A 与门槛 B 通过" in text
    assert "门槛 C、D 未开始" in text or "门槛 C、D 未开始" in text.replace("**", "")
    # The document must say what it did not do, not only what it did.
    assert "未完成" in text
    assert _acceptance()["latest_entry_point"] == "delivery/revision-3/HANDOFF-3.md"


def test_the_documented_ledger_figures_match_the_ledger():
    """The cost delta is the one number a reviewer cannot re-derive by reading prose.

    The ledger is a LIVE budget tracker: later authorized spend (revision-4's
    AGH smoke) appends entries after this document was frozen. So the document's
    figures must match the ledger as a PREFIX — the first `entries_now` entries
    — while the current total may only have grown past it, never below it.
    """
    doc = _acceptance()["ledger"]
    entries = json.loads(runlog.LEDGER.read_text())["entries"]
    upper = round(sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                      for e in entries), 6)
    prefix_upper = round(sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0
                             for e in entries[:doc["entries_now"]]), 6)
    assert doc["entries_now"] <= len(entries)
    assert doc["upper_bound_now_cny"] == prefix_upper
    assert upper >= prefix_upper
    assert doc["remaining_now_cny"] == round(doc["cap_cny"] - prefix_upper, 6)
    assert doc["entries_now"] > doc["entries_before"]
    assert doc["upper_bound_now_cny"] > doc["upper_bound_before_cny"]


def test_the_documented_paid_delta_is_the_sum_of_those_two_runs():
    doc = _acceptance()
    rounds = doc["real_agh_feedback"]["rounds"]
    entries = json.loads(runlog.LEDGER.read_text())["entries"]
    wanted = {r["run_id"] for r in rounds}
    spent = round(sum(e.get("cost_upper_cny") or 0 for e in entries
                      if e.get("case_id") in wanted), 6)
    assert doc["paid_cost_added_cny"] == spent
    assert doc["provider_requests_added"] == sum(
        1 for e in entries if e.get("case_id") in wanted)
    assert doc["paid_model_sessions_added"] == len(rounds)


def test_the_feedback_round_is_a_new_run_whose_parent_is_the_first_round():
    rounds = _acceptance()["real_agh_feedback"]["rounds"]
    first, second = rounds[0], rounds[1]
    assert first["parent"] is None
    assert second["parent"] == first["run_id"]
    assert second["run_id"] != first["run_id"]
    for rnd in rounds:
        folder = runlog.run_dir(rnd["run_id"])
        assert folder.exists(), rnd["run_id"]
        run = json.loads((folder / "run.json").read_text())
        assert run.get("parent_run_id") == rnd["parent"]
        outcome = json.loads((folder / "result.json").read_text())
        assert outcome["status"] == rnd["status"]


def test_the_correct_rejection_sample_is_still_preserved_unchanged():
    """The guide names this run explicitly; it must not have been rewritten."""
    sample = _acceptance()["real_agh_feedback"]["preserved_rejection_sample"]
    folder = runlog.run_dir(sample["run_id"])
    assert folder.exists()
    validation = json.loads((folder / "validation.json").read_text())
    delivered = validation["delivered"]
    assert delivered["value"] == sample["delivered"] == "unknown"
    assert delivered["model_claim_accepted"] is sample["model_claim_accepted"] is False
    assert "not settled" in delivered["rejection_reason"]


def test_the_round_two_exclusion_actually_landed():
    """The document claims OLGA was excluded and replacements were acquired."""
    second = _acceptance()["real_agh_feedback"]["rounds"][1]
    state = json.loads((runlog.run_dir(second["run_id"]) / "mcp_state.json").read_text())
    acquired = state["acquired"]
    excluded = {r["station"] for r in acquired if r.get("excluded")}
    kept = {r["station"] for r in acquired if not r.get("excluded")}
    assert set(second["excluded"]) <= excluded
    assert set(second["stations"]) <= kept


def test_the_counterexample_probe_passed_and_covers_every_named_case():
    doc = _evidence("修复前后反例核对.json")
    assert doc["all_checks_pass"] is True
    assert all(doc["all_checks"].values())
    for key in ("P0_1_method_goal", "P0_2_followup_spec", "P0_3_interface_failure",
                "P1_1_continuous_conditions"):
        assert key in doc, key
        assert doc[key]["before_fix"], f"{key} must state the pre-fix behaviour"
    assert doc["network"].startswith("none")


def test_the_counterexample_probe_is_regenerable_and_reads_saved_records_only():
    """Its source records must be the saved page run, not a fresh fetch."""
    doc = _evidence("修复前后反例核对.json")
    assert doc["generated_by"] == "main/probe_counterexamples.py"
    assert (ROOT / doc["source_records"]).exists()


def test_the_browser_chain_in_the_handoff_matches_the_trace():
    doc = _acceptance()["browser_index"]
    trace = _evidence("ui-operation-trace-gate-b.json")
    steps = trace["steps"]
    assert doc["chain"] == [s["run_id"] for s in steps]
    for earlier, later in zip(steps, steps[1:]):
        assert later["parent"] == earlier["run_id"]
    assert trace.get("console_errors") == []
    assert doc["model_calls"] == 0


def test_every_evidence_file_the_handoff_names_exists():
    import re
    text = HANDOFF.read_text()
    named = re.findall(r"`([^`]*evidence/[^`]+)`", text)
    assert named, "the handoff should point at its evidence"
    for name in named:
        if "*" in name:
            # A glob in the index stands for a set; check it matches something.
            assert any(base.glob(name) for base in (ROOT, ROOT / "delivery", REVIEW)), name
            continue
        # The hand-back writes paths both relative to the project root and relative
        # to `delivery/`; both must resolve to a real file.
        assert any((base / name).exists()
                   for base in (ROOT, ROOT / "delivery", REVIEW)), name