"""Excluding an observation must recompute, not just hide.

Loop one ends with the user asking to drop a bad channel. The guide requires the
system to say which checks need recomputation and to actually recompute them. That
is only true if the excluded record leaves the coincidence count, the integrity
flags and the delivered field — so those are what these tests pin down.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402

TARGET = [1.0, 12.0]
TASK = {"task_id": "t-test", "target_band_hz": TARGET, "method_band_hz": [1.0, 8.0]}


def rec(candidate_id, onset, *, flags=None, rate=100.0, status="analysed"):
    support = core.band_support(rate, TARGET)
    return {
        "candidate_id": candidate_id, "station": candidate_id.upper(),
        "network": "UW", "status": status, "acquisition_status": "ok",
        "distance_km": 10.0, "onset_utc": onset,
        "channel_integrity_flags": flags or [],
        "channel_integrity_ok": not flags,
        "three_component_ok": not flags, "vertical_ok": True,
        "sampling_rate_hz": rate, "band_support": support,
        "method_applicability": {"three_component_pick": not flags,
                                 "vertical_onset": support["supported"]},
        "target_band_hz": TARGET, "method_band_hz": [1.0, 8.0],
    }


def test_excluding_an_observation_changes_the_coincidence_count():
    records = [rec("a", 1000.0), rec("b", 1001.0), rec("c", 1002.0)]
    assert core.coincidence(core.active_records(records)) == 3
    core.exclude(records, "c", "user request")
    assert core.coincidence(core.active_records(records)) == 2
    assert core.coincidence(core.active_records(core.included(records, "c")[0])) == 3


def test_excluding_an_observation_can_change_the_delivered_verdict():
    records = [rec("a", 1000.0), rec("b", 1001.0), rec("c", 1002.0)]
    before = core.verdict_for_question(records, "Q_propagating", None, TASK)
    assert before["verdict"] == "coherent_multistation"
    core.exclude(records, "c", "exclude the discordant station")
    after = core.verdict_for_question(records, "Q_propagating", None, TASK)
    assert after["coincidence"] == 2
    assert after["verdict"] != "coherent_multistation"


def test_excluding_a_flagged_station_removes_its_flag_from_the_layers():
    records = [rec("a", 1000.0, flags=["UW.A..ENE:constant"]), rec("b", 1001.0)]
    before = core.layers(records, TASK, question_id="Q_instrument")
    assert before["channel_integrity"]["n_flagged"] == 1
    assert before["channel_integrity"]["n_flagged_included"] == 1
    core.exclude(records, "a", "bad horizontal channel")
    after = core.layers(records, TASK, question_id="Q_instrument")
    # the measurement stands, but it no longer feeds the judgement
    assert after["channel_integrity"]["n_flagged"] == 1
    assert after["channel_integrity"]["n_flagged_included"] == 0
    assert after["channel_integrity"]["stations_with_flags_excluded"] == ["A"]
    # the acquisition itself is still reported: excluding does not un-acquire data
    assert after["acquisition_status"]["attempted"] == 2
    assert after["acquisition_status"]["analysed"] == 2
    assert after["acquisition_status"]["excluded_by_user"] == 1
    assert "a" in after["excluded_candidates"]
    assert "b" in after["included_candidates"]


def test_excluding_does_not_delete_the_facts_already_measured():
    records = [rec("a", 1000.0, flags=["UW.A..ENE:constant"])]
    core.exclude(records, "a", "bad channel")
    kept = records[0]
    assert kept["excluded"] is True
    assert kept["channel_integrity_flags"] == ["UW.A..ENE:constant"]
    assert kept["onset_utc"] == 1000.0
    assert kept["exclusion_reason"] == "bad channel"
    # acquisition status still reports that the data were obtained
    assert core.layers(records, TASK)["acquisition_status"]["analysed"] == 1


def test_excluding_everything_is_reported_as_no_observations_not_as_a_negative():
    records = [rec("a", 1000.0)]
    core.exclude(records, "a", "user excludes the only station")
    verdict = core.verdict_for_question(records, "Q_propagating", None, TASK)
    assert verdict["verdict"] == "not_decidable"
    assert verdict["reason"] in ("no_observations", "no_analysable_data")
    # it must say nothing was judged, not that nothing is there
    assert "nothing can be judged" in verdict["statement"]
    assert "no event" not in verdict["statement"].lower()


def test_excluding_an_unknown_or_unacquired_id_is_a_no_op():
    records = [rec("a", 1000.0)]
    _, changed = core.exclude(records, "nope", "not acquired")
    assert changed is False
    assert core.active_records(records) == records


def test_excluding_twice_reports_no_second_change():
    records = [rec("a", 1000.0), rec("b", 1001.0)]
    core.exclude(records, "a", "first")
    _, changed = core.exclude(records, "a", "second")
    assert changed is False
    assert records[0]["exclusion_reason"] == "first"


def test_next_action_after_exclusion_asks_for_a_replacement_not_a_conclusion():
    records = [rec("a", 1000.0), rec("b", 1001.0), rec("c", 1002.0)]
    core.exclude(records, "c", "excluded")
    action = core.next_action(core.active_records(records), "Q_propagating")
    assert action["stop"] is False
    assert action["needs"]
    assert "结论" not in action["needs"]


def test_exclusion_to_three_stations_is_not_a_regional_negative():
    """Three stations with no clustering is 'single station', never 'nothing there'."""
    records = [rec("a", 1000.0), rec("b", 1100.0), rec("c", 1200.0), rec("d", 1300.0)]
    core.exclude(records, "d", "user excludes one station")
    verdict = core.verdict_for_question(records, "Q_propagating", None, TASK)
    assert verdict["verdict"] == "single_station_only"
    assert verdict["coincidence"] == 1
    assert "not searched are unknown" in verdict["statement"]


def test_exclusion_down_to_two_stations_is_reported_as_insufficient_not_as_single():
    records = [rec("a", 1000.0), rec("b", 1100.0), rec("c", 1200.0)]
    core.exclude(records, "c", "user excludes one station")
    verdict = core.verdict_for_question(records, "Q_propagating", None, TASK)
    assert verdict["verdict"] == "not_decidable"
    assert verdict["reason"] == "insufficient_stations"
    assert "unsearched stations" in verdict["statement"]