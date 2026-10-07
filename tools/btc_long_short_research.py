"""Offline research only: BTC hourly direction signals, authentic 4s execution.

Run with the research Python environment (numpy, numba, pandas, matplotlib).
No service imports, credentials, network calls, or trading writes.
"""
from __future__ import annotations
import argparse,json,math,time,hashlib
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
import pandas as pd
from numba import njit

HOUR=3600000
DAY=86400000
@njit(cache=True)
def payout(stake,entry,price,lev,direction,fee):
    n=np.rint(stake*10000)
    ratio=price/entry
    move=1-ratio if direction==-1 else ratio-1
    notional=n*lev
    return max(0.,np.floor(n+notional*move-notional*ratio*fee))/10000

@njit(cache=True)
def features(hours,valid,short_ema,daily_bear):
    n=len(hours);out=np.zeros((n,10));ema=np.zeros((n,3));atr=0.;consecutive=0
    periods=np.array([96,short_ema,200]);sums=np.zeros(3);values=np.zeros(3)
    for i in range(n):
        c=hours[i,4]
        for j in range(3):
            p=periods[j]
            if i<p:sums[j]+=c
            if i==p-1:values[j]=sums[j]/p
            elif i>=p:values[j]=2/(p+1)*c+(p-1)/(p+1)*values[j]
            ema[i,j]=values[j]
        tr=hours[i,2]-hours[i,3] if i==0 else max(hours[i,2]-hours[i,3],abs(hours[i,2]-hours[i-1,4]),abs(hours[i,3]-hours[i-1,4]))
        if i<14:atr+=tr/14
        else:atr=(13*atr+tr)/14
        consecutive=consecutive+1 if valid[i] else 0
        if i<max(205,short_ema+23) or consecutive<206:continue
        path=0.
        for k in range(i-11,i+1):path+=abs(hours[k,4]-hours[k-1,4])
        change=c-hours[i-12,4]
        out[i,0]=c>ema[i,0] and ema[i,0]>ema[i-24,0]
        out[i,1]=c<ema[i,1] and ema[i,1]<ema[i-24,1]
        out[i,2]=2*atr/c
        out[i,3]=abs(change)/path if path>0 else 0.
        out[i,4]=change
        out[i,5]=c<ema[i,2] and ema[i,2]<ema[i-24,2]
        out[i,6]=1
        out[i,7]=c
        out[i,8]=daily_bear[i]
        previous_low=np.inf
        for k in range(max(0,i-48),i):previous_low=min(previous_low,hours[k,3])
        out[i,9]=c<previous_low
    return out

