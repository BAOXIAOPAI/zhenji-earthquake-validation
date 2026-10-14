"""Operable local interface for the main line. Loopback only.

Exposes review results and budget state. Never exposes model credentials, and
never accepts a request that did not originate from this page.
"""
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import json
import os
import shutil
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "analysis"))

import core  # noqa: E402
import review
import samples as samplesmod  # noqa: E402
import task_input
import task as taskmod  # noqa: E402
import runlog  # noqa: E402

STATIC = Path(__file__).parent / "static"
RUNS = {}
LAST = {}   # last completed run per (case_id, mode), for instant preview
LOCK = threading.Lock()
ACTIVE = None
EXAMPLE_INITIAL_RUN = "r-e2e8419bf299785d"
EXAMPLE_FEEDBACK_RUN = "r-3ae9c6948c2f8967"
ACCEPTED_EXAMPLE_RUNS = {EXAMPLE_INITIAL_RUN, EXAMPLE_FEEDBACK_RUN}


def model_readiness(cfg=None, ledger=None):
    """Local prerequisites only; never reads a credential or calls a provider."""
    if (os.environ.get("EQ_STRICT_OFFLINE") or "").strip() not in ("", "0"):
        return {"model_available": False,
                "model_unavailable_reason": "当前为离线模式，可查看已有记录；新模型核查需要联网。"}
    key_path = ROOT.parent / "deepseek_model.txt"
    if not key_path.is_file() or not os.access(key_path, os.R_OK) or key_path.stat().st_size == 0:
        return {"model_available": False,
                "model_unavailable_reason": "本机尚未配置可读取的模型密钥文件，请完成配置后启动模型核查。"}
    required = (ROOT / ".venv/bin/python",
                ROOT / "vendor/agnes-harness/node_modules/.bin/tsx",
                ROOT / "vendor/agnes-harness/packages/cli/dist/local/agnes.mjs")
    if not shutil.which("node") or any(not p.is_file() for p in required):
        return {"model_available": False,
                "model_unavailable_reason": "本机模型运行依赖不完整，请按 README 完成 Python、Node 与 AGH 安装。"}
    try:
        cfg = cfg if cfg is not None else json.loads((ROOT / "analysis/model-budget.json").read_text())
        ledger = ledger if ledger is not None else json.loads((ROOT / "runs/model-budget-ledger.json").read_text())
        entries = ledger["entries"] if isinstance(ledger, dict) else ledger
        total = sum(e.get("cost_upper_cny") if e.get("cost_upper_cny") is not None
                    else e.get("reserved_cny") or 0 for e in entries)
        reserve = (cfg["input_token_reservation"] * cfg["input_million_peak"]
                   + cfg["max_output_tokens"] * cfg["output_million_peak"]) / 1e6
        if isinstance(ledger, dict) and ledger.get("halted"):
            reason = "模型费用台账已暂停，请先核对上次异常请求的费用记录。"
        elif total + reserve > cfg.get("guard_limit_cny", cfg["total_limit"]):
            reason = "剩余模型预算不足以预留一次请求，请先调整预算配置。"
        else:
            return {"model_available": True, "model_unavailable_reason": None}
    except (OSError, ValueError, KeyError, TypeError):
        reason = "模型预算配置或费用台账无法读取，请修复配置后重试。"
    return {"model_available": False, "model_unavailable_reason": reason}


def health():
    ledger_path = ROOT / "runs/model-budget-ledger.json"
    try:
        ledger = json.loads(ledger_path.read_text())
        entries = ledger["entries"] if isinstance(ledger, dict) else ledger
        total = sum(e.get("cost_upper_cny") if e.get("cost_upper_cny") is not None
                    else e.get("reserved_cny") or 0 for e in entries)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        ledger, total = None, None
    try:
        cfg = json.loads((ROOT / "analysis/model-budget.json").read_text())
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    # Report the configured request guard separately from compatibility fields.
    guard = cfg.get("guard_limit_cny", cfg.get("total_limit"))
    strict_offline = (os.environ.get("EQ_STRICT_OFFLINE") or "").strip() not in ("", "0")
    readiness = (model_readiness(cfg, ledger) if ledger is not None and cfg else {
        "model_available": False,
        "model_unavailable_reason": "智能核查费用设置或记录不可用，可查看已保存的结果；"
                                    "开启新核查前请先完成费用设置。"})
    return {"status": "ok", "active_run": ACTIVE,
            "cumulative_cost_upper_cny": round(total, 6) if total is not None else None,
            "total_limit_cny": cfg.get("total_limit"),
            "guard_limit_cny": guard,
            "legacy_normal_stop_cny": cfg.get("normal_stop"),
            # Two different offline switches: strict_offline forbids fetching NEW
            # observation material; it does not by itself say whether a model call
            # would be attempted. Keeping them separate stops the page from
            # claiming "no network" when only observation fetching is bounded.
            "strict_offline": strict_offline,
            "observation_offline": strict_offline,
            **readiness,
            "model_preflight_scope": "local_only",
            "budget_note": "模型请求按预算上限预留费用；费用缺失时暂停新请求。",
            "model": "DeepSeek Flash / AGH（仅 agent 模式）",
            "data_source": "EarthScope FDSN（真实公开数据）"}


def work(run_id, case_id, question, mode, user_task=None):
    global ACTIVE
    entry = RUNS[run_id]

    def on_step(event):
        entry["log"].append({**event, "at": time.time()})

    try:
        result = review.run_review(case_id, question, mode, on_step=on_step,
                                   user_task=user_task)
        entry["result"] = result
        # A job that completed is not the same as a review that succeeded. A
        # failed AGH session must surface as a failed review,
        # never as a completed one.
        review_status = result.get("status", "succeeded")
        entry["status"] = "done" if review_status == "succeeded" else "failed"
        entry["review_status"] = review_status
        LAST[(case_id, mode)] = {"log": list(entry["log"]), "result": result,
                                 "status": entry["status"],
                                 "review_status": review_status,
                                 "finished_at": time.time()}
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["review_status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        LAST[(case_id, mode)] = {"log": list(entry["log"]), "result": None,
                                 "status": "error", "error": entry["error"]}
        on_step({"message": "执行失败：" + entry["error"], "phase": "error"})
    finally:
        with LOCK:
            ACTIVE = None


