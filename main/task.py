"""Immutable task snapshots.

A task is what the user actually asked for, frozen before any model or network
action. The task_id is opaque and random: it must not encode the answer, because
the model sees it.

Two views exist and they must never be confused:

  task          the full record, kept evaluator-side. May carry a demo case
                reference and acceptance labels.
  model_view()  what the AGH prompt and every MCP tool may disclose. Carries the
                user's verbatim question and the physical task inputs only.

Freezing is write-once and content-addressed. Loading re-verifies the digest, so
a mutated task file is detected rather than silently used.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "main/tasks"

SCHEMA = "seismic-review-task/v1"
DEFAULT_BAND_HZ = [1.0, 12.0]
# 固定方法频带。与 core.METHOD_BAND 必须一致；测试会断言两者相同，因此这里是
# 记录而不是第二处真相。用户问的频带是 target_band_hz，两者不可混为一谈。
METHOD_BAND_HZ = [1.0, 8.0]
DEFAULT_GOAL = "three_component_pick"
DEFAULT_ALLOWED = ["discover", "acquire", "onset_check", "read_evidence"]
TOTAL_BUDGET_CNY = 20.0

# The demo injection is evaluator-side and must be disclosed wherever its result is
# shown. It derives a separate bad-channel copy; the downloaded file is untouched.
INJECTION_NOTE = (
    "演示用注入：本轮第一个取得台站的 E（东西）通道被替换为一个平直（恒定）副本，"
    "放在单独的 derived 文件里，原始下载文件未被改动。它是人工注入的演示数据，"
    "不是真实观测故障；结论只说明工具能否识别这一类不可靠通道。")

# Fields the model and MCP tools may see. Adding a key here is a disclosure
# decision: it must be a physical task input, never a label or an answer.
MODEL_VISIBLE_TASK_FIELDS = (
    "task_id", "user_question", "window_start", "window_end",
    "region", "target_band_hz", "method_goal", "allowed_actions",
    # 指南 §2.2：三站/三秒这类操作标准是公开合同，不是隐藏参考
    # 答案——三组（含默认 open 的 A）都必须拿到同一份。
    "operational_standard",
    # 本轮 新增：请求条件里模型必须知道、也必须被 validator 按同一份核对
    # 的项。它们都是用户提出的要求或公开事实，不含任何答案标签。
    "question_id", "scope", "diagnostic_bands_hz", "acquisition_allowed",
    "excluded_candidate_ids", "max_unique_candidates_episode",
)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_task_id() -> str:
    """Opaque, random, answer-free. Never derived from a case name."""
    return "t-" + secrets.token_hex(8)


def make_task(*, user_question, window_start, window_end, region=None,
              target_band_hz=None, method_goal=DEFAULT_GOAL, allowed_actions=None,
              budget_cny=None, demo_case_ref=None, acceptance=None,
              operational_standard=None, question_id=None, scope=None,
              diagnostic_bands_hz=None, acquisition_allowed=True,
              excluded_candidate_ids=None, max_unique_candidates_episode=6) -> dict:
    """Build a task record. Nothing here touches the network or a model."""
    question = (user_question or "").strip()
    if not question:
        raise ValueError("user_question is required")
    if not window_start or not window_end:
        raise ValueError("window_start and window_end are required")
    band = [float(x) for x in (target_band_hz or DEFAULT_BAND_HZ)]
    if len(band) != 2 or not 0 < band[0] < band[1]:
        raise ValueError("target_band_hz must be [low, high] with 0 < low < high")
    task = {
        "schema": SCHEMA,
        "task_id": new_task_id(),
        "created_at": _now(),
        "user_question": question,
        "window_start": str(window_start),
        "window_end": str(window_end),
        "region": region,
        "target_band_hz": band,
        "method_band_hz": list(METHOD_BAND_HZ),
        "method_goal": method_goal,
        "allowed_actions": list(allowed_actions or DEFAULT_ALLOWED),
        # 操作标准（如“3 秒窗内 ≥3 台触发”）：公开给所有组的判据，不是
        # 隐藏参考答案。缺省 None 表示该问题没有额外公开标准。
        "operational_standard": (str(operational_standard).strip()
                                 if operational_standard else None),
        "budget_cny": TOTAL_BUDGET_CNY if budget_cny is None else float(budget_cny),
        # ---- 请求条件：与目标频带分开，三者互不替代 -------------------
        "question_id": question_id or None,
        "scope": scope,
        "diagnostic_bands_hz": _diagnostic_bands(diagnostic_bands_hz),
        "acquisition_allowed": bool(acquisition_allowed),
        "excluded_candidate_ids": [str(c) for c in (excluded_candidate_ids or [])],
        "max_unique_candidates_episode": int(max_unique_candidates_episode),
        # Evaluator-side only. model_view() drops these.
        "demo_case_ref": demo_case_ref,
        "acceptance": acceptance,
    }
    if not task["question_id"]:
        # The criterion the user's own words imply. Stored so the requested
        # condition object, the prompt and the validator cannot classify the same
        # question differently.
        task["question_id"] = _classify(question)
    task["requested_conditions"] = requested_conditions_for(task)
    task["task_sha256"] = task_digest(task)
    return task


def _classify(text):
    try:
        import core  # noqa: PLC0415
        return core.classify_question(text)
    except Exception:  # noqa: BLE001
        return "Q_propagating"


def _diagnostic_bands(value):
    """Validate the declared diagnostic grid through the one normalizer."""
    import execution_conditions  # noqa: PLC0415
    return execution_conditions.normalize_diagnostic_bands(value)


def requested_conditions_for(task: dict) -> dict:
    """The frozen request's condition object, built in exactly one place."""
    import execution_conditions  # noqa: PLC0415
    return execution_conditions.from_task(task)


