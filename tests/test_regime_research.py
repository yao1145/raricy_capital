"""Causality and accounting checks for offline multi-regime research."""
import sys
from pathlib import Path
import pytest
np=pytest.importorskip("numpy")
pytest.importorskip("numba")
pytest.importorskip("pandas")
sys.path.insert(0,str(Path(__file__).parents[1]/"tools"))
import btc_regime_research as r

def fixture(n=902):
    rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100.
    f=np.zeros((n//900+2,16));f[:,12:14]=1.;f[:,14]=5;f[:,15]=1
    f[0,:12]=[1,0,.10,1,1,0,1,100,1,0,.2,0]
    cfg=np.array([5,.035,5,.02/.035,5,0,2,0,.7,.5,0,0,0,0,1.5,4,336,5],float)
    return rows,f,cfg

def test_pending_budget_captured_at_signal_time():
    rows,f,cfg=fixture();f[0,12]=.5;f[1:,12]=10
    _,t,_,_=r.regime_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    assert t[0,3]==pytest.approx(35.)

def test_range_entry_gate_does_not_exit_an_existing_long():
    rows,f,cfg=fixture(1802);f[1,:12]=f[0,:12];f[1,15]=0;f[1,14]=2
    _,t,_,_=r.regime_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    assert len(t)==1 and t[0,11]==8

def test_range_gate_blocks_new_entry():
    rows,f,cfg=fixture();f[0,15]=0
    _,t,_,_=r.regime_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.empty(0,np.int64),1000.)
    assert len(t)==0

def test_state_log_contributions_exclude_deposits():
    rows,f,cfg=fixture();f[0,14]=1;rows[901:,1:5]=101
    s,_,_,diagnostic=r.regime_replay(rows,np.ones(len(rows),bool),f,0,len(rows),cfg,np.array([3604000],np.int64),1000.)
    assert diagnostic[:,2].sum()==pytest.approx(np.log(s[4]),abs=1e-10)
    assert s[3]==2000

def test_regime_and_overlays_use_only_available_history():
    n=420;hours=np.zeros((n,6));hours[:,0]=np.arange(n)*r.HOUR
    hours[:,4]=100+np.sin(np.arange(n)/10)*2;hours[:,1]=hours[:,4]-.05
    hours[:,2]=hours[:,4]+.1;hours[:,3]=hours[:,4]-.1
    state=np.zeros((n,7));p=r.plans()[4]
    a=r.regime_features(hours,np.ones(n,bool),state,p)
    hours[360:,1:5]*=1.5
    z=r.regime_features(hours,np.ones(n,bool),state,p)
    np.testing.assert_allclose(a[:360],z[:360])

def test_extreme_drop_has_priority_over_range_or_trend():
    assert r.classify_state(-.09,0,.1,0,1,1)==0
    assert r.classify_state(.09,0,.1,0,1,1)==1
    assert r.classify_state(0,0,.1,.5,1,1)==2
    assert r.classify_state(0,0,.5,2,1,0)==3


def test_daily_quality_confirmation_cannot_see_future_days():
    days=np.zeros((280,6));days[:,0]=np.arange(280)*r.DAY
    days[:,4]=150-np.arange(280)*.1;days[:,1:4]=days[:,4,None]
    hours=np.zeros((280*24,6));hours[:,0]=np.arange(280*24)*r.HOUR
    valid=np.ones(280,bool)
    before=r.mapped_states(days,valid,hours)
    days[240:,1:5]*=3.
    after=r.mapped_states(days,valid,hours)
    np.testing.assert_array_equal(before[:240*24+23],after[:240*24+23])

