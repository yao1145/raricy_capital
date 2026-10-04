# docs/materials/research · 研究材料索引

本目录只放**研究证据与口径说明**，不放产品规则（那在 [GUIDE.md](../../usage/GUIDE.md) 与
[FUND_PLAN_V0.3.md](../../funds/FUND_PLAN_V0.3.md)），也不放代码契约（那在
[USAGE.md](../../usage/USAGE.md)）。读者定位为研究/评审；研究总览与结论见
[INTRODUCTION.md](../../funds/INTRODUCTION.md)。

## 1. 文件清单

| 文件 | 类型 | 内容 |
| --- | --- | --- |
| `evidence.json` | 归档研究数据 | 两条基金的全区间情景、年度独立档、事件窗口原始数值；含 `limitations` 与日频列名 |
| `annual_comparison.json` | 归档研究数据 | 两条基金**共有的 6 个年度独立档**的对照切片 |
| [README.md](README.md) | 说明 | 本文件：索引、证据边界、字段字典、引用规则 |
| [strategy_protocol.md](strategy_protocol.md) | 说明 | 冻结策略协议细则与代码位置 |
| [historical_evidence.md](historical_evidence.md) | 说明 | 完整历史数据表与读表结论 |
| [fund_vs_research_gap.md](fund_vs_research_gap.md) | 说明 | 历史研究口径与 v0.3 基金口径的差异、缺口与待核对项 |
| `../assets/historical_growth.png` | 图 | 总资产与交易桶单位净值轨迹 |
| `../assets/historical_drawdowns.png` | 图 | 日终回撤轨迹与 25% / 70% 停机阈值 |
| `../assets/annual_cohorts.png` | 图 | 各年度独立启动的结果 |
| `../assets/execution_stress.png` | 图 | 正常 vs 费用翻倍 + 额外 8 秒 |

更新证据时应另行保留版本、原始口径与来源哈希，避免覆盖本报告引用的数据。

## 2. 证据边界（先读这一段）

`evidence.json.scope` 的原话是：

> Existing authentic 4-second historical research; not a v0.3 monthly-distribution fund replay
> or live performance.

展开成四条硬边界：

1. **它是策略研究证据，不是基金净值表现。** 历史资金规则是「初始 1000、每月 1000、
   每笔已实现正净利润 90% 复投交易桶、10% 进留存现金、无利息」，**不同于** v0.3 的
   「月度可分配利润 10% 分红 + 5% 申购费 + 10% 紧急费 + 分红负债台账」。
2. **它没有跑过基金级回测。** 没有按 v0.3 规则重算过历史，因此不存在可引用的
   「基金单位净值序列」「分红后复利结果」。
3. **它不是未被触碰的样本外验证。** 参数筛选反复使用过同一段历史数据，存在过拟合。
4. **它不证明容量或未来收益。** 巨大的模拟余额只说明模拟路径；站点成交、强平、
   资金费率、借贷利息与滑点都未验证。

## 3. 数据来源与校验

`evidence.json.source_files` 记录了归档证据的原始来源文件与 SHA-256：

| 来源文件 | SHA-256 |
| --- | --- |
| `artifacts/btc_stability_r90/results.json` | `9d2b18fa0dab87d59692c54832a6cd360e567774cb0570630996f05432c8e53e` |
| `artifacts/btc_stability_r90/protocol.json` | `51788b6885dd74284f9d36994c991492522577d8f5f770db17a617b2619d12c3` |
| `artifacts/btc_highrisk_funds_2026-10-03_final_r90/results.json` | `dcda07b7d48f7bfc03da1e6d456581271f54519b71321d738b1768ff867f9667` |
| `artifacts/btc_highrisk_funds_2026-10-03_final_r90/protocol.json` | `14f20cb83e4a4b6ff47926179e3623ba5bec3a3f86875723949074dc5e4df28d` |
| `artifacts/btc_stability_r90/G12_normal_daily.npy` | `6a02b9563f0171041c58390326deb422a433804b948251ba926a1d9f53aabe3e` |
| `artifacts/btc_stability_r90/G12_combined_daily.npy` | `62ce49838c6402a0548e399b332c6cfb7364863e2ddd899bba6500932fc18bd2` |
| `artifacts/btc_highrisk_funds_2026-10-03_final_r90/H570_full_normal_daily.npy` | `a269651d5c8629431dfbe4dcf9722c422dfb562bb6228025d5daf32eed68c63a` |
| `artifacts/btc_highrisk_funds_2026-10-03_final_r90/H570_full_stress_daily.npy` | `f74b47327f72a72653bab39e1fb09b602a35966a4706fa593fb0997d8badaf8a` |

原始研究包在旧工程目录里，本仓库**不复制**它们；需要复核时按上表核对哈希。

**注意**：H570 暂存的就是 `btc_highrisk_funds_2026-10-03_final_r90` 研究包，它的正常情景全区间
净利润 `112483490.5353` 与 [FUND_PLAN_V0.3.md](../../funds/FUND_PLAN_V0.3.md) 附录一致。
本目录的表格与配图统一引用该最终版本，精确值见 `evidence.json`。

## 4. 结构速查

