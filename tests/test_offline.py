"""Check offline observation guards, cache misses and provider refusal."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import fdsn  # noqa: E402



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
    project = tmp_path / "project"
    (project / "analysis").mkdir(parents=True)
    shutil.copyfile(ROOT / "analysis/model-budget.json", project / "analysis/model-budget.json")
    (tmp_path / "deepseek_model.txt").write_text("Key: fake-offline-test-key\n")
    code = """
const {startBridge} = await import('./main/provider_bridge.mjs');
const b = await startBridge({project: process.env.ZHENJI_TEST_PROJECT, provider: 'deepseek',
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
        env={**os.environ, "EQ_STRICT_OFFLINE": "1", "ZHENJI_TEST_PROJECT": str(project),
             "EQ_BUDGET_LEDGER": str(tmp_path / "ledger.json"),
             "EQ_REQUEST_LOG_DIR": str(tmp_path / "requests")})
    assert proc.returncode == 0, proc.stderr[-300:]
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["status"] == 503
    assert "offline_refused" in result["body"]
