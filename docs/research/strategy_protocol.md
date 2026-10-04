# 冻结策略协议（v0.3）

本文描述 `src/raricy_capital/` 里**实际运行**的策略与风控规则。权威顺序是：

1. `src/raricy_capital/strategy.py` 与 `src/raricy_capital/hourly_signals.py`（纯函数规则）；
2. `src/raricy_capital/contracts.py` 的 `FundPolicy` / `POLICIES`（冻结参数）；
3. `src/raricy_capital/trader.py`（运行时闸门与状态机）；
4. 研究计划记录（`evidence.json.capital2.plan`）——**只在实现里存在对应字段时才有效**。

任何参数改动都要走 [USAGE.md](../USAGE.md) §9 的「研究 → 冻结 → 实现 → 文档」流程，
配置里写死策略会被 `policies_are_frozen` 拦下。

## 1. 数据与指标

| 项目 | 值 | 位置（文件 / 符号） |
| --- | --- | --- |
| 执行节奏 | 4 秒一个 tick | `trader.py` `_TICK_SECONDS` |
| 信号周期 | **已收盘小时 K 线** | `hourly_signals.py` `closed_signal` |
| 最短历史 | 206 根已收盘小时线 | `hourly_signals.py` `closed_signal`（不足时抛 `insufficient_closed_hours`） |
| EMA | 96 小时，前 96 根均值作种子，其后 `2/97·c + 95/97·EMA` | `hourly_signals.py` `closed_signal` |
| ATR | 14 小时，前 14 根均值作种子，其后 `(13·ATR + TR)/14` | `hourly_signals.py` `closed_signal` |
| 趋势 | `close > EMA96` **且** `EMA96 > 24 小时前的 EMA96` | `hourly_signals.py` `closed_signal`（`HourSignal.enter`） |
| 止损距离 | `distance = 2·ATR/close` | `hourly_signals.py` `closed_signal`（`HourSignal.distance`） |
| 仓位分母手续费 | `FEE = 0.0002` | `hourly_signals.py` `FEE` |
| 信号新鲜度 | 只在小时收盘后 **15 秒**内可开仓 | `strategy.py` `ENTRY_WINDOW_MS`、`signal_is_fresh` |

**4 秒数据不参与信号。** 它用于报价、止损比较、结算与账户对账。
小时 K 线缓存只保留已经完整走完的小时（`trader.py` 的交易循环），
所以半小时中途的快照永远不会被误当成该小时的最终收盘。

## 2. 共同交易规则

### 2.1 入场

- 条件：小时收盘价 > EMA96，且 EMA96 高于 24 小时前的 EMA96（趋势向上）。
- 只允许在**刚收盘的那个小时**之后 15 秒内下单（`strategy.py` 的 `signal_is_fresh`
  与 `trader.py` 的入场窗口检查）。**中途登录不会去追一个已经过期的信号。**
- 最多**一个自有仓位**；不加仓、不补仓。存在非自有仓位时只隔离（`untracked_positions`）
  并停止新开仓，绝不代为平仓（`trader.py` 的隔离分支）。

### 2.2 仓位与杠杆

`strategy.py` 的 `entry_quantity_units`：

```
q     = min(cap, risk / (distance + fee × (1 − distance)))      # 名义仓位 / 交易桶权益
stake = ⌊q × min(可用现金, 钱包) / leverage⌋                      # 保证金意义上的投入
```

- `q` 是**名义仓位比例**，`stake × leverage` 才是名义仓位。
- 低于站点最小份额（`minStake`）则不开仓。
- 波动越大 ⇒ `distance` 越大 ⇒ `q` 越小 ⇒ 名义仓位自动收缩。
- 低波动时 `cap` 会先被触及（风险预算被上限截住）。

冻结值（`contracts.py` 的 `POLICIES`，键只有 `capital1` 与 `capital2`）：

| | capital1 | capital2 |
| --- | --- | --- |
| `leverage` | 3 | 5 |
| `risk` | 0.0125 | 0.05 |
| `cap` | 2.0 | 5.0 |

### 2.3 止损与出场

`stop_price = entry × (1 − distance)`（`strategy.py` 的 `stop_price`），即固定 2ATR。
出场原因（`trader.py` 的出场判定，按优先级）：

| `reason` | 触发 |
| --- | --- |
| `drawdown` | 已永久停机 |
| `daily` | 当日已触发日内亏损暂停 |
| `operator_stop` | 操作方停机 |
| `atr_stop` | 价格 ≤ 止损价 |
| `time` | 持仓 ≥ `max_hold_days`（14）天 |
| `trend` | 新鲜小时信号显示趋势失效（`not trend`） |

**没有**止盈、移动止盈或分批减仓。

### 2.4 冷却

`strategy.py` 的 `cooldown_ready`：

```
ready_at = (退出时刻所在小时起点 + 1 小时) + 4 小时
```

即「下一个完整小时」之后再冷却 4 小时。退出发生在整点后不久时实际等待约 4 小时，
接近整点出口时约 5 小时。叠加 15 秒新鲜度窗口后，最早的重新入场落在
`ready_at` 之后的第一个小时边界。

