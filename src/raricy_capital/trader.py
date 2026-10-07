"""Shared live trader for both v0.3 funds (capital1 ER12 / capital2).

One :class:`FundTrader` drives every fund in ``policies`` against its own
``FundSiteClient`` and the shared :class:`FundLedger`. Per four-second tick it:

* finishes any persisted order intent first, then reconciles the authoritative
  account snapshot (balance + positions) at most once a minute;
* takes a fresh quote, marks the ledger (:meth:`FundLedger.mark_account`) with
  the authoritative wallet and the site settlement formula, and freezes the
  NAV peak whenever a mark fails;
* normalises trading ``units`` for external flows (subscriptions, redemptions,
  monthly distributions and emergency fees kept in the fund) so the trading NAV
  and the drawdown/daily-loss baselines only move on trading P&L;
* applies the frozen v0.3 rules: closed-hour EMA96/ATR14/rising-24 signal, 2*ATR
  stop, 14-day hold, one owned position, next-full-hour + 4h cooldown, Beijing
  daily pause, permanent 25%/70% drawdown halt and the capital1 12h ER12 gate.

Safety contract
---------------
* Every buy/sell intent is written to the store *before* the external request.
* A buy is retried only with its original idempotency key; after 20s an
  unanswered buy is left pending and new opens stop until it is reconciled. A
  halted or operator-disabled fund never replays an uncertain buy to open.
* A sell is idempotent by position id upstream, so re-submitting an exit only
  replays the recorded settlement and can never open a second one. The trader
  still reconciles against the authoritative snapshot first: a lot confirmed
  still open gets its exit submitted, a lot already gone is settled from that
  replay. Reconciliation keeps ownership authoritative, not the double-sell
  guard, which upstream already provides.
* Manual/foreign positions are quarantined, never flattened: they only set
  ``blocked='untracked_positions'`` and suppress new opens.
* ``live=False`` (the default) performs no external write at all; exits that
  need an order stay persisted and blocked rather than being dropped.
* New trader state starts ``stopped``: nothing opens until the operator (or the
  central root) explicitly enables the fund through :meth:`FundTrader.set_running`.

Ledger contract consumed here (see ``contracts.py`` and ``ledger.py``):
``status(fund_id)`` must expose ``equity_units`` (net of liabilities),
``available_cash_units`` (excluding pending receipts, institution fees and
confirmed liabilities), ``capital_flows_units`` and optionally
``non_trading_income_units`` (emergency fees kept in the fund).
``mark_account(fund_id, wallet_units, position_value_units, now_ms, quote_ms)``
returns the updated status dict; ``trade_realized(fund_id, trade_id, pnl_units,
now_ms)`` is idempotent by ``trade_id`` and receives P&L already net of fees.
"""
from __future__ import annotations

import asyncio
import secrets
import uuid
from decimal import Decimal

from .contracts import FundError, MONEY_SCALE, POLICIES, money_text, money_units
from . import strategy as S
from .hourly_signals import DAY, FEE, HOUR

NS_STATE = 'fund_trader'
NS_CANDLES = 'fund_trader_candles'

SNAPSHOT_INTERVAL_MS = 60000
BUY_CONFIRM_TIMEOUT_MS = 20000
CANDLE_MAX_AGE_MS = 300000

_TICK_SECONDS = 4.0


def _dtext(value: Decimal) -> str:
    return str(value.quantize(Decimal('0.00000001')))


def _default_state(fund_id: str) -> dict:
    return {
        'fund_id': fund_id,
        'units': None,            # Decimal string, set on the first good mark
        'flow_basis_units': 0,    # excluded flows already folded into units
        'nav': None,
        'peak': None,
        'day': None,
        'day_start': None,
        'paused': False,
        'halted': False,
        'stopped': True,
        'halt_reason': None,
        'stop_reason': None,
        'position': None,
        'pending': None,
        'blocked': None,
        'last_exit_ms': None,
        'exit_signal_ms': None,
        'last_loss': False,
        'last_settled_id': None,
        'completed_trades': 0,
        'last_mark_ms': None,
        'quote_ms': None,
        'last_snapshot_ms': None,
        'mark_failed': False,
        'last_error': None,
    }


