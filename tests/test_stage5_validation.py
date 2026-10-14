from pathlib import Path
import sys,json,copy
import pytest
R=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(R/'src/stage5'))
from evaluate_models import check
from analyze import calculate
from obspy import read

def result():
 p=json.loads((R/'runs/stage5/deterministic-baselines.json').read_text())[0]
 return p['output']

def test_correct_deterministic_output():
 o=result();assert not check(o,o['case_id'],set(o['evidence_ids']))

@pytest.mark.parametrize('field,value',[('phase_confirmed',True),('source_class_confirmed',True),('original_channel_restored',True),('needs_professional_review',False),('candidate_onsets_utc',[])])
def test_reject_overclaim_and_missing_candidates(field,value):
 o=result();o[field]=value;assert check(o,o['case_id'],set(o['evidence_ids']))

def test_proposed_but_unseen_result_rejected():
 o=result();assert check(o,o['case_id'],set())

def test_vertical_only_is_sufficient_and_native_rate_preserved():
 st=read(str(R/'data/raw/stage4/uw60540367-UW-LVP.mseed'));assert len(st)==1
 r,t,c=calculate(st[0],[1,8]);assert r['trigger_count']>0
 tr=read(str(R/'data/raw/stage4/uw60652962-UW-RADR.mseed')).select(component='Z')[0]
 _,t,_=calculate(tr,[1,8]);assert t.stats.sampling_rate==40 and t.stats.starttime==tr.stats.starttime

def test_nyquist_violation_rejected():
 tr=read(str(R/'data/raw/stage4/uw60652962-UW-RADR.mseed')).select(component='Z')[0]
 with pytest.raises(ValueError):calculate(tr,[1,25])
