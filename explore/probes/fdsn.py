"""Minimal FDSN/EarthScope client: station discovery, request construction, real fetch.

No credentials required (open services). All network access is to the fixed
EarthScope host. Nothing here reads reference picks or source classes.

严格离线模式（D1）：设 EQ_STRICT_OFFLINE=1 后本模块是唯一网络咽喉——
发现/元数据/波形请求一律只允许命中本地缓存，未命中立即抛
OfflineMaterialMissing（携带 URL 与类别），绝不回退联网。外联计数分四类
（发现/波形HTTP/模型provider/其他）由 offline_request_counts() 汇报，
供“真实外部请求 0”的口径核对。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import fnmatch
import urllib.parse
import urllib.request
from pathlib import Path

STATION_URL = "https://service.earthscope.org/fdsnws/station/1/query"
DATASELECT_URL = "https://service.earthscope.org/fdsnws/dataselect/1/query"
HOST = "service.earthscope.org"


class OfflineMaterialMissing(RuntimeError):
    """严格离线模式下材料缺失：立即说明，不联网补齐。"""

    def __init__(self, url: str, category: str):
        self.url = url
        self.category = category
        super().__init__(
            f"离线模式：材料缺失（{category}）——不联网补齐。URL：{url}")


def strict_offline() -> bool:
    return os.environ.get("EQ_STRICT_OFFLINE", "") not in ("", "0")


def request_category(url: str) -> str:
    if "/station/" in url:
        return "discovery"
    if "dataselect" in url:
        return "waveform_http"
    return "other_external"


_OFFLINE_STATE = {"refused": [], "cache_hits": 0}


def offline_request_counts() -> dict:
    """四类外联计数（D1 的“真实外部请求 0”口径）。

    严格离线模式下每一类都为 0：本模块不发请求；refused 记录被拒绝的
    缺失材料（含 URL 与类别）。模型 provider 的计数由驱动层负责（离线
    时不得调用，只能回放轨迹）。
    """
    mode = "strict_offline" if strict_offline() else "online"
    if strict_offline():
        return {
            "mode": mode,
            "discovery_requests": 0,
            "waveform_http": 0,
            "model_provider": 0,
            "other_external": 0,
            "cache_hits": _OFFLINE_STATE["cache_hits"],
            "refused": list(_OFFLINE_STATE["refused"]),
        }
    return {"mode": mode,
            "cache_hits": _OFFLINE_STATE["cache_hits"],
            "refused": list(_OFFLINE_STATE["refused"])}

# Level=channel text response column order (FDSN station service).
CHANNEL_COLUMNS = [
    "network", "station", "location", "channel", "latitude", "longitude",
    "elevation", "depth", "azimuth", "dip", "sensor", "scale", "scale_freq",
    "scale_units", "sample_rate", "start_time", "end_time",
]

_CACHE = None


def set_cache(path) -> None:
    global _CACHE
    _CACHE = Path(path)
    _CACHE.mkdir(parents=True, exist_ok=True)


def _get(url: str, timeout: int = 60) -> bytes:
    if urllib.parse.urlsplit(url).netloc != HOST:
        raise ValueError(f"Refusing non-EarthScope host: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "zhenji-explore/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def cached_get(url: str, timeout: int = 60):
    """Fetch with a content-addressed cache. Returns (bytes, from_cache, sha256).

    严格离线：缓存未命中立即抛 OfflineMaterialMissing（不打网络）。
    """
    if _CACHE is None:
        if strict_offline():
            raise OfflineMaterialMissing(url, request_category(url))
        payload = _get(url, timeout)
        return payload, False, hashlib.sha256(payload).hexdigest()
    key = hashlib.sha256(url.encode()).hexdigest()[:32]
    meta = _CACHE / (key + ".json")
    if meta.exists():
        record = json.loads(meta.read_text())
        body = _CACHE / record["file"]
        if body.exists() and hashlib.sha256(body.read_bytes()).hexdigest() == record["sha256"]:
            _OFFLINE_STATE["cache_hits"] += 1
            return body.read_bytes(), True, record["sha256"]
        # 元数据在而正文缺失/损坏：离线下是明确缺料，联网下按未命中处理。
        if strict_offline():
            _OFFLINE_STATE["refused"].append(
                {"url": url, "category": request_category(url),
                 "reason": "cache_body_missing_or_corrupt"})
            raise OfflineMaterialMissing(url, request_category(url))
    if strict_offline():
        _OFFLINE_STATE["refused"].append(
            {"url": url, "category": request_category(url),
             "reason": "cache_meta_missing"})
        raise OfflineMaterialMissing(url, request_category(url))
    payload = _get(url, timeout)
    digest = hashlib.sha256(payload).hexdigest()
    name = key + ".bin"
    (_CACHE / name).write_bytes(payload)
    meta.write_text(json.dumps({"url": url, "file": name, "sha256": digest,
                                "bytes": len(payload)}, indent=2))
    return payload, False, digest


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_channel_text(text: str) -> list[dict]:
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != len(CHANNEL_COLUMNS):
            continue
        row = dict(zip(CHANNEL_COLUMNS, parts))
        for key in ("latitude", "longitude", "elevation", "depth", "azimuth",
                    "dip", "scale", "sample_rate"):
            row[key] = _num(row[key])
        rows.append(row)
    return rows


def station_query(spec: dict, timeout: int = 60) -> tuple[list[dict], str]:
    """Build and run a station/channel query from a user-level spec."""
    params = {
        "starttime": spec["start"],
        "endtime": spec["end"],
        "level": "channel",
        "format": "text",
        "nodata": "404",
    }
    stations = [str(s).strip().upper() for s in (spec.get("stations") or [])]
    qualified = [s.split(".", 1) for s in stations if "." in s]
    plain = [s for s in stations if "." not in s]
    networks = spec.get("networks") or []
    # FDSN takes network and station as separate parameters. The user-facing
    # UW.CHIMA notation must never be sent literally as sta=UW.CHIMA.
    if networks:
        params["net"] = ",".join(networks)
    elif qualified and not plain:
        params["net"] = ",".join(dict.fromkeys(n for n, _ in qualified))
    if stations:
        params["sta"] = ",".join(dict.fromkeys(s.rsplit(".", 1)[-1] for s in stations))
    for key in ("minlatitude", "maxlatitude", "minlongitude", "maxlongitude"):
        if spec.get(key) is not None:
            params[key] = str(spec[key])
    if spec.get("minmagnitude") is not None:  # not a station param; ignored upstream
        params.pop("minmagnitude")
    url = STATION_URL + "?" + urllib.parse.urlencode(params)
    payload, was_cached, digest = cached_get(url, timeout)
    rows = parse_channel_text(payload.decode("utf-8", "replace"))
    if qualified:
        # Multi-network FDSN queries form a cross product. Preserve the exact
        # pairs the user named rather than admitting another network's station.
        rows = [r for r in rows if
                any(fnmatch.fnmatchcase(r["station"], s) for s in plain)
                or any(fnmatch.fnmatchcase(r["network"], n)
                       and fnmatch.fnmatchcase(r["station"], s)
                       for n, s in qualified)]
    return rows, url


def haversine_km(lat1, lon1, lat2, lon2):
    if None in (lat1, lon1, lat2, lon2):
        return None
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


CANONICAL = {"Z": (0.0, -90.0), "N": (0.0, 0.0), "E": (90.0, 0.0)}


def orientation_ok(channel: str, azimuth, dip) -> bool:
    if not channel or channel[-1] not in CANONICAL:
        return False
    if azimuth is None or dip is None:
        return False
    want_az, want_dip = CANONICAL[channel[-1]]
    return abs(azimuth - want_az) < 1e-6 and abs(dip - want_dip) < 1e-6


def coverage_ok(row: dict, start: str, end: str) -> bool:
    """Epoch coverage using FDSN semantics: an empty bound means open-ended.

    A blank EndTime does NOT mean "not active" — it means the epoch is still open.
    Whether data actually exists for that epoch is decided later by the real fetch.
    """
    opened = row["start_time"] or ""
    closed = row["end_time"] or ""
    starts_before = (not opened) or opened <= start
    ends_after = (not closed) or closed >= end
    return starts_before and ends_after


def group_stations(rows: list[dict]) -> dict:
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (row["network"], row["station"])
        group = groups.setdefault(key, {
            "network": row["network"], "station": row["station"],
            "latitude": row["latitude"], "longitude": row["longitude"],
            "elevation": row["elevation"], "channels": [],
        })
        group["channels"].append(row)
    return groups


def evaluate_group(group: dict, spec: dict) -> dict:
    """Check whether a station can serve the stated goal. Pure function, no network."""
    goal = spec["goal"]
    band = spec.get("band_hz") or [1.0, 12.0]
    needed_rate = 2.0 * band[1]
    checks = []
    seen = set()
    for row in group["channels"]:
        channel = row["channel"]
        component = channel[-1] if channel else "?"
        if component not in "ENZ" or channel in seen:
            continue
        seen.add(channel)
        checks.append({
            "channel": channel,
            "component": component,
            "orientation": orientation_ok(channel, row["azimuth"], row["dip"]),
            "covers_window": coverage_ok(row, spec["start"], spec["end"]),
            "sample_rate_hz": row["sample_rate"],
            "rate_sufficient": bool(row["sample_rate"] and row["sample_rate"] > needed_rate),
            "response_present": bool(row["scale"] and row["scale_units"]),
        })

    def usable(component):
        picks = [c for c in checks if c["component"] == component]
        return [c for c in picks if c["orientation"] and c["covers_window"]
                and c["rate_sufficient"] and c["response_present"]]

    z = usable("Z")
    e, n = usable("E"), usable("N")
    three = bool(z and e and n)
    vertical = bool(z)
    eligible = three if goal == "three_component_pick" else vertical
    reasons = []
    if not checks:
        reasons.append("该站在给定时段内没有 ENZ 命名通道")
    if not z:
        reasons.append("没有可用的垂直分量（方向/覆盖/采样率/响应任一不满足）")
    if goal == "three_component_pick" and z and not three:
        reasons.append("缺少可用的水平分量，无法做三分量拾取")
    return {
        "eligible": eligible,
        "three_component": three,
        "vertical_only": vertical and not three,
        "usable_channels": sorted({c["channel"] for c in (z + e + n)}),
        "max_sample_rate": max([c["sample_rate_hz"] or 0 for c in checks] or [0]),
        "reasons": reasons or (["满足目标条件"] if eligible else ["不满足目标条件"]),
        "channel_checks": checks,
    }


def choose_family(evaluation: dict, goal: str) -> str | None:
    """Pick the channel family that actually satisfies the goal.

    A station may expose several epochs/families (e.g. EHZ alongside ENE/ENN/ENZ).
    Requesting the wrong family yields an incomplete download, so the family is
    derived from the channels that passed the per-channel checks.
    """
    usable = evaluation.get("usable_channels") or []
    if not usable:
        return None
    by_family: dict[str, set] = {}
    for channel in usable:
        by_family.setdefault(channel[:2], set()).add(channel[-1])
    needed = {"E", "N", "Z"} if goal == "three_component_pick" else {"Z"}
    for family in sorted(by_family):
        if needed.issubset(by_family[family]):
            return family
    if goal == "three_component_pick":
        for family in sorted(by_family):
            if "Z" in by_family[family]:
                return family  # best effort; validation will record the shortfall
    return sorted(by_family)[0]


def build_requests(group: dict, spec: dict, family: str) -> dict:
    params = {
        "net": group["network"], "sta": group["station"], "loc": "--",
        "cha": family + "?", "start": spec["start"], "end": spec["end"],
        "format": "miniseed", "nodata": "404",
    }
    wave = DATASELECT_URL + "?" + urllib.parse.urlencode(params)
    sparams = dict(params)
    for wrong in ("format", "nodata", "start", "end"):
        sparams.pop(wrong, None)
    sparams.update({"starttime": spec["start"], "endtime": spec["end"],
                    "level": "response", "format": "xml", "nodata": "404"})
    meta = STATION_URL + "?" + urllib.parse.urlencode(sparams)
    return {"waveform_url": wave, "metadata_url": meta,
            "job": {"case_id": spec.get("case_id", "explore"),
                    "id": f"{group['network']}-{group['station']}-{family}",
                    "net": group["network"], "sta": group["station"],
                    "cha": family, "start": spec["start"], "end": spec["end"]}}
