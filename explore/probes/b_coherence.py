"""Direction B probe: competing explanations and the checks that discriminate them.

Given a candidate anomaly (a UTC window and a region), the system must:
  1. obtain independent observations at several stations,
  2. propose competing explanations,
  3. run checks that can actually support or refute each one,
  4. report what is supported, refuted and still unknown.

Independent verification is external: the USGS ComCat catalogue is consulted by
the *evaluator* only, after the checks, and never fed to the reasoning step.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import fdsn  # noqa: E402
import quality  # noqa: E402

OUT = ROOT / "explore/out"
COMcat = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# Frozen analysis parameters (configs/stage5/analysis-protocol.json)
STA_S, LTA_S, ON, OFF = 0.5, 10.0, 3.5, 1.0
IGNORE_FIRST_S, IGNORE_LAST_S = 15.0, 5.0
BAND = (1.0, 8.0)


# ------------------------------------------------------------------ signal ops
def bandpassed(trace, band=BAND, corners=4):
    from obspy import Trace
    out = trace.copy()
    out.detrend("linear")
    out.taper(0.02, type="cosine")
    nyq = out.stats.sampling_rate / 2.0
    if not band[0] < band[1] < nyq:
        return None, f"band {band} not supported at {out.stats.sampling_rate} Hz"
    out.filter("lowpass", freq=band[1], corners=corners, zerophase=False)
    out.filter("highpass", freq=band[0], corners=corners, zerophase=False)
    return out, None


def onsets(trace, fs):
    from obspy.signal.trigger import classic_sta_lta, trigger_onset
    data = np.asarray(trace.data, dtype=float)
    nsta, nlta = int(STA_S * fs), int(LTA_S * fs)
    if len(data) <= nlta + nsta:
        return None, "record shorter than the STA/LTA windows"
    cft = classic_sta_lta(data, nsta, nlta)
    raw = trigger_onset(cft, ON, OFF)
    start = float(trace.stats.starttime)
    lower = start + IGNORE_FIRST_S
    upper = float(trace.stats.endtime) - IGNORE_LAST_S
    kept = [(float(a) / fs, float(b) / fs) for a, b in raw]
    kept = [(a, b) for a, b in kept if lower <= start + a <= upper]
    return {"n_raw": len(raw), "n_kept": len(kept),
            "first_onset_utc": None if not kept else _iso(start + kept[0][0]),
            "first_onset_epoch": None if not kept else start + kept[0][0],
            "onsets_utc": [_iso(start + a) for a, _ in kept]}, None


def _iso(epoch):
    from obspy import UTCDateTime
    return str(UTCDateTime(epoch))


def clipping_fraction(trace):
    data = np.asarray(trace.data, dtype=float)
    if not len(data):
        return 0.0
    peak = np.max(np.abs(data))
    if peak == 0:
        return 0.0
    return float(np.mean(np.abs(data) >= peak * 0.9999))


# ------------------------------------------------------------------ retrieval
def gather(task, spec, max_stations=6, cache_dir=None):
    rows, station_url = fdsn.station_query(spec)
    groups = fdsn.group_stations(rows)
    center = (spec["center_lat"], spec["center_lon"])
    ranked = []
    for group in groups.values():
        evaluation = fdsn.evaluate_group(group, spec)
        distance = fdsn.haversine_km(center[0], center[1],
                                     group["latitude"], group["longitude"])
        if not evaluation["eligible"]:
            continue
        family = fdsn.choose_family(evaluation, spec["goal"])
        if family is None:
            continue
        ranked.append({"group": group, "evaluation": evaluation,
                       "distance_km": distance, "family": family})
    # nearest first; this is a deterministic acquisition rule, not the decision under test
    ranked.sort(key=lambda r: (r["distance_km"] if r["distance_km"] is not None else 9e9,
                               r["group"]["network"], r["group"]["station"]))
    observations = []
    for candidate in ranked[:max_stations]:
        observation = acquire(candidate, spec, task["task_id"])
        observations.append(observation)
    return {"station_query_url": station_url, "eligible": len(ranked),
            "observations": observations}


def acquire(candidate, spec, task_id):
    group = candidate["group"]
    requests = fdsn.build_requests(group, spec, candidate["family"])
    record = {
        "network": group["network"], "station": group["station"],
        "latitude": group["latitude"], "longitude": group["longitude"],
        "distance_km": None if candidate["distance_km"] is None
        else round(candidate["distance_km"], 2),
        "family": candidate["family"],
        "waveform_url": requests["waveform_url"],
        "metadata_url": requests["metadata_url"],
    }
    try:
        wave, _, sha_w = fdsn.cached_get(requests["waveform_url"], timeout=90)
        meta, _, sha_m = fdsn.cached_get(requests["metadata_url"], timeout=90)
    except Exception as exc:  # noqa: BLE001
        record.update(status="fetch_error", error=f"{type(exc).__name__}: {exc}"[:200])
        return record
    folder = OUT / "b_acquired" / task_id / f"{group['network']}_{group['station']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "waveform.mseed").write_bytes(wave)
    (folder / "metadata.xml").write_bytes(meta)
    record.update(status="fetched", waveform_sha256=sha_w, metadata_sha256=sha_m)
    try:
        from obspy import read, read_inventory
        st = read(str(folder / "waveform.mseed"), format="MSEED")
        inv = read_inventory(str(folder / "metadata.xml"), format="STATIONXML")
        report = quality.inspect(st, inv, requests["job"])
        record["channels"] = sorted(t.id for t in st)
        record["gaps"] = len(report["gaps_or_overlaps"])
        record["three_component"] = report["three_component_checks_pass"]
        record["integrity"] = integrity(st, report)
        z = st.select(component="Z")
        if not len(z):
            record["status"] = "no_vertical"
            return record
        trace = z[0]
        record["sampling_rate_hz"] = trace.stats.sampling_rate
        record["clipping_fraction"] = round(clipping_fraction(trace), 6)
        filtered, problem = bandpassed(trace)
        if filtered is None:
            record["onset_error"] = problem
        else:
            detection, problem = onsets(filtered, trace.stats.sampling_rate)
            if detection is None:
                record["onset_error"] = problem
            else:
                record["onsets"] = detection
                record["first_onset_utc"] = detection["first_onset_utc"]
                record["first_onset_epoch"] = detection["first_onset_epoch"]
        record["status"] = "analysed"
    except Exception as exc:  # noqa: BLE001
        record.update(status="analyse_error", error=f"{type(exc).__name__}: {exc}"[:200])
    return record


def integrity(st, report):
    flagged = []
    for channel in report["channel_checks"]:
        if not channel["finite"]:
            flagged.append(channel["id"] + ":non_finite")
        if channel["constant"]:
            flagged.append(channel["id"] + ":constant")
    if report["gaps_or_overlaps"]:
        flagged.append("gaps_or_overlaps")
    return flagged


# ------------------------------------------------------------------- reasoning
def moveout_fit(observations):
    """Fit t_onset = t0 + distance / v across stations. Returns fit diagnostics."""
    points = [(o["distance_km"], o["first_onset_epoch"])
              for o in observations
              if o.get("first_onset_epoch") is not None
              and o.get("distance_km") is not None]
    if len(points) < 2:
        return {"n": len(points), "fitted": False,
                "reason": "fewer than two stations produced an onset"}
    points.sort()
    d = np.array([p[0] for p in points], dtype=float)
    t = np.array([p[1] for p in points], dtype=float)
    t = t - t.min()
    if np.ptp(d) < 1e-6:
        return {"n": len(points), "fitted": False, "reason": "all stations at the same distance"}
    slope, intercept = np.polyfit(d, t, 1)
    predicted = slope * d + intercept
    residual = t - predicted
    ss_res = float(np.sum(residual ** 2))
    ss_tot = float(np.sum((t - t.mean()) ** 2)) or 1e-12
    r2 = 1.0 - ss_res / ss_tot
    velocity = None if abs(slope) < 1e-9 else 1.0 / slope
    return {
        "n": len(points), "fitted": True,
        "apparent_velocity_km_s": None if velocity is None else round(velocity, 3),
        "r_squared": round(r2, 4),
        "max_abs_residual_s": round(float(np.max(np.abs(residual))), 3),
        "pairs": [{"station": o["station"], "distance_km": o["distance_km"],
                   "onset_utc": o["first_onset_utc"]}
                  for o in observations if o.get("first_onset_epoch") is not None],
    }


def comcat_query(spec, radius_km=120):
    """Evaluator-only independent reference. Never passed to the reasoning step."""
    params = {
        "format": "geojson", "starttime": spec["start"], "endtime": spec["end"],
        "latitude": spec["center_lat"], "longitude": spec["center_lon"],
        "maxradiuskm": radius_km, "minmagnitude": 0,
    }
    url = COMcat + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001
        return {"queried": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    features = data.get("features", [])
    return {"queried": True, "count": len(features),
            "events": [{"id": f["id"], "mag": f["properties"].get("mag"),
                        "place": f["properties"].get("place"),
                        "time_utc": _iso(f["properties"]["time"] / 1000.0)}
                       for f in features[:5]]}


CHECKS = [
    {"id": "C1_channel_integrity",
     "asks": "是否存在恒定通道、非有限值、数据缺口或削波",
     "refutes": ["H_instrument_artifact"], "supports": []},
    {"id": "C2_onset_coherence",
     "asks": "多个台站是否在同一时窗内各自产生触发",
     "refutes": ["H_local_disturbance"], "supports": ["H_propagating_arrival"]},
    {"id": "C3_moveout_fit",
     "asks": "到时随震中距的变化是否能被单一视速度解释",
     "refutes": ["H_local_disturbance"], "supports": ["H_propagating_arrival"]},
    {"id": "C4_catalogue_context",
     "asks": "独立目录是否记录了该时段该区域的事件（评价器专用）",
     "refutes": [], "supports": []},
]


def interpret(observations, fit, catalogue):
    """Turn raw checks into support/refute/unknown states. No LLM, no free text."""
    analysed = [o for o in observations if o.get("status") == "analysed"]
    with_onset = [o for o in analysed if o.get("first_onset_epoch") is not None]
    flagged = [f for o in analysed for f in (o.get("integrity") or [])]
    clipping = [o["station"] for o in analysed
                if (o.get("clipping_fraction") or 0) > 0.05]
    verdicts = {}

    h_prop = []
    if len(with_onset) >= 3:
        h_prop.append(f"{len(with_onset)} 个台站各自产生了触发")
    if fit.get("fitted"):
        v = fit["apparent_velocity_km_s"]
        if v and 1.5 <= v <= 9.0 and fit["r_squared"] >= 0.6:
            h_prop.append(f"到时-距离拟合给出视速度 {v} km/s，R²={fit['r_squared']}")
        else:
            h_prop.append(f"拟合视速度 {v} km/s（R²={fit['r_squared']}）不在合理体波范围")
    verdicts["H_propagating_arrival"] = (
        "supported" if any("视速度" in x and "km/s" in x and "不在" not in x for x in h_prop)
        else "unknown" if len(with_onset) >= 2 else "refuted")
    verdicts["H_local_disturbance"] = (
        "supported" if len(with_onset) <= 1
        else "refuted" if len(with_onset) >= 3 else "unknown")
    verdicts["H_instrument_artifact"] = (
        "supported" if flagged or clipping else "unknown")
    return {
        "verdicts": verdicts,
        "evidence": {
            "stations_analysed": len(analysed),
            "stations_with_onset": len(with_onset),
            "integrity_flags": flagged,
            "clipping_stations": clipping,
            "moveout": fit,
            "catalogue": catalogue,
        },
        "notes": h_prop,
    }