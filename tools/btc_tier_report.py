"""Archive two-tier research results and produce an evidence-based report."""
from __future__ import annotations
import json,hashlib,shutil
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from btc_regime_report import table,n
from btc_tier_research import plans,stamp,HOUR,DAY,pf,STATES
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/"docs/materials/research/dual_tier_2026-10-07"
STUDIES={"risk_grid":"btc_tier_2026-10-07","turning_grid":"btc_tier_turning_2026-10-07","moderate_diagnostics":"btc_tier_diagnostics_2026-10-07"}
LABEL={"M0":"温和原版","M_L2":"温和：控波动80%／2倍","M_vol80":"温和：控波动80%／3倍","M_r100":"温和防守：风险1%","M_vol40":"温和：控波动40%",
       "M_short25_ema100":"温和：0.25%空头","M_short50_ema100":"温和：0.5%空头","M_dd_r175":"温和：1.75%＋回撤缩仓","M_dd_r200_floor25":"温和：2%＋回撤缩仓",
       "M_turn50":"温和：趋势分歧减半","M_turn75":"温和：趋势分歧降至75%","G0":"增长：上一轮多空优选","G_crash_half":"增长：急跌新仓减半",
       "G_loss2_50":"增长：两次空头亏损减半","G_turn75":"增长：趋势分歧降至75%","G_turn50":"增长：趋势分歧减半"}

