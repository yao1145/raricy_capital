"""Offline bear regime / stronger short research. Does not import the fund service."""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import time
from pathlib import Path
import numpy as np
import pandas as pd
from numba import njit
from btc_long_short_research import HOUR,DAY,payout,features,stamp,iso


def seeded_ema(close, period):
    out=np.full(len(close),np.nan)
    if len(close)<period:return out
    value=float(np.mean(close[:period]));out[period-1]=value
    for i in range(period,len(close)):
        value=2/(period+1)*close[i]+(period-1)/(period+1)*value;out[i]=value
    return out


def daily_states(days,valid,hours):
    close=days[:,4];ema={p:seeded_ema(close,p) for p in [20,60,100,200]}
    states=np.zeros((len(days),7));consecutive=0;atr=0.
    for i,c in enumerate(close):
        tr=days[i,2]-days[i,3] if i==0 else max(days[i,2]-days[i,3],abs(days[i,2]-close[i-1]),abs(days[i,3]-close[i-1]))
        if i<14:atr+=tr/14
        else:atr=(13*atr+tr)/14
        consecutive=consecutive+1 if valid[i] else 0
        if consecutive<14:continue
        states[i,5]=2*atr/c
        if i>=209:
            states[i,0]=c<ema[200][i]*.985 and ema[200][i]<ema[200][i-10]
            states[i,4]=c<ema[200][i] and ema[200][i]<ema[200][i-10]
        if i>=109:states[i,1]=c<ema[100][i]*.985 and ema[100][i]<ema[100][i-10]
        if i>=64:states[i,2]=c<ema[60][i]*.985 and ema[20][i]<ema[60][i] and ema[60][i]<ema[60][i-5]
        if i>=24:states[i,3]=c/close[i-7]-1<-.08 and c<ema[20][i] and ema[20][i]<ema[20][i-5]
    # A daily close is usable only when that day's candle has ended.
    ix=np.searchsorted(days[:,0]+DAY,hours[:,0]+HOUR,side='right')-1
    out=np.zeros((len(hours),7));good=ix>=0;out[good]=states[ix[good]]
    return out


def bear_features(hours,valid,state,p):
    f=np.column_stack((features(hours,valid,96,state[:,0]),state[:,5] if state.shape[1]>5 else np.zeros(len(hours)),np.zeros(len(hours))))
    if p.get('control'):
        if p['control']=='small':
            # Reproduce the previous 200-day small-short control, without 1.5% band.
            f[:,8]=state[:,4]
        return f
    ema={v:seeded_ema(hours[:,4],v) for v in set([24,p['entry_ema'],p['exit_ema']])}
    j=['ema200','ema100','ema60','week8'].index(p['regime'])
    bear=state[:,j]>0
    for i in range(len(hours)):
        if f[i,6]==0:continue
        close=hours[i,4]
        entrytrend=close<ema[p['entry_ema']][i] and ema[p['entry_ema']][i]<ema[p['entry_ema']][i-24]
        f[i,1]=bear[i] if p.get('core',False) else close<ema[p['exit_ema']][i]*(1+p['exit_band'])
        confirm=True
        if p['entry'].startswith('break'):
            n=int(p['entry'][5:]);confirm=close<np.min(hours[i-n:i,3])
        elif p['entry']=='pullback':
            confirm=hours[i,2]>=ema[24][i] and close<ema[24][i] and close<hours[i,1]
        f[i,8]=bear[i] and entrytrend and confirm
        if p['veto'] and bear[i]:f[i,0]=0
        if p.get('boost_risk',0)>0:
            path=np.sum(np.abs(np.diff(hours[i-24:i+1,4])))
            change=close-hours[i-24,4]
            er=abs(change)/path if path else 0.
            f[i,11]=close/hours[i-24,4]-1<p['boost_return'] and er>=p['boost_er']
    return f


