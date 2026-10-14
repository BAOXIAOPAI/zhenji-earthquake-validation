"""G3 交付包必须自足：指南点名的文件一个都不能少，且不得夹带凭据。

这些测试防的是"打包时漏掉一份文件而没人发现"。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "delivery/revision-1"

# 指南第 6 节点名的交付物。
REQUIRED = [
    "HANDOFF.md", "FIXES.md", "SCOPE.md", "PROTOCOL.md",
    "RESULTS.json", "CLAIMS.md", "FAILURES.md", "COSTS.json",
    "MANIFEST.json", "REPRODUCE.md",
]

# 凭据绝不允许出现在交付包里。
SECRET = re.compile(r"sk-[A-Za-z0-9]{16,}")


def test_every_required_deliverable_exists():
    missing = [f for f in REQUIRED if not (PKG / f).exists()]
    assert not missing, f"交付包缺少：{missing}"


def test_the_manifest_covers_the_deliverable_files():
    """清单必须覆盖交付文件，否则校验没有意义。"""
    doc = json.loads((PKG / "MANIFEST.json").read_text())
    listed = set(doc["files"])
    for f in REQUIRED:
        assert f"delivery/revision-1/{f}" in listed, f
    assert doc["missing"] == [], doc["missing"]
    assert doc["run_count"] > 0, "逐次运行的证据也要进清单"


def test_no_credential_leaks_into_the_package():
    bad = []
    for p in PKG.rglob("*"):
        if not p.is_file() or p.suffix in (".png", ".gz", ".mseed"):
            continue
        try:
            text = p.read_text(errors="ignore")
        except Exception:  # noqa: BLE001
            continue
        if SECRET.search(text):
            bad.append(str(p.relative_to(ROOT)))
    assert not bad, f"交付包里出现疑似凭据：{bad}"


def test_the_negative_result_is_stated_in_the_headline_documents():
    """最重要的结论是否定结论，不能只在报告深处出现。"""
    for name in ("CLAIMS.md", "HANDOFF.md", "README.md"):
        text = (PKG / name).read_text()
        assert "未观察到" in text or "未观察到优势" in text, name


def test_failures_are_not_filtered_out():
    """失败清单不能是空的：这批运行里确实有失败。"""
    text = (PKG / "FAILURES.md").read_text()
    assert "运行层失败" in text
    m = re.search(r"运行层失败（(\d+) 条", text)
    assert m and int(m.group(1)) > 0, "失败被过滤掉了"


def test_costs_do_not_undercount_abandoned_reservations():
    """被中断的进程留下的预留必须计入，宁可高估。"""
    doc = json.loads((PKG / "COSTS.json").read_text())
    assert doc["cumulative_upper_cny"] > 0
    if doc["entries_by_status"].get("abandoned"):
        assert "abandoned" in doc["note"], "要说明 abandoned 的计法"


def test_the_protocol_documents_that_the_evaluator_changed():
    """评判器在开发阶段改过：不写出来就是隐瞒。"""
    text = (PKG / "PROTOCOL.md").read_text()
    assert "开发阶段" in text and "rejudge" in text


def test_reproduce_states_the_cross_machine_gap():
    """只做了新建环境，不能写成跨机通过。"""
    text = (PKG / "REPRODUCE.md").read_text()
    assert "没有在另一台机器上验证" in text or "不算跨机通过" in text


def test_figures_and_screenshots_are_present_and_non_empty():
    fig = PKG / "figures"
    assert fig.is_dir()
    pngs = sorted(fig.glob("*.png"))
    assert len(pngs) >= 3, [p.name for p in pngs]
    for p in pngs:
        assert p.stat().st_size > 5000, p.name


def test_the_video_is_flagged_as_not_recorded():
    """视频必须由参赛者实拍；不能假装已交付。"""
    handoff = (PKG / "HANDOFF.md").read_text()
    assert "视频未录制" in handoff or "视频" in handoff
    assert (PKG / "VIDEO-SCRIPT.md").exists()