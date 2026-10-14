"""Direction C probe: constrained method selection and robustness diagnostics.

Question: for a given record, is an automatic onset stable across reasonable
method and parameter choices, and where can a single output not be trusted?

Independent reference: the curated P arrival sample shipped with miniPNW, with
its stated uncertainty. The reference is used by the evaluator ONLY; it never
enters the method grid or any selection step.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "prototype"))

import h5py  # noqa: E402
import pandas as pd  # noqa: E402
from waveform import read_trace  # noqa: E402

OUT = ROOT / "explore/out"

BANDS = [(1.0, 8.0), (3.0, 12.0), (5.0, 20.0)]
THRESHOLDS = [3.0, 3.5, 4.0, 5.0]
STA_S, LTA_S = 0.5, 10.0
IGNORE_FIRST_S, IGNORE_LAST_S = 5.0, 5.0


def load_records(limit=12):
    """Training earthquakes only; test/calibration cases are never opened here."""
    splits = pd.read_csv(ROOT / "evaluation/splits.csv").fillna("")
    rows = splits[(splits.split == "train") & (splits.source_type == "earthquake")]
    rows = rows.dropna(subset=["trace_P_arrival_sample", "trace_S_arrival_sample"])
    rows = rows[rows.trace_sampling_rate_hz == 100]
    rows = rows.sort_values("event_id").drop_duplicates("event_id").head(limit)
    return rows


def build_traces(handle, row):
    data = read_trace(handle, row.trace_name)[:, :15000]
    fs = 100.0
    return data, fs, float(pd.Timestamp(row.trace_start_time).timestamp())


def onset_after(tr_data, fs, band, threshold, origin_offset_s):
    """One deterministic onset estimate for one (band, threshold) configuration."""
    from obspy import Trace, UTCDateTime
    trace = Trace(np.asarray(tr_data, dtype=float))
    trace.stats.sampling_rate = fs
    trace.stats.starttime = UTCDateTime(origin_offset_s)
    trace.detrend("linear")
    trace.taper(0.02, type="cosine")
    nyq = fs / 2.0
    if not band[0] < band[1] < nyq:
        return None
    trace.filter("lowpass", freq=band[1], corners=4, zerophase=False)
    trace.filter("highpass", freq=band[0], corners=4, zerophase=False)
    from obspy.signal.trigger import classic_sta_lta, trigger_onset
    data = np.asarray(trace.data, dtype=float)
    cft = classic_sta_lta(data, int(STA_S * fs), int(LTA_S * fs))
    raw = trigger_onset(cft, threshold, 1.0)
    start = float(trace.stats.starttime)
    for a, _ in raw:
        seconds = float(a) / fs
        if IGNORE_FIRST_S <= seconds <= (len(data) / fs - IGNORE_LAST_S):
            return seconds
    return None


def ar_aic_pick(data, fs):
    """AR-AIC + STA/LTA P/S picker on the three components; returns seconds or None."""
    try:
        from obspy.signal.trigger import ar_pick
    except Exception:  # noqa: BLE001
        return None, None
    try:
        p, s = ar_pick(data[2].astype(float), data[0].astype(float),
                       data[1].astype(float), fs, 1.0, 20.0, 0.1,
                       0.55, 6.0, 0.6, 3.0, 0.5, 0.02, 8.0, 0.6, 0.5,
                       0.1, 0.2, 0.5)
    except Exception:  # noqa: BLE001
        return None, None
    duration = len(data[0]) / fs
    p_ok = bool(np.isfinite(p) and 0 < p < duration)
    s_ok = bool(p_ok and np.isfinite(s) and p < s < duration)
    return (float(p) if p_ok else None), (float(s) if s_ok else None)


def _first_number(value):
    """Some PNW columns are pipe-separated per-component values, e.g. '21.5|17.9|11.0'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, str):
        value = value.split("|")[0].strip()
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(out) else out


def analyse_record(handle, row):
    data, fs, start_epoch = build_traces(handle, row)
    ref_p = float(row.trace_P_arrival_sample) / fs
    ref_s = float(row.trace_S_arrival_sample) / fs

    estimates = []
    for band in BANDS:
        for threshold in THRESHOLDS:
            onset = onset_after(data[2], fs, band, threshold, start_epoch)
            estimates.append({"method": "stalta", "band_hz": list(band),
                              "threshold": threshold, "onset_s": onset,
                              "error_s": None if onset is None else round(onset - ref_p, 3)})
    p_aic, s_aic = ar_aic_pick(data, fs)
    estimates.append({"method": "ar_aic", "band_hz": [1.0, 20.0], "threshold": None,
                      "onset_s": p_aic, "error_s": None if p_aic is None else round(p_aic - ref_p, 3)})
    valid = [e["onset_s"] for e in estimates if e["onset_s"] is not None]
    errors = [e["error_s"] for e in estimates if e["error_s"] is not None]
    return {
        "case_id": row.case_id, "event_id": row.event_id,
        "station": f"{row.station_network_code}.{row.station_code}",
        "reference": {"p_s": round(ref_p, 3), "s_s": round(ref_s, 3),
                      "p_uncertainty_s": _first_number(row.trace_P_arrival_uncertainty_s),
                      "snr_db": _first_number(row.trace_snr_db)},
        "estimates": estimates,
        "spread_s": None if len(valid) < 2 else round(max(valid) - min(valid), 3),
        "n_valid": len(valid),
        "accuracy_s": {
            "n": len(errors),
            "median_abs_error_s": None if not errors else round(float(np.median(np.abs(errors))), 3),
            "max_abs_error_s": None if not errors else round(float(np.max(np.abs(errors))), 3),
            "any_within_1s": bool(errors) and any(abs(e) <= 1.0 for e in errors),
        },
        "ar_aic": {"p_s": p_aic, "s_s": s_aic},
    }


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    rows = load_records(limit)
    results = []
    with h5py.File(ROOT / "data/raw/pnw-mini/miniPNW_waveforms.hdf5", "r") as handle:
        for _, row in rows.iterrows():
            try:
                results.append(analyse_record(handle, row))
            except Exception as exc:  # noqa: BLE001
                results.append({"case_id": row.case_id, "error": f"{type(exc).__name__}: {exc}"[:200]})
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "c_robustness_results.json").write_text(
        json.dumps({"bands": BANDS, "thresholds": THRESHOLDS, "records": results},
                   ensure_ascii=False, indent=2))
    print(f"{'station':12s} {'refP':>7s} {'n':>3s} {'spread':>7s} {'medErr':>7s} {'maxErr':>7s} {'<=1s':>5s}")
    for r in results:
        if "error" in r:
            print(f"{r['case_id']:12s} ERROR {r['error']}")
            continue
        a = r["accuracy_s"]
        print(f"{r['station']:12s} {r['reference']['p_s']:7.2f} {r['n_valid']:3d} "
              f"{str(r['spread_s']):>7s} {str(a['median_abs_error_s']):>7s} "
              f"{str(a['max_abs_error_s']):>7s} {str(a['any_within_1s']):>5s}")
    ok = [r for r in results if "error" not in r]
    if ok:
        spreads = [r["spread_s"] for r in ok if r["spread_s"] is not None]
        within = sum(1 for r in ok if r["accuracy_s"]["any_within_1s"])
        print(f"\n汇总: {len(ok)} 条记录, 有效估计中位跨度 "
              f"{np.median(spreads):.2f}s, 至少一个估计落在参考 ±1s 内的记录 {within}/{len(ok)}")


if __name__ == "__main__":
    main()