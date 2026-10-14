import {readFileSync,writeFileSync,renameSync,mkdirSync,openSync,closeSync,unlinkSync,statSync} from 'node:fs';
import {dirname} from 'node:path';
const alive=(pid)=>{try{process.kill(pid,0);return true}catch{return false}};
// node:fs 没有 sleepSync；用 Atomics.wait 阻塞当前线程。
const sleep=(ms)=>Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,ms);
export class Budget {
 constructor(path,config){this.path=path;this.config=config;mkdirSync(dirname(path),{recursive:true});this.lock=path+'.lock';this.acquireLock();try{this.state=JSON.parse(readFileSync(path,'utf8'));}catch(e){if(e.code!=='ENOENT')throw e;this.state={entries:[],halted:false};this.save();}this.recover();}
 // 锁必须能被后来者接管，否则一次崩溃就永久堵死所有付费记账。
 // 只有确认属主进程已不在时才抢占；空锁文件是"刚创建还没写入 PID"，
 // 用 mtime 加一个宽限期区分它与崩溃留下的空壳。
 acquireLock(){
  for(let attempt=0;attempt<3;attempt++){
   try{this.fd=openSync(this.lock,'wx',0o600);writeFileSync(this.fd,String(process.pid));return;}
   catch(e){
    if(e.code!=='EEXIST')throw e;
    let owner=null;
    try{owner=Number(readFileSync(this.lock,'utf8').trim());}catch{}
    const mine=Number.isSafeInteger(owner)&&owner>1;
    let stale=false;
    if(mine)stale=!alive(owner);
    else{try{stale=(Date.now()-statSync(this.lock).mtimeMs)>60000}catch{stale=true}}
    if(stale&&attempt<2){try{unlinkSync(this.lock)}catch{};continue}
    if(attempt<2){sleep(50+attempt*100);continue}
    throw Error('Budget ledger is locked by another live process');
   }
  }
 }
 // 挂起条目只可能来自"在 reserve 与 settle 之间死掉的进程"。
 // 按预留额全额计入是保守选择：请求可能已经被服务并计费了。
 recover(){
  let changed=false;
  for(const e of this.state.entries){
   if(e.status==='pending'){
    e.status='abandoned';e.cost_upper_cny=e.reserved_cny;
    e.abandoned_at=new Date().toISOString();
    e.abandoned_reason='process ended between reserve and settle; counted at reservation';
    changed=true;
   }
  }
  if(changed)this.save();
 }
 save(){writeFileSync(this.path+'.tmp',JSON.stringify(this.state,null,2),{mode:0o600});renameSync(this.path+'.tmp',this.path);}
 total(){return this.state.entries.reduce((s,e)=>s+(e.cost_upper_cny??e.reserved_cny),0);}
 reserve(id,provider,caseId){
  if(this.state.halted)throw Error('Ledger halted pending human review');
  if(this.state.entries.some(e=>e.id===id))throw Error('Duplicate request');
  const prior=this.state.entries.filter(e=>e.provider===provider);
  if(provider==='agnes'&&prior.length>=2)throw Error('Agnes probe limit');
  if(prior.filter(e=>e.case_id===caseId).length>=this.config.max_requests_per_case)throw Error('Case request limit');
  const reserve=provider==='deepseek'?(this.config.input_token_reservation*this.config.input_million_peak+this.config.max_output_tokens*this.config.output_million_peak)/1e6:0;
  // Apply the authorized limit; retain normal_stop as the legacy fallback when
  // guard_limit_cny is absent from an older configuration.
  const guard=this.config.guard_limit_cny??Math.min(this.config.total_limit,this.config.normal_stop);
  if(this.total()+reserve>guard)throw Error('Budget exhausted');
  this.state.entries.push({id,provider,case_id:caseId,status:'pending',reserved_cny:reserve});this.save();
 }
 settle(id,usage){
  const e=this.state.entries.find(e=>e.id===id);if(!e||e.status!=='pending')throw Error('Invalid settlement');
  if(!usage||!Number.isSafeInteger(usage.prompt_tokens)||!Number.isSafeInteger(usage.completion_tokens)||usage.prompt_tokens<0||usage.completion_tokens<0){this.state.halted=true;this.save();throw Error('Usage missing or invalid; reservation retained');}
  e.usage=usage;
  if(e.provider==='deepseek'){
   e.cost_upper_cny=(usage.prompt_tokens*this.config.input_million_peak+usage.completion_tokens*this.config.output_million_peak)/1e6;
   if(usage.prompt_tokens>this.config.input_token_reservation||usage.completion_tokens>this.config.max_output_tokens){this.state.halted=true;this.save();throw Error('Token reservation violated');}
  }else e.cost_upper_cny=0; // Unknown Agnes monetary cost, separately reported, never used to claim free.
  e.status='settled';this.save();
 }
 uncertain(id,reason){const e=this.state.entries.find(e=>e.id===id);if(e){e.status='uncertain';e.reason=reason;}this.state.halted=true;this.save();}
 close(){if(this._closed)return;this._closed=true;try{closeSync(this.fd);}catch{}try{unlinkSync(this.lock);}catch{}}
}
export function validateDecision(text,caseId,evidence){
 const clean=text.trim().replace(/^```(?:json)?\s*/,'').replace(/\s*```$/,'');
 const d=JSON.parse(clean);
 if(d.case_id!==caseId||!['earthquake','non_target','review'].includes(d.decision)||!Array.isArray(d.evidence_ids)||!d.evidence_ids.length||d.evidence_ids.some(x=>!evidence.has(x))||typeof d.reason!=='string'||typeof d.stop_reason!=='string')throw Error('Invalid decision or unknown evidence reference');
 return d;
}
