"""Turn a user-written request into a structured, checkable task.

The guide (G1 4.1) requires a real entry point: the user fills in or pastes a UTC
window, a region or station list, the suspicion and the analysis goal. The parsed
task is shown back; **missing key content raises a clarification instead of
silently falling back to a preset event**. Compound questions become separate
sub-tasks, and out-of-scope requests are limited explicitly.

Parsing here is deterministic — regexes and keyword tables. It decides what the
user is asking, never what the answer is.
"""
from __future__ import annotations

import re

METHOD_BAND_HZ = [1.0, 8.0]
DEFAULT_TARGET_BAND_HZ = [1.0, 12.0]

# 命名区域。用户可以不写坐标而选一个已核验过的区域；这些框直接取自
# development 案例，来源可查，不是新造的地理假设。锁定案例的区域不在此列出。
NAMED_REGIONS = {
    "orcas": {"label": "Orcas / 圣胡安群岛（华盛顿州）",
              "center_lat": 48.611, "center_lon": -122.805,
              "minlatitude": 48.011, "maxlatitude": 49.211,
              "minlongitude": -123.405, "maxlongitude": -122.205,
              "source": "development 案例 E1_orcas_event / Q1_orcas_quiet 的复核区域"},
    "qgbp": {"label": "QGBP 周边（最近台站取不到元数据）",
             "center_lat": 48.73338, "center_lon": -122.66756,
             "minlatitude": 48.1334, "maxlatitude": 49.3334,
             "minlongitude": -123.2676, "maxlongitude": -122.0676,
             "source": "development 案例 F1_qgbp_metadata_gap 的失败样例区域"},
    "wauna": {"label": "Wauna / 普吉特湾西侧（华盛顿州）",
              "center_lat": 47.4, "center_lon": -122.722,
              "minlatitude": 46.8, "maxlatitude": 48.0,
              "minlongitude": -123.322, "maxlongitude": -122.122,
              "source": "development 案例 E2_wauna_event / Q2_wauna_quiet 的复核区域"},
    "sparse2003": {"label": "俄勒冈北部（台站稀疏，2003 年）",
                   "center_lat": 45.322, "center_lon": -121.651,
                   "minlatitude": 44.722, "maxlatitude": 45.922,
                   "minlongitude": -122.251, "maxlongitude": -121.051,
                   "source": "development 案例 S1_sparse_2003 的复核区域"},
}

SUPPORTED_GOALS = {
    "three_component_pick": "用三分量数据做 P/S 拾取",
    "vertical_onset": "只用垂直分量做到时检查",
}

# Requests this workbench cannot answer. Naming them is the honest move:
# silently answering a neighbouring question is worse than declining.
UNSUPPORTED_TOPICS = (
    (("震级", "magnitude", "里氏", "richter", "ml", "mw"),
     "本工作台不测定震级"),
    (("震中", "定位", "epicenter", "hypocenter", "event location", "locate"),
     "本工作台不定位事件"),
    (("深度", "depth", "focal depth"),
     "本工作台不测定震源深度"),
    (("震源机制", "矩张量", "moment tensor", "focal mechanism"),
     "本工作台不反演震源机制"),
    (("预警", "early warning", "early-warning"),
     "本工作台不做地震预警"),
    (("预测", "预报", "prediction", "forecast"),
     "本工作台不做地震预测"),
    (("伤亡", "损失", "casualt", "damage estimate"),
     "本工作台不做灾害评估"),
)

# The three criteria this workbench can actually test, and the words that
# indicate each. A question matching several is compound (see `build`).
CONCERNS = (
    ("Q_instrument",
     ("仪器", "元数据", "设备", "故障", "坏了", "通道异常", "传感器", "标定", "常值",
      "instrument", "metadata", "artifact", "sensor", "calibrat", "flat", "stuck"),
     "这条观测是不是仪器或元数据问题造成的"),
    ("Q_usable",
     ("能不能用", "可用", "能用", "可用于", "拾取", "频带", "采样率", "数据质量", "质量",
      "usable", "pick", "band", "sampling", "quality"),
     "这段观测能不能用于指定频带的拾取"),
    ("Q_propagating",
     ("多个台站", "多台", "传播", "共有", "一致", "跨台", "区域",
      "propagat", "coherent", "multi-station", "multistation", "across stations"),
     "这是不是多个台站共有的真实传播信号"),
)

