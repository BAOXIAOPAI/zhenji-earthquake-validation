"""Bounded recovery baseline: fixed sources, real feedback, immutable run copies."""
from pathlib import Path
from urllib.parse import urlsplit
import json,sys,shutil,subprocess,uuid,time
import numpy as np
from obspy import read,read_inventory
import engine,intake
from quality import inspect
from sample_grid import align_zne
ROOT=engine.ROOT
OUT=ROOT/'runs/stage9/recovery'
CHECKS=['requested_identity','covers_requested_interval_within_one_sample','metadata_unique_epoch','rate_matches_metadata','canonical_ENZ_orientation','response_present']

def assess(base,record):
 st=read(str(base.with_suffix('.mseed')));inv=read_inventory(str(base.with_suffix('.xml')));q=inspect(st,inv,record)
 def valid(r):return r['finite'] and not r['constant'] and all(r[k] for k in CHECKS)
 zs=[r for r in q['channel_checks'] if r['id'].endswith('Z')]
 q['vertical_valid']=bool(len(zs)==1 and valid(zs[0]) and not st.select(component='Z').get_gaps())
 q['phase_eligible']=False
 if q['three_component_checks_pass']:
  try:_,_,q['alignment']=align_zne([st.select(component=c)[0] for c in 'ZNE']);q['phase_eligible']=True
  except ValueError as e:q['alignment_issue']=str(e)
 q['failed_checks']=sorted({k for r in q['channel_checks'] for k in CHECKS if not r[k]})
 return q

def download(url,path):
 u=urlsplit(url)
 if u.scheme!='https' or u.netloc!='service.earthscope.org' or u.path not in ['/fdsnws/station/1/query','/fdsnws/dataselect/1/query']:raise ValueError('Source outside fixed service')
 p=subprocess.run(['curl','-L','--fail','--silent','--show-error','--max-time','45','--max-filesize','10485760','-o',str(path),url],capture_output=True,text=True,timeout=50)
 if p.returncode:raise ValueError('Download failed: '+p.stderr[:240])

def run(spec,fetch=download,transport='live',out=None):
 if spec['goal'] not in ['vertical','three_components']:raise ValueError('Unknown goal')
 folder=Path(out or OUT)/uuid.uuid4().hex;folder.mkdir(parents=True)
 state={'id':folder.name,'scenario':spec['id'],'goal':spec['goal'],'transport':transport,'started_at':engine.now(),'status':'running','events':[],'requests':0,'human_interventions_after_contract':0,'analysis_job':None,'source_contract':spec,'scientific_status':'unconfirmed','limits':['Development scenario, not held-out science evaluation','Candidate station grouping supplied externally; no independent event association','Runtime intervention count excludes input and candidate preparation']}
 engine.atomic(folder/'contract.json',spec)
 def save():engine.atomic(folder/'result.json',state)
 def event(action,**fields):state['events'].append({'at':engine.now(),'action':action,**fields});save()
 def files(base):return {suffix:engine.sha(base.with_suffix(suffix)) for suffix in ['.mseed','.xml']}
 def acquire(url,path):
  if state['requests']>=4:raise ValueError('Request budget reached')
  state['requests']+=1;save();fetch(url,path)
 start=time.perf_counter();save()
 try:
  record=dict(spec['initial']);base=folder/'initial'/'station';base.parent.mkdir()
  for suffix in ['.mseed','.xml']:shutil.copy2(ROOT/record['files'][suffix],base.with_suffix(suffix))
  event('snapshot_initial',hashes=files(base))
  q=assess(base,record);event('inspect',quality=q)
  eligible=lambda q:q['vertical_valid'] if spec['goal']=='vertical' else q['phase_eligible']
  # These quality predicates are fully computable; no language model is needed.
  for action,urlkey,suffix,needed in [
   ('refetch_metadata','station_url','.xml',lambda q:any(k!='covers_requested_interval_within_one_sample' for k in q['failed_checks'])),
   ('refetch_waveform','waveform_url','.mseed',lambda q:'covers_requested_interval_within_one_sample' in q['failed_checks'])]:
   if eligible(q) or not needed(q) or not record.get(urlkey):continue
   new=folder/action/'station';new.parent.mkdir()
   for ext in ['.mseed','.xml']:shutil.copy2(base.with_suffix(ext),new.with_suffix(ext))
   try:
    acquire(record[urlkey],new.with_suffix(suffix));nextq=assess(new,record)
    event(action,status='validated_response',url=record[urlkey],before=files(base),after=files(new),quality=nextq)
    base,q=new,nextq
   except Exception as exc:event(action,status='failed',error=str(exc)[:300])
  if not eligible(q):
   for index,alternative in enumerate(spec.get('alternatives',[])):
    new=folder/f'alternative_{index}'/'station';new.parent.mkdir()
    try:
     acquire(alternative['waveform_url'],new.with_suffix('.mseed'));acquire(alternative['station_url'],new.with_suffix('.xml'))
     nextq=assess(new,alternative)
     event('fetch_alternative',status='validated_response',station=alternative['net']+'.'+alternative['sta'],urls=[alternative['waveform_url'],alternative['station_url']],hashes=files(new),quality=nextq)
     if eligible(nextq):base,q,record=new,nextq,dict(alternative);break
    except Exception as exc:event('fetch_alternative',status='failed',error=str(exc)[:300])
  state['final_quality']=q
  if not eligible(q):
   state.update(status='stopped_insufficient',reason='Allowed sources do not provide inputs meeting the requested method conditions')
   event('stop',reason=state['reason']);return state
  manifest={'name':'补证验证 · '+spec['id'],'records':[{'waveform':'station.mseed','metadata':'station.xml','start':record['start'],'end':record['end']}]}
  engine.atomic(base.parent/'manifest.json',manifest);c=intake.import_package(base.parent/'manifest.json')
  job=engine.create_job(c['case_id'],'rules');state.update(analysis_job=job['id'],case_id=c['case_id']);save()
  result=engine.run_rules(job['id']);state['status']='completed' if result['status']=='completed' else 'analysis_failed'
  state['analysis_summary']={'p_available':sum(o.get('phases',{}).get('p_available',False) for o in result['observations'].values()),'s_available':sum(o.get('phases',{}).get('s_available',False) for o in result['observations'].values()),'unresolved':result['unresolved']}
  event('analyze',status=result['status'],analysis_job=job['id']);return state
 except Exception as exc:
  state.update(status='execution_failed',error=str(exc)[:300]);event('error',reason=state['error']);return state
 finally:
  state['finished_at']=engine.now();state['elapsed_seconds']=round(time.perf_counter()-start,3);save()

if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('scenario');args=p.parse_args()
 specs=json.loads((ROOT/'configs/stage9/scenarios.json').read_text());spec=next(s for s in specs if s['id']==args.scenario)
 print(json.dumps(run(spec),ensure_ascii=False,indent=2))