@njit(cache=True,nogil=True)
def replay(rows,mask,feat,begin,finish,cfg,deposits,initial):
    # max leverage, long risk/cap, short risk scale/cap, ER, regime, trail,
    # DD limit, daily pause, fee, execution delay in bars, dynamic leverage.
    maxlev,risk,cap,shortscale,shortcap,threshold,regime,trail,limit,dailylimit,fee,delay,dynamic=cfg[:13]
    longgate=cfg[13] if len(cfg)>13 else 1.
    cash=initial;reserve=0.;contrib=initial;units=initial
    equity=initial;peak=intrapeak=1.;dd=bound=0.;halt=0.;liqs=0
    stake=entry=stop=entryt=entryeq=distance=0.;direction=0;lev=1.;low=high=0.
    pending=0;due=0;pendingdir=0;pendingdistance=0.;reason=0
    last_exit_signal=-1e16;loss_at=-1e16;last_loss=False
    day=-1;day_start=1.;paused=False;ndep=0;nt=0;nd=0
    trades=np.zeros((max(2000,(finish-begin)//900+20),12))
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
                direction=pendingdir;distance=pendingdistance
                lev=maxlev
                if dynamic:lev=min(maxlev,max(1.,math.floor(.60/distance)))
                budget=risk if direction==1 else risk*shortscale
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
                trades[nt]=np.array([entryt,t,direction,stake,gain,stake*lev*op/entry*fee,mae*100,mfe*100,lev,stake*lev/entryeq,gain/entryeq,reason]);nt+=1
                cash+=returned;stake=0.;last_loss=gain<0
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
                trades[nt]=np.array([entryt,end,direction,stake,-stake,0.,mae*100,mfe*100,lev,stake*lev/entryeq,-stake/entryeq,7.]);nt+=1
                stake=0.;pending=0;last_loss=True;loss_at=end;last_exit_signal=end;liqs+=1
        equity=cash+(payout(stake,entry,cl,lev,direction,fee) if stake>0 else 0.)
        nav=equity/units;peak=max(peak,nav);intrapeak=max(intrapeak,nav)
        dd=max(dd,1-nav/peak);bound=max(bound,1-nav/intrapeak)
        if 1-nav/peak>=limit and not halt:halt=end
        if nav/day_start-1<=-dailylimit:paused=True
        if stake>0 and pending!=2 and mask[i]:
            cause=0
            if halt or paused:cause=5
            elif direction*(cl-stop)<=0:cause=1
            elif end-entryt>=14*DAY:cause=4
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
            ready=end>=(math.floor(last_exit_signal/HOUR)+1)*HOUR+4*HOUR
            if ready and feat[h,6]>0:
                side=0
                if feat[h,0]>0:
                    if longgate==0 or not (last_loss and end-loss_at<12*HOUR) or (feat[h,3]>=.3 and feat[h,4]>0):side=1
                elif shortscale>0 and feat[h,1]>0 and feat[h,3]>=threshold:
                    regime_ok=(regime==0 or (regime==1 and feat[h,5]>0) or (regime==2 and feat[h,8]>0) or (regime==3 and feat[h,8]>0 and feat[h,9]>0))
                    if regime_ok and (threshold==0 or feat[h,4]<0):side=-1
                if side:
                    pending=1;due=i+1+int(delay);pendingdir=side;pendingdistance=feat[h,2]
        if (end+8*HOUR)%DAY==0:
            daily[nd]=np.array([end,equity+reserve,nav,1-nav/peak,bound,contrib,cash,reserve]);nd+=1
    if stake>0:
        cl=rows[finish-1,4];end=rows[finish-1,0]+4000
        returned=payout(stake,entry,cl,lev,direction,fee);gain=returned-stake
        mae=max(0.,1-low/entry) if direction==1 else max(0.,high/entry-1)
        mfe=max(0.,high/entry-1) if direction==1 else max(0.,1-low/entry)
        trades[nt]=np.array([entryt,end,direction,stake,gain,stake*lev*cl/entry*fee,mae*100,mfe*100,lev,stake*lev/entryeq,gain/entryeq,8.]);nt+=1
        cash+=returned
        retained=np.floor(max(0.,np.rint(gain*10000))*.10)/10000
        if retained>0:units*=1-retained/cash;cash-=retained;reserve+=retained
    summary=np.array([cash+reserve,cash,reserve,contrib,cash/units,dd,bound,halt,liqs])
    return summary,trades[:nt],daily[:nd]

def stamp(s):return int(pd.Timestamp(s,tz='Asia/Shanghai').value//1000000)
def iso(t):return pd.Timestamp(int(t),unit='ms',tz='UTC').tz_convert('Asia/Shanghai').isoformat()
def plans():
    out=[]
    for family,risk,cap,lev,limit,daily in [('moderate',.0125,2.,3.,.25,.015),('aggressive',.05,5.,5.,.70,.05)]:
        base=dict(family=family,risk=risk,cap=cap,leverage=lev,dd=limit,daily=daily,short_ema=96,short_scale=0.,short_cap=cap,short_er=0.,regime=0,trail=0.,dynamic=0,long_gate=1 if family=='moderate' else 0)
        out.append(dict(base,code=family+'_long'))
        variations=[('mirror',1.,0.,0,96,0.,lev,cap),('er30_half',.5,.30,0,96,0.,lev,cap*.5),('er30',1.,.30,0,96,0.,lev,cap),('bear200',.5,.30,1,96,0.,lev,cap*.5),('slow192',.5,.30,1,192,0.,lev,cap*.5),('trail3',.5,.30,1,96,3.,lev,cap*.5),('er45',.5,.45,1,96,0.,lev,cap*.5),('lev7',.75,.30,1,96,0.,7.,3. if family=='moderate' else 7.),('lev10',1.,.30,1,96,0.,10.,3. if family=='moderate' else 8.),('daily200_small',.2,.35,2,96,3.,lev,cap*.25),('daily200_half',.5,.35,2,96,3.,lev,cap*.5),('break48',.3,.45,3,96,3.,lev,cap*.35)]
        for name,scale,er,reg,ema,trail,l,cp in variations:
            out.append(dict(base,code=family+'_'+name,short_scale=scale,short_er=er,regime=reg,short_ema=ema,trail=trail,leverage=l,cap=cp if name in ('lev7','lev10') else cap,short_cap=cp))
    return out

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--risk-sweep',action='store_true')
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    rows=np.load(args.data/'BTCUSDT-4s.npy',mmap_mode='r');mask=np.load(args.data/'available.npy',mmap_mode='r')
    hours=np.load(args.data/'hours.npy');valid=np.load(args.data/'hour_valid.npy')
    origin=int(rows[0,0]);end=int(rows[-1,0])+4000;cache={};records=[];details={}
    days=np.load(args.data/'days.npy');dv=np.load(args.data/'day_valid.npy')
    dema=np.full(len(days),np.nan);value=0.
    for i,c in enumerate(days[:,4]):
        if i<200:value+=c
        if i==199:value/=200
        elif i>=200:value=2/201*c+199/201*value
        if i>=199:dema[i]=value
    bears=np.zeros(len(days))
    for i in range(209,len(days)):
        if dv[i-10:i+1].all():bears[i]=days[i,4]<dema[i] and dema[i]<dema[i-10]
    last_day=np.searchsorted(days[:,0]+DAY,hours[:,0]+HOUR,side='right')-1
    daily_bear=np.array([bears[i] if i>=0 else 0. for i in last_day])
    candidates=plans()
    if args.risk_sweep:
        base=[p for p in candidates if p['code'] in ('aggressive_long','aggressive_daily200_small')]
        candidates=[dict(p,code=p['code']+'_risk'+str(int(risk*10000)),risk=risk) for p in base for risk in [.03,.035,.04,.045,.05]]
    def run(p,a,b,scenario='normal',save=False):
        start=stamp(a);finish=min(stamp(b),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
        if p['short_ema'] not in cache:cache[p['short_ema']]=features(hours,valid,p['short_ema'],daily_bear)
        months=pd.date_range(pd.Timestamp(a,tz='Asia/Shanghai').normalize().replace(day=1)+pd.offsets.MonthBegin(),pd.Timestamp(b,tz='Asia/Shanghai'),freq='MS')
        dep=np.array([int(d.value//1000000) for d in months if int(d.value//1000000)<finish],dtype=np.int64)
        initial=1000+1000*np.sum(dep<origin);dep=dep[dep>=origin]
        fee=.0004 if scenario=='stress' else .0002;delay=2 if scenario=='stress' else 0
        cfg=np.array([p['leverage'],p['risk'],p['cap'],p['short_scale'],p['short_cap'],p['short_er'],p['regime'],p['trail'],p['dd'],p['daily'],fee,delay,p['dynamic'],p['long_gate']],float)
        began=time.monotonic();s,t,d=replay(rows,mask,cache[p['short_ema']],int(begin),int(stop),cfg,dep,float(initial))
        assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01),'cashflow conservation'
        gain=t[:,4];norm=t[:,10];years=(finish-start)/DAY/365.25
        pf=lambda x:float(x[x>0].sum()/-x[x<0].sum()) if np.any(x<0) else None
        m=dict(code=p['code'],family=p['family'],start=iso(start),end_exclusive=iso(finish),scenario=scenario,total_assets=float(s[0]),net_profit=float(s[0]-s[3]),contributions=float(s[3]),unit_nav=float(s[4]),unit_nav_cagr_pct=float((s[4]**(1/years)-1)*100),close_dd_pct=float(s[5]*100),intrabar_dd_bound_pct=float(s[6]*100),halt_time=iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),win_rate_pct=float(np.mean(gain>0)*100) if len(t) else None,profit_factor=pf(gain),normalized_profit_factor=pf(norm),mae_pct=float(t[:,6].mean()) if len(t) else None,mfe_pct=float(t[:,7].mean()) if len(t) else None,short_trades=int(np.sum(t[:,2]<0)),short_profit=float(gain[t[:,2]<0].sum()),seconds=round(time.monotonic()-began,2))
        records.append(m)
        print(p['code'],a,scenario,round(m['unit_nav_cagr_pct'],2),round(m['intrabar_dd_bound_pct'],2),m['halt_time'],flush=True)
        if save:
            name=p['code']+'_'+scenario
            np.save(args.out/(name+'_trades.npy'),t);np.save(args.out/(name+'_daily.npy'),d)
            details[name]=(m,t,d)
        (args.out/'progress.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        return m
    for a,b in [('2017-01-01','2023-01-01'),('2023-01-01','2025-01-01')]:
        for p in candidates:
            run(p,a,b)
            if args.risk_sweep:run(p,a,b,'stress')
    selected=[]
    for family in sorted(set(p['family'] for p in candidates)):
        eligible=[]
        for p in candidates:
            if p['family']!=family or (p['short_scale']==0 and not args.risk_sweep):continue
            train=[r for r in records if r['code']==p['code'] and r['start'].startswith('2017') and r['scenario']=='normal'][0]
            val=[r for r in records if r['code']==p['code'] and r['start'].startswith('2023') and r['scenario']=='normal'][0]
            guards=[r for r in records if r['code']==p['code']]
            if args.risk_sweep and any(r['halt_time'] or r['intrabar_dd_bound_pct']>p['dd']*100 for r in guards):continue
            if not train['halt_time'] and not val['halt_time'] and max(train['intrabar_dd_bound_pct'],val['intrabar_dd_bound_pct'])<=p['dd']*100 and val['trades']>=30:
                score=val['unit_nav_cagr_pct']/max(5.,val['intrabar_dd_bound_pct'])
                if args.risk_sweep:
                    vals=[r for r in guards if r['start'].startswith('2023')]
                    score=min(r['unit_nav_cagr_pct'] for r in vals)/max(5.,max(r['intrabar_dd_bound_pct'] for r in vals))
                eligible.append((score,p))
        chosen=max(eligible,key=lambda z:z[0])[1] if eligible else [p for p in candidates if p['family']==family and p['short_scale']==0][0]
        selected.append(chosen)
    fullplans=list({p['code']:p for p in [p for p in candidates if p['short_scale']==0]+selected}.values())
    if args.risk_sweep:
        controls=[p for p in candidates if p['risk'] in (.04,.05)]
        fullplans=list({p['code']:p for p in controls+selected}.values())
    for p in fullplans:
        run(p,'2025-01-01','2026-10-01',save=True)
        run(p,'2017-01-01','2026-10-01',save=True)
        run(p,'2017-01-01','2026-10-01','stress',save=True)
    # Isolate leverage: identical nominal cap/risk, different margin and liquidation distance.
    for original in ([] if args.risk_sweep else selected):
        for l in [2.,4.,7.,10.]:
            if l<original['cap']:continue
            p=dict(original,code=original['code']+'_margin'+str(int(l)),leverage=l)
            run(p,'2017-01-01','2026-10-01')
        p=dict(original,code=original['code']+'_dynamic',dynamic=1)
        run(p,'2017-01-01','2026-10-01')
    protocol=dict(date='2026-10-07',upstream_sha='d2331679f4886bc96ed79aec4928938fae56437e',data=str(args.data),price_start=iso(origin),end_exclusive=iso(end),rows=len(rows),initial=1000,monthly=1000,reinvest=.9,candidates=candidates,selected=selected,selection=('train/validation normal and stress eligibility; worst validation CAGR/drawdown; chronological test after selection; historical prices previously reused' if args.risk_sweep else 'train 2017-2022 eligibility, validation 2023-2024 CAGR/drawdown; held test 2025-2026, no reselection after test'),limitations=['historical data previously reused; held test is chronological, not virgin data','research cash buckets, not monthly fund share/distribution ledger','4s OHLC intrabar best-before-worst drawdown bound; live stop checked at close then next observed open','one position per fund, aligned 4h cooldown; no funding/interest per site simulator','monthly deposits after permanent halt stay cash; no automatic restart','BTC spot data; simulator source may use Binance/Bybit different feed','history ends 2026-10-01; excludes Oct 1-7'])
    (args.out/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.out/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    pd.DataFrame(records).to_csv(args.out/'summary.csv',index=False,encoding='utf-8-sig')
    # Annual/half-year returns from daily cashflow-neutral NAV; strategy remains continuous.
    intervals=[]
    for key,(m,t,d) in details.items():
        if not m['start'].startswith('2017') or m['scenario']!='normal':continue
        frame=pd.DataFrame(d,columns=['time','assets','nav','dd','bound','inputs','cash','reserve'])
        dates=pd.to_datetime(frame.time.astype('int64'),unit='ms',utc=True).dt.tz_convert('Asia/Shanghai')-pd.Timedelta(milliseconds=1)
        frame['period']=dates.dt.year.astype(str)+'H'+np.where(dates.dt.month<=6,'1','2')
        prior=1.
        for period,part in frame.groupby('period',sort=True):
            left=float(part.iloc[0].time-DAY);right=float(part.iloc[-1].time)
            tr=t[(t[:,1]>left)&(t[:,1]<=right)];norm=tr[:,10]
            peak=prior;localdd=0.
            for nav in part.nav:peak=max(peak,nav);localdd=max(localdd,1-nav/peak)
            current=float(part.iloc[-1].nav)
            intervals.append(dict(code=m['code'],period=period,return_pct=(current/prior-1)*100,daily_dd_pct=localdd*100,trades=len(tr),win_rate_pct=float(np.mean(tr[:,4]>0)*100) if len(tr) else None,normalized_profit_factor=float(norm[norm>0].sum()/-norm[norm<0].sum()) if np.any(norm<0) else None,short_profit=float(tr[tr[:,2]<0,4].sum()),mae_pct=float(tr[:,6].mean()) if len(tr) else None,mfe_pct=float(tr[:,7].mean()) if len(tr) else None))
            prior=current
    pd.DataFrame(intervals).to_csv(args.out/'half_year.csv',index=False,encoding='utf-8-sig')
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,axes=plt.subplots(2,1,figsize=(11,7),sharex=True)
    for key,(m,t,d) in details.items():
        if not m['start'].startswith('2017') or m['scenario']!='normal':continue
        dt=pd.to_datetime(d[:,0],unit='ms',utc=True)
        axes[0].plot(dt,d[:,2],label=m['code']);axes[1].plot(dt,d[:,3]*100,label=m['code'])
    axes[0].set_yscale('log');axes[0].set_ylabel('Trading unit NAV (log)');axes[1].set_ylabel('Daily drawdown (%)');axes[0].legend(fontsize=8);axes[0].grid(alpha=.25);axes[1].grid(alpha=.25)
    fig.tight_layout();fig.savefig(args.out/'comparison.png',dpi=160);plt.close(fig)
    print('COMPLETE',json.dumps(selected),flush=True)
if __name__=='__main__':main()
