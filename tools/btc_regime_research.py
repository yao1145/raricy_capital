"""Offline regime overlays. Research only; no fund service or live actions."""
from __future__ import annotations
import argparse, json, math, time
from pathlib import Path
import numpy as np
import pandas as pd
from numba import njit
from btc_long_short_research import HOUR, DAY, payout, stamp, iso
from btc_bear_research import seeded_ema, daily_states, bear_features, extended_grid, pf, half_year_rows
STATES=["crash","surge","range","bear","bull","transition"]

def classify_state(r24,r6,er,gap_atr,bear,bull):
    if r24<=-.08 or r6<=-.04:return 0
    if r24>=.08 or r6>=.04:return 1
    if er<=.20 and gap_atr<=1.:return 2
    if bear:return 3
    if bull:return 4
    return 5

def plans(refined=False):
    common=extended_grid()[2].copy();common.update(short_cap=5.)
    bold=dict(common,code="boost10",boost_risk=.10,boost_er=.45,boost_return=-.03)
    out=[dict(extended_grid()[0],code="long35"),dict(common,code="hour2"),bold]
    out += [dict(bold,code="brake_soft",brake="soft"),dict(bold,code="brake_hard",brake="hard")]
    for target in [.6,.8,1.]:
        out.append(dict(bold,code=f"vol{int(target*100)}",vol_target=target))
    for threshold in [.15,.20,.25]:
        out.append(dict(bold,code=f"range{int(threshold*100)}",range_er=threshold))
    out += [dict(bold,code="bull5",bull_risk=.05),dict(bold,code="bear_long60",bear_long_mult=.6)]
    out += [dict(bold,code="brake_vol",brake="soft",vol_target=.8),
            dict(bold,code="brake_range",brake="soft",range_er=.20),
            dict(bold,code="vol_range",vol_target=.8,range_er=.20),
            dict(bold,code="all",brake="soft",vol_target=.8,range_er=.20),
            dict(bold,code="all_bull5",brake="soft",vol_target=.8,range_er=.20,bull_risk=.05),
            dict(bold,code="all_bull5_bear60",brake="soft",vol_target=.8,range_er=.20,bull_risk=.05,bear_long_mult=.6)]
    if refined:
        control=out[:3]+[next(p for p in out if p["code"]=="vol60")]
        vol=control[-1]
        more=[dict(control[0],code="long_vol60",vol_target=.6),
              dict(control[1],code="hour2_vol60",vol_target=.6)]
        for risk in [.05,.07]:more.append(dict(vol,code=f"boost{int(risk*100)}_vol60",boost_risk=risk))
        for target in [.5,.7]:more.append(dict(vol,code=f"vol{int(target*100)}",vol_target=target))
        for confirm in ["consensus","daily20","daily_er20","both"]:
            more.append(dict(vol,code="vol60_"+confirm,short_confirm=confirm))
        return control+more
    return out

