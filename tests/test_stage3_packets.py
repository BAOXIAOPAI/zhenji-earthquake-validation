"""Guard evidence provenance and non-training isolation; not scientific accuracy tests."""
import copy,json,sys
from pathlib import Path
import h5py,pandas as pd,pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src/stage3'))
from packets import coverage,build
from validate_packets import validate

@pytest.fixture
def evidence():
    meta=pd.read_csv(ROOT/'evaluation/splits.csv')
    paths=list((ROOT/'runs/stage3/packets').glob('*-metadata_rules.json'))
    packets=[json.loads(p.read_text()) for p in paths]
    with h5py.File(ROOT/'data/raw/pnw-mini/miniPNW_waveforms.hdf5','r') as f:
        yield meta,packets,f

@pytest.mark.parametrize('kind',['hash','time','constant','coverage'])
def test_reject_corrupt_evidence(evidence,kind):
    meta,ps,f=evidence
    p=copy.deepcopy(next(p for p in ps if (any(o['constant_channels'] for o in p['observations']) if kind=='constant' else len(p['observations'])>1)))
    if kind=='hash':p['observations'][0]['waveform_window_sha256_float64']='0'*64
    if kind=='time':p['observations'][0]['start_utc']='2000-01-01T00:00:00+00:00'
    if kind=='constant':next(o for o in p['observations'] if o['constant_channels'])['constant_channels']=[]
    if kind=='coverage':p['cross_station_coverage']['source_coverage_intervals'][0]['seconds']=121
    with pytest.raises(AssertionError):validate(p,meta,f)

@pytest.mark.parametrize('split',['test','calibration'])
def test_reject_nontraining_before_read(evidence,split):
    meta,_,_=evidence
    # Use a real case ID but synthetic split assignment; never read a held-out waveform.
    subset=meta[meta.split=='train'].iloc[:1].copy();subset['split']=split
    with pytest.raises(ValueError):build(subset.iloc[0].case_id,subset,None,'metadata_rules')

def test_source_coverage_not_phase_alignment():
    assert coverage([{'start_ns':0,'samples':15000},{'start_ns':200_000_000_000,'samples':15000}])['status']=='insufficient_overlap'
    p=coverage([{'start_ns':0,'samples':18000},{'start_ns':40_000_000_000,'samples':18000}])
    assert p['common_coverage_seconds']==140
    assert 'not extracted' in p['interpretation_limits'][0]
