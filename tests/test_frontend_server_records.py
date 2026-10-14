"""Read-only presentation regressions, using isolated records and no model calls."""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import server  # noqa: E402


@pytest.fixture
def records(tmp_path, monkeypatch):
    monkeypatch.setattr(server.runlog, "RUNLOG", tmp_path)

    def seed(run_id, **files):
        folder = tmp_path / run_id
        folder.mkdir()
        for name, value in files.items():
            (folder / (name + ".json")).write_text(json.dumps(value))
        return folder

    return seed


def test_probe_is_filtered_but_empty_evidence_is_honest(records):
    folder = records("probe", runtime_metrics={"arm": "probe", "model_requests": 0})
    original = (folder / "runtime_metrics.json").read_bytes()
    assert server.recent_runs() == []
    evidence = server.evidence("probe")
    assert evidence["internal"] is True
    assert evidence["source_kind"] == "probe"
    assert evidence["record_state"] == "empty"
    assert evidence["model_claim_verified"] is False
    assert evidence["outcome"]["status"] is None
    assert (folder / "runtime_metrics.json").read_bytes() == original
    assert len(list(folder.iterdir())) == 1


def test_failed_execution_is_visible_even_without_evidence(records):
    records("r-failed", result={"status": "failed", "failure_kind": "timeout"})
    row = server.recent_runs()[0]
    evidence = server.evidence("r-failed")
    assert row["status"] == "failed"
    assert row["record_state"] == evidence["record_state"] == "failed"
    assert row["source_kind"] == "real"
    assert evidence["outcome"] == {
        "status": "failed", "failure_kind": "timeout", "failure_detail": None}


def test_success_without_validation_is_not_verified(records):
    records("r-unverified", run={"arm": "adaptive_agent_a1", "model": "test"},
            task={"user_question": "问题"}, result={"status": "succeeded"})
    row = server.recent_runs()[0]
    assert row["status"] == "succeeded"
    assert row["record_state"] == "unverified"
    assert row["has_delivery"] is False
    assert row["model_claim_verified"] is False


def test_empty_verdict_cannot_turn_a_saved_success_into_a_completed_review(records):
    records("r-empty-result", run={"arm": "question"}, result={"status": "succeeded"},
            verdict={})
    row = server.recent_runs()[0]
    assert row["record_state"] == "unverified"
    assert row["has_delivery"] is False


def test_saved_rule_result_is_completed_without_model_validation(records):
    records("r-rule", run={"arm": "question", "model": None},
            result={"status": "succeeded"}, verdict={"verdict": "not_decidable"})
    row = server.recent_runs()[0]
    assert row["record_state"] == "completed"
    assert row["has_delivery"] is True
    assert row["model_claim_verified"] is False


def test_validator_result_and_fixture_source_stay_distinct(records):
    records("r-fixture", run={"fixture": True}, task={"user_question": "问题"},
            result={"status": "succeeded"},
            validation={"delivered": {"verdict": "pick_applicable",
                                      "model_claim_accepted": False,
                                      "fallback_used": True}, "agreement": False})
    row = server.recent_runs()[0]
    assert row["source_kind"] == "fixture"
    assert row["record_state"] == "verified"
    assert row["model_claim_verified"] is False
    assert row["fallback_used"] is True


def test_only_accepted_examples_get_demo_source(records):
    records("r-e2e8419bf299785d", run={"arm": "adaptive_agent_a1"})
    records("r-development", run={"demo_case_ref": "E1_orcas_event"})
    rows = {r["run_id"]: r for r in server.recent_runs()}
    assert rows["r-e2e8419bf299785d"]["accepted_example"] is True
    assert rows["r-e2e8419bf299785d"]["source_kind"] == "demo"
    assert rows["r-development"]["accepted_example"] is False
    assert rows["r-development"]["source_kind"] == "real"


def test_parent_stations_come_from_parent_acquisition(records):
    records("r-parent", mcp_state={"acquired": [
        {"station": "CHIMA", "network": "UW", "candidate_id": "c1", "excluded": True}]})
    records("r-child", run={"parent_run_id": "r-parent"})
    parent = server.evidence("r-child")["parent"]
    assert parent["acquisition"] == [
        {"station": "CHIMA", "network": "UW", "candidate_id": "c1", "excluded": True}]
    assert parent["excluded_candidates"] == ["c1"]


