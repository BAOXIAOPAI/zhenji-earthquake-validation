"""Shared, real-data analysis engine. No reference picks or source classes exposed."""
from pathlib import Path
import os,sys,json,uuid,hashlib,time,fcntl,shutil
from datetime import datetime,timezone
from contextlib import contextmanager
ROOT=Path(__file__).resolve().parents[1]
os.environ.setdefault('MPLCONFIGDIR',str(ROOT/'.cache/matplotlib'));os.environ.setdefault('XDG_CACHE_HOME',str(ROOT/'.cache'))
for path in [ROOT/'src',ROOT/'src/stage4',ROOT/'src/stage5']:sys.path.insert(0,str(path))
import numpy as np,pandas as pd,h5py
from obspy import Trace,Stream,read,read_inventory,UTCDateTime
from obspy.signal.trigger import ar_pick
from waveform import read_trace
from quality import inspect as inspect_external
from analyze import calculate
from sample_grid import align_zne
JOBS=ROOT/'runs/stage6/jobs'
SOURCE_JOBS=json.loads((ROOT/'configs/stage4/retrieval-jobs.json').read_text())
CASES=sorted(set(j['case_id'] for j in SOURCE_JOBS))
PARAMS=json.loads((ROOT/'configs/stage5/arpick-diagnostic.json').read_text())['parameters']

def now():return datetime.now(timezone.utc).isoformat()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def atomic(path,value):
 path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False));os.replace(temp,path)
IMPORTS=ROOT/'runs/stage7/imports'
def imported(case):
 if not isinstance(case,str) or len(case)!=39 or not case.startswith('import_') or any(c not in '0123456789abcdef' for c in case[7:]):return None
 p=IMPORTS/case/'manifest.json'
 return json.loads(p.read_text()) if p.exists() else None
def case_list():
 return [{'case_id':c,'name':f'候选 {i+1:02d}','can_refresh':True} for i,c in enumerate(CASES)]+[{'case_id':d['case_id'],'name':d['name'],'can_refresh':False} for p in sorted(IMPORTS.glob('*/manifest.json')) for d in [json.loads(p.read_text())]]
def case_check(case):
 if case not in CASES and imported(case) is None:raise ValueError('Unknown development case')

def job_path(job):
 if len(job)!=32 or any(c not in '0123456789abcdef' for c in job):raise ValueError('Invalid run ID')
 return JOBS/(job+'.json')
