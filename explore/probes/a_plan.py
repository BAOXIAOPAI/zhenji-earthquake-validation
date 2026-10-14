"""Direction A probe: from an incomplete task to an executable observation plan.

The user supplies only: a goal, a UTC window, and a region of interest.
The system must discover stations, decide which are usable for the goal, build
FDSN requests, fetch real data, and validate it.

Two framings are measured:
  (strict)   form + reasonable catalogue rule  vs  auto-plan, same user fields
  (end-to-end) manual net/sta/cha entry        vs  auto-plan

Reference material (which curated stations recorded the event) is used ONLY by
the evaluator, never by a planner.
"""
from __future__ import annotations

import collections
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import pandas as pd  # noqa: E402
import fdsn  # noqa: E402
import quality  # noqa: E402

OUT = ROOT / "explore/out"
CACHE = ROOT / "explore/out/cache"
BOX_DEG = 0.6


# ---------------------------------------------------------------- task loading
def load_tasks(limit=8, min_year=2013, window_s=120, seed=20261009):
    """Build realistic tasks from *training* cases only. The sealed 311 stay shut.

    Restricted to relatively recent years because pre-2013 PNW waveform coverage
    is sparse, which would make every strategy fail for reasons unrelated to
    selection quality.
    """
    joined = pd.read_csv(ROOT / "evaluation/splits.csv").fillna("")
    joined = joined[joined.split == "train"]
    joined = joined[joined.source_type == "earthquake"]
    joined = joined[joined.year >= min_year]
    joined = joined.dropna(subset=["trace_P_arrival_sample", "station_latitude_deg",
                                   "station_longitude_deg", "trace_start_time"])
    joined = joined.sort_values(["event_id", "case_id"]).drop_duplicates("station_code")
    tasks = []
    for _, r in joined.head(limit).iterrows():
        start = pd.Timestamp(r.trace_start_time)
        tasks.append({
            "task_id": f"task_{len(tasks)+1:02d}",
            "case_id": r.case_id,
            "event_id": r.event_id,
            "goal": "three_component_pick",
            "band_hz": [1.0, 12.0],
            "start": start.strftime("%Y-%m-%dT%H:%M:%S.000000"),
            "end": (start + pd.Timedelta(seconds=window_s)).strftime("%Y-%m-%dT%H:%M:%S.000000"),
            "region": {
                "center_lat": round(float(r.station_latitude_deg), 4),
                "center_lon": round(float(r.station_longitude_deg), 4),
                "minlatitude": round(float(r.station_latitude_deg) - BOX_DEG, 4),
                "maxlatitude": round(float(r.station_latitude_deg) + BOX_DEG, 4),
                "minlongitude": round(float(r.station_longitude_deg) - BOX_DEG, 4),
                "maxlongitude": round(float(r.station_longitude_deg) + BOX_DEG, 4),
            },
            # evaluator-only reference: which stations the curated dataset used
            "_reference_stations": sorted(set(
                joined[joined.event_id == r.event_id].station_code)),
            "_origin_station": r.station_code,
        })
    return tasks


def query_spec(task):
    spec = {"start": task["start"], "end": task["end"], "goal": task["goal"],
            "band_hz": task["band_hz"]}
    spec.update(task["region"])
    return spec


# ------------------------------------------------------------------- planners
def plan_rule(spec, groups, kind):
    """Deterministic catalogue rules = the fair strong baseline."""
    center = (spec["center_lat"], spec["center_lon"])
    scored = []
    for group in groups.values():
        evaluation = fdsn.evaluate_group(group, spec)
        distance = fdsn.haversine_km(center[0], center[1],
                                     group["latitude"], group["longitude"])
        scored.append((group, evaluation, distance))
    if kind == "nearest_any":
        # naive form rule: nearest station with any ENZ-named channel
        pool = [s for s in scored if any(c["component"] in "ENZ"
                                         for c in s[1]["channel_checks"])]
        key = lambda s: (s[2] if s[2] is not None else 9e9, s[0]["network"], s[0]["station"])
    elif kind == "nearest_vertical":
        pool = [s for s in scored if s[1]["vertical_only"] or s[1]["three_component"]]
        key = lambda s: (s[2] if s[2] is not None else 9e9, s[0]["network"], s[0]["station"])
    elif kind == "nearest_three_component":
        pool = [s for s in scored if s[1]["three_component"]]
        key = lambda s: (s[2] if s[2] is not None else 9e9, s[0]["network"], s[0]["station"])
    elif kind == "alphabetical_three_component":
        pool = [s for s in scored if s[1]["three_component"]]
        key = lambda s: (s[0]["network"], s[0]["station"])
    else:
        raise ValueError(kind)
    pool.sort(key=key)
    return pool