def _wave_file(folder, channels=("Z", "N", "E")):
    path = folder / "raw" / "c1" / "waveform.mseed"
    path.parent.mkdir(parents=True)
    stream = Stream([Trace(data=np.array([1000, 1002, 998, 1004], dtype=np.int32),
                           header={"network": "UW", "station": "TEST", "channel": "EN" + c,
                                   "sampling_rate": 100, "starttime": UTCDateTime(0)})
                     for c in channels])
    stream.write(str(path), format="MSEED")
    return path


def test_waveform_display_center_preserves_raw_envelope_and_file(records):
    folder = records("r-wave", mcp_state={"acquired": [{"candidate_id": "c1"}]})
    path = _wave_file(folder)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    wave = server.waveform("r-wave", "c1", buckets=2)
    assert set(wave["channels"]) == {"Z", "N", "E"}
    for channel in wave["channels"].values():
        assert channel["display_center"] == 1001.0
        assert channel["envelope"] == [[0.0, 1000.0, 1002.0], [0.02, 998.0, 1004.0]]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("second_rate", [2, 4])
def test_waveform_preserves_all_segments_and_real_gap_timing(records, second_rate):
    folder = records("r-segments", mcp_state={"acquired": [
        {"candidate_id": "c1", "channel_integrity_flags": ["gaps_or_overlaps"]}]})
    path = folder / "raw" / "c1" / "waveform.mseed"
    path.parent.mkdir(parents=True)
    first = Trace(data=np.array([2, 4], dtype=np.int32), header={
        "network": "UW", "station": "TEST", "channel": "ENZ",
        "sampling_rate": 2, "starttime": UTCDateTime(10)})
    second = Trace(data=np.array([10, 20, 30, 40], dtype=np.int32), header={
        "network": "UW", "station": "TEST", "channel": "ENZ",
        "sampling_rate": second_rate, "starttime": UTCDateTime(15)})
    # Store out of order to check that relative timing uses the earliest start.
    Stream([second, first]).write(str(path), format="MSEED")
    before = path.read_bytes()
    wave = server.waveform("r-segments", "c1", buckets=6)
    channel = wave["channels"]["Z"]
    assert channel["start_utc"] == UTCDateTime(10).isoformat()
    assert channel["n_samples"] == 6
    assert channel["display_center"] == pytest.approx(106 / 6, abs=0.0001)
    assert channel["envelope"] == [
        [0.0, 2.0, 2.0], [0.5, 4.0, 4.0],
        *[[5 + i / second_rate, float(value), float(value)]
          for i, value in enumerate([10, 20, 30, 40])]]
    assert not any(1 <= row[0] < 5 for row in channel["envelope"])
    assert channel["duration_seconds"] == 5 + 4 / second_rate
    assert channel["sampling_rate_hz"] == (2 if second_rate == 2 else None)
    assert channel["sampling_rates_hz"] == sorted({2, second_rate})
    assert wave["channel_integrity_flags"] == ["gaps_or_overlaps"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("case, expected", [
    ("unknown", "run_not_found"), ("empty", "no_observations"),
    ("candidate", "candidate_not_found"), ("missing", "waveform_file_missing"),
    ("corrupt", "waveform_read_failed"), ("components", "waveform_no_components"),
    ("state", "waveform_read_failed"),
])
def test_waveform_failure_reasons_are_distinct(records, case, expected):
    if case != "unknown":
        acquired = [] if case == "empty" else [{"candidate_id": "c1"}]
        folder = records("r-wave", mcp_state={"acquired": acquired})
        if case == "corrupt":
            path = folder / "raw" / "c1" / "waveform.mseed"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"invalid miniseed")
        elif case == "components":
            _wave_file(folder, channels=("X",))
        elif case == "state":
            (folder / "mcp_state.json").write_text("invalid json")
    wave = server.waveform("r-wave", "bad" if case == "candidate" else "c1")
    assert wave["code"] == expected
    assert wave["reason"]
    assert wave["error"]


@pytest.mark.parametrize("path, limit, status", [
    ("/api/runs", 40, 200), ("/api/runs?limit=200", 200, 200),
    ("/api/runs?limit=10000", 200, 200), ("/api/runs?limit=0", 1, 200),
    ("/api/runs?limit=bad", None, 400),
])
def test_run_endpoint_limit_is_bounded(monkeypatch, path, limit, status):
    received = []
    monkeypatch.setattr(server, "recent_runs", lambda limit: received.append(limit) or [])
    handler = object.__new__(server.Handler)
    handler.path = path
    handler.local = lambda: None
    responses = []
    handler.send = lambda code, body: responses.append((code, body))
    handler.do_GET()
    assert responses[0][0] == status
    assert received == ([] if limit is None else [limit])
