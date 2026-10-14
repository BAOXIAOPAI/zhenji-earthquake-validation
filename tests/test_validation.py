import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import numpy as np
import pandas as pd
import pytest
from waveform import features,read_trace
from baseline import evaluate
from evidence import EvidenceService


def test_gain_and_polarity_do_not_change_features():
    x=np.random.default_rng(4).normal(size=(3,12000))
    a=features(x,seconds=120);b=features(-7*x,seconds=120)
    assert a.keys()==b.keys()
    assert np.allclose(list(a.values()),list(b.values()),rtol=1e-8,atol=1e-8)


@pytest.mark.parametrize('x',[np.zeros((3,12000)),np.full((3,12000),np.nan),np.zeros((2,12000)),np.zeros((3,20))])
def test_invalid_waveform_never_becomes_negative_evidence(x):
    with pytest.raises(ValueError):features(x,seconds=120)


def test_trace_index_rejects_arbitrary_expression():
    with pytest.raises(ValueError):read_trace(None,"__import__('os')")


def test_review_does_not_count_as_correct_automatic_handling():
    m=evaluate([1,1,0,0],[.01,.5,.99,.5],.1,.9)
    assert m['earthquakes_discarded']==1
    assert m['non_target_accepted']==1
    assert m['review_count']==2
    assert m['auto_error_rate']==1


def test_event_splits_and_feature_whitelist():
    root=Path(__file__).resolve().parents[1]
    if not (root/'evaluation/splits.csv').exists():pytest.skip('Run audit first')
    df=pd.read_csv(root/'evaluation/splits.csv')
    assert df.groupby('event_id').split.nunique().max()==1
    cols=pd.read_csv(root/'data/processed/waveform-features.csv',nrows=0).columns
    assert not set(cols)&{'source_type','source_depth_km','source_magnitude','trace_P_arrival_sample','trace_snr_db','event_id'}
    assert df[df.label_conflict].split.eq('quarantine').all()


def test_runtime_blocks_test_cases_and_path_access():
    root=Path(__file__).resolve().parents[1]
    if not (root/'data/processed/runtime-index.json').exists():pytest.skip('Prepare runtime first')
    service=EvidenceService(root)
    df=pd.read_csv(root/'evaluation/splits.csv')
    assert not set(df[df.split!='train'].case_id)&set(service.index)
    with pytest.raises(ValueError):service.inspect('../../evaluation/splits.csv')
    case=next(iter(service.index))
    with pytest.raises(ValueError):service.inspect(case,record=-1)
    with pytest.raises(ValueError):service.inspect(case,action='read_labels')
    result=service.inspect(case,'spectral')
    assert result['status']=='ok'
    assert not set(result)&{'trace_name','source_type','event_id','source_depth_km','label'}


def test_cross_event_duplicates_never_cross_splits():
    root=Path(__file__).resolve().parents[1]
    if not (root/'evaluation/waveform-checks.csv').exists():pytest.skip('Run audit first')
    checks=pd.read_csv(root/'evaluation/waveform-checks.csv')
    labels=pd.read_csv(root/'evaluation/splits.csv')
    checks=checks.merge(labels[['row_id','split']],on='row_id')
    assert checks.groupby('window_sha256').split.nunique().max()==1
