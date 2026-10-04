"""闭环小时信号与结算公式（冻结的研究口径，原样复用）。

只保留基金交易真正需要的那部分：小时/日常数、:class:`HourSignal`、
:func:`closed_signal` 与 :func:`payout_units`。原研究模块里的三条试运行方案
（``Plan`` / ``PLANS``）属于旧机器人试运行，本包用
:class:`raricy_capital.contracts.FundPolicy`，因此没有带过来。

数值行为必须与提取前逐字一致：EMA96 / ATR14 / 24h 斜率、2*ATR 距离、以及站点
结算公式 ``payout_units``。任何改动都等于改动已研究的策略，不属于本次拆分。
"""

from __future__ import annotations
from dataclasses import dataclass
import math

HOUR = 3600000
DAY = 86400000
FEE = .0002


@dataclass(frozen=True)
class HourSignal:
    end_ms: int
    enter: bool
    exit: bool
    distance: float
    er: float
    change: float


def closed_signal(candles: list[list[float]], now_ms: int) -> HourSignal:
    closed = [r for r in candles if r[0] + HOUR <= now_ms]
    if len(closed) < 206:
        raise ValueError('insufficient_closed_hours')
    for i, r in enumerate(closed):
        if len(r) != 6 or not all(math.isfinite(float(x)) for x in r):
            raise ValueError('invalid_candle')
        if r[0] % HOUR or (i and r[0] - closed[i - 1][0] != HOUR):
            raise ValueError('hour_gap')
        if min(r[1:5]) <= 0 or r[2] < max(r[1], r[4], r[3]) or r[3] > min(r[1], r[4]):
            raise ValueError('invalid_ohlc')
    ema = []
    value = 0.
    atr = 0.
    for i, r in enumerate(closed):
        close = r[4]
        if i < 96:
            value += close
        if i == 95:
            value /= 96
        elif i >= 96:
            value = 2 / 97 * close + 95 / 97 * value
        ema.append(value if i >= 95 else math.nan)
        tr = r[2] - r[3] if i == 0 else max(r[2] - r[3], abs(r[2] - closed[i - 1][4]), abs(r[3] - closed[i - 1][4]))
        if i < 14:
            atr += tr / 14
        else:
            atr = (atr * 13 + tr) / 14
    close = closed[-1][4]
    trend = close > ema[-1] and ema[-1] > ema[-25]
    change = close - closed[-13][4]
    path = sum(abs(closed[i][4] - closed[i - 1][4]) for i in range(len(closed) - 12, len(closed)))
    return HourSignal(int(closed[-1][0]) + HOUR, trend, not trend, 2 * atr / close,
                      abs(change) / path if path else 0., change)


def payout_units(stake: float, entry: float, price: float, leverage: int, fee: float = FEE) -> int:
    n = round(stake * 10000)
    ratio = price / entry
    return max(0, math.floor(n + n * leverage * (ratio - 1) - n * leverage * ratio * fee))


__all__ = ['DAY', 'FEE', 'HOUR', 'HourSignal', 'closed_signal', 'payout_units']
