"""The round's runner: plan → gates → smoke → dev → freeze → validate → rescore → report.

Every subcommand takes `--protocol main/agent_optimization_protocol.json` and
`--output delivery/agent-optimization-2026-10-11`. Money is guarded by the original
20 CNY authorisation read live from `runs/model-budget-ledger.json`; this runner
never raises the cap, never deletes ledger entries and never lifts `halted`.

The runner deliberately does not inherit `main/run_c.py`'s revision-5 write
defaults: every path is resolved against the round's own output directory.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))
sys.path.insert(0, str(ROOT / "explore/probes"))

import core  # noqa: E402
import c_scorer  # noqa: E402
import prepare_agent  # noqa: E402

DEFAULT_PROTOCOL = ROOT / "main/agent_optimization_protocol.json"
DEFAULT_OUTPUT = ROOT / "delivery/agent-optimization-2026-10-11"
LEDGER = ROOT / "runs/model-budget-ledger.json"
BUDGET_CFG = ROOT / "prototype/model-budget.json"

# The originals the freeze must contain, not only their digests.
SNAPSHOT_SOURCES = (
    "main/execution_conditions.py", "main/evidence_state.py", "main/feedback.py",
    "main/finish_audit.py", "main/resource_limits.py", "main/mcp_launcher.py",
    "main/mcp_server.py", "main/core.py", "main/diagnostics.py", "main/task.py",
    "main/prepare_agent.py", "main/review.py", "main/run_agent_arm.py",
    "main/provider_bridge.mjs", "main/agent_run_optimization.mjs",
    "main/agent_run.mjs", "main/one_shot.mjs", "main/permission.mjs",
    "main/tools.json", "main/prompts/agent_policy_v0.txt",
    "main/prompts/agent_policy_v1.txt", "main/c_scorer.py",
    "main/agent_optimization_gates.py", "main/run_agent_optimization.py",
    "prototype/model-budget.json",
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    return path


def _load(path: Path):
    return json.loads(Path(path).read_text())


def budget_state(protocol) -> dict:
    """Live ledger state. Every non-settled reservation is counted, including
    abandoned entries; `halted` is reported and never cleared here."""
    cap = float(protocol.get("budget", {}).get("cumulative_authorization_cny", 20.0))
    if not LEDGER.exists():
        return {"known": False, "cap": cap, "halted": None,
                "reason": "ledger_missing"}
    ledger = _load(LEDGER)
    entries = ledger.get("entries", [])
    total = sum(e.get("cost_upper_cny") or e.get("reserved_cny") or 0.0
                for e in entries)
    cfg = _load(BUDGET_CFG)
    reserve = ((cfg["input_token_reservation"] * cfg["input_million_peak"]
                + cfg["max_output_tokens"] * cfg["output_million_peak"]) / 1e6)
    return {"known": True, "cap": cap, "halted": bool(ledger.get("halted")),
            "entries": len(entries),
            "settled_upper_cny": round(sum(e.get("cost_upper_cny") or 0.0
                                           for e in entries
                                           if e.get("status") == "settled"), 6),
            "total_upper_cny": round(total, 6),
            "remaining_cny": round(cap - total, 6),
            "per_request_reserve_cny": round(reserve, 6),
            "can_reserve_one": (cap - total) > reserve,
            "updated_at": _now()}


def _require_budget(protocol, output, *, stage):
    state = budget_state(protocol)
    if not state.get("known"):
        raise SystemExit(f"refusing to run: {state.get('reason')}")
    if state["halted"]:
        raise SystemExit("refusing to run: the ledger is halted (needs human review)")
    if not state["can_reserve_one"]:
        raise SystemExit(
            f"refusing to run: remaining {state['remaining_cny']} CNY cannot cover one "
            f"reserved request ({state['per_request_reserve_cny']} CNY)")
    stage_target = float(protocol.get("budget", {})
                         .get("stage_cost_pause_cny", {}).get(stage, 0) or 0)
    _write(Path(output) / "results" / f"budget-before-{stage}.json",
           {**state, "stage": stage, "stage_pause_target_cny": stage_target})
    return state


def _stage_reached(protocol, output, *, stage, baseline_total) -> dict:
    state = budget_state(protocol)
    delta = (state.get("total_upper_cny") or 0) - baseline_total
    target = float(protocol.get("budget", {})
                   .get("stage_cost_pause_cny", {}).get(stage, 0) or 0)
    state.update({"stage": stage, "stage_baseline_total_cny": baseline_total,
                  "stage_settled_delta_cny": round(delta, 6),
                  "stage_pause_target_cny": target,
                  "stage_pause_reached": bool(target and delta >= target)})
    _write(Path(output) / "results" / f"budget-after-{stage}.json", state)
    return state


# ------------------------------------------------------------------ task build
def _window(case):
    return core.window_of(case)


def freeze_spec(spec, *, source):
    """Freeze one protocol task spec into a write-once task record."""
    import task as taskmod
    case = core.case_by_id(spec["case_id"])
    window = _window(case)
    frozen = taskmod.make_task(
        user_question=spec["question"], window_start=window["start"],
        window_end=window["end"], region=case["region"],
        target_band_hz=spec.get("target_band_hz") or [1.0, 12.0],
        method_goal=spec.get("method_goal") or "three_component_pick",
        operational_standard=spec.get("operational_standard"),
        demo_case_ref=spec["case_id"], question_id=spec.get("question_id"),
        scope=spec.get("scope"),
        diagnostic_bands_hz=spec.get("diagnostic_bands_hz"),
        acquisition_allowed=spec.get("acquisition_allowed", True),
        max_unique_candidates_episode=spec.get("max_unique_candidates_episode", 6),
        acceptance={"source": source, "protocol_task": spec["id"],
                    "title": spec.get("title")})
    taskmod.freeze(frozen)
    return frozen


def _resolve_feedback_change(parent_run_id, fb):
    """Resolve a runtime feedback target (e.g. 'the first acquired station')."""
    import runlog
    change = copy.deepcopy(fb.get("change") or {})
    # The feedback's own permission flag rides on the change so the CHILD's frozen
    # conditions carry it. Without this the prompt said "do not fetch" while the
    # contract still allowed acquisition — the C2 review's contract mismatch.
    if fb.get("acquisition_allowed") is not None:
        change["acquisition_allowed"] = bool(fb["acquisition_allowed"])
    if change.get("kind") == "exclude" and not change.get("candidate_id") \
            and not change.get("station"):
        state = _load(runlog.run_dir(parent_run_id) / "mcp_state.json")
        acquired = [r for r in (state.get("acquired") or []) if not r.get("excluded")]
        if not acquired:
            raise ValueError("no acquired observation to exclude")
        change["candidate_id"] = acquired[-1]["candidate_id"]
        change["station"] = acquired[-1].get("station")
    return change


# -------------------------------------------------------------------- one run
def run_arm(spec, arm, profile, *, output, stage, batch_id, parent_run_id=None,
            fb=None, repeat=1):
    import run_agent_arm
    import run_c
    import task as taskmod
    import runlog
    import review

    case_id = spec["case_id"]
    question = spec["question"]
    question_id = spec.get("question_id") or core.classify_question(question)
    change = _resolve_feedback_change(parent_run_id, fb) if fb else None
    if parent_run_id:
        if arm == "A":
            row = run_agent_arm.run_followup(parent_run_id, fb["instruction"],
                                             change=change, profile=profile)
        elif arm == "M":
            row = run_c._m_followup(parent_run_id, fb["instruction"], change=change)
        else:
            row = review.run_followup(parent_run_id, change=change,
                                      instruction=fb["instruction"])
        row = {**row, "arm": arm, "batch": f"{batch_id}-followup",
               "feedback_variant": fb.get("id") or "followup",
               "parent_run_id": parent_run_id, "repeat": repeat}
        run_id = row.get("run_id")
    else:
        frozen = freeze_spec(spec, source=batch_id)
        if arm == "A":
            prepared = prepare_agent.prepare_task(
                frozen, arm=f"adaptive_agent_{profile.lower()}",
                analysis_level="quality_only")
            row = run_agent_arm._execute(
                case_id, prepared, question_id, question,
                driver_script="agent_run_optimization.mjs", profile=profile)
        elif arm == "M":
            row = run_agent_arm.run_one_shot_task(frozen, question=question)
        else:
            row = review.run_review(case_id, question, "full", user_task=frozen)
        row = {**row, "arm": arm, "batch": batch_id, "repeat": repeat,
               "task_id": row.get("task_id") or frozen["task_id"]}
        run_id = row.get("run_id")
    # Score every row through the one normalizer + scorer, and keep the canonical
    # input next to the score so it can be recomputed exactly.
    acquisition_allowed = (fb or {}).get("acquisition_allowed",
                                         spec.get("acquisition_allowed", True))
    raw = run_c._raw_row(arm, row)
    raw.update({"acquisition_allowed": acquisition_allowed,
           "method_goal": spec.get("method_goal"),
           "target_band_hz": spec.get("target_band_hz"),
           "question_id": question_id,
               "protocol_task": spec["id"],
               "profile": profile if arm == "A" else None, "repeat": repeat})
    if run_id:
        state_path = runlog.run_dir(run_id) / "mcp_state.json"
        if state_path.exists():
            state = _load(state_path)
            conditions = state.get("requested_conditions") or {}
            raw["requested_conditions"] = conditions
            raw["actual_conditions"] = __import__(
                "execution_conditions").actual_from_state(state, requested=conditions)
            raw["evidence_validity"] = __import__("evidence_state").validity(
                state, conditions)
            raw["condition_applications"] = state.get("condition_applications") or []
            raw["contract_violations"] = state.get("contract_events") or []
            raw["finish_audit"] = state.get("finish_audit")
    norm = c_scorer.normalize_row(raw)
    scored = c_scorer.score_row(norm)
    return {"run_id": run_id, "arm": arm, "profile": profile if arm == "A" else None,
            "parent_run_id": parent_run_id, "protocol_task": spec["id"],
            "repeat": repeat, "phase": "feedback" if fb else "first",
            "canonical_input": raw, "normalized": norm, "score": scored,
            "score_version_sha256": _sha(ROOT / "main/c_scorer.py")}


def _append(rows_path: Path, rows):
    existing = _load(rows_path) if rows_path.exists() else {"schema":
        "agent-optimization-rows/v1", "rows": []}
    existing["rows"].extend(rows)
    existing["updated_at"] = _now()
    _write(rows_path, existing)


# --------------------------------------------------------------------- commands
def cmd_plan(args):
    protocol = _load(args.protocol)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    import agent_optimization_preserve as preserve
    if not (output / preserve.MANIFEST_NAME).exists():
        preserve.snapshot(output)
    budget = budget_state(protocol)
    tasks = protocol["development"]["tasks"]
    val = protocol["validation"]["tasks"]
    plan = {
        "schema": "agent-optimization-plan/v1", "generated_at": _now(),
        "protocol": str(Path(args.protocol).relative_to(ROOT)),
        "protocol_sha256": _sha(Path(args.protocol)),
        "output": str(output.relative_to(ROOT)),
        "budget": budget,
        "profiles": protocol["profiles"],
        "planned_scale": {
            "development_sessions": len(tasks) * 3 * 2,
            "development_sessions_without_AT": len(tasks) * 2 * 2,
            "validation_sessions": len(val) * 3 * 2,
            "validation_sessions_without_AT": len(val) * 2 * 2,
            "smoke_sessions": 1,
            "initial_paid_ceiling": 43,
        },
        "stage_cost_pause_cny": protocol["budget"]["stage_cost_pause_cny"],
        "stop_conditions": [
            "预算不足、台账 halted 或有未解释结算异常",
            "严重合同违规或当前条件无法追溯",
            "AGH 原生事件与项目工具日志不一致",
            "执行版本/材料不等于冻结原件",
            "连续两次接口失败或资源未清理且原因未查明",
            "冻结后需要修改策略、代码、合同或评分",
            "到达本轮时间上限",
        ],
        "history_preservation": preserve.verify(output),
        "resource_limits": __import__("resource_limits").limits(),
    }
    _write(output / "results" / "plan.json", plan)
    _write(output / "contracts" / "development-tasks.json",
           {"batch_id": protocol["development"]["batch_id"],
            "repeats": protocol["development"]["repeats"], "tasks": tasks})
    _write(output / "contracts" / "validation-tasks.json",
           {"batch_id": protocol["validation"]["batch_id"],
            "repeats": protocol["validation"]["repeats"],
            "selection_rule": protocol["validation"]["selection_rule"], "tasks": val})
    print(json.dumps({"budget": budget, "planned_scale": plan["planned_scale"],
                      "history_unchanged": plan["history_preservation"]["unchanged"]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_gates(args):
    import agent_optimization_gates as gates
    output = Path(args.output)
    report = gates.run_all(output, include_g15=True)
    # The isolated regression of the existing suite, with outputs kept in this round.
    import subprocess
    log = output / "gates" / "pytest-isolated.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q", "tests"],
                          cwd=ROOT, capture_output=True, text=True, timeout=1800)
    log.write_text(proc.stdout + "\n" + proc.stderr)
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    summary = {"returncode": proc.returncode, "summary_line": tail}
    _write(output / "gates" / "regression.json", summary)
    print(json.dumps({"all_required_pass": report["all_required_pass"],
                      "g15_pass": report["g15_pass"], "regression": summary},
                     ensure_ascii=False, indent=2))
    return 0 if report["all_required_pass"] and proc.returncode == 0 else 1


def cmd_smoke(args):
    import run_agent_arm
    protocol = _load(args.protocol)
    output = Path(args.output)
    gates_path = output / "gates" / "free-gates.json"
    if not gates_path.exists() or not _load(gates_path).get("g15_pass"):
        raise SystemExit("refusing smoke: G15 has not passed")
    _require_budget(protocol, output, stage="smoke_web_fix")
    profile = args.profile or "AT"
    spec = {"id": "SMOKE", "case_id": "E1_orcas_event",
            "question": "这个候选异常是不是多个台站共有的真实传播信号？",
            "question_id": "Q_propagating", "method_goal": "three_component_pick",
            "target_band_hz": [1.0, 12.0], "scope": {"scope_kind": "all_orcas"}}
    case = core.case_by_id(spec["case_id"])
    window = _window(case)
    import task as taskmod
    frozen = taskmod.make_task(
        user_question=spec["question"], window_start=window["start"],
        window_end=window["end"], region=case["region"],
        operational_standard="三站/三秒（公开标准）", demo_case_ref=spec["case_id"],
        question_id="Q_propagating", scope={"scope_kind": "all_allowed"},
        acceptance={"source": "smoke"})
    taskmod.freeze(frozen)
    prepared = prepare_agent.prepare_task(frozen, arm=f"adaptive_agent_{profile.lower()}",
                                          analysis_level="quality_only")
    row = run_agent_arm._execute(spec["case_id"], prepared, "Q_propagating",
                                 spec["question"],
                                 driver_script="agent_run_optimization.mjs",
                                 profile=profile)
    record = {"schema": "agent-optimization-smoke/v1", "profile": profile,
              "run_id": row.get("run_id"), "status": row.get("status"),
              "failure_kind": row.get("failure_kind"), "row": row,
              "not_a_performance_result": True, "at": _now()}
    _write(output / "evidence" / "smoke.json", record)
    print(json.dumps({"run_id": record["run_id"], "status": record["status"],
                      "failure_kind": record["failure_kind"]}, ensure_ascii=False))
    return 0


def _profiles_for_dev(protocol, args):
    wanted = [p.strip().upper() for p in (args.profiles or "").split(",") if p.strip()]
    available = list(protocol["profiles"].keys())
    if not wanted:
        return available
    unknown = [p for p in wanted if p not in available]
    if unknown:
        raise SystemExit(f"unknown profiles: {unknown}")
    return wanted


def cmd_dev_feedback_repair(args, protocol, output, profiles):
    """Re-run ONLY the feedback halves of the development matrix.

    Used for the one allowed repair cycle: the first rounds used the profile driver
    correctly, but the feedback turn still went through the old inline driver, so
    A1's policy silently reverted on the second turn. Re-running the affected
    sessions is the minimal repair; the original rows are kept untouched.
    """
    rows_path = output / "development" / "rows.json"
    if not rows_path.exists():
        raise SystemExit("feedback repair needs the original development rows")
    original = _load(rows_path)["rows"]
    tasks = {t["id"]: t for t in protocol["development"]["tasks"]}
    baseline = budget_state(protocol)
    _require_budget(protocol, output, stage="development")
    repair_path = output / "development" / "rows-repair.json"
    stopped = None
    n = 0
    for row in original:
        if stopped:
            break
        if row.get("phase") != "first" or row.get("arm") != "A":
            continue
        if row.get("profile") not in profiles:
            continue
        task = tasks.get(row.get("protocol_task"))
        if not task or not task.get("feedback") or not row.get("run_id"):
            continue
        fb = {**task["feedback"], "id": f"{task['id']}-fb"}
        repaired = run_arm(task, "A", row["profile"], output=output,
                           stage="development",
                           batch_id=protocol["development"]["batch_id"] + "-repair",
                           parent_run_id=row["run_id"], fb=fb,
                           repeat=row.get("repeat") or 1)
        _append(repair_path, [repaired])
        n += 1
        after = _stage_reached(protocol, output, stage="development",
                               baseline_total=baseline["total_upper_cny"])
        if not after["can_reserve_one"]:
            stopped = {"reason": "insufficient remaining budget", "budget": after}
    _write(output / "development" / "feedback-repair-summary.json",
           {"schema": "agent-optimization-dev-repair/v1", "n_rows": n,
            "profiles": profiles, "stopped": stopped, "finished_at": _now(),
            "why": "反馈轮原先仍走 agent_run.mjs 的内联提示，A1 策略在第二轮回落；"
                   "本修复只重跑受影响的反馈会话"})
    print(json.dumps({"n_rows": n, "stopped": stopped,
                      "budget": budget_state(protocol)}, ensure_ascii=False, indent=2))
    return 0


def cmd_dev(args):
    protocol = _load(args.protocol)
    output = Path(args.output)
    if getattr(args, "feedback_only", False):
        return cmd_dev_feedback_repair(args, protocol, output,
                                       _profiles_for_dev(protocol, args))
    rows_path = output / "development" / "rows.json"
    baseline = budget_state(protocol)
    state = _require_budget(protocol, output, stage="development")
    profiles = _profiles_for_dev(protocol, args)
    tasks = protocol["development"]["tasks"]
    if args.only:
        wanted = {t.strip().upper() for t in args.only.split(",")}
        tasks = [t for t in tasks if t["id"].upper() in wanted]
    repeats = int(protocol["development"]["repeats"])
    batch = protocol["development"]["batch_id"]
    rows = []
    stopped = None
    for task in tasks:
        for repeat in range(1, repeats + 1):
            for profile in profiles:
                if stopped:
                    break
                first = run_arm(task, "A", profile, output=output, stage="development",
                                batch_id=batch, repeat=repeat)
                rows.append(first)
                fb = task.get("feedback")
                if fb and first.get("run_id"):
                    fb = {**fb, "id": f"{task['id']}-fb"}
                    rows.append(run_arm(task, "A", profile, output=output,
                                        stage="development", batch_id=batch,
                                        parent_run_id=first["run_id"], fb=fb,
                                        repeat=repeat))
                _append(rows_path, rows[-2:] if fb else rows[-1:])
                after = _stage_reached(protocol, output, stage="development",
                                       baseline_total=baseline["total_upper_cny"])
                if after["stage_pause_reached"] or not after["can_reserve_one"]:
                    stopped = {"reason": ("stage target reached" if
                                          after["stage_pause_reached"]
                                          else "insufficient remaining budget"),
                               "budget": after}
                    break
    _write(output / "development" / "dev-summary.json",
           {"schema": "agent-optimization-dev/v1", "batch_id": batch,
            "profiles": profiles, "tasks": [t["id"] for t in tasks],
            "repeats": repeats, "n_rows": len(rows),
            "stopped": stopped, "finished_at": _now()})
    print(json.dumps({"n_rows": len(rows), "stopped": stopped,
                      "budget": budget_state(protocol)}, ensure_ascii=False, indent=2))
    return 0


def _select_star(protocol, output):
    """Apply the published selection order to the development rows."""
    # Prefer the canonical merge (first rounds + the repaired feedback halves).
    canonical = output / "development" / "rows-canonical.json"
    rows_path = canonical if canonical.exists() else output / "development" / "rows.json"
    if not rows_path.exists():
        raise SystemExit("freeze needs development rows")
    rows = _load(rows_path)["rows"]
    profiles = list(protocol["profiles"].keys())
    stats = {}
    for profile in profiles:
        mine = [r for r in rows if r.get("profile") == profile]
        if not mine:
            continue
        violations = 0
        for r in mine:
            actual = (r["canonical_input"].get("actual_conditions") or {})
            match = actual.get("matches_requested") or {}
            if match and match.get("all") is False:
                violations += 1
            if r["canonical_input"].get("contract_violations"):
                violations += 1
        correct = sum(1 for r in mine
                      if r["score"]["dimensions"].get("field_completed")
                      or r["score"]["dimensions"].get("compliant_insufficient"))
        failures = sum(1 for r in mine
                       if r["normalized"]["status"] == "failed")
        cost = sum((r["canonical_input"].get("model_cost_cny") or 0)
                   for r in mine)
        tools = sum(sum((r["normalized"]["attempts"]["tool_attempts"].get(tool, {})
                         or {}).get("start", 0)
                        for tool in r["normalized"]["attempts"]["tool_attempts"])
                    for r in mine)
        accepted = sum(1 for r in mine
                       if r["normalized"]["final_delivery"]["model_claim_accepted"])
        stats[profile] = {"n": len(mine), "correct_handling": correct,
                          "agent_execution_success": accepted,
                          "execution_failures": failures,
                          "severe_violations": violations,
                          "tool_calls": tools, "cost_cny": round(cost, 6)}
    # The published order (book §11.2). `correct_handling` is the §12.2 metric; the
    # stricter `agent_execution_success` (which also requires no fallback) is
    # reported alongside so a reviewer can re-rank under that reading.
    ranked = sorted(stats.items(), key=lambda kv: (
        kv[1]["severe_violations"], -kv[1]["correct_handling"],
        kv[1]["execution_failures"], kv[1]["tool_calls"], kv[1]["cost_cny"],
        0 if kv[0] == "A1" else 1))
    return (ranked[0][0] if ranked else None), stats, ranked


def cmd_freeze(args):
    protocol = _load(args.protocol)
    output = Path(args.output)
    star, stats, ranked = _select_star(protocol, output)
    profile = (args.profile or star or "A1").upper()
    if profile not in protocol["profiles"]:
        raise SystemExit(f"unknown profile: {profile}")
    snapshot_id = args.snapshot or f"{profile.lower()}-{_now()[:10]}"
    folder = output / "snapshots" / snapshot_id
    if folder.exists() and any(folder.iterdir()):
        raise SystemExit(f"snapshot already exists and is not empty: {folder}")
    (folder / "source").mkdir(parents=True, exist_ok=True)
    files = {}
    for rel in SNAPSHOT_SOURCES:
        src = ROOT / rel
        if not src.exists():
            continue
        dest = folder / "source" / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        files[rel] = _sha(src)
    contract = {"schema": "agent-optimization-freeze/v1", "snapshot_id": snapshot_id,
                "created_at": _now(), "selected_profile": profile,
                "selection_stats": stats, "ranking": [list(r) for r in ranked],
                "selection_order": protocol["selection_order"],
                "policy": protocol["profiles"][profile]["policy"],
                "thinking": protocol["profiles"][profile]["thinking"],
                "model": {"name": args.model or "deepseek-flash",
                          "provider": "deepseek"},
                "resources": __import__("resource_limits").limits(),
                "protocol_sha256": _sha(Path(args.protocol)),
                "files": files}
    _write(folder / "snapshot.json", contract)
    (folder / "MANIFEST.sha256").write_text(
        "\n".join(f"{digest}  {rel}" for rel, digest in sorted(files.items())) + "\n")
    print(json.dumps({"snapshot_id": snapshot_id, "profile": profile,
                      "n_files": len(files)}, ensure_ascii=False))
    return 0


def cmd_validate(args):
    protocol = _load(args.protocol)
    output = Path(args.output)
    snapshot_id = args.snapshot
    if not snapshot_id:
        snaps = sorted((output / "snapshots").glob("*/snapshot.json"))
        if not snaps:
            raise SystemExit("no frozen snapshot; run freeze first")
        snapshot_id = snaps[-1].parent.name
    contract = _load(output / "snapshots" / snapshot_id / "snapshot.json")
    # The frozen originals must match the current working tree for the frozen files.
    mismatched = []
    for rel, digest in contract["files"].items():
        current = ROOT / rel
        if not current.exists() or _sha(current) != digest:
            mismatched.append(rel)
    if mismatched:
        raise SystemExit("refusing to validate: execution differs from the frozen "
                         f"snapshot for {mismatched}")
    baseline = budget_state(protocol)
    _require_budget(protocol, output, stage="validation")
    profile = contract["selected_profile"]
    tasks = protocol["validation"]["tasks"]
    repeats = int(protocol["validation"]["repeats"])
    batch = protocol["validation"]["batch_id"]
    rows_path = output / "validation" / "rows.json"
    rows = []
    stopped = None
    for task in tasks:
        for repeat in range(1, repeats + 1):
            for arm in ("A", "M", "R"):
                if stopped:
                    break
                first = run_arm(task, arm, profile, output=output, stage="validation",
                                batch_id=batch, repeat=repeat)
                rows.append(first)
                fb = task.get("feedback")
                if fb and first.get("run_id"):
                    fb = {**fb, "id": f"{task['id']}-fb"}
                    rows.append(run_arm(task, arm, profile, output=output,
                                        stage="validation", batch_id=batch,
                                        parent_run_id=first["run_id"], fb=fb,
                                        repeat=repeat))
                _append(rows_path, rows[-2:] if fb else rows[-1:])
            if not stopped:
                after = _stage_reached(protocol, output, stage="validation",
                                       baseline_total=baseline["total_upper_cny"])
                if after["stage_pause_reached"] or not after["can_reserve_one"]:
                    stopped = {"reason": "stage target or budget",
                               "budget": after}
                    break
    _write(output / "validation" / "validation-summary.json",
           {"schema": "agent-optimization-validation/v1", "batch_id": batch,
            "snapshot_id": snapshot_id, "profile": profile, "n_rows": len(rows),
            "stopped": stopped, "finished_at": _now()})
    print(json.dumps({"snapshot_id": snapshot_id, "profile": profile,
                      "n_rows": len(rows), "stopped": stopped},
                     ensure_ascii=False, indent=2))
    return 0


def _driver_record(run_id, profile):
    """The saved model answer for one run, read from the run's own directory."""
    import runlog
    folder = runlog.run_dir(run_id)
    candidates = []
    if profile:
        candidates.append(folder / f"agent_execution_{profile}_{run_id}.json")
    candidates.append(folder / "model_response.json")
    candidates.extend(sorted(folder.glob("agent_execution_*.json")))
    for path in candidates:
        if path.exists():
            return _load(path)
    return {}


