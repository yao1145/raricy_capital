"""Publish offline research evidence and charts without changing fund parameters."""
from __future__ import annotations
import json,shutil,hashlib
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from btc_regime_research import stamp,HOUR,DAY,STATES,pf
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"docs/materials/research/bear_regime_2026-10-07"
LABEL={"long35":"原单多3.5%","hour2":"常规空头2%","boost10":"条件空头10%","vol60":"条件空头＋波动控制","brake_vol":"反弹保护＋波动控制",
       "long_vol60":"单多＋波动控制","hour2_vol60":"常规空头2%＋波动控制","vol60_consensus":"多空优化：波动＋趋势确认",
       "boost7_vol60":"条件空头7%＋波动控制","boost5_vol60":"条件空头5%＋波动控制","vol50":"波动门槛50%","vol70":"波动门槛70%"}
STUDIES={"bear_grid":"btc_bear_2026-10-07","bear_extended":"btc_bear_extended_2026-10-07","cap_control":"btc_bear_control_2026-10-07",
         "regime_grid":"btc_regime_2026-10-07","regime_refined":"btc_regime_refined_2026-10-07","regime_neighbors":"btc_regime_diagnostics_2026-10-07"}

def table(headers,rows):
    return "| "+" | ".join(headers)+" |\n| "+" | ".join("---" for _ in headers)+" |\n"+"\n".join("| "+" | ".join(str(x) for x in row)+" |" for row in rows)
