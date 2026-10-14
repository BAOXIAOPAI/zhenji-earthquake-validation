"""Produce the acceptance-package side files: cost snapshot, checksum manifest,
third-party notice, and the minimal sample. No network, no model calls.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DELIVERY = ROOT / "delivery/next"
SAMPLE = DELIVERY / "sample"

SOURCES = [
    "main/core.py", "main/run_experiment.py", "main/make_delivery.py",
    "main/mcp_server.py", "main/agent_run.mjs", "main/permission.mjs",
    "main/provider_bridge.mjs", "main/run_agent_arm.py", "main/prepare_agent.py",
    "src/budget.mjs", "prototype/model-budget.json",
    "tests/test_main_core.py", "prototype/quality.py",
    "main/server.py", "main/review.py", "main/start.sh",
    "main/static/index.html", "main/static/app.js", "main/static/style.css",
    "explore/probes/fdsn.py", "explore/probes/b_coherence.py",
    "explore/probes/c_robustness.py",
]
DOCS = ["HANDOFF.md", "OPPORTUNITIES.md", "SCOPE.md", "PROTOCOL.md", "CLAIMS.md",
        "FAILURES.md", "REPRODUCE.md", "RESULTS.json",
        "screenshots/README.md", "screenshots/01-instrument-2-requests.png",
        "screenshots/02-propagation-6-requests.png",
        "screenshots/03-locked-case-8-requests.png"]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cost_snapshot():
    script = (
        "const {Budget}=await import('./src/budget.mjs');"
        "const fs=await import('node:fs');"
        "const cfg=JSON.parse(fs.readFileSync('prototype/model-budget.json','utf8'));"
        "const b=new Budget('runs/model-budget-ledger.json',cfg);"
        "const e=b.state.entries;"
        "const t=e.reduce((s,x)=>s+(x.cost_upper_cny??x.reserved_cny??0),0);"
        "const m=e.filter(x=>String(x.case_id||'').startsWith('main-'));"
        "console.log(JSON.stringify({entries:e.length,"
        "by_provider:Object.fromEntries([...new Set(e.map(x=>x.provider))]"
        ".map(p=>[p,e.filter(x=>x.provider===p).length])),"
        "cumulative_upper_cny:t,this_session_entries:m.length,"
        "this_session_upper_cny:m.reduce((s,x)=>s+(x.cost_upper_cny??x.reserved_cny??0),0),"
        "normal_stop_cny:cfg.normal_stop,total_limit_cny:cfg.total_limit,"
        "max_requests_per_case:cfg.max_requests_per_case}));b.close();"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def third_party():
    return """# 第三方来源、许可与责任边界

## 数据来源

| 来源 | 用途 | 访问方式 | 许可/说明 |
|---|---|---|---|
| EarthScope Consortium FDSN Web Services | 波形与台站响应元数据 | `service.iris.edu/fdsnws/{station,dataselect}/1` | 公开服务，无需凭据。各台网数据按其自身许可发布，本项目仅做研究性分析，不再分发原始波形 |
| USGS ComCat | **仅评价器使用**的独立事件参考 | `earthquake.usgs.gov/fdsnws/event/1` | 美国政府公开数据（公有领域） |
| miniPNW (PNW-ML, niyiyu) | 项目既有分类评测集 | 本地既有文件 | **本轮的 311 个测试事件未使用** |

## 软件

| 组件 | 版本 | 许可 |
|---|---|---|
| ObsPy | 1.5.1 | LGPL-3.0 |
| NumPy | 2.5.3 | BSD-3-Clause |
| MCP Python SDK | 2.2.0 | MIT |
| Node.js | 24.18.0 | MIT |
| tsx | 4.23.13 | MIT |
| Agnes Harness (AGH) | vendored 于 `vendor/agnes-harness` | 随仓库既有，未修改 |

## 模型服务

- DeepSeek API（provider `deepseek`，model `deepseek-flash`）。
- 凭据不在本交付包内，见 `REPRODUCE.md` §3.3。

## 责任边界（必须在项目说明中转达）

1. 本项目是**离线任务验证工作台**，**不是地震预警系统**，不提供实时告警，
   不构成任何形式的地震预测或防灾建议。
2. 系统输出的 `instrument_artifact` / `coherent_multistation` /
   `single_station_only` / `not_decidable` 是**观测层面的解释类别**，
   不等于震源类型、震级、位置或深度的判定。
3. 单台短窗观测在原理上无法确定位置、深度、震级、震源类型、震源机制，
   也无法区分天然地震与人工事件、雷声、音爆、落石。
