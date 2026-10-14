"""Limited, pre-declared stability diagnostic for one automatic onset.

The guide (G1 4.2, loop two) asks for exactly this: a **bounded, pre-specified**
tool that shows when a single onset estimate cannot be trusted. It is fixed here
and never tuned per case — the grid below is the whole menu.

Two properties matter and are enforced by construction:

* **No new network access.** `stability()` reads a waveform already on disk.
  Asking for "no new fetches" therefore still permits the full diagnostic.
* **No reference pick.** The curated arrival time is not a parameter and is not
  returned. It stays in the evaluator, so it can never influence the method choice
  or the reported stability.
"""
from __future__ import annotations

import numpy as np

# The complete, pre-declared diagnostic grid. Changing this changes the protocol.
GRID = {
    "bands_hz": [[1.0, 8.0], [3.0, 12.0], [5.0, 20.0]],
    "thresholds": [3.0, 3.5, 4.0, 5.0],
    "detectors": ["stalta_z", "ar_aic_3c"],
    "sta_s": 0.5,
    "lta_s": 10.0,
    "on": 3.5,
    "off": 1.0,
    "ignore_first_s": 15.0,
    "ignore_last_s": 5.0,
}
CLUSTER_S = 2.0        # onsets within this are treated as the same estimate
STABLE_SPREAD_S = 1.0  # a spread at or below this is reported as stable
# Agreement among one or two configurations is not agreement. Below this the
# tool reports that it cannot judge stability rather than calling it stable.
MIN_CONFIGURATIONS_FOR_STABILITY = 3


def grid_size():
    return len(GRID["bands_hz"]) * len(GRID["thresholds"]) + 1


def _validated_bands(bands_hz, declared):
    """The one entry point for a requested diagnostic grid.

    Delegates to `execution_conditions` so the CLI, the MCP tool, the feedback
    guard and the scorer all refuse exactly the same inputs. `None` is the only
    way to ask for the full declared grid.
    """
    import execution_conditions  # noqa: PLC0415
    try:
        return execution_conditions.normalize_diagnostic_bands(
            bands_hz, allowed=declared, field="bands_hz")
    except execution_conditions.ConditionError as exc:
        raise ValueError(str(exc)) from exc


def _stalta_onset(data, fs, start_epoch, band, threshold):
    from obspy import Trace, UTCDateTime
    from obspy.signal.trigger import classic_sta_lta, trigger_onset
    low, high = float(band[0]), float(band[1])
    if not low < high < fs / 2.0:
        return None, "nyquist_below_band"
    trace = Trace(np.asarray(data, dtype=float))
    trace.stats.sampling_rate = fs
    trace.stats.starttime = UTCDateTime(start_epoch)
    trace.detrend("linear")
    trace.taper(0.02, type="cosine")
    trace.filter("lowpass", freq=high, corners=4, zerophase=False)
    trace.filter("highpass", freq=low, corners=4, zerophase=False)
    values = np.asarray(trace.data, dtype=float)
    if len(values) <= int((GRID["sta_s"] + GRID["lta_s"]) * fs):
        return None, "window_too_short"
    cft = classic_sta_lta(values, int(GRID["sta_s"] * fs), int(GRID["lta_s"] * fs))
    duration = len(values) / fs
    for a, _ in trigger_onset(cft, threshold, GRID["off"]):
        seconds = float(a) / fs
        if GRID["ignore_first_s"] <= seconds <= duration - GRID["ignore_last_s"]:
            return seconds, "ok"
    return None, "no_trigger"


