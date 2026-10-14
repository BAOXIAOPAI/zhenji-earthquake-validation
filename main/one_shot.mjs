// M 臂：同一模型、一次取得完整许可证据、一次调用。
//
// 指南 5.3 要求 M 组是"同一模型一次取得完整许可证据"。它与 A 臂的区别只有
// 一个：采集在模型介入之前由确定性代码一次做完，模型只看到一次、只回答一次。
// 提示词、证据字段白名单、验证器与 A 臂完全一致，否则比较不成立。
//
// 退出码：0 成功，1 会话失败，2 用法错误，3 超时。
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {writeFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
import {join,resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {Budget} from '../src/budget.mjs';

const HERE=dirname(fileURLToPath(import.meta.url));
const project=resolve(HERE,'..');
const RUN=process.argv[2], model=process.argv[3]??'deepseek-flash';
if(!RUN)throw Error('Usage: node main/one_shot.mjs <run_id> [model]');

// 与 A 臂同源：证据字段白名单直接从 MCP 服务读取，不在这里另抄一份。
const mcpSource=await readFile(join(project,'main/mcp_server.py'),'utf8');
const fieldBlock=mcpSource.match(/ACQUISITION_FIELDS\s*=\s*\(([\s\S]*?)\)/);
if(!fieldBlock)throw Error('ACQUISITION_FIELDS 未找到');
// 注释里出现的引号字符串是举例，不是字段名；不去掉会把它们当成字段。
const ACQ_FIELDS=[...fieldBlock[1].replace(/#[^\n]*/g,'')
  .matchAll(/"([a-z_]+)"/g)].map(m=>m[1]);

const runlogRoot=process.env.RUNLOG_DIR?resolve(process.env.RUNLOG_DIR)
                                    :join(project,'main/runlog');
const runDir=join(runlogRoot,RUN);
const state=JSON.parse(await readFile(join(runDir,'mcp_state.json'),'utf8'));

const pick=(record)=>{
  const out={};
  for(const f of ACQ_FIELDS){
    if(record[f]!==undefined&&record[f]!==null&&record[f]!=='')out[f]=record[f];
  }
  return out;
};

const evidence=(state.acquired||[]).map(pick);
// 本地可算的派生结果（到时聚集、稳定性网格）与上面同等对待：A 能通过工具取得，
// M 也必须看到，否则对照不公平。
const derived=state.derived_evidence??null;
const question=state.user_question??'';
const followUp=state.follow_up??null;
// 公开操作标准：三站/三秒这类判据提供给所有模型入口，
// 不是隐藏参考答案。缺省时该段整体省略。
const standard=state.operational_standard??null;

const prompt=`SEISMIC_REVIEW_RUN=${RUN}
TASK_ID=${state.task_id}

You are reviewing one seismic candidate window for a colleague. Their question is:

  "${question}"
${followUp?`
They have seen your earlier answer and now add:

  "${followUp}"
`:''}
The window under review is ${state.window_start} to ${state.window_end} UTC.
The band they asked about is ${state.target_band_hz[0]}-${state.target_band_hz[1]} Hz.
${standard?`The colleague's public operational standard for this question is:
  ${standard}
This standard is shared openly (it is not a hidden answer key); apply it as stated.
`:''}
ALL the observations you are permitted to use have already been acquired for you.
There are no tools to call and no further data available. Decide from this evidence:

${JSON.stringify(evidence,null,1)}

${
/* 本地派生结果与工具返回同源，故一并给出 */
''}The following local computations were run on the same waveforms. They cost no
external requests and are the same ones the interactive tools would return:

${JSON.stringify(derived,null,1)}

Decide from the evidence above. If the evidence does not settle the question, say so
with the specific reason rather than guessing.

Reply with ONLY this JSON and no other text:
{"conclusion":"<one label matching the question that was asked>","stop_reason":"<one short sentence>","stations_used":<integer>}

The allowed conclusion labels depend on what was asked:
  multi-station propagation : coherent_multistation | single_station_only | not_decidable
  instrument / metadata     : instrument_artifact | no_instrument_anomaly_in_scope | instrument_unknown
  band / pick usability     : pick_applicable | pick_partially_applicable | pick_not_applicable | pick_unknown
Use the unknown variant of the group that matches the question when the evidence
does not settle the asked field. Never answer a band or instrument question with a
propagation label.`;

// Test hook (mirrors agent_run.mjs): stop after the prompt is on disk and
// before any provider request, so the M prompt can be verified offline.
writeFileSync(join(runDir,'prompt.txt'),prompt);
if(process.env.ONE_SHOT_DRY_RUN==='1'){
  const {createHash}=await import('node:crypto');
  console.log(JSON.stringify({RUN,status:'dry_run',
    prompt_sha256:createHash('sha256').update(prompt).digest('hex')}));
  process.exit(0);
}

// ---- 预算与密钥：与 A 臂同一套台账，同一套上限 ----
const cfg=JSON.parse(await readFile(join(project,'analysis/model-budget.json'),'utf8'));
const keyText=await readFile(join(project,'..',process.env.PROVIDER_KEY_FILE||'deepseek_model.txt'),'utf8');
const keyMatch=keyText.match(/(?:Key|key)\s*[:：]\s*(\S+)/);
if(!keyMatch)throw Error('Key field unavailable');
const budget=new Budget(join(project,'runs/model-budget-ledger.json'),cfg);

// The ledger is guarded by a lock file that must be released. Leaving it behind
// blocks every later accounting operation, so release is unconditional.
const requestId=randomUUID();
const body={model,max_tokens:cfg.max_output_tokens,thinking:{type:'disabled'},
            messages:[{role:'user',content:prompt}]};
const record={request_id:requestId,provider:'deepseek',model,case_id:RUN,arm:'one_shot_model',
              request:body};
mkdir(join(project,'main/runs/live-requests'),{recursive:true,mode:0o700});
const recPath=join(project,'main/runs/live-requests',requestId+'.json');
let answer=null,fatal=null,failureKind=null;
const began=Date.now();
// 记账、调用与锁的释放必须成对：reserve 抛错也会留下锁文件，阻塞之后所有记账。
try{
budget.reserve(requestId,'deepseek',RUN);
await writeFile(recPath,JSON.stringify(record,null,2),{mode:0o600});
try{
  const r=await fetch('https://api.deepseek.com/chat/completions',{
    method:'POST',
    headers:{'Content-Type':'application/json',Authorization:`Bearer ${keyMatch[1]}`},
    body:JSON.stringify(body),
    signal:AbortSignal.timeout(Math.min(cfg.request_timeout_ms,600000)),
  });
  if(!r.ok)throw Error('Provider HTTP '+r.status);
  const resp=await r.json();
  budget.settle(requestId,resp.usage);
  const choice=resp.choices?.[0];
  if(!choice?.message)throw Error('Missing assistant response');
  if(choice.finish_reason==='length')throw Error('Output token limit');
  answer=(choice.message.content??'').trim();
  record.response=resp;
}catch(e){
  const raw=String(e.message).replace(/sk-[\w-]+/g,'[REDACTED]');
  fatal=raw.slice(0,300);
  failureKind=/timeout|abort/i.test(raw)?'timeout':'model_fatal';
  console.error('FATAL '+failureKind+' '+fatal);
}
record.answer=answer;record.fatal=fatal;record.elapsed_ms=Date.now()-began;
await writeFile(recPath,JSON.stringify(record,null,2),{mode:0o600});
}finally{budget.close();}

await writeFile(join(runDir,'model_response.json'),JSON.stringify({
  RUN, RUN_ID:RUN, variant:'one_shot_model', arm:'one_shot_model', model,
  status:fatal?'failed':'succeeded', failure_kind:failureKind, answer, fatal,
  prompt, prompt_sha256:(await import('node:crypto')).createHash('sha256')
    .update(prompt).digest('hex'),
  evidence_count:evidence.length, elapsed_ms:record.elapsed_ms,
},null,2),{mode:0o600});

console.log(JSON.stringify({RUN,status:fatal?'failed':'succeeded',
                            failure_kind:failureKind,answer}));
if(fatal)process.exitCode=failureKind==='timeout'?3:1;
