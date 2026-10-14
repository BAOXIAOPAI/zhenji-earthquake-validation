// AGH re-mints provider call IDs. Match its trusted title, require the arguments
// to correspond to a real provider proposal, and grant only this invocation.
import {readFileSync} from 'node:fs';
import {join,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

const HERE=dirname(fileURLToPath(import.meta.url));
const TOOLSPEC=JSON.parse(readFileSync(join(HERE,'tools.json'),'utf8')).tools;
const SPEC=Object.fromEntries(TOOLSPEC.map(t=>[t.name,{
 keys:t.args.map(a=>a.name),
 required:t.args.filter(a=>a.required).map(a=>a.name),
 candidateScoped:!!t.candidate_scoped,
}]));

export function answerMainPermission(request, active, toolPrefix){
 const reject=()=>{const o=request.options.find(o=>o.kind==='reject_once');return o?{optionId:o.optionId}:{verdict:'rejected'};};
 const a=request.toolCall?.rawInput;
 if(!active||request.toolCall?.kind!=='execute')return reject();
 const title=request.toolCall?.title??'';
 const tool=Object.keys(SPEC).find(n=>title.startsWith(toolPrefix+n+' '));
 if(!tool||!a)return reject();
 const spec=SPEC[tool];
 const keys=Object.keys(a);
 if(!keys.every(k=>spec.keys.includes(k)))return reject();
 if(!spec.required.every(k=>keys.includes(k)))return reject();
 if(spec.candidateScoped&&!active.allowedCandidates.includes(a.candidate_id))return reject();
 for(const arg of TOOLSPEC.find(t=>t.name===tool).args){
  const v=a[arg.name];
  if(v===undefined)continue;
  if(arg.type==='number'&&typeof v!=='number')return reject();
  if(arg.type==='string'&&typeof v!=='string')return reject();
  if(typeof v==='number'&&arg.min!==undefined&&(v<arg.min||v>arg.max))return reject();
 }
 active.approvedProviderCallIds??=[];active.approvedHarnessCallIds??=[];
 if(active.approvedHarnessCallIds.includes(request.toolCall.toolCallId))return reject();
 const full=toolPrefix+tool;
 const call=active.responses.flatMap(r=>r.tool_calls??[]).find(c=>{
  if(c.function?.name!==full||active.approvedProviderCallIds.includes(c.id))return false;
  try{const raw=JSON.parse(c.function.arguments??'{}');
   if(Object.keys(raw).length!==keys.length)return false;
   return keys.every(k=>raw[k]===a[k]);
  }catch{return false}
 });
 const option=request.options.find(o=>o.kind==='allow_once');
 if(!call||!option)return reject();
 active.approvedProviderCallIds.push(call.id);active.approvedHarnessCallIds.push(request.toolCall.toolCallId);
 return {optionId:option.optionId};
}