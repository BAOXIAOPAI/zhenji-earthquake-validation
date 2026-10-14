"""Inspect native-rate external observations without interpolation or class inference."""
from pathlib import Path
import json,hashlib
import numpy as np
from obspy import read,read_inventory,UTCDateTime
R=Path(__file__).resolve().parents[2]

def inspect(st,inv,job):
 start,end=UTCDateTime(job['start']),UTCDateTime(job['end']);rows=[]
 for tr in st:
  t=tr.stats;matches=[c for n in inv if n.code==t.network for s in n if s.code==t.station for c in s if c.code==t.channel and c.location_code==t.location and (c.start_date is None or c.start_date<=t.starttime) and (c.end_date is None or c.end_date>=t.endtime)]
  c=matches[0] if len(matches)==1 else None
  orientation_ok=False
  if c and c.dip is not None and c.azimuth is not None:
   component=t.channel[-1]
   orientation_ok=bool((component=='Z' and abs(c.dip+90)<1e-6) or (component=='N' and abs(c.dip)<1e-6 and abs(c.azimuth)<1e-6) or (component=='E' and abs(c.dip)<1e-6 and abs(c.azimuth-90)<1e-6))
  rows.append({'id':tr.id,'start':str(t.starttime),'end':str(t.endtime),'sample_rate_hz':t.sampling_rate,'samples':t.npts,'finite':bool(np.isfinite(tr.data).all()),'constant':bool(np.ptp(tr.data.astype(float))==0),'requested_identity':t.network==job['net'] and t.station==job['sta'] and t.location=='' and t.channel.startswith(job['cha']),'covers_requested_interval_within_one_sample':abs(t.starttime-start)<=t.delta+1e-6 and abs(t.endtime-end)<=t.delta+1e-6,'metadata_unique_epoch':len(matches)==1,'rate_matches_metadata':c is not None and c.sample_rate is not None and abs(c.sample_rate-t.sampling_rate)<1e-9,'canonical_ENZ_orientation':orientation_ok,'response_present':c is not None and c.response is not None,'azimuth':None if c is None else c.azimuth,'dip':None if c is None else c.dip})
 gaps=st.get_gaps();components=sorted(t.stats.channel[-1] for t in st)
 basic=bool(rows) and not gaps and all(r['finite'] and not r['constant'] and r['requested_identity'] and r['covers_requested_interval_within_one_sample'] and r['metadata_unique_epoch'] and r['rate_matches_metadata'] and r['canonical_ENZ_orientation'] and r['response_present'] for r in rows)
 complete=basic and components==['E','N','Z']
 return {'case_id':job['case_id'],'id':job['id'],'channel_checks':rows,'gaps_or_overlaps':gaps,'basic_observation_checks_pass':basic,'three_component_checks_pass':complete,'state':'three_component_observation_acquired' if complete else 'partial_or_invalid_observation','next_action':'scientific_consistency_check_still_required' if complete else 'try_another_station_if_three_components_are_required','limits':['Native counts, not response-corrected physical amplitudes','No resampling, rotation, interpolation or phase association performed','Another station does not reconstruct the original missing channel','Catalogue membership and selected station identities are supplied, not independently discovered']}