WINDOW_WARN_S = 6 * 3600.0
REGION_WARN_DEG = 3.0
DEFAULT_WINDOW_S = 120.0

_ISO = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)\s*(Z|[+-]\d{2}:?\d{2})?")
_RANGE_HINT = re.compile(r"(到|至|—|–|~|～|\bto\b|\buntil\b|—)")
_BAND_RANGE = re.compile(
    r"(?<![\d.])([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(?:Hz\s*)?"
    r"[-–—~～到至]\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*Hz(?![a-z])",
    re.IGNORECASE)


def parse_time(value):
    """Accept ISO-8601 (with or without zone) or Unix seconds. Returns ISO-Z."""
    if value is None:
        return None, None
    if isinstance(value, (int, float)) or (
            isinstance(value, str) and re.fullmatch(r"\d{9,11}(\.\d+)?", value.strip())):
        from datetime import datetime, timezone
        seconds = float(value)
        moment = datetime.fromtimestamp(seconds, tz=timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S"), seconds
    text = str(value).strip()
    match = _ISO.search(text)
    if not match:
        return None, None
    from datetime import datetime, timezone
    date, clock, zone = match.groups()
    if len(clock) == 5:
        clock += ":00"
    if zone in (None, "Z"):
        offset = "+00:00"
    else:
        offset = zone if ":" in zone else zone[:3] + ":" + zone[3:]
    try:
        moment = datetime.fromisoformat(f"{date}T{clock}{offset}")
    except ValueError:
        return None, None
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S"), moment.timestamp()


def extract_times(text):
    """Pull up to two timestamps out of pasted text, in the order they appear."""
    found = []
    for match in _ISO.finditer(text or ""):
        iso, unix = parse_time(match.group(0))
        if iso:
            found.append({"iso": iso, "unix": unix, "raw": match.group(0)})
    return found[:2]


def detect_concerns(text):
    """Every criterion the question touches, in table order."""
    lowered = (text or "").lower()
    hits = []
    for question_id, words, label in CONCERNS:
        if any(word in lowered for word in words):
            hits.append({"question_id": question_id, "label": label})
    return hits


def detect_unsupported(text):
    lowered = (text or "").lower()
    return [{"topic": words[0], "statement": statement}
            for words, statement in UNSUPPORTED_TOPICS
            if any(word in lowered for word in words)]


def _region_block(raw):
    """Normalise the region into one of: preset / box / centre+radius / stations."""
    if raw.get("demo_case_ref"):
        return {"kind": "preset", "demo_case_ref": raw["demo_case_ref"]}, []
    named = raw.get("region_name") or (raw.get("region")
                                       if isinstance(raw.get("region"), str) else None)
    if named:
        key = str(named).strip().lower()
        entry = NAMED_REGIONS.get(key)
        if entry is None:
            return None, [{"field": "region",
                           "why": f"没有名为 {named!r} 的已核验区域",
                           "example": "可选：" + "、".join(sorted(NAMED_REGIONS))}]
        return {"kind": "preset_region", "name": key,
                **{k: v for k, v in entry.items() if k != "label"},
                "label": entry["label"]}, []
    if raw.get("stations"):
        stations = [str(s).strip() for s in raw["stations"] if str(s).strip()]
        if not stations:
            return None, [{"field": "stations", "why": "台站清单是空的",
                           "example": "UW.OLGA 或 OLGA"}]
        return {"kind": "stations", "stations": stations,
                "networks": raw.get("networks") or []}, []
    box_keys = ("minlatitude", "maxlatitude", "minlongitude", "maxlongitude")
    if all(raw.get(k) is not None for k in box_keys):
        return {"kind": "box", **{k: float(raw[k]) for k in box_keys},
                "center_lat": (float(raw["minlatitude"]) + float(raw["maxlatitude"])) / 2,
                "center_lon": (float(raw["minlongitude"]) + float(raw["maxlongitude"])) / 2}, []
    if raw.get("center_lat") is not None and raw.get("center_lon") is not None:
        from math import cos, radians
        lat, lon = float(raw["center_lat"]), float(raw["center_lon"])
        deg = float(raw.get("radius_deg") or 1.0)
        return {"kind": "center", "center_lat": lat, "center_lon": lon,
                "minlatitude": round(lat - deg, 4), "maxlatitude": round(lat + deg, 4),
                "minlongitude": round(lon - deg / max(cos(radians(lat)), 1e-6), 4),
                "maxlongitude": round(lon + deg / max(cos(radians(lat)), 1e-6), 4)}, []
    return None, [{"field": "region",
                   "why": "没有给出区域或台站，无法确定要查哪些台站",
                   "example": "中心点 48.611,-122.805 半径 1°；或台站清单 UW.OLGA,UW.MCW"}]


def build(raw):
    """Parse one user request into a structured task plus any clarification needed.

    `raw` keys: window_start, window_end, window_seconds, question, target_band_hz,
    method_goal, station_kind fields, demo_case_ref.
    """
    raw = dict(raw or {})
    question = (raw.get("question") or "").strip()
    notes = []

    # --- window ---------------------------------------------------------
    start_iso = end_iso = None
    start_raw, end_raw = raw.get("window_start"), raw.get("window_end")
    pasted = extract_times(question)
    if start_raw is None and pasted:
        start_raw = pasted[0]["iso"]
        notes.append(f"从问题文本中识别到时窗起点 {pasted[0]['raw']}")
    if end_raw is None and len(pasted) > 1:
        end_raw = pasted[1]["iso"]
        notes.append(f"从问题文本中识别到时窗终点 {pasted[1]['raw']}")
    start_iso, start_unix = parse_time(start_raw)
    end_iso, end_unix = parse_time(end_raw)

    missing = []
    if start_iso is None:
        missing.append({"field": "window_start", "why": "没有可识别的 UTC 时窗起点",
                        "example": "2025-03-03T11:00:00Z"})
    if end_iso is None and start_iso is not None and raw.get("window_seconds"):
        from datetime import datetime, timedelta, timezone
        seconds = float(raw["window_seconds"])
        end_iso = (datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                   + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S")
        end_unix = start_unix + seconds
        notes.append(f"按你给出的时长 {seconds:g} 秒推算终点 {end_iso}Z")
    if end_iso is None:
        missing.append({"field": "window_end", "why": "没有可识别的 UTC 时窗终点",
                        "example": "2025-03-03T11:02:00Z"})

    if start_unix is not None and end_unix is not None:
        if end_unix <= start_unix:
            missing.append({"field": "window_end", "why": "终点不晚于起点",
                            "example": "终点需大于起点"})
        elif end_unix - start_unix > WINDOW_WARN_S:
            notes.append(f"时窗长 {((end_unix - start_unix) / 3600):.1f} 小时，"
                         "数据量与耗时都会明显增加")

    # --- region ---------------------------------------------------------
    region, region_missing = _region_block(raw)
    missing.extend(region_missing)
    if region and region.get("kind") == "box":
        span = region["maxlatitude"] - region["minlatitude"]
        if span > REGION_WARN_DEG:
            notes.append(f"区域纬度跨度 {span:.1f}°，台站数会很多，"
                         f"实际只取最近的若干台")

    # --- goal and band --------------------------------------------------
    goal = raw.get("method_goal")
    if goal is not None and goal not in SUPPORTED_GOALS:
        missing.append({"field": "method_goal", "why": f"不支持的分析目标 {goal!r}",
                        "example": " 或 ".join(SUPPORTED_GOALS)})
        goal = None
    band = raw.get("target_band_hz")
    text_bands = list(dict.fromkeys(
        (float(match[0]), float(match[1])) for match in _BAND_RANGE.findall(question)))
    text_band = list(text_bands[0]) if len(text_bands) == 1 else None
    if len(text_bands) > 1:
        missing.append({"field": "target_band_hz",
                        "why": "问题描述包含多个不同频带，本版一次只核查一个目标频带，请统一描述与频带输入",
                        "example": "问题描述与频带输入都使用 5–20 Hz"})
    if band is None:
        if text_band is not None:
            band = text_band
            notes.append(f"从问题文本中识别到目标频带 {band[0]:g}–{band[1]:g} Hz")
        elif not text_bands:
            band = list(DEFAULT_TARGET_BAND_HZ)
            notes.append("未指定目标频带，使用默认 1–12 Hz；可在频带输入中修改")
    if band is not None:
        try:
            band = [float(band[0]), float(band[1])]
        except (TypeError, ValueError, IndexError):
            missing.append({"field": "target_band_hz", "why": "目标频带需为 [低, 高] 两个数",
                            "example": "[1.0, 12.0]"})
            band = None
        else:
            if not 0 < band[0] < band[1]:
                missing.append({"field": "target_band_hz", "why": "目标频带下界需小于上界",
                                "example": "[1.0, 12.0]"})
                band = None
            elif text_band is not None and band != text_band:
                missing.append({"field": "target_band_hz",
                                "why": (f"问题描述的频带 {text_band[0]:g}–{text_band[1]:g} Hz"
                                        f" 与频带输入 {band[0]:g}–{band[1]:g} Hz 不一致，请统一"),
                                "example": "修改问题描述或频带输入，使两处一致"})

    # --- what is being asked -------------------------------------------
    concerns = detect_concerns(question)
    unsupported = detect_unsupported(question)
    if not question and not raw.get("demo_case_ref"):
        missing.append({"field": "question", "why": "没有给出疑点或分析目标",
                        "example": "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？"})
    if not concerns and question:
        # An unclear question is not silently mapped onto a default criterion.
        missing.append({"field": "question",
                        "why": "无法判断这个问题对应哪一项可检验判据",
                        "example": "请指明是问通道/仪器问题、数据可用性，还是多台传播"})

    subtasks = ([{"question_id": c["question_id"], "label": c["label"]} for c in concerns]
                if len(concerns) > 1 else [])
    if subtasks:
        # The parser used to promise "已拆成子任务分别核查", but the actual review/run
        # path still executes ONE classification (the third review's P1-5 probe:
        # ready=True with two parsed concerns, only Q_instrument executed). Until
        # per-subtask execution exists, the honest contract is to ask the user to
        # choose one question instead of promising a check that will not happen.
        missing.append({
            "field": "question",
            "why": (f"这段描述里有 {len(subtasks)} 个可检验问题"
                    f"（{'、'.join(c['label'] for c in concerns)}）；"
                    "本版一次只核查一个问题，请选择其中一项"),
            "example": concerns[0]["label"]})
        notes.append(f"检测到 {len(subtasks)} 个问题；本版一次只核查一个问题，"
                     "不会替你全部核查")

    structured = {
        "window_start": start_iso, "window_end": end_iso,
        "region": region, "user_question": question,
        "target_band_hz": band, "method_goal": goal,
        "question_ids": [c["question_id"] for c in concerns],
    }
    structured = {k: v for k, v in structured.items() if v is not None}

    return {
        "ready": not missing,
        "structured": structured,
        "missing": missing,
        "clarifications": [f"{m['field']}：{m['why']}（例如 {m['example']}）" for m in missing],
        "concerns": concerns,
        "subtasks": subtasks,
        "unsupported": unsupported,
        "notes": notes,
        "echo": _echo(structured, concerns, missing),
    }


def _echo(structured, concerns, missing):
    """The plain-language restatement shown back to the user before anything runs."""
    if missing:
        return "还缺必要信息，未开始核查。"
    where = structured.get("region") or {}
    if where.get("kind") == "preset":
        place = f"预置案例 {where['demo_case_ref']}"
    elif where.get("kind") == "stations":
        place = "台站 " + "、".join(where["stations"])
    elif where.get("kind") == "center":
        place = f"中心 {where['center_lat']},{where['center_lon']} 半径 {where.get('radius_deg', 1.0)}°"
    else:
        place = (f"区域 {where.get('minlatitude')}–{where.get('maxlatitude')}°N、"
                 f"{where.get('minlongitude')}–{where.get('maxlongitude')}°E")
    band = structured.get("target_band_hz")
    goal = structured.get("method_goal")
    parts = [f"时窗 {structured['window_start']}Z 到 {structured['window_end']}Z",
             place]
    if band:
        parts.append(f"目标频带 {band[0]:g}–{band[1]:g} Hz")
    if goal:
        parts.append(SUPPORTED_GOALS.get(goal, goal))
    parts.append("要核查：" + "；".join(c["label"] for c in concerns))
    return "；".join(parts) + "。"
