"""G3 科学图：从真实运行的磁盘波形出图，不重算、不联网。

出三张：
  fig1_observation.png  一个真实时窗的观测事实——各台波形、自动到时、3 秒聚集窗
  fig2_stability.png    稳定性网格的分歧（同一到时在不同配置下的散布）
  fig3_comparison.png   三组对照的合同满足率与费用

数据来源是 main/runlog/<run_id>/，每条曲线都能用 run ID 追回原始文件。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / ".cache/matplotlib"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from obspy import read  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "delivery/revision-1/figures"
RUNLOG = ROOT / "main/runlog"
sys.path.insert(0, str(ROOT / "main"))

# 图上中文必须有字形，否则会画成方框。按可用性依次回退。
plt.rcParams["font.sans-serif"] = ["PingFang SC", "Heiti SC", "Songti SC",
                                   "Arial Unicode MS", "Hiragino Sans GB",
                                   "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.25,
                     "figure.dpi": 150, "savefig.bbox": "tight"})


def wave_paths(run):
    """该次运行磁盘上已有的全部波形，派生优先。"""
    found = {}
    for kind in ("derived", "raw"):
        base = run / kind
        if not base.exists():
            continue
        for d in sorted(base.iterdir()):
            f = d / "waveform.mseed"
            if f.exists() and d.name not in found:
                found[d.name] = f
    return found


def pick_run(min_stations=5, want="coherent_multistation"):
    """挑一个真实、可出图、结论为多台共有的运行。"""
    for d in sorted(RUNLOG.iterdir()):
        if not d.is_dir():
            continue
        paths = wave_paths(d)
        if len(paths) < min_stations:
            continue
        v = d / "validation.json"
        if not v.exists():
            continue
        try:
            val = json.loads(v.read_text())
        except Exception:  # noqa: BLE001
            continue
        if val["validator_verdict"]["verdict"] == want:
            return d, paths, val
    return None, None, None


def fig1():
    run, paths, val = pick_run()
    if run is None:
        print("fig1：没有可出图的运行，跳过")
        return
    st = json.loads((run / "mcp_state.json").read_text())
    onsets = {r["station"]: r.get("onset_utc") for r in st.get("acquired", [])
              if r.get("onset_utc") is not None}
    band = (1.0, 8.0)  # 方法频带，冻结值
    stations = sorted(paths, key=lambda c: next(
        (r.get("distance_km") or 1e9 for r in st.get("acquired", [])
         if r["candidate_id"] == c), 1e9))

    fig, axes = plt.subplots(len(stations), 1, figsize=(9, 1.5 * len(stations)),
                             sharex=True)
    axes = np.atleast_1d(axes)
    t0 = None
    streams = {}
    for c in stations:
        try:
            tr = read(str(paths[c]), format="MSEED")
            tr.merge(method=1, fill_value="interpolate")
            tr.detrend("linear")
            tr.filter("bandpass", freqmin=band[0], freqmax=band[1], corners=4,
                      zerophase=False)  # 因果，与流水线一致
            streams[c] = tr
            if t0 is None:
                t0 = tr[0].stats.starttime
        except Exception as exc:  # noqa: BLE001
            print(f"  跳过 {c}: {exc}")

    for ax, c in zip(axes, stations):
        meta = next((r for r in st.get("acquired", []) if r["candidate_id"] == c), {})
        name = meta.get("station", c[:8])
        dist = meta.get("distance_km")
        if c not in streams:
            ax.text(0.01, 0.5, "(波形不可读)", transform=ax.transAxes)
            continue
        tr = streams[c]
        for k, (comp, col) in enumerate(zip(tr, ("tab:blue", "tab:green",
                                                 "tab:orange"))):
            tt = comp.times() + (comp.stats.starttime - t0)
            ax.plot(tt, comp.data / (np.abs(comp.data).max() or 1) + k * 2.2,
                    lw=0.5, color=col, label=comp.stats.channel[-1])
        on = onsets.get(name)
        if on is not None:
            ax.axvline(on - t0.timestamp, color="crimson", lw=1.1, alpha=0.85)
        ax.set_ylabel(f"{name}\n{dist:.1f} km" if dist is not None else name, fontsize=8)
        ax.set_yticks([])
    axes[0].set_title(
        f"观测事实：{len(streams)} 个台站的因果 {band[0]:g}–{band[1]:g} Hz 波形与自动到时\n"
        f"run {run.name} ｜ 判决 {val['validator_verdict']['verdict']} "
        f"｜ 3 秒窗内触发 {val['validator_verdict'].get('coincidence')} 台", fontsize=10)

    if onsets:
        ts = sorted(onsets[s] - t0.timestamp for s in stations if s in onsets
                    and onsets.get(s) is not None)
        if ts:
            best, bi = 0, 0
            for i, a in enumerate(ts):
                n = sum(1 for b in ts[i:] if 0 <= b - a <= 3.0)
                if n > best:
                    best, bi = n, i
            axes[-1].axvspan(ts[bi], ts[bi] + 3.0, color="crimson", alpha=0.16,
                             label=f"3 秒聚集窗（{best} 台）")
            axes[-1].legend(loc="upper right", fontsize=7)
    axes[-1].set_xlabel("相对窗口起点的秒数 (s)")
    fig.text(0.005, -0.004,
             "红线＝自动到时；红带＝3 秒聚集窗。到时聚集是观测统计，不是震源或因果主张。",
             fontsize=7.5, color="0.3")
    p = OUT / "fig1_observation.png"
    fig.savefig(p)
    plt.close(fig)
    print(f"fig1 → {p.relative_to(ROOT)}（run {run.name}）")
    return run.name


def fig2():
    """稳定性：同一到时在预声明网格上的散布。真实诊断输出，不重算。"""
    rows = []
    for d in sorted(RUNLOG.iterdir()):
        s = d / "stability.json"
        if s.exists():
            try:
                rows.append((d.name, json.loads(s.read_text())))
            except Exception:  # noqa: BLE001
                pass
    # 也接受 mcp_state 里的 stability_checks
    if not rows:
        for d in sorted(RUNLOG.iterdir()):
            m = d / "mcp_state.json"
            if not m.exists():
                continue
            try:
                st = json.loads(m.read_text())
            except Exception:  # noqa: BLE001
                continue
            for cid, rec in (st.get("stability_checks") or {}).items():
                rows.append((d.name, {"station": rec.get("station"),
                                      "stability": rec.get("stability"),
                                      "spread_s": rec.get("spread_s"),
                                      "n_valid": rec.get("n_valid"),
                                      "grid_size": rec.get("grid_size"),
                                      "clusters": rec.get("clusters")}))
    if not rows:
        print("fig2：没有稳定性检查记录，跳过")
        return
    seen, uniq = set(), []
    for rid, rec in rows:
        k = (rec.get("station"), rec.get("spread_s"), rec.get("stability"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append((rid, rec))
    uniq.sort(key=lambda x: (x[1].get("spread_s") or 0))

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10, 3.6),
                                  gridspec_kw={"width_ratios": [2, 1]})
    names = [f"{r.get('station')}\n{rid[:10]}" for rid, r in uniq]
    spread = [r.get("spread_s") or 0 for _, r in uniq]
    cols = ["tab:green" if (r.get("stability") == "stable") else "tab:red"
            for _, r in uniq]
    ax.bar(range(len(uniq)), spread, color=cols)
    ax.set_xticks(range(len(uniq)))
    ax.set_xticklabels(names, fontsize=7, rotation=20, ha="right")
    ax.set_ylabel("网格上到时的散布 (s)")
    ax.set_title("稳定性诊断：同一到时在不同配置下的分歧", fontsize=10)
    for i, (_, r) in enumerate(uniq):
        ax.text(i, spread[i], f" {r.get('stability')}\n n={r.get('n_valid')}/"
                              f"{r.get('grid_size')}", fontsize=6.5, va="bottom")

    cnt = {}
    for _, r in uniq:
        cnt[r.get("stability")] = cnt.get(r.get("stability"), 0) + 1
    ax2.pie(list(cnt.values()), labels=list(cnt.keys()), autopct="%d",
            colors=["tab:green" if k == "stable" else "tab:red" for k in cnt])
    ax2.set_title("判定分布", fontsize=10)
    fig.text(0.005, -0.02,
             "绿色＝stable，红色＝不稳定。稳定性是在有限固定网格上的一致程度，"
             "不是到时正确性的证明。", fontsize=7.5, color="0.3")
    p = OUT / "fig2_stability.png"
    fig.savefig(p)
    plt.close(fig)
    print(f"fig2 → {p.relative_to(ROOT)}（{len(uniq)} 个台站·次）")


def fig3():
    f = ROOT / "delivery/revision-1/G2-RESULTS.json"
    if not f.exists():
        print("fig3：缺少 G2-RESULTS.json，跳过")
        return
    runs = json.loads(f.read_text())["runs"]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.4))
    arms = ["R", "M", "A"]
    colors = {"R": "tab:blue", "M": "tab:orange", "A": "tab:red"}

    for ax, (scope, pre) in zip(axes[:2], (("开发", "D"), ("锁定", "L"))):
        rows = [r for r in runs if r["task_id"].startswith(pre)]
        rates = []
        for a in arms:
            rs = [r for r in rows if r["arm"] == a]
            ok = sum(1 for r in rs if (r.get("evaluation") or {}).get("contract")
                     in ("satisfied", "insufficient_correctly"))
            rates.append(100 * ok / max(1, len(rs)))
        b = ax.bar(arms, rates, color=[colors[a] for a in arms])
        ax.set_ylim(0, 100)
        ax.set_ylabel("合同满足率 (%)")
        ax.set_title(f"{scope}集（{len(rows)} 次运行）", fontsize=10)
        for rect, v in zip(b, rates):
            ax.text(rect.get_x() + rect.get_width() / 2, v + 2, f"{v:.0f}%",
                    ha="center", fontsize=9)
        viol = [sum(1 for r in rows if r["arm"] == a
                    and (r.get("evaluation") or {}).get("certainty_error"))
                for a in arms]
        for i, v in enumerate(viol):
            if v:
                ax.text(i, 4, f"安全违规 {v}", ha="center", fontsize=7,
                        color="white", fontweight="bold")

    ax = axes[2]
    cost = [sum(r.get("model_cost_cny") or 0 for r in runs if r["arm"] == a)
            for a in arms]
    acq = [np.mean([r.get("stations_acquired") or 0 for r in runs if r["arm"] == a])
           for a in arms]
    b = ax.bar(arms, cost, color=[colors[a] for a in arms])
    ax.set_ylabel("模型费用 (CNY)")
    ax.set_title("费用与取数", fontsize=10)
    for rect, c, q in zip(b, cost, acq):
        ax.text(rect.get_x() + rect.get_width() / 2, c, f"{c:.3f}\n取数 {q:.1f} 台",
                ha="center", fontsize=8, va="bottom")
    ax.set_ylim(0, max(cost) * 1.35 if max(cost) else 1)

    axes[0].text(0, -0.30, "结论：未观察到模型优势——R（不调用模型）在两个集上都是最高分。",
                 transform=axes[0].transAxes, fontsize=8.5, color="crimson",
                 fontweight="bold")
    p = OUT / "fig3_comparison.png"
    fig.savefig(p)
    plt.close(fig)
    print(f"fig3 → {p.relative_to(ROOT)}")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    fig1()
    fig2()
    fig3()


if __name__ == "__main__":
    main()