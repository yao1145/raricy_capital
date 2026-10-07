"""Build the dated Markdown study and charts from completed offline replays."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

NAMES = {
    'moderate_long': '温和原版',
    'moderate_daily200_small': '温和＋小仓空头',
    'aggressive_long': '高风险原版（5%风险）',
    'aggressive_long_risk350': '高风险优化（3.5%风险）',
    'aggressive_daily200_small_risk400': '高风险多空（4%风险）',
}

def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] + ['| ' + ' | '.join(map(str, row)) + ' |' for row in rows])

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--direction', type=Path, required=True)
    ap.add_argument('--risk', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args=ap.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    records=[]
    for source,name in [(args.direction,'direction_grid'),(args.risk,'risk_grid')]:
        dest=args.out/name;dest.mkdir(exist_ok=True)
        # Only small research evidence; never copy the 3.45 GB source candles.
        for path in source.iterdir():
            if path.is_file() and path.name!='progress.json':shutil.copy2(path,dest/path.name)
        records.extend(json.loads((dest/'results.json').read_text(encoding='utf-8')))
    def get(code,scenario='normal',start='2017',end='2026'):
        return next(r for r in records if r['code']==code and r['scenario']==scenario and r['start'].startswith(start) and r['end_exclusive'].startswith(end))
    codes=list(NAMES)
    mainrows=[];pressure=[];recent=[];metrics=[]
    for code in codes:
        r=get(code);s=get(code,'stress');q=get(code,start='2025')
        mainrows.append([NAMES[code],f"{r['unit_nav_cagr_pct']:.2f}%",f"{r['intrabar_dd_bound_pct']:.2f}%",f"{r['total_assets']:,.2f}",f"{r['net_profit']:,.2f}",r['trades']])
        pressure.append([NAMES[code],f"{s['unit_nav_cagr_pct']:.2f}%",f"{s['intrabar_dd_bound_pct']:.2f}%",s['halt_time'][:10] if s['halt_time'] else '未停机',f"{s['total_assets']:,.2f}"])
        recent.append([NAMES[code],f"{q['unit_nav_cagr_pct']:.2f}%",f"{q['intrabar_dd_bound_pct']:.2f}%",f"{q['net_profit']:,.2f}",q['trades']])
        metrics.append([NAMES[code],f"{r['win_rate_pct']:.2f}%",f"{r['profit_factor']:.3f}",f"{r['normalized_profit_factor']:.3f}",f"{r['mae_pct']:.3f}%",f"{r['mfe_pct']:.3f}%",r['short_trades']])
    plt.rcParams.update({'font.family':'Microsoft YaHei','axes.unicode_minus':False,'font.size':10})
    fig,ax=plt.subplots(2,2,figsize=(15,9))
    colors=['#276fba','#22a08a','#c54e47','#a66bbe','#d58e25']
    for i,code in enumerate(codes):
        folder=args.out/('risk_grid' if '_risk' in code else 'direction_grid')
        daily=np.load(folder/(code+'_normal_daily.npy'))
        panel=ax[0,0] if code.startswith('moderate') else ax[0,1]
        panel.plot(pd.to_datetime(daily[:,0],unit='ms',utc=True),daily[:,2],label=NAMES[code],color=colors[i])
    for panel,title in [(ax[0,0],'温和方案：交易桶单位净值'),(ax[0,1],'高风险方案：交易桶单位净值')]:
        panel.set_yscale('log');panel.set_title(title+'（对数刻度）');panel.legend(fontsize=9);panel.grid(alpha=.2)
    labels=['温和原版','温和多空','高风险5%','高风险3.5%','高风险多空4%'];x=np.arange(len(codes))
    for panel,field,title in [(ax[1,0],'unit_nav_cagr_pct','全历史模拟年化：正常 / 压力'),(ax[1,1],'intrabar_dd_bound_pct','全历史盘中回撤上界：正常 / 压力')]:
        panel.bar(x-.18,[get(c)[field] for c in codes],.36,label='正常',color='#277cb0')
        panel.bar(x+.18,[get(c,'stress')[field] for c in codes],.36,label='费用×2、额外8秒',color='#dc9165')
        panel.set_xticks(x,labels,rotation=12);panel.set_title(title);panel.set_ylabel('%');panel.legend(fontsize=9);panel.grid(axis='y',alpha=.2)
    ax[1,1].axhline(70,ls='--',color='#ba4d43',lw=1)
    fig.suptitle('2017–2026年9月 · 4秒执行 · 月投1000 · 正净利润90%复投',fontsize=15)
    fig.tight_layout();fig.savefig(args.out/'overview.png',dpi=160);plt.close(fig)
    frames=[pd.read_csv(args.out/name/'half_year.csv') for name in ['direction_grid','risk_grid']]
    half=pd.concat(frames);half=half[half.code.isin(codes)].drop_duplicates(['code','period'])
    grid=half.pivot(index='code',columns='period',values='return_pct').reindex(codes)
    fig,ax=plt.subplots(figsize=(16,4.5));im=ax.imshow(grid,aspect='auto',cmap='RdYlGn',vmin=-40,vmax=100)
    ax.set_xticks(np.arange(len(grid.columns)),grid.columns,rotation=45,ha='right');ax.set_yticks(np.arange(len(codes)),[NAMES[c] for c in codes])
    for y in range(len(codes)):
        for x,value in enumerate(grid.iloc[y]):ax.text(x,y,f'{value:.0f}',ha='center',va='center',fontsize=8,color='white' if value>80 or value<-30 else 'black')
    ax.set_title('连续账户的半年单位净值收益（%；2017H2及2026H2为不完整区间）')
    fig.colorbar(im,ax=ax,label='收益%；色阶截断于-40与100，数字保留实际值')
    fig.tight_layout();fig.savefig(args.out/'half_year_heatmap.png',dpi=160);plt.close(fig)
    half.to_csv(args.out/'selected_half_year.csv',index=False,encoding='utf-8-sig')
    sha='d2331679f4886bc96ed79aec4928938fae56437e'
    root='https://github.com/raricycms/raricy.com/blob/'+sha+'/'
    text=f'''# BTC 多空与风险预算优化研究

研究日期：2026-10-07。**本次结论：温和主策略继续保留；高风险方案优先降低单笔风险到3.5%。空头适合继续小仓模拟观察，目前不足以证明应全面改成多空。**

## 1. 核心结果

同一引擎重新播放2017–2026年9月历史，账户累计外部出资117,000小鱼干。下表年化均为**剔除出资及留存现金流的交易桶单位净值CAGR**，含2017上市前现金等待期；资产为交易桶与留存桶之和。

{table(['方案','单位净值年化','盘中回撤上界','期末总资产','净利润','交易数'],mainrows)}

- 高风险3.5%风险版本，相比5%版本，正常年化由181.19%降到149.57%，盘中回撤上界由67.55%降到56.72%；压力情景仍能完整运行。
- 温和加入小仓空头，正常年化仅增加0.39个百分点，压力年化反而减少0.13个百分点。它的总资产也略低于原版，暂不替换温和主策略。
- 高风险4%多空版本的正常年化149.31%，回撤上界67.57%，压力回撤69.16%；与3.5%单多相比，没有更好的整体取舍。

![正常净值与压力测试对比](overview.png)

### 执行压力：手续费翻倍＋额外8秒延迟

{table(['方案','压力年化','压力回撤上界','永久停机','压力期末资产'],pressure)}

5%高风险单多在2022-05-30停机；5%小仓多空在2022-05-25停机。**停机后不重新启动**，后续月投保持现金。表中压力年化包含停机后的平坦净值期，因此与「停机前的活跃年化」不同。

### 最近独立启动区间：2025-01-01至2026-10-01（不含终点）

该区间重新以1000启动、月投1000，累计出资21,000；与完整历史账户不是同一个资金路径。

{table(['方案','区间年化','回撤上界','净利润','交易数'],recent)}

空头在最近区间确有帮助：温和年化22.04%→25.66%；风险4%的高风险年化55.94%→70.49%。**这种阶段性改善不能覆盖全历史与压力情景中的劣化。**

## 2. 新站点规则：已经按源码纳入研究

源码固定于提交`{sha}`，避免以后上游更新改变本次口径。

- 开仓接口增加`direction: "long" | "short"`，未传默认多头；杠杆可为**1至100的整数**，不接受任意小数。[杠杆与方向定义]({root}src/lib/market-leverage.ts)、[机器人接口]({root}docs/bot/trade-bot.md)。
- 手续费平仓时收一次，基数为「投入保证金×杠杆×平仓价/开仓价」，费率0.02%，两种方向都相同；最后向下取整到0.0001小鱼干。[结算公式]({root}src/lib/market-math.ts)。
- 多头强平价为`entry×(1−1/L)`；空头为`entry×(1＋1/L)`。**1倍空头仍可在价格翻倍时强平**。研究使用4秒高低价检测强平，亏损封顶于投入保证金。
- 站点模拟没有资金费、借贷利息；本次不添加这些费用。该条件不可直接搬到真实合约交易。

## 3. 数据、资金与筛选方法

- 沿用已有真实4秒OHLC数据，共71,949,600根；行情从2017-08-18开始，结束于2026-10-01，不包含10月1–7日。4秒是执行周期，EMA/ATR趋势信号仍取已完成的小时线。
- 初始1000、每月1000；每笔已实现正净利润90%留在交易桶、10%进入无息留存桶。所有亏损由交易桶承担。
- 月投按照当时净值增发研究单位，正利润留存相应减少单位数。年化不把外部存款误当收益；总资产保留所有留存现金。
- 这是**策略现金桶研究**，没有重放基金5%申购费、每月分红、申赎和分红负债。不能作为基金产品的历史净值。
- 第一轮26套方案：训练2017–2022，验证2023–2024；先排除回撤超限和停机，再按验证年化/回撤在空头候选中挑选；原多头单列作对照。2025–2026区间随后计算。
- 第二轮10套风险预算方案：同样的时间划分，训练和验证都跑正常及压力情景，剔除任一情景停机/超限，再按验证情景中较差年化/较大回撤筛选。选择结果为**单多、风险3.5%**，随后跑最近区间、完整历史及压力情景。
- 全部历史已经在此前研究中使用过，第二轮也在第一轮之后进行。上述时间划分是**按时间评估，不能称为全新样本外验证**。
- 本次采用206根连续有效小时的入场门槛；旧归档回测部分只要求8根。EMA在研究中递推整个历史，运行端从收到的窗口起始处初始化。因此本次单多属于统一引擎的重放对照，**不与旧报告巨额余额直接比较，也不宣称逐笔复刻当前服务**。

## 4. 具体优化参数

### A. 温和基金：保留原多头，3倍

- 小时EMA96；价格高于EMA，EMA高于24小时前EMA时做多。
- 初始止损2ATR14；趋势条件失效、止损、日内风控、总回撤或14天超时退出。
- 单笔风险预算1.25%，名义仓位上限权益2倍；杠杆3倍。
- 亏损后12小时内再开多，额外要求ER12≥0.30且12小时净变化为正。
- 单日损失暂停1.5%，总回撤停机25%；同时最多一仓，平仓后等下一整点再冷却4小时。

**空头观察候选：**保留上面多头，只有最近完成日线收盘低于200日EMA、该EMA低于10日前EMA，且小时价格低于EMA96、小时EMA低于24小时前、ER12≥0.35、12小时净变化为负时开空。空头风险0.25%，名义仓位上限0.5倍权益，初始止损2ATR，追踪止损3ATR。追踪ATR以入场信号的距离折算为固定基准，不随未来波动重估。

该候选不是同时持有多空对冲；一仓限制下会占用后续入场机会，亏损也会影响之后的多头ER门槛。

### B. 高风险基金：单笔风险从5%降至3.5%，杠杆5倍

- 延续原来的小时EMA96＋24小时斜率多头条件、2ATR止损和14天超时。
- 风险预算**3.5%**；名义仓位上限5倍；杠杆保持**5倍**。
- 不启用温和版本的亏损后ER12门槛；日内暂停仍为5%，总回撤停机70%。
- 保留一仓、整点冷却4小时，暂不加入空头。

3.5%为本次正常与压力训练/验证筛选结果。3%、3.5%、4%、4.5%、5%都在候选中；单多与小仓空头各五档，具体数值见[risk_grid/summary.csv](risk_grid/summary.csv)。4%单多正常年化153.83%、压力年化123.52%，但压力回撤68.18%，比3.5%的61.61%更接近停机线。

### 仓位公式

`d=2×ATR14/信号收盘价`，`q`为名义仓位/交易权益，`r`为风险预算，`c`为名义上限，`f`为费率。

- 多头：`q=min(c, r/(d+f×(1−d)), L)`。
- 空头：`q=min(c, r/(d+f×(1＋d)), L)`。
- 保证金：`floor(权益×q/L, 0.0001)`；不足1小鱼干时不开仓。

这是按止损价计算的**目标风险**；跨价、延迟、断线或强平可让实际损失超出预算，不构成损失保证。

## 5. 为什么不直接提高杠杆

名义仓位相同、没有强平时，账户盈利近似为`q×价格变化`，手续费也由`q`决定；杠杆变化只调整占用保证金与强平距离。

本次保持温和名义上限2倍与风险预算不变，将杠杆改为2、4、7、10倍，年化约39.90%、39.90%、39.90%、39.86%。10倍温和/高风险两组等名义仓位对照都出现过1次强平；其收益差异包含强平与保证金0.0001取整造成的仓位变化，不能仅归因于取整。本次还测试`L=min(上限, max(1, floor(0.60/d)))`的入场动态杠杆，结果也几乎一致。

**提高名义仓位或风险预算才会提高收益放大程度，同时也提高损失。**这里5%的版本已经在压力情景失效，进一步加杠杆并加仓缺少依据。不能把「可输入100倍」理解成「长期增长应使用100倍」。

## 6. 空头为什么没有普遍改善

26套方向候选覆盖：镜像做空、ER30过滤、半风险、200小时EMA熊市过滤、192小时慢空头、3ATR空头追踪、ER45、7/10倍并提高仓位上限、200日EMA小/半空头、日线熊市＋48小时破低。

- 镜像方案在训练阶段就触发回撤停机：温和2019-02-12、高风险2019-02-06；验证阶段也存在失效。不能仅根据熊市价格最终下跌判断空单会赚钱。
- 200小时和200日EMA含义不同。前者仍追随较短下跌，容易遭遇急反弹；200日过滤降低空头频率，但不能消除反弹。
- 小仓空头增加交易数与手续费；多空共用冷却、权益与日内风控，空头赚了钱也不保证总组合增值。
- 2026H1温和小仓空头净值增长22.20%，单多12.30%；但2025H2多空−3.27%、单多−1.22%，2026H2部分区间多空6.29%、单多8.09%。其优势随区间变化。

以上原因是基于策略结构与阶段结果的解释；本轮没有独立分解「反弹、费用、错过多头」各自的因果贡献。

## 7. 牛熊转换与半年表现

![连续账户半年收益](half_year_heatmap.png)

图中每个格子由连续账户的日终单位净值计算，资金路径不中途重置。2017H2从8月18日开始；2026H2只到9月底。数值保留实际收益，色阶在−40%/100%截断以便观察亏损区间。

- 2018H1：高风险5%为−36.11%，3.5%为−20.73%；减少风险预算减轻了下行。
- 2022H1：高风险5%为−30.51%，3.5%为−27.43%；优化版本仍会经历严重亏损，不能称为低风险。
- 2025H2：高风险5%为−24.29%，3.5%为−9.73%；但2026H1优化版也只增长3.26%，旧版24.21%。
- 温和空头并非熊市保险：2018H1略改善，2020H1、2021H2和2022H2也可能拖累。

完整半年交易数、胜率、归一化PF、MAE/MFE及区间日终回撤见[selected_half_year.csv](selected_half_year.csv)。半年回撤从该区间起点重新计算，不能相加成全历史最大回撤。

### 全历史交易指标

{table(['方案','胜率','金额PF','归一化PF','平均MAE','平均MFE','空头交易数'],metrics)}

金额PF为交易净盈利总额/净亏损总额，受后期仓位规模影响；归一化PF先以各笔入场权益归一化再求比值。MAE/MFE为持仓期间标的价格的最大不利/有利偏移，不是杠杆后账户收益。胜率不高仍盈利，依赖少数大趋势覆盖多次小亏。

## 8. 本轮没有解决的执行差异

- 普通信号在4秒收盘判断，下一条可观察开盘价成交；压力情景再延迟两根4秒。信号不在自身尚未完成的K线内成交。
- 强平按照OHLC高低价检测，但4秒内部走势顺序未知；盘中回撤按先有利再不利的保守上界计算。停机依据收盘净值，所以回撤上界及实际跳价损失可能超过阈值。
- 站点实际使用Binance/Bybit现价，研究使用历史BTC现货数据；未测两个报价源的逐笔差异、联网延迟、强平扫描频率与断网场景。
- 此轮被保留的全历史方案记录强平次数均为0；部分10倍候选及等名义仓位对照发生过强平。这不表示真实断线不会强平，也不是高倍杠杆安全证明。
- 永久停机阈值、日内暂停会改变后续交易路径。因此减少风险预算后的收益变化不一定严格按比例缩放。

## 9. 实施建议

1. 温和继续原版；小仓空头独立记录模拟结果，重点看「加入前后的组合净收益」，不要只看空头自己的PF。
2. 高风险优先采用本次3.5%风险预算作为下一版候选，保持5倍杠杆；不把现有70%停机线改高。
3. 待下一段新增数据进行前向比较，再决定是否启用空头。后续值得研究的是空头退出和分开的风险预算，而不是把多头全量镜像。

**本次只新增离线研究代码、可选研究依赖和报告；运行中的基金、配置、账簿、登录及交易没有改变，也没有部署或推送。**目前站点客户端仍是旧杠杆白名单且没有传方向。若要上线空头，还需将方向持久化到交易意图、仓位快照与对账，适配双向估值/止损/强平；不可只在`buy`请求里添加一个字段。

## 10. 复现与证据

使用独立研究环境，避免给运行中的服务安装新依赖：

```powershell
python -m venv .research-venv
.research-venv/Scripts/python.exe -m pip install -e ".[research,dev]"
.research-venv/Scripts/python.exe tools/btc_long_short_research.py --data <原始4秒数据目录> --out .build/direction_run
.research-venv/Scripts/python.exe tools/btc_long_short_research.py --data <原始4秒数据目录> --out .build/risk_run --risk-sweep
.research-venv/Scripts/python.exe tools/btc_long_short_report.py --direction .build/direction_run --risk .build/risk_run --out .build/report
.research-venv/Scripts/python.exe -m pytest tests/test_long_short_research.py
```

原始数据目录必须提供`BTCUSDT-4s.npy`、`available.npy`、`hours.npy`、`hour_valid.npy`、`days.npy`、`day_valid.npy`，不会自动下载或替换。3.45GB价格文件没有复制到仓库。两组研究包保留完整CSV、JSON、协议、小型交易/日频NPY轨迹和图表；[manifest.json](manifest.json)记录证据与脚本哈希。

- [direction_grid/summary.csv](direction_grid/summary.csv)：26套方向候选和杠杆对照的所有运行。
- [risk_grid/summary.csv](risk_grid/summary.csv)：10套风险预算候选的正常与压力筛选及最终重放。
- [direction_grid/protocol.json](direction_grid/protocol.json)、[risk_grid/protocol.json](risk_grid/protocol.json)：冻结参数、数据范围和筛选口径。
- [研究引擎](../../../../tools/btc_long_short_research.py)、[报告生成器](../../../../tools/btc_long_short_report.py)。

## 11. 外部研究依据

[A Century of Evidence on Trend-Following Investing](https://www.aqr.com/insights/research/journal-article/a-century-of-evidence-on-trend-following-investing)，Hurst、Ooi、Pedersen，2017：为多空趋势跟随提供跨资产历史背景。本轮借此提出「可测试方向」；该论文的多资产、较长周期证据不能证明BTC小时信号有效。本次具体收益与推荐均来自自己的模拟结果。
'''
    (args.out/'REPORT.md').write_text(text,encoding='utf-8')
    manifest={}
    for path in sorted(args.out.rglob('*')):
        if path.is_file() and path.name!='manifest.json':manifest[str(path.relative_to(args.out)).replace('\\','/')]=hashlib.sha256(path.read_bytes()).hexdigest()
    repo=Path(__file__).resolve().parents[1]
    scripts={str(p.relative_to(repo)).replace('\\','/'):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),repo/'tools/btc_long_short_research.py',repo/'tests/test_long_short_research.py']}
    protocol=json.loads((args.direction/'protocol.json').read_text(encoding='utf-8'))
    datapath=Path(protocol['data']);datahash={}
    for name in ['frozen_protocol.json','hours.npy','hour_valid.npy','days.npy','day_valid.npy','available.npy','BTCUSDT-4s.npy']:
        path=datapath/name
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        datahash[name]={'sha256':digest,'bytes':path.stat().st_size}
    (args.out/'manifest.json').write_text(json.dumps(dict(evidence=manifest,scripts=scripts,data=datahash),ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'Published offline evidence: {args.out}',flush=True)

if __name__=='__main__':main()