def _ar_aic_pick(components, fs, band=(1.0, 20.0)):
    """AR-AIC P/S picker; needs three components and enough Nyquist, else reports why.

    Returns (p, s, status, reason). `status` is one of:

      ok             the picker ran and produced a P onset
      no_pick        the picker ran but found no usable phase
      not_applicable the data cannot be used (no 3 components, Nyquist too low)
      error          the picker itself failed to execute

    The previous version passed 19 positional arguments to an `ar_pick` that
    accepts 15, so every call raised and was caught as `ar_pick_failed`. That made
    a tool-execution failure look like the algorithms disagreeing, and left the
    nominally 13-configuration grid with only the 12 STA/LTA entries ever running.
    """
    low, high = float(band[0]), float(band[1])
    if fs <= 0 or not low < high < fs / 2.0:
        return None, None, "not_applicable", "nyquist_below_band"
    if len(components) < 3:
        return None, None, "not_applicable", "needs_three_components"
    try:
        from obspy.signal.trigger import ar_pick
    except Exception as exc:  # noqa: BLE001
        return None, None, "error", f"ar_pick_unavailable:{type(exc).__name__}"
    z, n, e = (np.asarray(c, dtype=float) for c in components[:3])
    if not (len(z) == len(n) == len(e)):
        return None, None, "not_applicable", "component_length_mismatch"
    try:
        # Installed obspy signature (1.5.1):
        #   ar_pick(a, b, c, samp_rate, f1, f2, lta_p, sta_p, lta_s, sta_s,
        #           m_p, m_s, l_p, l_s, s_pick=True)
        # `m_p`/`m_s` are integer AR orders; `l_p`/`l_s` are seconds. The parameters
        # are declared here, once, and are not tuned per case.
        p, s = ar_pick(z, n, e, fs, low, high, 1.0, 0.5, 4.0, 1.0,
                       2, 2, 0.1, 0.2, s_pick=True)
    except Exception as exc:  # noqa: BLE001
        return None, None, "error", f"ar_pick_error:{type(exc).__name__}"
    duration = len(z) / fs
    p_ok = bool(np.isfinite(p) and 0 < float(p) < duration)
    if not p_ok:
        return None, None, "no_pick", "p_not_found"
    s_value = float(s) if (np.isfinite(s) and float(p) < float(s) < duration) else None
    return float(p), s_value, "ok", "ok"


def _cluster(onsets):
    """Group onsets that agree within CLUSTER_S, largest cluster first."""
    ordered = sorted(onsets)
    clusters = []
    for value in ordered:
        if clusters and value - clusters[-1]["values"][-1] <= CLUSTER_S:
            clusters[-1]["values"].append(value)
        else:
            clusters.append({"values": [value]})
    for cluster in clusters:
        cluster["n"] = len(cluster["values"])
        cluster["onset_s"] = float(np.median(cluster["values"]))
    clusters.sort(key=lambda c: (-c["n"], c["onset_s"]))
    return clusters


def _alignment(traces):
    """Do the components that a three-component pick uses share one time base?

    The previous version took the sampling rate and the start time from the first
    trace in the stream and applied them to every component. A probe that changed
    one component's header then produced estimates *outside the window* instead of
    a clear refusal. A three-component comparison is only meaningful after this
    check passes; nothing here resamples silently.
    """
    rows = []
    for component in ("Z", "N", "E"):
        sel = traces.select(component=component)
        if not len(sel):
            continue
        stats = sel[0].stats
        rows.append({"component": component,
                     "sampling_rate_hz": float(stats.sampling_rate),
                     "starttime": str(stats.starttime),
                     "n_samples": int(stats.npts),
                     "endtime": str(stats.endtime)})
    if len(rows) < 2:
        return {"checked_components": rows, "aligned": None,
                "reason": "fewer_than_two_components",
                "common_coverage": None}
    if len({r["sampling_rate_hz"] for r in rows}) > 1:
        return {"checked_components": rows, "aligned": False,
                "reason": "sampling_rate_mismatch", "common_coverage": None}
    if len({r["starttime"] for r in rows}) > 1:
        return {"checked_components": rows, "aligned": False,
                "reason": "start_time_mismatch", "common_coverage": None}
    start = max(r["starttime"] for r in rows)
    end = min(r["endtime"] for r in rows)
    if end <= start:
        return {"checked_components": rows, "aligned": False,
                "reason": "no_common_coverage", "common_coverage": None}
    return {"checked_components": rows, "aligned": True, "reason": "aligned",
            "common_coverage": {"start": start, "end": end}}