@njit(cache=True,nogil=True)
def bear_replay(rows,mask,feat,begin,finish,cfg,deposits,initial):
    # max leverage, long risk/cap, short risk scale/cap, ER, regime, trail,
    # DD limit, daily pause, fee, execution delay in bars, dynamic leverage.
    maxlev,risk,cap,shortscale,shortcap,threshold,regime,trail,limit,dailylimit,fee,delay,dynamic=cfg[:13]
    longgate=cfg[13] if len(cfg)>13 else 1.
    short_distance=cfg[14];short_cooldown=cfg[15];last_exit_dir=1
    short_timeout=cfg[16] if len(cfg)>16 else 336.;pendingboost=1.
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
                direction=pendingdir;distance=pendingdistance*(short_distance if direction==-1 and short_distance>0 else 1.)
                lev=maxlev
                if dynamic:lev=min(maxlev,max(1.,math.floor(.60/distance)))
                budget=risk if direction==1 else risk*shortscale*pendingboost
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
                trades[nt]=np.array([entryt,end,direction,stake,-stake,0.,mae*100,mfe*100,lev,stake*lev/entryeq,-stake/entryeq,7.]);nt+=1
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
                if feat[h,0]>0:
                    if longgate==0 or not (last_loss and end-loss_at<12*HOUR) or (feat[h,3]>=.3 and feat[h,4]>0):side=1
                elif shortscale>0 and feat[h,1]>0 and feat[h,3]>=threshold:
                    regime_ok=(regime==0 or (regime==1 and feat[h,5]>0) or (regime==2 and feat[h,8]>0) or (regime==3 and feat[h,8]>0 and feat[h,9]>0))
                    if regime_ok and (threshold==0 or feat[h,4]<0):side=-1
                if side:
                    pending=1;due=i+1+int(delay);pendingdir=side;pendingdistance=feat[h,10] if side==-1 and short_distance==0 else feat[h,2]
                    pendingboost=cfg[17] if side==-1 and len(cfg)>17 and cfg[17]>1 and feat[h,11]>0 else 1.
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


def plan_grid():
    common=dict(risk=.035,cap=5.,leverage=5.,short_risk=.035,short_cap=3.,short_er=.3,
                regime='ema100',entry_ema=96,exit_ema=48,exit_band=.005,entry='trend',veto=0,
                short_distance=1.5,trail=4.,short_cooldown=4.,dynamic=1,control=None)
    out=[dict(common,code='long35',control='long',short_risk=0.,short_distance=1.,dynamic=0),
         dict(common,code='small_short35',control='small',short_risk=.007,short_cap=1.25,
              short_er=.35,short_distance=1.,trail=3.,dynamic=0)]
    for regime in ['ema200','ema100','ema60']:
        for exit_ema in [48,96]:
            for veto in [0,1]:
                out.append(dict(common,code=f'{regime}_e{exit_ema}_v{veto}',regime=regime,exit_ema=exit_ema,veto=veto))
        for window in [24,48]:
            out.append(dict(common,code=f'{regime}_break{window}',regime=regime,entry=f'break{window}',exit_ema=96,veto=1,trail=0.,short_er=.35))
        out.append(dict(common,code=f'{regime}_pullback',regime=regime,entry='pullback',veto=1,short_er=.15))
    for veto in [0,1]:out.append(dict(common,code=f'week8_v{veto}',regime='week8',veto=veto))
    return out



def extended_grid():
    out=plan_grid()[:2]
    common=dict(risk=.035,cap=5.,leverage=5.,short_risk=.02,short_cap=3.,short_er=.30,
                regime='ema100',entry_ema=96,exit_ema=96,exit_band=.005,entry='trend',veto=0,
                short_distance=1.5,trail=4.,short_cooldown=4.,dynamic=1,control=None)
    # The best growth candidate from the hourly stage is retained as a control.
    out.append(dict(common,code='hour100_r2'))
    for regime in ['ema200','ema100','ema60']:
        for risk in [.035,.07,.10]:
            out.append(dict(common,code=f'core_{regime}_r{int(risk*10000)}',regime=regime,
                            short_risk=risk,short_cap=5.,short_er=.15,short_distance=0.,
                            trail=0.,core=True,short_timeout=2160.))
        out.append(dict(common,code=f'core_{regime}_r700_veto',regime=regime,
                        short_risk=.07,short_cap=5.,short_er=.15,short_distance=0.,
                        trail=0.,core=True,veto=1,short_timeout=2160.))
    for risk in [.05,.07,.10]:
        for strict,er,drop in [('soft',.45,-.03),('strict',.60,-.05)]:
            out.append(dict(common,code=f'boost_{strict}_{int(risk*10000)}',boost_risk=risk,
                            boost_er=er,boost_return=drop,short_cap=5.))
    return out

def pf(values):
    loss=-values[values<0].sum()
    return float(values[values>0].sum()/loss) if loss>0 else None


