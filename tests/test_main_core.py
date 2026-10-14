"""Regression tests for the frozen decision rule and the coherence statistic.

test_coincidence_ignores_earlier_onsets is the regression for a real bug: the
first version tested only `t - t0 <= window`, so every onset earlier than the
last one counted as a negative difference and the statistic was inflated.

Field names here follow the G0 split: there is no global `usable` any more, and
band support is judged against the band the user asked about.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402

TARGET = (1.0, 12.0)


def rec(candidate_id, onset, status="analysed", flags=None, rate=100.0,
        three=True, target=TARGET):
    support = core.band_support(rate, target)
    analysed = status == "analysed"
    return {"candidate_id": candidate_id, "station": candidate_id, "status": status,
            "onset_utc": onset, "channel_integrity_flags": flags or [],
            "channel_integrity_ok": not flags,
            "sampling_rate_hz": rate or None, "three_component_ok": three,
            "vertical_ok": analysed, "band_support": support,
            "method_applicability": {
                "three_component_pick": bool(three and analysed and support["supported"]),
                "vertical_onset": bool(analysed and support["supported"])}}


def test_coincidence_ignores_earlier_onsets():
    records = [rec("a", 1000.0), rec("b", 1020.0), rec("c", 1040.0), rec("d", 1060.0)]
    assert core.coincidence(records) == 1


def test_coincidence_counts_a_real_cluster():
    records = [rec("a", 1000.0), rec("b", 1001.0), rec("c", 1002.5),
               rec("d", 1090.0)]
    assert core.coincidence(records) == 3


def test_coincidence_boundary_is_inclusive():
    records = [rec("a", 1000.0), rec("b", 1003.0), rec("c", 1003.01)]
    assert core.coincidence(records) == 2


def test_sparse_region_is_not_decidable_before_single_station():
    """Protocol §2: insufficient_stations must be tested before single_station_only."""
    records = [rec("a", 1000.0), rec("b", 1200.0)]
    verdict = core.verdict_of(records, task={"target_band_hz": list(TARGET)})
    assert verdict["verdict"] == "not_decidable"
    assert verdict["reason"] == "insufficient_stations"


def test_instrument_artifact_requires_flag_and_low_coincidence():
    records = [rec("a", 1000.0, flags=["X.E:constant"]), rec("b", 1300.0),
               rec("c", 1600.0)]
    verdict = core.verdict_of(records, task={"target_band_hz": list(TARGET)})
    assert verdict["verdict"] == "instrument_artifact"

    coherent = [rec("a", 1000.0, flags=["X.E:constant"]), rec("b", 1000.5),
                rec("c", 1001.0)]
    assert core.verdict_of(
        coherent, task={"target_band_hz": list(TARGET)})["verdict"] == "coherent_multistation"


# --------------------------------------------------- G0-3 band / unknown semantics
def test_twenty_hz_cannot_support_one_to_twelve_hz():
    """Nyquist 10 Hz < 12 Hz: 20 Hz data must NOT count as supporting 1-12 Hz."""
    support = core.band_support(20.0, (1.0, 12.0))
    assert support["supported"] is False
    assert support["reason"] == "nyquist_below_target"
    assert support["nyquist_hz"] == 10.0


def test_fifty_hz_supports_one_to_twelve_hz():
    assert core.band_support(50.0, (1.0, 12.0))["supported"] is True


def test_eight_hz_data_does_not_support_one_to_twelve_hz():
    assert core.band_support(8.0, (1.0, 12.0))["supported"] is False


def test_no_data_is_unknown_not_band_unsupported():
    """A fetch failure must stay unknown; it is not evidence about the band."""
    records = [{"candidate_id": "a", "station": "A", "status": "fetch_error",
                "onset_utc": None, "channel_integrity_flags": [],
                "band_support": None, "method_applicability": {}}]
    verdict = core.verdict_for_question(records, "Q_usable", None,
                                        {"target_band_hz": list(TARGET)})
    assert verdict["reason"] == "no_analysable_data"
    assert verdict["terminated_by"] == "unanswered_no_data"
    assert verdict["answered"]["answered"] is False
    assert verdict["answered"]["value"] == "unknown"


def test_empty_acquisition_reports_no_observations():
    verdict = core.verdict_of([], task={"target_band_hz": list(TARGET)})
    assert verdict["reason"] == "no_observations"


def test_single_station_pick_applicability_is_separate_from_multistation_sufficiency():
    """A lone 50 Hz three-component station is applicable input; it is just not
    enough to judge multi-station evidence. Those are different statements."""
    records = [rec("HOOD", 1000.0, rate=50.0, three=True)]
    layers = core.layers(records, {"target_band_hz": list(TARGET)},
                         question_id="Q_usable")
    assert layers["method_applicability"]["stations_supporting_target_band"] == 1
    assert layers["method_applicability"]["per_station"][0]["applicability"][
        "three_component_pick"] is True
    assert layers["hypotheses"]["usable_for_requested_pick"]["state"] == "supported"
    assert core.verdict_of(records, task={"target_band_hz": list(TARGET)})[
        "reason"] == "insufficient_stations"


def test_bad_horizontal_channel_leaves_vertical_onset_applicable():
    """A constant E channel breaks the three-component pick, not the Z check."""
    records = [rec("OLGA", 1000.0, flags=["UW.OLGA..ENE:constant"], three=False)]
    layers = core.layers(records, {"target_band_hz": list(TARGET)},
                         question_id="Q_instrument")
    row = layers["method_applicability"]["per_station"][0]
    assert row["applicability"]["three_component_pick"] is False
    assert row["applicability"]["vertical_onset"] is True
    assert layers["channel_integrity"]["n_flagged"] == 1


def test_early_stop_does_not_become_a_regional_negative():
    """P1-2: stopping at three stations is not a global lower bound.

    With three stations and no clustering the rule reports a negative. Adding
    three more stations that DO cluster flips it to a positive, so the negative
    must be stated as bounded to what was searched.
    """
    first = [rec("a", 1000.0), rec("b", 1020.0), rec("c", 1040.0)]
    verdict = core.verdict_of(first, task={"target_band_hz": list(TARGET)})
    assert verdict["verdict"] == "single_station_only"
    assert "not searched" in verdict["statement"]
    assert "not a statement that no clustering exists" in verdict["statement"]

    more = first + [rec("d", 2000.0), rec("e", 2001.0), rec("f", 2002.0)]
    flipped = core.verdict_of(more, task={"target_band_hz": list(TARGET)})
    assert flipped["verdict"] == "coherent_multistation"
    assert verdict["verdict"] != flipped["verdict"], \
        "three-station stop is not a lower bound; the verdict can flip"


def test_sufficiency_statement_never_claims_unsearched_stations():
    verdict = core.verdict_of([rec("a", 1000.0)], task={"target_band_hz": list(TARGET)})
    assert "unsearched" in verdict["statement"]


# -------------------------------------------------------- question / layer mapping
def test_question_mapping_addresses_the_asked_field():
    records = [rec("a", 1000.0, flags=["X.E:constant"], three=False),
               rec("b", 1300.0), rec("c", 1600.0)]
    task = {"target_band_hz": list(TARGET)}
    instrument = core.verdict_for_question(records, "Q_instrument", None, task)
    assert instrument["answered"]["field"] == "channel_integrity_flags"
    assert instrument["answered"]["answered"] is True
    usable = core.verdict_for_question([rec("a", 1000.0, rate=6.192)], "Q_usable",
                                       None, task)
    assert usable["reason"] == "band_unsupported"
    assert usable["answered"]["field"] == "method_applicability"
    assert usable["answered"]["value"] is False
    propagating = core.verdict_for_question(records, "Q_propagating", None, task)
    assert propagating["answered"]["field"] == "verdict"


# ------------------------------------------- P0-2 question-specific answer fields
TASK = {"target_band_hz": list(TARGET)}


def test_q_usable_all_supported_is_a_positive_answer():
    records = [rec("good", 1000.0, rate=50.0), rec("also", 1001.0, rate=100.0)]
    verdict = core.verdict_for_question(records, "Q_usable", None, TASK)
    assert verdict["verdict"] == "pick_applicable"
    assert verdict["terminated_by"] == "band_check"
    assert verdict["answered"]["field"] == "method_applicability"
    assert verdict["answered"]["answered"] is True
    assert verdict["answered"]["value"] is True
    assert verdict["answered"]["answer_kind"] == "positive"


def test_q_usable_partial_support_is_a_partial_answer_not_a_band_or_coherence_one():
    """The defect: partial support used to fall through to an onset-clustering answer."""
    records = [rec("good", 1000.0, rate=50.0), rec("slow", 1001.0, rate=6.192)]
    verdict = core.verdict_for_question(records, "Q_usable", None, TASK)
    assert verdict["terminated_by"] == "band_partial"
    assert verdict["terminated_by"] != "fell_through_to_coherence"
    assert verdict["verdict"] == "pick_partially_applicable"
    assert verdict["answered"]["field"] == "method_applicability"
    assert verdict["answered"]["answered"] is True
    assert verdict["answered"]["value"] == "partial"
    assert verdict["answered"]["scope"]["stations_supporting"] == ["good"]
    assert verdict["answered"]["scope"]["stations_not_supporting"] == ["slow"]


def test_q_usable_no_supported_station_is_a_negative_answer():
    verdict = core.verdict_for_question([rec("slow", 1000.0, rate=6.192)],
                                        "Q_usable", None, TASK)
    assert verdict["reason"] == "band_unsupported"
    assert verdict["verdict"] == "pick_not_applicable"
    assert verdict["terminated_by"] == "band_check"
    assert verdict["answered"]["value"] is False
    assert verdict["answered"]["answer_kind"] == "negative"


def test_q_usable_without_measurable_data_stays_unknown():
    records = [{"candidate_id": "a", "station": "A", "status": "fetch_error",
                "onset_utc": None, "channel_integrity_flags": [],
                "band_support": None, "method_applicability": {}}]
    verdict = core.verdict_for_question(records, "Q_usable", None, TASK)
    assert verdict["terminated_by"] == "unanswered_no_data"
    assert verdict["answered"]["answered"] is False
    assert verdict["answered"]["value"] == "unknown"
    assert verdict["reason"] == "no_analysable_data"


def test_q_instrument_no_flag_found_is_a_scoped_negative_not_a_coherence_answer():
    records = [rec("a", 1000.0, rate=50.0), rec("b", 1000.5, rate=50.0),
               rec("c", 1001.0, rate=50.0)]
    verdict = core.verdict_for_question(records, "Q_instrument", None, TASK)
    assert verdict["terminated_by"] == "instrument_check"
    assert verdict["terminated_by"] != "fell_through_to_coherence"
    assert verdict["verdict"] == "no_instrument_anomaly_in_scope"
    assert verdict["answered"]["field"] == "channel_integrity_flags"
    assert verdict["answered"]["answered"] is True
    assert verdict["answered"]["value"] is False
    assert verdict["answered"]["answer_kind"] == "negative"
    assert verdict["answered"]["scope"]["stations_checked"] == ["a", "b", "c"]
    # A local negative must never be stated as a global clean bill of health.
    assert "not a global statement" in verdict["statement"]


def test_q_instrument_scope_names_the_stations_that_were_never_checked():
    records = [rec("checked", 1000.0, rate=50.0),
               {"candidate_id": "b", "station": "unfetched", "status": "fetch_error",
                "onset_utc": None, "channel_integrity_flags": [],
                "band_support": None, "method_applicability": {}}]
    verdict = core.verdict_for_question(records, "Q_instrument", None, TASK)
    scope = verdict["answered"]["scope"]
    assert scope["stations_checked"] == ["checked"]
    assert scope["stations_not_checked"] == ["unfetched"]
    assert "unfetched" not in verdict["statement"]


def test_a_vertical_only_station_is_applicable_for_a_vertical_onset():
    records = [rec("Z-only", 1000.0, rate=50.0, three=False)]
    layers = core.layers(records, TASK, question_id="Q_usable")
    row = layers["method_applicability"]["per_station"][0]
    assert row["applicability"]["vertical_onset"] is True
    assert row["applicability"]["three_component_pick"] is False
    assert row["support_state"] == "supported"


def test_a_bad_horizontal_channel_is_named_and_does_not_hide_the_vertical_check():
    records = [rec("OLGA", 1000.0, flags=["UW.OLGA..ENE:constant"], three=False)]
    verdict = core.verdict_for_question(records, "Q_instrument", None, TASK)
    assert verdict["verdict"] == "instrument_artifact"
    assert verdict["answered"]["value"] is True
    assert verdict["answered"]["scope"]["stations_with_flags"] == ["OLGA"]


def test_every_answer_carries_its_own_stop_condition():
    """The stop condition is the asked field, not the coherence rule."""
    # One inspected station settles an instrument question, even with no flag.
    one = [rec("a", 1000.0, rate=50.0)]
    assert core.question_is_decided(one, "Q_instrument", None, TASK) is True
    assert core.question_is_decided(one, "Q_usable", None, TASK) is True
    # A propagation question still needs the coherence rule to be decided.
    assert core.question_is_decided(one, "Q_propagating", None, TASK) is False


def test_every_answer_label_belongs_to_the_questions_own_vocabulary():
    cases = [
        ("Q_propagating", [rec("a", 1000.0), rec("b", 1000.5), rec("c", 1001.0)]),
        ("Q_instrument", [rec("a", 1000.0, flags=["X.E:constant"], three=False)]),
        ("Q_instrument", [rec("a", 1000.0, rate=50.0)]),
        ("Q_usable", [rec("a", 1000.0, rate=50.0)]),
        ("Q_usable", [rec("a", 1000.0, rate=50.0), rec("b", 1001.0, rate=6.192)]),
        ("Q_usable", [rec("a", 1000.0, rate=6.192)]),
        # no measurable data must still use the question's own unknown label, not
        # the propagation rule's `not_decidable`
        ("Q_propagating", [{"candidate_id": "a", "station": "A",
                            "status": "fetch_error", "onset_utc": None,
                            "channel_integrity_flags": [], "band_support": None}]),
        ("Q_instrument", [{"candidate_id": "a", "station": "A",
                           "status": "fetch_error", "onset_utc": None,
                           "channel_integrity_flags": [], "band_support": None}]),
        ("Q_usable", [{"candidate_id": "a", "station": "A",
                       "status": "fetch_error", "onset_utc": None,
                       "channel_integrity_flags": [], "band_support": None}]),
    ]
    for question_id, records in cases:
        verdict = core.verdict_for_question(records, question_id, None, TASK)
        assert verdict["verdict"] in core.ANSWER_LABELS[question_id], (
            f"{question_id} produced {verdict['verdict']}")


def test_layers_are_all_present_and_do_not_merge_meanings():
    # the eight required layers, plus documented bookkeeping keys
    records = [rec("a", 1000.0, flags=["X.E:constant"], three=False),
               rec("b", 1000.5), rec("c", 1001.0)]
    layers = core.layers(records, {"target_band_hz": list(TARGET)},
                         question_id="Q_propagating")
    required = {"acquisition_status", "channel_integrity", "method_applicability",
                "trigger_evidence", "robustness", "hypotheses", "validation",
                "next_action"}
    assert required <= set(layers)
    # the only extras are the documented bookkeeping keys, never merged meanings
    assert set(layers) - required == {"included_candidates", "excluded_candidates",
                                      "scope"}
    # A channel flag must not be reported as the cause of the candidate.
    assert "of itself" not in layers["channel_integrity"]["scope"]
    assert "does not by itself establish" in layers["channel_integrity"]["scope"]
    # Trigger evidence must disclaim source/causal meaning.
    assert "not a source" in layers["trigger_evidence"]["scope"]
    # Acquisition failure must not be reported as a band statement.
    assert "does NOT mean" in layers["acquisition_status"]["note"]


def test_trigger_evidence_scope_disclaims_unsearched_stations():
    records = [rec("a", 1000.0), rec("b", 1000.5), rec("c", 1001.0)]
    evidence = core.trigger_evidence(records)
    assert evidence["max_in_window"] == 3
    assert evidence["band_hz"] == list(core.METHOD_BAND)
    assert "unsearched" in evidence["scope"]


def test_method_band_and_target_band_are_reported_separately():
    spec = core.spec_for(core.CASES[0], {"window_start": "2025-03-03T13:02:20",
                                         "window_end": "2025-03-03T13:04:20",
                                         "region": core.CASES[0]["region"],
                                         "target_band_hz": [1.0, 12.0]})
    assert spec["method_band_hz"] == [1.0, 8.0]
    assert spec["target_band_hz"] == [1.0, 12.0]


# ------------------------------------------------------------------ case integrity
def test_window_is_120_seconds_and_cases_are_well_formed():
    ids = set()
    for case in core.CASES:
        assert case["case_id"] not in ids, "duplicate case id"
        ids.add(case["case_id"])
        assert case["question_id"] in core.QUESTIONS
        window = core.window_of(case)
        from obspy import UTCDateTime
        assert abs((UTCDateTime(window["end"]) - UTCDateTime(window["start"])) - 120.0) < 1e-6


def test_no_locked_case_is_used_for_development():
    locked = [c["case_id"] for c in core.CASES if c["split"] == "locked"]
    assert locked == ["E3_ptownsend_event", "Q3_ptownsend_quiet",
                      "E4_quilcene_event", "Q4_quilcene_quiet"]


def test_retired_inject_helper_raises_instead_of_rewriting_evidence():
    """P0-5: the old helper overwrote raw data in place. It must not be usable."""
    import pytest
    with pytest.raises(RuntimeError, match="retired"):
        core.inject_flat_channel("some-run", "some-candidate")