def n(x):return f"{x:,.2f}"
def main():
    OUT.mkdir(parents=True,exist_ok=True);records=[];locations={};half=[];states=[]
    counts={}
    for name,folder in STUDIES.items():
        src=ROOT/".build"/folder;dst=OUT/name;dst.mkdir(exist_ok=True)
        for p in src.iterdir():
            if p.suffix in [".json",".csv",".npy"] and p.name not in ["progress.json"]:shutil.copy2(p,dst/p.name)
            elif p.name=="engine_snapshot.py":shutil.copy2(p,dst/p.name)
        data=json.loads((src/"results.json").read_text());counts[name]=len(data)
        for m in data:
            m["evidence_group"]=name;records.append(m)
            if m["phase"]=="full" and (src/(m["code"]+"_full_"+m["scenario"]+"_daily.npy")).exists():
                locations[m["code"],m["scenario"]]=src
        if (src/"half_year.csv").exists():
            h=pd.read_csv(src/"half_year.csv");h["group"]=name;half.append(h)
        if (src/"regimes.csv").exists():
            g=pd.read_csv(src/"regimes.csv");g["group"]=name;states.append(g)
    data=pd.DataFrame(records);data.to_csv(OUT/"all_runs.csv",index=False,encoding="utf-8-sig")
    # Prefer the latest controlled replay when baseline codes are repeated.
    recent=data[data.phase=="recent"].drop_duplicates(["code","scenario"],keep="last")
    full=data[data.phase=="full"].drop_duplicates(["code","scenario"],keep="last")
    halves=pd.concat(half).drop_duplicates(["code","scenario","period"],keep="last")
    regimes=pd.concat(states).drop_duplicates(["code","scenario","state"],keep="last")
    # Raw money PF is derived from the archived exit-grouped trades.
    rawpf=[]
    for row in halves.itertuples():
        location=locations.get((row.code,row.scenario))
        if location is None:rawpf.append(np.nan);continue
        trades=np.load(location/(row.code+"_full_"+row.scenario+"_trades.npy"))
        year=int(row.period[:4]);halfno=int(row.period[-1]);month=1 if halfno==1 else 7
        begin=stamp(f"{year}-{month:02d}-01")
        finish=stamp(f"{year+1}-01-01") if halfno==2 else stamp(f"{year}-07-01")
        values=trades[(trades[:,1]>begin)&(trades[:,1]<=finish),4]
        rawpf.append(pf(values))
    halves["profit_factor"]=rawpf
    halves.to_csv(OUT/"half_year_comparison.csv",index=False,encoding="utf-8-sig")
    regimes.to_csv(OUT/"regime_comparison.csv",index=False,encoding="utf-8-sig")
    codes=["long35","long_vol60","hour2_vol60","boost10","vol60","vol60_consensus"]
    def metric(code,scenario="normal",frame=full):
        return frame[(frame.code==code)&(frame.scenario==scenario)].iloc[0]
    def trace(code,scenario="normal"):
        return np.load(locations[code,scenario]/(code+"_full_"+scenario+"_daily.npy"))
    # Daily event/rolling diagnostics are separate from the 4-second DD bound.
    events=[("2018熊市","2018-01-01","2019-01-01"),("2020三月急跌","2020-03-01","2020-04-01"),
            ("2020反弹阶段","2020-04-01","2020-07-01"),("2021五月大跌","2021-05-01","2021-06-01"),
            ("2022五月大跌","2022-05-01","2022-06-01"),("2022十一月大跌","2022-11-01","2022-12-01"),
            ("2022熊市全年","2022-01-01","2023-01-01"),("2024二三月大涨","2024-02-01","2024-04-01"),
            ("2025下半年","2025-07-01","2026-01-01")]
    eventrows=[];riskrows=[];annual=[]
    raw=Path(json.loads((OUT/"regime_refined/protocol.json").read_text())["data"])
    hours=np.load(raw/"hours.npy")
    def btc(a,b):
        ix=np.searchsorted(hours[:,0]+HOUR,[stamp(a),stamp(b)],side="right")-1
        return (hours[ix[1],4]/hours[ix[0],4]-1)*100
    for code in codes:
        for scenario in ["normal","stress"]:
            d=trace(code,scenario);times=d[:,0];nav=d[:,2]
            risk=dict(code=code,scenario=scenario)
            for days in [1,7,30,90,365]:
                risk["worst_"+str(days)+"d_pct"]=float(np.min(nav[days:]/nav[:-days]-1)*100)
            peak=np.maximum.accumulate(nav);length=best=0
            for underwater in nav<peak*(1-1e-12):
                length=length+1 if underwater else 0;best=max(best,length)
            risk["longest_underwater_daily_observations"]=best;riskrows.append(risk)
            for label,a,b in events:
                ix=np.searchsorted(times,[stamp(a),stamp(b)],side="right")-1
                na=nav[ix[0]];segment=nav[ix[0]:ix[1]+1]
                eventrows.append(dict(code=code,scenario=scenario,event=label,start=a,end_exclusive=b,
                                      return_pct=float((nav[ix[1]]/na-1)*100),daily_dd_pct=float(np.max(1-segment/np.maximum.accumulate(segment))*100),
                                      btc_price_return_pct=btc(a,b)))
        h=halves[(halves.code==code)&(halves.scenario=="normal")]
        for year,part in h.groupby(h.period.str[:4]):
            annual.append(dict(code=code,year=year,return_pct=(np.prod(1+part.return_pct.to_numpy()/100)-1)*100))
    pd.DataFrame(eventrows).to_csv(OUT/"events.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(riskrows).to_csv(OUT/"rolling_risk.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(annual).to_csv(OUT/"annual_returns.csv",index=False,encoding="utf-8-sig")
    plt.rcParams.update({"font.sans-serif":["Microsoft YaHei","DejaVu Sans"],"axes.unicode_minus":False,"font.size":10})
    colors=["#64748b","#0891b2","#8b5cf6","#f59e0b","#16a34a","#dc2626"]
    x=np.arange(len(codes));fig,axs=plt.subplots(2,2,figsize=(15,9),layout="constrained")
    for ax,field,title in [(axs[0,0],"cagr_pct","交易桶单位净值年化（%）"),(axs[0,1],"xirr_pct","定投资金年化：XIRR（%）"),
                           (axs[1,0],"dd_bound_pct","4秒盘中回撤上界（%）"),(axs[1,1],"total_assets","期末总资产（百万小鱼干）")]:
        for j,scenario in enumerate(["normal","stress"]):
            values=[metric(c,scenario)[field]/(1e6 if field=="total_assets" else 1) for c in codes]
            bars=ax.bar(x+(j-.5)*.35,values,.35,color="#0d9488" if j==0 else "#fb923c",label="正常" if j==0 else "费用翻倍＋8秒延迟")
            ax.bar_label(bars,fmt="%.1f",fontsize=8,padding=2)
        ax.set_title(title);ax.set_xticks(x,["单多","单多控波动","2%空头控波动","10%条件空头","条件空头控波动","趋势确认多空"],rotation=12)
        ax.grid(axis="y",alpha=.15);ax.set_axisbelow(True)
    fig.legend(*axs[0,0].get_legend_handles_labels(),loc="outside lower center",ncol=2,fontsize=10);fig.suptitle("2017–2026年9月｜初始1000＋月投1000｜90%正净利润复投",fontsize=15)
    fig.savefig(OUT/"overview.png",dpi=150);plt.close(fig)
    fig,axs=plt.subplots(2,1,figsize=(14,8),sharex=True,layout="constrained")
    for code,color in zip(codes,colors):
        d=trace(code);time=pd.to_datetime(d[:,0].astype("int64"),unit="ms",utc=True).tz_convert("Asia/Shanghai")
        axs[0].plot(time,d[:,2],label=LABEL[code],color=color,linewidth=1.)
        axs[1].plot(time,-d[:,3]*100,color=color,linewidth=.9)
    axs[0].set_yscale("log");axs[0].set_ylabel("交易桶单位净值（对数轴）");axs[0].legend(ncol=2,fontsize=9)
    axs[1].set_ylabel("日终净值回撤（%）")
    for ax in axs:ax.grid(alpha=.2)
    fig.suptitle("正常情景连续账户：存款已从净值收益中剔除",fontsize=14)
    fig.savefig(OUT/"nav_drawdown.png",dpi=150);plt.close(fig)
    selected="vol60_consensus";state_labels=["急跌","大涨","震荡","熊市","牛市","转换"]
    fig,axs=plt.subplots(1,2,figsize=(14,5),layout="constrained")
    for scenario,ax in zip(["normal","stress"],axs):
        for j,code in enumerate(["long35","long_vol60",selected]):
            g=regimes[(regimes.code==code)&(regimes.scenario==scenario)].set_index("state").reindex(STATES)
            ax.bar(np.arange(6)+(j-1)*.25,g.nav_log_contribution,.25,label=LABEL[code])
        ax.set_xticks(np.arange(6),state_labels);ax.axhline(0,color="#475569",linewidth=.7);ax.set_title("正常" if scenario=="normal" else "压力")
        ax.set_ylabel("净值对数变化贡献（可加总）");ax.grid(axis="y",alpha=.2)
    axs[0].legend(fontsize=8);fig.suptitle("按每根4秒开始前已完成的小时状态归因；不是各状态独立收益率")
    fig.savefig(OUT/"regimes.png",dpi=150);plt.close(fig)
    heat=pd.DataFrame(annual).pivot(index="code",columns="year",values="return_pct").reindex(codes)
    fig,ax=plt.subplots(figsize=(14,5),layout="constrained")
    im=ax.imshow(np.clip(heat.to_numpy(),-100,300),cmap="RdYlGn",vmin=-100,vmax=300,aspect="auto")
    ax.set_yticks(np.arange(len(codes)),[LABEL[c] for c in codes]);ax.set_xticks(np.arange(len(heat.columns)),heat.columns)
    for i in range(len(codes)):
        for j in range(len(heat.columns)):ax.text(j,i,f"{heat.iloc[i,j]:.0f}%",ha="center",va="center",fontsize=9)
    ax.set_title("连续账户正常年收益｜2026仅1–9月｜颜色上限300%，数字保留实际值")
    fig.colorbar(im,ax=ax,label="单位净值收益（%）");fig.savefig(OUT/"annual.png",dpi=150);plt.close(fig)
    rows=[]
    for code in codes:
        a=metric(code);b=metric(code,"stress")
        rows.append([LABEL[code],n(a.cagr_pct)+"%",n(b.cagr_pct)+"%",n(a.xirr_pct)+"%",n(b.xirr_pct)+"%",
                     n(a.dd_bound_pct)+"/"+n(b.dd_bound_pct)+"%",n(a.total_assets),n(b.total_assets)])
    overview=table(["方案","正常净值年化","压力净值年化","正常定投年化","压力定投年化","回撤上界 正常/压力","正常总资产","压力总资产"],rows)
    recenttable=table(["方案","正常净值年化","压力净值年化","正常/压力回撤上界","正常净利润","压力净利润"],
        [[LABEL[c],n(metric(c,frame=recent).cagr_pct)+"%",n(metric(c,"stress",recent).cagr_pct)+"%",
          n(metric(c,frame=recent).dd_bound_pct)+"/"+n(metric(c,"stress",recent).dd_bound_pct)+"%",
          n(metric(c,frame=recent).net_profit),n(metric(c,"stress",recent).net_profit)] for c in codes])
    eventtable=table(["条件/事件","BTC价格涨跌","原单多","单多控波动","2%空头控波动","趋势确认多空"],
                    [[label,n(btc(a,b))+"%"]+[n(next(e["return_pct"] for e in eventrows if e["code"]==c and e["scenario"]=="normal" and e["event"]==label))+"%" for c in ["long35","long_vol60","hour2_vol60",selected]] for label,a,b in events])
    halfrows=[]
    for period in sorted(halves[(halves.code==selected)&(halves.scenario=="normal")].period.unique()):
        z=halves[(halves.code==selected)&(halves.scenario=="normal")&(halves.period==period)].iloc[0]
        halfrows.append([period,n(z.return_pct)+"%",int(z.trades),n(z.win_rate_pct)+"%",n(z.profit_factor),n(z.normalized_profit_factor),int(z.short_trades),n(z.mae_pct)+"%",n(z.mfe_pct)+"%"])
    halftable=table(["半年","净值收益","交易数","胜率","PF","归一化PF","空头笔数","MAE均值","MFE均值"],halfrows)
    g=regimes[(regimes.code==selected)].set_index(["scenario","state"])
    statetable=table(["实时可知状态","正常/压力净值对数贡献","按该状态入场交易数","胜率","归一化PF"],
        [[label,n(g.loc["normal",state].nav_log_contribution)+"/"+n(g.loc["stress",state].nav_log_contribution),
          int(g.loc["normal",state].entry_trades),n(g.loc["normal",state].win_pct)+"%",n(g.loc["normal",state].normalized_pf)]
         for state,label in zip(STATES,state_labels)])
    dev=data[data.phase.isin(["train","validation"])].drop_duplicates(["code","phase","scenario"],keep="last")
    def devrow(c):
        a=dev[(dev.code==c)&(dev.phase=="train")&(dev.scenario=="stress")].iloc[0]
        z=dev[(dev.code==c)&(dev.phase=="validation")&(dev.scenario=="stress")].iloc[0]
        return [c,n(a.xirr_pct)+"%",n(z.xirr_pct)+"%",n(max(a.dd_bound_pct,z.dd_bound_pct))+"%"]
    ablation=table(["候选","2017–2022压力定投年化","2023–2024压力定投年化","这两档较大回撤"],
                   [devrow(c) for c in ["boost10","vol60","brake_soft","brake_hard","range20","bull5","bear_long60","all","all_bull5","long_vol60","hour2_vol60",selected]])
    a=metric(selected);b=metric(selected,"stress");long=metric("long35")
    delta=(a.total_assets/long.total_assets-1)*100
    report=f"""# BTC 多空与市场状态优化研究

研究日期：2026-10-07。**结论：高波动时减少风险预算，比继续提高杠杆更有效。趋势一致性确认改善了空头筛选，但大跌与最近区间仍存在短板。**

## 1. 选型结论

- **追求完整历史增长的研究候选：波动控制＋空头趋势确认。**正常净值年化{a.cagr_pct:.2f}%，压力{b.cagr_pct:.2f}%；定投资金年化分别{a.xirr_pct:.2f}%、{b.xirr_pct:.2f}%。正常总资产较原单多增加{delta:.2f}%。
- **侧重较小回撤及最近表现的研究对照：单多＋波动控制。**正常/压力回撤上界45.51%/48.48%；最近正常年化62.60%，压力35.35%。这一方案仍属于高风险研究，不能替代低风险基金定位。
- **保留常规2%空头＋波动控制作为较简单的多空对照。**最近正常净值年化68.18%，但压力期末利润低于单多控波动，不能仅据这项年化选胜者。
- 当前基金服务、账号、账簿及交易参数保持原状态。本研究没有执行站点交易。

## 2. 统一结果

2017-01-01至2026-10-01（不含终点）。初始1000、每月1000，外部出资共117,000。90%每笔已实现正净利润复投，10%进无息留存桶。正常费用0.02%；压力费用0.04%并额外延迟8秒。

{overview}

![年化、回撤与期末资产](overview.png)

“净值年化”是交易桶单位净值CAGR；“定投年化”是全部出资及期末两桶资产的XIRR口径。两者回答不同问题。**期末资产不能除以初始1000直接当收益率。**保留现金没有对外支付，XIRR以期末一次回收处理。表中巨额余额来自站点理想成交模型，不代表真实市场可容纳这种规模。

![连续净值与日终回撤](nav_drawdown.png)

### 最近独立启动：2025-01-01至2026-10-01

重新以1000启动、月投1000，累计出资21,000；区别于上面的连续账户。历史此前已经使用过，这不是从未观察过的样本外数据。

{recenttable}

**趋势确认多空最近正常利润11,046.89，少于原单多14,395.94和单多控波动15,788.86；最近压力也落后。**完整历史改善不能掩盖这项劣势。

## 3. 文献怎样影响了这次测试

本次搜索覆盖多空趋势、动量反转、波动率控制和BTC日内可预测性；阅读了六项原始研究的作者稿、摘要或出版社页面。关键来源与适用边界见[SOURCE_NOTES.md](SOURCE_NOTES.md)。

| 原始研究 | 得到的参考方向 | 本次落地 | 适用边界 |
| --- | --- | --- | --- |
| [Time Series Momentum](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf) | 用资产自身趋势决定多空，并按风险缩放 | 日/小时趋势结合、空头24/96/192小时一致性 | 论文主要是多资产、月度信号；不能当作BTC4秒盈利证据 |
| [Momentum Crashes](https://www.nber.org/papers/w20439) | 恐慌后的强反弹可能打击动量空头 | 急跌后超卖取消加码、6小时反弹退出 | 主要讨论横截面动量；这里只借用风险机制假设 |
| [Volatility Managed Portfolios](https://www.nber.org/papers/w22208) | 高波动时减少承担的风险 | 最近7天小时实现波动率缩放入场风险 | 这里不复刻论文组合，也不保证组合波动率恒定 |
| [Cryptocurrency momentum has (not) its moments](https://link.springer.com/article/10.1007/s11408-025-00474-9) | 加密动量有尾部损失，波动管理也有争议 | 正常/压力、邻近参数、近期对照一起评价 | 研究的是多币横截面组合，不能直接外推到BTC单标的 |
| [Intraday Return Predictability…](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253) | 动量与反转表现会随跳跃等条件改变 | 分急跌、大涨、震荡、牛熊状态评价 | 本次没有重现论文的具体日内预测变量 |
| [Bitcoin intraday time-series momentum](https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf) | BTC日内动量可有条件性 | 保留小时信号，4秒执行；不盲目加快入场 | 论文用成交量识别交易时段，本次未实施其时段模型 |

## 4. 最终研究候选的完整参数

### 多头

1. 最近完成小时收盘高于EMA96，EMA96高于24小时前。
2. 基础风险预算为交易桶权益3.5%；初始止损2ATR14。
3. 反向趋势、止损、风控或持仓14天退出；不新增固定小幅止盈。

### 空头

1. 最近完成日线收盘低于日EMA100的98.5%，该EMA低于10日前。
2. 小时收盘低于EMA96，小时EMA96低于24小时前。
3. ER12≥0.30，12小时净涨跌为负。
4. **额外一致性确认：小时收盘同时低于EMA24与EMA192，EMA192低于24小时前。**这些是入场条件，不会因为不再满足入场条件就立即平仓。
5. 普通空头风险预算2%；仅当24小时跌幅超过3%且ER24≥0.45，提高到10%。
6. 初始止损3小时ATR；空头追踪距离4ATR。ATR距离固定在入场信号基准，追踪止损只根据已经观察的收盘价向有利方向移动。
7. 小时收盘高于EMA96的100.5%、止损、风控或14天超时退出。

### 波动率与仓位

小时对数收益使用最近168小时、至少48小时的样本标准差，乘以平方根(24×365.25)年化，得到σ：

`risk_multiplier = max(0.35, min(1, 0.60 / σ))`

多头和空头预算同时乘这个因子。60%是风险缩放的参考门槛，**不是组合实际波动率目标**。只在开仓时确定，持仓中不动态加减仓。例如σ=120%，多头风险预算变为1.75%，条件空头变为5%。

名义仓位/权益：

`q = min(5, adjusted_risk / [stop_distance + fee × (1 − direction × stop_distance)], L)`

`L = min(5, max(1, floor(0.60 / stop_distance)))`；多头方向+1、空头−1。不是投入10%保证金，也不是账户每天最多亏10%。成交跳空、费用和风控退出可使实际亏损偏离目标。

同时最多一仓，平仓后等下一整点，再冷却4小时；日内净值损失5%暂停到下一北京时间自然日；总净值回撤70%永久停止。盘中损失或跳空可以超过70%，不能把它当硬性保证。

## 5. 熊市、大跌、大涨与震荡

### 事件窗口：连续账户正常净值收益

月份边界固定，统计窗口内全部涨跌，没有只截取最有利的几个小时。BTC列为同期无杠杆价格涨跌；策略列扣除模拟费用，并剔除存款。

{eventtable}

压力下的相同窗口及窗口内日终回撤见[events.csv](events.csv)。**熊市全年与单次急跌是两个问题：提前形成的下降趋势可获利，第一波突然暴跌及其反弹仍可能让持仓受损。**

![连续账户年度收益](annual.png)

### 因果状态归因

每根4秒开始时，只用已经完成的小时/日线分类，优先顺序：

1. 急跌：24小时≤−8%或6小时≤−4%。
2. 大涨：24小时≥8%或6小时≥4%。
3. 震荡：ER24≤0.20且距EMA96不超过1ATR。
4. 熊市：上述日EMA100熊市条件。
5. 牛市：日收盘高于EMA100的101.5%且EMA100上升。
6. 转换：其余情况。

{statetable}

“对数贡献”是这些状态内每根4秒净值变化对数的总和；六项可加成整体对数净值收益，**不能当各状态独立投资收益率，也不能解读成未来预测**。暴跌发生后才满足“急跌”分类，第一波损失可能被记在此前的牛市或转换状态。表中交易指标按入场时状态分组，持仓可跨状态，所以入场PF与状态期间净值贡献可以相反。

![按状态归因](regimes.png)

多空优化的急跌贡献为负；压力震荡贡献接近零。熊市、牛市与转换阶段是主要增长来源。**这次没有找到每种状态都能稳定高收益的方案。**

## 6. 哪些优化值得保留，哪些不值得

{ablation}

- **保留：波动控制。**单多对照也得到改善，说明提升不能全归功于空头。高波动时减少预算，降低早期回撤，后续资金能参与上涨。
- **保留为多空研究候选：短、中、较长小时趋势一致性。**减少部分空头入场，完整历史正常/压力增长与回撤优于未确认版本。
- **不把更大杠杆当主方向。**同样名义仓位下，提高杠杆主要改变保证金和强平距离；此前等名义10倍对照发生过强平，5倍上限更适合本轮。
- **反弹保护没有普遍胜出。**软保护略改善部分开发结果，但完整资产仍落后于单纯波动控制；硬保护会错过部分延续下跌。
- **震荡过滤过强会误伤趋势起点。**ER24过滤从0.15加到0.25，验证压力定投年化继续下降。把所有低ER小时都当无价值交易并不成立。
- **牛市风险加到5%没有稳定优势。**与其他过滤组合后，也会放大错误入场损失。
- **熊市全部禁止多头或把多头预算压到60%会牺牲反弹收益。**趋势确认多空保留原多头，避免仅凭日线标签错过恢复行情。
- **长期核心空头也不是自动改进。**日EMA200核心空头验证区间只有5笔且全部亏损；EMA100核心空头验证约10–11笔，PF很弱。小样本不宜推广。

参数邻域50%/60%/70%波动门槛、5%/7%/10%条件空头预算的完整和近期结果在[regime_neighbors/summary.csv](regime_neighbors/summary.csv)、[regime_refined/summary.csv](regime_refined/summary.csv)。相邻参数仍有历史盈利，但60%门槛附近表现有差别，不能称已证明参数稳定。7%加码在最近区间仍较弱，单纯降加码比例没有解决环境变化。

## 7. 胜率、PF与资金路径

多空优化完整正常胜率{a.win_rate_pct:.2f}%，PF={a.profit_factor:.3f}，归一化PF={a.normalized_pf:.3f}；平均MAE={a.mae_pct:.2f}%、MFE={a.mfe_pct:.2f}%，交易{int(a.trades)}笔，其中空头{int(a.short_trades)}笔。

它通过少数较大趋势盈利覆盖较多止损，所以胜率仍低。**不能把高年化归因于高胜率。**MAE/MFE是BTC价格偏移，不是杠杆后的账户回撤。

完整正常空头实现净损益{a.short_profit:,.2f}，但空头归一化PF={a.short_normalized_pf:.3f}。早期小资金获利、后期较大资金亏损会让两者相反；空头也改变后续多头本金与入场机会。因此整体余额增加不等于空头直接贡献了同等金额利润。最近区间空头归一化PF低于1，更不宜自动维持最大加码。

### 每半年统计：最终多空候选，正常情景

{halftable}

2017H2从有行情时开始；2026H2仅7–9月。全部方案、压力情景和半年日终回撤见[half_year_comparison.csv](half_year_comparison.csv)；日频最差1/7/30/90/365日与最长水下期见[rolling_risk.csv](rolling_risk.csv)。

## 8. 下一步优化方向

1. **先做冻结参数的模拟观察。**重点观察空头在下跌延续、急跌反弹中的分组盈亏，不因为完整历史冠军就直接替换当前基金。
2. **独立管理空头预算。**下一轮可测试空头连续亏损后缩减预算，恢复条件使用过去数据；不连带抑制多头反弹机会。目前尚未测试这条规则。
3. **把“大跌”分成第一波冲击与后续趋势。**下一轮测试异常波动期间降低新仓上限、观察反弹后再进入空头，避免简单追跌。
4. **多方案分桶只在互补性得到证明后采用。**这些方案共享BTC多头，相关性可能很高；分桶本身不等于跨资产分散。当前没有执行组合资金再平衡回测。

## 9. 证据、代码与复现

本轮加强空头、状态覆盖及邻域诊断共{sum(counts.values())}次执行，重复对照也计入；逐次原始值见[all_runs.csv](all_runs.csv)。前一份127次研究保留在[上一版报告](../long_short_2026-10-07/REPORT.md)，没有覆盖。

- 原始4秒数据71,949,600根，现价数据2017-08-18起，至2026-10-01；没有使用10月1–7日行情。上市前月投留现金。
- 4秒用于成交、止损、强平与回撤检查；信号来自完成的小时与日线。通过下一根有数据的4秒开盘执行，压力再延迟2根。
- 开发为2017–2022，验证为2023–2024；每阶段都重启资金账户。第一阶段按风险调整增长选结构，后续阶段按约束下较差情景增长、再按较差情景XIRR选择。最终规则选定后跑最近与完整区间，邻域诊断不重新选择。
- 全历史及最近历史曾多次参与先前研究，存在反复调参偏差。没有全新样本外验证、置信区间或未来收益保证。
- 使用站点源码提交`d2331679f4886bc96ed79aec4928938fae56437e`的多空、1–100整数杠杆、按平仓名义价值收一次费与保证金强平规则。没有资金费、借贷利息、滑点；不代表真实合约交易。
- 模拟利润逐笔90%复投，**不同于v0.3基金月度分红、5%申购费和紧急赎回规则**。本报告不称为基金实际净值表现。
- 保留了4秒盘中回撤上界、收盘停机与定投份额核算；历史缺价会等待下一有数据开盘，不等于运行端新鲜行情保护。

本轮针对性验证：20项测试通过，覆盖未来价格/日线不可见、下一根执行、手续费与强平、止损预算、波动缩放信号时点、震荡入场与出场分离、资金守恒和状态净值贡献守恒。未进行真实站点交易或部署验收。

项目入口：

```powershell
python tools/btc_bear_research.py --data <4秒数据目录> --out .build/btc_bear_2026-10-07
python tools/btc_bear_research.py --data <4秒数据目录> --out .build/btc_bear_extended_2026-10-07 --extended
python tools/btc_bear_research.py --data <4秒数据目录> --out .build/btc_bear_extended_2026-10-07 --extended --neighbors-only
python tools/btc_bear_control_research.py --data <4秒数据目录> --out .build/btc_bear_control_2026-10-07
python tools/btc_regime_research.py --data <4秒数据目录> --out .build/btc_regime_2026-10-07
python tools/btc_regime_research.py --data <4秒数据目录> --out .build/btc_regime_refined_2026-10-07 --refined
python tools/btc_regime_diagnostics.py --data <4秒数据目录> --out .build/btc_regime_diagnostics_2026-10-07 --previous .build/btc_regime_refined_2026-10-07
python tools/btc_regime_report.py
```

研究环境依赖numpy、pandas、numba、matplotlib；可安装项目`.[research]`。小文件、轨迹与快照保存在本目录，来源哈希见[manifest.json](manifest.json)。原始3.45GB行情不重复复制，数据SHA-256为`595b30ea805f823d14e2cd22698d181c16cf0a9af62f46bedca77c5dc66ff111`，该值沿用已核对的前版数据清单。
"""
    (OUT/"REPORT.md").write_text(report,encoding="utf-8")
    sources="""# 原始研究来源与应用边界

查询日期：2026-10-07。通过Exa按主题搜索并读取以下六项原始研究的摘要、出版社页面或作者接受稿。只把研究用于形成待验证的规则，不把论文历史收益搬到本项目。

1. **Moskowitz, Ooi, Pedersen (2012), Time Series Momentum.** Journal of Financial Economics 104(2), 228–250。DOI 10.1016/j.jfineco.2011.11.003。[作者PDF](https://w4.stern.nyu.edu/facdir/lpederse/papers/TimeSeriesMomentum.pdf)。研究58种期货与远期，发现过去1–12个月自身收益有延续性，较长期出现部分反转。参考方向是单标的自身趋势、风险缩放与多空切换。其多资产分散和月度周期不等同于本项目的BTC小时信号。

2. **Daniel, Moskowitz (2016), Momentum crashes.** Journal of Financial Economics 122(2), 221–247；2014年NBER工作论文20439。[原始摘要与发表记录](https://www.nber.org/papers/w20439)。恐慌、高波动后强反弹可能打击动量策略。主要讨论横截面赢家/输家策略；本次只将空头反弹风险作为可测试假设，没有声称复刻其动态模型。

3. **Moreira, Muir (2017), Volatility-Managed Portfolios.** Journal of Finance 72(4), 1611–1644；NBER工作论文22208。[原始摘要与发表记录](https://www.nber.org/papers/w22208)。多类股票因子与货币套利组合在高波动期减少风险后，历史风险调整收益有改善。本次采用过去168小时BTC波动率在入场时缩放风险，使用更简单规则，没有沿用论文系数或预测未来波动。

4. **Grobys, Kolari, Sandretto, Shahzad, Äijö (2025), Cryptocurrency momentum has (not) its moments.** Financial Markets and Portfolio Management。DOI 10.1007/s11408-025-00474-9。[出版社全文](https://link.springer.com/article/10.1007/s11408-025-00474-9)。研究2016–2023年大市值多币动量，发现尾部崩溃与个别币种极端变动会显著影响结论，波动管理可有帮助，但相关文献存在真实时间表现与偏差争议。这支持本次必须同时看压力、近期、参数邻域，不支持无条件增加空头杠杆。

5. **Wen, Bouri, Xu, Zhao (2022), Intraday Return Predictability in the Cryptocurrency Markets: Momentum, Reversal, or Both.** [作者SSRN摘要](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4080253)。BTC样本2013-03-03至2020-05-31，日内动量与反转会随跳跃、FOMC、流动性及疫情环境变化。本次借用“条件改变可能改变信号收益”的思路，没有把研究摘要当作本项目参数的证明。

6. **Shen, Urquhart, Wang (2022), Bitcoin intraday time-series momentum.** Financial Review 57(2), 319–344。DOI 10.1111/fire.12290。[大学机构库接受稿](https://centaur.reading.ac.uk/100181/3/21Sep2021Bitcoin%20Intraday%20Time-Series%20Momentum.R2.pdf)。作者以成交量识别BTC交易时段，研究前后半小时可预测性及高波动/高量条件。本次未实施其成交量时段模型；“4秒执行”不是论文中的日内信号。

以上摘要均为本项目的简短转述；未复制论文全文。研究提出的是历史统计证据，项目结果来自自己的冻结数据与实际执行回放。
"""
    (OUT/"SOURCE_NOTES.md").write_text(sources,encoding="utf-8")
    selected_plan=next(p for p in json.loads((OUT/"regime_refined/protocol.json").read_text())["candidates"] if p["code"]==selected)
    (OUT/"research_candidate.json").write_text(json.dumps(dict(scope="offline candidate, not live fund configuration",plan=selected_plan,
                                                              limitation="recent results underperform single-long volatility control; crash contribution negative"),indent=2),encoding="utf-8")
    sourcefiles=["tools/btc_bear_research.py","tools/btc_bear_control_research.py","tools/btc_long_short_research.py","tools/btc_regime_research.py",
                 "tools/btc_regime_diagnostics.py","tools/btc_regime_report.py","tests/test_bear_research.py","tests/test_regime_research.py"]
    manifest=dict(scope="offline research only",runs=counts,total_runs=sum(counts.values()),sources={},artifacts={},
                  price_sha256_from_previous_manifest="595b30ea805f823d14e2cd22698d181c16cf0a9af62f46bedca77c5dc66ff111")
    for rel in sourcefiles:manifest["sources"][rel]=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    for p in OUT.rglob("*"):
        if p.is_file() and p.name!="manifest.json":manifest["artifacts"][p.relative_to(OUT).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
    (OUT/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding="utf-8")
    index=ROOT/"docs/materials/research/README.md";text=index.read_text(encoding="utf-8-sig")
    link="- [2026-10-07 加强空头与市场状态优化](bear_regime_2026-10-07/REPORT.md)：原始论文参考、条件空头、波动率与趋势一致性测试；正常/压力、近期、牛熊事件、半年统计和证据清单。\n"
    if link not in text:text=text.replace("## 新增研究\n","## 新增研究\n\n"+link);index.write_text(text,encoding="utf-8")
    index=ROOT/"docs/README.md";text=index.read_text(encoding="utf-8-sig")
    link="| 多空与市场状态进一步优化 | [研究报告](materials/research/bear_regime_2026-10-07/REPORT.md) |\n"
    if link not in text:text=text.replace("| 原始研究证据与口径差距 |",link+"| 原始研究证据与口径差距 |");index.write_text(text,encoding="utf-8")
    print(json.dumps(dict(out=str(OUT),runs=counts,total=sum(counts.values()),candidate=selected),ensure_ascii=False))
if __name__=="__main__":main()