def regime_features(hours,valid,state,p):
    f=np.column_stack((bear_features(hours,valid,state,p),np.ones((len(hours),2)),np.full(len(hours),5),np.ones(len(hours))))
    close=hours[:,4];e24=seeded_ema(close,24);e96=seeded_ema(close,96);e192=seeded_ema(close,192)
    ser=pd.Series(close);change=ser.diff()
    vol=np.log(ser).diff().rolling(168,min_periods=48).std(ddof=1).to_numpy()*np.sqrt(24*365.25)
    gain=change.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean().to_numpy()
    loss=(-change.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean().to_numpy()
    rsi=np.divide(gain,gain+loss,out=np.full(len(close),.5),where=gain+loss>0)*100
    for i in range(24,len(hours)):
        r24=close[i]/close[i-24]-1;r6=close[i]/close[i-6]-1
        path=np.sum(np.abs(np.diff(close[i-24:i+1])));er=abs(close[i]-close[i-24])/path if path else 0.
        gap=abs(close[i]-e96[i])/(close[i]*f[i,2]/2) if f[i,2]>0 else np.inf
        bear=state[i,1]>0;bull=state[i,6]>0
        f[i,14]=classify_state(r24,r6,er,gap,bear,bull)
        if p.get("short_confirm"):
            confirm=p["short_confirm"]
            consensus=i>=216 and close[i]<e24[i] and close[i]<e192[i] and e192[i]<e192[i-24]
            daily20=state.shape[1]>8 and state[i,8]>0
            dailyer=state.shape[1]>7 and state[i,7]>=.25 and daily20
            if ((confirm=="consensus" and not consensus) or (confirm=="daily20" and not daily20)
                or (confirm=="daily_er20" and not dailyer) or (confirm=="both" and not (dailyer and consensus))):
                f[i,8]=0
        if p.get("range_er") is not None and er<=p["range_er"] and gap<=1.:
            f[i,8]=0;f[i,15]=0
        if p.get("brake"):
            # Exhaustion cancels the BOOST only; daily/hourly short eligibility remains.
            oversold=18 if p["brake"]=="hard" else 12
            if rsi[i]<oversold:f[i,11]=0
            rebound=.015 if p["brake"]=="hard" else .025
            if r6>rebound and close[i]>e24[i]:
                f[i,1]=0;f[i,8]=0;f[i,11]=0
        mult=1.
        if p.get("vol_target") and np.isfinite(vol[i]) and vol[i]>0:
            mult=max(.35,min(1.,p["vol_target"]/vol[i]))
        f[i,12]=f[i,13]=mult
        if p.get("bull_risk") and bull and er>=.45 and r24>0 and f[i,14]!=1:
            f[i,12]*=p["bull_risk"]/p["risk"]
        if p.get("bear_long_mult") and bear:f[i,12]*=p["bear_long_mult"]
    return f

def mapped_states(days,valid,hours):
    state=daily_states(days,valid,hours);ema=seeded_ema(days[:,4],100)
    bull=np.zeros(len(days));consecutive=0
    for i in range(len(days)):
        consecutive=consecutive+1 if valid[i] else 0
        if i>=109 and consecutive>=14:
            bull[i]=days[i,4]>ema[i]*1.015 and ema[i]>ema[i-10]
    ix=np.searchsorted(days[:,0]+DAY,hours[:,0]+HOUR,side="right")-1
    ok=ix>=0;state[ok,6]=bull[ix[ok]]
    e20=seeded_ema(days[:,4],20);quality=np.zeros((len(days),2))
    for i in range(24,len(days)):
        if not np.all(valid[i-20:i+1]):continue
        path=np.sum(np.abs(np.diff(days[i-20:i+1,4])))
        quality[i,0]=abs(days[i,4]-days[i-20,4])/path if path else 0.
        quality[i,1]=days[i,4]<e20[i] and e20[i]<e20[i-5]
    mapped=np.zeros((len(hours),2));mapped[ok]=quality[ix[ok]]
    return np.column_stack((state,mapped))

def xirr(assets,start,finish):
    months=pd.date_range(pd.Timestamp(start,unit="ms",tz="UTC").tz_convert("Asia/Shanghai")+pd.offsets.MonthBegin(),
                         pd.Timestamp(finish,unit="ms",tz="UTC").tz_convert("Asia/Shanghai"),freq="MS")
    times=[start]+[int(x.value//1000000) for x in months if int(x.value//1000000)<finish]
    age=np.array([(finish-t)/DAY/365 for t in times])
    # Solve terminal value of each 1000 contribution using a bracket in log(1+r).
    lo,hi=-10.,10.
    for _ in range(100):
        mid=(lo+hi)/2
        if 1000*np.exp(mid*age).sum()>assets:hi=mid
        else:lo=mid
    return math.expm1((lo+hi)/2)*100

@njit(cache=True,nogil=True)
def regime_replay(rows,mask,feat,begin,finish,cfg,deposits,initial):
    # max leverage, long risk/cap, short risk scale/cap, ER, regime, trail,
    # DD limit, daily pause, fee, execution delay in bars, dynamic leverage.
    maxlev,risk,cap,shortscale,shortcap,threshold,regime,trail,limit,dailylimit,fee,delay,dynamic=cfg[:13]
    longgate=cfg[13] if len(cfg)>13 else 1.
    short_distance=cfg[14];short_cooldown=cfg[15];last_exit_dir=1
    short_timeout=cfg[16] if len(cfg)>16 else 336.;pendingboost=1.;pendingmult=1.;entry_state=5;entry_risk=0.;previous_nav=1.
    diagnostic=np.zeros((6,4))
    cash=initial;reserve=0.;contrib=initial;units=initial
    equity=initial;peak=intrapeak=1.;dd=bound=0.;halt=0.;liqs=0
    stake=entry=stop=entryt=entryeq=distance=0.;direction=0;lev=1.;low=high=0.
    pending=0;due=0;pendingdir=0;pendingdistance=0.;reason=0
    last_exit_signal=-1e16;loss_at=-1e16;last_loss=False
    day=-1;day_start=1.;paused=False;ndep=0;nt=0;nd=0
    trades=np.zeros((max(2000,(finish-begin)//900+20),14))
    daily=np.zeros(((finish-begin)//21600+5,8))
    for i in range(begin,finish):
        t=rows[i,0];end=t+4000;op=rows[i,1];cl=rows[i,4]
        this_day=int((t+8*HOUR)//DAY)
        if this_day!=day:day=this_day;day_start=equity/units;paused=False
        while ndep<len(deposits) and t>=deposits[ndep]:
            before=cash+(payout(stake,entry,op,lev,direction,fee) if stake>0 else 0.)
            if not mask[i]:before=equity
            if before>0:units*=1+1000/before
            cash+=1000;contrib+=1000;ndep+=1
        if mask[i] and pending and i>=due:
            if pending==1 and stake==0 and not halt and not paused:
                direction=pendingdir;distance=pendingdistance*(short_distance if direction==-1 and short_distance>0 else 1.)
                lev=maxlev
                if dynamic:lev=min(maxlev,max(1.,math.floor(.60/distance)))
                budget=(risk if direction==1 else risk*shortscale*pendingboost)*pendingmult
                entry_risk=budget
                maxq=cap if direction==1 else shortcap
                q=min(maxq,budget/(distance+fee*(1-direction*distance)),lev)
                amount=np.floor(np.rint(cash*10000)*(q/lev))/10000
                if amount>=1.:
                    entryeq=cash;cash-=amount;stake=amount;entry=op;entryt=t
                    stop=entry*(1-direction*distance);low=high=entry
            elif pending==2 and stake>0:
                low=min(low,op);high=max(high,op)
                returned=payout(stake,entry,op,lev,direction,fee);gain=returned-stake
                mae=max(0.,1-low/entry) if direction==1 else max(0.,high/entry-1)
                mfe=max(0.,high/entry-1) if direction==1 else max(0.,1-low/entry)
                trades[nt]=np.array([entryt,t,direction,stake,gain,stake*lev*op/entry*fee,mae*100,mfe*100,lev,stake*lev/entryeq,gain/entryeq,reason,entry_state,entry_risk]);nt+=1
                cash+=returned;stake=0.;last_loss=gain<0;last_exit_dir=direction
                if last_loss:loss_at=t
                retained=np.floor(max(0.,np.rint(gain*10000))*.10)/10000
                if retained>0:
                    units*=1-retained/cash;cash-=retained;reserve+=retained
            pending=0
        if stake>0 and mask[i]:
            low=min(low,rows[i,3]);high=max(high,rows[i,2])
            favourable=rows[i,2] if direction==1 else rows[i,3]
            adverse=rows[i,3] if direction==1 else rows[i,2]
            best=(cash+payout(stake,entry,favourable,lev,direction,fee))/units
            worst=(cash+payout(stake,entry,adverse,lev,direction,fee))/units
            intrapeak=max(intrapeak,best);bound=max(bound,1-worst/intrapeak)
            liquidation=entry*(1-direction/lev)
            hit=(direction==1 and lev>1 and rows[i,3]<=liquidation) or (direction==-1 and rows[i,2]>=liquidation)
            if hit:
                mae=max(0.,1-low/entry) if direction==1 else max(0.,high/entry-1)
                mfe=max(0.,high/entry-1) if direction==1 else max(0.,1-low/entry)
                trades[nt]=np.array([entryt,end,direction,stake,-stake,0.,mae*100,mfe*100,lev,stake*lev/entryeq,-stake/entryeq,7.,entry_state,entry_risk]);nt+=1
                stake=0.;pending=0;last_loss=True;loss_at=end;last_exit_signal=end;liqs+=1;last_exit_dir=direction
        equity=cash+(payout(stake,entry,cl,lev,direction,fee) if stake>0 else 0.)
        nav=equity/units;peak=max(peak,nav);intrapeak=max(intrapeak,nav)
        dd=max(dd,1-nav/peak);bound=max(bound,1-nav/intrapeak)
        if 1-nav/peak>=limit and not halt:halt=end
        if nav/day_start-1<=-dailylimit:paused=True
        if stake>0 and pending!=2 and mask[i]:
            cause=0
            if halt or paused:cause=5
            elif direction*(cl-stop)<=0:cause=1
            elif end-entryt>=(short_timeout*HOUR if direction==-1 else 14*DAY):cause=4
            elif (i+1)%900==0:
                h=i//900
                if feat[h,6]>0 and ((direction==1 and feat[h,0]==0) or (direction==-1 and feat[h,1]==0)):cause=3
            if cause:pending=2;due=i+1+int(delay);reason=cause;last_exit_signal=end
            elif trail>0 and direction==-1:
                # Update from observed closes after checking the current stop;
                # never assume the intrabar high/low ordering to tighten a stop.
                stop=min(stop,cl+trail*entry*distance/2)
        if mask[i] and (i+1)%900==0 and stake==0 and pending==0 and not halt and not paused:
            h=i//900
            ready=end>=(math.floor(last_exit_signal/HOUR)+1)*HOUR+(short_cooldown if last_exit_dir==-1 else 4.)*HOUR
            if ready and feat[h,6]>0:
                side=0
                if feat[h,0]>0 and feat[h,15]>0:
                    if longgate==0 or not (last_loss and end-loss_at<12*HOUR) or (feat[h,3]>=.3 and feat[h,4]>0):side=1
                elif shortscale>0 and feat[h,1]>0 and feat[h,3]>=threshold:
                    regime_ok=(regime==0 or (regime==1 and feat[h,5]>0) or (regime==2 and feat[h,8]>0) or (regime==3 and feat[h,8]>0 and feat[h,9]>0))
                    if regime_ok and (threshold==0 or feat[h,4]<0):side=-1
                if side:
                    pending=1;due=i+1+int(delay);pendingdir=side;pendingdistance=feat[h,10] if side==-1 and short_distance==0 else feat[h,2]
                    pendingmult=feat[h,12] if side==1 else feat[h,13];entry_state=int(feat[h,14])
                    pendingboost=cfg[17] if side==-1 and len(cfg)>17 and cfg[17]>1 and feat[h,11]>0 else 1.
        # Attribute changes to the last CLOSED hour available at this bar's start.
        state_hour=i//900-1
        known_state=int(feat[state_hour,14]) if state_hour>=0 else 5
        diagnostic[known_state,0]+=1.
        if stake>0:diagnostic[known_state,1]+=1.
        if nav>0 and previous_nav>0 and nav!=previous_nav:diagnostic[known_state,2]+=math.log(nav/previous_nav)
        diagnostic[known_state,3]=max(diagnostic[known_state,3],1-nav/peak)
        previous_nav=nav
        if (end+8*HOUR)%DAY==0:
            daily[nd]=np.array([end,equity+reserve,nav,1-nav/peak,bound,contrib,cash,reserve]);nd+=1
    if stake>0:
        cl=rows[finish-1,4];end=rows[finish-1,0]+4000
        returned=payout(stake,entry,cl,lev,direction,fee);gain=returned-stake
        mae=max(0.,1-low/entry) if direction==1 else max(0.,high/entry-1)
        mfe=max(0.,high/entry-1) if direction==1 else max(0.,1-low/entry)
        trades[nt]=np.array([entryt,end,direction,stake,gain,stake*lev*cl/entry*fee,mae*100,mfe*100,lev,stake*lev/entryeq,gain/entryeq,8.,entry_state,entry_risk]);nt+=1
        cash+=returned
        retained=np.floor(max(0.,np.rint(gain*10000))*.10)/10000
        if retained>0:units*=1-retained/cash;cash-=retained;reserve+=retained
    summary=np.array([cash+reserve,cash,reserve,contrib,cash/units,dd,bound,halt,liqs])
    return summary,trades[:nt],daily[:nd],diagnostic

def main():
    ap=argparse.ArgumentParser();ap.add_argument("--data",type=Path,required=True);ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--refined",action="store_true")
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    rows=np.load(args.data/"BTCUSDT-4s.npy",mmap_mode="r");mask=np.load(args.data/"available.npy",mmap_mode="r")
    hours=np.load(args.data/"hours.npy");valid=np.load(args.data/"hour_valid.npy")
    state=mapped_states(np.load(args.data/"days.npy"),np.load(args.data/"day_valid.npy"),hours)
    origin=int(rows[0,0]);end=int(rows[-1,0])+4000;records=[];half=[];regime_rows=[];cache={}
    candidates=plans(args.refined)
    def run(p,a,z,phase,scenario,save=False):
        start=stamp(a);finish=min(stamp(z),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
        if p["code"] not in cache:cache[p["code"]]=regime_features(hours,valid,state,p)
        months=pd.date_range(pd.Timestamp(a,tz="Asia/Shanghai")+pd.offsets.MonthBegin(),pd.Timestamp(z,tz="Asia/Shanghai"),freq="MS")
        dep=np.array([int(x.value//1000000) for x in months if int(x.value//1000000)<finish],np.int64)
        initial=1000+1000*np.sum(dep<origin);dep=dep[dep>=origin]
        fee=.0004 if scenario=="stress" else .0002;delay=2 if scenario=="stress" else 0
        cfg=np.array([p["leverage"],p["risk"],p["cap"],p["short_risk"]/p["risk"],p["short_cap"],p["short_er"],2,
                      p["trail"],.7,.05,fee,delay,p["dynamic"],0,p["short_distance"],p["short_cooldown"],p.get("short_timeout",336.),
                      p.get("boost_risk",0)/p["short_risk"] if p["short_risk"] else 1.],float)
        started=time.monotonic();s,t,d,g=regime_replay(rows,mask,cache[p["code"]],int(begin),int(stop),cfg,dep,float(initial))
        assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01)
        assert np.isclose(np.sum(g[:,2]),np.log(s[4]),atol=1e-7)
        short=t[t[:,2]<0];years=(finish-start)/DAY/365.25
        m=dict(code=p["code"],phase=phase,scenario=scenario,start=iso(start),end_exclusive=iso(finish),
               total_assets=float(s[0]),net_profit=float(s[0]-s[3]),contributions=float(s[3]),unit_nav=float(s[4]),
               cagr_pct=float((s[4]**(1/years)-1)*100),xirr_pct=xirr(s[0],start,finish),close_dd_pct=float(s[5]*100),dd_bound_pct=float(s[6]*100),
               halt_time=iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),
               win_rate_pct=float(np.mean(t[:,4]>0)*100) if len(t) else None,profit_factor=pf(t[:,4]),normalized_pf=pf(t[:,10]),
               short_trades=len(short),short_profit=float(short[:,4].sum()),short_normalized_pf=pf(short[:,10]),
               mae_pct=float(t[:,6].mean()) if len(t) else None,mfe_pct=float(t[:,7].mean()) if len(t) else None,seconds=round(time.monotonic()-started,2))
        records.append(m)
        if save:
            name=p["code"]+"_"+phase+"_"+scenario;np.save(args.out/(name+"_trades.npy"),t);np.save(args.out/(name+"_daily.npy"),d)
            half.extend(half_year_rows(p["code"],scenario,t,d))
            for k,label in enumerate(STATES):
                tr=t[t[:,12]==k]
                regime_rows.append(dict(code=p["code"],phase=phase,scenario=scenario,state=label,
                                       hours=g[k,0]/900,exposure_pct=g[k,1]/max(1.,g[k,0])*100,nav_log_contribution=g[k,2],
                                       global_dd_during_state_pct=g[k,3]*100,entry_trades=len(tr),win_pct=np.mean(tr[:,4]>0)*100 if len(tr) else None,
                                       normalized_pf=pf(tr[:,10]),mae_pct=tr[:,6].mean() if len(tr) else None,mfe_pct=tr[:,7].mean() if len(tr) else None))
        (args.out/"progress.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
        print(p["code"],phase,scenario,"XIRR",round(m["xirr_pct"],2),"NAV",round(m["cagr_pct"],2),"DD",round(m["dd_bound_pct"],2),m["halt_time"],flush=True)
        return m
    for p in candidates:
        for phase,a,z in [("train","2017-01-01","2023-01-01"),("validation","2023-01-01","2025-01-01")]:
            for scenario in ["normal","stress"]:run(p,a,z,phase,scenario)
    ranking=[]
    for p in candidates[4 if args.refined else 3:]:
        rr=[r for r in records if r["code"]==p["code"]]
        if any(r["halt_time"] or r["dd_bound_pct"]>70 or r["liquidations"] for r in rr):continue
        growth=[]
        for phase in ["train","validation"]:
            growth.append(min(r["xirr_pct"] for r in rr if r["phase"]==phase)/100)
        score=math.expm1((6*math.log1p(growth[0])+2*math.log1p(growth[1]))/8)*100
        ranking.append(dict(code=p["code"],score=score))
    ranking.sort(key=lambda x:x["score"],reverse=True)
    selected=[x["code"] for x in ranking[:2]]
    final=[p for p in candidates if p["code"] in (["long35","hour2","boost10","vol60"] if args.refined else ["long35","hour2","boost10"])+selected]
    protocol=dict(date="2026-10-07",scope="offline research only",data=str(args.data),initial=1000,monthly=1000,reinvest=.9,
                  max_leverage=5,max_nominal_cap=5,daily_pause=.05,permanent_dd=.7,upstream_sha="d2331679f4886bc96ed79aec4928938fae56437e",
                  candidates=candidates,selected=selected,ranking=ranking,refined=args.refined,
                  selection="weighted worst-scenario XIRR: 2017-2022 weight6, 2023-2024 weight2; no selection on full/recent; DD<=70 and zero liquidations",
                  recent_and_full="post-selection diagnostics, repeatedly reused history, not virgin holdout",
                  regime_rules={"priority":STATES,"crash":"24h <= -8% or 6h <= -4%","surge":"24h >= 8% or 6h >= 4%",
                                "range":"ER24 <= .20 and distance to EMA96 <= ATR14","bear":"dailyEMA100 .985 band and 10-day slope falling",
                                "bull":"dailyEMA100 1.015 band and 10-day slope rising","transition":"remaining"},
                  limitations=["historic spot prices differ from live feed","no funding/borrow/slippage by simulator contract",
                               "per-trade retention is not monthly fund accounting","fees doubled plus extra8s execution in stress",
                               "70% close halt cannot bound gap loss or intrabar DD","state contributions are not separately tradable returns"])
    (args.out/"protocol.json").write_text(json.dumps(protocol,indent=2),encoding="utf-8")
    for p in final:
        for scenario in ["normal","stress"]:
            run(p,"2025-01-01","2026-10-01","recent",scenario)
            run(p,"2017-01-01","2026-10-01","full",scenario,True)
    (args.out/"results.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
    pd.DataFrame(records).to_csv(args.out/"summary.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(half).to_csv(args.out/"half_year.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(regime_rows).to_csv(args.out/"regimes.csv",index=False,encoding="utf-8-sig")
    (args.out/"engine_snapshot.py").write_text(Path(__file__).read_text(encoding="utf-8"),encoding="utf-8")
    print("COMPLETE",selected,flush=True)

if __name__=="__main__":main()
