"""Offline risk-tier study; no live service imports or operations."""
from __future__ import annotations
import argparse,json,math,time
from pathlib import Path
import numpy as np
import pandas as pd
from numba import njit
from btc_regime_research import regime_features,mapped_states,xirr,STATES
from btc_bear_research import seeded_ema,pf,half_year_rows
from btc_long_short_research import HOUR,DAY,payout,stamp,iso
import btc_regime_research as r

@njit(cache=True)
def drawdown_factor(dd,limit,start,floor):
    if start<=0 or dd<=start:return 1.
    return max(floor,min(1.,(limit-dd)/(limit-start)))

def plans(turning=False):
    baseline=dict(r.plans()[0],tier="moderate",code="M0",risk=.0125,cap=2.,leverage=3.,
                  short_risk=0.,short_cap=.5,daily_pause=.015,dd_limit=.25,longgate=1)
    out=[baseline,dict(r.plans()[1],tier="moderate",code="M_old_small",control="small",risk=.0125,cap=2.,leverage=3.,
                      short_risk=.0025,short_cap=.5,short_er=.35,short_distance=1.,trail=3.,dynamic=0,daily_pause=.015,dd_limit=.25,longgate=1)]
    for target in [.4,.6,.8]:out.append(dict(baseline,code="M_vol"+str(int(target*100)),vol_target=target))
    for risk in [.01,.015,.02]:
        out.append(dict(baseline,code="M_r"+str(int(risk*10000)),risk=risk,vol_target=.6))
    for period in [192,384]:
        out.append(dict(baseline,code="M_ema"+str(period),long_ema=period,vol_target=.6))
    for risk,regime in [(.0025,"ema100"),(.0025,"ema200"),(.005,"ema100")]:
        out.append(dict(baseline,code=f"M_short{int(risk*10000)}_"+regime,control=None,short_risk=risk,short_cap=.5,
                        short_er=.30,regime=regime,entry_ema=96,exit_ema=96,exit_band=.005,entry="trend",veto=0,
                        short_distance=1.5,trail=4.,dynamic=1,vol_target=.6,short_confirm="consensus"))
    out.append(dict(baseline,code="M_dd8",vol_target=.6,dd_start=.08,dd_floor=.35))
    for risk in [.015,.02]:
        out.append(dict(baseline,code=f"M_dd8_r{int(risk*10000)}",risk=risk,vol_target=.6,dd_start=.08,dd_floor=.35))
    for s in out:s["family"]="moderate"
    growth=next(p for p in r.plans(True) if p["code"]=="vol60_consensus").copy()
    growth.update(code="G0",tier="growth",family="growth",daily_pause=.05,dd_limit=.70,longgate=0)
    out.append(growth)
    for start,floor in [(.20,.5),(.30,.35)]:
        out.append(dict(growth,code="G_dd"+str(int(start*100)),dd_start=start,dd_floor=floor))
    for count,mult in [(2,.5),(3,.5),(2,.25)]:
        out.append(dict(growth,code=f"G_loss{count}_{int(mult*100)}",short_loss_count=count,short_loss_mult=mult))
    out.append(dict(growth,code="G_dd_loss",dd_start=.20,dd_floor=.5,short_loss_count=2,short_loss_mult=.5))
    for risk in [.05,.07]:
        out.append(dict(growth,code=f"G_boost{int(risk*100)}_loss",boost_risk=risk,short_loss_count=2,short_loss_mult=.5))
    for period in [192,384]:
        out.append(dict(growth,code="G_ema"+str(period),long_ema=period))
    out.append(dict(growth,code="G_crash_half",crash_mult=.5))
    if turning:
        m=next(p for p in out if p["code"]=="M_vol80")
        g=next(p for p in out if p["code"]=="G0")
        out=[next(p for p in out if p["code"]=="M0"),m,g]
        for scale in [.5,.75]:
            out += [dict(m,code=f"M_turn{int(scale*100)}",turn_mult=scale),
                    dict(g,code=f"G_turn{int(scale*100)}",turn_mult=scale)]
        out += [dict(m,code="M_dd_r175",risk=.0175,vol_target=.6,dd_start=.08,dd_floor=.35),
                dict(m,code="M_dd_r200_floor25",risk=.02,vol_target=.6,dd_start=.08,dd_floor=.25),
                dict(m,code="M_L2",leverage=2.)]
    return out

