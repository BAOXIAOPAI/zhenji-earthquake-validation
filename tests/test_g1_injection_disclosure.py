"""The disclosed demo injection and the verified-event sample.

The second review asks the main display to use a verified real event window plus a
*clearly disclosed* bad-channel injection, with a control that has no injection.
These tests pin the three things that could silently go wrong: the sample drifting
from the verification evidence, the injection flag leaking into the model view, and
the injection not actually happening when it is switched on.
"""
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import prepare_agent  # noqa: E402
import samples as samplesmod  # noqa: E402
import task as taskmod  # noqa: E402
import task_input  # noqa: E402


def test_the_sample_matches_the_verified_event_evidence():
    """The page's sample and the verification record must not drift apart."""
    ev = json.loads((ROOT / "delivery/revision-2/evidence/real-event-window.json")
                    .read_text())
    event = samplesmod.SAMPLES["event"]
    quiet = samplesmod.SAMPLES["quiet"]
    assert ev["verified"] is True
    assert event["event_id"] == ev["event_id"]
    assert event["origin_time_utc"] == ev["origin_time_utc"]
    assert event["magnitude"] == ev["magnitude"]
    assert event["magnitude_type"] == ev["magnitude_type"]
    assert event["place"] == ev["place"]
    assert event["catalog_url"] == ev["catalog_url"]
    # the sample windows are the verified windows (recorded to the second)
    assert ev["window_start_utc"].startswith(event["window_start"])
    assert ev["window_end_utc"].startswith(event["window_end"])
    assert ev["quiet_window_start_utc"].startswith(quiet["window_start"])
    assert ev["quiet_window_end_utc"].startswith(quiet["window_end"])
    assert event["window_start"] < ev["origin_time_utc"] < event["window_end"]


def test_the_sample_is_a_ready_task_and_names_the_same_stations():
    event = samplesmod.SAMPLES["event"]
    parsed = task_input.build({
        "window_start": event["window_start"], "window_end": event["window_end"],
        "question": event["question"], "region": event["region"],
        "target_band_hz": event["target_band_hz"]})
    assert parsed["ready"], parsed.get("missing")
    frozen = taskmod.task_from_spec(parsed["structured"],
                                    user_question=event["question"])
    assert frozen["demo_case_ref"] is None, "the sample must not be a preset case"
    assert frozen["target_band_hz"] == event["target_band_hz"]
    # the sample declares which verified stations recorded it
    assert [s.split(".")[1] for s in event["stations"]] == ["OLGA", "TURTL",
                                                            "RNWY", "LOPEZ"]


def test_every_sample_carries_a_disclosure_and_a_no_injection_control():
    for name, sample in samplesmod.SAMPLES.items():
        assert sample["disclosure"], name
        assert "不是真实观测故障" in sample["injection_note"], name
        assert "注入" in sample["injection_note"], name
    # the event sample's disclosure states the catalog fact, not a source claim
    assert "ComCat" in samplesmod.SAMPLES["event"]["disclosure"]
    # the quiet sample is explicitly a control
    assert "对照" in samplesmod.SAMPLES["quiet"]["disclosure"]
    assert samplesmod.SAMPLES["quiet"]["event_id"] is None


def test_the_injection_flag_is_evaluator_side_and_never_reaches_the_model():
    parsed = task_input.build({
        "window_start": "2023-03-01T11:34:43", "window_end": "2023-03-01T11:36:43",
        "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？", "region": "orcas"})
    frozen = taskmod.task_from_spec(parsed["structured"],
                                    user_question="这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
                                    demo_injection=True)
    assert (frozen["acceptance"] or {}).get("injected") is True
    assert (frozen["acceptance"] or {}).get("injection_note") == taskmod.INJECTION_NOTE
    view = taskmod.model_view(frozen)
    blob = json.dumps(view, ensure_ascii=False)
    for leaked in ("inject", "演示用注入", "synthetic", "derived"):
        assert leaked not in blob, f"{leaked} leaked into the model view"
    # the acquisition layer sees the flag, the model does not
    case = taskmod.case_for_task(frozen)
    assert case["injected"] is True
    assert case["injection"] == taskmod.INJECTION_NOTE
    # the evaluator record carries the switch that the MCP server reads
    evaluator = prepare_agent.evaluator_state(frozen)
    assert evaluator["inject_first"] is True


