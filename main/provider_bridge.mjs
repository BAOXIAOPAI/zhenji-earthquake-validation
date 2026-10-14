// Loopback billing/tool/resource guard for the main-line adaptive arm.
// AGH still owns the model/tool loop and MCP execution; this only enforces the
// cumulative budget ledger, the run-scoped tool allowlist, the one resolved set of
// execution limits, and (for the thinking profile) the DeepSeek reasoning-content
// protocol.
import {createServer} from 'node:http';
import {randomBytes,randomUUID,createHash} from 'node:crypto';
import {readFileSync,writeFileSync,mkdirSync,renameSync} from 'node:fs';
import {join,dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {Budget} from '../src/budget.mjs';

// The tool surface comes from main/tools.json, not from a list copied into this
// file. A hard-coded copy here once rejected a tool the model could legitimately
// see, which killed a whole session with 'Tool outside allowlist'.
const HERE=dirname(fileURLToPath(import.meta.url));
export const TOOLSPEC=JSON.parse(readFileSync(join(HERE,'tools.json'),'utf8')).tools;
const TOOLS=new Map(TOOLSPEC.map(t=>[t.name,t]));
// The exact names this run may offer. Exported so a test can pin it: deriving the
// allow-list from a Map with spread silently produced pairs, not names, and every
// session died with 'No run-scoped tool offered'.
export const runScopedTools=toolPrefix=>TOOLSPEC.map(t=>toolPrefix+t.name);

// The ONE resource config (analysis/model-budget.json). Python reads the same
// file through main/resource_limits.py; neither side invents its own number.
export const RESOURCES=JSON.parse(
  readFileSync(join(HERE,'..','analysis','model-budget.json'),'utf8'));
const LIMITS={
  maxRequests: RESOURCES.max_requests_per_case,
  softRequests: RESOURCES.soft_request_line,
  maxToolCalls: RESOURCES.max_tools_per_case,
  maxOutputTokens: RESOURCES.max_output_tokens,
  maxOutputTokensFallback: RESOURCES.max_output_tokens_fallback,
  requestTimeoutMs: RESOURCES.request_timeout_ms,
  caseTimeoutMs: RESOURCES.case_timeout_ms,
};

export function readKey(path){const s=readFileSync(path,'utf8');const m=s.match(/(?:Key|key)\s*[:：]\s*(\S+)/);if(!m)throw Error('Key field unavailable');return m[1];}

const suffixOf=(name,prefix)=>name?.startsWith(prefix)?name.slice(prefix.length):null;

export function checkTool(call,active,toolPrefix){
 const n=call.function.name;const s=suffixOf(n,toolPrefix);
 const spec=s?TOOLS.get(s):null;
 if(!spec)throw Error('Tool outside allowlist');
 let a;try{a=JSON.parse(call.function.arguments??'{}');}catch{throw Error('Invalid tool arguments');}
 const allowed=new Set(spec.args.map(x=>x.name));
 for(const key of Object.keys(a)){if(!allowed.has(key))throw Error('Unknown argument');}
 for(const arg of spec.args){if(arg.required&&a[arg.name]===undefined)throw Error('Missing argument '+arg.name);}
 for(const arg of spec.args){
  const v=a[arg.name];
  if(v===undefined)continue;
  if(arg.type==='number'&&typeof v!=='number')throw Error('Bad argument '+arg.name);
  if(arg.type==='string'&&typeof v!=='string')throw Error('Bad argument '+arg.name);
  if(typeof v==='number'&&arg.min!==undefined&&(v<arg.min||v>arg.max))throw Error('Bad argument '+arg.name);
 }
 if(spec.candidate_scoped&&!active.allowedCandidates.includes(a.candidate_id))throw Error('Station outside this plan');
 // The tool ceiling comes from the single resource config, not from a second
 // hard-coded number that no document described.
 if(++active.toolCalls>LIMITS.maxToolCalls)throw Error('Tool call limit');
 return {name:n,args:a};
}

// --------------------------------------------------------------- thinking mode
// DeepSeek's thinking mode requires the full assistant history — including the
// `reasoning_content` of turns that did not call a tool — to be returned on every
// follow-up request. AGH rebuilds context from its own store and can drop that
// field, so the bridge keeps the ORIGINAL provider replies for this run only and
// restores the field on the way out. It never fabricates an empty string for a
// turn it cannot match: an unmatched or ambiguous history fails loudly.
const digestOf=value=>createHash('sha256').update(JSON.stringify(value??null)).digest('hex');

export function rememberAssistant(active,message){
  if(!message)return;
  const calls=message.tool_calls??[];
  active.reasoning.push({
    content: message.content??null,
    tool_call_ids: calls.map(c=>c.id).filter(Boolean).sort(),
    tool_names: calls.map(c=>c.function?.name).filter(Boolean),
    args_digest: digestOf(calls.map(c=>[c.id,c.function?.name,c.function?.arguments])),
    reasoning_content: message.reasoning_content??null,
  });
}

export function restoreReasoning(messages,active,{required=false}={}){
  if(!active.reasoning.length)return {restored:0,missing:0};
  const assistantIdx=[];
  messages.forEach((m,i)=>{if(m.role==='assistant')assistantIdx.push(i);});
  let restored=0,missing=0;
  const used=new Set();
  assistantIdx.forEach((idx,order)=>{
    const msg=messages[idx];
    const calls=msg.tool_calls??[];
    const ids=calls.map(c=>c.id).filter(Boolean).sort();
    const sameIds=(a,b)=>a.length===b.length&&a.every((v,i)=>v===b[i]);
    let candidates=active.reasoning
      .map((entry,i)=>({entry,i}))
      .filter(({i,entry})=>!used.has(i)&&(
        (ids.length&&sameIds(ids,entry.tool_call_ids)) ||
        (!ids.length&&!entry.tool_call_ids.length&&
          (msg.content??'')===(entry.content??''))));
    // Same content, different tool ids must not be confused: narrow by content too.
    if(candidates.length>1&&ids.length){
      const narrowed=candidates.filter(({entry})=>
        (msg.content??'')===(entry.content??''));
      if(narrowed.length)candidates=narrowed;
    }
    if(candidates.length!==1){
      missing++;
      if(required)throw Error(
        `Ambiguous reasoning history at assistant #${order+1}: `+
        `${candidates.length} candidates`);
      return;
    }
    const {entry,i}=candidates[0];used.add(i);
    if(entry.reasoning_content){
      msg.reasoning_content=entry.reasoning_content;
      restored++;
    }else if(msg.reasoning_content===undefined){
      // No reasoning for this turn (e.g. a non-thinking profile, or a turn the
      // provider genuinely returned without one). Nothing to restore and nothing
      // invented.
    }
  });
  if(required&&missing&&!restored)throw Error('Reasoning history missing');
  return {restored,missing};
}

export async function startBridge({project,provider,model,toolPrefix,transport=fetch,
                                  thinking=null}={}){
 const cfg=JSON.parse(readFileSync(join(project,'analysis/model-budget.json'),'utf8'));
 const key=readKey(join(project,'..',provider==='deepseek'?'deepseek_model.txt':'key及通知.txt'));
 const base='https://api.deepseek.com';
 // 严格离线时拒绝模型 provider 外联。离线下模型轨迹只能回放已记录
 // 的运行（runlog），不得声称离线执行了真实模型。
 const offline=process.env.EQ_STRICT_OFFLINE&&process.env.EQ_STRICT_OFFLINE!=='0';
 // Test-only redirection so a mock-provider integration run never writes into the
 // real budget ledger. The default path is unchanged.
 const ledgerPath=process.env.EQ_BUDGET_LEDGER?resolve(process.env.EQ_BUDGET_LEDGER)
   :join(project,'runs/model-budget-ledger.json');
 const budget=new Budget(ledgerPath,cfg);
 // Reserve each request against the configured cost limit.
 const stageGuard=cfg.guard_limit_cny??cfg.total_limit;
 // The profile decides whether thinking is enabled. `disabled` remains selectable
 // and is the default, but it is no longer forced: a disabled profile is a
 // declared configuration, not a hidden override.
 const thinkingCfg=thinking??{
   enabled: process.env.AGENT_THINKING==='enabled'||process.env.AGENT_THINKING==='1',
   effort: process.env.AGENT_REASONING_EFFORT||null};
 const token=randomBytes(32).toString('hex');let active=null;let busy=false;let closed=false;
 const folder=process.env.EQ_REQUEST_LOG_DIR?resolve(process.env.EQ_REQUEST_LOG_DIR)
   :join(project,'main/runs/live-requests');mkdirSync(folder,{recursive:true,mode:0o700});
 const runlogRoot=process.env.RUNLOG_DIR?process.env.RUNLOG_DIR:join(project,'main/runlog');
 const maxOutputTokens=()=>{
   const v=cfg.max_output_tokens;
   return v===undefined?LIMITS.maxOutputTokens:v;};
 const publishMetrics=()=>{
   if(!active)return;
   const dir=join(runlogRoot,active.id);mkdirSync(dir,{recursive:true});
   const payload={run_id:active.id,arm:active.arm,
     model_requests:active.requests,model_requests_limit:LIMITS.maxRequests,
     model_requests_soft_line:LIMITS.softRequests,
     tool_proposed:active.calls.length,tool_calls_limit:LIMITS.maxToolCalls,
     thinking:thinkingCfg.enabled?{type:'enabled',effort:thinkingCfg.effort??null}
                                :{type:'disabled'},
     updated_at:new Date().toISOString()};
   const tmp=join(dir,'runtime_metrics.json.tmp');
   writeFileSync(tmp,JSON.stringify(payload,null,2),{mode:0o600});
   renameSync(tmp,join(dir,'runtime_metrics.json'));
 };
 const localReply=(res,body,content,toolCalls,reasoning)=>{
  const e={id:'bridge-'+randomUUID(),object:'chat.completion.chunk',created:0,model};
  res.writeHead(200,{'Content-Type':'text/event-stream'});
  const delta={role:'assistant'};
  // Final text and the reasoning field stay separate on the wire.
  if(reasoning)delta.reasoning_content=reasoning;
  if(toolCalls?.length)delta.tool_calls=toolCalls.map((t,index)=>({...t,index}));
  else delta.content=content;
  res.write('data: '+JSON.stringify({...e,choices:[{index:0,delta,finish_reason:null}]})+'\n\n');
  res.end('data: '+JSON.stringify({...e,choices:[{index:0,delta:{},finish_reason:toolCalls?.length?'tool_calls':'stop'}],usage:body.usage??{prompt_tokens:0,completion_tokens:0,total_tokens:0}})+'\n\ndata: [DONE]\n\n');
 };
 const server=createServer(async(req,res)=>{
  if(req.headers.authorization!==`Bearer ${token}`){res.writeHead(401).end();return;}
  if(req.method==='GET'&&req.url==='/v1/models'){res.setHeader('Content-Type','application/json');res.end(JSON.stringify({data:[{id:model}]}));return;}
  if(req.method!=='POST'||req.url!=='/v1/chat/completions'){res.writeHead(404).end();return;}
  let requestId,upstreamSent=false,ownsBusy=false;const requestStart=Date.now();
  try{
   const parts=[];for await(const c of req){parts.push(c);if(parts.reduce((s,b)=>s+b.length,0)>2e6)throw Error('Payload too large');}
   const body=JSON.parse(Buffer.concat(parts).toString());
    if(offline){res.writeHead(503,{'Content-Type':'application/json'}).end(JSON.stringify({error:{message:'strict offline: model provider refused (EQ_STRICT_OFFLINE=1); replay recorded runs instead',type:'offline_refused'}}));return;}
   if(!active||!JSON.stringify(body.messages).includes('SEISMIC_REVIEW_RUN='+active.caseId)){localReply(res,{},'Local validation session');return;}
   if(busy)throw Error('Concurrent request refused');busy=true;ownsBusy=true;
   if(Date.now()>active.deadline)throw Error('Case deadline');
   if(active.requests>=active.maxRequests)throw Error('Case request limit');
   active.requests++;
   // Offer only this run's own tools: AGH otherwise discloses the whole harness
   // toolset (shell, files, mcp_manage) and the model would be free to leave scope.
   const allow=new Set(runScopedTools(toolPrefix));
   const offered=(body.tools??[]).map(t=>t.function?.name).filter(Boolean);
   body.tools=(body.tools??[]).filter(t=>allow.has(t.function?.name));
   if(!body.tools.length)throw Error('No run-scoped tool offered');
   active.offeredUnscoped=offered.filter(n=>!allow.has(n));
   delete body.tool_choice;delete body.stream_options;delete body.max_completion_tokens;
   body.model=model;body.stream=false;body.max_tokens=maxOutputTokens();
   if(thinkingCfg.enabled){
     body.thinking={type:'enabled'};
     if(thinkingCfg.effort)body.reasoning_effort=thinkingCfg.effort;
     // Protocol adaptation, not a second agent loop: restore the reasoning field
     // AGH's context rebuild dropped, using THIS run's original replies only.
     const r=restoreReasoning(body.messages??[],active,{required:true});
     active.reasoningRestored=r.restored;
   }else{
     body.thinking={type:'disabled'};delete body.reasoning_effort;
   }
   delete body.temperature;delete body.top_p;
   if(budget.total()>stageGuard)throw Error('Stage internal cost stop');
   requestId=randomUUID();budget.reserve(requestId,provider,active.id);
   const record={request_id:requestId,provider,model,case_id:active.caseId,arm:active.arm,
     thinking:thinkingCfg,reasoning_restored:active.reasoningRestored??0,request:body};
   writeFileSync(join(folder,requestId+'.json'),JSON.stringify(record,null,2),{mode:0o600});
   upstreamSent=true;
   const r=await transport(base+'/chat/completions',{method:'POST',headers:{'Content-Type':'application/json',Authorization:`Bearer ${key}`},body:JSON.stringify(body),signal:AbortSignal.timeout(Math.min(cfg.request_timeout_ms,active.deadline-Date.now())),redirect:'error'});
   if(!r.ok)throw Error('Provider HTTP '+r.status);
   const answer=await r.json();budget.settle(requestId,answer.usage);
   record.response=answer;record.elapsed_ms=Date.now()-requestStart;writeFileSync(join(folder,requestId+'.json'),JSON.stringify(record,null,2),{mode:0o600});
   const choice=answer.choices?.[0];if(!choice?.message)throw Error('Missing assistant response');
   if(choice.finish_reason==='length')throw Error('Output token limit');
   const message=choice.message;const proposed=message.tool_calls??[];
   for(const call of proposed){const checked=checkTool(call,active,toolPrefix);active.calls.push(checked);}
   // Remember the ORIGINAL reply (reasoning included) for this run's later turns.
   rememberAssistant(active,message);
   active.responses.push({requestId,usage:answer.usage,content:message.content,
     reasoning_content:message.reasoning_content??null,tool_calls:proposed});
   publishMetrics();
   localReply(res,answer,message.content??'',proposed,message.reasoning_content??null);
  }catch(e){
   const safe=String(e.message).replace(/sk-[\w-]+/g,'[REDACTED]');
   if(active)active.errors.push(safe);
   if(upstreamSent&&requestId&&budget.state.entries.find(e=>e.id===requestId)?.status==='pending')budget.uncertain(requestId,'Transport/HTTP/usage failure');
   res.writeHead(400,{'Content-Type':'application/json'}).end(JSON.stringify({error:{message:safe,type:'validation_guard'}}));
  }finally{if(ownsBusy)busy=false;}
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 return {baseUrl:`http://127.0.0.1:${server.address().port}/v1`,apiKey:token,budget,
  limits:LIMITS,thinking:thinkingCfg,
  begin({id,caseId,arm,candidateId=null,allowedCandidates=[],maxRequests=LIMITS.maxRequests}){if(busy)throw Error('Still running');active={id,caseId,arm,candidateId,allowedCandidates,maxRequests,requests:0,toolCalls:0,calls:[],responses:[],errors:[],reasoning:[],reasoningRestored:0,deadline:Date.now()+cfg.case_timeout_ms};publishMetrics();return active;},
  end(){const result=active;active=null;return result;},
  async close(){if(closed){return {already:true};}closed=true;server.closeAllConnections();await new Promise(r=>server.close(r));budget.close();return {already:false};}
 };
}