def work_followup(track_id, parent_run_id, change, instruction):
    """One feedback recompute, tracked like any other run and tied to its parent."""
    global ACTIVE
    entry = RUNS[track_id]

    def on_step(event):
        entry["log"].append({**event, "at": time.time()})

    try:
        result = review.run_followup(parent_run_id, change, instruction,
                                     on_step=on_step)
        entry["result"] = result
        entry["status"] = "done"
        entry["review_status"] = result.get("status", "succeeded")
        entry["parent_run_id"] = parent_run_id
        LAST[(result["run_id"], "feedback")] = {
            "log": list(entry["log"]), "result": result, "status": "done",
            "review_status": entry["review_status"], "finished_at": time.time()}
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["review_status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        on_step({"message": "反馈重算失败：" + entry["error"], "phase": "error"})
    finally:
        with LOCK:
            ACTIVE = None


def _agent_followup(parent_run_id, change, instruction):
    """Indirection over the real Agent feedback path.

    A module-level hook, not a direct call, so a test can monkeypatch
    `server._agent_followup` and verify the HTTP wiring without paying for a
    model. The import is lazy because `run_agent_arm` pulls in the driver and
    provider machinery, which the deterministic path must not need.
    """
    import run_agent_arm

    return run_agent_arm.run_followup(parent_run_id, instruction, change=change)


def work_agent_followup(track_id, parent_run_id, change, instruction):
    """One real-Agent feedback turn, tracked like any other run.

    Mirrors `work_followup` exactly: the child run the agent path creates is the
    run record, while the in-memory entry (keyed by the track id the caller
    polls) points at it, so `GET /api/review/<track_id>` works the same way it
    does for the deterministic path. The parent run is never rewritten.
    """
    global ACTIVE
    entry = RUNS[track_id]

    def on_step(event):
        entry["log"].append({**event, "at": time.time()})

    try:
        result = _agent_followup(parent_run_id, change, instruction)
        if not isinstance(result, dict):
            raise TypeError("Agent 反馈未返回运行记录")
        # Disclose which engine actually ran, so the page reads the same keys for
        # both paths and a real Agent round is never mistaken for a recompute.
        result.setdefault("mode", "agent_feedback")
        result.setdefault("executed_by", "agent")
        entry["result"] = result
        review_status = result.get("status", "succeeded")
        entry["status"] = "done" if review_status == "succeeded" else "failed"
        entry["review_status"] = review_status
        entry["parent_run_id"] = parent_run_id
        child_id = result.get("run_id")
        if child_id:
            LAST[(child_id, "feedback")] = {
                "log": list(entry["log"]), "result": result,
                "status": entry["status"],
                "review_status": entry["review_status"], "finished_at": time.time()}
    except Exception as exc:  # noqa: BLE001
        entry["status"] = "error"
        entry["review_status"] = "error"
        entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
        on_step({"message": "反馈重算失败：" + entry["error"], "phase": "error"})
    finally:
        with LOCK:
            ACTIVE = None


def _display_metadata(folder, run_meta, task, outcome, validation, state):
    """Display classifications; never replace the saved execution outcome."""
    metrics = {}
    if not run_meta.get("arm") and (folder / "runtime_metrics.json").exists():
        metrics = json.loads((folder / "runtime_metrics.json").read_text())
    internal = (bool(run_meta.get("internal")) or folder.name == "probe"
                or (run_meta.get("arm") or metrics.get("arm")) == "probe")
    example = (folder.name in ACCEPTED_EXAMPLE_RUNS and not internal
               and not run_meta.get("fixture"))
    source = ("probe" if internal else "fixture" if run_meta.get("fixture")
              else "demo" if example else "real")
    delivered = validation.get("delivered") or {}
    has_observations = bool(state.get("acquired"))
    has_tools = bool(_read_jsonl(folder / "tool_calls.jsonl"))
    rule_result = (json.loads((folder / "verdict.json").read_text())
                   if (folder / "verdict.json").exists() else {})
    has_rule_result = bool(rule_result.get("verdict"))
    has_validated_result = (bool(delivered.get("verdict"))
                            or bool(delivered.get("field")
                                    and delivered.get("field_value") is not None))
    has_delivery = has_validated_result or has_rule_result
    status = outcome.get("status")
    if status and status != "succeeded":
        record_state = "failed"
    elif not (run_meta or task or has_observations or has_tools or has_delivery):
        record_state = "empty"
    elif not status:
        record_state = "unrecorded"
    elif has_validated_result and (isinstance(delivered.get("model_claim_accepted"), bool)
                                  or isinstance(validation.get("agreement"), bool)):
        record_state = "verified"
    elif has_rule_result and not run_meta.get("model"):
        record_state = "completed"
    else:
        record_state = "unverified"
    return {"source_kind": source, "internal": internal,
            "accepted_example": example, "record_state": record_state,
            "has_observations": has_observations, "has_tool_calls": has_tools,
            "has_delivery": has_delivery,
            "model_claim_verified": (has_validated_result
                                     and delivered.get("model_claim_accepted") is True)}


def recent_runs(limit=40):
    """Recent run records, newest first, each with its own status and cost."""
    root = runlog.RUNLOG
    if not root.exists():
        return []
    rows = []
    for folder in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not folder.is_dir():
            continue
        outcome = runlog.read_outcome(folder.name)
        run_meta = {}
        if (folder / "run.json").exists():
            run_meta = json.loads((folder / "run.json").read_text())
        cost = {}
        if (folder / "cost.json").exists():
            cost = json.loads((folder / "cost.json").read_text())
        task = {}
        if (folder / "task.json").exists():
            task = json.loads((folder / "task.json").read_text())
        validation = {}
        if (folder / "validation.json").exists():
            validation = json.loads((folder / "validation.json").read_text())
        state = {}
        if (folder / "mcp_state.json").exists():
            state = json.loads((folder / "mcp_state.json").read_text())
        display = _display_metadata(folder, run_meta, task, outcome or {},
                                    validation, state)
        if display["internal"]:
            continue
        delivered = validation.get("delivered") or {}
        config = {}
        if (folder / "agent_config.json").exists():
            try:
                config = json.loads((folder / "agent_config.json").read_text())
            except ValueError:
                config = {}
        # What the page needs to label a row honestly: the delivered verdict (not
        # the model's wording), whether the model claim was accepted, whether a
        # rule fallback delivered instead, and the real driver/profile.
        rows.append({
            **display,
            "run_id": folder.name,
            "task_id": run_meta.get("task_id"),
            "demo_case_ref": run_meta.get("demo_case_ref"),
            "arm": run_meta.get("arm"),
            "started_at": run_meta.get("started_at"),
            "status": (outcome or {}).get("status", "unrecorded"),
            "failure_kind": (outcome or {}).get("failure_kind"),
            "failure_detail": (outcome or {}).get("failure_detail"),
            "parent_run_id": run_meta.get("parent_run_id"),
            "user_question": task.get("user_question"),
            "follow_up": (task.get("acceptance") or {}).get("follow_up"),
            "delivered_verdict": delivered.get("verdict"),
            "field": delivered.get("field"),
            "field_value": delivered.get("field_value"),
            "answer_kind": delivered.get("answer_kind"),
            "model_claim_accepted": delivered.get("model_claim_accepted"),
            "fallback_used": delivered.get("fallback_used"),
            "origin": delivered.get("origin"),
            "audit_outcome": delivered.get("audit_outcome"),
            "result_determined": delivered.get("result_determined"),
            "condition_id": (validation.get("requested_conditions") or {})
            .get("condition_id"),
            "driver": config.get("driver"),
            "profile": config.get("profile"),
            "analysis_level": config.get("analysis_level"),
            "fixture": bool(run_meta.get("fixture")),
            "model_cost_known": cost.get("model_cost_known"),
            "model_cost_cny": cost.get("model_cost_upper_cny"),
            "http_attempts": cost.get("http_attempts"),
            "stations_acquired": (outcome or {}).get("stations_acquired"),
        })
        if len(rows) >= limit:
            break
    return rows


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def demo_guide():
    """Saved example parameters and scoped evidence; never runs a review."""
    missing = []
    records = {}
    runs = []
    for run_id in (EXAMPLE_INITIAL_RUN, EXAMPLE_FEEDBACK_RUN):
        files = {}
        for name in ("run", "task", "mcp_state", "result", "validation", "cost"):
            path = runlog.run_dir(run_id) / (name + ".json")
            try:
                value = json.loads(path.read_text())
                if not isinstance(value, dict):
                    raise ValueError("运行记录需要 JSON 对象")
                files[name] = value
            except (OSError, ValueError):
                missing.append({"run_id": run_id, "file": name + ".json",
                                "reason": "记录缺失或无法读取"})
        records[run_id] = files
        meta = files.get("run", {})
        validation = files.get("validation", {})
        delivered = validation.get("delivered") or {}
        checks = validation.get("acceptance_checks") or {}
        accepted = (len(files) == 6 and not meta.get("fixture")
                    and not meta.get("internal")
                    and meta.get("run_id") == run_id
                    and (meta.get("arm") or "").startswith("adaptive_agent")
                    and files["result"].get("run_id") == run_id
                    and validation.get("run_id") == run_id
                    and bool(meta.get("task_id"))
                    and meta["task_id"] == files["task"].get("task_id")
                    == validation.get("task_id")
                    and files["result"].get("status") == "succeeded"
                    and delivered.get("model_claim_accepted") is True
                    and delivered.get("task_completed") is True
                    and (validation.get("finish_audit") or {}).get("can_finish") is True
                    and bool(checks) and all(v is True for v in checks.values()))
        if len(files) == 6 and not accepted:
            missing.append({"run_id": run_id, "file": "validation.json",
                            "reason": "该示例目前没有完整的已验收证据"})
        cost = files.get("cost", {})
        amount = cost.get("model_cost_upper_cny")
        cost_known = (cost.get("model_cost_known") is True
                      and type(amount) in (int, float) and amount >= 0)
        runs.append({"run_id": run_id, "parent_run_id": meta.get("parent_run_id"),
                     "status": files.get("result", {}).get("status"),
                     "accepted_example": bool(accepted),
                     "model_cost_known": cost_known,
                     "model_cost_upper_cny": amount if cost_known else None})

    initial = records[EXAMPLE_INITIAL_RUN]
    task = initial.get("task", {})
    stations = sorted({r["network"] + "." + r["station"]
                       for r in initial.get("mcp_state", {}).get("acquired", [])
                       if r.get("network") and r.get("station")
                       and r.get("status") == "analysed"})
    form = {"source_run_id": EXAMPLE_INITIAL_RUN,
            "window_start": task.get("window_start"), "window_end": task.get("window_end"),
            "region_name": "", "stations": stations,
            "target_band_hz": task.get("target_band_hz"),
            "method_goal": task.get("method_goal"), "question": task.get("user_question")}
    parsed = task_input.build(form)
    if not stations or not parsed["ready"] or not task.get("target_band_hz") or not task.get("method_goal"):
        missing.append({"run_id": EXAMPLE_INITIAL_RUN, "file": "task.json / mcp_state.json",
                        "reason": "缺少已取得台站或合法的任务参数"})
    chain = (runs[1]["parent_run_id"] == EXAMPLE_INITIAL_RUN
             and all(r["accepted_example"] for r in runs))
    if runs[1]["accepted_example"] and not chain:
        missing.append({"run_id": EXAMPLE_FEEDBACK_RUN, "file": "run.json",
                        "reason": "反馈记录没有指向已验收首轮"})
    complete = not missing
    if complete:
        form["window_start"] = parsed["structured"]["window_start"] + "Z"
        form["window_end"] = parsed["structured"]["window_end"] + "Z"
    else:
        form = None
    cost_known = complete and all(r["model_cost_known"] for r in runs)
    return {
        "status": "ok" if complete else "incomplete",
        "examples": {"initial_run_id": EXAMPLE_INITIAL_RUN,
                     "feedback_run_id": EXAMPLE_FEEDBACK_RUN},
        "form": form,
        "stats": {"scope": "accepted_examples",
                  "scope_note": "仅统计这两条可回放示例，不代表全项目运行量或成功率。",
                  "run_count": sum(r["accepted_example"] for r in runs) if complete else None,
                  "feedback_chain_count": int(chain) if complete else None,
                  "model_cost_known": cost_known,
                  "model_cost_upper_cny": round(sum(r["model_cost_upper_cny"] for r in runs), 6)
                  if cost_known else None},
        "runs": runs,
        "verification_topics": ["所问结论与公开判据是否一致", "本轮条件和核查范围是否匹配",
                                "引用的证据能否追溯", "停止依据和完成状态是否有实际证据"],
        "missing": missing,
    }


def _iso(unix):
    """A readable UTC stamp plus the raw number, so neither is lost."""
    if unix is None:
        return None
    try:
        from datetime import datetime, timezone
        return {"utc": datetime.fromtimestamp(float(unix), timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "unix": float(unix)}
    except (TypeError, ValueError, OSError):
        return {"utc": None, "unix": None}


def _diagnostic_parameters(state):
    """The diagnostic grid each stability check actually ran, read from the run.

    A requested grid and an executed grid are different facts; this returns the
    executed one (bands_evaluated / detectors) so the page never presents the
    request as the measurement. `full_grid` false means a narrowed grid ran.
    """
    out = []
    for candidate_id, entry in (state.get("stability_checks") or {}).items():
        entry = entry or {}
        effective = entry.get("effective_parameters") or {}
        aggregate = entry.get("aggregate_parameters") or {}
        out.append({
            "candidate_id": candidate_id,
            "station": entry.get("station"),
            "condition_ref": entry.get("condition_ref"),
            "bands_hz": effective.get("bands_hz"),
            "detector_bands": effective.get("detector_bands"),
            "grid_size": entry.get("grid_size") or effective.get("grid_size"),
            "target_band_hz": effective.get("target_band_hz"),
            "stability": entry.get("stability"),
            "spread_s": entry.get("spread_s"),
            "n_valid": entry.get("n_valid"),
            "n_configurations": aggregate.get("n_configurations"),
            "n_configurations_error": aggregate.get("n_configurations_error"),
            "n_configurations_not_applicable":
                aggregate.get("n_configurations_not_applicable"),
            # Three-valued: true / false / null (not checked because components
            # were insufficient). The page must not collapse null into "通过".
            "alignment": aggregate.get("alignment"),
            "reason": entry.get("reason"),
            "statement": entry.get("statement"),
            "next_step": entry.get("next_step"),
            "actor": entry.get("actor"),
            "freshness": entry.get("freshness"),
        })
    return out


def evidence(run_id):
    """Everything behind one run's conclusion, so it can be checked line by line.

    Deliberately keeps the three things apart that used to be merged: what was
    acquired, what the model claimed, and what the validator accepted. The raw and
    derived hashes are both shown because a rewritten waveform must be visible as a
    separate object, not as the same file with a changed digest.
    """
    folder = runlog.run_dir(run_id)
    if not folder.exists():
        return {"error": "未知运行", "run_id": run_id}
    task = json.loads((folder / "task.json").read_text()) if (folder / "task.json").exists() else {}
    state = (json.loads((folder / "mcp_state.json").read_text())
             if (folder / "mcp_state.json").exists() else {})
    cost = (json.loads((folder / "cost.json").read_text())
            if (folder / "cost.json").exists() else {})
    outcome = runlog.read_outcome(run_id) or {}
    response = (json.loads((folder / "model_response.json").read_text())
                if (folder / "model_response.json").exists() else {})
    validation = (json.loads((folder / "validation.json").read_text())
                  if (folder / "validation.json").exists() else {})

    records = []
    for r in state.get("acquired", []):
        channels = r.get("channels") or []
        records.append({
            "station": r.get("station"), "network": r.get("network"),
            "candidate_id": r.get("candidate_id"),
            "channels": channels,
            "status": r.get("status"), "failed_request": r.get("failed_request"),
            "distinction": r.get("distinction"),
            "supplementary_conditions": r.get("supplementary_conditions"),
            "window": {"utc_start": r.get("window_start"), "utc_end": r.get("window_end")},
            "onsets": {k: _iso(v) for k, v in (r.get("onsets") or {}).items()},
            "onset_utc": _iso(r.get("onset_unix")),
            "sampling_rate_hz": r.get("sampling_rate_hz"),
            "target_band_hz": r.get("target_band_hz"),
            "method_band_hz": r.get("method_band_hz"),
            "band_support": r.get("band_support"),
            "channel_integrity_flags": r.get("channel_integrity_flags"),
            "excluded": r.get("excluded"),
            # The onset-grid condition actually in force for this record
            #: a follow-up round can narrow the declared grid,
            # and the record's own executed grid must stay inspectable.
            "stability_bands_hz": r.get("stability_bands_hz"),
            "stability": (r.get("stability") or {}).get("stability")
                         if isinstance(r.get("stability"), dict) else r.get("stability"),
            "source": {"waveform_url": r.get("waveform_url"),
                       "metadata_url": r.get("metadata_url")},
            "hashes": {"raw_waveform_sha256": r.get("raw_waveform_sha256"),
                       "raw_metadata_sha256": r.get("raw_metadata_sha256"),
                       "waveform_sha256": r.get("waveform_sha256"),
                       "derived_waveform_sha256": r.get("derived_waveform_sha256"),
                       "derivation": r.get("derivation")},
            "distance_km": r.get("distance_km"),
        })

    run_meta = (json.loads((folder / "run.json").read_text())
                if (folder / "run.json").exists() else {})
    feedback = (json.loads((folder / "feedback.json").read_text())
                if (folder / "feedback.json").exists() else None)

    def _load(name):
        path = folder / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except ValueError:
            return None

    # Read-only display fields the workbench needs. Every one is read from this
    # run's own saved files; nothing is recomputed and no success field is filled in.
    requested_conditions = (validation.get("requested_conditions")
                            or state.get("requested_conditions"))
    actual_conditions = None
    try:
        import execution_conditions as EC  # noqa: PLC0415
        if requested_conditions:
            actual_conditions = EC.actual_from_state(state,
                                                     requested=requested_conditions)
    except Exception:  # noqa: BLE001
        actual_conditions = None
    finish_audit = (validation.get("finish_audit")
                    or state.get("finish_audit")
                    or _load("finish_audit.json"))
    agent_config = _load("agent_config.json")
    condition_applications = state.get("condition_applications") or []
    resource_cleanup = _load("resource_cleanup.json") or _load("cleanup_status.json")
    native = _load("native_agh_evidence.json") or {}
    diagnostic_parameters = _diagnostic_parameters(state)

    parent_summary = None
    parent_id = run_meta.get("parent_run_id")
    if parent_id:
        parent_folder = runlog.run_dir(parent_id)
        if parent_folder.exists():
            parent_validation = {}
            if (parent_folder / "validation.json").exists():
                parent_validation = json.loads(
                    (parent_folder / "validation.json").read_text())
            parent_state = {}
            if (parent_folder / "mcp_state.json").exists():
                parent_state = json.loads(
                    (parent_folder / "mcp_state.json").read_text())
            parent_task = {}
            if (parent_folder / "task.json").exists():
                parent_task = json.loads(
                    (parent_folder / "task.json").read_text())
            parent_summary = {
                "run_id": parent_id,
                "requested_conditions": (parent_validation.get(
                    "requested_conditions") or parent_state.get(
                        "requested_conditions")),
                "delivered": (parent_validation.get("delivered") or {}),
                "finish_audit": parent_validation.get("finish_audit"),
                "excluded_candidates": [r.get("candidate_id")
                                        for r in (parent_state.get("acquired") or [])
                                        if r.get("excluded")],
                "acquisition": [{k: r.get(k) for k in
                                 ("station", "network", "candidate_id", "excluded")}
                                for r in (parent_state.get("acquired") or [])],
                "user_question": parent_task.get("user_question"),
            }

    return {
        **_display_metadata(folder, run_meta, task, outcome, validation, state),
        "run_id": run_id,
        "parent_run_id": run_meta.get("parent_run_id"),
        "feedback": feedback,
        "task": {"task_id": task.get("task_id"),
                 "user_question": task.get("user_question"),
                 "follow_up": (task.get("acceptance") or {}).get("follow_up"),
                 "window_start": task.get("window_start"),
                 "window_end": task.get("window_end"),
                 "region": task.get("region"),
                 "target_band_hz": task.get("target_band_hz"),
                 "method_band_hz": task.get("method_band_hz"),
                 "scope": task.get("scope"),
                 "task_sha256": task.get("task_sha256"),
                 "prompt_sha256": task.get("prompt_sha256")},
        "scope": {"arm": run_meta.get("arm"),
                  "parent_run_id": run_meta.get("parent_run_id"),
                  "demo_case_ref": run_meta.get("demo_case_ref"),
                  "included_candidates": state.get("included_candidates"),
                  "excluded_candidates": state.get("excluded_candidates")},
        "acquisition": records,
        "tool_calls": _read_jsonl(folder / "tool_calls.jsonl"),
        "stability_checks": state.get("stability_checks"),
        "exclusions": state.get("exclusions"),
        # ---- display-adaptation fields (read-only) -------------------------
        "requested_conditions": requested_conditions,
        "actual_conditions": actual_conditions,
        "finish_audit": finish_audit,
        "agent_config": agent_config,
        "condition_applications": condition_applications,
        "cleanup": resource_cleanup,
        "diagnostic_parameters": diagnostic_parameters,
        "native": {"agh_session_id": native.get("agh_session_id"),
                   "native_tool_pair": native.get("native_tool_pair"),
                   "native_sessions_db": native.get("native_sessions_db"),
                   "native_host_audit": native.get("native_host_audit"),
                   "mcp_tool_pair": native.get("mcp_tool_pair")},
        # A labelled fixture (e.g. a hand-written claim validated by production
        # code to show one UI state) must never be presented as a real AGH run.
        "fixture": bool(run_meta.get("fixture")),
        "fixture_note": run_meta.get("fixture_note"),
        "parent": parent_summary,
        "replay": True,
        "model_claim": (validation.get("model_claim")
                        if validation else
                        {"conclusion": None,
                         "note": "本次运行没有调用模型（确定性模式），因此没有模型主张。"}),
        "validator": ({"delivered": validation.get("delivered"),
                       "agreement": validation.get("agreement"),
                       "acquisition_summary": validation.get("acquisition")}
                      if validation else
                      {"delivered": None, "agreement": None,
                       "note": "确定性模式的判决完全由代码规则给出，不存在需要驳回的模型主张。"}),
        "outcome": {"status": outcome.get("status"),
                    "failure_kind": outcome.get("failure_kind"),
                    "failure_detail": outcome.get("failure_detail")},
        "cost": {"model_cost_known": cost.get("model_cost_known"),
                 "model_cost_upper_cny": cost.get("model_cost_upper_cny"),
                 "http_attempts": cost.get("http_attempts"),
                 "logical_acquisitions": cost.get("logical_acquisitions"),
                 "cache_hits": cost.get("cache_hits")},
    }


def waveform(run_id, candidate_id, buckets=520):
    """One stored waveform from ONE run, as a downsampled min/max envelope.

    Read-only and bounded: the candidate must already belong to this run's own
    acquisition list, so the endpoint can never be used to read an arbitrary path.
    No new data is fetched, no science runs, and the source file is never modified.
    The marker is only the onset this run actually recorded; a computed-but-none
    check is reported as such and no marker is drawn.
    """
    folder = runlog.run_dir(run_id)
    if not folder.exists():
        return {"error": "未知运行，无法读取波形", "code": "run_not_found",
                "reason": "未知运行，无法读取波形", "run_id": run_id}
    state = {}
    if (folder / "mcp_state.json").exists():
        try:
            state = json.loads((folder / "mcp_state.json").read_text())
        except ValueError:
            return {"error": "本次运行的观测记录无法解析",
                    "code": "waveform_read_failed", "reason": "本次运行的观测记录无法解析",
                    "run_id": run_id, "candidate_id": candidate_id}
    acquired = state.get("acquired") or []
    if not acquired:
        return {"error": "本次运行没有观测记录", "code": "no_observations",
                "reason": "本次运行没有观测记录", "run_id": run_id,
                "candidate_id": candidate_id}
    record = next((r for r in acquired
                   if r.get("candidate_id") == candidate_id), None)
    if record is None:
        return {"error": "该候选不在本次运行的观测里", "code": "candidate_not_found",
                "reason": "该候选不在本次运行的观测里", "run_id": run_id,
                "candidate_id": candidate_id}
    path, source = None, None
    for kind in ("derived", "raw"):
        candidate_path = folder / kind / candidate_id / "waveform.mseed"
        if candidate_path.exists():
            path, source = candidate_path, kind
            break
    if path is None:
        return {"error": "本次运行的波形文件缺失", "code": "waveform_file_missing",
                "reason": "观测已记录，但本次运行没有保存该候选的波形文件",
                "run_id": run_id,
                "candidate_id": candidate_id, "station": record.get("station"),
                "source": None}
    try:
        from obspy import read  # noqa: PLC0415
        stream = read(str(path), format="MSEED")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"波形读取失败：{type(exc).__name__}",
                "code": "waveform_read_failed", "reason": "波形文件无法解析，请检查数据文件",
                "run_id": run_id,
                "candidate_id": candidate_id, "station": record.get("station")}
    channels = {}
    for component in ("Z", "N", "E"):
        selected = sorted((trace for trace in stream.select(component=component)
                           if len(trace.data)), key=lambda trace: trace.stats.starttime)
        if not selected:
            continue
        start = selected[0].stats.starttime
        n_samples = sum(len(trace.data) for trace in selected)
        rates = sorted({float(trace.stats.sampling_rate) for trace in selected})
        step = max(1, n_samples // max(1, int(buckets)))
        envelope = []
        duration = 0.0
        total = 0.0
        for trace in selected:
            data = trace.data
            rate = float(trace.stats.sampling_rate)
            offset = float(trace.stats.starttime - start)
            duration = max(duration, offset + len(data) / rate)
            total += float(data.sum(dtype="float64"))
            # Downsample each segment independently so gaps retain their real
            # time offset and no min/max bucket spans missing observations.
            for i in range(0, len(data), step):
                chunk = data[i:i + step]
                envelope.append([round(offset + i / rate, 3),
                                 round(float(chunk.min()), 4),
                                 round(float(chunk.max()), 4)])
        envelope.sort(key=lambda row: row[0])
        channels[component] = {
            "sampling_rate_hz": rates[0] if len(rates) == 1 else None,
            "sampling_rates_hz": rates,
            "start_utc": start.isoformat(),
            "duration_seconds": round(duration, 6),
            "n_samples": int(n_samples),
            "display_center": round(total / n_samples, 4),
            "envelope": envelope,
        }
    if not channels:
        return {"error": "波形文件没有可显示的 Z/N/E 分量",
                "code": "waveform_no_components", "reason": "波形文件没有可显示的 Z/N/E 分量",
                "run_id": run_id, "candidate_id": candidate_id}
    onset = record.get("onset_utc")
    try:
        onset_seconds = float(onset) if onset is not None else None
    except (TypeError, ValueError):
        onset_seconds = None
    # An absolute Unix onset is converted to seconds into the record window.
    window_start = record.get("window_start")
    if onset_seconds is not None and onset_seconds > 1e6 and window_start:
        try:
            from datetime import datetime, timezone  # noqa: PLC0415
            base = datetime.fromisoformat(
                str(window_start).replace("Z", "+00:00")).replace(
                    tzinfo=timezone.utc).timestamp()
            onset_seconds = round(onset_seconds - base, 3)
        except (TypeError, ValueError):
            pass
    return {
        "run_id": run_id, "candidate_id": candidate_id,
        "station": record.get("station"), "network": record.get("network"),
        "distance_km": record.get("distance_km"),
        "source": source,
        "source_label": ("派生副本（人工注入或改写，已披露）" if source == "derived"
                         else "原始下载波形"),
        "derived": source == "derived",
        "excluded": bool(record.get("excluded")),
        "status": record.get("status"),
        "window": {"start": record.get("window_start"),
                   "end": record.get("window_end")},
        "onset": {"computed": record.get("onset_computed"),
                  "source": record.get("onset_source"),
                  "seconds": onset_seconds,
                  "band_hz": record.get("onset_band_hz")
                  or record.get("method_band_hz"),
                  "note": ("本次到时检查已执行，未找到满足判据的触发，因此不画到时标记"
                           if record.get("onset_computed") and onset_seconds is None
                           else None)},
        "hashes": {"raw_waveform_sha256": record.get("raw_waveform_sha256"),
                   "derived_waveform_sha256": record.get("derived_waveform_sha256")},
        "channel_integrity_flags": record.get("channel_integrity_flags") or [],
        "normalization": "各分量独立归一化，仅用于观察形态，不作跨站幅值比较",
        "display_center_note": "分量均值仅用于显示居中，原始包络和波形文件未改写",
        "channels": channels,
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, ctype="application/json; charset=utf-8"):
        data = (json.dumps(body, ensure_ascii=False).encode()
                if ctype.startswith("application/json")
                else body.encode() if isinstance(body, str) else body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise PermissionError("Local host required")
        origin = self.headers.get("Origin")
        if origin and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            raise PermissionError("Cross-origin request denied")

    def do_GET(self):
        try:
            self.local()
            path = urlparse(self.path).path
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path in assets:
                name, ctype = assets[path]
                return self.send(200, (STATIC / name).read_bytes(), ctype)
            if path == "/api/health":
                return self.send(200, health())
            if path == "/api/modes":
                return self.send(200, {"modes": [{"id": k, "label": v}
                                                 for k, v in review.MODES.items()]})
            if path == "/api/cases":
                return self.send(200, {"cases": [
                    {"case_id": c["case_id"], "kind": c["kind"], "split": c["split"],
                     "question": core.QUESTIONS[c["question_id"]],
                     "question_id": c["question_id"], "injected": bool(c.get("injected")),
                     "expect": c.get("expect")}
                    for c in core.CASES]})
            if path.startswith("/api/review/"):
                run_id = path.rsplit("/", 1)[-1]
                entry = RUNS.get(run_id)
                if entry is None:
                    return self.send(404, {"error": "未知运行"})
                return self.send(200, {"status": entry["status"], "log": entry["log"],
                                       "result": entry.get("result"),
                                       "review_status": entry.get("review_status"),
                                       "from_user_task": entry.get("from_user_task"),
                                       "error": entry.get("error")})
            if path == "/api/runs":
                query = parse_qs(urlparse(self.path).query)
                try:
                    limit = max(1, min(200, int(query.get("limit", ["40"])[0])))
                except ValueError:
                    return self.send(400, {"error": "运行记录数量必须为整数"})
                return self.send(200, {"runs": recent_runs(limit=limit)})
            if path == "/api/preview":
                query = parse_qs(urlparse(self.path).query)
                key = (query.get("case_id", [""])[0], query.get("mode", ["question"])[0])
                found = LAST.get(key)
                if found is None:
                    return self.send(404, {"error": "该组合尚无已完成记录"})
                return self.send(200, {
                    **found,
                    "is_live_current": found.get("status") == "done"
                                      and (found.get("result") or {}).get("is_current_run"),
                    "label": "本次运行" if found.get("status") == "done"
                             else "本次运行失败"})
            if path == "/api/goals":
                return self.send(200, {"goals": [
                    {"id": k, "label": v} for k, v in task_input.SUPPORTED_GOALS.items()]})
            if path == "/api/samples":
                return self.send(200, {"samples": samplesmod.samples()})
            if path == "/api/demo-guide":
                guide = demo_guide()
                return self.send(200 if guide["status"] == "ok" else 503, guide)
            if path == "/api/regions":
                return self.send(200, {"regions": [
                    {"name": k, **v} for k, v in sorted(task_input.NAMED_REGIONS.items())]})
            if path.startswith("/api/evidence/"):
                run_id = path.rsplit("/", 1)[-1]
                return self.send(200, evidence(run_id))
            if path.startswith("/api/waveform/"):
                parts = [p for p in path.split("/") if p]
                if len(parts) != 4:
                    return self.send(400, {"error": "波形接口需要 run 与候选编号"})
                return self.send(200, waveform(parts[2], parts[3]))
            self.send(404, {"error": "Not found"})
        except PermissionError as exc:
            self.send(403, {"error": str(exc)})
        except Exception:  # noqa: BLE001
            self.send(500, {"error": "读取失败"})

    def followup(self, body):
        """The user's feedback turn: a new run that reuses the parent's observations.

        Which engine runs is decided by the PARENT, not by a silent default: a
        parent that was a real Agent run (`arm` starts with `adaptive_agent`)
        must go back through the real Agent path, never a silent deterministic
        recompute. A deterministic parent keeps the old recompute path. The
        caller can override explicitly with `"engine": "agent" | "rule"`; asking
        for `"agent"` on a non-Agent parent is a clear 400 rather than a
        surprise model spend. Either way the child is a NEW run with
        `parent_run_id` set; the parent's record is never rewritten.
        """
        global ACTIVE
        parent_run_id = (body.get("parent_run_id") or "").strip()
        if not parent_run_id:
            return self.send(400, {"error": "缺少上一轮运行的编号"})
        folder = runlog.run_dir(parent_run_id)
        if not (folder / "task.json").exists():
            return self.send(404, {"error": f"未知的上一轮运行：{parent_run_id}"})
        # The parent's own run record says how it was executed. Only a real Agent
        # arm counts; `one_shot_model` is a model arm but not the adaptive Agent
        # feedback path, so it is deliberately not treated as an Agent parent.
        run_meta = (json.loads((folder / "run.json").read_text())
                    if (folder / "run.json").exists() else {})
        parent_is_agent = str(run_meta.get("arm") or "").startswith("adaptive_agent")
        engine = body.get("engine")
        if engine is not None and engine not in ("agent", "rule"):
            return self.send(400, {"error": "engine 只能是 \"agent\" 或 \"rule\""})
        if engine == "agent" and not parent_is_agent:
            return self.send(400, {
                "error": "上一轮不是真实 Agent 运行，不能走 Agent 反馈；"
                         "请改用 engine=\"rule\" 做确定性重算。"})
        use_agent = (engine == "agent") or (engine is None and parent_is_agent)
        mode = "agent_feedback" if use_agent else "feedback"
        executed_by = "agent" if use_agent else "deterministic"
        state = (json.loads((folder / "mcp_state.json").read_text())
                 if (folder / "mcp_state.json").exists() else {})
        if not state.get("acquired"):
            return self.send(400, {"error": "上一轮没有取得任何观测，无法在不新增请求"
                                            "的前提下重算"})
        change = body.get("change")
        instruction = (body.get("instruction") or "").strip() or None
        if not change and not instruction:
            return self.send(400, {"error": "请给出要改的条件（排除台站或新的目标频带）"})
        # Acquisition permission is a structured fact, not prose. Absent means the
        # reuse path's default: reuse the stored observations, fetch nothing new.
        permission = body.get("acquisition_allowed")
        if permission is None and isinstance(change, dict):
            permission = change.get("acquisition_allowed",
                                    change.get("allow_new_acquisition"))
        candidate_change = dict(change) if isinstance(change, dict) else change
        if isinstance(candidate_change, dict) and permission is not None:
            candidate_change["acquisition_allowed"] = bool(permission)
        try:
            # Validate AND normalise before spending a thread. The normalised object
            # is what the worker executes: validating one shape and executing another
            # is how the "don't fetch" condition was lost.
            change = review.feedback_change(state, candidate_change, instruction)
        except ValueError as exc:
            return self.send(400, {"error": str(exc)[:180]})
        if permission is not None:
            change["acquisition_allowed"] = bool(permission)
        effective_permission = bool(change.get("acquisition_allowed", False))
        if use_agent:
            readiness = model_readiness()
            if not readiness["model_available"]:
                return self.send(503, {"error": readiness["model_unavailable_reason"],
                                       **readiness})
        with LOCK:
            if ACTIVE:
                return self.send(409, {"error": "已有核查在执行中，请等待完成。",
                                       "active_run": ACTIVE})
            track_id = uuid.uuid4().hex
            RUNS[track_id] = {"status": "running", "log": [], "result": None,
                              "mode": mode, "parent_run_id": parent_run_id,
                              "from_user_task": True, "started_at": time.time()}
            ACTIVE = track_id
        worker = work_agent_followup if use_agent else work_followup
        threading.Thread(target=worker,
                         args=(track_id, parent_run_id, change, instruction),
                         daemon=True).start()
        return self.send(202, {"run_id": track_id, "mode": mode,
                               "parent_run_id": parent_run_id,
                               "executed_by": executed_by,
                               # The permission the run actually executes under, so the
                               # page never has to infer it from the user's wording.
                               "acquisition_allowed": effective_permission,
                               "change": change})

    def do_POST(self):
        global ACTIVE
        try:
            self.local()
            if self.headers.get("X-EQ-Request") != "1":
                raise PermissionError("Request header required")
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("Invalid body size")
            body = json.loads(self.rfile.read(length))
            route = urlparse(self.path).path
            if route == "/api/task":
                # Parse only. Nothing is fetched and no model is called, so the user
                # can see what was understood before anything is spent.
                return self.send(200, task_input.build(body))
            if route == "/api/followup":
                return self.followup(body)
            if route != "/api/review":
                return self.send(404, {"error": "Not found"})
            mode = body.get("mode", "question")
            if mode not in review.MODES:
                raise ValueError("未知模式")
            if mode == "agent":
                readiness = model_readiness()
                if not readiness["model_available"]:
                    return self.send(503, {"error": readiness["model_unavailable_reason"],
                                           **readiness})
            user_task = None
            if body.get("task"):
                # A request the user typed. The window and region come from them;
                # a missing window raises a clarification instead of a preset event.
                parsed = task_input.build(body["task"])
                if not parsed.get("ready"):
                    return self.send(400, {"error": "任务信息不完整，请先补齐",
                                           "missing": parsed.get("missing") or []})
                exactly = parsed["structured"].get("user_question") or parsed.get("echo")
                question = body["task"].get("question") or exactly or ""
                if not question:
                    raise ValueError("用户任务缺少问题描述")
                user_task = taskmod.task_from_spec(
                    parsed["structured"], user_question=question,
                    demo_injection=bool(body["task"].get("inject_demo")))
                taskmod.freeze(user_task)
                case_id = taskmod.case_for_task(user_task)["case_id"]
            else:
                case_id = body.get("case_id")
                question = (body.get("question") or "")[:500]
                core.case_by_id(case_id)
            with LOCK:
                if ACTIVE:
                    return self.send(409, {"error": "已有核查在执行中，请等待完成。",
                                           "active_run": ACTIVE})
                run_id = uuid.uuid4().hex
                RUNS[run_id] = {"status": "running", "log": [], "result": None,
                                "mode": mode, "case_id": case_id,
                                "from_user_task": user_task is not None,
                                "started_at": time.time()}
                ACTIVE = run_id
            threading.Thread(target=work,
                             args=(run_id, case_id, question, mode, user_task),
                             daemon=True).start()
            return self.send(202, {"run_id": run_id, "mode": mode, "case_id": case_id,
                                   "from_user_task": user_task is not None})
        except PermissionError as exc:
            self.send(403, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            self.send(400, {"error": str(exc)[:180]})
        except Exception:  # noqa: BLE001
            self.send(500, {"error": "执行失败"})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"震迹核查台 http://127.0.0.1:{port}", flush=True)
    server.serve_forever()
