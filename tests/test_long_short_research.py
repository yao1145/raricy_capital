"""Focused checks for the standalone simulator-matching research engine."""
import sys
from pathlib import Path
import pytest
np = pytest.importorskip("numpy")
pytest.importorskip("numba")
pytest.importorskip("pandas")
sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
import btc_long_short_research as r
def test_directional_payout_keeps_fee_on_exit_notional():
    assert r.payout(100,100,90,5,-1,.0002)==pytest.approx(149.91)
    assert r.payout(100,100,90,5,1,.0002)==pytest.approx(49.91)
    assert r.payout(100,100,110,5,-1,.0002)==pytest.approx(49.8899)
    assert r.payout(100,100,100,5,-1,.0002)==pytest.approx(99.9)
def test_short_liquidation_even_at_one_times_leverage():
    n=903;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    rows[901,2]=200
    feat=np.zeros((2,8));feat[0]=[0,1,.1,1,-1,1,1,100]
    cfg=np.array([1,.05,.5,1,.5,0,0,0,.7,.05,.0002,0,0],float)
    summary,trades,daily=r.replay(rows,np.ones(n,dtype=bool),feat,0,n,cfg,np.empty(0,np.int64),1000.)
    assert summary[8]==1 and len(trades)==1
    assert trades[0,2]==-1 and trades[0,11]==7
    assert trades[0,4]==-trades[0,3]
    assert summary[0]-summary[3]==pytest.approx(trades[:,4].sum())
def test_closed_hour_signal_cannot_fill_inside_its_own_bar():
    n=901;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    rows[899,1:5]=110
    feat=np.zeros((2,8));feat[0]=[1,0,.1,1,1,0,1,110]
    cfg=np.array([3,.01,2,0,2,0,0,0,.25,.015,.0002,0,0],float)
    _,trades,_=r.replay(rows,np.ones(n,dtype=bool),feat,0,n,cfg,np.empty(0,np.int64),1000.)
    assert len(trades)==1 and trades[0,0]==3600000

@pytest.mark.parametrize("direction", [1, -1])
def test_leverage_does_not_multiply_return_when_notional_is_fixed(direction):
    low_gain = r.payout(200, 100, 105, 3, direction, .0002) - 200
    high_gain = r.payout(60, 100, 105, 10, direction, .0002) - 60
    assert high_gain == pytest.approx(low_gain, abs=.00011)

def test_hourly_features_do_not_read_future_candles():
    hours=np.zeros((320,6));hours[:,0]=np.arange(320)*r.HOUR
    hours[:,4]=100+np.arange(320)*.1;hours[:,1]=hours[:,4]
    hours[:,2]=hours[:,4]+.3;hours[:,3]=hours[:,4]-.3
    valid=np.ones(320,dtype=bool);bear=np.zeros(320)
    before=r.features(hours,valid,96,bear)
    hours[260:,1:5]*=2
    after=r.features(hours,valid,96,bear)
    np.testing.assert_array_equal(before[:260],after[:260])
    assert before[204,6]==0 and before[205,6]==1