def test_the_derivation_writes_a_separate_file_and_never_touches_the_raw_one():
    """The injection must be a derived copy — the raw download stays byte-identical."""
    import tempfile
    from obspy import Stream, Trace, UTCDateTime, read

    tmp = Path(tempfile.mkdtemp())
    raw_folder = tmp / "runlog" / "r-injection-test" / "raw" / "cand-ok"
    raw_folder.mkdir(parents=True)
    st = Stream()
    for comp in ("Z", "N", "E"):
        data = np.arange(100, dtype=float) if comp != "E" else np.linspace(-5, 5, 100)
        st.append(Trace(data=data, header={
            "network": "UW", "station": "TEST", "location": "", "channel": "HH" + comp,
            "sampling_rate": 100.0,
            "starttime": UTCDateTime("2023-03-01T11:34:43")}))
    raw = raw_folder / "waveform.mseed"
    st.write(str(raw), format="MSEED")
    raw_before = raw.read_bytes()

    original_root, original_runlog = core.ROOT, core.RUNLOG
    core.ROOT, core.RUNLOG = tmp, tmp / "runlog"
    try:
        derived = core._derive_flat_channel(
            {"run_id": "r-injection-test", "task_id": "t-x"}, "cand-ok", raw,
            "sha-of-parent")
    finally:
        core.ROOT, core.RUNLOG = original_root, original_runlog

    assert derived is not None
    assert derived["record"]["synthetic"] is True
    assert derived["record"]["parent_waveform_sha256"] == "sha-of-parent"
    assert raw.read_bytes() == raw_before, "the raw file must not be rewritten"

    out = tmp / "runlog" / "r-injection-test" / "derived" / "cand-ok" / "waveform.mseed"
    assert out.exists()
    assert derived["record"]["derived_sha256"] != derived["record"]["parent_waveform_sha256"]
    injected = read(str(out), format="MSEED")
    e = np.asarray(injected.select(component="E")[0].data, float)
    z = np.asarray(injected.select(component="Z")[0].data, float)
    assert float(np.ptp(e)) == 0.0, "the injected E channel must be constant"
    assert float(np.ptp(z)) > 0.0, "the other components must be untouched"
    assert (out.parent / "derivation.json").exists()

def test_a_failed_injection_is_reported_instead_of_silently_skipped(tmp_path):
    """A disclosed injection that did not happen must say so.

    The pre-fix `_derive_flat_channel` swallowed every error and returned None, so a
    requested injection could silently not apply while the result still claimed it.
    """
    state = {"run_id": "r-injection-fail", "task_id": "t-x"}
    missing = tmp_path / "not-there.mseed"
    assert core._derive_flat_channel(state, "cand", missing, "sha") is None
    assert "FileNotFoundError" in state["_injection_error"]

    # ...and a stream with no E component gives its own explicit reason
    from obspy import Stream, Trace, UTCDateTime
    only_z = Stream([Trace(data=np.arange(50, dtype=float), header={
        "network": "UW", "station": "VERT", "location": "", "channel": "EHZ",
        "sampling_rate": 100.0, "starttime": UTCDateTime("2023-03-01T11:34:43")})])
    path = tmp_path / "vert.mseed"
    only_z.write(str(path), format="MSEED")
    state2 = {"run_id": "r-injection-fail2", "task_id": "t-x"}
    assert core._derive_flat_channel(state2, "cand", path, "sha") is None
    assert "no E component" in state2["_injection_error"]


def test_a_feedback_recompute_reuses_the_injected_variant_not_the_raw_file(tmp_path):
    """A disclosed injection must survive the first feedback round.

    The recompute used to read `raw/` unconditionally, which silently undid the
    injection: the child re-analysed the untouched download while the result still
    said the injection applied.
    """
    import review
    folder = tmp_path / "run"
    (folder / "raw" / "c1").mkdir(parents=True)
    (folder / "derived" / "c1").mkdir(parents=True)
    raw = folder / "raw" / "c1" / "waveform.mseed"
    derived = folder / "derived" / "c1" / "waveform.mseed"
    raw.write_bytes(b"raw")
    derived.write_bytes(b"derived")

    chosen = review._analysis_waveform(folder, "c1")
    assert chosen == derived, "the derived variant is what the parent analysed"
    # with no derived variant the raw download is the analysis waveform
    derived.unlink()
    assert review._analysis_waveform(folder, "c1") == raw
    raw.unlink()
    assert review._analysis_waveform(folder, "c1") is None


def test_the_failure_sample_is_operable_and_declares_what_should_happen():
    """A transparent failure scenario, not a hidden one."""
    failure = samplesmod.SAMPLES["failure"]
    parsed = task_input.build({
        "window_start": failure["window_start"], "window_end": failure["window_end"],
        "question": failure["question"], "region": failure["region"],
        "target_band_hz": failure["target_band_hz"]})
    assert parsed["ready"], parsed.get("missing")
    # the sample states the expected outcome, so the user can check it happened
    assert "没有任何数据" in failure["disclosure"]
    assert "未知" in failure["disclosure"]
    assert "不会用历史结果顶替" in failure["disclosure"]
    # it is a decade beyond the catalogued data, so it cannot quietly be a real event
    assert failure["window_start"] > "2030-01-01"
    assert failure["event_id"] is None


def test_the_offline_sample_reuses_the_verified_window_and_explains_the_count():
    event = samplesmod.SAMPLES["event"]
    assert event["offline_note"]
    assert "真实外部请求" in event["offline_note"]
    # the offline claim is about a real counter, not a promise, and it does not
    # hard-code a number that a warm cache would contradict
    assert "0" in event["offline_note"]
    assert "缓存命中是真实计数" in event["offline_note"]


def test_the_cache_is_wired_into_the_deterministic_entry_point():
    """Without this the same window is re-downloaded and nothing runs offline."""
    import inspect
    import review
    src = inspect.getsource(review.run_review)
    assert "set_cache" in src and "core.CACHE" in src
