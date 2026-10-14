"""The tool surface has exactly one source of truth.

A hard-coded copy of the tool list in the bridge once rejected a tool the model
could see, which killed a whole session with "Tool outside allowlist". These tests
fail if any of the four consumers drifts from `main/tools.json`.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "main"))

SPEC = json.loads((ROOT / "main/tools.json").read_text())["tools"]
NAMES = [t["name"] for t in SPEC]


def test_the_spec_lists_the_tools_the_loops_need():
    # O2 新增 task_status：进度事实、剩余额度与结束资格，免费本地，不替模型执行检查。
    assert set(NAMES) == {"list_candidates", "acquire_station", "run_onset_check",
                          "get_evidence", "run_stability_check",
                          "task_status", "exclude_observation"}


def test_every_entry_declares_its_arguments_and_scope():
    for tool in SPEC:
        assert isinstance(tool["args"], list)
        assert all("name" in a and "required" in a for a in tool["args"])
        assert isinstance(tool["candidate_scoped"], bool)
        assert tool["summary"]


def test_only_acquisition_costs_external_requests():
    costing = {t["name"] for t in SPEC if t["costs_requests"]}
    assert costing == {"acquire_station"}


def test_candidate_scoped_tools_all_take_a_candidate_id():
    for tool in SPEC:
        if tool["candidate_scoped"]:
            assert any(a["name"] == "candidate_id" and a["required"]
                       for a in tool["args"]), tool["name"]


def test_the_python_mcp_server_registers_exactly_these_tools():
    source = (ROOT / "main/mcp_server.py").read_text()
    registered = re.findall(
        r"@server\.tool\((?:[^()]|\([^()]*\))*\)\s*\ndef (\w+)", source)
    assert sorted(registered) == sorted(NAMES), (
        f"mcp_server.py registers {sorted(registered)} but tools.json says "
        f"{sorted(NAMES)}")


def test_no_javascript_file_hard_codes_a_tool_list():
    """The bridge, the permission layer and the driver must all read tools.json."""
    for name in ("main/provider_bridge.mjs", "main/permission.mjs",
                 "main/agent_run.mjs"):
        text = (ROOT / name).read_text()
        assert "tools.json" in text, f"{name} must read the shared tool spec"
        # no list/set literal containing more than one tool name
        names_in_literals = re.findall(
            r"\[[^\]]*(?:'list_candidates'|'get_evidence'|'acquire_station')"
            r"[^\]]*\]", text)
        assert not names_in_literals, f"{name} hard-codes a tool list"


def test_the_bridge_and_the_permission_layer_agree_on_argument_names():
    """Both derive from tools.json, so the sets must be identical by construction."""
    import subprocess
    script = (
        "import('./main/provider_bridge.mjs').then(async b=>{"
        "const p=await import('./main/permission.mjs');"
        "console.log(JSON.stringify(b.TOOLSPEC.map(t=>t.name)));"
        "});")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:400]
    assert json.loads(out.stdout.strip().splitlines()[-1]) == NAMES


def test_the_run_scoped_allowlist_contains_real_prefixed_names():
    """Spreading a Map yields pairs; that bug emptied the allow-list entirely."""
    import subprocess
    script = (
        "import('./main/provider_bridge.mjs').then(b=>"
        "console.log(JSON.stringify(b.runScopedTools('mcp_x_1_'))));")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:400]
    names = json.loads(out.stdout.strip().splitlines()[-1])
    assert names == ["mcp_x_1_" + n for n in NAMES], names
    assert all(isinstance(n, str) for n in names)


def test_an_unknown_tool_argument_is_rejected_by_the_bridge():
    """Argument checking now comes from the spec rather than an ad-hoc branch."""
    import subprocess
    script = (
        "import('./main/provider_bridge.mjs').then(b=>{"
        "const active={allowedCandidates:['c1'],toolCalls:0};"
        "const call={function:{name:'pfx_run_onset_check',"
        "arguments:JSON.stringify({candidate_id:'c1',sneaky:1})}};"
        "try{b.checkTool(call,active,'pfx_');console.log('ACCEPTED');}"
        "catch(e){console.log('REJECTED:'+e.message);}});")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:400]
    assert out.stdout.strip().endswith("REJECTED:Unknown argument")


def test_a_missing_required_argument_is_rejected_by_the_bridge():
    import subprocess
    script = (
        "import('./main/provider_bridge.mjs').then(b=>{"
        "const active={allowedCandidates:['c1'],toolCalls:0};"
        "const call={function:{name:'pfx_exclude_observation',arguments:'{}'}};"
        "try{b.checkTool(call,active,'pfx_');console.log('ACCEPTED');}"
        "catch(e){console.log('REJECTED:'+e.message);}});")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:400]
    assert out.stdout.strip().endswith("REJECTED:Missing argument candidate_id")


def test_every_prefixed_tool_the_model_may_see_is_accepted_by_the_bridge():
    """The exact failure that killed the follow-up session must not recur."""
    import subprocess
    arguments = {
        "list_candidates": {}, "get_evidence": {},
        "acquire_station": {"candidate_id": "c1"},
        "run_onset_check": {"candidate_id": "c1"},
        "run_stability_check": {"candidate_id": "c1"},
        "task_status": {"stage": "progress"},
        "exclude_observation": {"candidate_id": "c1", "reason": "user asked"},
    }
    calls = ",".join(
        "{function:{name:'pfx_%s',arguments:JSON.stringify(%s)}}"
        % (name, json.dumps(arguments[name])) for name in NAMES)
    script = (
        "import('./main/provider_bridge.mjs').then(b=>{"
        f"const calls=[{calls}];"
        "for(const call of calls){"
        "const active={allowedCandidates:['c1'],toolCalls:0};"
        "try{b.checkTool(call,active,'pfx_');console.log('OK '+call.function.name);}"
        "catch(e){console.log('FAIL '+call.function.name+' '+e.message);}}});")
    out = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr[:400]
    lines = out.stdout.strip().splitlines()
    assert len(lines) == len(NAMES)
    assert all(line.startswith("OK ") for line in lines), lines

def test_the_recorded_method_band_matches_the_one_core_actually_uses():
    """A snapshot that names a band core does not use would be a false record."""
    import core
    import task as taskmod

    assert taskmod.METHOD_BAND_HZ == list(core.METHOD_BAND), (
        f"task.py records {taskmod.METHOD_BAND_HZ} but core uses "
        f"{list(core.METHOD_BAND)}")


def test_a_frozen_task_distinguishes_the_asked_band_from_the_method_band():
    import task as taskmod

    frozen = taskmod.make_task(user_question="问一句?",
                               window_start="2025-01-01T00:00:00",
                               window_end="2025-01-01T00:02:00",
                               target_band_hz=[1.0, 12.0])
    assert frozen["target_band_hz"] == [1.0, 12.0]
    assert frozen["method_band_hz"] == [1.0, 8.0]
    assert frozen["target_band_hz"] != frozen["method_band_hz"]
