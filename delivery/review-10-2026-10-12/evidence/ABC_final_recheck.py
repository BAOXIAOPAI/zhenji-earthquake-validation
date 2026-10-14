"""Independent functional audit, no model/daemon/network and no history writes.
All product probe states/tasks live in TemporaryDirectory; only this new review's
JSON result is written. Execute with .venv/bin/python -B this_file.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).with_suffix('.json')
def read(p):
    return json.loads(Path(p).read_text())

with tempfile.TemporaryDirectory(prefix='review9-abc-') as tmp:
    tmp = Path(tmp)
    os.environ.update(RUNLOG_DIR=str(tmp / 'runlog'), EQ_CACHE_DIR=str(tmp / 'cache'),
                      EQ_STRICT_OFFLINE='1', MPLCONFIGDIR=str(tmp / 'mpl'))
    font_cache = ROOT / '.cache/matplotlib'
    if font_cache.exists():
        shutil.copytree(font_cache, tmp / 'mpl', dirs_exist_ok=True)
    sys.path.insert(0, str(ROOT / 'main'))
    import core
    import execution_conditions as EC
    import feedback
    import finish_audit
    sys.path.insert(0, str(ROOT / 'main'))
    import mcp_server
    import prepare_agent
    import run_agent_arm as agent
    import runlog
    import task
    task.TASKS = tmp / 'tasks'
    task.TASKS.mkdir()
    source = ROOT / 'main/runlog/r-ab5d401974765197'
    base = read(source / 'mcp_state.json')
    frozen = read(source / 'task.json')
    cid = base['acquired'][0]['candidate_id']
    out = {'schema': 'review9-functional-ABC/v1', 'network_or_model_calls': 0}

    def checked(name, state, frozen_task, *, refs, scope, stop='public_criterion_satisfied'):
        state = copy.deepcopy(state)
        folder = runlog.run_dir(name)
        folder.mkdir(parents=True)
        state['run_id'] = name
        (folder / 'mcp_state.json').write_text(json.dumps(state))
        (folder / 'task.json').write_text(json.dumps(frozen_task))
        requested = state['requested_conditions']
        question = requested['question_id']
        verdict = core.verdict_for_question(state['acquired'], question, None, frozen_task)
        claim = {'conclusion': verdict['verdict'], 'field': requested['required_field'],
                 'condition_id': requested['condition_id'],
                 'scope_candidate_ids': scope, 'evidence_refs': refs}
        if stop is not None:
            claim['stop_reason_code'] = stop
        result = agent.validate(name, '', frozen_task, state['acquired'],
                                {'answer': json.dumps(claim)}, question)
        return {'claim': claim, 'accepted': result['delivered']['model_claim_accepted'],
                'fallback_used': result['delivered']['fallback_used'],
                'task_completed': result['delivered']['task_completed'],
                'result_determined': result['delivered'].get('result_determined'),
                'audit_outcome': result['finish_audit']['outcome'],
                'can_finish': result['finish_audit']['can_finish'],
                'stop_bases': result['finish_audit']['stop_bases'],
                'unmet': result['finish_audit']['unmet'],
                'acceptance': result['acceptance_checks'],
                'evidence': result['evidence_ref_report'],
                'scope': result['scope_report']}

    quality = copy.deepcopy(base)
    quality['derived_evidence_checks'] = {}
    quality['stability_checks'] = {}
    quality['tool_events'] = []
    for r in quality['acquired']:
        r.update(onset_computed=False, onset_utc=None, stability=None,
                 stability_computed=False)
    out['positive_quality_applicability_without_onset'] = checked(
        'positive-quality', quality, frozen,
        refs=[f'quality:{cid}', f'applicability:{cid}'], scope=[cid])
    out['negative_uncomputed_onset'] = checked(
        'bad-onset', quality, frozen, refs=[f'onset:{cid}'], scope=[cid])
    out['negative_fabricated_scope'] = checked(
        'bad-scope', quality, frozen, refs=[f'quality:{cid}'], scope=['invented-id'])
    out['negative_missing_stop_code'] = checked(
        'bad-stop', quality, frozen, refs=[f'quality:{cid}'], scope=[cid], stop=None)

    excluded = copy.deepcopy(quality)
    change = feedback.normalize_change(excluded, {'kind':'exclude', 'candidate_id':cid})
    after = feedback.requested_after(excluded, change)
    excluded['requested_conditions'] = after
    excluded['acquired'][0].update(excluded=True, excluded_by='deterministic_guard')
    excluded_task = {**frozen, 'requested_conditions': after,
                     'acquisition_allowed': False, 'excluded_candidate_ids': [cid]}
    out['positive_unique_excluded_legal_unknown'] = checked(
        'excluded-unknown', excluded, excluded_task, refs=[], scope=[], stop='scope_exhausted')
    out['negative_excluded_reference'] = agent._evidence_refs(
        [f'quality:{cid}'], excluded, after)

    unstarted = prepare_agent.model_state(
        'unstarted-explicit',
        {**frozen, 'requested_conditions': quality['requested_conditions']},
        read(source / 'discovery.json'), analysis_level='quality_only')
    out['unstarted_explicit_scope_unknown'] = checked(
        'unstarted-explicit', unstarted, frozen, refs=[], scope=[], stop='scope_exhausted')
    out['unstarted_explicit_scope_unknown'].update(
        entry_level='production prepare_agent.model_state + production validate',
        acquisition_allowed=unstarted['requested_conditions']['acquisition_allowed'],
        explicit_requested_ids=unstarted['requested_conditions']['scope']['candidate_ids'],
        eligible_count=unstarted['discovery']['n_eligible'])

    v3path = ROOT / 'main/runlog/r-c8f9ca0979c35163'
    v3state, v3task = read(v3path/'mcp_state.json'), read(v3path/'task.json')
    audit = finish_audit.audit_finish(v3state, task=v3task)
    out['positive_real_three_of_three_stop'] = {
        'source': v3path.name, 'attempted': len(v3state['attempted_candidate_ids_episode']),
        'cap': v3state['requested_conditions']['max_unique_candidates_episode'],
        'outcome': audit['outcome'], 'can_finish': audit['can_finish'],
        'stop_basis': audit['stop_basis']}

    # Full feedback prepare uses real local metadata/state and writes temporary only.
    parent_id = 'capped-parent'
    parent = runlog.run_dir(parent_id)
    parent.mkdir()
    capped_task = copy.deepcopy(frozen)
    capped_task['max_unique_candidates_episode'] = 3
    capped = copy.deepcopy(quality)
    req = copy.deepcopy(capped['requested_conditions'])
    req['max_unique_candidates_episode'] = 3
    req['condition_id'] = EC.condition_id(req)
    capped['requested_conditions'] = req
    capped_task['requested_conditions'] = req
    capped['attempted_candidate_ids_episode'] = [cid, 'failed-2', 'failed-3']
    (parent/'task.json').write_text(json.dumps(capped_task))
    (parent/'mcp_state.json').write_text(json.dumps(capped))
    (parent/'discovery.json').write_text((source/'discovery.json').read_text())
    (parent/'agent_config.json').write_text(json.dumps({'profile':'A1',
        'analysis_level':'quality_only','recorded':True}))
    prepared = prepare_agent.prepare_followup(parent_id, 'only vertical',
        change={'kind':'method_goal', 'method_goal':'vertical_onset'},
        analysis_level='quality_only')
    childfolder = runlog.run_dir(prepared['run_id'])
    child, childtask = read(childfolder/'mcp_state.json'), read(childfolder/'task.json')
    allowed, refused = mcp_server._acquisition_gate(child, 'new-4')
    out['feedback_default_and_cap'] = {
        'parent_cap':3, 'child_task_cap':childtask['max_unique_candidates_episode'],
        'child_state_cap':child['requested_conditions']['max_unique_candidates_episode'],
        'child_permission':child['requested_conditions']['acquisition_allowed'],
        'attempts':child['attempted_candidate_ids_episode'],
        'allowed_new':allowed, 'refusal':refused}
    opened = copy.deepcopy(child)
    opened['requested_conditions']['acquisition_allowed'] = True
    allowed, refused = mcp_server._acquisition_gate(opened, 'new-4')
    out['feedback_cap_with_explicit_permission'] = {'allowed_new':allowed, 'refusal':refused}

    captured=[]
    saved_prepare, saved_execute = prepare_agent.prepare_task, agent._execute
    prepare_agent.prepare_task = lambda *a, **kw: {'run_id':'not-launched',
        'task_id':frozen['task_id'], 'analysis_level':kw['analysis_level']}
    def launch_stub(*args, **kwargs):
        captured.append(kwargs)
        return {'run_id':'not-launched'}
    agent._execute = launch_stub
    agent.run_user_task(frozen)
    prepare_agent.prepare_task = saved_prepare
    saved_followup = prepare_agent.prepare_followup
    prepare_agent.prepare_followup = lambda *a, **kw: {'run_id':'not-launched',
        'task_id':frozen['task_id'], 'analysis_level':kw['analysis_level']}
    agent.run_followup(parent_id, 'change goal',
        change={'kind':'method_goal','method_goal':'vertical_onset'})
    prepare_agent.prepare_followup, agent._execute = saved_followup, saved_execute
    out['product_launch_parameters_no_driver_launched'] = captured

    # Watchdog identity checks only; the stop subprocess is stubbed, so this never
    # starts/stops a daemon or modifies a product resource.
    original_subprocess = agent.subprocess.run
    watchdog = {}
    for name, recorded in [('owner-match', 'owner-match'), ('owner-mismatch', 'another-run')]:
        f = runlog.run_dir(name)
        f.mkdir()
        home = tmp / (name + '-home')
        home.mkdir()
        (f / 'runtime-owner.json').write_text(json.dumps({
            'run_id': recorded, 'agh_home': str(home), 'owner_id': name + '-owner'}))
        invoked = []
        def stop_stub(cmd, **kwargs):
            invoked.append({'cmd':cmd, 'agh_home':kwargs['env']['AGH_HOME']})
            return SimpleNamespace(returncode=0, stdout='stopped', stderr='')
        agent.subprocess.run = stop_stub
        response = agent._stop_owned_daemon(name)
        watchdog[name] = {'response':response, 'stop_invoked':bool(invoked),
                          'only_expected_home':not invoked or invoked[0]['agh_home']==str(home)}
    agent.subprocess.run = original_subprocess
    out['watchdog_run_identity_no_daemon_launched'] = watchdog

OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2))
print(json.dumps(out, ensure_ascii=False, indent=2))
