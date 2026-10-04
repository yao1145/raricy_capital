"""Pure v0.3 fund strategy rules: closed hourly signal, sizing and risk gates.

The hourly indicators and the settlement formula are deliberately reused from
the frozen BTC research implementation
(:mod:`raricy_capital.hourly_signals`) so a live fund trade is evaluated with
exactly the researched protocol. Nothing in this module performs I/O or mutates
caller state.

Money is handled in integer 1e-4 units (:data:`MONEY_SCALE`) everywhere; the
ratio helpers use :class:`~decimal.Decimal` and never accumulate floats.
"""
from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from .contracts import FundError, FundPolicy
from .hourly_signals import (  # frozen research inputs, reused on purpose
    DAY,
    FEE,
    HOUR,
    HourSignal,
    closed_signal,
    payout_units,
)

__all__ = [
    'DAY', 'HOUR', 'FEE', 'HourSignal', 'closed_signal', 'payout_units',
    'ENTRY_WINDOW_MS', 'beijing_day', 'iso_utc_ms', 'stop_price',
    'entry_quantity_units', 'nav_units_after_flow', 'signal_is_fresh',
    'cooldown_ready', 'loss_gate_allows', 'position_value_units',
]

#: A freshly closed hour may only be acted on inside this window; later ticks
#: wait for the next hour so a mid-session login never chases an old signal.
ENTRY_WINDOW_MS = 15000

_BEIJING_OFFSET_MS = 8 * HOUR


def beijing_day(now_ms: int) -> int:
    """Beijing wall-clock day number used for the daily loss pause reset."""
    return (int(now_ms) + _BEIJING_OFFSET_MS) // DAY


def iso_utc_ms(text: object) -> int:
    """Parse a real-UTC ISO-8601 site timestamp (``opened_at`` etc.).

    The trial's fish-flow timestamps are UTC+8 wall clock mislabelled ``Z`` and
    must not be routed here; those are handled by the ledger, not the trader.
    """
    dt = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise FundError('invalid_time')
    return int(dt.timestamp() * 1000)


def stop_price(entry: float, distance: float) -> float:
    """Fixed initial stop: entry minus the 2*ATR/close fraction."""
    if not 0.0 < distance < 1.0:
        raise FundError('invalid_distance')
    return entry * (1.0 - distance)


def entry_quantity_units(*, available_cash_units: int, policy: FundPolicy,
                         distance: float, fee_rate: float,
                         min_stake_units: int) -> int:
    """Stake in 1e-4 units for one entry, or ``0`` when it is below the minimum.

    The notional fraction is bounded by both the per-trade risk budget and the
    nominal cap (``q = min(cap, risk / (distance + fee*(1-distance)))``); the
    stake is then ``q`` of the *available* trading cash divided by leverage.
    """
    if not 0.0 < distance < 1.0:
        raise FundError('invalid_distance')
    if available_cash_units <= 0:
        return 0
    q = min(policy.cap, policy.risk / (distance + fee_rate * (1.0 - distance)))
    if not q > 0:
        return 0
    amount = (Decimal(int(available_cash_units)) * Decimal(str(q))
              / Decimal(policy.leverage)).to_integral_value(rounding=ROUND_FLOOR)
    amount = int(amount)
    if amount < max(0, int(min_stake_units)):
        return 0
    return amount


def nav_units_after_flow(units: Decimal | str, equity_units: int,
                         delta_flow_units: int) -> Decimal:
    """Rescale the trading unit count so an external cash flow leaves NAV flat.

    ``units`` is the number of trading-bucket units; NAV is ``equity / units``.
    When an external flow of ``delta_flow_units`` (subscription, redemption,
    monthly distribution, emergency fee kept in the fund) lands, the unit count
    is scaled by ``equity / (equity - flow)`` so that NAV only ever moves on
    trading P&L. Trading P&L itself is *not* a flow and must not be passed in.

    A unit count of zero means no trading baseline has been established yet
    (e.g. the first issued shares); the first positive equity sets it directly
    so NAV starts at 1 instead of inheriting an unallocated wallet's cash.
    """
    units = Decimal(units)
    delta = int(delta_flow_units)
    if int(equity_units) <= 0:
        return units
    if units <= 0:
        return Decimal(int(equity_units))
    if delta == 0:
        return units
    before = int(equity_units) - delta
    if before <= 0:
        # A brand-new or wiped-out bucket: rebase so NAV restarts from 1.
        return Decimal(int(equity_units))
    return units * Decimal(int(equity_units)) / Decimal(before)


def signal_is_fresh(signal: HourSignal | None, now_ms: int) -> bool:
    """True only for the hour that closed immediately before ``now_ms``."""
    return bool(signal) and signal.end_ms == (int(now_ms) // HOUR) * HOUR


def cooldown_ready(exit_signal_ms: int | None, now_ms: int,
                   policy: FundPolicy) -> bool:
    """Exit cooldown: the next full hour after the exit, then 4 more hours."""
    if exit_signal_ms is None:
        return True
    ready_at = (int(exit_signal_ms) // HOUR + 1) * HOUR + policy.cooldown_hours * HOUR
    return int(now_ms) >= ready_at


def loss_gate_allows(policy: FundPolicy, signal: HourSignal,
                     ms_since_loss: int | None, last_loss: bool) -> bool:
    """capital1 ER12 gate: after a losing close, 12h need ER>=0.30 and net>0."""
    if not last_loss or not policy.loss_gate_hours or ms_since_loss is None:
        return True
    if ms_since_loss >= policy.loss_gate_hours * HOUR:
        return True
    return signal.er >= 0.30 and signal.change > 0


def position_value_units(stake_units: int, entry: float, price: float,
                         leverage: int, fee_rate: float = FEE) -> int:
    """Authoritative mark-to-market value in 1e-4 units via the site formula."""
    if stake_units <= 0:
        return 0
    return payout_units(int(stake_units) / 10000.0, entry, price, leverage, fee_rate)
