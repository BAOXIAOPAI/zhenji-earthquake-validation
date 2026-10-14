"""Main line core: cases, candidate discovery, acquisition, checks, frozen verdict.

This is the spine that every comparison arm drives. It is deterministic: the
same decision rule is applied to all arms, so the only thing under test is the
acquisition/stopping policy and the question mapping.

No reference material (ComCat, PNW labels, reference picks) is read here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

import fdsn  # noqa: E402
import quality  # noqa: E402


def stability_summary(stream, *, bands_hz=None, target_band_hz=None) -> dict:
    """The compact parameter-stability record attached to one observation.

    `diagnostics` owns the grid; this only keeps the fields a reader and the
    interface need, and turns a check that could not run into an explicit reason
    instead of an absent key (absent would read as "stable").
    """
    import diagnostics  # noqa: PLC0415
    try:
        res = diagnostics.stability(stream, bands_hz=bands_hz,
                                    target_band_hz=target_band_hz)
    except Exception as exc:  # noqa: BLE001
        return {"stability": "check_failed", "spread_s": None,
                "reason": f"{type(exc).__name__}: {exc}"[:200],
                "statement": "参数稳定性检查本身没有跑完，因此不能视为稳定。",
                "next_step": "重跑一次该台站的到时检查；这次的结果不可用。",
                "bands_evaluated": None, "full_grid": bands_hz is None}
    keep = ("stability", "spread_s", "n_configurations", "n_valid", "grid_size",
            "clusters", "onsets_min_s", "onsets_max_s", "onsets_median_s", "mad_s",
            "statement", "next_step", "reason", "bands_evaluated", "full_grid",
            "target_band_hz", "n_configurations_error",
            "n_configurations_not_applicable", "n_configurations_no_pick",
            # per-algorithm bands/counts, the component-alignment check and
            # the unit of every onset. Without these the interface can only show
            # the STA/LTA band and cannot explain a refused three-component pick.
            "detectors", "alignment", "onset_reference")
    return {k: res.get(k) for k in keep if k in res}

RUNS = ROOT / "main/runs"          # historical runs: read-only, never rewritten
# Relocatable run/cache stores. Must match runlog.py: the raw waveform a run wrote
# and the record that describes it have to live under the same root.
RUNLOG = Path(os.environ.get("RUNLOG_DIR") or (ROOT / "main/runlog"))
CACHE = Path(os.environ.get("EQ_CACHE_DIR") or (ROOT / "main/cache"))

# Frozen signal parameters.
#
# METHOD_BAND is the onset detector's own band. It is NOT the band the user asked
# about. The two must be reported separately: a 1–12 Hz question is checked
# against 1–12 Hz, while the detector still runs at 1–8 Hz. Silently substituting
# one for the other is the defect this constant split exists to prevent.
BAND = (1.0, 8.0)
METHOD_BAND = BAND
DEFAULT_TARGET_BAND = (1.0, 12.0)
STA_S, LTA_S, ON, OFF = 0.5, 10.0, 3.5, 1.0
IGNORE_FIRST_S, IGNORE_LAST_S = 15.0, 5.0
COINCIDENCE_WINDOW_S = 3.0
MAX_STATIONS = 6
WINDOW_S = 120.0

# The analysis goal the user asked for. It decides WHICH input condition must
# hold, so it can never be replaced by "the sample rate can carry the band".
DEFAULT_METHOD_GOAL = "three_component_pick"
METHOD_GOAL_LABELS = {
    "three_component_pick": "三分量 P/S 拾取",
    "vertical_onset": "垂直分量到时检查",
}
# Human-readable per-station reason for an input condition that does not hold.
INPUT_REASON_TEXT = {
    "no_analysable_data": "没有可分析的数据",
    "no_band_data": "没有可测的频带数据",
    "nyquist_below_target": "采样率低于目标频带所需",
    "no_vertical_component": "缺少垂直分量",
    "vertical_unhealthy": "垂直分量存在但自身不健康（常值/非有限/覆盖或元数据问题）",
    "stale_condition": "该观测的派生判断基于旧任务条件，尚未按当前条件重判",
    "three_component_missing": "三分量通道不完整或存在坏通道",
    "three_component_ok": "三分量输入条件成立",
    "vertical_ok": "垂直分量输入条件成立",
}

# One channel's own health checks, named so a limitation can point at the exact
# channel and problem instead of saying "missing" for everything.
CHANNEL_PROBLEM_TEXT = {
    "non_finite": "含非有限值",
    "constant": "常值",
    "window_not_covered": "未覆盖请求时窗",
    "metadata_not_unique": "元数据不唯一",
    "rate_metadata_mismatch": "采样率与元数据不符",
    "orientation_not_canonical": "分量方向不是标准 ENZ",
    "no_response": "元数据缺少仪器响应",
    "identity_mismatch": "数据身份与请求不符",
}

# --------------------------------------------------------------------- cases
BOX_DEG = 0.6


def box(lat, lon, deg=BOX_DEG):
    return {"center_lat": lat, "center_lon": lon,
            "minlatitude": round(lat - deg, 4), "maxlatitude": round(lat + deg, 4),
            "minlongitude": round(lon - deg, 4), "maxlongitude": round(lon + deg, 4)}


def _iso_plus(start: str, seconds: float) -> str:
    from obspy import UTCDateTime
    return str(UTCDateTime(start) + seconds)


CASES = [
    # --- development: real catalogued events, paired with quiet controls
    {"case_id": "E1_orcas_event", "kind": "event", "split": "development",
     "start": "2025-03-03T13:02:20", "origin": "2025-03-03T13:02:37",
     "region": box(48.611, -122.805), "question_id": "Q_propagating",
     "expect": "catalogued_event"},
    {"case_id": "Q1_orcas_quiet", "kind": "quiet", "split": "development",
     "start": "2025-03-03T11:00:00", "origin": "2025-03-03T11:00:00",
     "region": box(48.611, -122.805), "question_id": "Q_propagating",
     "expect": "no_catalogued_event"},
    {"case_id": "E2_wauna_event", "kind": "event", "split": "development",
     "start": "2026-09-29T21:05:40", "origin": "2026-09-29T21:05:56",
     "region": box(47.400, -122.722), "question_id": "Q_propagating",
     "expect": "catalogued_event"},
    {"case_id": "Q2_wauna_quiet", "kind": "quiet", "split": "development",
     "start": "2026-09-29T19:00:00", "origin": "2026-09-29T19:00:00",
     "region": box(47.400, -122.722), "question_id": "Q_propagating",
     "expect": "no_catalogued_event"},
    {"case_id": "S1_sparse_2003", "kind": "sparse", "split": "development",
     "start": "2003-01-19T18:43:12", "origin": "2003-01-19T18:43:12",
     "region": box(45.322, -121.651), "question_id": "Q_usable",
     "expect": "not_decidable"},
    # 失败样例：这个时窗内最近的三台站（QGBP/QHSH/QRWC）真实取不到元数据，
    # 而 LUMI/MCW 正常。诚实的回答取决于能否把"这台查不到"与"这台没记录"
    # 区分开，因此它专门用来检验失败归因，而不是用来检验信号判断。
    {"case_id": "F1_qgbp_metadata_gap", "kind": "quiet", "split": "development",
     "start": "2025-03-03T11:00:00", "origin": "2025-03-03T11:00:00",
     "region": box(48.73338, -122.66756), "question_id": "Q_usable",
     "expect": "no_event"},
    {"case_id": "A1_injected_artifact", "kind": "artifact", "split": "development",
     "start": "2025-03-03T11:00:00", "origin": "2025-03-03T11:00:00",
     "region": box(48.611, -122.805), "question_id": "Q_instrument",
     "expect": "instrument_artifact", "injected": True,
     "injection": "第一个取得台站的 E 通道被替换为恒定值（人工注入，非自然观测）；置于安静时窗，"
                  "使伪迹成为主导解释而不是叠加在真实事件上"},
    # 三分量通道演示：同一真实事件窗的坏 E 派生副本，注入固定
    # 到 OLGA，使 R/M/A 三组对照使用同一份派生副本（“同一真实保存波形的
    # 坏E派生副本”）。开发材料，不作独立科学发现。
    {"case_id": "C1_T1_orcas_badE", "kind": "artifact", "split": "development",
     "start": "2025-03-03T13:02:20", "origin": "2025-03-03T13:02:37",
     "region": box(48.611, -122.805), "question_id": "Q_usable",
     "expect": "pick_partially_applicable", "injected": True,
     "inject_station": "OLGA",
     "injection": "已披露的人工注入：OLGA 的 E（东西）通道被替换为恒定值，置于单独的 "
                  "derived 文件，原始下载文件未改动。它是演示/对照用注入数据，不是真实"
                  "观测故障。注入固定在该台站，三组对照使用同一份派生副本。"},
    # --- locked: not used for any tuning
    {"case_id": "E3_ptownsend_event", "kind": "event", "split": "locked",
     "start": "2023-10-09T02:20:50", "origin": "2023-10-09T02:21:08",
     "region": box(48.040, -122.731), "question_id": "Q_propagating",
     "expect": "catalogued_event"},
    {"case_id": "Q3_ptownsend_quiet", "kind": "quiet", "split": "locked",
     "start": "2023-10-09T05:00:00", "origin": "2023-10-09T05:00:00",
     "region": box(48.040, -122.731), "question_id": "Q_propagating",
     "expect": "no_catalogued_event"},
    {"case_id": "E4_quilcene_event", "kind": "event", "split": "locked",
     "start": "2023-12-24T15:13:45", "origin": "2023-12-24T15:14:04",
     "region": box(47.820, -122.960), "question_id": "Q_propagating",
     "expect": "catalogued_event"},
    {"case_id": "Q4_quilcene_quiet", "kind": "quiet", "split": "locked",
     "start": "2023-12-24T10:00:00", "origin": "2023-12-24T10:00:00",
     "region": box(47.820, -122.960), "question_id": "Q_propagating",
     "expect": "no_catalogued_event"},
]

QUESTIONS = {
    "Q_propagating": "这个候选异常是不是多个台站共有的真实传播信号？",
    "Q_instrument": "这个候选异常是不是仪器或元数据问题造成的？",
    "Q_usable": "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
}


def case_by_id(case_id):
    for case in CASES:
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)


# Keyword mapping from a free-text question onto the criterion it implies.
# Deterministic, and NOT a model: `classify_question` exists so that the
# validator answers the question the user actually asked rather than the case's
# preset question. Only an AGH session may use a model for this mapping.
# Word tables, not a semantic model: `classify_question` is a keyword rule and its
# accuracy is unmeasured (see FIXES.md). The tables are ordered, first match wins.
# A bare "通道" used to sit in Q_instrument, so "能不能做三通道拾取" — a question
# about whether the data supports a pick — was read as a question about instrument
# failure. Method words belong to Q_usable; only fault words belong here.
QUESTION_KEYWORDS = (
    ("Q_instrument", ("仪器", "元数据", "设备", "故障", "坏了", "传感器", "标定",
                      "通道故障", "通道异常", "通道坏", "坏通道", "通道有问题",
                      "instrument", "metadata", "artifact", "sensor", "calibrat")),
    ("Q_usable", ("能不能用", "可用", "能用", "可用于", "拾取", "频带", "采样率", "数据质量",
                  "三通道", "三分量", "垂直分量", "到时检查",
                  "usable", "pick", "band", "sampling", "quality")),
)


def classify_question(text, default="Q_propagating"):
    """Map a free-text question onto the review criterion it implies."""
    lowered = (text or "").lower()
    for question_id, words in QUESTION_KEYWORDS:
        if any(word in lowered for word in words):
            return question_id
    return default


def window_of(case):
    return {"start": _iso_plus(case["start"], 0), "end": _iso_plus(case["start"], WINDOW_S)}


def spec_for(case, task=None):
    """Request spec for one case, optionally overridden by a frozen task.

    The task supplies the window and the *target* band the user asked about.
    The method band stays frozen and is recorded as `method_band_hz` so the two
    are never conflated.
    """
    start = task["window_start"] if task else _iso_plus(case["start"], 0)
    end = task["window_end"] if task else _iso_plus(case["start"], WINDOW_S)
    target = tuple(task["target_band_hz"]) if task else DEFAULT_TARGET_BAND
    # A free-input task carries its own window, region, band and goal, so no preset
    # case is needed. `case` may therefore be None (or a synthetic task context).
    spec = {"start": start, "end": end,
            "goal": (task or {}).get("method_goal", "three_component_pick"),
            "band_hz": list(METHOD_BAND),
            "method_band_hz": list(METHOD_BAND),
            "target_band_hz": list(target),
            "case_id": (case or {}).get("case_id")}
    spec.update((task or {}).get("region") or (case or {}).get("region") or {})
    return spec


# ------------------------------------------------------------------ discovery
def discover(case, task=None) -> dict:
    """Candidate stations for a case. Returns anonymous IDs plus user-visible facts."""
    spec = spec_for(case, task)
    rows, station_url = fdsn.station_query(spec)
    groups = fdsn.group_stations(rows)
    # A distance is only meaningful against a point the user actually named. For a
    # station-list request there is no such point, and deriving one from the
    # stations themselves would report a misleading 0 km.
    has_centre = (spec.get("center_lat") is not None
                  and spec.get("center_lon") is not None)
    candidates = []
    for (network, station), group in sorted(groups.items()):
        evaluation = fdsn.evaluate_group(group, spec)
        distance = (fdsn.haversine_km(spec["center_lat"], spec["center_lon"],
                                      group["latitude"], group["longitude"])
                    if has_centre else None)
        family = fdsn.choose_family(evaluation, spec["goal"])
        candidates.append({
            "candidate_id": hashlib.sha256(f"{case['case_id']}|{network}|{station}".encode()).hexdigest()[:12],
            "network": network, "station": station,
            "distance_km": None if distance is None else round(distance, 2),
            "family": family,
            "analysis_eligible": bool(evaluation["eligible"]),
            "three_component": bool(evaluation["three_component"]),
            "vertical_only": bool(evaluation["vertical_only"]),
            "band_support": band_support(_max_rate(evaluation), spec["target_band_hz"]),
            "reason": "；".join(evaluation["reasons"]),
        })
    eligible = [c for c in candidates if c["analysis_eligible"]]
    eligible.sort(key=lambda c: (c["distance_km"] if c["distance_km"] is not None else 9e9,
                                 c["network"], c["station"]))
    return {"station_query_url": station_url, "station_rows": len(rows),
            "candidates": candidates, "eligible": eligible,
            "n_eligible": len(eligible),
            "target_band_hz": list(spec["target_band_hz"]),
            "method_band_hz": list(spec["method_band_hz"])}


def _max_rate(evaluation):
    return max([c["sample_rate_hz"] or 0 for c in evaluation["channel_checks"]] or [0])


def band_support(sampling_rate_hz, target_band_hz) -> dict:
    """Can this sample rate carry the band the user actually asked about?

    Support is a statement about the requested band only. It is not a statement
    that the data are complete, that an onset exists, or that an event is real.
    """
    rate = float(sampling_rate_hz or 0)
    low, high = (float(target_band_hz[0]), float(target_band_hz[1]))
    nyquist = rate / 2.0
    return {"supported": bool(rate > 0 and nyquist > high),
            "sampling_rate_hz": rate or None,
            "nyquist_hz": nyquist or None,
            "target_band_hz": [low, high],
            "required_rate_hz": 2 * high,
            "reason": ("ok" if rate > 0 and nyquist > high
                       else "no_data" if rate <= 0 else "nyquist_below_target")}


# ----------------------------------------------------------------- acquisition
def _emit(state, kind, record=None, message=None):
    """Optional progress hook. No-op unless the caller supplied state['on_step']."""
    hook = state.get("on_step")
    if hook:
        hook({"kind": kind, "record": record, "message": message})


def _run_folder(state, kind, candidate_id):
    folder = RUNLOG / state["run_id"] / kind / candidate_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _read_only(path):
    """The raw download is evidence: make it non-writable so nothing rewrites it."""
    try:
        os.chmod(path, 0o444)
    except OSError:
        pass


def _count(state, **deltas):
    for key, value in deltas.items():
        state[key] = state.get(key, 0) + value


def channel_problems(row) -> list:
    """One channel's own health problems, each named.

    The checks are exactly the per-channel conditions `quality.inspect` measures;
    a channel with no listed problem is healthy. "Z exists" and "Z is usable
    input" are different facts: a constant Z channel is present AND unusable.
    """
    problems = []
    if not row.get("finite"):
        problems.append("non_finite")
    if row.get("constant"):
        problems.append("constant")
    if not row.get("covers_requested_interval_within_one_sample"):
        problems.append("window_not_covered")
    if not row.get("metadata_unique_epoch"):
        problems.append("metadata_not_unique")
    if not row.get("rate_matches_metadata"):
        problems.append("rate_metadata_mismatch")
    if not row.get("canonical_ENZ_orientation"):
        problems.append("orientation_not_canonical")
    if not row.get("response_present"):
        problems.append("no_response")
    if not row.get("requested_identity"):
        problems.append("identity_mismatch")
    return problems


def quality_fields(st, inv, job, target_band_hz) -> dict:
    """Every quality/applicability field, computed together so none can go stale.

    These are deliberately separate fields rather than one global `usable`: one
    station can be good for a vertical onset check and unusable for a
    three-component pick at the same time. A constant horizontal channel makes
    `three_component_ok` false while `vertical_ok` stays true.

    `vertical_ok` means a HEALTHY vertical component exists — the Z channel's own
    finite/constant/coverage/metadata/orientation checks pass — not merely that a
    Z trace is present. A constant Z used to pass as `vertical_ok=True` and let a
    vertical-onset question come back `pick_applicable` on a dead channel.
    """
    report = quality.inspect(st, inv, job)
    flags = integrity_flags(report)
    vertical = st.select(component="Z")
    z_ids = {t.id for t in vertical}
    z_rows = [row for row in report["channel_checks"] if row["id"] in z_ids]
    z_health = {row["id"]: channel_problems(row) for row in z_rows}
    healthy_z = [cid for cid, problems in z_health.items() if not problems]
    vertical_ok = bool(healthy_z)
    rate = float(vertical[0].stats.sampling_rate) if len(vertical) else 0.0
    support = band_support(rate, target_band_hz)
    three = bool(report["three_component_checks_pass"])
    return {
        "channels": sorted(t.id for t in st),
        "gaps": len(report["gaps_or_overlaps"]),
        "channel_integrity_flags": flags,
        "channel_integrity_ok": not flags,
        "three_component_ok": three,
        "vertical_ok": vertical_ok,
        # Per-Z-channel detail, so a limitation can name the channel and the
        # problem instead of collapsing every failure into "no vertical".
        "vertical_health": {
            "ok": vertical_ok,
            "channels_present": sorted(z_health),
            "channels_healthy": sorted(healthy_z),
            "problems": {cid: problems for cid, problems in sorted(z_health.items())
                         if problems},
            "problem_text": {cid: [CHANNEL_PROBLEM_TEXT.get(p, p) for p in problems]
                             for cid, problems in sorted(z_health.items()) if problems},
        },
        "sampling_rate_hz": rate or None,
        "band_support": support,
        "method_applicability": {
            "three_component_pick": bool(three and vertical_ok and support["supported"]),
            "vertical_onset": bool(vertical_ok and support["supported"]),
        },
    }


def eligible_for(record, method="vertical_onset") -> bool:
    """Is this record usable as input to `method`? Method-specific by design."""
    if record.get("status") != "analysed":
        return False
    return bool((record.get("method_applicability") or {}).get(method))


def acquire(case, candidate, state, *, analysis_level=None) -> dict:
    """Download one candidate, keep the raw copy immutable, then analyse it.

    The raw download is written once and made read-only. Any transform we apply
    (the injected flat channel) goes to a separate derived file, and the derived
    hash plus the parent raw hash are recorded. That makes the source chain
    checkable instead of resting on a note that says the hash changed on purpose.

    `analysis_level` selects the thin split (): `"full"` (default, unchanged)
    computes the onset and the stability grid during acquisition; `"quality_only"`
    stops after the input-condition checks so the onset and the grid are genuinely
    computed only when a later tool asks for them. When the caller passes None the
    run state's own `analysis_level` is used, so an MCP run controls it once.
    """
    if analysis_level is None:
        analysis_level = state.get("analysis_level") or "full"
    if analysis_level not in ("full", "quality_only"):
        raise ValueError(f"unknown analysis_level: {analysis_level!r}")
    spec = spec_for(case, state.get("task"))
    _emit(state, "acquire_start", record=candidate)
    requests = fdsn.build_requests(
        {"network": candidate["network"], "station": candidate["station"],
         "latitude": None, "longitude": None}, spec, candidate["family"])
    record = {"candidate_id": candidate["candidate_id"],
              "network": candidate["network"], "station": candidate["station"],
              "distance_km": candidate["distance_km"],
              "family": candidate["family"],
              "window_start": spec["start"], "window_end": spec["end"],
              "target_band_hz": list(spec["target_band_hz"]),
              "method_band_hz": list(spec["method_band_hz"]),
              "waveform_url": requests["waveform_url"],
              "metadata_url": requests["metadata_url"],
              "parent_record_id": None}
    raw = _run_folder(state, "raw", candidate["candidate_id"])
    raw_wave, raw_meta = raw / "waveform.mseed", raw / "metadata.xml"
    # Metadata and waveform are fetched separately so a failure can be attributed
    # to the right request. A single try around both used to report a bare
    # "HTTPError 404", which could not distinguish "this station has no metadata"
    # from "the station exists but recorded nothing in this window" — two
    # situations with opposite next steps.
    attempts = 0
    wave = meta = None
    sha_w = sha_m = None
    cached_w = cached_m = False
    failure = None
    try:
        meta, cached_m, sha_m = fdsn.cached_get(requests["metadata_url"], timeout=90)
        attempts += 0 if cached_m else 1
    except Exception as exc:  # noqa: BLE001
        attempts += 1
        failure = ("metadata_unavailable", "metadata", exc)
    if failure is None:
        try:
            wave, cached_w, sha_w = fdsn.cached_get(requests["waveform_url"], timeout=90)
            attempts += 0 if cached_w else 1
        except Exception as exc:  # noqa: BLE001
            attempts += 1
            failure = ("waveform_unavailable", "waveform", exc)
    if failure is not None:
        kind, which, exc = failure
        _count(state, http_attempts=attempts, logical_acquisitions=1, requests=2)
        record.update(status=kind, acquisition_status=kind,
                      error=f"{type(exc).__name__}: {exc}"[:200],
                      failed_request=which,
                      metadata_available=meta is not None,
                      waveform_available=False,
                      **_failure_guidance(kind, which, record, meta is not None))
        _emit(state, "acquire_done", record=record)
        return record
    # Real attempts only: a cache hit issues no request. `requests` keeps the old
    # "2 per logical acquisition" accounting so the historical arms stay comparable.
    _count(state, http_attempts=attempts, logical_acquisitions=1, requests=2,
           cache_hits=int(bool(cached_w)) + int(bool(cached_m)))
    state["bytes"] = state.get("bytes", 0) + len(wave) + len(meta)
    raw_wave.write_bytes(wave)
    raw_meta.write_bytes(meta)
    _read_only(raw_wave)
    _read_only(raw_meta)
    record.update(status="fetched", acquisition_status="ok",
                  raw_waveform_sha256=sha_w, raw_metadata_sha256=sha_m,
                  waveform_sha256=sha_w, metadata_sha256=sha_m)
    analysis_wave = raw_wave
    # 三分量通道演示：inject_station 把注入固定到指定台站——R/M/A 三组对照必须
    # 用同一份派生坏通道副本，否则“同一真实保存波形的坏E派生副本”不成立。
    # inject_first（第一个取得台站）保持不变，供 A1 演示案例沿用。
    pin = state.get("inject_station")
    pinned = (pin is not None and candidate.get("station") == pin
              and not state.get("_injected"))
    if pinned or (state.get("inject_first") and not state.get("_injected")):
        derived = _derive_flat_channel(state, candidate["candidate_id"], raw_wave, sha_w)
        if derived:
            state["_injected"] = True
            analysis_wave = derived["path"]
            record["derivation"] = derived["record"]
            record["parent_record_id"] = candidate["candidate_id"]
            record["derived_waveform_sha256"] = derived["record"]["derived_sha256"]
            record["injected"] = True
        else:
            # Honest failure: the demo injection was requested but did not apply.
            # Leave `_injected` unset so the next station gets the attempt, and say
            # so on this record instead of letting the banner overclaim.
            record["injection_failed"] = (state.get("_injection_error")
                                         or "unknown reason")
    record = _analyse(candidate, state, record, analysis_wave, raw_meta,
                      analysis_level=analysis_level)
    _emit(state, "acquire_done", record=record)
    return record


def _failure_guidance(kind, which, record, metadata_available) -> dict:
    """Say which request failed, keep what was learned, name the next condition.

    The point is to avoid both a bare error string and a pseudo-explanation. Each
    branch states only what the recorded fact supports: which request failed and
    which question a user would have to settle before this observation can be used.
    """
    station = f"{record['network']}.{record['station']}"
    window = f"{record['window_start']} – {record['window_end']}"
    preserved = ["waveform_url", "metadata_url", "window_start", "window_end",
                 "distance_km", "target_band_hz"]
    if kind == "metadata_unavailable":
        return {
            "evidence_preserved": preserved,
            "distinction": (f"{station} 的台站元数据请求本身失败，因此无法判断该台在"
                            f" {window} 内是否有记录，也无法判断通道条件。"),
            "supplementary_conditions": [
                "确认该台在请求时段内的台站服务是否可用（元数据查询返回的是服务端错误或超时，不是'无数据'）",
                f"在服务恢复后对 {station} 重试同一次查询",
                "若该台长期无元数据，改选元数据可用的邻近台站",
            ],
            "next_step": "先解决元数据可用性，再谈这台站能否支持任何结论。",
        }
    return {
        "evidence_preserved": preserved,
        "distinction": (f"{station} 的台站元数据可用，但 {window} 内的波形请求返回无数据；"
                        f"这是'该台在该时窗内没有记录可下载'，不是'仪器故障'，"
                        f"也不是'该台不存在'。"),
        "supplementary_conditions": [
            "改用确实有记录的时窗重试，而不是把这个空结果当作观测结论",
            "确认该时窗是否落在该台的可用期之内（台站可用期以元数据为准）",
            "若同一时窗内邻近台站有数据，用邻近台站构成独立观测",
        ],
        "next_step": "换时窗或换台站；不要用时窗外的推断填补这个空缺。",
    }


def _analyse(candidate, state, record, waveform_path, metadata_path,
             *, analysis_level="full") -> dict:
    """Compute every derived field for one acquisition. One place, one pass.

    `analysis_level="quality_only"` is the thin split: it downloads, hashes and
    checks the input conditions (channels, health, band support, per-goal
    applicability) but does NOT run the onset detector or the stability grid. The
    book requires this to be a real computation split rather than merely dropping
    tool names: the previous code computed `first_onset` and `stability_summary`
    inside every acquisition, so "the agent decided not to compute it" was false.
    `analysis_level="full"` remains the default so every existing caller keeps its
    behaviour.
    """
    try:
        from obspy import read, read_inventory
        st = read(str(waveform_path), format="MSEED")
        inv = read_inventory(str(metadata_path), format="STATIONXML")
        job = {"case_id": state.get("task_id") or state.get("case_id"),
               "id": candidate["candidate_id"], "net": candidate["network"],
               "sta": candidate["station"], "cha": candidate["family"],
               "start": record["window_start"], "end": record["window_end"]}
        record.update(quality_fields(st, inv, job, record["target_band_hz"]))
        record["method_applicability_goal"] = (
            state.get("method_goal")
            or (state.get("task") or {}).get("method_goal")
            or DEFAULT_METHOD_GOAL)
        z = st.select(component="Z")
        if not len(z):
            record["status"] = "no_vertical"
            record["analysis_level"] = analysis_level
            return record
        if analysis_level == "quality_only":
            # No onset, no stability: both are on-demand computations. The keys are
            # written as explicit "not computed" so a reader can never mistake an
            # absent value for a measured "no onset".
            record["onset_utc"] = None
            record["onset_computed"] = False
            record["onset_source"] = "not_computed"
            record["stability"] = None
            record["stability_computed"] = False
            record["analysis_level"] = "quality_only"
            record["status"] = "analysed"
            return record
        record["onset_utc"] = first_onset(z[0], record["method_band_hz"])
        record["onset_computed"] = True
        record["onset_source"] = "acquire"
        # The parameter-stability check is local (no network, no model) and is part
        # of the observation, not a separate request. It is recorded even when it is
        # unstable: "the automatic onset disagrees with itself" is a result a user
        # needs, and a failed check is reported as a reason, never dropped.
        record["stability"] = stability_summary(
            st, bands_hz=record.get("stability_bands_hz"),
            target_band_hz=record["target_band_hz"])
        record["stability_computed"] = True
        record["analysis_level"] = "full"
        record["status"] = "analysed"
    except Exception as exc:  # noqa: BLE001
        record.update(status="analyse_error", error=f"{type(exc).__name__}: {exc}"[:200])
    return record


def compute_onset(record, waveform_path) -> dict:
    """Actually run the STA/LTA onset detector for one stored observation ().

    Returns `computed=True` only when the detector ran. A missing waveform or a
    missing Z channel is reported as such, never as "no onset".
    """
    from obspy import read
    path = Path(waveform_path)
    if not path.exists():
        return {"computed": False, "reason": "no_stored_waveform", "onset_utc": None}
    st = read(str(path), format="MSEED")
    band = list(record.get("method_band_hz") or METHOD_BAND)
    z = st.select(component="Z")
    if not len(z):
        return {"computed": False, "reason": "no_vertical", "onset_utc": None,
                "band_hz": band}
    return {"computed": True, "reason": "computed",
            "onset_utc": first_onset(z[0], band), "band_hz": band,
            "algorithm": "stalta_z"}


def compute_stability(record, waveform_path, *, bands_hz=None,
                      target_band_hz=None) -> dict:
    """Actually run the declared diagnostic grid for one stored observation."""
    from obspy import read
    path = Path(waveform_path)
    if not path.exists():
        return {"stability": "not_applicable", "reason": "no_stored_waveform",
                "computed": False, "bands_evaluated": None}
    st = read(str(path), format="MSEED")
    summary = stability_summary(st, bands_hz=bands_hz,
                                target_band_hz=target_band_hz)
    summary["computed"] = True
    return summary


def _component(st, component):
    sel = st.select(component=component)
    return np.asarray(sel[0].data, dtype=float) if len(sel) else np.array([])


def integrity_flags(report):
    flags = []
    for channel in report["channel_checks"]:
        if not channel["finite"]:
            flags.append(channel["id"] + ":non_finite")
        if channel["constant"]:
            flags.append(channel["id"] + ":constant")
    if report["gaps_or_overlaps"]:
        flags.append("gaps_or_overlaps")
    return flags


def first_onset(trace, band=METHOD_BAND):
    from obspy.signal.trigger import classic_sta_lta, trigger_onset
    low, high = float(band[0]), float(band[1])
    out = trace.copy()
    out.detrend("linear")
    out.taper(0.02, type="cosine")
    nyq = out.stats.sampling_rate / 2.0
    if not low < high < nyq:
        return None
    out.filter("lowpass", freq=high, corners=4, zerophase=False)
    out.filter("highpass", freq=low, corners=4, zerophase=False)
    fs = out.stats.sampling_rate
    data = np.asarray(out.data, dtype=float)
    if len(data) <= int((STA_S + LTA_S) * fs):
        return None
    cft = classic_sta_lta(data, int(STA_S * fs), int(LTA_S * fs))
    start = float(out.stats.starttime)
    duration = len(data) / fs
    for a, _ in trigger_onset(cft, ON, OFF):
        seconds = float(a) / fs
        if IGNORE_FIRST_S <= seconds <= duration - IGNORE_LAST_S:
            return start + seconds
    return None


def _rel(path) -> str:
    """Repo-relative when possible, absolute otherwise.

    RUNLOG and CACHE are env-configurable, so a run store may legitimately live
    outside the checkout. Recording a derivation must not depend on that.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _derive_flat_channel(state, candidate_id, parent_path, parent_sha):
    """Write the injected variant as a DERIVED file; never touch the raw download."""
    state.pop("_injection_error", None)
    try:
        from obspy import read
        st = read(str(parent_path), format="MSEED")
        sel = st.select(component="E")
        if not len(sel):
            state["_injection_error"] = "no E component in the downloaded stream"
            return None
        params = {"channel": sel[0].id, "replacement": "median_of_original"}
        sel[0].data = np.full(len(sel[0].data), float(np.median(sel[0].data)))
        folder = _run_folder(state, "derived", candidate_id)
        path = folder / "waveform.mseed"
        st.write(str(path), format="MSEED")
        record = {"transform": "flat_channel/1", "params": params,
                  "parent_waveform_sha256": parent_sha,
                  "parent_path": _rel(parent_path),
                  "derived_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                  "derived_path": _rel(path),
                  "synthetic": True}
        (folder / "derivation.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2))
        return {"path": path, "record": record}
    except Exception as exc:  # noqa: BLE001
        # Never fail silently: a disclosed injection that did not happen must be
        # reported as such rather than leaving a banner that claims it did.
        state["_injection_error"] = f"{type(exc).__name__}: {exc}"
        return None


def inject_flat_channel(run_id, candidate_id):
    """Retired: this rewrote raw evidence in place.

    It now raises instead of mutating a stored observation. Use
    `state['inject_first']`, which routes the transform to derived/.
    """
    raise RuntimeError(
        "inject_flat_channel() rewrote raw evidence and is retired; "
        "set state['inject_first'] to derive a separate injected copy instead")


def reanalyse(candidate, state, record, waveform_path=None, metadata_path=None) -> dict:
    """Recompute every derived field from the stored files (used after injection).

    Runs the same `quality_fields()` path as the first analysis, so no field can
    be left stale.
    """
    raw = RUNLOG / state["run_id"] / "raw" / candidate["candidate_id"]
    wave = waveform_path or raw / "waveform.mseed"
    meta = metadata_path or raw / "metadata.xml"
    return _analyse(candidate, state, record, wave, meta)

# ---------------------------------------------------------------------- verdict
def active_records(records):
    """Drop observations the user has explicitly excluded.

    Excluding an observation is a real recomputation, not a display filter: every
    layer below is computed from this list, so a removed bad channel really does
    change the coincidence count and the delivered field.
    """
    return [r for r in records if not r.get("excluded")]


def exclude(records, candidate_id, reason=None):
    """Mark one observation excluded. Returns (records, changed) — no hidden state."""
    changed = False
    for record in records:
        if record.get("candidate_id") == candidate_id and not record.get("excluded"):
            record["excluded"] = True
            record["exclusion_reason"] = reason or "user excluded this observation"
            changed = True
    return records, changed


def included(records, candidate_id, reason=None):
    """Reverse an exclusion (the user changed their mind)."""
    for record in records:
        if record.get("candidate_id") == candidate_id and record.get("excluded"):
            record["excluded"] = False
            record["inclusion_reason"] = reason or "user restored this observation"
            return records, True
    return records, False


def coincidence(records, window_s=COINCIDENCE_WINDOW_S):
    """Largest number of onsets falling inside one `window_s`-wide sliding window.

    Both bounds are required: an earlier onset has a negative difference from a
    later one, so testing only `t - t0 <= window_s` would count every earlier
    onset and inflate the statistic for the latest time in the list.
    """
    times = sorted(r["onset_utc"] for r in records if r.get("onset_utc") is not None)
    if not times:
        return 0
    best = 0
    for i, t0 in enumerate(times):
        count = sum(1 for t in times[i:] if 0.0 <= t - t0 <= window_s)
        best = max(best, count)
    return best


def acquisition_status(records) -> dict:
    """What happened when we tried to get observations. Never a signal claim."""
    analysed = [r for r in records if r.get("status") == "analysed"]
    failed = [r for r in records if r.get("status") in
              ("fetch_error", "analyse_error", "no_vertical")]
    return {
        "attempted": len(records),
        "analysed": len(analysed),
        "failed": len(failed),
        "excluded_by_user": len(records) - len(active_records(records)),
        "failure_reasons": sorted({r.get("status") for r in failed}),
        "note": "missing data means the observation is unavailable; it does NOT mean "
                "the band is unsupported or that no event occurred. "
                "`excluded_by_user` counts data that WERE obtained and are no longer "
                "used in the judgement below",
    }


def channel_integrity(records) -> dict:
    """Per-channel measurable conditions. Not the cause of the candidate anomaly.

    A check can only ever speak about the components actually inspected. The
    previous mapping turned "no flag on the stations I acquired" into a fall-through
    to the onset question, so "did the instrument cause this" was never answered.
    The scope fields below make the limits of a negative answer explicit: stations
    that were never acquired stay `unknown`, not "clean".
    """
    flagged = {}
    checked = {}
    for record in records:
        channels = record.get("channels") or []
        # A record counts as inspected when its channel conditions were actually
        # measured — either the channel list is present or the integrity result is.
        if channels or record.get("channel_integrity_ok") is not None:
            checked[record["station"]] = sorted(channels)
        for flag in (record.get("channel_integrity_flags") or []):
            flagged.setdefault(record["station"], []).append(flag)
    live = {s for s in flagged
            if any(r.get("station") == s and not r.get("excluded") for r in records)}
    return {"stations_with_flags": flagged, "n_flagged": len(flagged),
            "n_flagged_included": len(live),
            "stations_with_flags_excluded": sorted(set(flagged) - live),
            "stations_checked": sorted(checked),
            "components_checked": checked,
            "stations_not_checked": sorted(
                r["station"] for r in records
                if r.get("station") not in checked),
            "scope": "a flagged channel is a checkable anomaly in that channel; it "
                     "does not by itself establish what caused the candidate. "
                     "`stations_checked` lists the stations whose components were "
                     "actually inspected; an unacquired station is unknown, not clean. "
                     "Flags measured on an excluded station are still reported, but "
                     "no longer affect the verdict"}


def _same_band(a, b) -> bool:
    """Two [low, high] bands are the same condition within float tolerance."""
    try:
        return (len(a) == 2 and len(b) == 2
                and abs(float(a[0]) - float(b[0])) < 1e-9
                and abs(float(a[1]) - float(b[1])) < 1e-9)
    except (TypeError, ValueError):
        return False


def _reason_text(row) -> str:
    """The limit reason, naming the concrete channel and problem when known."""
    base = INPUT_REASON_TEXT.get(row["input_reason"], row["input_reason"])
    if row["input_reason"] == "vertical_unhealthy":
        health = row.get("vertical_health") or {}
        named = []
        for cid, problems in (health.get("problem_text")
                              or health.get("problems") or {}).items():
            texts = [CHANNEL_PROBLEM_TEXT.get(p, p) for p in problems]
            named.append(cid + "：" + "、".join(texts))
        if named:
            base += "（" + "；".join(named) + "）"
    return base


def method_applicability(records, target_band_hz, method_goal=None) -> dict:
    """Whether the specified band/component/method conditions hold, per station.

    Three things are kept apart on purpose, because the third review showed that
    collapsing them turns a data gap or a broken channel into a positive method
    answer:

      band_state   can the sample rate carry the band the user asked about?
      input_state  does the *asked method* have the inputs it needs?
      applicability  the record's own frozen `method_applicability[goal]`

    `input_state` is decided by `method_goal`: a three-component pick needs a
    healthy three-component set, a vertical onset check only needs Z. A constant
    horizontal channel makes the first fail while the second still holds, so the
    aggregated answer must move when the user changes the analysis goal.

    Each state is `met`/`supported`, `not_met`/`unsupported` or `unknown`. The
    third state matters: a fetch that returned nothing says nothing about the band
    or the method, and reporting it as a negative would turn a data gap into a
    scientific finding.
    """
    goal = method_goal or DEFAULT_METHOD_GOAL
    if goal not in METHOD_GOAL_LABELS:
        goal = DEFAULT_METHOD_GOAL
    rows = []
    for r in records:
        support = r.get("band_support")
        # A derived judgement is only valid for the conditions it was computed
        # under. A record carried forward from a round with a different target
        # band must be re-judged first (prepare_followup does); if it was not,
        # the honest state here is "unknown", never the stale False/True.
        judged_band = (r.get("target_band_hz")
                       or (support or {}).get("target_band_hz"))
        stale = bool(judged_band) and not _same_band(judged_band, target_band_hz)
        if support is None or stale:
            band_state = "unknown"
        elif support.get("supported"):
            band_state = "supported"
        else:
            band_state = "unsupported"
        analysed = r.get("status") == "analysed"
        vertical_ok = bool(r.get("vertical_ok")) and analysed
        three_ok = bool(r.get("three_component_ok")) and vertical_ok
        # "No Z channel" and "a Z channel exists but failed its own health
        # checks" are different limitations; the second names the channel.
        has_z = any(str(c).split(".")[-1].endswith("Z") for c in (r.get("channels") or []))
        vertical_reason = ("no_vertical_component" if not has_z
                           else "vertical_unhealthy")
        if not analysed:
            input_state, input_reason = "unknown", "no_analysable_data"
        elif stale:
            input_state, input_reason = "unknown", "stale_condition"
        elif band_state == "unknown":
            input_state, input_reason = "unknown", "no_band_data"
        elif band_state == "unsupported":
            input_state, input_reason = "not_met", "nyquist_below_target"
        elif goal == "vertical_onset":
            input_state, input_reason = (("met", "vertical_ok") if vertical_ok
                                         else ("not_met", vertical_reason))
        elif not vertical_ok:
            input_state, input_reason = "not_met", vertical_reason
        elif not three_ok:
            input_state, input_reason = "not_met", "three_component_missing"
        else:
            input_state, input_reason = "met", "three_component_ok"
        rows.append({"candidate_id": r.get("candidate_id"), "station": r.get("station"),
                     "status": r.get("status"), "band_support": support,
                     "support_state": band_state, "band_state": band_state,
                     "stale_condition": stale,
                     "three_component_ok": r.get("three_component_ok"),
                     "vertical_ok": r.get("vertical_ok"),
                     "vertical_health": r.get("vertical_health"),
                     "channels": r.get("channels"),
                     "channel_integrity_flags": r.get("channel_integrity_flags"),
                     "input_state": input_state, "input_reason": input_reason,
                     "applicability": r.get("method_applicability")})
    with_data = [r for r in rows if r["band_state"] != "unknown"]
    supported = [r for r in with_data if r["band_state"] == "supported"]
    unsupported = [r for r in with_data if r["band_state"] == "unsupported"]
    # The input-condition denominator is the stations whose *method* condition was
    # actually decidable, which is not the same as "stations with band data".
    decided = [r for r in rows if r["input_state"] in ("met", "not_met")]
    met = [r for r in decided if r["input_state"] == "met"]
    not_met = [r for r in decided if r["input_state"] == "not_met"]
    unknown = [r for r in rows if r["input_state"] == "unknown"]
    not_met_detail = [{"station": r["station"], "candidate_id": r["candidate_id"],
                       "reason": r["input_reason"],
                       "detail": _reason_text(r),
                       "channels": r.get("channels"),
                       "channel_integrity_flags": r.get("channel_integrity_flags")}
                      for r in not_met]
    band_summary = (f"{len(supported)} 站支持 {float(target_band_hz[0]):g}-"
                    f"{float(target_band_hz[1]):g} Hz 频带（有数据的 {len(with_data)} 站中）")
    goal_summary = _goal_summary(goal, met, decided, not_met_detail, unknown)
    return {
        "target_band_hz": list(target_band_hz),
        "method_goal": goal, "method_goal_label": METHOD_GOAL_LABELS[goal],
        # --- per-station states (the authoritative detail) -------------------
        "per_station": rows,
        # --- band-support layer: one of the three separate answers -----------
        "stations_with_band_data": len(with_data),
        "stations_supporting_target_band": len(supported),
        "stations_not_supporting_target_band": len(unsupported),
        "stations_unknown": len(rows) - len(with_data),
        "stations_supporting": [r["station"] for r in supported],
        "stations_not_supporting": [r["station"] for r in unsupported],
        "stations_with_unknown_support": [r["station"] for r in rows
                                          if r["band_state"] == "unknown"],
        # --- method-input layer: a different answer, with its own denominator -
        "stations_input_decided": len(decided),
        "stations_input_met": len(met),
        "stations_input_not_met": len(not_met),
        "stations_input_unknown": len(unknown),
        "input_conditions_met": [r["station"] for r in met],
        "input_conditions_not_met": not_met_detail,
        "input_conditions_unknown": [r["station"] for r in unknown],
        "band_summary": band_summary,
        "goal_summary": goal_summary,
        "scope": ("`band_summary` answers only whether the sample rate can carry the "
                  "asked band. `goal_summary` answers whether the asked method's own "
                  "inputs hold. Neither says an arrival is identifiable, and `unknown` "
                  "means no measurable data rather than an unsupported band. A record "
                  "whose derived judgement was computed under different task conditions "
                  "is reported as `stale_condition` (unknown), never as its stale "
                  "True/False."),
    }


def _goal_summary(goal, met, decided, not_met_detail, unknown):
    """One readable sentence that names the denominator and the limiting stations."""
    label = METHOD_GOAL_LABELS.get(goal, goal)
    reasons = []
    for row in not_met_detail:
        text = row["detail"]
        flags = row.get("channel_integrity_flags") or []
        if flags:
            text += f"（{'、'.join(flags)}）"
        reasons.append(f"{row['station']}：{text}")
    tail = ("；" + "；".join(reasons)) if reasons else ""
    unknown_note = (f"，另有 {len(unknown)} 站没有可判断的数据"
                    if unknown else "")
    return (f"{label}：{len(met)}/{len(decided)} 个可判断台站满足输入条件"
            f"{unknown_note}{tail}")


def trigger_evidence(records, window_s=COINCIDENCE_WINDOW_S) -> dict:
    """Onset facts for the frozen detector, over the acquired observations only."""
    analysed = [r for r in records if r.get("status") == "analysed"]
    onsets = [{"candidate_id": r["candidate_id"], "station": r["station"],
               "onset_utc": r.get("onset_utc")} for r in analysed
              if r.get("onset_utc") is not None]
    return {"algorithm": "classic_sta_lta/1", "on": ON, "off": OFF,
            "band_hz": list(METHOD_BAND), "window_s": window_s,
            "stations_analysed": len(analysed), "onsets": onsets,
            "max_in_window": coincidence(analysed, window_s),
            "scope": "clustering of detected onset times among the observations "
                     "actually acquired; not a source location, magnitude or causal "
                     "attribution, and not a statement about unsearched stations"}


def hypotheses(records, task) -> dict:
    """What the computed facts support, weaken or leave unknown.

    Only measured quantities appear here. Nothing is inferred past them.
    """
    target = list((task or {}).get("target_band_hz") or DEFAULT_TARGET_BAND)
    goal = (task or {}).get("method_goal") or DEFAULT_METHOD_GOAL
    app = method_applicability(records, target, goal)
    integ = channel_integrity(records)
    trigger = trigger_evidence(records)
    met = app["stations_input_met"]
    decided = app["stations_input_decided"]
    return {
        "instrument_or_metadata_anomaly": {
            "state": "supported" if integ["n_flagged"] else
                     ("unknown" if not records else "not_supported"),
            "basis": f"{integ['n_flagged']} station(s) carry a channel condition flag",
            "limit": "identifies an anomalous channel, not the cause of the candidate"},
        "usable_for_requested_pick": {
            "state": ("supported" if met else
                      ("unknown" if decided == 0
                       else "not_supported")),
            "basis": (f"{met}/{decided} decidable station(s) meet the "
                      f"{app['method_goal_label']} input conditions over "
                      f"{target[0]:g}-{target[1]:g} Hz. "
                      f"Band support: {app['band_summary']}"),
            "limit": "the asked method's input conditions, not that an arrival is "
                     "identifiable"},
        "shared_propagation": {
            "state": ("supported" if trigger["max_in_window"] >= 3 else
                      ("unknown" if trigger["stations_analysed"] < 3 else
                       "not_supported")),
            "basis": f"max {trigger['max_in_window']} onsets within "
                     f"{trigger['window_s']:g} s over {trigger['stations_analysed']} "
                     "analysed station(s)",
            "limit": "onset-time clustering is not a source or causal claim; "
                     "unsearched stations are unknown"},
    }


def next_action(records, question_id, min_stations=3) -> dict:
    """What is still missing, and why we stopped. Concrete, never 'ask an expert'."""
    records = active_records(records)
    analysed = [r for r in records if r.get("status") == "analysed"]
    failed = [r for r in records if r.get("status") in ("fetch_error", "analyse_error")]
    if not records:
        return {"stop": False, "needs": "acquire at least one observation",
                "why": "nothing has been acquired yet"}
    if not analysed and failed:
        return {"stop": True, "needs": "retry the failed stations, or choose another",
                "why": "every attempted acquisition failed; the band and the signal "
                       "are both still unknown"}
    if question_id == "Q_instrument":
        flagged = [r for r in analysed if r.get("channel_integrity_flags")]
        checked = sorted({r["station"] for r in analysed})
        if flagged:
            return {"stop": True, "needs": "none for this question",
                    "why": f"a channel condition is measurable on {flagged[0]['station']}; "
                           "its cause is still not established"}
        # A negative instrument answer is bounded to the components inspected.
        return {"stop": True, "needs": "none identified",
                "why": f"checked {len(checked)} station(s) "
                       f"({', '.join(checked)}); no channel anomaly was found in this "
                       "scope, and stations never acquired remain unknown"}
    if question_id == "Q_usable":
        measured = [r for r in analysed if r.get("band_support")]
        if measured:
            return {"stop": True, "needs": "none identified",
                    "why": f"{len(measured)} station(s) carry measurable band data, so "
                           "the asked band question is answerable for the reviewed set "
                           "(stations not acquired remain unknown)"}
    if not analysed:
        return {"stop": True, "needs": "a station with an analysable vertical component",
                "why": "no acquisition produced analysable data"}
    if len(analysed) < min_stations:
        return {"stop": False,
                "needs": f"at least {min_stations - len(analysed)} more analysed station(s)",
                "why": "fewer than the minimum needed to judge multi-station evidence"}
    return {"stop": True, "needs": "none identified",
            "why": "the minimum number of analysed stations has been reached"}


def stability_grid_bands():
    """The bands of the declared onset grid. One source of truth: `diagnostics`."""
    import diagnostics  # noqa: PLC0415
    return [list(b) for b in diagnostics.GRID["bands_hz"]]


def stability_bands_within(target_band_hz):
    """Declared grid bands that lie inside a target band (fallback: all of them).

    Used to turn "the automatic onset is unstable — narrow the band" into an
    operable, disclosed re-run instead of advice the user cannot execute.
    """
    declared = stability_grid_bands()
    if not target_band_hz:
        return declared
    lo, hi = float(target_band_hz[0]), float(target_band_hz[1])
    inside = [b for b in declared if b[0] >= lo - 1e-9 and b[1] <= hi + 1e-9]
    if inside:
        return inside
    # Nothing sits fully inside the request: take the narrowest declared band that
    # overlaps it, so the action still narrows instead of silently doing nothing.
    overlap = [b for b in declared if b[1] > lo and b[0] < hi]
    return [min(overlap, key=lambda b: b[1] - b[0])] if overlap else declared


def stability_layer(records):
    """Per-station parameter stability, kept as its own layer.

    This is the layer that makes an "input conditions were met" answer readable:
    the band can be supported and the automatic onset still not reproducible.
    """
    out = {}
    for record in active_records(records):
        s = record.get("stability")
        if s:
            out[record.get("station") or record.get("candidate_id")] = s
    return out or None


def layers(records, task, *, question_id=None, robustness=None,
           validation=None) -> dict:
    """The eight separated layers. Meanings are never merged.

    Two of them are *historical facts about what happened* and therefore keep
    reporting excluded observations: excluding a station decides which evidence to
    reason from, it does not claim the data were never obtained. The inference
    layers below use only the included observations.
    """
    active = active_records(records)
    target = list((task or {}).get("target_band_hz") or DEFAULT_TARGET_BAND)
    goal = (task or {}).get("method_goal") or DEFAULT_METHOD_GOAL
    return {
        "acquisition_status": acquisition_status(records),
        "channel_integrity": channel_integrity(records),
        "method_applicability": method_applicability(active, target, goal),
        "trigger_evidence": trigger_evidence(active),
        "robustness": robustness,
        "hypotheses": hypotheses(active, task),
        "validation": validation,
        "next_action": next_action(active, question_id),
        "included_candidates": [r.get("candidate_id") for r in active],
        "excluded_candidates": [r.get("candidate_id") for r in records
                                if r.get("excluded")],
        "scope": "included_candidates lists the observations the judgements above "
                 "were computed from; excluded observations stay visible in "
                 "acquisition_status and channel_integrity as measured facts",
    }


def verdict_of(records, observed_candidate=None, task=None) -> dict:
    """The frozen decision rule (PROTOCOL §2). Identical for every arm.

    The labels are the task's answer vocabulary. `statement` carries the scope so
    a label is never read as more than the rule actually computed.
    """
    records = active_records(records)
    target = list((task or {}).get("target_band_hz") or DEFAULT_TARGET_BAND)
    analysed = [r for r in records if r.get("status") == "analysed"]
    eligible = [r for r in analysed if eligible_for(r, "vertical_onset")]
    flag_target = observed_candidate or (analysed[0]["candidate_id"] if analysed else None)
    observed = next((r for r in analysed if r["candidate_id"] == flag_target), None)
    integrity = bool(observed and observed.get("channel_integrity_flags"))
    n = len(eligible)
    co = coincidence(analysed)
    base = {"coincidence": co, "eligible_stations": n, "integrity_flag": integrity,
            "target_band_hz": target, "method_band_hz": list(METHOD_BAND)}
    if not records:
        return {**base, "verdict": "not_decidable", "reason": "no_observations",
                "statement": "No observation was acquired, so nothing can be judged.",
                "statement_zh": "没有取得任何观测，因此无法作出判断。"}
    if not analysed:
        return {**base, "verdict": "not_decidable", "reason": "no_analysable_data",
                "statement": "No station produced analysable data; the band support "
                             "and the signal are both still unknown.",
                "statement_zh": "没有任何台站产出可分析的数据；频带支持与信号两者"
                                "目前都还是未知。"}
    if n < 3:
        return {**base, "verdict": "not_decidable", "reason": "insufficient_stations",
                "statement": f"Only {n} analysable station(s) in the searched set. "
                             "No clustering here says nothing about unsearched stations.",
                "statement_zh": f"被搜索到的可分析台站只有 {n} 个；这里没有出现聚类，"
                                "不能说明未被搜索的台站是什么情况。"}
    if integrity and co <= 1:
        return {**base, "verdict": "instrument_artifact",
                "reason": "observed_channel_flagged",
                "statement": "A checkable channel anomaly is present at the reviewed "
                             "station and no onset clustering was found. This does not "
                             "by itself establish the cause of the candidate.",
                "statement_zh": "被复核的台站上存在可检查的通道异常，且没有发现到时聚类。"
                                "这本身还不能确定该候选信号的成因。"}
    if co >= 3:
        return {**base, "verdict": "coherent_multistation",
                "reason": "coincident_onsets>=3",
                "statement": f"{co} onset times fall inside one "
                             f"{COINCIDENCE_WINDOW_S:g} s window. Onset clustering is "
                             "not a source or causal claim.",
                "statement_zh": f"{co} 个到时落在同一个 "
                                f"{COINCIDENCE_WINDOW_S:g} 秒窗口内。到时聚类不等于"
                                "震源结论，也不等于成因结论。"}
    if co <= 1:
        return {**base, "verdict": "single_station_only",
                "reason": "coincident_onsets<=1",
                "statement": f"At most {co} onset(s) cluster within "
                             f"{COINCIDENCE_WINDOW_S:g} s over the {n} analysable "
                             "station(s) searched. Stations not searched are unknown; "
                             "this is not a statement that no clustering exists.",
                "statement_zh": f"在已搜索的 {n} 个可分析台站中，最多 {co} 个到时落在 "
                                f"{COINCIDENCE_WINDOW_S:g} 秒内。未被搜索的台站仍是未知；"
                                "这不等于“不存在聚类”。"}
    return {**base, "verdict": "not_decidable", "reason": "ambiguous_coincidence",
            "statement": f"{co} onsets cluster, which is below the rule's decisive "
                         "threshold and above the single-station case.",
            "statement_zh": f"{co} 个到时聚类，低于本规则的决定性阈值，"
                            "又高于单台站情形。"}


# ------------------------------------------------------------------- question mapping
#
# Each real user question gets its own answer vocabulary, its own required evidence
# and its own stop condition. The previous version answered only the coherence
# question: a band question with partial support, or an instrument question with no
# flag found, "fell through to coherence" and was delivered as an onset-clustering
# statement. Those are different fields and are never substituted for each other.
#
# `verdict` is the answer label for the question that was asked. `answered` carries
# the value of the specific field, whether that field was actually settled, and the
# scope it was settled over. `delivered`/UI keep the two apart.
ANSWER_LABELS = {
    "Q_propagating": ("coherent_multistation", "single_station_only", "not_decidable"),
    "Q_instrument": ("instrument_artifact", "no_instrument_anomaly_in_scope",
                     "instrument_unknown"),
    "Q_usable": ("pick_applicable", "pick_partially_applicable", "pick_not_applicable",
                 "pick_unknown"),
}

# The honest "no measurable data" label for each question. The frozen propagation
# rule always says `not_decidable`, so spreading its result into a band or channel
# question would answer that question with a propagation label. Keep them apart.
UNKNOWN_LABELS = {
    "Q_propagating": "not_decidable",
    "Q_instrument": "instrument_unknown",
    "Q_usable": "pick_unknown",
}


def _unanswered(question_id, field, base, detail):
    """The honest "no measurable data" answer: unknown, and not settled.

    `base` is the frozen propagation rule's result, so its label is a propagation
    label. Replacing it with the asked question's own unknown label is what keeps a
    band question from being answered with `not_decidable` (review ).
    """
    return {**base, "answered_question": question_id,
            "verdict": UNKNOWN_LABELS.get(question_id, base.get("verdict")),
            "terminated_by": "unanswered_no_data", "field_answered": None,
            "answered": {"field": field, "answered": False, "value": "unknown",
                         "answer_kind": "unknown", "detail": detail,
                         "scope": {}},
            # Keep the frozen rule's scope statement: "nothing can be judged" is a
            # statement about the review, not a negative finding about the window.
            "statement": base.get("statement") or (
                "No observation produced measurable data, so this field stays unknown "
                "rather than being answered as a negative.")}


def verdict_for_question(records, question_id, observed_candidate=None, task=None) -> dict:
    """Answer the user's actual question, with its own field, scope and stop reason.

    The label returned in `verdict` belongs to the question's own vocabulary; the
    `answered` block says which field settles it and over what scope. A coherence
    label is only delivered for a coherence question.
    """
    records = active_records(records)
    target = list((task or {}).get("target_band_hz") or DEFAULT_TARGET_BAND)
    analysed = [r for r in records if r.get("status") == "analysed"]
    base = verdict_of(records, observed_candidate, task)
    if not analysed:
        return _unanswered(question_id, _asked_field(question_id), base,
                           "no observation produced measurable data")

    if question_id == "Q_instrument":
        integ = channel_integrity(records)
        flagged = sorted(integ["stations_with_flags"])
        checked = sorted(integ["stations_checked"])
        unchecked = sorted(set(integ["stations_not_checked"]))
        scope = {"stations_checked": checked, "components_checked":
                 integ["components_checked"], "stations_with_flags": flagged,
                 "stations_not_checked": unchecked}
        if flagged:
            health = ["measured channel anomaly"]
            label, reason, value, kind = ("instrument_artifact",
                                          "observed_channel_flagged", True, "positive")
            detail = (f"在已检查的分量上测到通道异常：{flagged}。"
                      "这只定位到异常通道，不等于已确定候选异常的成因。")
            statement = ("A channel condition is measurable at the reviewed station; "
                         "this identifies the channel anomaly, not the cause of the "
                         "candidate.")
            statement_zh = ("被复核的台站上可以测到通道条件；这定位到的是通道异常，"
                            "不是候选异常的成因。")
        else:
            label, reason, value, kind = ("no_instrument_anomaly_in_scope",
                                          "no_channel_anomaly_in_scope", False, "negative")
            detail = (f"已检查 {len(checked)} 个台站的通道条件（{checked}），"
                      f"在这个范围内未发现可测量的通道异常；"
                      f"{len(unchecked)} 个台站未被检查，仍是未知。")
            statement = (f"Within the {len(checked)} station(s) actually inspected "
                         f"({', '.join(checked) or 'none'}), no channel anomaly was "
                         "measured. Stations never acquired remain unknown; this is "
                         "not a global statement that the instrument was healthy.")
            statement_zh = (f"在实际检查过的 {len(checked)} 个台站"
                            f"（{', '.join(checked) or '无'}）上没有测到通道异常。"
                            f"{len(unchecked)} 个台站没有被取到，仍是未知；"
                            "这不是“仪器完好”的全局结论。")
        return {**base, "verdict": label, "reason": reason,
                "coincidence": base["coincidence"],
                "eligible_stations": base["eligible_stations"],
                "integrity_flag": bool(flagged), "target_band_hz": target,
                "method_band_hz": list(METHOD_BAND),
                "answered_question": question_id, "terminated_by": "instrument_check",
                "field_answered": "channel_integrity_flags",
                "answered": {"field": "channel_integrity_flags", "answered": True,
                             "value": value, "answer_kind": kind, "detail": detail,
                             "scope": scope},
                "statement": statement, "statement_zh": statement_zh}

    if question_id == "Q_usable":
        goal = (task or {}).get("method_goal") or DEFAULT_METHOD_GOAL
        app = method_applicability(records, target, goal)
        measured = app["stations_with_band_data"]
        decided = app["stations_input_decided"]
        met = app["stations_input_met"]
        not_met = app["input_conditions_not_met"]
        if measured == 0:
            return _unanswered(question_id, "method_applicability", base,
                               "no observation produced measurable band data")
        scope = {"goal": goal, "goal_label": app["method_goal_label"],
                 "stations_supporting": app["stations_supporting"],
                 "stations_not_supporting": app["stations_not_supporting"],
                 "stations_with_unknown_support": app["stations_with_unknown_support"],
                 "target_band_hz": target,
                 "stations_input_met": app["input_conditions_met"],
                 "stations_input_not_met": [
                     {"station": r["station"], "reason": r["reason"],
                    "detail": r.get("detail"),
                    "channel_integrity_flags": r.get("channel_integrity_flags")}
                     for r in not_met],
                 "stations_input_unknown": app["input_conditions_unknown"],
                 "band_summary": app["band_summary"],
                 "goal_summary": app["goal_summary"]}
        limit = "；".join(f"{r['station']}：{r['detail']}" for r in not_met) or "—"
        if decided == 0:
            # Band data exist, but the asked method's condition could not be decided
            # on any station (e.g. no vertical component anywhere). That is unknown,
            # not a positive method answer.
            return _unanswered(question_id, "method_applicability", base,
                               "no station had a decidable input condition for "
                               f"{app['method_goal_label']}")
        if met == decided:
            label, reason, value, kind = ("pick_applicable", "input_conditions_met",
                                          True, "positive")
            detail = (f"{met}/{decided} 个可判断台站满足{app['method_goal_label']}的输入条件；"
                      f"频带方面：{app['band_summary']}。"
                      "这只说方法输入条件成立，不说明到时一定可辨识。")
        elif met == 0:
            # The band may be supported while the method's own inputs still fail.
            only_band = all(r["reason"] == "nyquist_below_target" for r in not_met)
            reason = "band_unsupported" if only_band else "input_conditions_not_met"
            label, value, kind = ("pick_not_applicable", False, "negative")
            detail = (f"没有任何可判断台站满足{app['method_goal_label']}的输入条件。"
                      f"频带方面：{app['band_summary']}。限制：{limit}")
        else:
            label, reason, value, kind = ("pick_partially_applicable",
                                          "input_conditions_partial",
                                          "partial", "partial")
            detail = (f"{met}/{decided} 个可判断台站满足{app['method_goal_label']}的输入条件，"
                      f"{decided - met} 个不满足。频带方面：{app['band_summary']}。"
                      f"限制：{limit}。这是部分适用的回答，不是对全部台站的判断。")
        statement = (f"Method applicability was judged against the goal the user "
                     f"asked for ({app['method_goal_label']}), not by the band count "
                     f"alone. {app['goal_summary']}. Input conditions met is not the "
                     "same as a P/S arrival being identifiable.")
        statement_zh = (f"这次是按你要求的目标（{app['method_goal_label']}）判断方法是否适用，"
                        f"不是只看频带数量。{app['goal_summary']}。"
                        "输入条件满足 ≠ 已经识别出 P/S 震相。")
        return {**base, "verdict": label, "reason": reason,
                "coincidence": base["coincidence"],
                "eligible_stations": base["eligible_stations"],
                "target_band_hz": target, "method_band_hz": list(METHOD_BAND),
                "method_goal": goal,
                "answered_question": question_id,
                "terminated_by": ("band_check" if met in (0, decided)
                                  else "band_partial"),
                "field_answered": "method_applicability",
                "answered": {"field": "method_applicability", "answered": True,
                             "value": value, "answer_kind": kind, "detail": detail,
                             "scope": scope},
                "statement": statement, "statement_zh": statement_zh}

    # Q_propagating (and any question whose deciding field really is the label):
    # the coherence rule is the answer, and `answered_field` says whether it settled.
    return {**base, "answered_question": question_id,
            "terminated_by": "coherence", "field_answered": "verdict",
            "answered": answered_field(base, question_id)}


def _asked_field(question_id):
    return {"Q_instrument": "channel_integrity_flags",
            "Q_usable": "method_applicability"}.get(question_id, "verdict")


def question_is_decided(records, question_id, observed_candidate=None, task=None) -> bool:
    """Has the asked field been answered for the scope reviewed so far?

    The stop condition is now the question's own field, not the coherence rule: an
    instrument or band question is settled as soon as one inspected station settles
    it, and a partial band answer is a real answer with its own scope.
    """
    result = verdict_for_question(records, question_id, observed_candidate, task)
    return bool((result.get("answered") or {}).get("answered"))


def answered_field(verdict: dict, question_id: str) -> dict:
    """The verdict field that answers the question, and whether it truly does."""
    if question_id == "Q_instrument":
        answered = bool(verdict.get("integrity_flag"))
        return {"field": "channel_integrity_flags", "answered": answered,
                "value": answered, "answer_kind": "positive" if answered else "unknown",
                "detail": verdict["verdict"], "scope": {}}
    if question_id == "Q_usable":
        reason = verdict.get("reason")
        if reason == "band_unsupported":
            return {"field": "method_applicability", "answered": True,
                    "value": False, "answer_kind": "negative",
                    "detail": "no station supports the target band", "scope": {}}
        if reason == "no_analysable_data":
            return {"field": "method_applicability", "answered": False,
                    "value": "unknown", "answer_kind": "unknown",
                    "detail": "no measurable data", "scope": {}}
        return {"field": "method_applicability", "answered": False,
                "value": "unknown", "answer_kind": "unknown",
                "detail": "band applicability and arrival identifiability are "
                          "separate; this run did not settle the asked field",
                "scope": {}}
    # A `not_decidable` outcome is an honest "not enough evidence", not an answer.
    # Counting it as settled is what let uncertain quiet windows score as passes.
    settled = verdict.get("reason") not in (
        "insufficient_stations", "ambiguous_coincidence", "no_observations",
        "no_analysable_data")
    return {"field": "verdict", "answered": settled,
            "value": verdict["verdict"] if settled else "unknown",
            "answer_kind": "label" if settled else "unknown",
            "detail": verdict["reason"], "scope": {}}

# --------------------------------------------------------------------------- arms
def arm_full_evidence(case, discovery, state):
    for candidate in discovery["eligible"][:MAX_STATIONS]:
        state["records"].append(acquire(case, candidate, state))
    return "full_evidence"


def arm_fixed_pipeline(case, discovery, state):
    for candidate in discovery["eligible"][:3]:
        state["records"].append(acquire(case, candidate, state))
    return "fixed_pipeline"


def arm_adaptive_rule(case, discovery, state):
    """Coherence-only stopping: ignores what was actually asked."""
    task = state.get("task")
    for candidate in discovery["eligible"][:MAX_STATIONS]:
        state["records"].append(acquire(case, candidate, state))
        current = verdict_of(state["records"], state.get("observed_candidate"), task)
        if current["reason"] not in ("insufficient_stations", "ambiguous_coincidence",
                                     "no_observations"):
            break
    return "adaptive_rule"


def arm_adaptive_question(case, discovery, state):
    """Question-conditioned stopping: stop once the asked question is answered."""
    question_id = state.get("question_id") or case["question_id"]
    task = state.get("task")
    for candidate in discovery["eligible"][:MAX_STATIONS]:
        state["records"].append(acquire(case, candidate, state))
        if question_is_decided(state["records"], question_id,
                               state.get("observed_candidate"), task):
            break
    return "adaptive_question"