"""The G2 contract scorer: one field mapping, no overridden copy, no wrong blame.

The second review found three defects here: a duplicate `REQUIRED_FIELD` assignment
that silently overrode the first, a feedback gate that compared a field value to a
label string, and a blame rule that read an interface failure as the policy choosing
not to gather evidence. These tests pin all three down on constructed rows, so they
cost nothing and never call a model.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import run_g2  # noqa: E402
import runlog  # noqa: E402


def row(**kw):
    base = {"status": runlog.SUCCEEDED, "verdict": None, "delivered": {},
            "claim": None, "terminated_by": None, "coincidence": None,
            "answered": None, "stations_acquired": 1, "n_eligible": 3,
            "acquisition": {}}
    base.update(kw)
    return base


def test_the_required_field_table_has_one_value_per_question():
    assert run_g2.REQUIRED_FIELD == {
        "Q_propagating": "verdict",
        "Q_usable": "method_applicability",
        "Q_instrument": "channel_integrity_flags"}


def test_a_partial_band_answer_scores_as_the_asked_field_not_a_substitution():
    r = row(verdict="pick_partially_applicable", claim="pick_partially_applicable",
            terminated_by="band_partial",
            delivered={"verdict": "pick_partially_applicable",
                       "field": "method_applicability", "field_value": "partial",
                       "task_completed": True})
    out = run_g2.evaluate({"id": "D1"}, "Q_usable", r)
    assert out["contract"] == "satisfied"
    assert out["field_delivered"] is True
    assert out["answered_asked_question"] is True


def test_a_scoped_instrument_negative_scores_as_satisfied():
    r = row(verdict="no_instrument_anomaly_in_scope",
            claim="no_instrument_anomaly_in_scope", terminated_by="instrument_check",
            delivered={"verdict": "no_instrument_anomaly_in_scope",
                       "field": "channel_integrity_flags", "field_value": False,
                       "task_completed": True})
    out = run_g2.evaluate({"id": "D2"}, "Q_instrument", r)
    assert out["contract"] == "satisfied"
    assert out["field_delivered"] is True


def test_an_interface_failure_is_not_blamed_on_the_policy():
    r = row(status=runlog.SUCCEEDED, verdict="pick_unknown", answered=False,
            terminated_by="unanswered_no_data", stations_acquired=0, n_eligible=6,
            acquisition={"tool_errors": 3, "tools_with_errors": ["acquire_station"]},
            delivered={"verdict": "pick_unknown", "field": "method_applicability",
                       "field_value": "unknown", "task_completed": False})
    out = run_g2.evaluate({"id": "D3"}, "Q_usable", r)
    assert out["contract"] == "failed"
    assert "接口层工具失败 3 次" in out["why"]
    assert "模型主动不取证" in out["why"]


def test_no_acquisition_without_an_interface_error_is_still_a_policy_failure():
    r = row(status=runlog.SUCCEEDED, verdict="pick_unknown", answered=False,
            terminated_by="unanswered_no_data", stations_acquired=0, n_eligible=6,
            acquisition={"tool_errors": 0},
            delivered={"verdict": "pick_unknown", "field": "method_applicability",
                       "field_value": "unknown", "task_completed": False})
    out = run_g2.evaluate({"id": "D4"}, "Q_usable", r)
    assert out["contract"] == "failed"
    assert "取证不足" in out["why"]


def test_a_certainty_claim_without_enough_evidence_is_a_violation():
    r = row(verdict="coherent_multistation", claim="coherent_multistation",
            coincidence=2, terminated_by="coherence",
            delivered={"verdict": "coherent_multistation", "field": "verdict",
                       "field_value": "coherent_multistation",
                       "task_completed": True})
    out = run_g2.evaluate({"id": "D5"}, "Q_propagating", r)
    assert out["contract"] == "violation"
    assert out["certainty_error"] is True


def test_the_feedback_gate_reads_the_label_not_the_field_value():
    """A channel flag has value True; the gate must match on the label string."""
    flagged = {"delivered": {"verdict": "instrument_artifact",
                             "field": "channel_integrity_flags",
                             "field_value": True, "value": True}}
    assert run_g2.needs_followup(flagged) is True
    clean = {"delivered": {"verdict": "no_instrument_anomaly_in_scope",
                           "field": "channel_integrity_flags",
                           "field_value": False, "value": False}}
    assert run_g2.needs_followup(clean) is False
    # An old record whose delivered only has `value` must not be misread as a label.
    old = {"delivered": {"value": True}}
    assert run_g2.needs_followup(old) is False