def task_digest(task: dict) -> str:
    """Digest over the model-visible content plus the evaluator-side labels.

    The digest covers `demo_case_ref` and `acceptance` deliberately: they are
    part of the frozen record, and a reviewer must be able to detect if they
    were edited after the fact.
    """
    body = {k: v for k, v in task.items() if k not in ("task_sha256", "frozen_at")}
    return _digest(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str))


def model_view(task: dict) -> dict:
    """The only task content an AGH prompt or MCP tool may disclose."""
    return {k: task.get(k) for k in MODEL_VISIBLE_TASK_FIELDS}


def task_from_state_view(state: dict) -> dict:
    """Rebuild the frozen task from the declared model-visible fields.

Prompt construction, MCP tools, recomputation and validation use this same
contract, including the method goal."""
    task = {k: state.get(k) for k in MODEL_VISIBLE_TASK_FIELDS}
    task["method_band_hz"] = list(state.get("method_band_hz") or METHOD_BAND_HZ)
    if task.get("method_goal") not in ("three_component_pick", "vertical_onset"):
        task["method_goal"] = DEFAULT_GOAL
    if not task.get("target_band_hz"):
        task["target_band_hz"] = list(DEFAULT_BAND_HZ)
    # fields: defaulted here, never invented downstream. A state written before
    # the field existed carries an explicit null, so `default=` alone is not enough
    # — `bool(None)` would silently turn "unknown" into "acquisition forbidden".
    task["diagnostic_bands_hz"] = _diagnostic_bands(task.get("diagnostic_bands_hz"))
    allowed = task.get("acquisition_allowed")
    task["acquisition_allowed"] = True if allowed is None else bool(allowed)
    task["excluded_candidate_ids"] = list(task.get("excluded_candidate_ids") or [])
    task["max_unique_candidates_episode"] = int(
        task.get("max_unique_candidates_episode") or 6)
    if task.get("question_id") is None:
        try:
            import core  # noqa: PLC0415
            task["question_id"] = core.classify_question(task.get("user_question"))
        except Exception:  # noqa: BLE001
            task["question_id"] = "Q_propagating"
    # The condition object is rebuilt from the same fields, so a tool that reads
    # `requested_conditions` and the validator that rebuilds it cannot disagree.
    task["requested_conditions"] = requested_conditions_for(task)
    return task


def freeze(task: dict) -> Path:
    """Write the task once. Refuses to overwrite an existing frozen task."""
    TASKS.mkdir(parents=True, exist_ok=True)
    path = TASKS / f"{task['task_id']}.json"
    if path.exists():
        raise FileExistsError(f"task already frozen: {task['task_id']}")
    record = dict(task)
    record["frozen_at"] = _now()
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    os.chmod(path, 0o444)  # belt and braces; the loader also re-verifies the digest
    return path