def plan_auto(spec, groups):
    """Goal-aware planner with explicit, inspectable scoring and a fallback ladder."""
    center = (spec["center_lat"], spec["center_lon"])
    ranked = []
    for group in groups.values():
        evaluation = fdsn.evaluate_group(group, spec)
        distance = fdsn.haversine_km(center[0], center[1],
                                     group["latitude"], group["longitude"])
        # Primary key: does it meet the goal? Then rate headroom, then distance.
        rate = evaluation["max_sample_rate"] or 0
        tier = 0 if evaluation["eligible"] else (1 if evaluation["vertical_only"] else 2)
        ranked.append({
            "group": group, "evaluation": evaluation, "distance_km": distance,
            "tier": tier, "rate_hz": rate,
            "score": (tier, -rate, distance if distance is not None else 9e9),
        })
    ranked.sort(key=lambda r: r["score"])
    return ranked


# ------------------------------------------------------------------ execution
def run_candidate(candidate, spec, strategy, task_id):
    group = candidate["group"]
    evaluation = candidate["evaluation"]
    family = fdsn.choose_family(evaluation, spec["goal"])
    record = {
        "task_id": task_id, "strategy": strategy,
        "network": group["network"], "station": group["station"],
        "family": family,
        "distance_km": None if candidate.get("distance_km") is None
        else round(candidate["distance_km"], 2),
        "tier": candidate.get("tier"),
        "designed_eligible": evaluation["eligible"],
    }
    if family is None:
        record.update(status="no_usable_family", task_satisfied=False)
        return record
    requests = fdsn.build_requests(group, spec, family)
    record["waveform_url"] = requests["waveform_url"]
    record["metadata_url"] = requests["metadata_url"]
    try:
        wave, cached_w, sha_w = fdsn.cached_get(requests["waveform_url"], timeout=90)
        meta, cached_m, sha_m = fdsn.cached_get(requests["metadata_url"], timeout=90)
    except Exception as exc:  # noqa: BLE001 - network failures are results
        record.update(status="fetch_error", error=f"{type(exc).__name__}: {exc}"[:200])
        return record
    folder = OUT / "acquired" / task_id / f"{strategy}_{group['network']}_{group['station']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "waveform.mseed").write_bytes(wave)
    (folder / "metadata.xml").write_bytes(meta)
    record.update(status="fetched", waveform_sha256=sha_w, metadata_sha256=sha_m,
                  waveform_bytes=len(wave), cached=(cached_w and cached_m))
    try:
        from obspy import read, read_inventory
        st = read(str(folder / "waveform.mseed"), format="MSEED")
        inv = read_inventory(str(folder / "metadata.xml"), format="STATIONXML")
        report = quality.inspect(st, inv, requests["job"])
        record["quality"] = {
            "channels": sorted(t.id for t in st),
            "three_component_pass": report["three_component_checks_pass"],
            "basic_pass": report["basic_observation_checks_pass"],
            "gaps": len(report["gaps_or_overlaps"]),
            "state": report["state"],
        }
        record["task_satisfied"] = bool(report["three_component_checks_pass"])
    except Exception as exc:  # noqa: BLE001
        record.update(task_satisfied=False, status="validate_error",
                      error=f"{type(exc).__name__}: {exc}"[:200])
    return record