def get_job(job):return json.loads(job_path(job).read_text())
@contextmanager
def job_lock(job):
 p=job_path(job)
 with p.with_suffix('.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX)
  yield
  fcntl.flock(lock,fcntl.LOCK_UN)
def update(job,**fields):
 with job_lock(job):
  value=get_job(job);value.update(fields);value['updated_at']=now();atomic(job_path(job),value)
 return value

def records(case):
 case_check(case)
 custom=imported(case)
 if custom:return [{'record_id':j['id'],'label':j['net']+'.'+j['sta'],'source':'本地导入观测 · 关联未经确认'} for j in custom['records']]
 return [{'record_id':'original','label':'原始台站','source':'miniPNW 本地开发数据'}]+[{'record_id':j['id'],'label':j['net']+'.'+j['sta'],'source':'EarthScope 已下载观测'} for j in SOURCE_JOBS if j['case_id']==case]

def source_paths(job):
 # Optional refreshed cache is separate from immutable prior experiment inputs.
 index=ROOT/'runs/stage6/cache-index.json'
 cached=json.loads(index.read_text()).get(job['id']) if index.exists() else None
 if cached:
  base=ROOT/cached['base'];return base.with_suffix('.mseed'),base.with_suffix('.xml'),cached['retrieved_at']
 base=ROOT/'data/raw/stage4'/job['id'];return base.with_suffix('.mseed'),base.with_suffix('.xml'),'2026-10-04'

def load_record(case,record_id,frozen=None):
 case_check(case)
 if record_id=='original':
  meta=pd.read_csv(ROOT/'evaluation/splits.csv');g=meta[(meta.case_id==case)&(meta.split=='train')]
  if len(g)!=1:raise ValueError('Prototype original record must be unique training record')
  r=g.iloc[0]
  with h5py.File(ROOT/'data/raw/pnw-mini/miniPNW_waveforms.hdf5','r') as f:x=read_trace(f,r.trace_name)[:,:15000]
  st=Stream([Trace(x[i].copy(),header={'network':r.station_network_code,'station':r.station_code,'channel':r.station_channel_code+c,'sampling_rate':100.,'starttime':UTCDateTime(r.trace_start_time)}) for i,c in enumerate('ENZ')])
  rows=[{'component':t.stats.channel[-1],'id':t.id,'finite':bool(np.isfinite(t.data).all()),'constant':bool(np.ptp(t.data)==0),'sampling_rate_hz':100.,'start_utc':str(t.stats.starttime),'samples':t.stats.npts,'metadata_ok':True} for t in st]
  provenance={'kind':'miniPNW processed training window','retrieved_at':'2026-10-02','array_sha256_float64':hashlib.sha256(np.ascontiguousarray(x).tobytes()).hexdigest(),'limits':'ENZ/100 Hz follow dataset convention; instrument response not present. First 150 seconds only.'}
 else:
  custom=imported(case)
  js=[j for j in (custom['records'] if custom else SOURCE_JOBS) if j['case_id']==case and j['id']==record_id]
  if len(js)!=1:raise ValueError('Record outside selected case')
  j=js[0]
  if frozen:
   m,xml=Path(frozen['waveform']),Path(frozen['metadata']);stamp=frozen['retrieved_at']
   if sha(m)!=frozen['waveform_sha256'] or sha(xml)!=frozen['stationxml_sha256']:raise ValueError('Frozen input hash mismatch')
  elif custom:
   base=Path(j['base']);m,xml,stamp=base.with_suffix('.mseed'),base.with_suffix('.xml'),j['retrieved_at']
   if sha(m)!=j['waveform_sha256'] or sha(xml)!=j['stationxml_sha256']:raise ValueError('Imported input hash mismatch')
  else:m,xml,stamp=source_paths(j)
  st=read(str(m));inv=read_inventory(str(xml));q=inspect_external(st,inv,j)
  rows=[]
  for r in q['channel_checks']:
   ok=all(r[k] for k in ['requested_identity','covers_requested_interval_within_one_sample','metadata_unique_epoch','rate_matches_metadata','canonical_ENZ_orientation','response_present'])
   rows.append({'component':r['id'][-1],'id':r['id'],'finite':r['finite'],'constant':r['constant'],'sampling_rate_hz':r['sample_rate_hz'],'start_utc':r['start'],'samples':r['samples'],'metadata_ok':ok,'failed_checks':[k for k in ['requested_identity','covers_requested_interval_within_one_sample','metadata_unique_epoch','rate_matches_metadata','canonical_ENZ_orientation','response_present'] if not r[k]]})
  provenance={'kind':'local imported observation' if custom else 'EarthScope cached real download','retrieved_at':stamp,'waveform_sha256':sha(m),'stationxml_sha256':sha(xml),'waveform_url':j.get('waveform_url'),'metadata_url':j.get('station_url'),'limits':'Native counts; not response-corrected physical amplitude. Station grouping supplied by curated index.'}
 z=[r for r in rows if r['component']=='Z'];gaps=st.get_gaps()
 valid=lambda r:r['finite'] and not r['constant'] and r['metadata_ok']
 zvalid=len(z)==1 and valid(z[0]) and not st.select(component='Z').get_gaps()
 three=sorted(r['component'] for r in rows)==['E','N','Z'] and all(valid(r) for r in rows) and not gaps
 grid=None;grid_error=None
 if three:
  try:_,_,grid=align_zne([st.select(component=c)[0] for c in 'ZNE'])
  except ValueError as e:grid_error=str(e)
 q={'record_id':record_id,'station':st[0].stats.network+'.'+st[0].stats.station,'channels':rows,'vertical_valid':bool(zvalid),'three_components_valid':bool(three),'phase_eligible':bool(three and grid is not None),'alignment':grid,'alignment_issue':grid_error,'gaps_or_overlaps':len(gaps),'provenance':provenance}
 return st,q

def waveform(case,record_id,frozen=None):
 st,q=load_record(case,record_id,frozen);origin=min(t.stats.starttime for t in st);channels=[]
 for t in st:
  x=t.data.astype(float);mean=float(np.nanmean(x));scale=max(float(np.nanmax(abs(x-mean))),1e-12);step=max(1,int(np.ceil(len(x)/750)));points=[]
  for i in range(0,len(x),step):
   y=(x[i:i+step]-mean)/scale;points.append([float(t.stats.starttime-origin)+(i+len(y)/2)/t.stats.sampling_rate,float(y.min()),float(y.max())])
  channels.append({'name':t.stats.channel[-1],'points':points,'scale_native_counts':scale,'constant':bool(np.ptp(x)==0)})
 return {'record_id':record_id,'origin_utc':str(origin),'duration_seconds':max(float(t.stats.endtime-origin) for t in st),'channels':channels,'quality':q}

def create_job(case,mode):
 case_check(case)
 if mode not in ['rules','agh']:raise ValueError('Invalid mode')
 job=uuid.uuid4().hex
 data={'id':job,'case_id':case,'mode':mode,'status':'queued','created_at':now(),'updated_at':now(),'records':records(case),'observations':{},'events':[],'unresolved':[],'cost_upper_cny':0,'model_used':False}
 data['frozen_inputs']={}
 custom=imported(case)
 for r in data['records']:
  rid=r['record_id']
  if rid=='original':continue
  j=next(j for j in (custom['records'] if custom else SOURCE_JOBS) if j['case_id']==case and j['id']==rid)
  if custom:
   base=Path(j['base']);m,x,stamp=base.with_suffix('.mseed'),base.with_suffix('.xml'),j['retrieved_at']
   if sha(m)!=j['waveform_sha256'] or sha(x)!=j['stationxml_sha256']:raise ValueError('Imported input hash mismatch')
  else:m,x,stamp=source_paths(j)
  base=JOBS.parent/'inputs'/job/rid;base.parent.mkdir(parents=True,exist_ok=True)
  shutil.copy2(m,base.with_suffix('.mseed'));shutil.copy2(x,base.with_suffix('.xml'))
  data['frozen_inputs'][rid]={'waveform':str(base.with_suffix('.mseed').resolve()),'metadata':str(base.with_suffix('.xml').resolve()),'retrieved_at':stamp,'waveform_sha256':sha(base.with_suffix('.mseed')),'stationxml_sha256':sha(base.with_suffix('.xml'))}
 atomic(JOBS.parent/'waveforms'/(job+'.json'),{'case_id':case,'records':[waveform(case,r['record_id'],data['frozen_inputs'].get(r['record_id'])) for r in data['records']]})
 atomic(job_path(job),data);return data

def execute(job,record_id,action):
 if action not in ['inspect','transients','phases']:raise ValueError('Unknown action')
 with job_lock(job):
  state=get_job(job)
  if state['status'] not in ['queued','running']:raise ValueError('Run already finished')
  if record_id not in [r['record_id'] for r in state['records']]:raise ValueError('Record outside run')
  old=state['observations'].get(record_id,{})
  if action in old:return old[action]
  if action!='inspect' and 'inspect' not in old:raise ValueError('Inspect record first')
  started=time.perf_counter()
  try:
   st,q=load_record(state['case_id'],record_id,state.get('frozen_inputs',{}).get(record_id))
   snapshot=json.loads((JOBS.parent/'waveforms'/(job+'.json')).read_text())
   expected=next(r['quality']['provenance'] for r in snapshot['records'] if r['record_id']==record_id)
   if q['provenance']!=expected:raise ValueError('Input provenance changed since task creation')
   if action=='inspect':result=q
   elif action=='transients':
    if not q['vertical_valid']:result={'status':'not_applicable','reason':'No valid Z observation'}
    else:result={'status':'computed','bands':[calculate(st.select(component='Z')[0],b)[0] for b in [[1,8],[3,12]]],'phase_confirmed':False}
   elif not q['phase_eligible']:result={'status':'not_applicable','reason':q['alignment_issue'] or 'Three valid oriented components required'}
   else:
    zne=[st.select(component=c)[0] for c in 'ZNE'];arrays,origin,alignment=align_zne(zne);fs=zne[0].stats.sampling_rate
    if PARAMS['f2']>=fs/2:raise ValueError('Band exceeds Nyquist')
    p,s=ar_pick(*[a.astype(float) for a in arrays],fs,**PARAMS)
    vp=bool(np.isfinite(p) and 0<p<len(arrays[0])/fs);vs=bool(vp and np.isfinite(s) and p<s<len(arrays[0])/fs)
    result={'status':'computed','method':'AR-AIC + STA/LTA','p_utc':str(origin+p) if vp else None,'s_utc':str(origin+s) if vs else None,'raw_p_seconds':float(p) if np.isfinite(p) else None,'raw_s_seconds':float(s) if np.isfinite(s) else None,'p_available':vp,'s_available':vs,'alignment':alignment,'sampling_rate_hz':fs,'parameters':PARAMS,'phase_confirmed':False}
  except Exception as exc:result={'status':'error','reason':str(exc)[:250]}
  state['observations'].setdefault(record_id,{})[action]=result
  state['status']='running';state['updated_at']=now();state['events'].append({'at':now(),'record_id':record_id,'action':action,'status':result.get('status','checked'),'elapsed_ms':round((time.perf_counter()-started)*1000,2)})
  atomic(job_path(job),state)
 return result

def finalize(job,error=None):
 with job_lock(job):
  state=get_job(job);missing=[];unresolved=[]
  for r in state['records']:
   rid=r['record_id'];o=state['observations'].get(rid,{});q=o.get('inspect',{})
   if not q or q.get('status')=='error':missing.append(rid+':inspect');continue
   for action,needed in [('transients',q['vertical_valid']),('phases',q['phase_eligible'])]:
    if needed and o.get(action,{}).get('status')!='computed':missing.append(rid+':'+action)
   reasons={'requested_identity':'台站或通道身份不匹配','covers_requested_interval_within_one_sample':'波形未覆盖要求的时间窗','metadata_unique_epoch':'元数据时段不唯一或缺失','rate_matches_metadata':'元数据采样率缺失或与波形不一致','canonical_ENZ_orientation':'分量方向缺失或不符合 ENZ 条件','response_present':'缺少仪器响应'}
   for channel in q['channels']:
    for check in channel.get('failed_checks',[]):unresolved.append(channel['id']+'：'+reasons[check]+'。')
   flat=[c['component'] for c in q['channels'] if c['constant']]
   if flat:unresolved.append(q['station']+'：'+','.join(flat)+' 通道恒定；未推断成因，未恢复原通道。')
   if not q['phase_eligible']:unresolved.append(q['station']+'：不满足三分量拾取条件，仅保留适用检查。')
   phase=o.get('phases',{})
   if phase.get('status')=='computed' and not phase['s_available']:unresolved.append(q['station']+'：未取得有效 S 到时，不能将零返回值当成到时。')
   if phase.get('status')=='computed' and not phase['p_available']:unresolved.append(q['station']+'：未取得有效 P 到时。')
  state.update(status='failed' if error else 'incomplete' if missing else 'completed',finished_at=now(),missing_actions=missing,unresolved=unresolved,error=error)
  state['summary']='适用检查已完成，结果待专业复核。' if not missing and not error else '核查未完成；已取得的结果保留，请查看缺失步骤。'
  state['scientific_status']='unconfirmed';state['limitations']=['算法到时尚未经专业确认，不能据此判定天然地震或自动剔除事件。','台站分组由导入者提供；未实现独立事件关联。' if imported(state['case_id']) else '追加台站来自已知目录分组；未实现未知事件自动关联。','导入观测的台站分组未经确认；没有独立泛化评价。' if imported(state['case_id']) else '示例属于已见开发案例；没有运行最终测试。']
  atomic(job_path(job),state)
 return state

def run_rules(job):
 update(job,status='running')
 try:
  for r in get_job(job)['records']:
   rid=r['record_id'];q=execute(job,rid,'inspect')
   if q.get('vertical_valid'):execute(job,rid,'transients')
   if q.get('phase_eligible'):execute(job,rid,'phases')
  return finalize(job)
 except Exception as exc:return finalize(job,str(exc)[:250])

def markdown(state):
 lines=['# 候选事件核查记录','',f"案例：{state['case_id']}",f"运行：{state['id']}",f"模式：{state['mode']}；状态：{state['status']}",'',state.get('summary','尚未完成'),'']
 for rid,o in state['observations'].items():
  lines+=['## '+rid,'','```json',json.dumps(o,ensure_ascii=False,indent=2),'```','']
 lines+=['## 未解决事项','']+['- '+s for s in state.get('unresolved',[])]+['','## 适用边界','']+['- '+s for s in state.get('limitations',[])];return '\n'.join(lines)

if __name__=='__main__':
 if sys.argv[1]=='rules':
  j=create_job(sys.argv[2],'rules');print(json.dumps(run_rules(j['id']),ensure_ascii=False))
 elif sys.argv[1]=='update':print(json.dumps(update(sys.argv[2],**json.loads(sys.argv[3])),ensure_ascii=False))
 elif sys.argv[1]=='finalize':print(json.dumps(finalize(sys.argv[2],sys.argv[3] if len(sys.argv)>3 else None),ensure_ascii=False))