def revalidate_saved(run_id, profile, canonical):
    """Re-run the deterministic acceptance on a SAVED run. No model, no network.

    Acceptance and scoring are pure functions of the run's own artifacts, so a
    correction to them must be replayable on the paid evidence without buying new
    sessions. This is the "规范输入 + 修正依据 + 评分版本" path, and it never
    overwrites the original rows.
    """
    import run_agent_arm
    import runlog
    folder = runlog.run_dir(run_id)
    if not (folder / "mcp_state.json").exists():
        return None
    task = _load(folder / "task.json")
    state = _load(folder / "mcp_state.json")
    driver = _driver_record(run_id, profile)
    question_id = task.get("question_id") or canonical.get("question_id")
    return run_agent_arm.validate(run_id, task.get("demo_case_ref") or "",
                                  task, state.get("acquired") or [], driver,
                                  question_id)


def cmd_rescore(args):
    import c_scorer
    output = Path(args.output)
    source = Path(args.input)
    payload = _load(source)
    rows = payload.get("rows", payload if isinstance(payload, list) else [])
    out_rows = []
    for row in rows:
        canonical = dict(row.get("canonical_input", row))
        before = row.get("score") or {}
        revalidated = None
        if args.revalidate and row.get("run_id"):
            revalidated = revalidate_saved(row["run_id"], row.get("profile"), canonical)
        if revalidated:
            # Fold the re-validated acceptance/delivery back into the canonical
            # input, keeping every original fact.
            canonical.update({
                "delivered": revalidated["delivered"],
                "model_claim": revalidated["model_claim"],
                "acquisition": revalidated["acquisition"],
                "validator_verdict": revalidated["validator_verdict"],
                "acceptance_checks": revalidated["acceptance_checks"],
                "evidence_ref_report": revalidated["evidence_ref_report"],
                "finish_audit": revalidated["finish_audit"],
                "requested_conditions": revalidated["requested_conditions"],
                "agreement": revalidated["agreement"],
            })
        norm = c_scorer.normalize_row(canonical)
        score = c_scorer.score_row(norm)
        out_rows.append({"protocol_task": row.get("protocol_task"),
                         "arm": row.get("arm"), "profile": row.get("profile"),
                         "phase": row.get("phase"), "repeat": row.get("repeat"),
                         "run_id": row.get("run_id"),
                         "canonical_input": canonical, "normalized": norm,
                         "score": score,
                         "changed_primary": (before.get("primary") != score["primary"]
                                             if before else None),
                         "accepted_before": ((row.get("normalized") or {})
                                             .get("final_delivery") or {}
                                             ).get("model_claim_accepted"),
                         "accepted_after": norm["final_delivery"]["model_claim_accepted"]})
    label = f"{source.parent.name}-{source.stem}"
    target = output / "results" / f"rescore-{label}.json"
    _write(target, {"schema": "agent-optimization-rescore/v1",
                    "source": str(source), "scorer": "main/c_scorer.py",
                    "validator": "main/run_agent_arm.py::validate",
                    "score_version_sha256": _sha(ROOT / "main/c_scorer.py"),
                    "revalidated_from_saved_runs": bool(args.revalidate),
                    "rescored_at": _now(), "rows": out_rows})
    print(json.dumps({"source": str(source), "n_rows": len(out_rows),
                      "changed_primary": sum(1 for r in out_rows
                                             if r["changed_primary"]),
                      "target": str(target.relative_to(ROOT))}, ensure_ascii=False))
    return 0


