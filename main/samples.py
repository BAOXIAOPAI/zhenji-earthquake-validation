"""Verified event, quiet-control and unavailable-data examples.

The event metadata is supported by evidence/real-event-window.json. Channel
injection is disclosed separately and never alters the downloaded observation.
This module only describes samples; it does not fetch data or call a model."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence/real-event-window.json"

_INJECTION = (
    "演示用注入：本轮第一个取得台站的 E（东西）通道被替换为一个平直（恒定）副本，"
    "放在单独的 derived 文件里，原始下载文件未被改动。它是人工注入的演示数据，"
    "不是真实观测故障；结论只说明工具能否识别这一类不可靠通道。")

SAMPLES = {
    "event": {
        "label": "已核实真实事件 uw61900332（不注入）",
        "window_start": "2023-03-01T11:34:43",
        "window_end": "2023-03-01T11:36:43",
        "region": "orcas",
        "target_band_hz": [1.0, 12.0],
        "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
        "mode": "full",
        "verified": True,
        "event_id": "uw61900332",
        "catalog": "USGS/ANSS ComCat",
        "origin_time_utc": "2023-03-01T11:35:43.850Z",
        "magnitude": 2.67,
        "magnitude_type": "ml",
        "place": "6 km WNW of Orcas, Washington",
        "catalog_url": ("https://earthquake.usgs.gov/fdsnws/event/1/query"
                        "?eventid=uw61900332&format=geojson"),
        "stations": ["UW.OLGA", "UW.TURTL", "UW.RNWY", "UW.LOPEZ"],
        "disclosure": (
            "真实事件：2023-03-01T11:35:44Z，M2.67，“6 km WNW of Orcas, Washington”"
            "（USGS/ANSS ComCat，人工复核）。事件位于窗口开始后约 61 秒，前面是"
            "事件前噪声。本样例不注入任何东西。"),
        "stability_note": (
            "这个真实窗口的自动到时并不稳定：在本项目固定的 13 配置网格上，"
            "若干台站的到时分成互相分离的簇。样例用它来演示“依据/影响/建议/重算”。"
            "每一台实际落在哪一簇、离群多少、跨度多大，以本轮计算出的诊断结果为准"
            "（见结果区的逐站表），本说明不代替计算结果。"
            "事件震源时刻 2023-03-01T11:35:43.85Z 只用于说明该时刻在窗口中的位置；"
            "没有目录参考的其它自由窗口不套用任何事件结论。"),
        "offline_note": (
            "已有完整缓存时可复用保存的观测，不发起新的波形请求。"
            "首次获取或缓存不完整时需要联网；实际请求数和缓存命中数见核查记录。"),
        "injection_note": _INJECTION,
    },
    "quiet": {
        "label": "同一区域静默对照",
        "window_start": "2023-03-01T08:34:43",
        "window_end": "2023-03-01T08:36:43",
        "region": "orcas",
        "target_band_hz": [1.0, 12.0],
        "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
        "mode": "full",
        "verified": True,
        "event_id": None,
        "disclosure": (
            "静默对照：与真实事件同一区域、时长一致，但取事件前 3 小时的窗口。"
            "该窗口没有已知事件。把两组结果并排看，才能分清“有信号”与“只有噪声”。"),
        "injection_note": _INJECTION,
    },
    "failure": {
        "label": "会失败的样例（该时窗无数据）",
        "window_start": "2035-06-01T00:00:00",
        "window_end": "2035-06-01T00:02:00",
        "region": "orcas",
        "target_band_hz": [1.0, 12.0],
        "question": "这段观测能不能用于 1–12 Hz 的 P/S 拾取？",
        "mode": "question",
        "verified": True,
        "event_id": None,
        "disclosure": (
            "失败样例：2035 年的这个窗口在 FDSN 上没有任何数据。用它看失败是怎么"
            "被说清楚的——结果不会说“没有事件”，而是逐台站给出请求级原因"
            "（例如 HTTP 422），把所问字段交付为“未知”，并且不会用历史结果顶替。"
            "这不是把失败藏起来，而是把失败归到它真正发生的那一层。"),
        "injection_note": _INJECTION,
    },
}


def samples() -> dict:
    """The samples as served to the interface, with the evidence check disclosed."""
    out = {k: dict(v) for k, v in SAMPLES.items()}
    if EVIDENCE.exists():
        try:
            ev = json.loads(EVIDENCE.read_text())
            out["event"]["evidence_file"] = str(EVIDENCE.relative_to(ROOT))
            out["event"]["evidence_verified_at_utc"] = ev.get("verified_at_utc")
        except ValueError:
            pass
    return out