def execute_plan(task, spec, candidate_plan, strategy, max_attempts=3):
    """Try the ranked plan in order until the goal is met or the budget runs out."""
    attempts = []
    for candidate in candidate_plan[:max_attempts]:
        result = run_candidate(candidate, spec, strategy, task["task_id"])
        attempts.append(result)
        if result.get("task_satisfied"):
            break
    return {
        "task_id": task["task_id"], "strategy": strategy,
        "attempts_used": len(attempts),
        "first_attempt_success": bool(attempts and attempts[0].get("task_satisfied")),
        "eventual_success": any(a.get("task_satisfied") for a in attempts),
        "attempts": attempts,
    }


def main():
    fdsn.set_cache(CACHE)
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks(limit=int(sys.argv[1]) if len(sys.argv) > 1 else 8)
    results = []
    for task in tasks:
        spec = query_spec(task)
        rows, station_url = fdsn.station_query(spec)
        groups = fdsn.group_stations(rows)
        auto = plan_auto(spec, groups)
        entry = {
            "task_id": task["task_id"], "case_id": task["case_id"],
            "window": [task["start"], task["end"]],
            "region_center": [task["region"]["center_lat"], task["region"]["center_lon"]],
            "station_rows": len(rows), "stations": len(groups),
            "station_query_url": station_url,
            "plans": {},
        }
        for name, planner in [
            ("auto_plan", auto),
            ("rule_nearest_any", plan_rule(spec, groups, "nearest_any")),
            ("rule_nearest_vertical", plan_rule(spec, groups, "nearest_vertical")),
            ("rule_nearest_three_component", plan_rule(spec, groups, "nearest_three_component")),
            ("rule_alphabetical_three_component",
             plan_rule(spec, groups, "alphabetical_three_component")),
        ]:
            normalised = []
            for item in planner:
                if "evaluation" in item and "group" in item:
                    normalised.append(item)
                else:
                    group, evaluation, distance = item
                    normalised.append({"group": group, "evaluation": evaluation,
                                       "distance_km": distance})
            try:
                entry["plans"][name] = execute_plan(task, spec, normalised, name)
            except Exception as exc:  # noqa: BLE001
                entry["plans"][name] = {"strategy": name, "error": str(exc)[:200],
                                        "trace": traceback.format_exc()[-400:]}
        # evaluator-only reference comparison (never fed to a planner)
        entry["_reference"] = {
            "curated_stations": task["_reference_stations"],
            "origin_station": task["_origin_station"],
            "chosen_by_auto": [c["group"]["station"] for c in auto[:3]],
        }
        results.append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k != "plans"},
                         ensure_ascii=False), flush=True)
        for name, value in entry["plans"].items():
            print(f"   {name:36s} first={value.get('first_attempt_success')} "
                  f"eventual={value.get('eventual_success')} "
                  f"attempts={value.get('attempts_used')}", flush=True)
    (OUT / "a_direction_results.json").write_text(
        json.dumps({"tasks": results}, ensure_ascii=False, indent=2))
    summarise(results)


def _failure_reason(attempt):
    if attempt.get("task_satisfied"):
        return None
    if attempt.get("status") == "fetch_error":
        return "fetch_error(404/网络)"
    if attempt.get("status") == "validate_error":
        return "validate_error"
    if attempt.get("status") == "no_usable_family":
        return "no_usable_family"
    quality_block = attempt.get("quality") or {}
    if quality_block and not quality_block.get("basic_pass"):
        return "data_not_usable(覆盖/方向/采样率/响应)"
    return attempt.get("status") or "unknown"


def summarise(results):
    strategies = sorted({s for r in results for s in r.get("plans", {})})
    print("\n=== A 方向汇总 ===")
    for strategy in strategies:
        runs = [r["plans"][strategy] for r in results if strategy in r["plans"]]
        ok = sum(1 for r in runs if r.get("first_attempt_success"))
        ev = sum(1 for r in runs if r.get("eventual_success"))
        att = sum(r.get("attempts_used", 0) for r in runs)
        fails = collections.Counter()
        for r in runs:
            first = (r.get("attempts") or [{}])[0]
            reason = _failure_reason(first)
            if reason:
                fails[reason] += 1
        print(f"{strategy:36s} first-attempt {ok}/{len(runs)}  eventual {ev}/{len(runs)}"
              f"  attempts {att}  first-fail={dict(fails)}")


if __name__ == "__main__":
    main()