def _detector_disclosure(estimates):
    """Per-algorithm bands and outcome counts, so a narrowed grid is not misread.

    The review's P1-4: "收紧到 3–12 Hz" was really four STA/LTA configs at 3–12 Hz
    plus one AR-AIC at 1–20 Hz, while `bands_evaluated` disclosed only the former.
    Each algorithm now lists its own bands and its own outcome counts.
    """
    out = {}
    for estimate in estimates:
        row = out.setdefault(estimate["detector"], {
            "bands_hz": set(), "n_configurations": 0, "n_ok": 0,
            "n_no_pick": 0, "n_not_applicable": 0, "n_error": 0})
        row["bands_hz"].add(tuple(estimate["band_hz"]))
        row["n_configurations"] += 1
        status = estimate.get("status")
        key = {"ok": "n_ok", "no_pick": "n_no_pick",
               "not_applicable": "n_not_applicable", "error": "n_error"}.get(status)
        if key:
            row[key] += 1
    return {name: {**row, "bands_hz": [list(b) for b in sorted(row["bands_hz"])]}
            for name, row in out.items()}


def classify_spread(valid, clusters, spread):
    """Turn a spread + clusters into an honest name, statement and next step.

    Kept as a pure function so the real TURTL shape (12 of 13 configurations in one
    cluster plus a single outlier) can be pinned without synthesising a waveform.
    The previous wording called that "没有形成多数一致的簇", which is false.
    """
    values = np.asarray(valid, dtype=float)
    big = [c for c in clusters if c["n"] >= 2]
    if spread <= STABLE_SPREAD_S:
        return ("stable",
                f"全部 {len(valid)} 个配置的到时跨度仅 {spread:.2f} 秒，"
                "在这个有限网格内是一致估计。",
                "可以按这个到时继续；仍不构成对事件存在或其来源的证明。")
    if len(big) >= 2:
        return ("unstable_bimodal",
                f"{len(valid)} 个配置给出两个以上互相分离的到时簇"
                f"（最大簇 {big[0]['n']} 个在 {big[0]['onset_s']:.1f} 秒，"
                f"次大簇 {big[1]['n']} 个在 {big[1]['onset_s']:.1f} 秒），"
                f"跨度 {spread:.1f} 秒。",
                "单一到时不可用：需要人工查看这两个时刻附近的波形图，"
                "或先收紧频带再复核；不要把其中任何一个当作确定到时。")
    if big and big[0]["n"] > len(valid) / 2:
        outliers = [c for c in clusters if c is not big[0]]
        outlier_s = "、".join(f"{c['onset_s']:.1f}" for c in outliers[:3])
        return ("unstable",
                f"{len(valid)} 个配置中有 {big[0]['n']} 个落在主要簇 "
                f"{big[0]['onset_s']:.1f} 秒（多数），但完整配置集合仍有 "
                f"{len(valid) - big[0]['n']} 个离群"
                + (f"（在 {outlier_s} 秒）" if outlier_s else "")
                + f"，跨度 {spread:.1f} 秒。",
                "多数簇存在，但完整配置集合里仍有明显离群："
                "可以报告主要簇及其离群范围，不要把这个到时当作唯一确定值；"
                "或先收紧频带再复核。")
    return ("unstable",
            f"{len(valid)} 个配置的到时分散在 {values.min():.1f}–{values.max():.1f} 秒，"
            f"跨度 {spread:.1f} 秒，没有形成多数一致的簇。",
            "这个到时不稳定，不足以支持任何结论；"
            "建议直接报告为无法确定，并说明已检查的配置范围。")


