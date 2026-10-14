// Adaptive arm: a real AGH session decides which observations to acquire and when
// to stop, for exactly one run id.
//
// The task comes from main/runlog/<run_id>/mcp_state.initial.json, which holds the
// user's VERBATIM question. The previous version read a per-case manifest, so an
// edited question never reached the model (evaluation P0-1). The prompt is written
// once and hashed, so the question in the prompt and in the tool state can be tied
// to the same frozen task.
//
// Exit codes: 0 succeeded, 1 session fatal, 2 usage error, 3 timeout.
import {execFile} from 'node:child_process';
import {mkdir,mkdtemp,readFile,writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {join,resolve} from 'node:path';
import {createClient,memoryJournal} from '../vendor/agnes-harness/packages/sdk/src/index.node.ts';
import {runResourceCommand} from '../vendor/agnes-harness/packages/resource-control-cli/src/resources.ts';
import {mcpLocalToolPrefix} from '../vendor/agnes-harness/packages/base/src/mcp/naming.ts';
import {startBridge} from './provider_bridge.mjs';
import {answerMainPermission} from './permission.mjs';

const project=resolve('.'),RUN=process.argv[2];
if(!RUN)throw Error('Usage: node main/agent_run.mjs <run_id> [model]');
// Run records live under RUNLOG_DIR so the layout is relocatable (G3 clean-env
// reproduction) instead of being hard-coded to this checkout.
const runlogRoot=process.env.RUNLOG_DIR?resolve(process.env.RUNLOG_DIR):join(project,'main/runlog');
const provider='deepseek',model=process.argv[3]??'deepseek-flash';
const stated=process.env.AGENT_VARIANT==='stated';
const variant=stated?'stated':'open';
const serverId='seismic-review',slug='seismic-review';
const toolPrefix=mcpLocalToolPrefix(slug);
const runDir=join(runlogRoot,RUN);
const sha256=t=>createHash('sha256').update(t).digest('hex');
// The frozen analysis goal, named explicitly so the model judges input
// conditions against the SAME goal the final validator uses (review-4 A2).
const GOAL_LABEL={'three_component_pick':'three-component P/S pick (needs three healthy components)',
  'vertical_onset':'vertical-component onset check (needs a healthy Z channel only)'};

const writeOnce=async(path,text)=>{
  try{await writeFile(path,text,{flag:'wx',mode:0o600});return true;}
  catch(e){if(e.code==='EEXIST')return false;throw e;}
};

// ---- the frozen task, read from this run's own state ------------------------
let state;
try{state=JSON.parse(await readFile(join(runDir,'mcp_state.initial.json'),'utf8'));}
catch(e){console.error('FATAL no run state for '+RUN);process.exit(2);}
const question=state.user_question??'';
if(!question){console.error('FATAL run state carries no user_question');process.exit(2);}
// A follow-up session carries the previous observations forward and adds a new
// request from the same user. It must appear verbatim, like the original question.
const followUp=state.follow_up??null;
const carried=state.carried_forward??[];
const methodGoal=state.method_goal||'three_component_pick';
const goalLabel=GOAL_LABEL[methodGoal]||methodGoal;
// 公开操作标准（review-5 §2.2）：默认 open 的 A 也必须拿到同一份判据。
const standard=state.operational_standard??null;

const prompt=`SEISMIC_REVIEW_RUN=${RUN}
TASK_ID=${state.task_id}

You are reviewing one seismic candidate window for a colleague. Their question is:

  "${question}"
${followUp?`
They have seen your earlier answer and now add:

  "${followUp}"

The observations from that earlier session are already acquired and are listed for
you; reuse them. Do not fetch a station that is already listed.
`:''}
The window under review is ${state.window_start} to ${state.window_end} UTC.
The band they asked about is ${state.target_band_hz[0]}-${state.target_band_hz[1]} Hz.
The analysis goal they asked for is ${methodGoal} — ${goalLabel}.
${standard?`The colleague's public operational standard for this question is:
  ${standard}
It is shared openly with every session (not a hidden answer key); decide and stop
consistently with it.
`:''}
Judge each station's input conditions against THAT goal: a bad horizontal channel
fails a three-component pick but can still satisfy a vertical onset check; a bad
or constant Z channel satisfies neither. Presence of a channel is not its health.
${carried.length?`Already acquired in the earlier session: ${carried.join(', ')}.\n`:''}
Answer it using only the ${slug} tools. Do not use shell, files, web, skills or delegation.

Tools and what each costs:
  list_candidates(limit)            free, no requests
  acquire_station(candidate_id)     costs two real external requests
  run_onset_check(candidate_id)     free, local, recomputes nothing else
  get_evidence()                    free, no requests
  run_stability_check(candidate_id) free, local: re-reads the waveform already
                                    stored for that station and reports whether
                                    its automatic onset is stable across a fixed
                                    grid of bands and thresholds. Use it when an
                                    onset looks clean but you are not sure it is
                                    reproducible.
  exclude_observation(candidate_id, reason)
                                    free, local: removes one acquired observation
                                    and RECOMPUTES every later check, returning a
                                    before/after for each. Use it when the user
                                    asks to drop a station or a flagged channel.

Method: call list_candidates first. Then acquire the observations you judge necessary, and after each acquisition call get_evidence and run_onset_check on what you acquired. Each acquire_station costs two real external requests, so acquire only what the question needs. Stop as soon as the evidence is decisive; if the question cannot be decided from the available observations, stop and say so rather than acquiring everything. If the user asks not to fetch anything new, work only from the stations already acquired.
${stated?`
Acceptance criterion you will be scored against: a shared propagating signal may be
concluded ONLY when at least 3 independent stations all show an onset inside the same
3-second window. With fewer than 3 analysed stations the propagation question is NOT
decidable, however clean the individual onsets look. A channel integrity flag at the
station under review settles the instrument question on its own. An observation whose
sample rate cannot support the requested band is unusable on its own.
`:''}
When finished, reply with ONLY this JSON and no other text:
{"conclusion":"<one label matching the question that was asked>","stop_reason":"<one short sentence>","stations_used":<integer>}

The allowed conclusion labels depend on what was asked:
  multi-station propagation : coherent_multistation | single_station_only | not_decidable
  instrument / metadata     : instrument_artifact | no_instrument_anomaly_in_scope | instrument_unknown
  band / pick usability     : pick_applicable | pick_partially_applicable | pick_not_applicable | pick_unknown
Use the *unknown variant of the group that matches the question when the evidence
does not settle the asked field. Never answer a band or instrument question with a
propagation label.`;

// Record the prompt exactly as it was built, and bind it to the frozen task.
await writeOnce(join(runDir,'prompt.txt'),prompt);
await writeOnce(join(runDir,'binding.json'),JSON.stringify({
  run_id:RUN,task_id:state.task_id,prompt_sha256:sha256(prompt),
  user_question_sha256:sha256(question),
  mcp_state_sha256:sha256(await readFile(join(runDir,'mcp_state.initial.json'),'utf8')),
  bound_at:new Date().toISOString()},null,2));

// Test hook: stop after the prompt and binding are on disk, before any daemon or
// model is touched. Used to verify offline that an edited question really reaches
// the prompt. Never set in a real run.
if(process.env.AGENT_DRY_RUN==='1'){
  console.log(JSON.stringify({RUN,status:'dry_run',prompt_sha256:sha256(prompt)}));
  process.exit(0);
}

let client,bridge,active,fatal=null,failureKind=null,sessionId=null,answer=null,transcript=[];
const began=Date.now();
try{
 await (await import('node:fs/promises')).mkdir(runDir,{recursive:true});
 const root=await mkdtemp('/tmp/eq-main-'),home=join(root,'h'),cwd=join(root,'w');
 await mkdir(join(home,'profiles/local-dev'),{recursive:true,mode:0o700});await mkdir(cwd);
 await writeFile(join(home,'profiles/local-dev/profile.yaml'),'name: local-dev\ncomputerUse:\n  enabled: false\n');
 const cliEntry=join(project,'vendor/agnes-harness/packages/cli/dist/local/agnes.mjs');
 const env={PATH:process.env.PATH,TMPDIR:'/tmp',AGH_HOME:home,AGNES_PROFILE:'local-dev',AGNES_MCP_STDIO_ALLOWLIST:join(project,'.venv/bin/python'),MPLCONFIGDIR:join(project,'.cache/matplotlib'),XDG_CACHE_HOME:join(project,'.cache')};
 const exec=(bin,args,options={})=>new Promise((resolve,reject)=>{const c=execFile(bin,args,{cwd,env,timeout:120000,maxBuffer:2**22,...options},(e,out,err)=>e?reject(Error('Subprocess failure '+String(err).slice(0,300))):resolve(out));c.stdin.end();});
 const cli=args=>exec(process.execPath,[cliEntry,...args]);
 await cli(['sessions','--json']);const owner=JSON.parse(await readFile(join(home,'data/daemon/owner.json'),'utf8'));
 client=createClient({transport:{kind:'unix',path:owner.socketPath},auth:{kind:'local'},journal:memoryJournal()});await client.initialize();
 bridge=await startBridge({project,provider,model,toolPrefix});
 await client.config.save({providerId:'deepseek',baseUrl:bridge.baseUrl,apiKey:bridge.apiKey,model,expectedRevision:0});
 const io={write:()=>{},confirm:async()=>true};
 const addArgs=['add',serverId,'--name',serverId,'--stdio',join(project,'.venv/bin/python'),
  '--arg',join(project,'main/mcp_server.py'),'--arg',RUN];
 // The allow-list is read from tools.json so it can never disagree with the
 // bridge's own guard about which tools exist.
 const toolSpec=JSON.parse(await readFile(join(project,'main/tools.json'),'utf8')).tools;
 for(const t of toolSpec)addArgs.push('--allow-tool',t.name);
 await runResourceCommand('mcp',addArgs,client,io);
 for(const action of ['trust','enable']){const r=await client.mcp.servers.get({profile:'local-dev',serverId});await runResourceCommand('mcp',[action,serverId,...(action==='trust'?['trusted']:[]),'--expected-revision',r.revision],client,io);}
 await client.workspace.add(cwd);const session=await client.session.new({cwd});sessionId=session.id??session.sessionId;
 session.onPermissionRequest(async r=>answerMainPermission(r,active,toolPrefix));await session.attach();
 active=bridge.begin({id:RUN,caseId:RUN,arm:'adaptive_agent_'+variant,candidateId:null,allowedCandidates:state.discovery.eligible.map(c=>c.candidate_id),maxRequests:24});
 await session.prompt(prompt,{signal:AbortSignal.timeout(600000)});
 for(const r of active.responses)transcript.push({content:r.content,tool_calls:(r.tool_calls??[]).map(c=>c.function?.name)});
 const final=active.responses.filter(r=>!r.tool_calls?.length).at(-1)?.content??'';
 answer=final.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
}catch(e){
 const raw=String(e.message).replace(/sk-[\w-]+/g,'[REDACTED]');
 fatal=raw.slice(0,300);
 failureKind=/timeout|abort/i.test(raw)?'timeout':'model_fatal';
 console.error('FATAL '+failureKind+' '+fatal);
}
finally{
 const execution=bridge?bridge.end():null;
 if(client)try{await client.close()}catch{};try{await cli(['daemon','stop'])}catch{};if(bridge)await bridge.close();
 const record={RUN,RUN_ID:RUN,variant,stated,model,sessionId,fatal,failure_kind:failureKind,
   status:fatal?'failed':'succeeded',answer,transcript,execution,elapsed_ms:Date.now()-began};
 const path=join(project,'main/out','agent_execution_'+variant+'_'+RUN+'.json');
 try{await writeFile(path,JSON.stringify(record,null,2),{mode:0o600});}catch{}
 console.log(JSON.stringify({RUN,status:record.status,failure_kind:failureKind,answer}));
 if(fatal)process.exitCode=failureKind==='timeout'?3:1;
}