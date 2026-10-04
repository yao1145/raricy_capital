"""Focused v0.3 FundTrader tests: money flow, halts, reconciliation, freeze.

These use isolated fake clients and a fake ledger only; no real site or store
schema beyond :class:`FundStore` is touched, and every fake is deterministic.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from raricy_capital import strategy as S
from raricy_capital.contracts import FundError, POLICIES
from raricy_capital.store import FundStore
from raricy_capital.trader import FundTrader

H = 3600000
BASE = 1800000000000
FUND = 'capital1'


def history(count=300):
    t = BASE
    return [[t + i * H, 100 + i * .1, 100.3 + i * .1, 99.8 + i * .1, 100.1 + i * .1, 1.]
            for i in range(count)]


def boundary(now):
    return (now // H) * H


class FakeLedger:
    def __init__(self):
        self.wallet = {}
        self.position = {}
        self.flows = {}
        self.other = {}
        self.liabilities = {}
        self.realized = {}
        self.shares = {}
        self.fail_trade_realized = False
        self.last_mark_now_ms = None
        self.last_mark_quote_ms = None

    def _ensure(self, fund_id):
        self.wallet.setdefault(fund_id, 0)
        self.position.setdefault(fund_id, 0)
        self.flows.setdefault(fund_id, 0)
        self.other.setdefault(fund_id, 0)
        self.liabilities.setdefault(fund_id, 0)
        self.realized.setdefault(fund_id, {})
        self.shares.setdefault(fund_id, 1)

    def set_wallet(self, fund_id, units):
        self._ensure(fund_id)
        self.wallet[fund_id] = int(units)

    def set_shares(self, fund_id, atoms):
        self._ensure(fund_id)
        self.shares[fund_id] = int(atoms)

    def add_flow(self, fund_id, units):
        self._ensure(fund_id)
        self.flows[fund_id] += int(units)

    def add_non_trading(self, fund_id, units):
        self._ensure(fund_id)
        self.other[fund_id] += int(units)

    def status(self, fund_id):
        self._ensure(fund_id)
        wallet = self.wallet[fund_id]
        return {
            'fund_id': fund_id,
            'equity_units': wallet + self.position[fund_id] - self.liabilities[fund_id],
            'wallet_units': wallet,
            'position_value_units': self.position[fund_id],
            'available_cash_units': wallet - self.liabilities[fund_id],
            'capital_flows_units': self.flows[fund_id],
            'non_trading_income_units': self.other[fund_id],
            'liabilities_units': self.liabilities[fund_id],
            'realized_profit_units': sum(self.realized[fund_id].values()),
            'shares_atoms': self.shares[fund_id],
            'quote_ms': None,
        }

    def mark_account(self, fund_id, wallet_units, position_value_units, now_ms, quote_ms):
        self._ensure(fund_id)
        self.last_mark_now_ms = int(now_ms)
        self.last_mark_quote_ms = int(quote_ms)
        self.wallet[fund_id] = int(wallet_units)
        self.position[fund_id] = int(position_value_units)
        return self.status(fund_id)

    def trade_realized(self, fund_id, trade_id, pnl_units, now_ms):
        self._ensure(fund_id)
        if self.fail_trade_realized:
            raise RuntimeError('ledger_down')
        events = self.realized[fund_id]
        if trade_id not in events:
            events[trade_id] = int(pnl_units)
        return {'trade_id': trade_id, 'pnl_units': events[trade_id]}


class FakeClient:
    def __init__(self, wallet_units, rows, *, price=130.0):
        self.wallet_units = int(wallet_units)
        self.rows = rows
        self.price = price
        self.fee_rate = 0.0002
        self.positions = []
        self.calls = []
        self.sell_raises = False
        self.snapshot_raises = False
        self.quote_raises = False
        self.buy_raises = False
        self.sell_payout = '0.0000'
        self.on_buy = None

    async def snapshot(self):
        self.calls.append('snapshot')
        if self.snapshot_raises:
            raise RuntimeError('network')
        return {'balance': self.wallet_units / 10000.0, 'positions': list(self.positions),
                'feeRate': self.fee_rate, 'minStake': 1, 'leverageEnabled': True,
                'leverageOptions': [1, 2, 3, 5, 10, 20]}

    async def balance(self):
        return self.wallet_units

    async def quote(self):
        if self.quote_raises:
            raise RuntimeError('network')
        return self.price, self.fee_rate

    async def candles(self):
        return [list(row) for row in self.rows]

    async def buy(self, amount, leverage, key):
        self.calls.append(('buy', amount, leverage, key))
        if self.on_buy:
            self.on_buy()
        if self.buy_raises:
            raise RuntimeError('network')
        opened = datetime.fromtimestamp((BASE + 300 * H) / 1000, timezone.utc)
        return {'position': {'id': 'srv-1', 'symbol': 'BTCUSDT', 'stake': amount,
                             'entry_price': self.price, 'leverage': leverage,
                             'liquidation_price': 0,
                             'opened_at': opened.isoformat().replace('+00:00', 'Z')},
                'replayed': False}

    async def sell(self, position_id):
        self.calls.append(('sell', position_id))
        if self.sell_raises:
            raise RuntimeError('network')
        return {'position_id': position_id, 'payout': self.sell_payout,
                'exit_price': self.price, 'liquidated': False}


@pytest.fixture
def tmp_path():
    # Pytest's global temp dir is not writable under the Windows sandbox.
    path = Path(__file__).resolve().parents[1] / 'data' / 'funds_tests' / secrets.token_hex(8)
    path.mkdir(parents=True)
    return path


@pytest.fixture(autouse=True)
def close_stores(monkeypatch):
    stores = []
    original = FundStore.__init__

    def create(self, *args, **kwargs):
        original(self, *args, **kwargs)
        stores.append(self)

    monkeypatch.setattr(FundStore, '__init__', create)
    yield
    for store in stores:
        store.close()


def make(tmp_path, *, live=True, wallet_units=5_000_000, fund=FUND, client=None):
    store = FundStore(tmp_path / 'funds.sqlite3')
    ledger = FakeLedger()
    ledger.set_wallet(fund, wallet_units)
    client = client or FakeClient(wallet_units, history())
    trader = FundTrader(store, ledger, {fund: client}, live=live)
    return trader, store, ledger, client


def state_of(store, fund=FUND):
    return store.get('fund_trader', fund, {})


# --------------------------------------------------------------- frozen rules
def test_frozen_policy_parameters_are_not_mutated(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    assert POLICIES['capital1'].leverage == 3 and POLICIES['capital2'].leverage == 5
    assert POLICIES['capital1'].risk == pytest.approx(0.0125)
    assert POLICIES['capital2'].risk == pytest.approx(0.05)
    assert POLICIES['capital1'].cap == 2.0 and POLICIES['capital2'].cap == 5.0
    assert POLICIES['capital1'].daily_loss == pytest.approx(0.015)
    assert POLICIES['capital2'].daily_loss == pytest.approx(0.05)
    assert POLICIES['capital1'].drawdown_limit == pytest.approx(0.25)
    assert POLICIES['capital2'].drawdown_limit == pytest.approx(0.70)
    assert POLICIES['capital1'].loss_gate_hours == 12
    assert POLICIES['capital2'].loss_gate_hours == 0
    published = trader.public_status()['policies']
    assert published['capital1']['leverage'] == 3 and published['capital2']['leverage'] == 5


def test_pure_risk_helpers():
    policy = POLICIES['capital1']
    # Sizing is a fraction of available cash under both risk and cap.
    amount = S.entry_quantity_units(available_cash_units=5_000_000, policy=policy,
                                    distance=0.01, fee_rate=0.0002, min_stake_units=10000)
    assert 0 < amount <= 5_000_000 * policy.cap // policy.leverage
    assert S.entry_quantity_units(available_cash_units=10000, policy=policy,
                                  distance=0.5, fee_rate=0.0002, min_stake_units=10000) == 0
    # Cooldown: next full hour plus 4 hours.
    exit_ms = boundary(BASE) + 10
    assert not S.cooldown_ready(exit_ms, exit_ms + H, policy)
    assert S.cooldown_ready(exit_ms, exit_ms + 5 * H, policy)
    # capital1 ER12 gate only after a losing close within 12h.
    class Sig:
        end_ms = exit_ms
        enter = exit = True
        distance = 0.01
        er = 0.1
        change = 1.0
    assert not S.loss_gate_allows(policy, Sig(), H, True)
    Sig.er = 0.5
    assert S.loss_gate_allows(policy, Sig(), H, True)
    assert S.loss_gate_allows(policy, Sig(), 13 * H, True)
    assert S.loss_gate_allows(policy, Sig(), H, False)
    Sig.er = 0.1
    assert S.loss_gate_allows(POLICIES['capital2'], Sig(), H, True)


# ------------------------------------------------------------------ live gate
@pytest.mark.asyncio
async def test_live_false_never_sends_orders(tmp_path):
    trader, store, ledger, client = make(tmp_path, live=False)
    now = boundary(BASE + 300 * H) + 1000
    await trader.tick(now)
    assert not [c for c in client.calls if isinstance(c, tuple)]
    assert state_of(store)['position'] is None
    assert trader.public_status()['live'] is False


@pytest.mark.asyncio
async def test_buy_intent_is_persisted_before_request(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)

    def check_persisted():
        pending = state_of(store)['pending']
        assert pending and pending['action'] == 'buy' and pending['key'] in str(client.calls[-1])

    client.on_buy = check_persisted
    await trader.tick(now)
    state = state_of(store)
    assert state['position'] is not None and state['pending'] is None
    assert state['position']['stop'] < client.price
    assert state['position']['stake_units'] > 0
    assert [c for c in client.calls if c[0] == 'buy'].__len__() == 1


@pytest.mark.asyncio
async def test_unknown_buy_retries_same_key_then_stops(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)
    client.buy_raises = True
    await trader.tick(now)
    first = state_of(store)
    assert first['pending']['action'] == 'buy' and first['position'] is None
    keys = [c[3] for c in client.calls if isinstance(c, tuple) and c[0] == 'buy']
    assert len(keys) == 1
    # Same-key retry is allowed while the confirm deadline has not passed.
    await trader.tick(now + 4000)
    keys = [c[3] for c in client.calls if isinstance(c, tuple) and c[0] == 'buy']
    assert len(keys) == 2 and keys[0] == keys[1]
    # Past the deadline the pending buy is quarantined instead of retried forever.
    await trader.tick(now + 25000)
    assert state_of(store)['blocked'] == 'unconfirmed_buy'
    assert len([c for c in client.calls if isinstance(c, tuple) and c[0] == 'buy']) == 2


# ------------------------------------------------------------- reconciliation
@pytest.mark.asyncio
async def test_uncertain_sell_is_reconciled_before_repeating(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)
    await trader.tick(now)
    position_id = state_of(store)['position']['id']
    client.positions = [{'id': position_id, 'symbol': 'BTCUSDT', 'stake': 1,
                         'entryPrice': client.price, 'leverage': 3, 'openedAt': 'x'}]
    client.sell_raises = True
    await trader.stop(FUND, 'operator', now + 4000)
    await trader.tick(now + 4000)
    assert client.calls[-1][0] == 'sell'  # first attempt came from reconciliation
    assert [c for c in client.calls if c[0] == 'sell'].__len__() == 1
    assert state_of(store)['pending']['action'] == 'sell'
    # A fresh reconciliation snapshot shows the lot still open, so the exit is
    # submitted once more and now succeeds.
    client.sell_raises = False
    client.sell_payout = '83.3333'
    await trader.tick(now + 8000)
    sells = [c for c in client.calls if c[0] == 'sell']
    assert len(sells) == 2 and sells[0][1] == sells[1][1] == position_id
    state = state_of(store)
    assert state['position'] is None and state['pending'] is None
    assert ledger.realized[FUND]  # realized P&L booked exactly once


@pytest.mark.asyncio
async def test_uncertain_sell_never_repeats_without_reconciliation(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)
    await trader.tick(now)
    client.sell_raises = True
    await trader.stop(FUND, 'operator', now + 4000)
    await trader.tick(now + 4000)
    assert len([c for c in client.calls if c[0] == 'sell']) == 1
    # Reconciliation itself fails: the trader must not fire the sell again.
    client.snapshot_raises = True
    await trader.tick(now + 8000)
    assert len([c for c in client.calls if c[0] == 'sell']) == 1
    assert state_of(store)['pending']['action'] == 'sell'


@pytest.mark.asyncio
async def test_unknown_manual_position_is_quarantined_not_flattened(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    client.positions = [{'id': 'manual-lot', 'symbol': 'BTCUSDT', 'stake': 5,
                         'entryPrice': client.price, 'leverage': 2, 'openedAt': 'x'}]
    await trader.tick(now)
    assert state_of(store)['blocked'] == 'untracked_positions'
    assert not [c for c in client.calls if c[0] == 'sell']
    # Available cash already covers the ask: the correct plan is 'sufficient'
    # and it must not touch the manual lot.
    covered = await trader.close_for_liquidity(FUND, 1_000_000, now + 4000)
    assert covered['action'] == 'sufficient'
    assert covered['shortfall_units'] <= 0
    assert covered['position_id'] is None
    assert not [c for c in client.calls if c[0] == 'sell']
    # A shortfall beyond available cash cannot be met from a manual position we
    # do not own, so the plan reports it instead of flattening anything.
    short = await trader.close_for_liquidity(FUND, 10_000_000, now + 8000)
    assert short['action'] == 'no_owned_position'
    assert short['shortfall_units'] == 5_000_000
    assert not [c for c in client.calls if c[0] == 'sell']


@pytest.mark.asyncio
async def test_close_for_liquidity_plans_owned_exit(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)
    await trader.tick(now)
    ledger.set_wallet(FUND, 0)  # no free cash left
    plan = await trader.close_for_liquidity(FUND, 1_000_000, now + 4000)
    assert plan['action'] == 'exit_owned_position'
    assert plan['shortfall_units'] == 1_000_000
    assert state_of(store)['pending']['action'] == 'sell'


# ----------------------------------------------------------------- money flow
@pytest.mark.asyncio
async def test_external_flows_do_not_reset_nav_or_halt(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000  # outside the entry window
    await trader.tick(now)
    base_nav = Decimal(state_of(store)['nav'])
    assert base_nav == Decimal('1.00000000')
    # A subscription (capital flow) must leave the trading NAV unchanged.
    ledger.add_flow(FUND, 5_000_000)
    client.wallet_units += 5_000_000
    await trader.tick(now + 4000)
    assert Decimal(state_of(store)['nav']) == base_nav
    # Emergency fee kept in the fund is non-trading income: also excluded.
    ledger.add_non_trading(FUND, 100_000)
    client.wallet_units += 100_000
    await trader.tick(now + 8000)
    assert Decimal(state_of(store)['nav']) == base_nav
    # Only trading P&L moves NAV.
    client.wallet_units -= 500_000
    await trader.tick(now + 12000)
    assert Decimal(state_of(store)['nav']) < base_nav


@pytest.mark.asyncio
async def test_drawdown_halt_is_permanent_and_capital_does_not_clear_it(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000
    await trader.tick(now)
    client.wallet_units = 3_750_000  # -25% trading loss
    await trader.tick(now + 4000)
    state = state_of(store)
    assert state['halted'] is True and state['halt_reason'] == 'drawdown'
    await trader.stop(FUND, 'operator', now + 8000, permanent=True)
    # Extra capital is a flow, so NAV is preserved and the halt is not lifted.
    ledger.add_flow(FUND, 5_000_000)
    client.wallet_units += 5_000_000
    await trader.tick(now + 12000)
    assert state_of(store)['halted'] is True
    assert trader.public_status()['funds'][FUND]['phase'] == 'halted'


@pytest.mark.asyncio
async def test_failed_mark_freezes_peak_and_nav(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000
    await trader.tick(now)
    peak = state_of(store)['peak']
    nav = state_of(store)['nav']
    client.quote_raises = True
    await trader.tick(now + 4000)
    state = state_of(store)
    assert state['mark_failed'] is True and state['last_error'] == 'RuntimeError'
    assert state['peak'] == peak and state['nav'] == nav


@pytest.mark.asyncio
async def test_beijing_daily_pause_resets_next_day(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000
    await trader.tick(now)
    client.wallet_units = 4_925_000  # -1.5% trading loss
    await trader.tick(now + 4000)
    assert state_of(store)['paused'] is True
    assert state_of(store)['halted'] is False
    assert any(info['fund_id'] == FUND for _, info in store.list_items('notices'))
    await trader.tick(now + 86400000)  # next Beijing day resets the pause
    assert state_of(store)['paused'] is False


def test_position_value_uses_site_settlement_formula():
    assert S.position_value_units(100_0000, 100.0, 101.0, 3) == 1029394
    assert S.position_value_units(0, 100.0, 101.0, 3) == 0


# ------------------------------------------------------- operator run switch
@pytest.mark.asyncio
async def test_new_fund_is_stopped_until_enabled(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    # A brand-new fund opens nothing until it is explicitly enabled.
    await trader.tick(now)
    assert state_of(store)['position'] is None
    assert not [c for c in client.calls if isinstance(c, tuple) and c[0] == 'buy']
    assert trader.public_status()['funds'][FUND]['phase'] == 'stopped'
    await trader.set_running(FUND, True, now)
    await trader.tick(now)
    assert state_of(store)['position'] is not None
    # Disabling queues the owned exit exactly like stop().
    await trader.set_running(FUND, False, now + 4000)
    assert state_of(store)['stopped'] is True
    assert state_of(store)['pending']['action'] == 'sell'
    # A permanent halt can never be restarted, even with extra capital.
    await trader.stop(FUND, 'operator', now + 8000, permanent=True)
    ledger.add_flow(FUND, 5_000_000)
    client.wallet_units += 5_000_000
    with pytest.raises(FundError):
        await trader.set_running(FUND, True, now + 12000)


@pytest.mark.asyncio
async def test_set_running_rejects_non_bool(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    with pytest.raises(FundError):
        await trader.set_running(FUND, 1, now)


# ------------------------------------------------------------- money safety
@pytest.mark.asyncio
async def test_settlement_ledger_failure_keeps_position_for_retry(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 1000
    await trader.set_running(FUND, True, now)
    await trader.tick(now)
    position_id = state_of(store)['position']['id']
    client.sell_payout = '83.3333'
    ledger.fail_trade_realized = True
    await trader.stop(FUND, 'operator', now + 4000)
    await trader.tick(now + 4000)
    state = state_of(store)
    # The site payout is not lost: the owned lot and its exit stay pending.
    assert state['position'] is not None and state['position']['id'] == position_id
    assert state['pending'] and state['pending']['action'] == 'sell'
    assert ledger.realized[FUND] == {}
    # Idempotent retry reads the same site settlement and commits atomically.
    ledger.fail_trade_realized = False
    await trader.tick(now + 8000)
    state = state_of(store)
    assert state['position'] is None and state['pending'] is None
    assert position_id in ledger.realized[FUND]


@pytest.mark.asyncio
async def test_quote_observation_time_drives_the_mark(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000  # outside the entry window
    await trader.tick(now)
    observed = now - 500
    client.quote_ms = observed
    await trader.tick(now + 4000)
    state = state_of(store)
    assert state['quote_ms'] == observed
    assert state['last_mark_ms'] == max(now + 4000, observed)
    assert ledger.last_mark_quote_ms == observed


@pytest.mark.asyncio
async def test_zero_share_wallet_is_not_trading_assets(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    ledger.set_shares(FUND, 0)
    now = boundary(BASE + 300 * H) + 20000
    await trader.tick(now)
    assert state_of(store)['units'] is None  # unallocated wallet, no baseline
    # The first issued shares establish the baseline at NAV 1 without
    # fabricating a peak from the unallocated wallet.
    ledger.set_shares(FUND, 5_000_000 * 100_000_000)
    await trader.tick(now + 4000)
    state = state_of(store)
    assert Decimal(state['nav']) == Decimal('1.00000000')
    assert Decimal(state['peak']) == Decimal('1.00000000')


@pytest.mark.asyncio
async def test_halt_persists_holder_notice_and_fund_state(tmp_path):
    trader, store, ledger, client = make(tmp_path)
    now = boundary(BASE + 300 * H) + 20000
    store.put('funds', FUND, {
        'fund_id': FUND, 'state': 'active', 'wallet_units': 5_000_000,
        'position_value_units': 0, 'quote_ms': 0, 'updated_ms': 0,
        'shares_atoms': 5_000_000 * 100_000_000})
    await trader.tick(now)
    client.wallet_units = 3_750_000  # -25% trading loss
    await trader.tick(now + 4000)
    assert state_of(store)['halted'] is True
    notices = [info for _, info in store.list_items('notices')]
    assert any(info['fund_id'] == FUND for info in notices)
    fund = store.get('funds', FUND)
    assert fund['state'] == 'permanent_halt'
    # The mirror only writes ``state``; no other ledger field is reset.
    assert fund['wallet_units'] == 5_000_000