def tier_features(hours,valid,state,p):
    f=regime_features(hours,valid,state,p)
    period=p.get("long_ema",96)
    if period!=96:
        ema=seeded_ema(hours[:,4],period);continuous=0
        for i in range(len(hours)):
            continuous=continuous+1 if valid[i] else 0
            if continuous<max(206,period+24):f[i,6]=0;f[i,0]=0;continue
            f[i,0]=hours[i,4]>ema[i] and ema[i]>ema[i-24]
    if p.get("turn_mult"):
        e24=seeded_ema(hours[:,4],24)
        for i in range(30,len(hours)):
            # Fast/slow disagreement changes NEW entry risk, not the exit trend.
            correction=hours[i,4]<e24[i] and e24[i]<e24[i-6]
            rebound=hours[i,4]>e24[i] or e24[i]>e24[i-6]
            if correction:f[i,12]*=p["turn_mult"]
            if rebound:f[i,13]*=p["turn_mult"]
    if p.get("crash_mult"):
        f[f[:,14]==0,12:14]*=p["crash_mult"]
    return f

@njit(cache=True,nogil=True)
def tier_replay(rows,mask,feat,begin,finish,cfg,deposits,initial):
    # max leverage, long risk/cap, short risk scale/cap, ER, regime, trail,
    # DD limit, daily pause, fee, execution delay in bars, dynamic leverage.
    maxlev,risk,cap,shortscale,shortcap,threshold,regime,trail,limit,dailylimit,fee,delay,dynamic=cfg[:13]
    longgate=cfg[13] if len(cfg)>13 else 1.
    short_distance=cfg[14];short_cooldown=cfg[15];last_exit_dir=1
    short_timeout=cfg[16] if len(cfg)>16 else 336.;pendingboost=1.;pendingmult=1.;entry_state=5;entry_risk=0.;previous_nav=1.
    diagnostic=np.zeros((6,4))
    short_losses=0
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
                if direction==-1:short_losses=short_losses+1 if gain<0 else 0
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
                if direction==-1:short_losses+=1
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
                    if len(cfg)>19 and cfg[18]>0:
                        pendingmult*=drawdown_factor(1-nav/peak,limit,cfg[18],cfg[19])
                    if side==-1 and len(cfg)>21 and cfg[20]>0 and short_losses>=cfg[20]:pendingmult*=cfg[21]
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
    ap.add_argument("--turning",action="store_true")
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    rows=np.load(args.data/"BTCUSDT-4s.npy",mmap_mode="r");mask=np.load(args.data/"available.npy",mmap_mode="r")
    hours=np.load(args.data/"hours.npy");valid=np.load(args.data/"hour_valid.npy")
    state=mapped_states(np.load(args.data/"days.npy"),np.load(args.data/"day_valid.npy"),hours)
    origin=int(rows[0,0]);end=int(rows[-1,0])+4000;records=[];halves=[];groups=[];cache={};grid=plans(args.turning)
    def run(p,a,z,phase,scenario,save=False):
        start=stamp(a);finish=min(stamp(z),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
        key=tuple((k,str(v)) for k,v in sorted(p.items()) if k not in ["code","tier","family","risk","short_risk","cap","short_cap","leverage","dd_start","dd_floor","short_loss_count","short_loss_mult"])
        if key not in cache:cache[key]=tier_features(hours,valid,state,p)
        dates=pd.date_range(pd.Timestamp(a,tz="Asia/Shanghai")+pd.offsets.MonthBegin(),pd.Timestamp(z,tz="Asia/Shanghai"),freq="MS")
        deposits=np.array([int(x.value//1000000) for x in dates if int(x.value//1000000)<finish],np.int64)
        initial=1000+1000*np.sum(deposits<origin);deposits=deposits[deposits>=origin]
        fee=.0004 if scenario=="stress" else .0002;delay=2 if scenario=="stress" else 0
        cfg=np.array([p["leverage"],p["risk"],p["cap"],p["short_risk"]/p["risk"],p["short_cap"],p["short_er"],2,p["trail"],
                      p["dd_limit"],p["daily_pause"],fee,delay,p["dynamic"],p["longgate"],p["short_distance"],p["short_cooldown"],
                      p.get("short_timeout",336.),p.get("boost_risk",0)/p["short_risk"] if p["short_risk"] else 1.,
                      p.get("dd_start",0),p.get("dd_floor",1),p.get("short_loss_count",0),p.get("short_loss_mult",1)],float)
        began=time.monotonic();s,t,d,g=tier_replay(rows,mask,cache[key],int(begin),int(stop),cfg,deposits,float(initial))
        assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01)
        assert np.isclose(g[:,2].sum(),np.log(s[4]),atol=1e-7)
        short=t[t[:,2]<0];years=(finish-start)/DAY/365.25
        m=dict(code=p["code"],tier=p["tier"],phase=phase,scenario=scenario,start=iso(start),end_exclusive=iso(finish),total_assets=float(s[0]),net_profit=float(s[0]-s[3]),
               contributions=float(s[3]),unit_nav=float(s[4]),cagr_pct=(s[4]**(1/years)-1)*100,xirr_pct=xirr(s[0],start,finish),close_dd_pct=s[5]*100,
               dd_bound_pct=s[6]*100,halt_time=iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),
               win_rate_pct=np.mean(t[:,4]>0)*100 if len(t) else None,profit_factor=pf(t[:,4]),normalized_pf=pf(t[:,10]),
               short_trades=len(short),short_profit=float(short[:,4].sum()),short_normalized_pf=pf(short[:,10]),
               mae_pct=np.mean(t[:,6]) if len(t) else None,mfe_pct=np.mean(t[:,7]) if len(t) else None,seconds=round(time.monotonic()-began,2))
        records.append(m)
        if save:
            name=p["code"]+"_"+phase+"_"+scenario
            np.save(args.out/(name+"_trades.npy"),t);np.save(args.out/(name+"_daily.npy"),d)
            if phase=="full":halves.extend(half_year_rows(p["code"],scenario,t,d))
            for k,label in enumerate(STATES):
                tr=t[t[:,12]==k];groups.append(dict(code=p["code"],tier=p["tier"],phase=phase,scenario=scenario,state=label,hours=g[k,0]/900,
                                exposure_pct=g[k,1]/max(1,g[k,0])*100,nav_log_contribution=g[k,2],entry_trades=len(tr),
                                win_pct=np.mean(tr[:,4]>0)*100 if len(tr) else None,normalized_pf=pf(tr[:,10])))
        (args.out/"progress.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
        print(p["code"],phase,scenario,"NAV",round(m["cagr_pct"],2),"XIRR",round(m["xirr_pct"],2),"DD",round(m["dd_bound_pct"],2),m["halt_time"],flush=True)
    for p in grid:
        for phase,a,z in [("train","2017-01-01","2023-01-01"),("validation","2023-01-01","2025-01-01")]:
            for scenario in ["normal","stress"]:run(p,a,z,phase,scenario)
    rankings={};selected=[]
    for tier in ["moderate","growth"]:
        ranks=[]
        for p in grid:
            if p["tier"]!=tier or p["code"] in (["M0","M_old_small","M_vol80","M_L2","G0"] if args.turning else ["M0","M_old_small","G0"]):continue
            rr=[m for m in records if m["code"]==p["code"]]
            if any(m["halt_time"] or m["liquidations"] or m["dd_bound_pct"]>p["dd_limit"]*100 for m in rr):continue
            lo=[min(m["xirr_pct"] for m in rr if m["phase"]==phase)/100 for phase in ["train","validation"]]
            score=math.expm1((6*math.log1p(lo[0])+2*math.log1p(lo[1]))/8)*100
            ranks.append(dict(code=p["code"],score=score,max_dd=max(m["dd_bound_pct"] for m in rr)))
        ranks.sort(key=lambda x:x["score"],reverse=True);rankings[tier]=ranks
        selected += [x["code"] for x in ranks[:2]]
    finalcodes=list(dict.fromkeys((["M0","M_vol80","M_L2","G0"] if args.turning else ["M0","M_old_small","G0"])+selected))
    protocol=dict(date="2026-10-07",scope="offline research only; no live fund changes",data=str(args.data),candidates=grid,
                  selected=selected,ranking=rankings,final_codes=finalcodes,turning=args.turning,
                  moderate_constraints=dict(dd_limit=.25,daily_pause=.015,max_leverage=3,max_nominal=2),
                  growth_constraints=dict(dd_limit=.70,daily_pause=.05,max_leverage=5,max_nominal=5),
                  selection="train2017-2022 weight6, validation2023-2024 weight2, worst normal/stress XIRR; zero liquidation, no halt, DD within tier limit",
                  limitations=["same history repeatedly reused; no virgin holdout","4s spot execution; closed hourly/daily signals","no funding/interest/slippage in simulator",
                               "90% per-positive-trade reinvestment differs from monthly fund accounting","close DD halt may overshoot; intrabar bound is conservative"],
                  cashflow=dict(initial=1000,monthly=1000,reinvest=.9),extra_rules=dict(dd="at entry signal scale by max(floor,min(1,(limit-current_nav_dd)/(limit-start))); no forced new exits",
                           short_gate="consecutive closed short losses only; reset by nonloss short; reduced budget until recovery, never future outcomes"))
    (args.out/"protocol.json").write_text(json.dumps(protocol,indent=2),encoding="utf-8")
    for p in grid:
        if p["code"] not in finalcodes:continue
        for scenario in ["normal","stress"]:
            run(p,"2017-01-01","2026-10-01","full",scenario,True)
            run(p,"2025-01-01","2026-10-01","recent",scenario,True)
    (args.out/"results.json").write_text(json.dumps(records,indent=2),encoding="utf-8")
    pd.DataFrame(records).to_csv(args.out/"summary.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(halves).to_csv(args.out/"half_year.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(groups).to_csv(args.out/"regimes.csv",index=False,encoding="utf-8-sig")
    (args.out/"engine_snapshot.py").write_text(Path(__file__).read_text(encoding="utf-8"),encoding="utf-8")
    print("COMPLETE",selected,flush=True)
if __name__=="__main__":main()