def half_year_rows(code,scenario,trades,daily):
    df=pd.DataFrame(daily,columns=['time','assets','nav','dd','bound','inputs','cash','reserve'])
    dates=pd.to_datetime(df.time.astype('int64'),unit='ms',utc=True).dt.tz_convert('Asia/Shanghai')-pd.Timedelta(milliseconds=1)
    df['period']=dates.dt.year.astype(str)+'H'+np.where(dates.dt.month<=6,'1','2')
    out=[];prior=1.
    for period,part in df.groupby('period',sort=True):
        end=float(part.iloc[-1].time);left=float(part.iloc[0].time-DAY)
        tr=trades[(trades[:,1]>left)&(trades[:,1]<=end)]
        current=float(part.iloc[-1].nav);peak=prior;dd=0.
        for nav in part.nav:peak=max(peak,nav);dd=max(dd,1-nav/peak)
        short=tr[tr[:,2]<0]
        out.append(dict(code=code,scenario=scenario,period=period,return_pct=(current/prior-1)*100,
                        daily_dd_pct=dd*100,trades=len(tr),win_rate_pct=float(np.mean(tr[:,4]>0)*100) if len(tr) else None,
                        normalized_profit_factor=pf(tr[:,10]),short_trades=len(short),short_profit=float(short[:,4].sum()),
                        short_normalized_pf=pf(short[:,10]),mae_pct=float(tr[:,6].mean()) if len(tr) else None,
                        mfe_pct=float(tr[:,7].mean()) if len(tr) else None))
        prior=current
    return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--data',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);ap.add_argument('--extended',action='store_true');ap.add_argument('--neighbors-only',action='store_true')
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    rows=np.load(args.data/'BTCUSDT-4s.npy',mmap_mode='r');mask=np.load(args.data/'available.npy',mmap_mode='r')
    hours=np.load(args.data/'hours.npy');valid=np.load(args.data/'hour_valid.npy')
    days=np.load(args.data/'days.npy');dv=np.load(args.data/'day_valid.npy');states=daily_states(days,dv,hours)
    origin=int(rows[0,0]);end=int(rows[-1,0])+4000
    records=json.loads((args.out/'results.json').read_text()) if args.neighbors_only else [];all_plans=extended_grid() if args.extended else plan_grid();cache={};halves=pd.read_csv(args.out/'half_year.csv').to_dict('records') if args.neighbors_only else [];selected=[]
    def run(p,a,b,scenario='normal',phase='train',save=False):
        start=stamp(a);finish=min(stamp(b),end);begin=max(0,(start-origin)//4000);stop=min(len(rows),(finish-origin)//4000)
        key=tuple(p[k] for k in ['control','regime','entry_ema','exit_ema','exit_band','entry','veto'])
        key+=tuple(p.get(k) for k in ['core','boost_risk','boost_er','boost_return'])
        if key not in cache:cache[key]=bear_features(hours,valid,states,p)
        months=pd.date_range(pd.Timestamp(a,tz='Asia/Shanghai').normalize().replace(day=1)+pd.offsets.MonthBegin(),pd.Timestamp(b,tz='Asia/Shanghai'),freq='MS')
        dep=np.array([int(d.value//1000000) for d in months if int(d.value//1000000)<finish],dtype=np.int64)
        initial=1000+1000*np.sum(dep<origin);dep=dep[dep>=origin]
        fee=.0004 if scenario=='stress' else .0002;delay=2 if scenario=='stress' else 0
        cfg=np.array([p['leverage'],p['risk'],p['cap'],p['short_risk']/p['risk'],p['short_cap'],p['short_er'],2,
                      p['trail'],.7,.05,fee,delay,p['dynamic'],0,p['short_distance'],p['short_cooldown'],p.get('short_timeout',336.),p.get('boost_risk',0)/p['short_risk'] if p['short_risk'] else 1.],float)
        began=time.monotonic();s,t,d=bear_replay(rows,mask,cache[key],int(begin),int(stop),cfg,dep,float(initial))
        assert np.isclose(s[0]-s[3],t[:,4].sum(),rtol=1e-9,atol=.01),'cashflow conservation'
        short=t[t[:,2]<0];years=(finish-start)/DAY/365.25
        m=dict(code=p['code'],phase=phase,scenario=scenario,start=iso(start),end_exclusive=iso(finish),
               total_assets=float(s[0]),net_profit=float(s[0]-s[3]),contributions=float(s[3]),unit_nav=float(s[4]),
               cagr_pct=float((s[4]**(1/years)-1)*100),close_dd_pct=float(s[5]*100),dd_bound_pct=float(s[6]*100),
               halt_time=iso(s[7]) if s[7] else None,liquidations=int(s[8]),trades=len(t),
               win_rate_pct=float(np.mean(t[:,4]>0)*100) if len(t) else None,profit_factor=pf(t[:,4]),normalized_pf=pf(t[:,10]),
               short_trades=len(short),short_profit=float(short[:,4].sum()),short_normalized_pf=pf(short[:,10]),
               short_win_rate_pct=float(np.mean(short[:,4]>0)*100) if len(short) else None,
               mae_pct=float(t[:,6].mean()) if len(t) else None,mfe_pct=float(t[:,7].mean()) if len(t) else None,
               seconds=round(time.monotonic()-began,2))
        records.append(m)
        if save:
            name=p['code']+'_'+phase+'_'+scenario
            np.save(args.out/(name+'_trades.npy'),t);np.save(args.out/(name+'_daily.npy'),d)
            halves.extend(half_year_rows(p['code'],scenario,t,d))
        (args.out/'progress.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
        print(p['code'],phase,scenario,round(m['cagr_pct'],2),round(m['dd_bound_pct'],2),m['halt_time'],flush=True)
        return m
    def develop(plans):
        for p in plans:
            for phase,a,b in [('train','2017-01-01','2023-01-01'),('validation','2023-01-01','2025-01-01')]:
                for scenario in ['normal','stress']:run(p,a,b,scenario,phase)
    def rank(plans):
        eligible=[]
        for p in plans:
            if p['control']:continue
            rr=[r for r in records if r['code']==p['code'] and r['phase'] in ['train','validation']]
            if len(rr)!=4 or any(r['halt_time'] or r['dd_bound_pct']>70 or r['short_trades']<10 for r in rr):continue
            train=min(r['cagr_pct'] for r in rr if r['phase']=='train')/100
            val=min(r['cagr_pct'] for r in rr if r['phase']=='validation')/100
            growth=math.expm1((6*math.log1p(train)+2*math.log1p(val))/8)*100
            score=growth/max(5.,max(r['dd_bound_pct'] for r in rr))
            eligible.append((score,p,growth))
        return sorted(eligible,key=lambda z:z[2] if args.extended else z[0],reverse=True)
    if not args.neighbors_only:develop(all_plans)
    structures=[] if args.extended else rank(all_plans)[:2]
    (args.out/'structure_selection.json').write_text(json.dumps([dict(score=s,growth=g,plan=p) for s,p,g in structures],indent=2),encoding='utf-8')
    more=[]
    for _,p,_ in structures:
        for risk,cap in [(.02,3.),(.035,3.),(.035,5.),(.05,3.),(.05,5.),(.07,3.),(.07,5.)]:
            for cooldown in ([4.,1.] if risk==.05 and cap==3. else [4.]):
                more.append(dict(p,code=p['code']+f'_r{int(risk*10000)}c{int(cap)}w{int(cooldown)}',short_risk=risk,short_cap=cap,short_cooldown=cooldown))
    all_plans+=more
    if not args.neighbors_only:develop(more)
    ranks=rank(all_plans)
    if ranks:selected.append(ranks[0][1])
    bold=[x for x in ranks if x[1]['short_risk']>=.05 or x[1].get('boost_risk',0)>=.05]
    if bold and bold[0][1]['code'] not in [p['code'] for p in selected]:selected.append(bold[0][1])
    # Fix selection before recent/full evaluation; controls remain visible.
    final=list({p['code']:p for p in all_plans[:(3 if args.extended else 2)]+selected}.values())
    protocol=dict(date='2026-10-07',scope='offline only, high-risk research; no live fund changes',
                  upstream_sha='d2331679f4886bc96ed79aec4928938fae56437e',data=str(args.data),rows=len(rows),
                  price_start=iso(origin),end_exclusive=iso(end),initial=1000,monthly=1000,reinvest=.9,
                  long_risk=.035,long_cap=5.,leverage=5.,daily_pause=.05,permanent_dd=.70,candidates=all_plans,
                  selected=selected,extended=args.extended,neighbors_scope='post-selection sensitivity; selected plan unchanged' if args.neighbors_only else None,structure_selection=[p for _,p,_ in structures],
                  ranking=[dict(score=s,growth=g,code=p['code']) for s,p,g in ranks],
                  selection=('maximize weighted worst-scenario log growth under 70% DD constraint; train 2017-2022 and validation 2023-2024, normal and stress; no selection on recent/full' if args.extended else 'normal+stress train 2017-2022 and validation 2023-2024; weighted worst-scenario log growth / max drawdown; no selection on 2025-2026 or full results'),
                  limitations=['same historical prices repeatedly reused; no virgin holdout','200/100/60-day EMA state with 1.5% band; daily close only after completion',
                  'spot BTC 4s OHLC; next observed open execution; no funding/interest by simulator contract',
                  'per-trade 90% positive-profit reinvestment differs from monthly fund ledger',
                  'intrabar drawdown upper bound; halt checked at close; maximum loss may exceed threshold',
                  'source engine copied into separate offline module; prior study files unchanged'])
    (args.out/'protocol.json').write_text(json.dumps(protocol,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.neighbors_only:
        prefixes={p['code'].rsplit('_',1)[0] for p in selected if p.get('boost_risk',0)>0}
        final=[p for p in all_plans if p['code'].rsplit('_',1)[0] in prefixes and p.get('boost_risk',0) in (.05,.07)]
    for p in final:
        run(p,'2025-01-01','2026-10-01',phase='recent')
        for scenario in ['normal','stress']:run(p,'2017-01-01','2026-10-01',scenario,'full',True)
    (args.out/'results.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    pd.DataFrame(records).to_csv(args.out/'summary.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(halves).to_csv(args.out/'half_year.csv',index=False,encoding='utf-8-sig')
    print('COMPLETE',json.dumps([p['code'] for p in selected]),flush=True)

if __name__=='__main__':main()
