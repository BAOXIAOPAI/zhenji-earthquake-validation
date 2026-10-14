"""Explicit local observation-package intake; never fetches manifest URLs."""
from pathlib import Path
import json, uuid, shutil, re
import numpy as np
from obspy import read, read_inventory, UTCDateTime
from engine import ROOT, atomic, now, sha
REGISTRY=ROOT/'runs/stage7/imports'

def import_package(manifest, registry=None):
 registry=Path(registry or REGISTRY)
 manifest=Path(manifest).resolve()
 if manifest.stat().st_size>65536:raise ValueError('Manifest exceeds 64 KiB')
 spec=json.loads(manifest.read_text())
 if not isinstance(spec,dict) or set(spec)!={'name','records'}:raise ValueError('Expected name and records only')
 if not isinstance(spec['name'],str) or not 1<=len(spec['name'].strip())<=80:raise ValueError('Name must be 1–80 characters')
 rows=spec['records']
 if not isinstance(rows,list) or not 1<=len(rows)<=8:raise ValueError('Expected 1–8 station records')
 prepared=[];total=0;identities=set()
 for row in rows:
  if not isinstance(row,dict) or set(row)!={'waveform','metadata','start','end'}:raise ValueError('Record requires waveform, metadata, start, end')
  paths=[]
  for key in ['waveform','metadata']:
   value=row[key]
   if not isinstance(value,str) or '://' in value or Path(value).is_absolute():raise ValueError('Use relative local file paths')
   p=(manifest.parent/value).resolve()
   if not p.is_relative_to(manifest.parent) or not p.is_file():raise ValueError('Files must remain inside package directory')
   total+=p.stat().st_size
   if total>32*1024*1024:raise ValueError('Package exceeds 32 MiB')
   paths.append(p)
  start,end=UTCDateTime(row['start']),UTCDateTime(row['end'])
  if not 30<=end-start<=300:raise ValueError('Window must be 30–300 seconds')
  st=read(str(paths[0]),format='MSEED');read_inventory(str(paths[1]),format='STATIONXML')
  if not 1<=len(st)<=3:raise ValueError('Expected 1–3 continuous component traces')
  identity={(t.stats.network,t.stats.station,t.stats.location,t.stats.channel[:2]) for t in st}
  if len(identity)!=1:raise ValueError('One station/channel family per record')
  net,sta,loc,cha=next(iter(identity))
  if loc or not all(re.fullmatch('[A-Za-z0-9]{1,8}',v) for v in [net,sta,cha]):raise ValueError('Unsupported station identity/location')
  if (net,sta,cha) in identities:raise ValueError('Duplicate station record')
  identities.add((net,sta,cha))
  components=[t.stats.channel[-1] for t in st]
  if len(set(components))!=len(components) or any(c not in 'ENZ' for c in components):raise ValueError('Unique ENZ components required')
  for t in st:
   if not 40<=t.stats.sampling_rate<=200 or not 30<=t.stats.endtime-t.stats.starttime<=301:raise ValueError('Unsupported sample rate or duration')
   if not np.isfinite(t.data).all():raise ValueError('Non-finite waveform')
  prepared.append((paths,{'net':net,'sta':sta,'cha':cha,'start':str(start),'end':str(end)}))
 # Publish only after all records validate; unregistered partial copies are not candidates.
 case='import_'+uuid.uuid4().hex;folder=registry/case;folder.mkdir(parents=True)
 entries=[]
 for i,(paths,j) in enumerate(prepared):
  rid=f'observation_{i+1:02d}';base=folder/rid
  for source,suffix in zip(paths,['.mseed','.xml']):shutil.copy2(source,base.with_suffix(suffix))
  entries.append({**j,'id':rid,'case_id':case,'base':str(base.resolve()),'retrieved_at':now(),'waveform_sha256':sha(base.with_suffix('.mseed')),'stationxml_sha256':sha(base.with_suffix('.xml'))})
 out={'case_id':case,'name':spec['name'].strip(),'created_at':now(),'records':entries,'source':'user-supplied local observation package; grouping unverified'}
 atomic(folder/'manifest.json',out);return out

if __name__=='__main__':
 import argparse
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('manifest');args=parser.parse_args()
 print(json.dumps(import_package(args.manifest),ensure_ascii=False,indent=2))
