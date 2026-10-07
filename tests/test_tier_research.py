"""Focused checks for drawdown and per-leg adaptation in offline tier research."""
import sys
from pathlib import Path
import pytest
np=pytest.importorskip("numpy")
pytest.importorskip("numba")
pytest.importorskip("pandas")
sys.path.insert(0,str(Path(__file__).parents[1]/"tools"))
import btc_tier_research as t
import btc_regime_research as r

def fixture(n=902):
    rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100.
    f=np.zeros((n//900+2,16));f[:,:12]=[1,0,.1,1,1,0,1,100,1,0,.2,0]
    f[:,12:14]=1;f[:,14]=5;f[:,15]=1
    cfg=np.array([5,.035,5,.02/.035,5,0,2,0,.7,.5,0,0,0,0,1.5,4,336,5,0,1,0,1],float)
    return rows,f,cfg

def test_disabled_overlays_match_frozen_engine():
    rows,f,cfg=fixture();args=(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    old=r.regime_replay(*args);new=t.tier_replay(*args)
    for a,b in zip(old,new):np.testing.assert_array_equal(a,b)

def test_drawdown_budget_bounds_and_neutral_deposit():
    assert t.drawdown_factor(.04,.25,.08,.35)==1
    assert t.drawdown_factor(.165,.25,.08,.35)==pytest.approx(.5)
    assert t.drawdown_factor(.26,.25,.08,.35)==.35
    rows,f,cfg=fixture(5402);rows[901:,1:5]=90;cfg[8]=.25;cfg[18]=.01;cfg[19]=.35
    _,trades,_,_=t.tier_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.array([18000000],np.int64),1000.)
    assert len(trades)==2 and trades[1,13]<trades[0,13]
    assert trades[1,13]==pytest.approx(.035*(.25-.035)/(.25-.01))

def test_short_loss_gate_does_not_lower_long_budget():
    rows,f,cfg=fixture(5402)
    f[0,0]=0;f[0,1]=1;f[0,4]=-1;f[0,8]=1
    rows[901:,1:5]=116;cfg[7]=0;cfg[20]=1;cfg[21]=.25
    _,trades,_,_=t.tier_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    assert trades[0,2]==-1 and trades[0,4]<0
    assert trades[1,2]==1 and trades[1,13]==pytest.approx(.035)

@pytest.mark.parametrize("code",["M_ema384","M_turn50"])
def test_slow_signal_features_do_not_see_future_prices(code):
    n=520;h=np.zeros((n,6));h[:,0]=np.arange(n)*r.HOUR
    h[:,4]=100+np.arange(n)*.01;h[:,1]=h[:,4]-.02;h[:,2]=h[:,4]+.1;h[:,3]=h[:,4]-.1
    valid=np.ones(n,bool);state=np.zeros((n,9))
    p=next(p for p in t.plans()+t.plans(True) if p["code"]==code)
    a=t.tier_features(h,valid,state,p);h[450:,1:5]*=2
    b=t.tier_features(h,valid,state,p)
    np.testing.assert_array_equal(a[:450],b[:450])


def test_short_loss_gate_reduces_budget_then_recovers_after_a_win():
    rows,f,cfg=fixture(10802);f[:,0]=0;f[:,1]=1;f[:,4]=-1;f[:,8]=1
    rows[901:5401,1:5]=116;rows[5401:,1:5]=90;f[6,1]=0
    cfg[7]=0;cfg[20]=1;cfg[21]=.25
    _,trades,_,_=t.tier_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    assert len(trades)==3 and trades[0,4]<0 and trades[1,4]>0
    np.testing.assert_allclose(trades[:,13],[.02,.005,.02])