def load_task(task_id: str) -> dict:
    """Load a frozen task and verify it was not modified after freezing."""
    path = TASKS / f"{task_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"unknown task: {task_id}")
    record = json.loads(path.read_text())
    if record.get("task_id") != task_id:
        raise ValueError(f"task id mismatch in {path.name}")
    if record.get("task_sha256") != task_digest(record):
        raise ValueError(f"task digest mismatch (file was modified): {task_id}")
    return record


def task_from_case(case, *, user_question=None, question_id=None, target_band_hz=None,
                   method_goal=None, operational_standard=None):
    """Build a task from a preset demo case. The case is only a demo reference."""
    sys_path = ROOT / "main"
    if str(sys_path) not in os.sys.path:
        os.sys.path.insert(0, str(sys_path))
    import core  # noqa: PLC0415

    question = user_question or core.QUESTIONS[question_id or case["question_id"]]
    window = core.window_of(case)
    return make_task(
        user_question=question,
        window_start=window["start"],
        window_end=window["end"],
        region=case["region"],
        target_band_hz=target_band_hz or DEFAULT_BAND_HZ,
        method_goal=method_goal or DEFAULT_GOAL,
        demo_case_ref=case["case_id"],
        operational_standard=operational_standard,
        acceptance={"expect": case.get("expect"), "kind": case.get("kind"),
                    "split": case.get("split")},
    )

def task_from_spec(spec, *, user_question=None, region=None,
                   demo_injection=False):
    """Build a task from a user-written spec  rather than a preset case.

    `spec` is the `structured` block from `main/task_input.py`. The window, region
    and target band come from the user; nothing is defaulted to a preset event.
    A spec with no window is refused rather than quietly filled in.

    `demo_injection` is the disclosed bad-channel demonstration. It is evaluator-side
    only: `model_view()` never carries it, and every result that uses it says so.
    """
    spec = dict(spec)
    if not spec.get("window_start") or not spec.get("window_end"):
        raise ValueError("user task needs an explicit UTC window; "
                         "ask for clarification instead of using a default event")
    question = user_question or spec.get("user_question")
    if not question:
        raise ValueError("user task needs the question being asked")
    where = region if region is not None else spec.get("region")
    if not where:
        raise ValueError("user task needs a region or station list")
    demo = where.get("demo_case_ref") if where.get("kind") == "preset" else None
    acceptance = {"source": "user_spec",
                  "question_ids": spec.get("question_ids") or []}
    if demo_injection:
        acceptance.update({"injected": True, "injection_note": INJECTION_NOTE})
    goals = spec.get("question_ids") or []
    return make_task(
        user_question=question,
        window_start=spec["window_start"],
        window_end=spec["window_end"],
        region=where,
        target_band_hz=spec.get("target_band_hz") or DEFAULT_BAND_HZ,
        method_goal=spec.get("method_goal") or "three_component_pick",
        acceptance=acceptance,
        demo_case_ref=demo,
        # the question's own criterion and the user's requested diagnostic
        # grid ride into the frozen task; the scope is resolved against the real
        # candidate list at prepare time (candidate ids do not exist before
        # discovery).
        question_id=(goals[0] if goals else None),
        diagnostic_bands_hz=spec.get("diagnostic_bands_hz"),
        acquisition_allowed=spec.get("acquisition_allowed", True),
        excluded_candidate_ids=spec.get("excluded_candidate_ids"),
    )


def case_for_task(task):
    """A synthetic case so existing discovery/arm code can serve a user task.

    The case id is derived from the task, so candidate ids stay stable for a given
    user request without leaking any evaluation label.
    """
    region = task.get("region") or {}
    acceptance = task.get("acceptance") or {}
    return {
        "case_id": "user-" + task["task_id"].split("-", 1)[-1][:12],
        "start": task["window_start"],
        "region": region,
        # question_ids may legitimately be empty (a free-form user spec that did not
        # name a question type); index only after the emptiness check.
        "question_id": next(iter(acceptance.get("question_ids") or []),
                            "Q_propagating"),
        "kind": "user_task",
        "split": "user",
        "expect": None,
        "origin": None,
        # The disclosed demo injection, if the user turned it on. It reaches the
        # acquisition layer as a derived copy and is reported with the result.
        "injected": bool(acceptance.get("injected")),
        "inject_station": acceptance.get("inject_station"),
        "injection": acceptance.get("injection_note") or INJECTION_NOTE,
    }
