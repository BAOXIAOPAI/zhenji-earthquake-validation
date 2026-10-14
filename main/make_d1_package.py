"""D1 最小可携带样例包构建器（review-5 指南 §5）。

产出 `delivery/revision-5/d1-package/`：
  - materials/cache/     运行样例实际用到的原始材料（EarthScope 响应原样，
                         .bin/.json 成对；原始文件保全，不做任何改写）
  - samples.json         两个真实样例的规格、问题与预期结果
  - MANIFEST-SHA.txt     每个材料文件的 SHA256（不含清单自身）
  - README.md            来源/许可、UTC 时窗、参数、入口命令、环境依赖、
                         派生故障样例的注入说明（单列）

只打包真实用到的材料：构建时以跟踪模式跑一遍样例流程，记录
cached_get 实际命中的每一个 URL，拷贝对应缓存条目——不扩大数据集。
派生故障（E→constant）是运行时注入，不修改任何原始文件；注入规格在
README 单列。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import fdsn  # noqa: E402
import runlog  # noqa: E402

OUT = ROOT / "delivery/revision-5/d1-package"


def _trace_run(case_id: str, question: str, workdir: Path,
               followup=None) -> tuple[dict, dict | None]:
    """在一个独立 runlog 里跑样例流程，返回（首轮结果, 反馈结果）。

    用真实生产入口（review.run_review / run_followup）跑，保证材料集合
    与实际运行完全一致。
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {"RUNLOG_DIR": str(Path(tmp) / "runlog"),
               "EQ_CACHE_DIR": str(ROOT / "main/cache"),
               "PATH": "/usr/bin:/bin",
               "MPLCONFIGDIR": str(Path(tmp) / "mpl")}
        code = (
            "import sys, json\n"
            f"sys.path.insert(0, {str(ROOT / 'main')!r})\n"
            f"sys.path.insert(0, {str(ROOT / 'explore/probes')!r})\n"
            "import review, core\n"
            "core.fdsn.set_cache(core.CACHE)\n"
            "seen = {}\n"
            "orig = core.fdsn.cached_get\n"
            "def traced(url, timeout=60):\n"
            "    payload, hit, sha = orig(url, timeout)\n"
            "    seen[url] = sha\n"
            "    return payload, hit, sha\n"
            "core.fdsn.cached_get = traced\n"
            "core.cached_get = traced\n"
            f"res = review.run_review({case_id!r}, {question!r}, 'full')\n"
            "out = {'first': {'run_id': res['run_id'], 'verdict': res['verdict'],"
            " 'n_eligible': res['n_eligible']}, 'seen': seen}\n"
        )
        if followup:
            change, instruction = followup
            code += (
                f"child = review.run_followup(res['run_id'],"
                f" change={change!r}, instruction={instruction!r})\n"
                "out['followup'] = {'run_id': child['run_id'],"
                " 'verdict': child['verdict']}\n"
            )
        code += (
            "import pathlib\n"
            f"pathlib.Path({str(workdir / 'trace.json')!r})"
            ".write_text(json.dumps(out, ensure_ascii=False, indent=1))\n"
        )
        proc = subprocess.run([sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, cwd=str(ROOT))
        if proc.returncode != 0:
            print(proc.stderr[-2000:], file=sys.stderr)
            raise SystemExit(f"样例流程失败：{case_id}")
        trace = json.loads((workdir / "trace.json").read_text())
        first = trace["first"]
        follow = trace.get("followup")
        return first, follow, trace["seen"]


def _copy_materials(seen: dict, dest: Path) -> list[dict]:
    """把 seen 的 URL 对应缓存条目原样拷进包（.bin/.json 成对）。"""
    dest.mkdir(parents=True, exist_ok=True)
    entries = []
    for url in sorted(seen):
        key = hashlib.sha256(url.encode()).hexdigest()[:32]
        meta_path = ROOT / "main/cache" / (key + ".json")
        record = json.loads(meta_path.read_text())
        bin_src = ROOT / "main/cache" / record["file"]
        shutil.copy2(bin_src, dest / record["file"])
        shutil.copy2(meta_path, dest / (key + ".json"))
        digest = hashlib.sha256((dest / record["file"]).read_bytes()).hexdigest()
        assert digest == record["sha256"], f"材料校验失败：{url}"
        entries.append({"url": url, "file": record["file"],
                        "meta": key + ".json", "sha256": digest,
                        "bytes": record["bytes"]})
    return entries


def _expected_e1() -> dict:
    """E1 主样例的预期结果（确定性判决 + 逐站诊断摘要）。"""
    return {
        "question": "这个信号是不是多台共同记录到的？",
        "question_id": "Q_propagating",
        "verdict": "coherent_multistation",
        "coincidence": 6,
        "note": "三站/三秒公开标准：3 秒窗内 ≥3 台触发。逐站诊断：MCW "
                "unstable_bimodal（双簇，spread≈4.4 s）；ORCA 通道家族 HN"
                "（HNE/HNN/HNZ，200 Hz）非 HH；其余站 stable。",
        "feedback_after_exclude": {
            "change": {"kind": "exclude", "station": "OLGA"},
            "instruction": "把 OLGA 排除掉，结论会不会变？",
            "expected_verdict": "coherent_multistation",
            "note": "排除一台后 5 台仍在 3 秒窗内触发，聚集不变；"
                    "参与集合收窄、父记录不改。",
        },
    }


def _expected_s1() -> dict:
    """S1 稀疏样例的预期结果（唯一候选 → 排除 → 正确未知）。"""
    return {
        "question": "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
        "question_id": "Q_usable",
        "verdict": "pick_applicable",
        "n_eligible": 1,
        "note": "唯一合格候选 HOOD（BH 三分量，50 Hz）满足频带。",
        "feedback_after_exclude": {
            "change": {"kind": "exclude", "station": "HOOD"},
            "instruction": "排除 HOOD，只复用已有观测重算，不要新增取数。",
            "expected_verdict": "pick_unknown",
            "note": "唯一候选被用户排除后无可用观测：正确未知"
                    "（answered=false，task_completed=false），"
                    "不是否定结论。",
        },
    }


def build() -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        # 样例 1：E1 主窗（真实事件，全诊断）+ 排除反馈。
        e1_first, e1_follow, e1_seen = _trace_run(
            "E1_orcas_event", "这个信号是不是多台共同记录到的？", work,
            followup=({"kind": "exclude", "station": "OLGA"},
                      "把 OLGA 排除掉，结论会不会变？"))
        # 样例 2：S1 稀疏（唯一候选 → 正确未知）。
        s1_first, s1_follow, s1_seen = _trace_run(
            "S1_sparse_2003", "这段观测能不能用来做 1–12 Hz 的 P/S 拾取？",
            work,
            followup=({"kind": "exclude", "station": "HOOD"},
                      "排除 HOOD，只复用已有观测重算，不要新增取数。"))
        entries = _copy_materials({**e1_seen, **s1_seen},
                                  OUT / "materials/cache")
        samples = {
            "schema": "d1-package/v1",
            "built_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "samples": [
                {"id": "E1", "case_id": "E1_orcas_event",
                 "case": "真实 Orca 次声窗事件（UW 网，2025-03-03）",
                 "window_start": "2025-03-03T13:02:20Z",
                 "window_end": "2025-03-03T13:04:20Z",
                 "stations": ["OLGA", "MCW", "ORCA", "LUMI", "GUEM", "TURTL"],
                 "channel_families": "EN?（200 Hz）为主，ORCA 为 HN?（200 Hz）",
                 "first_run": e1_first, "followup_run": e1_follow,
                 "expected": _expected_e1()},
                {"id": "S1", "case_id": "S1_sparse_2003",
                 "case": "稀疏窗（2003，唯一合格候选 HOOD）",
                 "window_start": "2003-01-19T18:12:00Z",
                 "window_end": "2003-01-19T18:14:00Z",
                 "stations": ["HOOD"],
                 "channel_families": "BH?（50 Hz）",
                 "first_run": s1_first, "followup_run": s1_follow,
                 "expected": _expected_s1()},
            ],
            "materials": entries,
            "parameters": {
                "target_band_hz": [1, 12],
                "onset_grid": "13 个频带-滤波组合（diagnostics.py 网格）",
                "coincidence_window_s": core.COINCIDENCE_WINDOW_S,
                "multistation_standard": "3 秒窗内 ≥3 台触发（公开给用户）",
                "stability_class": "stable / unstable / unstable_bimodal /"
                                   " insufficient_configurations / not_applicable",
            },
            "derived_fault_sample": {
                "note": "派生故障单列：E→constant 注入是运行时派生"
                        "（prepare→acquire 阶段对内存副本置常值），"
                        "不修改、不替换包内任何原始文件。",
                "runtime_spec": {
                    "case": "C1_T1_orcas_badE",
                    "inject_station": "OLGA",
                    "injection": "E 分量置常值（ENE:constant 标记）",
                    "expected_first_verdict": "pick_partially_applicable",
                    "expected_after_vertical_goal": "pick_applicable",
                },
            },
        }
        (OUT / "samples.json").write_text(
            json.dumps(samples, ensure_ascii=False, indent=1))
    _write_manifest(OUT)
    _write_readme(OUT, entries)
    print(f"D1 包构建完成：{OUT}")
    print(f"  材料 {len(entries)} 份（{sum(e['bytes'] for e in entries)} 字节）")
    return OUT


