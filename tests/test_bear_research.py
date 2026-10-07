"""Focused causal/execution checks for stronger bear-market short research."""
import sys
from pathlib import Path
import pytest
np=pytest.importorskip('numpy')
pytest.importorskip('numba')
pytest.importorskip('pandas')
sys.path.insert(0,str(Path(__file__).parents[1]/'tools'))
import btc_bear_research as b

def test_daily_bear_state_uses_only_completed_day():
    days=np.zeros((260,6));days[:,0]=np.arange(260)*b.DAY
    days[:,4]=100-np.arange(260)*.1;days[:,1:4]=days[:,4,None]
    hours=np.zeros((260*24,6));hours[:,0]=np.arange(260*24)*b.HOUR
    state=b.daily_states(days,np.ones(260,dtype=bool),hours)
    assert state[209*24+22,0]==0
    assert state[209*24+23,0]==1
    days[211:,4]*=3
    changed=b.daily_states(days,np.ones(260,dtype=bool),hours)
    np.testing.assert_array_equal(state[:211*24],changed[:211*24])

def test_short_stop_multiplier_does_not_widen_long_stop():
    n=906;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    rows[901:,1:5]=88
    cfg=np.array([3,.01,2,1,2,0,2,0,.7,.5,.0002,0,0,0,1.5,4],float)
    f=np.zeros((2,10));f[0]=[1,0,.1,1,1,0,1,100,1,0]
    _,trades,_=b.bear_replay(rows,np.ones(n,dtype=bool),f,0,n,cfg,np.empty(0,np.int64),1000.)
    assert len(trades)==1 and trades[0,11]==1 and trades[0,1]==3608000

def test_wider_short_stop_sizes_position_by_wider_risk_distance():
    n=902;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    cfg=np.array([5,.035,5,1,3,0,2,0,.7,.5,.0002,0,0,0,1.5,4],float)
    f=np.zeros((2,10));f[0]=[0,1,.1,1,-1,1,1,100,1,0]
    _,trades,_=b.bear_replay(rows,np.ones(n,dtype=bool),f,0,n,cfg,np.empty(0,np.int64),1000.)
    expected=1000*.035/(.15+.0002*1.15)/5
    assert trades[0,3]==pytest.approx(np.floor(expected*10000)/10000)

def test_short_entry_confirmation_does_not_become_exit_condition():
    h=np.zeros((310,6));h[:,0]=np.arange(310)*b.HOUR
    h[:,4]=150-np.arange(310)*.1;h[:,1]=h[:,4]+.02;h[:,2]=h[:,1]+.03;h[:,3]=h[:,4]-.03
    state=np.ones((310,4))
    p=dict(regime='ema100',entry_ema=96,exit_ema=48,exit_band=0,entry='break48',veto=0)
    f=b.bear_features(h,np.ones(310,dtype=bool),state,p)
    # The latest close may not clear the last low, while the exit trend stays bearish.
    h[308,3]=1
    f=b.bear_features(h,np.ones(310,dtype=bool),state,p)
    assert f[309,1]==1 and f[309,8]==0

@pytest.mark.parametrize('direction',[1,-1])
def test_acceleration_boost_changes_only_short_budget(direction):
    n=902;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    f=np.zeros((2,12));f[0]=[direction==1,direction==-1,.1,1,direction,1,1,100,1,0,.2,1]
    cfg=np.array([5,.035,5,.02/.035,5,0,2,0,.7,.5,.0002,0,0,0,1.5,4,336,3.5],float)
    _,t,_=b.bear_replay(rows,np.ones(n,dtype=bool),f,0,n,cfg,np.empty(0,np.int64),1000.)
    risk=.035 if direction==1 else .07
    distance=.1 if direction==1 else .15
    expected=1000*risk/(distance+.0002*(1-direction*distance))/5
    assert t[0,3]==pytest.approx(np.floor(expected*10000)/10000)

def test_daily_stop_is_used_only_by_core_short():
    n=902;rows=np.zeros((n,6));rows[:,0]=np.arange(n)*4000;rows[:,1:5]=100
    f=np.zeros((2,12));f[0]=[0,1,.01,1,-1,1,1,100,1,0,.2,0]
    cfg=np.array([5,.035,5,1,5,0,2,0,.7,.5,.0002,0,0,0,0,4,2160,1],float)
    _,t,_=b.bear_replay(rows,np.ones(n,dtype=bool),f,0,n,cfg,np.empty(0,np.int64),1000.)
    expected=1000*.035/(.2+.0002*1.2)/5
    assert t[0,3]==pytest.approx(np.floor(expected*10000)/10000)
