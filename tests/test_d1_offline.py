"""D1 严格离线模式的测试（review-5 指南 §5）。

两层：
  1. 无付费单元测试——离线开关、缓存未命中拒绝、四类外联计数口径、
     provider 桥的离线拒绝、清单不含自身；
  2. 端到端验收——跑 `run_d1_check.check()`（独立子进程 + 网络拦截），
     包不存在时跳过。这是“独立目录、新建环境、网络拦截开启”的
     机器验收，热缓存 0 波形 HTTP 不替代它。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import fdsn  # noqa: E402

PACKAGE = ROOT / "delivery/revision-5/d1-package"


@pytest.fixture
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("EQ_STRICT_OFFLINE", "1")
    fdsn._OFFLINE_STATE["refused"] = []
    fdsn._OFFLINE_STATE["cache_hits"] = 0
    fdsn.set_cache(tmp_path / "empty-cache")
    yield
    monkeypatch.delenv("EQ_STRICT_OFFLINE", raising=False)


def test_offline_cache_miss_refuses_without_network(offline):
    """缓存未命中 → OfflineMaterialMissing，绝不联网（urlopen 装哨兵）。"""
    calls = []

    def sentinel(url, *args, **kwargs):
        calls.append(url)
        raise AssertionError(f"离线模式不得联网：{url}")

    real = urllib.request.urlopen
    urllib.request.urlopen = sentinel
    try:
        with pytest.raises(fdsn.OfflineMaterialMissing) as exc:
            fdsn.cached_get(
                "https://service.earthscope.org/fdsnws/station/1/query?x=1")
    finally:
        urllib.request.urlopen = real
    assert calls == [], "离线模式不得发起任何网络请求"
    assert exc.value.category == "discovery"
    assert "station/1/query" in exc.value.url


def test_offline_counts_report_all_four_categories_zero(offline):
    counts = fdsn.offline_request_counts()
    assert counts["mode"] == "strict_offline"
    for key in ("discovery_requests", "waveform_http", "model_provider",
                "other_external"):
        assert counts[key] == 0, f"{key} 必须为 0（真实外部请求 0 的口径）"
    assert counts["cache_hits"] == 0
    # 一次被拒的缺失也计入 refused（不掩盖）。
    with pytest.raises(fdsn.OfflineMaterialMissing):
        fdsn.cached_get(
            "https://service.earthscope.org/fdsnws/dataselect/1/query?x=1")
    counts = fdsn.offline_request_counts()
    assert counts["refused"][0]["category"] == "waveform_http"
    assert counts["waveform_http"] == 0  # 拒绝不等于发起了请求


def test_offline_cache_hit_serves_normally(offline):
    """缓存命中的材料照常可用——离线不是拒绝服务。"""
    key = fdsn.hashlib.sha256(b"https://example.invalid/x").hexdigest()[:32]
    cache = Path(fdsn._CACHE)
    (cache / f"{key}.bin").write_bytes(b"payload")
    (cache / f"{key}.json").write_text(json.dumps(
        {"url": "https://example.invalid/x", "file": f"{key}.bin",
         "sha256": fdsn.hashlib.sha256(b"payload").hexdigest(),
         "bytes": 7}))
    payload, hit, digest = fdsn.cached_get("https://example.invalid/x")
    assert payload == b"payload" and hit is True
    assert fdsn.offline_request_counts()["cache_hits"] == 1


def test_corrupt_cache_body_is_material_missing(offline):
    """元数据在而正文损坏：离线下同样是明确缺料，不是联网回退。"""
    key = fdsn.hashlib.sha256(b"https://example.invalid/y").hexdigest()[:32]
    cache = Path(fdsn._CACHE)
    (cache / f"{key}.bin").write_bytes(b"corrupted")
    (cache / f"{key}.json").write_text(json.dumps(
        {"url": "https://example.invalid/y", "file": f"{key}.bin",
         "sha256": "0" * 64, "bytes": 9}))
    with pytest.raises(fdsn.OfflineMaterialMissing):
        fdsn.cached_get("https://example.invalid/y")
    refusals = fdsn.offline_request_counts()["refused"]
    assert refusals[0]["reason"] == "cache_body_missing_or_corrupt"


def test_provider_bridge_refuses_when_offline(tmp_path):
    """EQ_STRICT_OFFLINE=1 时 provider 桥对补全请求返回 503 offline_refused。

    离线模式下模型被拒绝调用；轨迹只能回放，不声称离线执行真实模型。
    """
    code = """
const {startBridge} = await import('./main/provider_bridge.mjs');
const b = await startBridge({project: process.cwd(), provider: 'deepseek',
                             model: 'deepseek-flash', toolPrefix: 'x'});
try {
  const resp = await fetch(b.baseUrl + '/chat/completions', {
    method: 'POST',
    headers: {'Content-Type': 'application/json',
              Authorization: 'Bearer ' + b.apiKey},
    body: JSON.stringify({model: 'deepseek-flash', messages: [
      {role: 'user', content: 'SEISMIC_REVIEW_RUN=test'}]})});
  const body = await resp.text();
  console.log(JSON.stringify({status: resp.status, body: body.slice(0, 200)}));
} finally {
  await b.close();
}
"""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        capture_output=True, text=True, cwd=str(ROOT), timeout=90,
        env={**os.environ, "EQ_STRICT_OFFLINE": "1"})
    if proc.returncode != 0:
        pytest.skip(f"provider 桥在测试环境不可用：{proc.stderr[-150:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["status"] == 503
    assert "offline_refused" in result["body"]


def test_manifest_excludes_itself_and_covers_materials():
    if not (PACKAGE / "MANIFEST-SHA.txt").exists():
        pytest.skip("D1 包未构建")
    lines = [l.strip() for l in
             (PACKAGE / "MANIFEST-SHA.txt").read_text().splitlines()]
    entries = [l for l in lines if l and not l.startswith("#")]
    rels = [l.split(None, 1)[1] for l in entries]
    assert rels, "清单必须覆盖材料"
    assert not any(r.endswith("MANIFEST-SHA.txt") for r in rels), \
        "清单不得包含自身摘要"
    assert all(r.startswith("materials/cache/") for r in rels)


@pytest.mark.timeout(600)
def test_d1_offline_acceptance_four_scenarios(tmp_path):
    """端到端验收：独立目录、新建环境、网络拦截开启（约 1–2 分钟）。"""
    if not (PACKAGE / "samples.json").exists():
        pytest.skip("D1 包未构建")
    import run_d1_check
    record = run_d1_check.check(PACKAGE, output_dir=tmp_path)
    assert record["manifest_verified"] is True
    for name, scenario in record["scenarios"].items():
        assert scenario["passed"] is True, f"{name}: {scenario['result']}"
        assert scenario["result"]["intercept_count"] == 0, \
            f"{name}：拦截记录非空即存在外联尝试"
    assert record["all_passed"] is True


@pytest.mark.timeout(400)
def test_d1_offline_ui_check_through_real_server(tmp_path):
    """界面级：真实 server + 页面同款 API，严格离线零外联。"""
    if not (PACKAGE / "samples.json").exists():
        pytest.skip("D1 包未构建")
    import run_d1_ui_check
    record = run_d1_ui_check.check(output_dir=tmp_path)
    assert record["passed"] is True
    assert record["first_round"]["http_attempts"] == 0
    assert record["followup"]["parent_unchanged"] is True
