"""Post-selection diagnostics for fixed regime neighbors and single-leg controls."""
from __future__ import annotations
import argparse,json
from pathlib import Path
import numpy as np
import pandas as pd
import btc_regime_research as r

def main():
    ap=argparse.ArgumentParser()
    for name in ["data","out","previous"]:ap.add_argument("--"+name,type=Path,required=True)
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    existing=json.loads((args.previous/"results.json").read_text())
    done={m["code"] for m in existing if m["phase"]=="full"}
    codes=["long_vol60","hour2_vol60","boost5_vol60","boost7_vol60","vol50","vol70"]
    plans=[p for p in r.plans(True) if p["code"] in codes and p["code"] not in done]
    rows=np.load(args.data/"BTCUSDT-4s.npy",mmap_mode="r");mask=np.load(args.data/"available.npy",mmap_mode="r")
    hours=np.load(args.data/"hours.npy");valid=np.load(args.data/"hour_valid.npy")
    state=r.mapped_states(np.load(args.data/"days.npy"),np.load(args.data/"day_valid.npy"),hours)
    origin=int(rows[0,0]);end=int(rows[-1,0])+4000
    records=[];half=[];regimes=[]
    for p in plans:
        f=r.regime_features(hours,valid,state,p)
        for phase,a,z in [("full","2017-01-01","2026-10-01"),("recent","2025-01-01","2026-10-01")]:
            for scenario in ["normal","stress"]:
                start=r.stamp(a);finish=min(r.stamp(z),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
                dates=pd.date_range(pd.Timestamp(a,tz="Asia/Shanghai")+pd.offsets.MonthBegin(),pd.Timestamp(z,tz="Asia/Shanghai"),freq="MS")
                dep=np.array([int(x.value//1000000) for x in dates if int(x.value//1000000)<finish],np.int64)
                initial=1000+1000*np.sum(dep<origin);dep=dep[dep>=origin]
                fee=.0004 if scenario=="stress" else .0002;delay=2 if scenario=="stress" else 0
                cfg=np.array([5,p["risk"],5,p["short_risk"]/p["risk"],5,p["short_er"],2,p["trail"],.7,.05,fee,delay,p["dynamic"],0,
                              p["short_distance"],4,336,p.get("boost_risk",0)/p["short_risk"] if p["short_risk"] else 1.],float)
                s,t,d,g=r.regime_replay(rows,mask,f,int(begin),int(stop),cfg,dep,float(initial))
                assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01)
                assert np.isclose(g[:,2].sum(),np.log(s[4]),atol=1e-7)
                short=t[t[:,2]<0];years=(finish-start)/r.DAY/365.25
                m=dict(code=p["code"],phase=phase,scenario=scenario,start=r.iso(start),end_exclusive=r.iso(finish),total_assets=float(s[0]),
                       net_profit=float(s[0]-s[3]),contributions=float(s[3]),unit_nav=float(s[4]),cagr_pct=(s[4]**(1/years)-1)*100,
                       xirr_pct=r.xirr(s[0],start,finish),close_dd_pct=s[5]*100,dd_bound_pct=s[6]*100,
                       halt_time=r.iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),win_rate_pct=np.mean(t[:,4]>0)*100,
                       profit_factor=r.pf(t[:,4]),normalized_pf=r.pf(t[:,10]),short_trades=len(short),short_profit=float(short[:,4].sum()),
                       short_normalized_pf=r.pf(short[:,10]),mae_pct=float(t[:,6].mean()),mfe_pct=float(t[:,7].mean()))
                records.append(m);name=p["code"]+"_"+phase+"_"+scenario
                if phase=="full":
                    np.save(args.out/(name+"_trades.npy"),t);np.save(args.out/(name+"_daily.npy"),d)
                    half.extend(r.half_year_rows(p["code"],scenario,t,d))
                    for k,label in enumerate(r.STATES):
                        tr=t[t[:,12]==k]
                        regimes.append(dict(code=p["code"],phase=phase,scenario=scenario,state=label,hours=g[k,0]/900,
                                            exposure_pct=g[k,1]/max(1.,g[k,0])*100,nav_log_contribution=g[k,2],
                                            global_dd_during_state_pct=g[k,3]*100,entry_trades=len(tr),
                                            win_pct=np.mean(tr[:,4]>0)*100 if len(tr) else None,normalized_pf=r.pf(tr[:,10]),
                                            mae_pct=tr[:,6].mean() if len(tr) else None,mfe_pct=tr[:,7].mean() if len(tr) else None))
                print(p["code"],phase,scenario,round(m["cagr_pct"],2),round(m["dd_bound_pct"],2),flush=True)
    protocol=dict(purpose="post-selection neighbors and single-leg controls; no reselection on full/recent",plans=plans,data=str(args.data),previous=str(args.previous),
                  selection_unchanged=json.loads((args.previous/"protocol.json").read_text())["selected"])
    (args.out/"protocol.json").write_text(json.dumps(protocol,indent=2),encoding="utf-8")
    (args.out/"results.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
    pd.DataFrame(records).to_csv(args.out/"summary.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(half).to_csv(args.out/"half_year.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(regimes).to_csv(args.out/"regimes.csv",index=False,encoding="utf-8-sig")
if __name__=="__main__":main()

