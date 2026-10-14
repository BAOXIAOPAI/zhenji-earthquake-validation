"""The loop-two diagnostic: a bounded grid that shows when a pick is unstable.

All waveforms here are synthetic, so the tests are offline and deterministic.
The one thing they must prove is that the tool reports instability **without**
ever consulting a reference arrival time.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import diagnostics  # noqa: E402

FS = 100.0
DURATION_S = 150.0


def synth(bursts, duration_s=DURATION_S, fs=FS, seed=0, components=("Z", "N", "E")):
    """Noise plus bursts given as (start_s, freq_hz, amplitude[, ramp_s]).

    `ramp_s` matters: a sharp onset is broadband and every band will see it, while
    a smoothly ramped burst stays inside its own band. Real instability comes from
    the latter, so the tests use it.
    """
    from obspy import Trace, Stream, UTCDateTime
    rng = np.random.default_rng(seed)
    n = int(duration_s * fs)
    base = rng.normal(0, 1e-3, n)
    data = base.copy()
    for burst in bursts:
        start_s, freq, amp = burst[0], burst[1], burst[2]
        ramp_s = burst[3] if len(burst) > 3 else 0.0
        i0 = int(start_s * fs)
        seg = np.arange(n - i0) / fs
        env = np.exp(-seg / 12.0)
        if ramp_s > 0:
            env = env * np.where(seg < ramp_s,
                                 0.5 * (1 - np.cos(np.pi * seg / ramp_s)), 1.0)
        data[i0:] += amp * env * np.sin(2 * np.pi * freq * seg)
    traces = []
    for index, component in enumerate(components):
        shifted = np.roll(data, index * 3)
        traces.append(Trace(shifted, header={
            "station": "TEST", "channel": f"HH{component}",
            "sampling_rate": fs, "starttime": UTCDateTime(0)}))
    return Stream(traces)


def test_the_grid_is_fixed_and_declared_before_any_case_is_run():
    assert diagnostics.GRID["bands_hz"] == [[1.0, 8.0], [3.0, 12.0], [5.0, 20.0]]
    assert diagnostics.GRID["thresholds"] == [3.0, 3.5, 4.0, 5.0]
    assert diagnostics.grid_size() == 13
    assert diagnostics.GRID["ignore_first_s"] == 15.0


def test_a_single_clean_onset_is_reported_stable():
    result = diagnostics.stability(synth([(50.0, 6.0, 5.0)]))
    assert result["stability"] == "stable"
    assert result["spread_s"] is not None and result["spread_s"] <= 1.0
    assert result["n_valid"] >= 10
    assert len(result["clusters"]) == 1
    assert result["applicable"] is True


def test_two_separated_onsets_are_reported_as_unstable():
    # A weak, smoothly ramped 18 Hz burst early plus a strong sharp 4 Hz burst late.
    # The 1-8 Hz band sees only the late one; the 3-12 and 5-20 Hz bands see the
    # early one. The grid therefore cannot agree on a single time.
    result = diagnostics.stability(synth([(40.0, 18.0, 0.02, 4.0), (100.0, 4.0, 5.0)]))
    assert result["stability"] in ("unstable", "unstable_bimodal")
    assert result["spread_s"] > diagnostics.STABLE_SPREAD_S
    assert len(result["clusters"]) >= 2
    assert result["onsets_min_s"] < 55.0 < 90.0 < result["onsets_max_s"]


def test_an_unstable_result_says_what_to_do_next_and_what_it_does_not_mean():
    result = diagnostics.stability(synth([(40.0, 18.0, 0.02, 4.0), (100.0, 4.0, 5.0)]))
    assert result["next_step"]
    assert len(result["next_step"]) > 20
    assert "不表示信号不存在" in result["scope"]
    assert "有没有事件" in result["scope"]


def test_a_quiet_record_is_never_called_stable():
    """A record with no coherent onset must not yield a stability verdict."""
    result = diagnostics.stability(synth([]))
    assert result["stability"] in ("no_estimate", "insufficient_configurations")
    assert result["stability"] != "stable"
    assert "不要据此判断有无事件" in result["next_step"] or "无法确定" in result["next_step"]
    assert result["n_valid"] < diagnostics.MIN_CONFIGURATIONS_FOR_STABILITY


def test_a_constant_record_is_named_a_bad_channel_not_an_unstable_pick():
    """Filtering a constant trace yields numerical rubbish a detector will fire on."""
    from obspy import Stream, Trace, UTCDateTime
    flat = Stream([Trace(np.ones(15000), header={
        "station": "TEST", "channel": "HHZ", "sampling_rate": FS,
        "starttime": UTCDateTime(0)})])
    result = diagnostics.stability(flat)
    assert result["stability"] == "not_applicable"
    assert result["reason"] == "constant_channel"
    assert result["applicable"] is False
    assert result["spread_s"] is None
    assert "坏的或无数据的通道" in result["next_step"]


def test_a_non_finite_record_is_named_rather_than_detected_on():
    from obspy import Stream, Trace, UTCDateTime
    broken = np.ones(15000)
    broken[500] = np.nan
    stream = Stream([Trace(broken, header={
        "station": "TEST", "channel": "HHZ", "sampling_rate": FS,
        "starttime": UTCDateTime(0)})])
    result = diagnostics.stability(stream)
    assert result["stability"] == "not_applicable"
    assert result["reason"] == "non_finite_channel"


def test_one_or_two_configurations_are_not_treated_as_agreement():
    """Agreement among one configuration is not agreement."""
    assert diagnostics.MIN_CONFIGURATIONS_FOR_STABILITY >= 3
    result = diagnostics.stability(synth([]))
    if result["stability"] == "insufficient_configurations":
        assert "无法判断" in result["statement"]
        assert result["reason"] == "too_few_configurations"


def test_a_vertical_only_station_reports_ar_aic_as_not_applicable():
    result = diagnostics.stability(synth([(50.0, 6.0, 5.0)], components=("Z",)))
    aic = next(e for e in result["estimates"] if e["detector"] == "ar_aic_3c")
    assert aic["calculable"] is False
    assert aic["reason"] == "needs_three_components"
    # the STA/LTA part still works, so the diagnostic is not simply unavailable
    stata = [e for e in result["estimates"] if e["detector"] == "stalta_z"]
    assert any(e["calculable"] for e in stata)
    assert result["applicable"] is True


def test_an_empty_stream_is_reported_as_not_applicable():
    from obspy import Stream
    result = diagnostics.stability(Stream())
    assert result["stability"] == "not_applicable"
    assert result["applicable"] is False
    assert result["reason"] == "empty_stream"


def test_a_band_above_nyquist_is_not_calculable_instead_of_guessed():
    slow = synth([(50.0, 2.0, 5.0)], fs=8.0)   # Nyquist 4 Hz
    result = diagnostics.stability(slow)
    above = [e for e in result["estimates"]
             if e["detector"] == "stalta_z" and e["band_hz"] == [5.0, 20.0]]
    assert above and all(not e["calculable"] for e in above)
    assert all(e["reason"] == "nyquist_below_band" for e in above)


def test_the_tool_never_consults_or_returns_a_reference_pick():
    result = diagnostics.stability(synth([(50.0, 6.0, 5.0)]))
    assert result["reference_used"] is False
    assert result["network_access"] is False
    blob = repr(result)
    for forbidden in ("reference_p", "ref_p", "arrival_sample", "trace_P"):
        assert forbidden not in blob
    # the signature has no place to put one either
    import inspect
    params = set(inspect.signature(diagnostics.stability).parameters)
    assert not any("reference" in p or "ref" == p for p in params)


def test_no_new_network_access_is_needed_for_the_full_grid():
    """The guide: when the user asks for no new fetches, this must still run."""
    assert diagnostics.stability.__module__ == "diagnostics"
    source = (ROOT / "main/diagnostics.py").read_text()
    for forbidden in ("urllib", "requests", "cached_get", "fdsn", "dataselect"):
        assert forbidden not in source, f"diagnostics must stay offline: {forbidden}"


def test_every_configuration_is_reported_with_a_reason_when_it_fails():
    result = diagnostics.stability(synth([]))
    assert len(result["estimates"]) == 13
    for estimate in result["estimates"]:
        assert "calculable" in estimate
        assert estimate["reason"]
        if not estimate["calculable"]:
            assert estimate["onset_s"] is None


def test_the_result_says_how_many_configurations_were_not_calculable():
    slow = synth([(50.0, 2.0, 5.0)], fs=8.0)
    result = diagnostics.stability(slow)
    assert result["n_configurations_not_calculable"] >= 4
    assert result["grid_size"] == 13
    assert result["n_configurations"] == 13


def test_the_old_ar_pick_call_signature_really_was_invalid():
    """Reproduce the old defect without needing any waveform.

    The previous call passed 19 positional arguments; the installed signature
    accepts at most 15. This is the exact binding failure the review found.
    """
    import inspect
    from obspy.signal.trigger import ar_pick
    parameters = list(inspect.signature(ar_pick).parameters)
    assert len(parameters) == 15, "installed signature changed; recheck the fix"
    with pytest.raises(TypeError, match="positional arguments"):
        ar_pick(*([None] * 19))


def test_ar_aic_actually_executes_instead_of_always_erroring():
    """regression: 19 positional args were passed to a 15-arg `ar_pick`.

    Every AR-AIC configuration therefore raised and was reported as
    ``ar_pick_failed``, which was then read as an algorithm that could not agree.
    The picker must really run on a three-component waveform.
    """
    result = diagnostics.stability(synth([(50.0, 6.0, 5.0)]))
    aic = next(e for e in result["estimates"] if e["detector"] == "ar_aic_3c")
    assert aic["status"] in ("ok", "no_pick"), aic["reason"]
    assert "ar_pick_failed" not in aic["reason"]
    assert "ar_pick_error" not in aic["reason"]
    assert result["n_configurations_error"] == 0
    if aic["status"] == "ok":
        assert aic["onset_s"] is not None
        assert 0.0 <= aic["onset_s"] <= DURATION_S
        # The P pick precedes the S pick when both are usable.
        if aic["s_onset_s"] is not None:
            assert aic["onset_s"] <= aic["s_onset_s"]


def test_execution_error_not_applicable_and_no_pick_are_separate_counts():
    """A tool that failed, a data set the method does not fit, and a clean run that
    found no phase are three different outcomes and must not share one number."""
    vertical = diagnostics.stability(synth([(50.0, 6.0, 5.0)], components=("Z",)))
    assert vertical["n_configurations_error"] == 0
    assert vertical["n_configurations_not_applicable"] >= 1
    slow = diagnostics.stability(synth([(50.0, 2.0, 5.0)], fs=8.0))
    assert slow["n_configurations_error"] == 0
    assert slow["n_configurations_not_applicable"] >= 4
    quiet = diagnostics.stability(synth([]))
    assert quiet["n_configurations_error"] == 0
    assert quiet["n_configurations_no_pick"] >= 1


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_a_stable_pick_stays_stable_across_noise_seeds(seed):
    result = diagnostics.stability(synth([(50.0, 6.0, 5.0)], seed=seed))
    assert result["stability"] == "stable", result["statement"]