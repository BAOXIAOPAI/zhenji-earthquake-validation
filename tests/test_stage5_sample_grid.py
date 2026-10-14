import sys
from pathlib import Path
import numpy as np,pytest
from obspy import Trace,UTCDateTime
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/stage5'))
from sample_grid import align_zne

def traces(offset):
 return [Trace(np.arange(100,dtype=float)+i*100,header={'sampling_rate':40,'starttime':UTCDateTime(0)+(offset if i==1 else 0)}) for i in range(3)]

def test_integer_crop_preserves_actual_samples():
 t=traces(.025);a,start,r=align_zne(t)
 assert r['cropped_samples_at_start_ZNE']==[1,0,1] and r['retained_samples']==99
 assert all(np.array_equal(x,src.data[k:k+99]) for x,src,k in zip(a,t,[1,0,1]))
 assert not r['interpolated'] and start==UTCDateTime(.025)

def test_fractional_grid_rejected():
 with pytest.raises(ValueError):align_zne(traces(.0125))

def test_mixed_rates_rejected():
 t=traces(0);t[1].stats.sampling_rate=100
 with pytest.raises(ValueError):align_zne(t)