def stability(stream, *, bands_hz=None, target_band_hz=None):
    """Run the declared grid over one waveform already on disk.

    The grid is fixed by default. `bands_hz` restricts it to a subset of the
    declared bands — that is a *different, disclosed* check, not a silent way to
    make an unstable result look stable: the reply always says which bands were
    evaluated and whether the full declared grid was used.

    Returns the spread, the clusters and an actionable next step. The reference
    arrival time is deliberately absent from the return value.
    """
    from obspy import read
    declared = [list(b) for b in GRID["bands_hz"]]
    # O1: `None` means "the full declared grid"; an explicitly supplied grid is
    # validated and refused if it is empty, non-finite, reversed, duplicated or
    # outside the pre-declared menu. The previous `bands_hz or GRID[...]` made an
    # empty list fall back to the full grid, so a user who asked for a narrower
    # diagnostic silently got the default one and the run was still scored as if
    # the request had been honoured.
    bands = _validated_bands(bands_hz, declared)
    full_grid = bands == declared
    ctx = {"bands_evaluated": bands, "full_grid": full_grid,
           "target_band_hz": (list(target_band_hz) if target_band_hz else None)}

    def result(*args, **kwargs):
        return _result(*args, **{**ctx, **kwargs})

    if isinstance(stream, (str, bytes)) or hasattr(stream, "__fspath__"):
        stream = read(str(stream), format="MSEED")
    traces = stream
    if not len(traces):
        return result("not_applicable", [], "波形为空", "重新获取该台站的数据",
                      reason="empty_stream")
    z = traces.select(component="Z")
    if not len(z):
        return result("not_applicable", [], "没有垂直分量，无法做到时检查",
                      "换一个含垂直分量的台站，或改用其它台站交叉验证",
                      reason="no_vertical")
    z_data = np.asarray(z[0].data, dtype=float)
    # Degenerate channels must be named as such. Filtering a constant trace
    # produces numerical rubbish that a detector will happily "trigger" on.
    if not np.all(np.isfinite(z_data)):
        return result("not_applicable", [], "垂直分量含非有限值，无法做任何到时检查",
                      "这是一条坏通道：先确认通道状态，或换一个台站交叉验证",
                      reason="non_finite_channel")
    if float(np.ptp(z_data)) == 0.0:
        return result("not_applicable", [],
                      "垂直分量为常值，整段没有任何变化，不存在可检测的到时",
                      "这是一条坏的或无数据的通道，不能用来判断有无事件；"
                      "先排除该通道或换台站，不要把它当作'没有到时'",
                      reason="constant_channel")
    # The detector runs on Z, so it must use Z's own time base. Taking it from
    # `traces[0]` mis-dated the picks whenever another component came first.
    fs = float(z[0].stats.sampling_rate)
    start_epoch = float(z[0].stats.starttime)
    alignment = _alignment(traces)
    components = []
    for component in ("Z", "N", "E"):
        sel = traces.select(component=component)
        if len(sel):
            components.append(np.asarray(sel[0].data, dtype=float))
    # Every `result(...)` call now carries the alignment check and the units.
    ctx["alignment"] = alignment
    ctx["onset_reference"] = "s（相对该台站波形起点）"

    estimates = []
    for band in bands:
        for threshold in GRID["thresholds"]:
            onset, reason = _stalta_onset(z_data, fs, start_epoch, band, threshold)
            status = ("ok" if onset is not None else
                      "no_pick" if reason == "no_trigger" else "not_applicable")
            estimates.append({"detector": "stalta_z", "band_hz": list(band),
                              "threshold": threshold, "onset_s": onset,
                              "calculable": onset is not None, "status": status,
                              "reason": reason})
    if alignment["aligned"] is False:
        # A three-component picker cannot be compared across components that do not
        # share a time base. Refuse with the concrete reason instead of producing an
        # estimate outside the window.
        p_aic, s_aic = None, None
        aic_status, aic_reason = "not_applicable", "component_alignment_mismatch"
    else:
        p_aic, s_aic, aic_status, aic_reason = _ar_aic_pick(components, fs)
    estimates.append({"detector": "ar_aic_3c", "band_hz": [1.0, 20.0],
                      "threshold": None, "onset_s": p_aic,
                      "s_onset_s": s_aic, "calculable": p_aic is not None,
                      "status": aic_status, "reason": aic_reason})

    valid = [e["onset_s"] for e in estimates if e["onset_s"] is not None]
    # Kept apart on purpose (evaluation P1-2): a config that failed to execute is
    # not "the algorithms disagree", and a data set the method cannot be applied to
    # is not a failed run either.
    blocked = [e for e in estimates if e["status"] == "error"]
    not_applicable = [e for e in estimates if e["status"] == "not_applicable"]
    no_pick = [e for e in estimates if e["status"] == "no_pick"]
    counts = {"error": len(blocked), "not_applicable": len(not_applicable),
              "no_pick": len(no_pick)}
    if not valid:
        return result("no_estimate", estimates, "所有配置都没有给出可用到时",
                       "放宽到时窗或改用其它台站；不要据此判断有无事件",
                       reason="no_valid_estimate", counts=counts)

    values = np.asarray(valid, dtype=float)
    spread = float(values.max() - values.min())
    clusters = _cluster(valid)

    if len(valid) < MIN_CONFIGURATIONS_FOR_STABILITY:
        # Agreement among one or two configurations is not evidence of stability.
        return result(
            "insufficient_configurations", estimates,
            f"只有 {len(valid)} 个配置给出了到时，少于判断稳定性所需的 "
            f"{MIN_CONFIGURATIONS_FOR_STABILITY} 个，因此无法判断它稳不稳。",
            "不要把这个到时当作已确认：可以换采样率更高的台站，"
            "或直接报告为无法确定。",
            spread=spread, clusters=clusters, n_valid=len(valid),
            onsets_min_s=float(values.min()), onsets_max_s=float(values.max()),
            onsets_median_s=float(np.median(values)),
            mad_s=float(np.median(np.abs(values - np.median(values)))),
            reason="too_few_configurations", counts={**counts,
                                                    "error": len(blocked)})

    kind, statement, step = classify_spread(valid, clusters, spread)

    return result(kind, estimates, statement, step, spread=spread,
                   clusters=clusters, n_valid=len(valid),
                   onsets_min_s=float(values.min()), onsets_max_s=float(values.max()),
                   onsets_median_s=float(np.median(values)),
                   mad_s=float(np.median(np.abs(values - np.median(values)))),
                   counts=counts)