def cmd_report(args):
    protocol = _load(args.protocol)
    output = Path(args.output)
    import agent_optimization_preserve as preserve
    gates_path = output / "gates" / "free-gates.json"
    gates = _load(gates_path) if gates_path.exists() else {"results": []}
    dev_summary = output / "development" / "dev-summary.json"
    val_summary = output / "validation" / "validation-summary.json"
    status = {
        "schema": "agent-optimization-status/v1", "generated_at": _now(),
        "budget": budget_state(protocol),
        "history_preservation": preserve.verify(output),
        "gates": {r["id"]: r["status"] for r in gates.get("results", [])},
        "development": (_load(dev_summary) if dev_summary.exists() else
                        {"status": "not_run"}),
        "validation": (_load(val_summary) if val_summary.exists() else
                       {"status": "not_run"}),
        "smoke": (_load(output / "evidence" / "smoke.json")
                  if (output / "evidence" / "smoke.json").exists() else
                  {"status": "not_run"}),
        "web_feedback": (_load(output / "evidence" / "web-feedback.json")
                         if (output / "evidence" / "web-feedback.json").exists()
                         else {"status": "not_run"}),
    }
    _write(output / "results" / "status.json", status)
    # MANIFEST over everything this round produced.
    manifest = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.json":
            manifest[str(path.relative_to(output))] = {
                "sha256": _sha(path), "bytes": path.stat().st_size}
    _write(output / "MANIFEST.json",
           {"schema": "agent-optimization-manifest/v1", "generated_at": _now(),
            "n_files": len(manifest), "files": manifest})
    print(json.dumps({"status": status["gates"], "budget": status["budget"],
                      "history_unchanged": status["history_preservation"]["unchanged"],
                      "n_manifest_files": len(manifest)}, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "gates", "smoke", "dev",
                                            "freeze", "validate", "rescore", "report"])
    parser.add_argument("--protocol", default=str(DEFAULT_PROTOCOL))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--profile", default=None)
    parser.add_argument("--profiles", default=None,
                        help="comma-separated profile subset for dev")
    parser.add_argument("--snapshot", default=None)
    parser.add_argument("--input", default=None)
    parser.add_argument("--only", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--feedback-only", action="store_true",
                        dest="feedback_only",
                        help="repair cycle: re-run only the feedback halves")
    parser.add_argument("--revalidate", action="store_true",
                        help="re-run the deterministic acceptance on saved runs")
    args = parser.parse_args()
    # The protocol may point at its own output directory.
    if args.output == str(DEFAULT_OUTPUT):
        try:
            args.output = str(ROOT / _load(Path(args.protocol))["output_dir"])
        except Exception:  # noqa: BLE001
            pass
    import core
    core.fdsn.set_cache(core.CACHE)
    return {"plan": cmd_plan, "gates": cmd_gates, "smoke": cmd_smoke,
            "dev": cmd_dev, "freeze": cmd_freeze, "validate": cmd_validate,
            "rescore": cmd_rescore, "report": cmd_report}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())