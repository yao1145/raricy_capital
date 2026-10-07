"""Matched exposure control; run from project root, offline only."""
import argparse,json,sys
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path('tools').resolve()))
import btc_bear_research as b
ap=argparse.ArgumentParser();ap.add_argument('--data',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
p=b.extended_grid()[2].copy();p.update(code='hour100_r2_cap5',short_cap=5.)
rows=np.load(args.data/'BTCUSDT-4s.npy',mmap_mode='r');mask=np.load(args.data/'available.npy',mmap_mode='r')
hours=np.load(args.data/'hours.npy');valid=np.load(args.data/'hour_valid.npy')
state=b.daily_states(np.load(args.data/'days.npy'),np.load(args.data/'day_valid.npy'),hours)
f=b.bear_features(hours,valid,state,p);origin=int(rows[0,0]);end=int(rows[-1,0])+4000
out=[];half=[]
for phase,a,z in [('train','2017-01-01','2023-01-01'),('validation','2023-01-01','2025-01-01'),('recent','2025-01-01','2026-10-01'),('full','2017-01-01','2026-10-01')]:
 for scenario in (['normal'] if phase=='recent' else ['normal','stress']):
  start=b.stamp(a);finish=min(b.stamp(z),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
  months=pd.date_range(pd.Timestamp(a,tz='Asia/Shanghai')+pd.offsets.MonthBegin(),pd.Timestamp(z,tz='Asia/Shanghai'),freq='MS')
  dep=np.array([int(x.value//1000000) for x in months if int(x.value//1000000)<finish],np.int64)
  initial=1000+1000*np.sum(dep<origin);dep=dep[dep>=origin]
  fee=.0004 if scenario=='stress' else .0002;delay=2 if scenario=='stress' else 0
  cfg=np.array([5,.035,5,.02/.035,5,.3,2,4,.7,.05,fee,delay,1,0,1.5,4,336,1.],float)
  s,t,d=b.bear_replay(rows,mask,f,int(begin),int(stop),cfg,dep,float(initial))
  assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01)
  short=t[t[:,2]<0];years=(finish-start)/b.DAY/365.25
  r=dict(code=p['code'],phase=phase,scenario=scenario,start=b.iso(start),end_exclusive=b.iso(finish),total_assets=float(s[0]),net_profit=float(s[0]-s[3]),contributions=float(s[3]),unit_nav=float(s[4]),cagr_pct=float((s[4]**(1/years)-1)*100),close_dd_pct=float(s[5]*100),dd_bound_pct=float(s[6]*100),halt_time=b.iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),win_rate_pct=float(np.mean(t[:,4]>0)*100),profit_factor=b.pf(t[:,4]),normalized_pf=b.pf(t[:,10]),short_trades=len(short),short_profit=float(short[:,4].sum()),short_normalized_pf=b.pf(short[:,10]),short_win_rate_pct=float(np.mean(short[:,4]>0)*100),mae_pct=float(t[:,6].mean()),mfe_pct=float(t[:,7].mean()))
  out.append(r);print(phase,scenario,round(r['cagr_pct'],2),round(r['dd_bound_pct'],2),flush=True)
  if phase=='full':
   np.save(args.out/(p['code']+'_full_'+scenario+'_trades.npy'),t);np.save(args.out/(p['code']+'_full_'+scenario+'_daily.npy'),d)
   half+=b.half_year_rows(p['code'],scenario,t,d)
(args.out/'results.json').write_text(json.dumps(out,indent=2),encoding='utf-8')
(args.out/'protocol.json').write_text(json.dumps(dict(plan=p,purpose='matched cap5 control, post-selection diagnostic, no reselection',data=str(args.data)),indent=2),encoding='utf-8')
pd.DataFrame(out).to_csv(args.out/'summary.csv',index=False,encoding='utf-8-sig');pd.DataFrame(half).to_csv(args.out/'half_year.csv',index=False,encoding='utf-8-sig')