**任何卖出都启动冷却**，包括止损、超时、强平与外部平仓
（`trader.py` 结算时写入 `exit_signal_ms`）。H570 研究计划里的 `cooldown = 240`（分钟）与之一致。

### 2.5 日切、日内暂停、永久停机

`strategy.py` 的 `beijing_day`：

```
day = (now_ms + 8 小时) // 24 小时
```

与 H570 研究计划的 `day_offset = 8.0` 一致。跨日时记录 `day_start = 当前 NAV` 并**解除**日内暂停。

| 机制 | 触发条件 | 后果 | 位置 |
| --- | --- | --- | --- |
| 日内暂停 | `NAV / 当日起始 NAV − 1 ≤ −daily_loss` | 安排退出持仓、当日不开新仓；下一北京日恢复 | `trader.py` 的日内暂停判定；`daily_loss`：capital1 1.5% / capital2 5% |
| 永久停机 | `1 − NAV / 峰值 ≥ drawdown_limit` | 置 `halted`，安排退出，**永不再开仓** | `trader.py` 的停机判定；`drawdown_limit`：capital1 25% / capital2 70% |

- **峰值只升不降**（`trader.py` 的峰值更新）。
- 停机触发会同时写一条持有人通知（`_persist_notice`），并把基金状态镜像成
  `permanent_halt`（`_sync_fund_state`）。
- 停机且无持仓时，运行时进入**只读**模式：继续估值与对账，但绝不评估开仓
  （`trader.py` 的停机只读分支）。

## 3. capital1 的 ER12 亏损门

`strategy.py` 的 `loss_gate_allows`：

```python
def loss_gate_allows(policy, signal, ms_since_loss, last_loss) -> bool:
    if not last_loss or not policy.loss_gate_hours or ms_since_loss is None:
        return True
    if ms_since_loss >= policy.loss_gate_hours * HOUR:
        return True
    return signal.er >= 0.30 and signal.change > 0
```

- **触发**：上一笔已平仓交易亏损（`trader.py` 结算时写入 `last_loss`）。
- **窗口**：从**结算时刻**起 12 小时（`loss_gate_hours = 12`），不是从信号小时起算。
- **要求**：同时满足 `ER12 ≥ 0.30` **且**过去 12 小时净价格变化 > 0。
- **到期**：12 小时之后该限制自动失效；趋势条件、冷却、风险闸门仍然生效。
- **ER12 定义**：`|收盘 − 12 小时前收盘| ÷ Σ|逐小时变化|`（`hourly_signals.py` 的
  `closed_signal`，输出 `HourSignal.er`），区间 0–1。越高表示趋势越连贯；震荡行情里该值偏低。

设计意图：亏过一次之后，只在行情重新走出干净方向时才允许回头，减少震荡里的连续止损。

## 4. capital2（H570）没有这道门

`FundPolicy.loss_gate_hours` 默认 0，`loss_gate_allows` 立即返回 `True`。
研究计划记录里 `gate = "baseline"`，全区间没有 ER 相关字段。所以 H570 亏损后只有 4 小时冷却。

### 4.1 研究计划记录 vs 冻结实现

`evidence.json.capital2.plan` 是一份带很多开关的研究记录。**只有下列字段在实现里有对应**：

| 计划字段 | 值 | 实现位置（文件 / 符号） |
| --- | --- | --- |
| `ema_period` | 96 | `hourly_signals.py` `closed_signal` |
| `slope_hours` | 24 | `hourly_signals.py` `closed_signal`（趋势判定） |
| `atr_period` / `atr_multiplier` | 14 / 2.0 | `hourly_signals.py` `closed_signal` |
| `leverage` / `risk` / `cap` | 5 / 0.05 / 5.0 | `contracts.py` `POLICIES['capital2']` |
| `daily` / `dd` | 0.05 / 0.7 | `contracts.py` `POLICIES['capital2']` |
| `timeout` | 20160 分钟 = 14 天 | `FundPolicy.max_hold_days` |
| `cooldown` | 240 分钟 = 4 小时 | `FundPolicy.cooldown_hours` |
| `day_offset` | 8.0 | `strategy.py` `beijing_day` |
| `fee` | 0.0002 | `hourly_signals.py` `FEE` |

**下列字段在运行时没有实现**，属于研究包内部表示，不构成运行规则：
`kind` / `fast` / `slow`、`breakout_lookback = 48`、`exit_low_lookback = 24`、
`target`、`fixed_q`、`delay`、`bootstrap`、`daily_filter`、`adx_min`、`vol_filter`、
`momentum_filter`、`volume_filter`、`dynamic` / `weak` / `middle` / `strong`、
`trail_mode` / `trail_k`、`protect_trigger` / `protect_lock`、`exit_mode`、
`model` / `band` / `confirmation`、`align_cooldown`、`early_reentry`、
`flow_window` / `flow_threshold` / `flow_residual` / `flow_rebound`、
`extreme_window` / `extreme_ratio`、`rebalance` / `rebalance_mode`、
`threshold` / `recovery_hours`。