class FundTrader:
    """Frozen v0.3 strategy runtime shared by both funds."""

    def __init__(self, store, ledger, clients, policies=POLICIES, *, live: bool = False,
                 snapshot_interval_ms: int = SNAPSHOT_INTERVAL_MS):
        if not isinstance(clients, dict):
            raise FundError('clients_mapping_required')
        self.store = store
        self.ledger = ledger
        self.clients = dict(clients)
        self.policies = dict(policies)
        self.live = bool(live)
        self.snapshot_interval_ms = int(snapshot_interval_ms)
        self._locks = {fund_id: asyncio.Lock() for fund_id in self.policies}
        self._snapshots: dict[str, tuple[int, dict]] = {}

    # ------------------------------------------------------------------ state
    def _load(self, fund_id: str) -> dict:
        state = self.store.get(NS_STATE, fund_id, None)
        if not isinstance(state, dict):
            return _default_state(fund_id)
        merged = _default_state(fund_id)
        merged.update(state)
        merged['fund_id'] = fund_id
        return merged

    def _save(self, fund_id: str, state: dict) -> None:
        self.store.put(NS_STATE, fund_id, state)

    def _event(self, fund_id: str, event: str, details: dict | None = None,
               now_ms: int | None = None, level: str = 'info') -> None:
        try:
            self.store.append_event(event, fund_id=fund_id, level=level,
                                    details=details or {}, created_ms=now_ms)
        except Exception:  # audit must never break trading
            pass

    def _mark_error(self, fund_id: str, state: dict, exc: BaseException,
                    now_ms: int, code: str | None = None) -> None:
        # Only the exception category is recorded; upstream bodies/credentials
        # are never echoed into state or events.
        raw = code or getattr(exc, 'code', None)
        code = raw if isinstance(raw, str) and raw else type(exc).__name__
        state['mark_failed'] = True
        state['last_error'] = code
        self._save(fund_id, state)
        self._event(fund_id, 'mark_failed', {'code': code}, now_ms, 'warning')

    # ------------------------------------------------------------------- tick
    async def tick(self, now_ms: int) -> dict:
        for fund_id in list(self.policies):
            if self.clients.get(fund_id) is None:
                continue
            try:
                await self._tick_fund(fund_id, now_ms)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let one fund kill the loop
                state = self._load(fund_id)
                self._mark_error(fund_id, state, exc, now_ms, 'tick_failed')
        return self.public_status()

    async def _tick_fund(self, fund_id: str, now_ms: int) -> None:
        async with self._locks[fund_id]:
            client = self.clients.get(fund_id)
            if client is None:
                return
            state = self._load(fund_id)
            if state['pending']:
                try:
                    await self._resume_pending(fund_id, client, state, now_ms)
                except Exception as exc:
                    self._mark_error(fund_id, state, exc, now_ms, 'pending_failed')
                return
            # A permanently halted fund with no owned lot stays read-only: it never
            # opens again, but the authoritative balance/quote mark must keep
            # flowing so holders and settlements still see a valid valuation.
            read_only = bool(state['halted'] and not state['position'])
            try:
                await self._cycle(fund_id, client, state, now_ms, read_only=read_only)
            except Exception as exc:
                self._mark_error(fund_id, state, exc, now_ms)

    # ---------------------------------------------------------------- account
    async def _snapshot(self, client, fund_id: str, now_ms: int, *, force: bool = False) -> dict:
        cached = self._snapshots.get(fund_id)
        if not force and cached and now_ms - cached[0] < self.snapshot_interval_ms:
            return cached[1]
        snapshot = await client.snapshot()
        if not isinstance(snapshot, dict):
            raise FundError('invalid_snapshot')
        self._snapshots[fund_id] = (now_ms, snapshot)
        return snapshot

    @staticmethod
    def _position_ids(snapshot: dict) -> list[str]:
        rows = snapshot.get('positions')
        if not isinstance(rows, list):
            raise FundError('invalid_snapshot')
        ids = []
        for row in rows:
            if not isinstance(row, dict) or not row.get('id'):
                raise FundError('invalid_position_snapshot')
            ids.append(str(row['id']))
        return ids

    async def _cycle(self, fund_id: str, client, state: dict, now_ms: int,
                     *, read_only: bool = False) -> None:
        policy = self.policies[fund_id]
        snapshot = await self._snapshot(client, fund_id, now_ms)
        snapshot_ms = self._snapshots.get(fund_id, (now_ms, {}))[0]
        owned = state['position']
        owned_id = owned['id'] if owned else None
        ids = self._position_ids(snapshot)

        foreign = [pid for pid in ids if pid != owned_id]
        if foreign:
            # Never flatten positions we do not own; quarantine instead.
            state['blocked'] = 'untracked_positions'
            self._event(fund_id, 'untracked_positions', {'count': len(foreign)}, now_ms, 'warning')

        if (owned and owned_id not in ids
                and snapshot_ms > owned.get('confirmed_ms', 0)):
            await self._settle_external(fund_id, client, state, owned_id, now_ms)
            owned = state['position']
            owned_id = owned['id'] if owned else None

        fee_rate = snapshot.get('feeRate')
        if fee_rate is not None and abs(float(fee_rate) - FEE) > 1e-12:
            state['blocked'] = 'fee_changed'
            self._event(fund_id, 'fee_changed', {'fee_rate': float(fee_rate)}, now_ms, 'warning')
        options = snapshot.get('leverageOptions')
        # 空列表或缺失 = 站点不再下发杠杆白名单（2026-10-07 起为 1–100 整数），
        # 不是「无杠杆可用」；只有站点真的给出非空白名单时才按它拦。
        if snapshot.get('leverageEnabled') is False or (
                isinstance(options, list) and options and policy.leverage not in options):
            state['blocked'] = 'site_leverage_unavailable'
            self._event(fund_id, 'site_leverage_unavailable', None, now_ms, 'warning')
        raw_min = snapshot.get('minStake')
        min_stake_units = money_units(str(raw_min), positive=True) if raw_min is not None else MONEY_SCALE

        price, fee = await client.quote()
        price = float(price)
        fee = float(fee)
        if not price > 0 or price != price or price in (float('inf'), float('-inf')):
            raise FundError('invalid_price')
        if abs(fee - FEE) > 1e-12:
            state['blocked'] = 'fee_changed'
            self._event(fund_id, 'fee_changed', {'fee_rate': fee}, now_ms, 'warning')

        # The quote carries the instant it was actually observed. The mark's
        # valuation time must never be older than that observation, or an early
        # month-end tick whose price only arrived later would look like a
        # pre-cutoff valuation (look-ahead).
        quote_ms = self._quote_ms(client, now_ms)
        mark_ms = max(now_ms, quote_ms)

        wallet_units = int(await client.balance())
        mark_ms = max(mark_ms, int(getattr(client, 'balance_ms', 0) or 0))
        if owned:
            position_value = S.position_value_units(owned['stake_units'], owned['entry'],
                                                    price, owned['leverage'], fee)
        else:
            position_value = 0

        mark = self.ledger.mark_account(fund_id, wallet_units, int(position_value), mark_ms, quote_ms)
        if not isinstance(mark, dict) or 'equity_units' not in mark:
            mark = self.ledger.status(fund_id)
        equity_units = int(mark['equity_units'])
        available_cash_units = int(mark['available_cash_units'])
        excluded = int(mark.get('capital_flows_units', 0)) + int(mark.get('non_trading_income_units', 0))
        shares_atoms = mark.get('shares_atoms')
        try:
            allocated = shares_atoms is None or int(shares_atoms) > 0
        except (TypeError, ValueError):
            allocated = True
        reconciling = self.store.get('reconciliation_holds', fund_id, {}).get('hold', False)
        if not reconciling and not state['blocked']:
            self._normalize(state, equity_units, excluded, min_stake_units, allocated=allocated)
            self._risk_limits(fund_id, state, policy, now_ms)

        signal = None
        signal_error = None
        if not read_only:
            try:
                signal = await self._signal(client, fund_id, state, now_ms)
            except Exception as exc:
                signal_error = type(exc).__name__

        state['mark_failed'] = False
        state['last_error'] = signal_error
        state['last_mark_ms'] = mark_ms
        state['quote_ms'] = quote_ms
        state['last_snapshot_ms'] = snapshot_ms

        if read_only:
            # Halted and flat: never opens, but keeps valuing for holders.
            self._save(fund_id, state)
            return
        intent = self._decide(fund_id, state, policy, signal, price, fee,
                              wallet_units, available_cash_units, min_stake_units, now_ms)
        if intent is not None:
            await self._execute(fund_id, client, state, intent, now_ms)
        else:
            self._save(fund_id, state)

    def _normalize(self, state: dict, equity_units: int, excluded_units: int,
                   min_stake_units: int, *, allocated: bool = True) -> None:
        if not allocated:
            # A wallet with no issued shares is unallocated money, not fund
            # trading assets: it must not seed NAV, peak or the drawdown
            # baseline. Only the flow basis is recorded so later external flows
            # stay neutral once the first shares are issued.
            state['flow_basis_units'] = excluded_units
            return
        if state['units'] is None or Decimal(state['units']) <= 0:
            # Zero -> first issued shares: establish the baseline from the
            # now-allocated equity. _risk_limits only ever raises the peak, so
            # an established peak survives this initialization.
            units = Decimal(equity_units) if equity_units > 0 else Decimal(str(min_stake_units))
            state['flow_basis_units'] = excluded_units
        else:
            delta = excluded_units - int(state['flow_basis_units'])
            units = S.nav_units_after_flow(state['units'], equity_units, delta)
            state['flow_basis_units'] = excluded_units
        units = max(units, Decimal('0'))
        state['units'] = _dtext(units) if units > 0 else '0'
        nav = Decimal(equity_units) / units if units > 0 else Decimal('0')
        state['nav'] = _dtext(nav)

    def _risk_limits(self, fund_id: str, state: dict, policy, now_ms: int) -> None:
        nav = Decimal(state['nav'] or '0')
        peak = Decimal(state['peak']) if state['peak'] is not None else nav
        if state['peak'] is None:
            state['peak'] = _dtext(nav)
        elif nav > peak:
            state['peak'] = _dtext(nav)
        peak = Decimal(state['peak'])

        today = S.beijing_day(now_ms)
        if state['day'] != today:
            state['day'] = today
            state['day_start'] = _dtext(nav)
            state['paused'] = False
        if state['day_start'] is None:
            state['day_start'] = _dtext(nav)

        if peak > 0 and (Decimal('1') - nav / peak) >= Decimal(str(policy.drawdown_limit)):
            if not state['halted']:
                state['halted'] = True
                state['halt_reason'] = 'drawdown'
                self._event(fund_id, 'permanent_halt', {'reason': 'drawdown'}, now_ms, 'error')
                self._persist_notice(fund_id,
                    f'{policy.label} 触发永久回撤停机（回撤达到 {policy.drawdown_limit:.0%}），'
                    f'停止新开仓；已有份额仍按权威净值结算。', now_ms)
            else:
                state['halted'] = True
        day_start = Decimal(state['day_start'])
        if day_start > 0 and (nav / day_start - Decimal('1')) <= -Decimal(str(policy.daily_loss)):
            if not state['paused']:
                state['paused'] = True
                self._event(fund_id, 'daily_pause', {'reason': 'daily_loss'}, now_ms, 'warning')
                self._persist_notice(fund_id,
                    f'{policy.label} 当日亏损达到 {policy.daily_loss:.1%}，今日暂停新开仓，'
                    f'下一交易日恢复。', now_ms)
            else:
                state['paused'] = True
        # Mirror the risk phase into the ledger's fund record (its ``state`` key
        # only) so holders and settlements observe halt/pause without any other
        # accounting field being reset.
        self._sync_fund_state(fund_id, state, now_ms)

    def _quote_ms(self, client, now_ms: int) -> int:
        """The instant the client actually observed the quote, or the tick time."""
        value = getattr(client, 'quote_ms', None)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return now_ms
        return value

    def _persist_notice(self, fund_id: str, content: str, now_ms: int) -> None:
        # Ledger notices are the canonical holder inbox; fall back to the same
        # store shape so a halt/pause notice is never lost if the ledger does
        # not expose it.
        try:
            notify = getattr(self.ledger, 'notify_holders', None) or getattr(
                self.ledger, '_notify', None)
            if callable(notify):
                notify(fund_id, None, content, now_ms)
                return
        except Exception:
            pass
        try:
            notice_id = uuid.uuid4().hex[:16]
            self.store.put('notices', notice_id, {
                'id': notice_id, 'fund_id': fund_id, 'user_id': None,
                'content': content, 'created_ms': now_ms, 'status': 'queued'})
            self._event(fund_id, 'notice', {'notice_id': notice_id}, now_ms)
        except Exception:
            pass

    def _sync_fund_state(self, fund_id: str, state: dict, now_ms: int) -> None:
        target = 'permanent_halt' if state['halted'] else 'paused' if state['paused'] else 'active'
        try:
            setter = getattr(self.ledger, 'set_fund_state', None)
            if callable(setter):
                setter(fund_id, target, now_ms)
                return
        except Exception:
            pass
        try:
            record = self.store.get('funds', fund_id, None)
            if not isinstance(record, dict):
                return
            current = record.get('state')
            if current in ('created', 'active', 'paused', 'permanent_halt') and current != target:
                updated = dict(record)
                updated['state'] = target
                self.store.put('funds', fund_id, updated)
        except Exception:
            pass

    # ----------------------------------------------------------------- signal
    async def _signal(self, client, fund_id: str, state: dict, now_ms: int):
        boundary = (now_ms // HOUR) * HOUR
        cache = self.store.get(NS_CANDLES, fund_id, None)
        if not isinstance(cache, list):
            cache = None
        # Refetch whenever the store lacks the hour that just closed. Only
        # fully elapsed hours are kept, so a mid-hour snapshot is never later
        # mistaken for the final close of that hour.
        stale = bool(cache) and cache[-1][0] < boundary - HOUR
        if not cache or stale:
            rows = await client.candles()
            if isinstance(rows, dict):
                rows = rows.get('candles')
            normalized = []
            for row in rows or []:
                if not isinstance(row, (list, tuple)) or len(row) < 6:
                    raise FundError('invalid_candles')
                open_ms = int(row[0])
                if open_ms + HOUR > now_ms:
                    continue  # still forming
                normalized.append([open_ms] + [float(x) for x in row[1:6]])
            if normalized:
                self.store.put(NS_CANDLES, fund_id, normalized)
                cache = normalized
        if not cache:
            return None
        try:
            return S.closed_signal(cache, now_ms)
        except ValueError:
            return None

    # ----------------------------------------------------------------- decide
    def _decide(self, fund_id: str, state: dict, policy, signal, price: float,
                fee: float, wallet_units: int, available_cash_units: int,
                min_stake_units: int, now_ms: int) -> dict | None:
        position = state['position']
        if position:
            fresh = S.signal_is_fresh(signal, now_ms)
            reason = None
            if state['halted']:
                reason = 'drawdown'
            elif state['paused']:
                reason = 'daily'
            elif state['stopped']:
                reason = 'operator_stop'
            elif price <= position['stop']:
                reason = 'atr_stop'
            elif now_ms - position['entry_ms'] >= policy.max_hold_days * DAY:
                reason = 'time'
            elif fresh and signal.exit:
                reason = 'trend'
            if reason:
                return {'action': 'sell', 'position_id': position['id'], 'reason': reason,
                        'created_ms': now_ms, 'signal_ms': now_ms}
            return None

        if state['halted'] or state['paused'] or state['stopped'] or state['blocked']:
            return None
        if any(self.store.get(ns, fund_id, {}).get('hold', False) for ns in
               ('settlement_holds', 'reconciliation_holds', 'valuation_holds')):
            return None
        if not self.live:
            return None  # default no-write mode never stages a new open
        if signal is None or not S.signal_is_fresh(signal, now_ms) or not signal.enter:
            return None
        if now_ms - signal.end_ms > S.ENTRY_WINDOW_MS:
            return None
        ms_since_loss = None if state['last_exit_ms'] is None else now_ms - int(state['last_exit_ms'])
        if not S.cooldown_ready(state['exit_signal_ms'], now_ms, policy):
            return None
        if not S.loss_gate_allows(policy, signal, ms_since_loss, bool(state['last_loss'])):
            return None
        usable = min(int(available_cash_units), int(wallet_units))
        amount = S.entry_quantity_units(available_cash_units=usable, policy=policy,
                                        distance=signal.distance, fee_rate=fee,
                                        min_stake_units=min_stake_units)
        if amount <= 0:
            return None
        return {'action': 'buy', 'amount_units': amount, 'amount': money_text(amount),
                'leverage': policy.leverage, 'distance': signal.distance,
                'key': 'fund-' + secrets.token_hex(16),
                'created_ms': now_ms, 'signal_ms': signal.end_ms}

    # ---------------------------------------------------------------- execute
    async def _execute(self, fund_id: str, client, state: dict, intent: dict, now_ms: int) -> None:
        if not self.live:
            # Local persistence is allowed, external writes are not. A pending
            # exit is retained so it fires once live is enabled; a staged entry
            # is dropped because nothing was ever sent.
            self._event(fund_id, 'external_write_blocked', {'action': intent['action']},
                        now_ms, 'warning')
            if intent['action'] == 'sell':
                state['pending'] = intent
            state['blocked'] = 'live_disabled'
            self._save(fund_id, state)
            return

        state['pending'] = intent
        self._save(fund_id, state)  # intent is durable before any request
        self._event(fund_id, 'order_intent',
                    {'action': intent['action'],
                     'amount_units': intent.get('amount_units'),
                     'leverage': intent.get('leverage'),
                     'reason': intent.get('reason')}, now_ms)
        try:
            if intent['action'] == 'buy':
                response = await client.buy(intent['amount'], intent['leverage'], intent['key'])
            else:
                response = await client.sell(intent['position_id'])
        except Exception as exc:
            state = self._load(fund_id)
            state['last_error'] = type(exc).__name__
            self._save(fund_id, state)
            self._event(fund_id, 'order_uncertain', {'action': intent['action']},
                        now_ms, 'warning')
            return
        state = self._load(fund_id)
        if intent['action'] == 'buy':
            self._confirm_buy(fund_id, state, intent, response, now_ms)
        else:
            self._confirm_sell(fund_id, state, intent, response, now_ms)

    async def _resume_pending(self, fund_id: str, client, state: dict, now_ms: int) -> None:
        pending = state['pending']
        if pending['action'] == 'buy':
            if not self.live:
                state['blocked'] = 'live_disabled'
                self._save(fund_id, state)
                return
            if state['halted'] or state['stopped']:
                # A halted or operator-disabled fund must never replay an
                # uncertain open. Hold the intent quarantined; only a later site
                # reconciliation, never a blind retry, may resolve its fate.
                if state['blocked'] != 'stop_with_unconfirmed_buy':
                    state['blocked'] = 'unconfirmed_buy' if state['halted'] else 'stop_with_unconfirmed_buy'
                self._save(fund_id, state)
                return
            if now_ms - int(pending['created_ms']) > BUY_CONFIRM_TIMEOUT_MS:
                # Same-key replay is safe, but an unanswered buy beyond the
                # deadline is left for reconciliation instead of retried forever.
                state['blocked'] = 'unconfirmed_buy'
                self._save(fund_id, state)
                self._event(fund_id, 'unconfirmed_buy', {'key': pending['key']}, now_ms, 'error')
                return
            try:
                response = await client.buy(pending['amount'], pending['leverage'], pending['key'])
            except Exception as exc:
                state['last_error'] = type(exc).__name__
                self._save(fund_id, state)
                return
            state = self._load(fund_id)
            self._confirm_buy(fund_id, state, pending, response, now_ms)
            return
        await self._resume_sell(fund_id, client, state, now_ms)

    async def _resume_sell(self, fund_id: str, client, state: dict, now_ms: int) -> None:
        """Resolve a persisted sell intent against the authoritative snapshot.

        The site's sell endpoint is idempotent by position id, so re-submitting
        an exit only replays the settlement recorded for that lot. Reconciliation
        still runs first so ownership stays authoritative: a lot confirmed still
        open needs its exit submitted, while a lot already gone is settled from
        that recorded replay.
        """
        if not self.live:
            state['blocked'] = 'live_disabled'
            self._save(fund_id, state)
            return
        pending = state['pending']
        position_id = pending['position_id']
        snapshot = await self._snapshot(client, fund_id, now_ms, force=True)
        still_open = position_id in self._position_ids(snapshot)
        if still_open:
            # The owned lot is still on the account, so this is the real exit
            # attempt, not a replay.
            try:
                response = await client.sell(position_id)
            except Exception as exc:
                state = self._load(fund_id)
                state['last_error'] = type(exc).__name__
                self._save(fund_id, state)
                return
        else:
            # The lot is gone: the idempotent endpoint replays the settlement
            # recorded for this position id.
            try:
                response = await client.sell(position_id)
            except Exception:
                state = self._load(fund_id)
                state['blocked'] = 'settlement_unknown'
                self._save(fund_id, state)
                self._event(fund_id, 'settlement_unknown', {'position_id': position_id},
                            now_ms, 'error')
                return
        state = self._load(fund_id)
        self._confirm_sell(fund_id, state, pending, response, now_ms)

    async def _settle_external(self, fund_id: str, client, state: dict,
                               position_id: str, now_ms: int) -> None:
        """Our owned lot vanished (manual close or liquidation)."""
        if not self.live:
            state['blocked'] = 'reconcile_required'
            self._save(fund_id, state)
            self._event(fund_id, 'reconcile_required', {'position_id': position_id},
                        now_ms, 'warning')
            return
        intent = {'action': 'sell', 'position_id': position_id,
                  'reason': 'external_close', 'created_ms': now_ms, 'signal_ms': now_ms}
        try:
            response = await client.sell(position_id)
        except Exception:
            state['blocked'] = 'settlement_unknown'
            self._save(fund_id, state)
            self._event(fund_id, 'settlement_unknown', {'position_id': position_id},
                        now_ms, 'error')
            return
        self._confirm_sell(fund_id, state, intent, response, now_ms)

    # -------------------------------------------------------------- confirms
    def _confirm_buy(self, fund_id: str, state: dict, intent: dict, response: dict, now_ms: int) -> None:
        row = response.get('position') if isinstance(response, dict) else None
        if not isinstance(row, dict) or not row.get('id'):
            self._blocked_mismatch(fund_id, state, 'buy_confirmation_mismatch', now_ms)
            return
        if row.get('symbol') not in (None, 'BTCUSDT'):
            self._blocked_mismatch(fund_id, state, 'buy_confirmation_mismatch', now_ms)
            return
        leverage = int(row.get('leverage', intent['leverage']))
        try:
            stake_units = money_units(str(row.get('stake')), positive=True)
        except FundError:
            self._blocked_mismatch(fund_id, state, 'buy_confirmation_mismatch', now_ms)
            return
        if leverage != int(intent['leverage']) or stake_units != int(intent['amount_units']):
            self._blocked_mismatch(fund_id, state, 'buy_confirmation_mismatch', now_ms)
            return
        try:
            entry = float(row['entry_price'])
        except (KeyError, TypeError, ValueError):
            self._blocked_mismatch(fund_id, state, 'invalid_fill', now_ms)
            return
        if not entry > 0 or entry != entry or entry == float('inf'):
            self._blocked_mismatch(fund_id, state, 'invalid_fill', now_ms)
            return
        try:
            opened = S.iso_utc_ms(row['opened_at']) if row.get('opened_at') else now_ms
        except FundError:
            opened = now_ms
        state['position'] = {
            'id': str(row['id']),
            'stake_units': stake_units,
            'entry': entry,
            'leverage': leverage,
            'distance': float(intent['distance']),
            'stop': S.stop_price(entry, float(intent['distance'])),
            'liquidation_price': row.get('liquidation_price'),
            'entry_ms': opened,
            'signal_ms': int(intent['signal_ms']),
            'confirmed_ms': now_ms,
        }
        state['pending'] = None
        state['blocked'] = None
        state['last_error'] = None
        self._save(fund_id, state)
        self._event(fund_id, 'opened', {'position_id': state['position']['id'],
                                        'stake_units': stake_units, 'leverage': leverage},
                    now_ms)

    def _confirm_sell(self, fund_id: str, state: dict, intent: dict, response: dict, now_ms: int) -> None:
        position = state['position']
        row = response if isinstance(response, dict) else {}
        position_id = str(row.get('position_id') or intent.get('position_id') or '')
        if position_id and position_id == state['last_settled_id']:
            state['pending'] = None
            self._save(fund_id, state)
            return
        if not position or position['id'] != position_id:
            self._blocked_mismatch(fund_id, state, 'unowned_settlement', now_ms)
            return
        try:
            payout_units = money_units(str(row.get('payout', 0)))
        except FundError:
            self._blocked_mismatch(fund_id, state, 'invalid_payout', now_ms)
            return
        if payout_units < 0:
            self._blocked_mismatch(fund_id, state, 'invalid_payout', now_ms)
            return
        pnl_units = payout_units - int(position['stake_units'])
        # The ledger P&L and the trader's cleared position must commit together.
        # trade_realized is idempotent by position id, so if it raises the whole
        # transaction rolls back and the sell intent stays pending; the next
        # tick re-reads the same site payout by position id instead of losing the
        # P&L. Money state is never hidden behind a swallowed event-log failure:
        # logging happens only after the transaction commits.
        updated = dict(state)
        with self.store.transaction():
            # P&L is payout minus stake; the site already deducted its fee, so
            # the ledger must not deduct anything again.
            self.ledger.trade_realized(fund_id, position_id, pnl_units, now_ms)
            updated['last_loss'] = pnl_units < 0
            updated['last_exit_ms'] = now_ms
            updated['exit_signal_ms'] = int(intent.get('signal_ms') or now_ms)
            updated['position'] = None
            updated['pending'] = None
            updated['last_settled_id'] = position_id
            updated['completed_trades'] = int(state['completed_trades']) + 1
            updated['last_error'] = None
            self._save(fund_id, updated)
        state.update(updated)
        self._event(fund_id, 'closed', {'position_id': position_id,
                                        'pnl_units': pnl_units,
                                        'payout_units': payout_units,
                                        'liquidated': bool(row.get('liquidated')),
                                        'reason': intent.get('reason')}, now_ms)

    def _blocked_mismatch(self, fund_id: str, state: dict, code: str, now_ms: int) -> None:
        state['blocked'] = code
        state['last_error'] = code
        self._save(fund_id, state)
        self._event(fund_id, 'confirmation_mismatch', {'code': code}, now_ms, 'error')

    # ------------------------------------------------------------------- stop
    async def stop(self, fund_id: str, reason: str, now_ms: int, permanent: bool = False) -> dict:
        if fund_id not in self.policies:
            raise FundError('unknown_fund')
        async with self._locks[fund_id]:
            state = self._load(fund_id)
            if permanent:
                state['halted'] = True
                state['halt_reason'] = reason
            else:
                state['stopped'] = True
                state['stop_reason'] = reason
            position = state['position']
            pending = state['pending']
            if position and not (pending and pending['action'] == 'sell'):
                state['pending'] = {'action': 'sell', 'position_id': position['id'],
                                    'reason': reason, 'created_ms': now_ms, 'signal_ms': now_ms}
                self._event(fund_id, 'order_intent',
                            {'action': 'sell', 'reason': reason, 'source': 'stop'}, now_ms)
            elif pending and pending['action'] == 'buy':
                state['blocked'] = 'stop_with_unconfirmed_buy'
            self._save(fund_id, state)
            self._event(fund_id, 'fund_stop',
                        {'reason': reason, 'permanent': bool(permanent)}, now_ms)
            return self._fund_status(fund_id, state)

    async def set_running(self, fund_id: str, enabled: bool, now_ms: int) -> dict:
        """Enable or disable *new openings* for one fund.

        New trader state starts stopped, so nothing opens until the operator (or
        the central root) explicitly enables it. Disabling queues the same
        owned-lot exit as :meth:`stop`; enabling is refused once the fund is
        permanently halted, because extra capital must never revive it.
        """
        if fund_id not in self.policies:
            raise FundError('unknown_fund')
        if not isinstance(enabled, bool):
            raise FundError('invalid_running')
        async with self._locks[fund_id]:
            state = self._load(fund_id)
            if enabled:
                if state['halted']:
                    raise FundError('permanent_halt')
                state['stopped'] = False
                state['stop_reason'] = None
                self._event(fund_id, 'fund_started', None, now_ms)
            else:
                state['stopped'] = True
                state['stop_reason'] = 'operator_disabled'
                position = state['position']
                pending = state['pending']
                if position and not (pending and pending['action'] == 'sell'):
                    state['pending'] = {'action': 'sell', 'position_id': position['id'],
                                        'reason': 'operator_disabled', 'created_ms': now_ms,
                                        'signal_ms': now_ms}
                    self._event(fund_id, 'order_intent',
                                {'action': 'sell', 'reason': 'operator_disabled',
                                 'source': 'disable'}, now_ms)
                elif pending and pending['action'] == 'buy':
                    state['blocked'] = 'stop_with_unconfirmed_buy'
                self._event(fund_id, 'fund_stop',
                            {'reason': 'operator_disabled', 'permanent': False}, now_ms)
            self._save(fund_id, state)
            self._sync_fund_state(fund_id, state, now_ms)
            return self._fund_status(fund_id, state)

    # -------------------------------------------------------------- liquidity
    async def close_for_liquidity(self, fund_id: str, amount_units: int, now_ms: int,
                                  reason: str = 'liquidity') -> dict:
        """Raise ``amount_units`` of cash for a settlement/redemption obligation.

        Returns a plan; it never flattens a position it does not own. When the
        ledger's available cash (already net of pending receipts, institution
        fees and confirmed liabilities) cannot cover the request and an owned
        position exists, it queues that position's exit, to be executed by the
        next :meth:`tick`. If the ask can never be met from one owned lot, the
        shortfall is reported rather than hidden.
        """
        if fund_id not in self.policies:
            raise FundError('unknown_fund')
        requested = int(amount_units)
        async with self._locks[fund_id]:
            state = self._load(fund_id)
            try:
                available = int(self.ledger.status(fund_id)['available_cash_units'])
            except Exception:
                available = None
            shortfall = None if available is None else requested - available
            position = state['position']
            pending = state['pending']
            if shortfall is None:
                action = 'unknown_cash'
            elif shortfall <= 0:
                action = 'sufficient'
            elif position is None:
                action = 'no_owned_position'
            elif pending:
                action = 'pending_exit'
            elif not self.live:
                action = 'live_disabled'
            else:
                state['pending'] = {'action': 'sell', 'position_id': position['id'],
                                    'reason': reason, 'created_ms': now_ms, 'signal_ms': now_ms}
                self._save(fund_id, state)
                self._event(fund_id, 'order_intent',
                            {'action': 'sell', 'reason': reason, 'source': 'liquidity'}, now_ms)
                action = 'exit_owned_position'
            return {'fund_id': fund_id, 'requested_units': requested,
                    'available_cash_units': available, 'shortfall_units': shortfall,
                    'action': action,
                    'position_id': position['id'] if position else None}

    # ---------------------------------------------------------------- status
    def _fund_status(self, fund_id: str, state: dict) -> dict:
        policy = self.policies[fund_id]
        position = state['position']
        pending = state['pending']
        if state['halted']:
            phase = 'halted'
        elif state['stopped']:
            phase = 'stopped'
        elif state['paused']:
            phase = 'daily_pause'
        elif state['blocked']:
            phase = 'blocked'
        elif position:
            phase = 'holding'
        else:
            phase = 'flat'
        return {
            'fund_id': fund_id,
            'label': policy.label,
            'leverage': policy.leverage,
            'risk': policy.risk,
            'cap': policy.cap,
            'daily_loss': policy.daily_loss,
            'drawdown_limit': policy.drawdown_limit,
            'phase': phase,
            'blocked': state['blocked'],
            'halt_reason': state['halt_reason'],
            'stop_reason': state['stop_reason'],
            'nav': state['nav'],
            'peak': state['peak'],
            'day': state['day'],
            'day_start': state['day_start'],
            'units': state['units'],
            'flow_basis_units': int(state['flow_basis_units']),
            'position': None if not position else {
                'id': position['id'], 'stake_units': int(position['stake_units']),
                'entry': position['entry'], 'stop': position['stop'],
                'leverage': position['leverage'], 'entry_ms': position['entry_ms'],
            },
            'pending': None if not pending else pending['action'],
            'completed_trades': int(state['completed_trades']),
            'last_mark_ms': state['last_mark_ms'],
            'quote_ms': state['quote_ms'],
            'last_exit_ms': state['last_exit_ms'],
            'last_loss': bool(state['last_loss']),
            'mark_failed': bool(state['mark_failed']),
            'last_error': state['last_error'],
        }

    def public_status(self) -> dict:
        return {
            'live': self.live,
            'tick_seconds': _TICK_SECONDS,
            'policies': {fund_id: policy.public() for fund_id, policy in self.policies.items()},
            'funds': {fund_id: self._fund_status(fund_id, self._load(fund_id))
                      for fund_id in self.policies},
        }