def _result(kind, estimates, statement, next_step, *, spread=None, clusters=None,
            n_valid=0, onsets_min_s=None, onsets_max_s=None, onsets_median_s=None,
            mad_s=None, reason=None, counts=None, blocked=0, bands_evaluated=None,
            full_grid=True, target_band_hz=None, alignment=None,
            onset_reference=None):
    counts = dict(counts or {})
    # `blocked` is the historical name for "gave no onset for a reason other than
    # no_trigger". It is kept as an alias so old readers do not silently break, but
    # the split below is what should be read.
    counts.setdefault("error", counts.get("error", blocked))
    counts.setdefault("not_applicable", 0)
    counts.setdefault("no_pick", 0)
    return {
        "grid": GRID,
        "grid_size": grid_size(),
        # Which bands this check actually evaluated, and whether that was the whole
        # declared grid. A restricted grid is a different, weaker check; a reader
        # must be able to see that without reverse-engineering the estimates.
        "bands_evaluated": ([list(b) for b in bands_evaluated]
                            if bands_evaluated else [list(b) for b in GRID["bands_hz"]]),
        "full_grid": bool(full_grid),
        "target_band_hz": (list(target_band_hz) if target_band_hz else None),
        "n_configurations": len(estimates) or grid_size(),
        "n_valid": n_valid,
        "estimates": estimates,
        # Per-algorithm bands and outcome counts. `bands_evaluated` alone only
        # described the STA/LTA subset while AR-AIC kept its own 1–20 Hz band.
        "detectors": _detector_disclosure(estimates),
        # Do the components share one time base? A three-component comparison is
        # only meaningful when they do, and a mismatch is refused with its reason.
        "alignment": alignment or {"checked_components": [], "aligned": None,
                                   "reason": "not_checked", "common_coverage": None},
        # Onsets are seconds relative to the waveform start of this station.
        "onset_reference": onset_reference or "s",
        "spread_s": spread,
        "clusters": clusters or [],
        "onsets_min_s": onsets_min_s,
        "onsets_max_s": onsets_max_s,
        "onsets_median_s": onsets_median_s,
        "mad_s": mad_s,
        "stability": kind,
        "applicable": kind not in ("not_applicable",),
        "statement": statement,
        "next_step": next_step,
        "reason": reason,
        # execution error / data not applicable / ran but found no phase: three
        # different situations that must not be merged into one "blocked" number.
        "n_configurations_error": counts.get("error", 0),
        "n_configurations_not_applicable": counts.get("not_applicable", 0),
        "n_configurations_no_pick": counts.get("no_pick", 0),
        # Configurations that produced no onset for a reason other than a plain
        # no-trigger: execution errors plus data the method cannot be applied to.
        "n_configurations_not_calculable": (counts.get("error", 0)
                                            + counts.get("not_applicable", 0)),
        "reference_used": False,
        "network_access": False,
        "scope": ("这是在一个预先固定的有限配置网格上的到时一致性检查。"
                  "它回答的是'这个自动到时稳不稳'，"
                  "不回答'有没有事件'、'事件在哪'或'是谁触发的'。"
                  "跨度大表示自动到时不可信，不表示信号不存在。"),
    }