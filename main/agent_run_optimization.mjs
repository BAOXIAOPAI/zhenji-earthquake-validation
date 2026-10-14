// Agent-optimisation arm driver: a real AGH session decides which observations to
// acquire, which checks to run and when to stop, for exactly one run id.
//
// This is the round's own driver. It differs from main/agent_run.mjs in the three
// places the execution book fixes:
//   * the prompt is read from a frozen template file (A0/A1 policy), not built
//     inline, so the two strategies can be diffed and hashed;
//   * every resource this process creates is owned, registered and closed once;
//   * the MCP child is started through main/mcp_launcher.py with an explicit
//     isolation config, and every output goes to the run's own directory.
//
// Exit codes: 0 succeeded, 1 session fatal, 2 usage error, 3 timeout.
import {execFile} from 'node:child_process';
import {mkdir,mkdtemp,readFile,writeFile,rm,readdir,stat,copyFile} from 'node:fs/promises';
import {existsSync,readFileSync,writeFileSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {join,resolve} from 'node:path';
import {createClient,memoryJournal} from '../vendor/agnes-harness/packages/sdk/src/index.node.ts';
import {runResourceCommand} from '../vendor/agnes-harness/packages/resource-control-cli/src/resources.ts';
import {mcpLocalToolPrefix} from '../vendor/agnes-harness/packages/base/src/mcp/naming.ts';
import {startBridge,RESOURCES} from './provider_bridge.mjs';
import {answerMainPermission} from './permission.mjs';
import {snapshotSqliteDb} from './native_archive.mjs';

const project=resolve('.'),RUN=process.argv[2],PROFILE=(process.argv[3]??'A1').toUpperCase();
if(!RUN)throw Error('Usage: node main/agent_run_optimization.mjs <run_id> <A0|A1|AT> [model]');
const model=process.argv[4]??process.env.AGENT_MODEL??'deepseek-flash';
const runlogRoot=process.env.RUNLOG_DIR?resolve(process.env.RUNLOG_DIR):join(project,'main/runlog');
const cacheRoot=process.env.EQ_CACHE_DIR?resolve(process.env.EQ_CACHE_DIR):join(project,'main/cache');
const outRoot=process.env.EQ_OUTPUT_DIR?resolve(process.env.EQ_OUTPUT_DIR):join(project,'delivery/agent-optimization-2026-10-11');
const offline=process.env.EQ_STRICT_OFFLINE&&process.env.EQ_STRICT_OFFLINE!=='0';
const serverId='seismic-review',slug='seismic-review';
const toolPrefix=mcpLocalToolPrefix(slug);
const runDir=join(runlogRoot,RUN);
const sha256=t=>createHash('sha256').update(t).digest('hex');
// Test-only mock provider: only the upstream model transport is replaced. The AGH
// daemon, MCP child, bridge guards and ownership records stay real.
const mockProvider=process.env.EQ_MOCK_PROVIDER?resolve(process.env.EQ_MOCK_PROVIDER):null;
const mockTransport=mockProvider?(await import(mockProvider)).transport:null;
if(mockProvider&&typeof mockTransport!=='function')throw Error('EQ_MOCK_PROVIDER must export a transport() function');

const POLICIES={A0:'main/prompts/agent_policy_v0.txt',A1:'main/prompts/agent_policy_v1.txt',
  AT:'main/prompts/agent_policy_v1.txt'};
const policyPath=POLICIES[PROFILE]??POLICIES.A1;
const THINKING={A0:{enabled:false,effort:null},A1:{enabled:false,effort:null},
  AT:{enabled:true,effort:'high'}}[PROFILE]??{enabled:false,effort:null};

const writeOnce=async(path,text)=>{
  try{await writeFile(path,text,{flag:'wx',mode:0o600});return true;}
  catch(e){if(e.code==='EEXIST')return false;throw e;}
};

const GOAL_LABEL={'three_component_pick':'three-component P/S pick (needs three healthy components)',
  'vertical_onset':'vertical-component onset check (needs a healthy Z channel only)'};
const TOOL_LINES=[
  '  list_candidates(limit)            free, no requests',
  '  acquire_station(candidate_id)     costs two real external requests',
  '  run_onset_check(candidate_id)     free, local: computes the station\'s first',
  '                                    STA/LTA onset from its stored waveform, or',
  '                                    returns the value already computed for the',
  '                                    current condition',
  '  get_evidence()                    free, no requests: current evidence, the',
  '                                    requested and actual conditions, the scope,',
  '                                    which evidence is stale, and the obligations.',
  '                                    Citable ids: quality:<id> and',
  '                                    applicability:<id> (measured at acquisition),',
  '                                    onset:<id> (only after it was computed),',
  '                                    stability:<id> (only for this round\'s grid)',
  '  run_stability_check(candidate_id) free, local: runs THIS round\'s declared',
  '                                    diagnostic grid over the stored waveform and',
  '                                    reports whether the onset is reproducible',
  '  task_status(stage)                free, local: progress facts, remaining',
  '                                    allowance and finish eligibility. stage is',
  '                                    "progress" or "finish"',
  '  exclude_observation(candidate_id, reason)',
  '                                    free, local: removes one acquired observation',
  '                                    and RECOMPUTES every later check',
].join('\n');

const OUTPUT_SCHEMA=JSON.stringify({
  field:"<the asked field this run delivered>",
  conclusion:"<one label matching the question that was asked>",
  condition_id:"<the condition_id served by get_evidence / task_status>",
  scope_candidate_ids:["<candidate ids you actually answered for>"],
  evidence_refs:["quality:<candidate_id>","applicability:<candidate_id>","onset:<candidate_id>","stability:<candidate_id>"],
  stop_reason_code:"<public_criterion_satisfied | scope_exhausted | material_missing | resource_limited | unanswered_no_data | coherence>",
  limitations:["<what this answer does not establish>"],
},null,1);

function buildPrompt(state){
  const template=readFileSync(join(project,policyPath),'utf8');
  const followUp=state.follow_up??null;
  const carried=state.carried_forward??[];
  const standard=state.operational_standard??null;
  const requested=state.requested_conditions??{};
  const scope=requested.scope??state.scope??null;
  const scopeSection=scope?`Reporting scope for this task: ${JSON.stringify(scope)}. Answer
within that scope and list the candidate ids you actually answered for. Do not silently
shrink a whole-network question to a single station.`:'';
  const conditionsSection=[
    `Requested conditions in force (condition_id ${requested.condition_id??'unknown'}):`,
    `  target band ${(requested.target_band_hz??state.target_band_hz??[]).join('-')} Hz`,
    `  method goal ${requested.method_goal??state.method_goal}`,
    `  diagnostic grid ${JSON.stringify(requested.diagnostic_bands_hz??state.diagnostic_bands_hz??null)}`,
    `  acquisition allowed: ${requested.acquisition_allowed===false?'NO — reuse stored observations only':'yes'}`,
    `  excluded candidates: ${JSON.stringify(requested.excluded_candidate_ids??[])}`,
    'A stability-grid change never rewrites the band the user asked about. Report the',
    'diagnostic grid you actually ran, not only the one that was requested.',
  ].join('\n');
  const replacements={
    '{{RUN}}':RUN,
    '{{TASK_ID}}':state.task_id??'',
    '{{QUESTION}}':state.user_question??'',
    '{{FOLLOW_UP_SECTION}}':followUp?`
They have seen your earlier answer and now add:

  "${followUp}"

The observations from that earlier session are already acquired and are listed for
you; reuse them. Do not fetch a station that is already listed. If this round
forbids new acquisition, that constraint is absolute.
`:'',
    '{{WINDOW_START}}':state.window_start??'',
    '{{WINDOW_END}}':state.window_end??'',
    '{{TARGET_BAND}}':(state.target_band_hz??[]).join('-'),
    '{{METHOD_GOAL}}':state.method_goal??'three_component_pick',
    '{{GOAL_LABEL}}':GOAL_LABEL[state.method_goal]??state.method_goal,
    '{{STANDARD_SECTION}}':standard?`The colleague's public operational standard for this question is:
  ${standard}
It is shared openly with every session (not a hidden answer key); decide and stop
consistently with it.
`:'',
    '{{CARRIED_SECTION}}':carried.length?`Already acquired in the earlier session: ${carried.join(', ')}.\n`:'',
    '{{SLUG}}':slug,
    '{{TOOLS_SECTION}}':TOOL_LINES,
    '{{SCOPE_SECTION}}':scopeSection,
    '{{CONDITIONS_SECTION}}':conditionsSection,
    '{{OUTPUT_SCHEMA}}':OUTPUT_SCHEMA,
  };
  let prompt=template;
  for(const [key,value] of Object.entries(replacements))prompt=prompt.split(key).join(value);
  if(/\{\{[A-Z_]+\}\}/.test(prompt))throw Error('unsubstituted placeholder in prompt template');
  return prompt;
}

// ------------------------------------------------------------- owned resources
// Everything this process creates is registered here, so a failure, a timeout and
// a normal exit all close the same set exactly once. The previous driver defined
// `cli` inside the try block and called it from `finally`, so the stop either
// threw or was skipped.
function ownedResources(){
  const items={client:null,bridge:null,session:null,root:null,home:null,
    owner:null,cli:null,inFlight:null,sessionId:null};
  let closed=false;
  let rootRemovalBlocked=false;
  const log=[];
  return {
    items,log,
    track(key,value){items[key]=value;return value;},
    get closed(){return closed;},
    // Once cleanup has timed out the process may keep unwinding in the background;
    // removing the owned home then would destroy the only material that can locate
    // the still-running daemon. This latch makes that impossible.
    blockRootRemoval(reason){rootRemovalBlocked=true;
      log.push({step:'block_root_removal',ok:true,detail:reason});},
    async closeOnce(reason){
      if(closed)return {already:true,reason,log};
      closed=true;
      const errors=[];
      const step=async(name,fn)=>{
        if(!fn)return null;
        try{const r=await fn();log.push({step:name,ok:true,detail:r??null});return true;}
        catch(e){const msg=String(e?.message??e).slice(0,300);
          log.push({step:name,ok:false,error:msg});errors.push(`${name}: ${msg}`);return false;}
      };
      // 1. stop new requests and cancel this run's in-flight work
      await step('abort_inflight',items.inFlight
        ?()=>{items.inFlight.abort();return 'aborted';}:null);
      // 2. detach and close the client so no further provider call is minted
      await step('detach_session',items.session?()=>items.session.detach():null);
      await step('close_client',items.client?()=>items.client.close():null);
      // 3. stop only THIS run's AGH daemon, with the same AGH_HOME identity
      let stopConfirmed=!items.cli;         // no daemon was started: nothing to stop
      if(items.cli){
        stopConfirmed=await step('stop_daemon',()=>items.cli(['daemon','stop']))===true;
      }
      // 4. close the loopback bridge (settles the budget lock)
      await step('close_bridge',items.bridge?()=>items.bridge.close():null);
      // 5. remove the temp tree ONLY when the daemon that owns it was confirmed
      //    stopped. Deleting it while the daemon still lives erases the home and
      //    owner id that are the only way to find and stop it afterwards.
      let rootRemoved=false;
      if(stopConfirmed&&!rootRemovalBlocked){
        rootRemoved=await step('remove_root',
          items.root?()=>rm(items.root,{recursive:true,force:true}):null)===true;
      }else{
        const why=(rootRemovalBlocked?'root removal was blocked after a cleanup timeout'
                                      :'daemon stop was not confirmed');
        log.push({step:'remove_root',ok:false,skipped:true,error:why});
        errors.push(`remove_root: skipped because ${why}`);
      }
      const cleanupNeeded=!stopConfirmed||errors.length>0;
      return {already:false,reason,log,cleanup_errors:errors,
        stop_confirmed:stopConfirmed,root_removed:rootRemoved,
        cleanup_needed:cleanupNeeded,
        cleanup_ok:errors.length===0&&stopConfirmed};
    },
  };
}

// One necessary piece of NATIVE AGH evidence, captured before cleanup removes the
// owned home: the session id, the daemon owner identity and an inventory (with
// hashes) of the harness-side session store. It is enough to tie this run to the
// real AGH session that produced it without rebuilding a bulk archive.
async function captureNativeEvidence(){
  const home=owned.items.home;
  const evidence={schema:'native-agh-evidence/v1',run_id:RUN,profile:PROFILE,
    agh_session_id:owned.items.sessionId,agh_home:home,owner:owned.items.owner,
    captured_at:new Date().toISOString(),files:[]};
  if(!home||!existsSync(home)){evidence.status='no_owned_home';}
  else{
    evidence.status='captured';
    const walk=async(dir,rel)=>{
      let entries=[];
      try{entries=await readdir(dir,{withFileTypes:true});}catch{return;}
      for(const entry of entries){
        const full=join(dir,entry.name),r=rel?rel+'/'+entry.name:entry.name;
        if(entry.isDirectory()){await walk(full,r);}
        else{
          let size=null,digest=null;
          try{const s=await stat(full);size=s.size;
            if(s.size<=2**20)digest=sha256(await readFile(full));
          }catch{}
          evidence.files.push({path:r,size,sha256:digest});
        }
      }
    };
    await walk(home,'');
    evidence.file_count=evidence.files.length;
    // The native AGH session store and host audit are copied out before cleanup:
    // they are the harness-side originals, not a re-serialisation by this driver.
    const sessionsDb=join(home,'data/sessions.db');
    const hostAudit=join(home,'data/audit/host.jsonl');
    if(existsSync(sessionsDb)){
      try{const snap=await snapshotSqliteDb(sessionsDb,join(runDir,'native_sessions.db'));
        if(snap.ok)evidence.native_sessions_db='native_sessions.db';
        else evidence.native_sessions_db_error=String(snap.error??'snapshot failed').slice(0,200);
      }catch(e){
        evidence.native_sessions_db_error=String(e?.message??e).slice(0,200);}
    }
    if(existsSync(hostAudit)){
      try{await copyFile(hostAudit,join(runDir,'native_host_audit.jsonl'));
        evidence.native_host_audit='native_host_audit.jsonl';}catch(e){
        evidence.native_host_audit_error=String(e?.message??e).slice(0,200);}
    }
    // One native tool/call + tool/result pair with its session key and sequence,
    // read straight from the AGH session store.
    if(existsSync(sessionsDb)){
      try{
        const {DatabaseSync}=await import('node:sqlite');
        const db=new DatabaseSync(sessionsDb,{readOnly:true});
        const rows=db.prepare("select session_key,seq,type,origin from events "
          +"where type in ('session/start','turn/start','tool/call','tool/result') "
          +"order by seq").all();
        db.close();
        evidence.native_events=rows.map(r=>({session_key:r.session_key,seq:r.seq,
          type:r.type,origin:r.origin}));
        const call=rows.find(r=>r.type==='tool/call');
        const result=rows.find(r=>r.type==='tool/result'&&(!call||r.seq>=call.seq));
        if(call&&result)evidence.native_tool_pair={session_key:call.session_key,
          call_seq:call.seq,result_seq:result.seq,result_origin:result.origin};
      }catch(e){evidence.native_sqlite_extract_error=String(e?.message??e).slice(0,200);}
    }
  }
  // The run's own MCP tool log pair, tied to this run id; kept clearly separate
  // from the native session-store events above.
  try{
    const logPath=join(runDir,'tool_calls.jsonl');
    if(existsSync(logPath)){
      const own=readFileSync(logPath,'utf8').split('\n').filter(Boolean)
        .map(line=>{try{return JSON.parse(line);}catch{return null;}})
        .filter(entry=>entry&&entry.run_id===RUN);
      const call=own.find(e=>e.phase==='start');
      const result=call?own.find(e=>e.phase!=='start'&&e.tool===call.tool):null;
      if(call)evidence.mcp_tool_pair={tool:call.tool,arguments:call.arguments,
        call_at:call.at,result_phase:result?.phase??null,
        result:result?.result??null,result_at:result?.at??null};
    }
  }catch(e){evidence.mcp_tool_pair_error=String(e?.message??e).slice(0,200);}
  await writeOnce(join(runDir,'native_agh_evidence.json'),
    JSON.stringify(evidence,null,2));
  return evidence;
}

let prompt='';
try{prompt=buildPrompt(JSON.parse(readFileSync(join(runDir,'mcp_state.initial.json'),'utf8')));}
catch(e){console.error('FATAL no run state for '+RUN+': '+e.message);process.exit(2);}

await writeOnce(join(runDir,'prompt.txt'),prompt);
await writeOnce(join(runDir,'prompt_binding.json'),JSON.stringify({
  run_id:RUN,profile:PROFILE,policy:policyPath,policy_sha256:sha256(readFileSync(join(project,policyPath),'utf8')),
  prompt_sha256:sha256(prompt),thinking:THINKING,
  mcp_state_sha256:sha256(readFileSync(join(runDir,'mcp_state.initial.json'),'utf8')),
  bound_at:new Date().toISOString()},null,2));

if(process.env.AGENT_DRY_RUN==='1'){
  console.log(JSON.stringify({RUN,profile:PROFILE,status:'dry_run',prompt_sha256:sha256(prompt)}));
  process.exit(0);
}

const owned=ownedResources();
let fatal=null,failureKind=null,answer=null,execution=null,cleanup=null;
let sessionTimer=null,sessionTimedOut=false,sessionTimeoutMs=0;
const began=Date.now();
const events=[];
try{
  await mkdir(runDir,{recursive:true});
  const root=await mkdtemp('/tmp/eq-opt-'),home=join(root,'h'),cwd=join(root,'w');
  owned.track('root',root);owned.track('home',home);
  await mkdir(join(home,'profiles/local-dev'),{recursive:true,mode:0o700});await mkdir(cwd);
  await writeFile(join(home,'profiles/local-dev/profile.yaml'),'name: local-dev\ncomputerUse:\n  enabled: false\n');
  const isolation=join(runDir,'mcp_isolation.json');
  const isolationCfg={RUNLOG_DIR:runlogRoot,EQ_CACHE_DIR:cacheRoot,
    EQ_STRICT_OFFLINE:offline?'1':'0',EQ_OUTPUT_DIR:outRoot,EQ_TASK_DIR:join(project,'main/tasks'),
    MPLCONFIGDIR:join(project,'.cache/matplotlib'),XDG_CACHE_HOME:join(project,'.cache')};
  writeFileSync(isolation,JSON.stringify(isolationCfg,null,2),{mode:0o600});
  await writeOnce(join(runDir,'mcp_isolation.summary.json'),
    JSON.stringify({keys:Object.keys(isolationCfg).sort(),
      sha256:sha256(JSON.stringify(isolationCfg)),strict_offline:!!offline},null,2));
  const cliEntry=join(project,'vendor/agnes-harness/packages/cli/dist/local/agnes.mjs');
  const env={PATH:process.env.PATH,TMPDIR:'/tmp',AGH_HOME:home,AGNES_PROFILE:'local-dev',
    AGNES_MCP_STDIO_ALLOWLIST:join(project,'.venv/bin/python'),
    MPLCONFIGDIR:join(project,'.cache/matplotlib'),XDG_CACHE_HOME:join(project,'.cache')};
  const exec=(bin,args,options={})=>new Promise((res,rej)=>{const c=execFile(bin,args,{cwd,env,timeout:120000,maxBuffer:2**22,...options},(e,out,err)=>e?rej(Error('Subprocess failure '+String(err).slice(0,300))):res(out));c.stdin.end();});
  const cli=args=>exec(process.execPath,[cliEntry,...args]);
  owned.track('cli',cli);
  await cli(['sessions','--json']);
  const owner=JSON.parse(await readFile(join(home,'data/daemon/owner.json'),'utf8'));
  owned.track('owner',owner);
  // A non-sensitive identity record, so a Python-level watchdog can stop this
  // daemon without guessing at process names.
  await writeOnce(join(runDir,'runtime-owner.json'),JSON.stringify({
    run_id:RUN,profile:PROFILE,pid:process.pid,agh_home:home,
    socket_path:owner.socketPath??null,owner_id:owner.id??null,
    started_at:new Date().toISOString()},null,2));
  const client=owned.track('client',createClient({transport:{kind:'unix',path:owner.socketPath},auth:{kind:'local'},journal:memoryJournal()}));
  await client.initialize();
  const bridge=owned.track('bridge',await startBridge({project,provider:'deepseek',model,toolPrefix,thinking:THINKING,
    // Test-only: a local mock provider replaces ONLY the upstream model transport.
    // The AGH daemon, the MCP child, the bridge guards and the resource ownership
    // are all real, so a free integration run still exercises the lifecycle.
    ...(mockProvider?{transport:mockTransport}:{})}));
  await client.config.save({providerId:'deepseek',baseUrl:bridge.baseUrl,apiKey:bridge.apiKey,model,expectedRevision:0});
  const io={write:()=>{},confirm:async()=>true};
  const addArgs=['add',serverId,'--name',serverId,'--stdio',join(project,'.venv/bin/python'),
    '--arg',join(project,'main/mcp_launcher.py'),'--arg','--run-id','--arg',RUN,
    '--arg','--config','--arg',isolation];
  const toolSpec=JSON.parse(await readFile(join(project,'main/tools.json'),'utf8')).tools;
  for(const t of toolSpec)addArgs.push('--allow-tool',t.name);
  await runResourceCommand('mcp',addArgs,client,io);
  for(const action of ['trust','enable']){
    const r=await client.mcp.servers.get({profile:'local-dev',serverId});
    await runResourceCommand('mcp',[action,serverId,...(action==='trust'?['trusted']:[]),'--expected-revision',r.revision],client,io);
  }
  await client.workspace.add(cwd);
  const session=owned.track('session',await client.session.new({cwd}));
  owned.track('sessionId',session.id??session.sessionId);
  session.onPermissionRequest(async r=>answerMainPermission(r,activeState(),toolPrefix));
  await session.attach();
  const controller=new AbortController();
  owned.track('inFlight',controller);
  // One declared session time limit (540 s), unifying cancellation with the
  // resource ownership record: when it fires, the same abort+cancel path runs and
  // the same owned daemon is stopped. Without it a hung AGH tool or session could
  // only be caught by the outer Python watchdog.
  sessionTimeoutMs=Number(process.env.EQ_SESSION_TIMEOUT_MS)||(RESOURCES.case_timeout_ms??540000);
  sessionTimer=setTimeout(()=>{sessionTimedOut=true;controller.abort();},sessionTimeoutMs);
  let active=null;
  const activeState=()=>active;
  const state=JSON.parse(readFileSync(join(runDir,'mcp_state.initial.json'),'utf8'));
  active=bridge.begin({id:RUN,caseId:RUN,arm:'adaptive_agent_'+PROFILE.toLowerCase(),
    candidateId:null,allowedCandidates:state.discovery.eligible.map(c=>c.candidate_id),
    maxRequests:RESOURCES.max_requests_per_case});
  const started=Date.now();
  await session.prompt(prompt,{signal:controller.signal});
  execution=bridge.end();
  if(sessionTimedOut){
    // The AGH session can resolve with an empty answer after an abort; a cancelled
    // session is a TIMEOUT, never a successful empty delivery.
    fatal=`session exceeded ${sessionTimeoutMs} ms and was cancelled`;
    failureKind='timeout';
    console.error('FATAL timeout '+fatal);
  }else{
    const final=(execution?.responses??[]).filter(r=>!r.tool_calls?.length).at(-1)?.content??'';
    answer=final.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
    events.push(...(execution?.responses??[]).map((r,i)=>({
      seq:i+1,request_id:r.requestId,model_requests:1,
      tool_names:(r.tool_calls??[]).map(c=>c.function?.name),
      tool_args_sha256:sha256(JSON.stringify((r.tool_calls??[]).map(c=>c.function?.arguments))),
      content_sha256:r.content?sha256(r.content):null,
      reasoning_present:!!r.reasoning_content,
      reasoning_chars:r.reasoning_content?String(r.reasoning_content).length:0,
      prompt_tokens:r.usage?.prompt_tokens??null,completion_tokens:r.usage?.completion_tokens??null,
    })));
    events.push({seq:events.length+1,kind:'bridge_summary',reasoning_restored:execution?.reasoningRestored??0,
      errors:(execution?.errors??[]).slice(0,10),elapsed_ms:Date.now()-started});
  }
}catch(e){
  const raw=String(e?.message??e).replace(/sk-[\w-]+/g,'[REDACTED]');
  fatal=raw.slice(0,300);
  failureKind=(sessionTimedOut||/timeout|abort/i.test(raw))?'timeout':'model_fatal';
  if(process.env.EQ_DEBUG_ERRORS==='1'){
    console.error('FATAL_DETAIL '+JSON.stringify({message:e?.message,code:e?.code,
      data:e?.data,stack:String(e?.stack??'').slice(0,1200)}));
  }
  console.error('FATAL '+failureKind+' '+fatal);
}finally{
  if(sessionTimer)clearTimeout(sessionTimer);
  // Test-only: keep the owned home for post-mortem inspection.
  if(process.env.EQ_KEEP_OWNED_HOME==='1')owned.blockRootRemoval('EQ_KEEP_OWNED_HOME');
  // Native AGH execution evidence is written while the owned home still exists:
  // it is the harness-side record of the session this run really created, so the
  // demo run remains identifiable even after a clean cleanup removes the home.
  let nativeEvidence=null;
  try{nativeEvidence=await captureNativeEvidence();}catch(e){
    nativeEvidence={run_id:RUN,status:'capture_failed',
      error:String(e?.message??e).slice(0,200)};}
  // Bounded cleanup grace: an upstream `daemon stop` can legitimately take ~35 s,
  // so 45 s is the declared bound. The primary error is preserved; a cleanup error
  // is recorded separately and never replaces it.
  const grace=Number(process.env.EQ_CLEANUP_GRACE_MS)||RESOURCES.cleanup_grace_ms||45000;
  let cleanupTimedOut=false;
  try{
    cleanup=await Promise.race([
      owned.closeOnce(fatal?'after_failure':'normal'),
      new Promise(res=>setTimeout(()=>{
        cleanupTimedOut=true;
        // Latch first: the losing closeOnce() keeps unwinding and must not remove
        // the home that still holds a possibly-live daemon.
        owned.blockRootRemoval('cleanup grace exceeded');
        res({timed_out:true,stop_confirmed:false,cleanup_ok:false,
             cleanup_needed:true,note:`cleanup exceeded ${grace} ms`});
      },grace)),
    ]);
  }catch(e){cleanup={already:false,cleanup_errors:[String(e?.message??e)],
    stop_confirmed:false,cleanup_ok:false,cleanup_needed:true};}
  const cleanupOk=(cleanup?.cleanup_ok===true)&&!cleanupTimedOut;
  const cleanupNeeded=cleanup?.cleanup_needed===true||!cleanupOk;
  // A cleanup that failed or timed out is NOT a full success: the run keeps its
  // original error and the cleanup state, and the exit code says so.
  const effectiveFailure=failureKind||(cleanupTimedOut?'cleanup_timeout'
    :(!cleanupOk?'cleanup_failed':null));
  let writeError=null;
  const runFailed=Boolean(fatal||!cleanupOk||cleanupTimedOut);
  const record={RUN,RUN_ID:RUN,profile:PROFILE,policy:policyPath,thinking:THINKING,model,
    sessionId:owned.items.sessionId,fatal,failure_kind:effectiveFailure,
    status:runFailed?'failed':'succeeded',answer,model_requests:execution?.requests??null,
    tool_proposals:(execution?.calls??[]).length,reasoning_restored:execution?.reasoningRestored??0,
    protocol_events:events,cleanup,cleanup_ok:cleanupOk,cleanup_needed:cleanupNeeded,
    owned_home:owned.items.home,root_removed:cleanup?.root_removed??false,
    session_timeout_ms:sessionTimeoutMs,
    session_timed_out:sessionTimedOut,
    native_agh_evidence:(nativeEvidence?.file_count??null),
    elapsed_ms:Date.now()-began};
  const recordPath=join(runDir,'agent_execution_'+PROFILE+'_'+RUN+'.json');
  try{await writeFile(recordPath,JSON.stringify(record,null,2),{mode:0o600});}
  catch(e){writeError=String(e?.message??e).slice(0,200);}
  // Outside the owned home on purpose: this file must survive the home being
  // preserved so an operator can find and stop the daemon it names.
  try{await writeFile(join(runDir,'cleanup_status.json'),JSON.stringify({
    run_id:RUN,profile:PROFILE,status:record.status,
    stop_confirmed:cleanup?.stop_confirmed??false,cleanup_ok:cleanupOk,
    cleanup_needed:cleanupNeeded,timed_out:cleanupTimedOut,
    owned_home:owned.items.home,root_removed:cleanup?.root_removed??false,
    errors:cleanup?.cleanup_errors??[],log:cleanup?.log??[],
    captured_at:new Date().toISOString()},null,2),{mode:0o600});}catch{}
  // Honest label: these are request/tool-proposal summaries synthesised from the
// bridge's own responses, NOT native AGH session events. The native tool/call and
// tool/result pair lives in native_agh_evidence.json / native_sessions.db.
  try{await writeFile(join(runDir,'bridge_request_summary.json'),JSON.stringify({
    schema:'bridge-request-summary/v1',
    note:'synthesised from provider-bridge responses; not native AGH session events',
    run_id:RUN,profile:PROFILE,agh_session_id:record.sessionId,
    model,thinking:THINKING,reasoning_restored:record.reasoning_restored,
    events:events},null,2),{mode:0o600});}catch{}
  console.log(JSON.stringify({RUN,profile:PROFILE,status:record.status,
    failure_kind:effectiveFailure,answer,cleanup_ok:cleanupOk,
    cleanup_needed:cleanupNeeded,stop_confirmed:cleanup?.stop_confirmed??false,
    write_error:writeError}));
  // A record that could not be written, a cleanup that failed, or a cleanup that
  // timed out all make the run non-successful at the exit-code level too.
  if(fatal||writeError||cleanupTimedOut||!cleanupOk){
    process.exitCode=(failureKind==='timeout'||cleanupTimedOut)?3:(cleanupOk?1:4);
  }
}