def main():
    OUT.mkdir(parents=True,exist_ok=True);records=[];locations={};halves=[];states=[];counts={}
    for name,folder in STUDIES.items():
        src=ROOT/".build"/folder;dst=OUT/name;dst.mkdir(exist_ok=True)
        for p in src.iterdir():
            if (p.suffix in [".json",".csv",".npy"] and p.name!="progress.json") or p.name=="engine_snapshot.py":shutil.copy2(p,dst/p.name)
        data=json.loads((src/"results.json").read_text());counts[name]=len(data)
        for row in data:
            row["group"]=name;records.append(row)
            if row["phase"] in ["full","recent"] and (src/(row["code"]+"_"+row["phase"]+"_"+row["scenario"]+"_daily.npy")).exists():
                locations[row["code"],row["phase"],row["scenario"]]=src
        h=pd.read_csv(src/"half_year.csv");halves.append(h)
        g=pd.read_csv(src/"regimes.csv");states.append(g)
    data=pd.DataFrame(records);data.to_csv(OUT/"all_runs.csv",index=False,encoding="utf-8-sig")
    summary=data.drop_duplicates(["code","phase","scenario"],keep="last")
    summary.to_csv(OUT/"comparison.csv",index=False,encoding="utf-8-sig")
    half=pd.concat(halves).drop_duplicates(["code","scenario","period"],keep="last")
    groups=pd.concat(states).drop_duplicates(["code","phase","scenario","state"],keep="last")
    groups.to_csv(OUT/"regime_comparison.csv",index=False,encoding="utf-8-sig")
    def metric(c,scenario="normal",phase="full"):
        return summary[(summary.code==c)&(summary.scenario==scenario)&(summary.phase==phase)].iloc[0]
    def trace(c,scenario="normal",phase="full"):
        return np.load(locations[c,phase,scenario]/(c+"_"+phase+"_"+scenario+"_daily.npy"))
    rawpf=[]
    for row in half.itertuples():
        tr=np.load(locations[row.code,"full",row.scenario]/(row.code+"_full_"+row.scenario+"_trades.npy"))
        year=int(row.period[:4]);first=int(row.period[-1])==1;a=stamp(f"{year}-01-01" if first else f"{year}-07-01")
        b=stamp(f"{year}-07-01" if first else f"{year+1}-01-01")
        rawpf.append(pf(tr[(tr[:,1]>a)&(tr[:,1]<=b),4]))
    half["profit_factor"]=rawpf;half.to_csv(OUT/"half_year_comparison.csv",index=False,encoding="utf-8-sig")
    moderate=["M0","M_L2","M_vol40","M_r100","M_short25_ema100","M_short50_ema100"]
    growth=["G0","G_crash_half","G_loss2_50","G_turn75","G_turn50"]
    codes=moderate+growth
    def comparisons(codes):
        return table(["方案","正常年化","压力年化","正常/压力定投年化XIRR","正常/压力回撤上界","正常/压力总资产"],
            [[LABEL[c],n(metric(c).cagr_pct)+"%",n(metric(c,"stress").cagr_pct)+"%",
              n(metric(c).xirr_pct)+"/"+n(metric(c,"stress").xirr_pct)+"%",
              n(metric(c).dd_bound_pct)+"/"+n(metric(c,"stress").dd_bound_pct)+"%",
              n(metric(c).total_assets)+"/"+n(metric(c,"stress").total_assets)] for c in codes])
    def recents(codes):
        return table(["方案","正常/压力年化","正常/压力回撤上界","正常/压力净利润"],
            [[LABEL[c],n(metric(c,phase="recent").cagr_pct)+"/"+n(metric(c,"stress","recent").cagr_pct)+"%",
              n(metric(c,phase="recent").dd_bound_pct)+"/"+n(metric(c,"stress","recent").dd_bound_pct)+"%",
              n(metric(c,phase="recent").net_profit)+"/"+n(metric(c,"stress","recent").net_profit)] for c in codes])
    events=[("2018熊市","2018-01-01","2019-01-01"),("2020三月急跌","2020-03-01","2020-04-01"),
            ("2021五月大跌","2021-05-01","2021-06-01"),("2022五月大跌","2022-05-01","2022-06-01"),
            ("2022十一月大跌","2022-11-01","2022-12-01"),("2022熊市全年","2022-01-01","2023-01-01"),
            ("2024二三月大涨","2024-02-01","2024-04-01"),("2025下半年","2025-07-01","2026-01-01")]
    eventcodes=["M0","M_L2","M_r100","M_short25_ema100","G0","G_crash_half"];ev=[];risk=[]
    for code in eventcodes:
        for scenario in ["normal","stress"]:
            d=trace(code,scenario);times=d[:,0];nav=d[:,2]
            rr=dict(code=code,scenario=scenario)
            for days in [1,7,30,90,365]:rr["worst_"+str(days)+"d_pct"]=float(np.min(nav[days:]/nav[:-days]-1)*100)
            risk.append(rr)
            for label,a,z in events:
                ix=np.searchsorted(times,[stamp(a),stamp(z)],side="right")-1;seg=nav[ix[0]:ix[1]+1]
                ev.append(dict(code=code,scenario=scenario,event=label,return_pct=(nav[ix[1]]/nav[ix[0]]-1)*100,
                               daily_dd_pct=float(np.max(1-seg/np.maximum.accumulate(seg))*100)))
    pd.DataFrame(ev).to_csv(OUT/"events.csv",index=False,encoding="utf-8-sig")
    pd.DataFrame(risk).to_csv(OUT/"rolling_risk.csv",index=False,encoding="utf-8-sig")
    def eventtable(codes):
        return table(["事件"]+[LABEL[c] for c in codes],[[label]+[n(next(x["return_pct"] for x in ev if x["code"]==c and x["scenario"]=="normal" and x["event"]==label))+"%" for c in codes] for label,_,_ in events])
    plt.rcParams.update({"font.sans-serif":["Microsoft YaHei","DejaVu Sans"],"axes.unicode_minus":False,"font.size":10})
    fig,axs=plt.subplots(2,2,figsize=(15,9),layout="constrained")
    shortnames={"M0":"原版","M_L2":"轻波动／2倍","M_vol40":"强波动控制","M_r100":"1%风险","M_short25_ema100":"0.25%空头","M_short50_ema100":"0.5%空头",
                "G0":"原优选","G_crash_half":"急跌减半","G_loss2_50":"连亏减半","G_turn75":"分歧降25%","G_turn50":"分歧减半"}
    for i,cc in enumerate([moderate,growth]):
        for j,(field,title) in enumerate([("cagr_pct","交易桶净值年化（%）"),("dd_bound_pct","4秒盘中回撤上界（%）")]):
            ax=axs[i,j];x=np.arange(len(cc))
            for k,scenario in enumerate(["normal","stress"]):
                bars=ax.bar(x+(k-.5)*.34,[metric(c,scenario)[field] for c in cc],.34,label="正常" if k==0 else "费用翻倍＋额外8秒",color="#0d9488" if k==0 else "#fb923c")
                ax.bar_label(bars,fmt="%.1f",fontsize=8,padding=2)
            ax.set_title(("温和档：" if i==0 else "增长档：")+title);ax.set_xticks(x,[shortnames[c] for c in cc],rotation=10);ax.grid(axis="y",alpha=.15);ax.set_axisbelow(True)
    fig.legend(*axs[0,0].get_legend_handles_labels(),loc="outside lower center",ncol=2)
    fig.suptitle("2017–2026年9月｜初始1000＋月投1000｜每笔正净利润90%复投",fontsize=15)
    fig.savefig(OUT/"overview.png",dpi=150);plt.close(fig)
    fig,axs=plt.subplots(2,2,figsize=(14,8),layout="constrained")
    for i,cc in enumerate([["M0","M_L2","M_r100","M_short25_ema100"],["G0","G_crash_half","G_loss2_50","G_turn75"]]):
        for code in cc:
            d=trace(code);time=pd.to_datetime(d[:,0].astype("int64"),unit="ms",utc=True).tz_convert("Asia/Shanghai")
            axs[i,0].plot(time,d[:,2],label=LABEL[code],linewidth=1);axs[i,1].plot(time,-d[:,3]*100,label=LABEL[code],linewidth=1)
        axs[i,0].set_yscale("log");axs[i,0].set_title(("温和" if i==0 else "增长")+"：正常净值（对数轴）")
        axs[i,1].set_title("日终净值回撤（%）");axs[i,0].legend(fontsize=8)
        for ax in axs[i]:ax.grid(alpha=.2)
    fig.savefig(OUT/"nav_drawdown.png",dpi=150);plt.close(fig)
    # States are attributed using the last closed hour, not future labels.
    state_table=[]
    for state,label in zip(STATES,["急跌","大涨","震荡","熊市","牛市","转换"]):
        values=[]
        for c in ["M0","M_L2","M_r100","G0","G_crash_half"]:
            g=groups[(groups.code==c)&(groups.phase=="full")&(groups.scenario=="stress")&(groups.state==state)].iloc[0]
            values.append(n(g.nav_log_contribution))
        state_table.append([label]+values)
    statetable=table(["压力下状态对数贡献","温和原版","温和轻波动","温和1%风险","增长原优选","增长急跌减半"],state_table)
    # Actual baseline source and new recommendations remain separately identifiable.
    allplans={p["code"]:p for p in plans()+plans(True)}
    selected=dict(scope="offline candidates only; not live fund settings",moderate=allplans["M_L2"],defensive=allplans["M_r100"],
                  growth=allplans["G0"],growth_observation=allplans["G_crash_half"],moderate_short_observation=allplans["M_short25_ema100"],
                  recommendation_is_synthesis="development ranking is preserved; full/recent diagnostics led to retaining original controls rather than auto-replacing them")
    (OUT/"research_candidates.json").write_text(json.dumps(selected,indent=2),encoding="utf-8")
    sources="""# 本轮原始资料

查询日期：2026-10-07。Exa五个搜索方向，共20条搜索候选（包含重复）；进一步读取以下五项原始资料的正文开头、摘要与方法说明。没有把论文策略或收益直接搬到项目。

1. **Cederburg, O’Doherty, Wang, Yan (2020), On the performance of volatility-managed portfolios.** JFE 138, 95–117；DOI 10.1016/j.jfineco.2020.04.015。[作者大学PDF](https://www.lehigh.edu/~xuy219/research/COWY.pdf)。103种股票策略中，波动管理没有普遍胜出，某些依赖事后最优组合的收益不能实时实现。项目因此采用固定风险缩放公式，保留未缩放对照，同时检查正常与压力、完整与近期。

2. **Time-series momentum and market timing in Bitcoin (2026).** Risk Management 28，article54，2026-07-10发布；DOI 10.1057/s41283-026-00234-7。[出版社摘要](https://link.springer.com/article/10.1057/s41283-026-00234-7)。论文比较信号速度，报告较慢的12周基准信号优于较快方案，动态调速帮助有限。项目只测试EMA96的邻域192/384小时，没有声称复现其12周模型；入场和止损周期搭配不同，结果不能直接相提并论。

3. **Van Hemert, Ganz, Harvey, Rattray, Sanchez Martin, Yawitch (2020), Drawdowns.** Journal of Portfolio Management46(8),34–50。[作者PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P147_Drawdowns.pdf)。回撤规则可帮助识别策略质量变化，但降低风险通常同时影响预期收益。项目测试回撤接近限额时缩减新仓预算，以及空头连亏后的独立预算保护。空头连亏规则是项目自行提出的假设，不是论文验证过的BTC结论。

4. **Goulding, Harvey, Mazzoleni (2023), Momentum turning points.** JFE149,378–406；DOI10.1016/j.jfineco.2023.05.007。[作者PDF](https://people.duke.edu/~charvey/Research/Published_Papers/P158_Momentum_turning_points.pdf)。快慢信号的同向与分歧可描述牛市、熊市、调整和反弹，速度需要在噪声与转向反应之间权衡。项目测试快EMA24与原慢趋势分歧时降低新仓预算，没有复现其股票月度组合。

5. **Baltussen, Martens, van der Linden (2026), The Best Defensive Strategies: Two Centuries of Evidence.** Financial Analysts Journal，2026-01-22上线，pages6–34；DOI10.1080/0015198X.2025.2602270。[出版社全文](https://www.tandfonline.com/doi/full/10.1080/0015198X.2025.2602270)。多资产防御策略与趋势跟随在回撤的不同阶段可以互补，趋势在最初转向时可能方向不利。本项目只有BTC，无法复制其跨资产DAR组合；只借此区分突然冲击和持续下跌，并测试已知急跌状态下缩小新仓。

这些资料支持提出测试问题，不能证明项目未来盈利。防御与收益之间的取舍，必须由本项目实际回放结果说明。
"""
    (OUT/"SOURCE_NOTES.md").write_text(sources,encoding="utf-8")
    dev=summary[summary.phase.isin(["train","validation"])]
    def devrow(c):
        rr=dev[dev.code==c];a=rr[(rr.phase=="train")&(rr.scenario=="stress")].iloc[0];b=rr[(rr.phase=="validation")&(rr.scenario=="stress")].iloc[0]
        return [c,n(a.xirr_pct)+"%",n(b.xirr_pct)+"%",n(rr.dd_bound_pct.max())+"%","停机" if any(rr.halt_time.notna()) else "未停机"]
    ablation=table(["候选","训练压力XIRR","验证压力XIRR","四档最大回撤上界","开发停机"],
        [devrow(c) for c in ["M0","M_vol80","M_r150","M_r200","M_ema192","M_ema384","M_dd8_r200","M_dd_r175","M_dd_r200_floor25","M_turn50","M_turn75","G0","G_dd20","G_loss2_50","G_turn75","G_crash_half"]])
    totals=sum(counts.values());main_m=metric("M_L2");old_m=metric("M0")
    report=f"""# 温和与增长两档：风险预算、趋势拐点进一步研究

研究日期：2026-10-07。**本轮没有找到全面优于上一轮增长策略的新方案。温和档有小幅、较稳妥的改善；更强防守需要接受较低收益。**

## 1. 最终建议

| 用途 | 研究建议 | 本轮判断 |
| --- | --- | --- |
| 温和主候选 | 原EMA96与ER12规则＋轻波动控制；风险1.25%、名义上限2倍；可采用2倍杠杆 | 正常/压力净值年化39.87%/34.73%，回撤上界22.55%/22.95%；改善幅度有限 |
| 更低回撤对照 | 风险1%、波动门槛60%、名义上限2倍、杠杆3倍 | 正常/压力年化30.74%/28.15%，回撤17.72%/18.79% |
| 增长主候选 | 保留上一轮波动控制＋空头趋势确认 | 正常/压力年化176.05%/142.41%；新保护规则尚未全面胜出 |
| 增长观察候选 | 急跌状态的新仓预算减半 | 最近年化略改善，完整历史增长略低；可以继续观察，不自动替换 |
| 温和空头观察 | 风险0.25%、名义上限0.5倍，日/小时趋势确认 | 最近有所改善，全历史压力表现仍弱于轻波动单多 |

**基金服务、账号、账簿和运行参数没有修改。**这些是策略研究候选，不是实际基金净值，也没有实施真实交易、重启或部署。

## 2. 数据与资金口径

- 2017-01-01至2026-10-01，不包含终点；真实价格从2017-08-18起，上市前保持现金。71,949,600根4秒OHLC；4秒执行、小时及已完成日线信号。
- 初始1000、每月1000，完整区间累计出资117,000；每笔已实现正净利润90%复投、10%进入无息留存桶。
- 最近区间2025-01-01至2026-10-01重新启动，累计出资21,000。它与连续账户是两个资金路径。
- 正常手续费0.02%，平仓名义价值收取一次；压力费用0.04%并额外延迟8秒。沿用上轮已固定的站点多空、杠杆与强平契约，不添加站点不存在的资金费。
- 温和保持原总回撤停机25%、日内暂停1.5%；增长保持70%、5%。均使用交易桶单位净值判断，盘中回撤另给保守上界。
- 保留同时一仓、平仓后下一整点再冷却4小时。没有改变现行基金的申购费、月度分红和申赎账簿。

## 3. 完整结果：温和档

年化为剔除外部资金流的交易桶单位净值CAGR；XIRR是全部月投及期末两桶资产的资金加权年化。

{comparisons(moderate)}

温和轻波动2倍版本，正常总资产相对原版增加{(main_m.total_assets/old_m.total_assets-1)*100:.2f}%。年化提升约0.36个百分点，**不能称为显著突破**。

### 降低杠杆为什么收益几乎不变

M_vol80为3倍，M_L2为2倍。两者保持相同风险预算及2倍名义上限，回放收益在表格精度内一致，均未强平。降低杠杆主要增加占用保证金、拉远强平价；名义仓位不变，账户涨跌不会自动变小。2倍空头或多头仍有风险，历史未强平不能保证未来不强平。

如果要真正降低账户回撤，M_r100同时降低风险预算，效果比只改杠杆明显。

## 4. 完整结果：增长档

{comparisons(growth)}

- 连续两笔空头亏损后预算减半：完整和最近收益下降，压力回撤反而更高，**本轮不采用**。
- 快慢趋势分歧缩仓：在增长档没有全面改善，50%缩仓版本压力回撤达到65.85%。
- 急跌新仓减半：完整年化176.05%→173.53%，压力142.41%→140.75%；压力回撤60.81%→60.00%。这是小幅收益与风险交换，不是全面升级。
- 原增长候选在最近区间仍有较高回撤，上一版提出的局限没有消失。

![两档正常与压力对比](overview.png)

## 5. 最近区间：检验环境变化

{recents(["M0","M_L2","M_r100","M_short25_ema100","M_short50_ema100","M_dd_r175","M_dd_r200_floor25","G0","G_crash_half","G_loss2_50"])}

较强账户回撤缩仓的温和候选最近正常年化只剩7.65%或8.42%，远低于原版22.04%。其原因之一是风险要随着净值离开回撤区逐渐恢复，缩仓可能让后续反弹参与度持续偏低。该机制可以降低损失，也可能延长恢复过程。

温和0.5%空头最近正常/压力年化26.41%/17.98%，比轻波动单多高，但完整压力年化33.80%低于34.73%，压力回撤23.59%也高于22.95%。不能只凭近期选胜者。

温和趋势分歧减半M_turn50在开发区间合格，完整压力盘中回撤上界却达到25.07%，最近压力达到25.08%；收盘未触发永久停机。**按本轮25%盘中约束仍判为不合格**，不会因为收盘未停机而放宽标准。

![连续净值与日终回撤](nav_drawdown.png)

## 6. 熊市、急跌与大涨事件

固定自然月/年边界，连续账户正常净值收益，没有截取最有利的几小时。

{eventtable(eventcodes)}

同窗口压力结果、窗口内日终回撤见[events.csv](events.csv)。熊市全年与突然暴跌要分别看：趋势形成后空头可能提供增长，最初冲击及随后反弹仍可能产生损失。

### 震荡及其他状态归因

{statetable}

沿用上轮因果分类：每根4秒开始前，只用已完成的小时/日线。急跌、大涨优先，然后按ER24及距EMA96分类震荡，其余分熊市、牛市和转换。这里是状态内净值变化的**对数贡献**，六项可相加，不能当成各状态的独立策略收益率。第一波暴跌发生前还没有急跌标签，损失可被记在此前状态。

完整/近期、正常/压力的状态统计见[regime_comparison.csv](regime_comparison.csv)。没有找到所有状态都持续高收益的规则。

## 7. 这轮搜索怎样指导测试

五个方向20条搜索候选（含重复），进一步读取五项原始研究；出处、日期和边界见[SOURCE_NOTES.md](SOURCE_NOTES.md)。

| 原始研究 | 核心启发 | 本项目测试与结论 |
| --- | --- | --- |
| [On the performance of volatility-managed portfolios](https://www.lehigh.edu/~xuy219/research/COWY.pdf) | 波动管理不普遍胜出，事后组合权重可能无法实时实现 | 固定缩放公式，保留原策略；温和轻缩放略改善，强缩放牺牲收益 |
| [Time-series momentum and market timing in Bitcoin](https://link.springer.com/article/10.1057/s41283-026-00234-7) | BTC较慢信号可减少过度反应，但速度选择有市场差异 | 测试EMA192/384小时没有胜出；并未复现论文12周模型 |
| [Drawdowns](https://people.duke.edu/~charvey/Research/Published_Papers/P147_Drawdowns.pdf) | 回撤规则会同时改变风险与预期收益 | 温和风险2%加入强缩仓后能避开部分停机，但近期增长大幅下降 |
| [Momentum turning points](https://people.duke.edu/~charvey/Research/Published_Papers/P158_Momentum_turning_points.pdf) | 快慢信号分歧可帮助描述拐点 | 自行设计入场预算折扣；没有获得普遍改善 |
| [The Best Defensive Strategies](https://www.tandfonline.com/doi/full/10.1080/0015198X.2025.2602270) | 趋势跟随与防御策略在回撤不同阶段可能互补 | 只测试BTC已知急跌状态新仓折扣，没有复制跨资产DAR组合 |

**论文用于提出假设，实际选型依据本项目回放。**不能从股票月度或多资产研究直接推导BTC小时策略获利。

## 8. 推荐温和候选的具体规则

1. 小时收盘高于EMA96，EMA96高于24小时前才做多；初始止损2ATR14，趋势失效、止损、14天超时或风控退出。
2. 亏损后12小时内再次开多，额外要求ER12≥0.30且12小时净变化为正。
3. 基础单笔风险1.25%，名义仓位上限权益2倍，杠杆2倍。
4. 使用最近168小时、至少48小时的小时对数收益样本标准差，并年化得到σ；在入场信号确定预算：
   `mult = max(0.35, min(1, 0.80 / σ))`。
   80%是风险缩放门槛，**不是实际组合波动率目标**；σ=160%时风险预算减半。
5. 单日损失1.5%暂停到次日；总回撤25%永久停止；同时一仓，下一整点后冷却4小时。
6. 该主候选保持单多。空头观察候选另行记为风险0.25%、名义上限0.5倍、日EMA100熊市与小时EMA24/96/192下行确认、ER12≥0.30、3ATR初始止损、4ATR固定入场基准追踪。

更低回撤版本：风险1%、波动门槛60%、杠杆3倍，其余相同；这是实际跑过的M_r100。风险预算与杠杆不能互相替代。

增长候选完整参数沿用[上一轮报告](../bear_regime_2026-10-07/REPORT.md)：多头3.5%，普通空头2%，下跌延续确认时空头10%预算，波动门槛60%，最大5倍杠杆。候选文件见[research_candidates.json](research_candidates.json)。

## 9. 新规则、筛选过程与失败方案

{ablation}

- 回撤缩仓：在新仓信号时用已知交易桶净值计算当前回撤D；超过起点S后，乘以`max(floor,min(1,(limit-D)/(limit-S)))`。只改变新仓预算，不凭未来损益决定仓位，不强制增加新退出规则。
- 空头连亏缩仓：仅记录已经关闭的空头亏损；达到2或3次后预算打折，空头非亏损交易后恢复。多头不直接受该空头预算折扣影响，但账户本金和既有亏损后ER规则仍有资金路径耦合。
- 趋势分歧缩仓：原慢趋势允许交易时，多头遇到收盘低于EMA24且EMA24低于6小时前，就打折；空头遇到收盘高于EMA24或EMA24上升，也打折。条件只用于新仓，不替代原趋势出场。
- 急跌折扣：已知24小时跌幅≤−8%或6小时≤−4%时，下一新仓风险减半；不会提前预测第一波跳跌。
- EMA延长：只把长期信号改为192/384小时，止损ATR仍是小时尺度。较慢入场与较紧止损的搭配可能影响表现，因此这轮失败不能证明所有慢周期BTC策略无效。
- 温和1.5%风险普通波动控制候选，开发正常盘中回撤25.72%，即使收盘未停机也不合格；2%普通风险则在开发阶段停机。
- 开发合格只决定继续诊断，不保证完整/最近也合格。M_turn50就是反例。

第一轮28套候选，第二轮10套（包括重复对照），随后6套温和固定参数诊断。本轮共{totals}次实际执行。开发为2017–2022、验证2023–2024，各自独立账户、正常与压力均运行；按70%或25%约束剔除停机、强平、盘中超限，使用6:2权重的较差情景XIRR排序。选择记录保留在各轮protocol.json；完整与最近随后运行。

综合建议保留了一些原对照，而不是自动采用每轮新候选排名第一。这个建议已经看过完整与最近诊断，**属于同历史上的综合判断，不是全新样本外证明**。没有更新基金正式策略文件或推广收益口径。

## 10. 证据与复现

- [全部236次执行](all_runs.csv)、[去重对照](comparison.csv)、[每半年收益/胜率/PF/归一化PF/交易数/MAE/MFE](half_year_comparison.csv)、[滚动最差区间](rolling_risk.csv)。
- 快照、配置和完整交易/日频轨迹分别保存在risk_grid、turning_grid、moderate_diagnostics目录；筛选轮同时保存最近轨迹，诊断轮最近原始结果见summary.csv。原始3.45GB行情不重复复制。
- 26项针对性测试通过：覆盖因果性、手续费/强平、冻结引擎等价、回撤预算与存款中性、空头预算缩减及恢复、趋势分歧不看未来、资金守恒与状态贡献守恒。未做真实站点、基金账簿或部署验收。
- 历史价格已经反复用于调参；无全新样本外、置信区间或未来收益保证。站点理想成交和逐笔90%复投不能替代基金级回测。
- 25%/70%为收盘停机触发标准；盘中、跳空、缺价与下一根成交可能超限。缺价时等待下一有数据开盘，与运行端行情新鲜度保护不同。
- 来源与产物SHA-256见[manifest.json](manifest.json)。数据哈希沿用已核对前版清单：`595b30ea805f823d14e2cd22698d181c16cf0a9af62f46bedca77c5dc66ff111`。

```powershell
python tools/btc_tier_research.py --data <4秒数据目录> --out .build/btc_tier_2026-10-07
python tools/btc_tier_research.py --data <4秒数据目录> --out .build/btc_tier_turning_2026-10-07 --turning
python tools/btc_tier_diagnostics.py --data <4秒数据目录> --out .build/btc_tier_diagnostics_2026-10-07 --previous .build/btc_tier_2026-10-07
python tools/btc_tier_report.py
```

需安装项目研究依赖`.[research]`。建议下一轮先冻结这些候选进行模拟观察，特别跟踪空头在下跌延续及急跌反弹中的表现；继续在同一份历史上堆叠过滤条件没有显示稳定收益改善。
"""
    (OUT/"REPORT.md").write_text(report,encoding="utf-8")
    for path,marker,line in [
        (ROOT/"docs/materials/research/README.md","## 新增研究\n","- [2026-10-07 温和与增长两档进一步优化](dual_tier_2026-10-07/REPORT.md)：波动控制、较低杠杆、小仓空头、回撤/连亏保护与趋势分歧；236次回放和五项原始资料。\n"),
        (ROOT/"docs/README.md","| 原始研究证据与口径差距 |","| 温和与增长档进一步研究 | [两档研究报告](materials/research/dual_tier_2026-10-07/REPORT.md) |\n")]:
        text=path.read_text(encoding="utf-8-sig")
        if line not in text:text=text.replace(marker,marker+"\n"+line if marker.startswith("##") else line+marker);path.write_text(text,encoding="utf-8")
    manifest=dict(scope="offline only",counts=counts,total_runs=totals,source_hashes={},artifacts={},
                  price_sha256_from_previous_manifest="595b30ea805f823d14e2cd22698d181c16cf0a9af62f46bedca77c5dc66ff111")
    for rel in ["tools/btc_tier_research.py","tools/btc_tier_diagnostics.py","tools/btc_tier_report.py","tools/btc_regime_research.py",
                "tools/btc_bear_research.py","tools/btc_long_short_research.py","tools/btc_regime_report.py","tests/test_tier_research.py",
                "tests/test_regime_research.py","tests/test_bear_research.py","tests/test_long_short_research.py"]:
        manifest["source_hashes"][rel]=hashlib.sha256((ROOT/rel).read_bytes()).hexdigest()
    for path in OUT.rglob("*"):
        if path.is_file() and path.name!="manifest.json":manifest["artifacts"][path.relative_to(OUT).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
    (OUT/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    print(json.dumps(dict(out=str(OUT),runs=counts,total=totals),ensure_ascii=False))
if __name__=="__main__":main()