```
evidence.json
├── scope / cashflow / dates / source_files / daily_columns / limitations
├── capital1
│   ├── code = "G12"
│   ├── full[]     4 个全区间情景：normal / fee_double / delay8 / combined
│   ├── cohorts[]  9 个年度独立档：y2018 … y2026（annual_fresh）
│   └── events[]   事件窗口，分 context = continuous | warm90，情景 normal / fee_double /
│                  delay8 / combined
└── capital2
    ├── code = "H570"；plan = 研究计划记录（大量开关，多数关闭）
    └── results[]  part = development | review | full(normal/stress) | cohort2018 |
                   cohort2020 | cohort2021 | cohort2022 | cohort2024 | cohort2025
```

几个**容易踩的结构事实**：

- `capital2` **没有** `events` 数组。事件研究只存在于 G12 侧；**不要**据此臆造 H570 的事件统计。
- `capital2.results` 里 `development`（2017-01-01→2023-01-01）、`review`（2023-01-01→2026-10-01）、
  `full`、各 `cohort` 是**彼此独立的运行**，不能相乘、不能首尾相接。
- `capital1.cohorts` 的 `y2026` 出资 9000（8 次月投），是**不完整年度**。
- `capital1.full` 各行的 `monthly_deposits = 116`，与 `initial=1000` 合计 117000。

## 5. 字段字典（引用时按此列名）

金额统一是**小鱼干**；比率字段是**百分数**（例如 `26.6995` 表示 26.6995%）。

| 字段 | 含义 | 备注 |
| --- | --- | --- |
| `contributions` | 累计外部出资 | 全区间 117000；年度独立档 12000；`y2026` 9000 |
| `trading_bucket` | 交易桶资产 | 策略盈亏发生地 |
| `retained_cash` | 留存现金桶 | 历史规则是「每笔正净利润 10%」，**不是** v0.3 月度分红 |
| `total_assets` | 两桶合计 | 含全部出资，规模数字 |
| `net_profit` | `total_assets − contributions` | 不含外部出资；事件行内等于期末−期初−期内新增出资 |
| `profit_over_contributions_pct` | 净利润 ÷ 累计出资 | **不是**年化，也不是收益率；受出资时点影响 |
| `trading_unit_nav` / `unit_nav_cagr_pct` | 交易桶单位净值 / 其 CAGR | 剔除外部流量；**仅 capital1 提供 `unit_nav_cagr_pct`** |
| `money_weighted_return_pct` | 资金加权年化（XIRR 口径） | 两条基金都有，可跨情景比较 |
| `close_unit_nav_drawdown_pct` | 4秒收盘交易桶单位净值最大回撤 | 4秒收盘口径 |
| `intrabar_unit_nav_drawdown_bound_pct` | 盘中保守回撤上界 | **引用「回撤上界」用这一列** |
| `worst_1d/7d/30d/90d/365d_return_pct` | 最差滚动区间收益 | capital1 五项齐全；capital2 只有 `worst_30d_return_pct` |
| `trades` / `win_rate_pct` / `profit_factor` | 交易数 / 胜率 / PF | PF = 总盈利 ÷ 总亏损 |
| `gross_winning_profit` / `gross_losing_amount` | 总盈利 / 总亏损 | **仅 capital1 提供** |
| `fees` | 累计手续费 | 口径见 `limitations` |
| `exposure_time_pct` | 有持仓的时间占比 | |
| `average_nominal_over_equity` | 平均名义仓位 ÷ 交易桶权益 | 远低于表内 2 倍 / 5 倍上限 |
| `active_months` / `negative_months` / `negative_month_pct` / `worst_month_pct` | 活跃月数 / 亏损月数及占比 / 最差月 | **仅 capital1 提供** |
| `longest_underwater_days_active` / `no_new_high_at_end` | 最长水下天数 / 期末未创新高 | **仅 capital1 提供** |
| `halt_time` / `liquidations` / `rate_rejections` | 永久停机时点 / 强平 / 费率拒绝 | 全区间 `null` / `0` / `0` |
| `daily_pauses` | 日内暂停次数 | **仅 capital2 提供** |
| `mean_mae_pct` / `mean_mfe_pct` / `normalized_profit_factor` | 平均最大不利/有利偏移、归一化 PF | **仅 capital2 提供** |
| `net_profit`（`daily_columns`） | 日频列的**最后一列** = `total_assets − contributions` | 资产变动剔除新增出资后的部分；**不是** halt 标记 |

`daily_columns` 声明的日频列顺序为：
`timestamp_ms, trading_bucket, retained_cash, contributions, trading_unit_nav,
close_drawdown_pct, total_assets, net_profit`。四张图就是基于这些列画的；
最后一列是 `net_profit`，**没有** `halted_flag`。

## 6. 引用规则

引用本目录的数字时，请同时说清**基金、情景、口径、区间**，例如：

> capital1（G12）组合压力情景，全区间 2017-08-18 至 2026-10-01（不含终点），
> 盘中回撤上界 24.4867%（`intrabar_unit_nav_drawdown_bound_pct`）。

不要做的四件事：

1. 不要用 `total_assets ÷ 初始出资` 再开方当作年化；
2. 不要把 `retained_cash` 说成「分红」或「投资人现金」；
3. 不要把年度独立档的数字说成连续账户的年度收益；
4. 不要把没有的字段（H570 的事件、H570 的负月占比、G12 的日均暂停次数）推算出来。
