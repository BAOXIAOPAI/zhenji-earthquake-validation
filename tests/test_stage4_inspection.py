import sys,json
from pathlib import Path
import numpy as np
from obspy import read,read_inventory
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R/'src/stage4'))
from inspect_downloads import inspect

def material():
 j=json.loads((R/'configs/stage4/retrieval-jobs.json').read_text())[1]
 return j,read(str(R/'data/raw/stage4'/f"{j['id']}.mseed")),read_inventory(str(R/'data/raw/stage4'/f"{j['id']}.xml"))

def test_real_complete_then_missing_component():
 j,s,i=material();assert inspect(s,i,j)['three_component_checks_pass']
 assert not inspect(s[:2],i,j)['three_component_checks_pass']

def test_constant_channel_rejected():
 j,s,i=material();s[0].data[:]=0
 assert not inspect(s,i,j)['three_component_checks_pass']

def test_wrong_identity_rejected():
 j,s,i=material();j['sta']='WRONG'
 assert not inspect(s,i,j)['three_component_checks_pass']

def test_partial_time_coverage_rejected():
 j,s,i=material();s.trim(s[0].stats.starttime+5,s[0].stats.endtime)
 assert not inspect(s,i,j)['three_component_checks_pass']

def test_wrong_metadata_orientation_rejected():
 j,s,i=material();i[0][0][0].dip=10
 assert not inspect(s,i,j)['three_component_checks_pass']