同时注意：这些可选的过滤器/动态风险/保护止损开关**全部处于关闭状态**
（`adx_min = 0`、`vol_filter = 0`、`dynamic = "fixed"`、`trail_mode = 0`、
`early_reentry = false`、`rebalance = 0` 等）。**被选中的是最朴素的基础档**，
带过滤器的变体没有入选。

`hourly_signals.py` 现在只带过来 `closed_signal` 与 `payout_units`，原研究模块的
`Plan` / `PLANS` 注册表**没有**保留；本包的运行注册表只有 `contracts.py` 的 `POLICIES`，
键是 `capital1` 与 `capital2`。研究计划记录里的字段即使被记录，也不构成额外的实时信号。

## 5. 运行时闸门（`trader.py`）

| 闸门 | 行为 |
| --- | --- |
| 意图先落盘 | 任何买/卖意图**先持久化**再发外部请求（`trader.py` 的下单路径） |
| 买入幂等 | 只带原幂等键重试；超过 20 秒未确认即置 `unconfirmed_buy` 等对账，不无限重放（`trader.py` 的未确认买入处理） |
| 已停机/已停用不重放未确认买入 | `trader.py` 的重放前置检查 |
| 卖出按仓位 ID 幂等 | 先对账再提交；仓位已消失则按记录结算（`trader.py` 的卖出路径） |
| 非自有仓位隔离 | 只置 `untracked_positions` 并停止开仓，不代平（`trader.py` 的隔离分支） |
| 对账闸门 | `settlement_holds` / `reconciliation_holds` / `valuation_holds` 任一存在则不开仓（`trader.py` 的开仓前置检查） |
| 费率/杠杆校验 | 与冻结值不一致置 `fee_changed` / `site_leverage_unavailable` |
| `live=False`（默认） | **不产生任何外部写**；需要下单的出场会保留为阻塞而不是丢弃（`trader.py` 的 live 闸门） |
| 启动状态 | 新状态以 `stopped` 起步，需显式 `set_running` 才可能开仓（`trader.py` 的状态初始化与 `set_running`） |
| 对账频率 | 快照最多每分钟一次（`trader.py` 的 `_TICK_SECONDS` 与快照节流） |
| 流动性 | `close_for_liquidity` 只排队自有仓位的退出，不足时**如实报告缺口**而不是隐藏（`trader.py` `close_for_liquidity`） |

## 6. 资金流归一：为什么「加钱」不能解除停机

`strategy.py` 的 `nav_units_after_flow`：

```
units' = units × equity / (equity − flow)
```

- 外部流量（种子出资、已发行申购本金、赎回、月度分红、紧急费留存）落地时**只缩放单位数**，
  让交易桶单位净值**只随交易盈亏变动**；交易盈亏本身不是流量，不会被归一。
- 因此**新增存款不改变 NAV、也不改变峰值与回撤基线**，停机判定与日内暂停判定都不受影响。
- `halted` 是**独立布尔状态**，没有任何代码路径会因为资金流把它清掉；
  `set_running(True)` 在停机状态下直接抛 `permanent_halt`（`trader.py` 的 `set_running`）。
- 唯一的例外是「桶被清零」：当 `equity − flow ≤ 0` 时单位数重置，
  使 NAV 从 1 重新起算（`strategy.py` `nav_units_after_flow` 的清零分支）。**但停机标记依然保留**，
  所以即使净值重新从 1 起算，基金**仍然不会**重新开仓。
- 已发行份额为 0 的钱包视为**未分配资金**，不会给 NAV 播种
  （`trader.py` 的资金流落地与份额分配路径）。

## 7. 持仓生命周期（状态机）

```
stopped ──set_running(true)──▶ flat ──入场信号──▶ holding
   ▲                            │   ▲                │
   │                            │   │                ├── 止损/超时/趋势失效/暂停/停机 ──▶ flat（进入冷却）
   └──set_running(false)────────┘   │                │
                                    └── 冷却 + 入场信号┘
flat ──回撤达阈值──▶ halted（只读估值，永不再开仓）
flat/holding ──日内亏损达线──▶ daily_pause（当日不开新仓，次日恢复）
任意状态 ──确认不匹配/费率变化/对账未完成──▶ blocked（停止新开仓，等待人工处理）
```

状态可见于 `trader.py` 的 `_fund_status`：`phase` 取
`halted / stopped / daily_pause / blocked / holding / flat`。

## 8. 与研究口径的关系

本协议与历史研究**共用同一套规则**（`strategy.py` 与 `hourly_signals.py` 刻意复用冻结实现），
但**资金规则不同**：历史模拟是「每笔正净利润 90% 复投 / 10% 留存现金」，
v0.3 是「月度可分配利润 10% 分红 + 5% 申购费 + 10% 紧急费 + 负债台账」。
差异清单与缺口见 [fund_vs_research_gap.md](fund_vs_research_gap.md)。
