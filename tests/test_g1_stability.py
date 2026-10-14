"""The parameter-stability layer and the recommended-action re-run.

The second review asks for one genuinely valuable instability result that shows its
basis, impact, recommended action and what changes on re-run — not just a final
label. These tests pin the parts that could quietly lie:

  * a *restricted* grid must announce that it is narrower than the declared one;
  * a check that could not run must be recorded, not dropped (a missing key reads
    as "stable");
  * the recommended action must narrow the grid or be refused with a reason;
  * the re-run's change must be visible per station, because an aggregate maximum
    hides the station that actually improved.
"""
import sys
from pathlib import Path

import numpy as np
import pytest
from obspy import Stream, Trace, UTCDateTime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

import core  # noqa: E402
import diagnostics  # noqa: E402
import review  # noqa: E402


def _stream(onset_s=65.0, fs=100.0, duration_s=130.0):
    """A small impulsive onset on a quiet vertical trace, plus one horizontal."""
    n = int(duration_s * fs)
    rng = np.random.default_rng(7)
    data = rng.normal(0.0, 1.0, n)
    t = np.arange(n) / fs
    burst = np.exp(-0.5 * ((t - onset_s) / 0.15) ** 2)
    data = data + 400.0 * burst
    start = UTCDateTime("2023-03-01T11:34:43")
    st = Stream()
    for comp in ("Z", "N", "E"):
        st.append(Trace(data=data.copy() if comp != "E" else data * 0.6,
                        header={"network": "UW", "station": "TEST", "location": "",
                                "channel": "HH" + comp, "sampling_rate": fs,
                                "starttime": start}))
    return st


def test_the_declared_grid_is_the_single_source_for_the_band_helpers():
    assert core.stability_grid_bands() == [list(b)
                                           for b in diagnostics.GRID["bands_hz"]]
    declared = core.stability_grid_bands()
    assert core.stability_bands_within([3.0, 12.0]) == [[3.0, 12.0]]
    # 1–12 Hz contains two declared bands, so it is already narrower than the grid
    assert core.stability_bands_within([1.0, 12.0]) == [[1.0, 8.0], [3.0, 12.0]]
    # nothing sits fully inside 2–8 Hz: the narrowest overlapping band is used, so
    # the action still narrows instead of silently doing nothing
    assert core.stability_bands_within([2.0, 8.0]) == [[1.0, 8.0]]
    assert core.stability_bands_within([50.0, 60.0]) == declared  # no overlap


def test_the_default_is_the_full_declared_grid_and_says_so():
    res = diagnostics.stability(_stream())
    assert res["full_grid"] is True
    assert res["bands_evaluated"] == [list(b) for b in diagnostics.GRID["bands_hz"]]
    assert res["n_configurations"] == diagnostics.grid_size()
    assert {tuple(e["band_hz"]) for e in res["estimates"]
            if e["detector"] == "stalta_z"} == {tuple(b) for b in diagnostics.GRID["bands_hz"]}


def test_a_restricted_grid_is_a_narrower_check_and_announces_it():
    res = diagnostics.stability(_stream(), bands_hz=[[3.0, 12.0]])
    assert res["full_grid"] is False
    assert res["bands_evaluated"] == [[3.0, 12.0]]
    assert res["n_configurations"] == len(diagnostics.GRID["thresholds"]) + 1
    evaluated = {tuple(e["band_hz"]) for e in res["estimates"]}
    assert evaluated == {(3.0, 12.0), (1.0, 20.0)}  # the grid band + AR-AIC's own
    # a narrower grid must never be presented as the full one
    assert res["n_configurations"] < diagnostics.grid_size()


def test_a_check_that_could_not_run_is_recorded_not_dropped(tmp_path):
    summary = core.stability_summary(tmp_path / "missing.mseed")
    # An absent key would read as "no instability"; the failure must be named.
    assert summary["stability"] == "check_failed"
    assert summary["spread_s"] is None
    assert "FileNotFoundError" in (summary["reason"] or "")
    assert "不能视为稳定" in summary["statement"]


def test_the_stability_layer_drops_excluded_observations(tmp_path):
    kept = {"candidate_id": "c1", "station": "KEEP",
            "stability": {"stability": "unstable", "spread_s": 9.0}}
    gone = {"candidate_id": "c2", "station": "GONE", "excluded": True,
            "stability": {"stability": "stable", "spread_s": 0.1}}
    layer = core.stability_layer([kept, gone])
    assert set(layer) == {"KEEP"}
    assert core.stability_layer([gone]) is None


def test_the_band_action_is_refused_when_it_would_not_narrow_the_grid():
    state = {"records": [{"candidate_id": "c1", "station": "TEST"}]}
    with pytest.raises(ValueError) as exc:
        review.feedback_change(state, {"kind": "stability_bands",
                                       "target_band_hz": [1.0, 20.0]})
    assert "没有把到时网格收窄" in str(exc.value)
    # a band that does narrow is accepted and normalised
    ok = review.feedback_change(state, {"kind": "stability_bands",
                                        "target_band_hz": [3.0, 12.0]})
    assert ok["bands_hz"] == [[3.0, 12.0]]
    # an undeclared band is refused rather than silently widened
    with pytest.raises(ValueError):
        review.feedback_change(state, {"kind": "stability_bands",
                                       "bands_hz": [[4.0, 9.0]]})


def test_a_restricted_grid_change_is_diffed_per_station(tmp_path, monkeypatch):
    """The re-run's change must be visible per station, not only as an aggregate."""
    folder = tmp_path / "run"
    for cand in ("c1", "c2"):
        (folder / "raw" / cand).mkdir(parents=True)
        (folder / "raw" / cand / "waveform.mseed").write_bytes(b"x")
        (folder / "raw" / cand / "metadata.xml").write_bytes(b"x")

    records = [
        {"candidate_id": "c1", "station": "IMPROVES", "network": "UW",
         "stability": {"stability": "unstable", "spread_s": 9.44,
                       "full_grid": True}},
        {"candidate_id": "c2", "station": "STAYS", "network": "UW",
         "stability": {"stability": "unstable_bimodal", "spread_s": 44.27,
                       "full_grid": True}},
    ]
    state = {"records": records, "run_id": "r-test"}

    def fake_reanalyse(candidate, st, record, wave=None, meta=None):
        spread = 0.03 if record["station"] == "IMPROVES" else 44.25
        state_kind = ("stable" if record["station"] == "IMPROVES"
                      else "unstable_bimodal")
        record["stability"] = {"stability": state_kind, "spread_s": spread,
                               "full_grid": False}
        return record

    monkeypatch.setattr(core, "reanalyse", fake_reanalyse)
    recomputed, detail = review._apply_change(
        {"kind": "stability_bands", "bands_hz": [[3.0, 12.0]]}, state, folder)
    assert recomputed == 2
    per = {s["station"]: s for s in detail["stations"]}
    assert per["IMPROVES"]["state_before"] == "unstable"
    assert per["IMPROVES"]["state_after"] == "stable"
    assert per["IMPROVES"]["before"] == 9.44 and per["IMPROVES"]["after"] == 0.03
    assert per["STAYS"]["state_before"] == per["STAYS"]["state_after"]

    # ...and the snapshot diff carries the per-station view, so an aggregate
    # maximum that barely moves cannot hide the improvement.
    snap = review._stability_snapshot(core.stability_layer(records))
    assert snap["stability_by_station"]["IMPROVES"]["state"] == "stable"
    assert snap["stability_full_grid"] is False
    assert snap["stability_state"] == "mixed"