4. `miniPNW` 的 `px` 类别是**未确认的爆破**，不是已确认爆炸。
5. 安静对照的"无目录事件"受参考目录探测下限限制，
   **不得表述为"无误报"**（见 `FAILURES.md` F-5）。
"""


def minimal_sample(results):
    by_case = {}
    for row in results:
        by_case.setdefault(row["case_id"], {})[row["arm"]] = {
            "verdict": row.get("verdict"), "reason": row.get("reason"),
            "requests_to_verdict": row.get("requests_to_verdict"),
            "correct": row.get("correct"),
        }
    sample = {
        "purpose": "本轮主张的最小可复现证据：同一正确判决，问题条件化把外部请求从 6 降到 2",
        "case_id": "A1_injected_artifact",
        "injected": True,
        "injection_note": "第一个取得台站的 E 通道被替换为恒定值（人工注入，非自然观测）；"
                          "置于安静时窗，使伪迹成为主导解释",
        "question": "这个候选异常是不是仪器或元数据问题造成的？",
        "arms": by_case.get("A1_injected_artifact", {}),
        "reproduce": [
            "MPLCONFIGDIR=.cache/matplotlib .venv/bin/python main/run_experiment.py A1_injected_artifact",
            "MPLCONFIGDIR=.cache/matplotlib .venv/bin/python main/make_delivery.py",
        ],
        "expected": {"full_evidence": 12, "fixed_pipeline": 6,
                     "adaptive_rule": 6, "adaptive_question": 2},
    }
    SAMPLE.mkdir(parents=True, exist_ok=True)
    (SAMPLE / "sample.json").write_text(json.dumps(sample, ensure_ascii=False, indent=2))
    (SAMPLE / "README.md").write_text(
        "# 最小样例\n\n"
        "`sample.json` 记录了一个案例（`A1_injected_artifact`）在四个组下的判决与请求数。\n\n"
        "**为什么是这个案例。** 它是本轮唯一一个「问题类型改变了最优采集量」的案例：\n"
        "问的是仪器问题，而仪器问题由单个台站的通道状态即可定论，\n"
        "不需要再采其余台站。\n\n"
        "**预期结果。**\n\n"
        "| 组 | 判决 | 请求数 |\n|---|---|---|\n"
        "| `full_evidence` | `instrument_artifact` | 12 |\n"
        "| `fixed_pipeline` | `instrument_artifact` | 6 |\n"
        "| `adaptive_rule` | `instrument_artifact` | 6 |\n"
        "| `adaptive_question` | `instrument_artifact` | **2** |\n\n"
        "**注入声明。** 该案例的伪迹是**人工注入**的，不是自然观测到的故障，\n"
        "在 `RESULTS.json` 与本文件中都以 `injected: true` 明确标注。\n"
        "注入会改写磁盘上的 MSEED，因此**该台站的 `waveform_sha256` 在校验时不再匹配**，\n"
        "这是预期行为而不是损坏：全仓 131 条波形哈希中恰好 4 条不一致，\n"
        "就是本案例在 4 个组下的首个台站。\n\n"
        "**复现命令**见 `sample.json` 的 `reproduce` 字段。\n")
    return sample


def main():
    results = json.loads((DELIVERY / "RESULTS.json").read_text())["rule_arm_rows"]

    costs = cost_snapshot()
    (DELIVERY / "COSTS.json").write_text(json.dumps(
        {"generated_by": "main/make_manifest.py", **costs}, ensure_ascii=False, indent=2))

    (DELIVERY / "THIRD_PARTY.md").write_text(third_party())

    sample = minimal_sample(results)

    files = {}
    for name in DOCS + ["COSTS.json", "THIRD_PARTY.md", "sample/sample.json",
                        "sample/README.md"]:
        path = DELIVERY / name
        if path.exists():
            files[f"delivery/next/{name}"] = {"sha256": sha256(path),
                                              "bytes": path.stat().st_size}
    for name in SOURCES:
        path = ROOT / name
        if path.exists():
            files[name] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    manifest = {
        "generated_by": "main/make_manifest.py",
        "note": "校验清单。源文件路径相对 earthquake-validation/。",
        "files": files,
        "count": len(files),
    }
    (DELIVERY / "MANIFEST.json").write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2))

    print(f"COSTS.json      累计上界 {costs['cumulative_upper_cny']:.6f} 元，"
          f"本轮 {costs['this_session_upper_cny']:.6f} 元 / {costs['this_session_entries']} 条")
    print(f"MANIFEST.json   {manifest['count']} 个文件已校验")
    print(f"THIRD_PARTY.md  已写入")
    print(f"sample/         A1 四组判决 {sample['arms']}")


if __name__ == "__main__":
    main()