def _write_manifest(out: Path) -> None:
    lines = ["# D1 材料清单（SHA256；不含本清单自身）", ""]
    for path in sorted((out / "materials/cache").iterdir()):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  materials/cache/{path.name}")
    lines.append("")
    (out / "MANIFEST-SHA.txt").write_text("\n".join(lines))


def _write_readme(out: Path, entries: list[dict]) -> None:
    samples = json.loads((out / "samples.json").read_text())
    e1 = next(s for s in samples["samples"] if s["id"] == "E1")
    s1 = next(s for s in samples["samples"] if s["id"] == "S1")
    readme = f"""# 震迹 D1 最小可携带样例包

构建时间：{samples['built_at']}（`main/make_d1_package.py`，材料与运行
入口同源，原样拷贝自内容寻址缓存）。

## 内容

| 项 | 说明 |
| --- | --- |
| `materials/cache/` | {len(entries)} 份原始材料（EarthScope FDSN 响应原样：{sum(e['bytes'] for e in entries)} 字节） |
| `samples.json` | 两个真实样例的规格、参数与预期结果 |
| `MANIFEST-SHA.txt` | 材料逐文件 SHA256（不含清单自身） |
| `offline-check.json` | 严格离线 4 场景验收记录（`main/run_d1_check.py` 生成） |

## 样例

1. **E1（主样例）**：{e1['window_start']} 起 2 分钟，UW 网
   {', '.join(e1['stations'])}。问题“这个信号是不是多台共同记录到的？”
   预期：`{e1['expected']['verdict']}`（三站/三秒标准，
   coincidence={e1['expected']['coincidence']}）；逐站诊断含 MCW
   unstable_bimodal 与 ORCA HN 家族 200 Hz。反馈轮：排除 OLGA 后聚集
   结论不变、参与集合收窄、父记录不改。
2. **S1（稀疏样例）**：{s1['window_start']} 起 2 分钟，唯一合格候选
   HOOD。预期：首轮 `{s1['expected']['verdict']}`；排除 HOOD 后
   **正确未知**（不是否定结论）。

## 来源与许可

数据来源：EarthScope/IRIS FDSN web service（service.earthscope.org，
公开服务，无需凭证）。所有材料是按 URL 取回的原始响应（station 元数据
text 与 dataselect miniseed），未做任何改写；SHA256 见
MANIFEST-SHA.txt。使用遵守 EarthScope 数据政策（科学用途、注明来源）。

## 派生故障样例（单列）

E→constant 是**运行时注入**（内存副本置常值并打 `ENE:constant` 标记），
不修改包内任何原始文件。规格见 samples.json 的 `derived_fault_sample`。

## 严格离线运行（入口命令）

```bash
# 1) 验收 4 场景（独立目录、网络拦截开启、EQ_STRICT_OFFLINE=1）
.venv/bin/python main/run_d1_check.py delivery/revision-5/d1-package

# 2) 用本包材料启动界面（严格离线，真实外部请求 0）
EQ_STRICT_OFFLINE=1 EQ_CACHE_DIR=delivery/revision-5/d1-package/materials/cache \\
    .venv/bin/python main/server.py
```

离线口径（“真实外部请求 0”）：发现请求 0 / 波形 HTTP 0 / 模型 provider
0 / 其他外联 0——四类分别计数，见 offline-check.json 与
`fdsn.offline_request_counts()`。逻辑取数（从本地缓存命中）单独计数，
不是外部请求。模型在离线模式下被拒绝调用（provider_bridge 返回 503
offline_refused）；模型轨迹可回放已记录 run，**不得声称离线执行了真实
模型**。

## 环境依赖

Python 3.12 + obspy（.venv/bin/python，依赖见项目）；界面为
main/server.py（纯标准库 http.server，回环地址）。网络拦截：
EQ_STRICT_OFFLINE=1 时 fdsn.cached_get 未命中缓存立即报
OfflineMaterialMissing（不联网）；验收脚本另装 socket 级拦截证明零外联。
"""
    (out / "README.md").write_text(readme)


if __name__ == "__main__":
    build()
