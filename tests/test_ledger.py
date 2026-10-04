"""Financial invariant tests for :mod:`raricy_capital.ledger`.

These tests exercise the money-bearing paths only: example-month dividends,
cutoff snapshots, subscription receipts/refunds, the 20% redemption gate,
emergency fees/exemptions, reinvestment, replay safety and the NAV exclusion of
liabilities.  Other modules are not required.
"""
from datetime import datetime
from decimal import Decimal

import pytest

from raricy_capital.contracts import BEIJING, FundError
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore

U = 10_000          # money units per 1.0 fish
ATOMS = 100_000_000  # share atoms per 1.0 share
F = 'capital1'
G = 'capital2'


@pytest.fixture
def led(tmp_path):
    store = FundStore(tmp_path / 'fund.db')
    ledger = FundLedger(store)
    yield ledger
    store.close()


def ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BEIJING).timestamp() * 1000)


def mark(ledger, fish, when, fund=F):
    ledger.mark_account(fund, int(fish * U), 0, when, when)


def seed(ledger, user, fish, when, fund=F):
    return ledger.seed(fund, user, int(fish * U), when)


# --------------------------------------------------------------------- example
def test_example_month_dividends_match_plan(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 2000, t0)
    mark(led, 10000, t0)
    seed(led, 'others', 8000, t0)

    assert led.status(F)['nav'] == '1.00000000'
    assert led.status(F, 'inst')['user_shares_atoms'] == 2000 * ATOMS

    # Month 1: +1000 realised, NAV 1.1000, dividend 100 -> ex-div 1.0900
    led.trade_realized(F, 't1', 1000 * U, ms(2026, 1, 20))
    mark(led, 11000, ms(2026, 1, 20))
    mark(led, 11000, ms(2026, 1, 31, 23, 59, 55))
    r1 = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r1['status'] == 'settled'
    assert Decimal(r1['nav_before']) == Decimal('1.1')
    assert r1['dividend_units'] == 100 * U
    assert Decimal(r1['exdiv_nav']) == Decimal('1.09')
    div1 = {a['user_id']: a['dividend_units'] for a in r1['holder_allocations']}
    assert div1['inst'] == 20 * U and div1['others'] == 80 * U

    # Month 2: -500, NAV 1.0400, benchmark 1.0900 -> no dividend
    led.trade_realized(F, 't2', -500 * U, ms(2026, 2, 20))
    mark(led, 10500, ms(2026, 2, 20))
    mark(led, 10500, ms(2026, 2, 28, 23, 59, 55))
    r2 = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    assert Decimal(r2['nav_before']) == Decimal('1.04')
    assert r2['dividend_units'] == 0
    assert Decimal(r2['benchmark_after']) == Decimal('1.09')

    # Month 3: +300, NAV 1.0700 < benchmark -> still no dividend
    led.trade_realized(F, 't3', 300 * U, ms(2026, 3, 20))
    mark(led, 10800, ms(2026, 3, 20))
    mark(led, 10800, ms(2026, 3, 31, 23, 59, 55))
    r3 = led.settle_month(F, '2026-03', ms(2026, 4, 2))
    assert Decimal(r3['nav_before']) == Decimal('1.07')
    assert r3['dividend_units'] == 0

    # Month 4: +400, NAV 1.1100; only the 0.0200 above benchmark is distributable
    led.trade_realized(F, 't4', 400 * U, ms(2026, 4, 20))
    mark(led, 11200, ms(2026, 4, 20))
    mark(led, 11200, ms(2026, 4, 30, 23, 59, 55))
    r4 = led.settle_month(F, '2026-04', ms(2026, 5, 2))
    assert Decimal(r4['nav_before']) == Decimal('1.11')
    assert r4['distributable_units'] == 200 * U      # not the whole 400 profit
    assert r4['dividend_units'] == 20 * U
    assert Decimal(r4['exdiv_nav']) == Decimal('1.108')
    div4 = {a['user_id']: a['dividend_units'] for a in r4['holder_allocations']}
    assert div4['inst'] == 4 * U and div4['others'] == 16 * U


# ------------------------------------------------------------ cutoff / valuation
def test_settle_rejects_lookahead_and_stale_valuation(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    with pytest.raises(FundError) as exc:
        led.settle_month(F, '2026-01', ms(2026, 1, 20))
    assert exc.value.code == 'month_not_ended'

    # No fresh pre-cutoff mark: settlement must hang rather than fake a month-end price.
    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['status'] == 'pending_valuation'
    assert r['reason'] == 'stale_valuation'
    assert led.notices(status='queued')


def test_post_cutoff_flow_cannot_distort_settlement(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.trade_realized(F, 't1', 100 * U, ms(2026, 1, 6))
    mark(led, 1100, ms(2026, 1, 31, 23, 59, 55))

    # A new subscription arrives on Feb 1, after the Jan cutoff, before settlement.
    sub = led.create_subscription(F, 'u2', 500 * U, 'after-cutoff', ms(2026, 2, 1, 10))
    led.receive_transfer(F, {
        'transfer_id': 'feb1', 'from_user_id': 'u2', 'amount_units': 525 * U,
        'note': sub['payment_note'], 'occurred_ms': ms(2026, 2, 1, 10),
    }, ms(2026, 2, 1, 10))

    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['status'] == 'settled'
    # Registration was frozen at the cutoff: only the seeded holder exists.
    assert r['shares_at_cutoff_atoms'] == 1000 * ATOMS
    assert r['dividend_units'] == 10 * U
    assert [a['user_id'] for a in r['holder_allocations']] == ['inst']
    assert r['issued_external_shares_atoms'] == 0


# ------------------------------------------------------------- subscriptions
def test_late_receipt_refunds_in_full_without_shares(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 's1', tsub)
    assert sub['total_units'] == 105 * U
    assert sub['expires_ms'] == tsub + 180_000
    assert len(sub['payment_note']) <= 30

    r = led.receive_transfer(F, {
        'transfer_id': 'late1', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub + 200_000,
    }, tsub + 201_000)
    assert r['status'] == 'refunded'
    assert r['refund_payout_id'] is not None

    mark(led, 1105, tsub + 202_000)  # the late money did arrive in the wallet
    st = led.status(F)
    assert st['shares_atoms'] == 1000 * ATOMS  # no shares issued
    assert st['pending_receipts_units'] == 0
    assert st['liabilities_units'] == 105 * U
    assert st['nav'] == '1.00000000'
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1
    assert payouts[0]['kind'] == 'refund'
    assert payouts[0]['amount_units'] == 105 * U
    assert len(payouts[0]['idempotency_key']) <= 48


def test_timely_receipt_discovered_after_expiry_is_honoured(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 's2', tsub)

    # The local scanner only notices the payment well after the 180s TTL.
    led.expire_subscriptions(tsub + 10_000_000)
    r = led.receive_transfer(F, {
        'transfer_id': 'ontime1', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub + 1000,
    }, tsub + 10_000_001)
    assert r['status'] == 'received'  # judged by upstream arrival time


def test_settlement_issues_external_shares_exactly_once(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 1000 * U, 's3', tsub)
    led.receive_transfer(F, {
        'transfer_id': 'tx3', 'from_user_id': 'u2', 'amount_units': 1050 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub,
    }, tsub + 1000)
    mark(led, 2050, ms(2026, 1, 31, 23, 59, 55))

    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['issued_external_shares_atoms'] == 1000 * ATOMS
    st = led.status(F)
    assert st['pending_receipts_units'] == 0
    assert st['fee_balance_units'] == 50 * U      # institution fee excluded from NAV
    assert st['shares_atoms'] == 2000 * ATOMS
    assert st['nav'] == '1.00000000'
    assert led.status(F, 'u2')['user_shares_atoms'] == 1000 * ATOMS

    r2 = led.settle_month(F, '2026-01', ms(2026, 2, 3))
    assert r2 == r
    assert led.status(F)['shares_atoms'] == 2000 * ATOMS


def test_receipt_dedup_and_trade_replay(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    s1 = led.create_subscription(F, 'u2', 100 * U, 'dup', tsub)
    s2 = led.create_subscription(F, 'u2', 100 * U, 'dup', tsub + 5000)
    assert s1['id'] == s2['id']

    tx = {'transfer_id': 'dup1', 'from_user_id': 'u2', 'amount_units': 105 * U,
          'note': s1['payment_note'], 'occurred_ms': tsub}
    first = led.receive_transfer(F, tx, tsub + 1000)
    second = led.receive_transfer(F, tx, tsub + 2000)
    assert first['status'] == 'received'
    assert second['duplicated'] is True
    assert led.status(F)['pending_receipts_units'] == 100 * U

    led.trade_realized(F, 'tr', 10 * U, tsub + 3000)
    led.trade_realized(F, 'tr', 10 * U, tsub + 4000)
    assert led.status(F)['realized_profit_units'] == 10 * U


# --------------------------------------------------------------- redemptions
def test_ordinary_redemption_20pct_gate_and_double_reservation(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    mark(led, 1000, ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'r1', ms(2026, 1, 6))
    assert req['shares_reserved_atoms'] == 500 * ATOMS

    # Cannot reserve the same shares again (1000 held, 500 already reserved).
    with pytest.raises(FundError) as exc:
        led.request_redemption(F, 'inst', 600 * U, 'ordinary', 'r2', ms(2026, 1, 6))
    assert exc.value.code == 'insufficient_shares'

    mark(led, 1000, ms(2026, 1, 31, 23, 59, 55))
    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['shares_at_cutoff_atoms'] == 1000 * ATOMS
    assert r['redeemed_shares_atoms'] == 200 * ATOMS      # 20% cap of old shares
    holder = led.holder(F, 'inst')
    assert holder['shares_atoms'] == 800 * ATOMS
    assert holder['reserved_atoms'] == 300 * ATOMS        # remainder carried
    carried = led.store.get('redemptions', req['id'])
    assert carried['status'] == 'pending'
    assert carried['period'] == '2026-02'
    payout = led.pending_payouts(F)[0]
    assert payout['kind'] == 'redemption'
    assert payout['amount_units'] == 200 * U


def test_emergency_fee_stays_in_fund_and_lifts_benchmark(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 1000, t0)
    seed(led, 'others', 1000, t0)

    mark(led, 2000, ms(2026, 1, 10, 17))
    req = led.request_redemption(F, 'others', 100 * U, 'emergency', 'e1', ms(2026, 1, 10, 17))
    assert req['ready_ms'] == ms(2026, 1, 10, 20)

    mark(led, 2000, ms(2026, 1, 10, 19, 59, 55))
    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))
    assert res['status'] == 'settled'
    s = res['settlements'][0]
    assert s['gross_units'] == 100 * U
    assert s['fee_units'] == 10 * U       # 10% retained by the fund
    assert s['payout_units'] == 90 * U

    st = led.status(F)
    assert st['shares_atoms'] == 1900 * ATOMS
    assert st['liabilities_units'] == 90 * U
    assert Decimal(st['nav']) > Decimal('1')
    # The retained fee is non-investment income, so NAV equals the lifted benchmark
    # instead of creating phantom distributable profit.
    assert abs(Decimal(st['nav']) - Decimal(st['benchmark_nav'])) < Decimal('1e-7')


def test_emergency_exemption_waives_fee(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 1000, t0)
    seed(led, 'others', 1000, t0)

    mark(led, 2000, ms(2026, 1, 10, 17))
    led.request_redemption(F, 'others', 100 * U, 'emergency', 'e2',
                           ms(2026, 1, 10, 17), exempt=True)
    mark(led, 2000, ms(2026, 1, 10, 19, 59, 55))
    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30), exempt=True)
    s = res['settlements'][0]
    assert s['fee_units'] == 0
    assert s['payout_units'] == 100 * U
    assert led.status(F)['benchmark_nav'] == '1.00000000'


# ------------------------------------------------------------- reinvest / NAV
def test_dividend_reinvest_issues_shares_instead_of_cash(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.set_dividend_choice(F, 'inst', 1)

    led.trade_realized(F, 't1', 100 * U, ms(2026, 1, 20))
    mark(led, 1100, ms(2026, 1, 20))
    mark(led, 1100, ms(2026, 1, 31, 23, 59, 55))
    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))

    assert r['cash_dividend_units'] == 0
    assert r['reinvest_value_units'] == 10 * U
    assert r['issued_reinvest_shares_atoms'] == 917_431_192
    holder = led.holder(F, 'inst')
    assert holder['shares_atoms'] == 1000 * ATOMS + 917_431_192
    assert Decimal(r['exdiv_nav']) == Decimal('1.09')
    assert abs(Decimal(led.status(F)['nav']) - Decimal('1.09')) < Decimal('1e-6')
    assert led.pending_payouts(F) == []


def test_nav_excludes_liabilities_pending_and_fees(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    assert led.status(F)['nav'] == '1.00000000'

    # A pending receipt and its institution fee sit in the wallet but not the fund.
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 's9', tsub)
    led.receive_transfer(F, {
        'transfer_id': 'tx9', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub,
    }, tsub + 1000)
    mark(led, 1105, tsub + 2000)
    st = led.status(F)
    assert st['wallet_units'] == 1105 * U
    assert st['pending_receipts_units'] == 100 * U
    assert st['fee_balance_units'] == 5 * U
    assert st['equity_units'] == 1000 * U
    assert st['nav'] == '1.00000000'


def test_unknown_fund_and_bad_amounts_rejected(led):
    with pytest.raises(FundError) as exc:
        led.status('nope')
    assert exc.value.code == 'invalid_fund'
    assert led.status(G)['fund_id'] == G  # both frozen policies are valid
    with pytest.raises(FundError):
        led.mark_account(F, -1, 0, ms(2026, 1, 5), ms(2026, 1, 5))
    with pytest.raises(FundError):
        led.seed(F, 'inst', 1000 * U, ms(2026, 1, 5))  # no assets on account yet


# ------------------------------------------------------- v0.3 settlement rules
def test_seed_repeat_cannot_remint_already_allocated_cash(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    with pytest.raises(FundError) as exc:
        led.seed(F, 'other', 500 * U, t0)
    assert exc.value.code == 'insufficient_liquidity'
    assert led.status(F)['shares_atoms'] == 1000 * ATOMS


def test_shareless_fund_issues_first_subscription_at_nav_one(led):
    t = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 'first', t)
    led.receive_transfer(F, {
        'transfer_id': 'first-tx', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': t,
    }, t + 1000)
    mark(led, 105, ms(2026, 1, 31, 23, 59, 55))

    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['issued_external_shares_atoms'] == 100 * ATOMS
    st = led.status(F)
    assert st['shares_atoms'] == 100 * ATOMS
    assert st['pending_receipts_units'] == 0
    assert st['fee_balance_units'] == 5 * U
    assert st['nav'] == '1.00000000'


def test_month_profit_gate_blocks_losing_month_above_benchmark(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    # Month 1 is a pure mark gain: nothing is distributable (no realised profit)
    # and the benchmark stays at 1.0000 while month 2 starts at NAV 1.1000.
    mark(led, 1100, ms(2026, 1, 31, 23, 59, 55))
    r1 = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r1['dividend_units'] == 0
    assert Decimal(led.status(F)['benchmark_nav']) == Decimal('1')

    # Month 2 loses 0.05 NAV but is still above the benchmark: R/H alone would pay
    # a dividend, the month's own negative investment profit must block it.
    led.trade_realized(F, 'm2', 50 * U, ms(2026, 2, 20))
    mark(led, 1050, ms(2026, 2, 28, 23, 59, 55))
    r2 = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    assert Decimal(r2['nav_before']) == Decimal('1.05')
    assert Decimal(r2['month_profit_per_share']) < 0
    assert r2['distributable_units'] == 0
    assert r2['dividend_units'] == 0


def test_retriable_pending_period_completes_with_a_pre_cutoff_mark(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    first = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert first['status'] == 'pending_valuation'
    assert len(led.notices(status='queued')) == 1

    # A late-delivered mark whose quote is genuinely pre-cutoff completes the
    # frozen valuation; the retry settles without a duplicate notice.
    led.mark_account(F, 1100 * U, 0, ms(2026, 2, 2, 0, 30), ms(2026, 1, 31, 23, 59, 55))
    again = led.settle_month(F, '2026-01', ms(2026, 2, 2, 1))
    assert again['status'] == 'settled'
    assert Decimal(again['nav_before']) == Decimal('1.1')
    assert len(led.notices(status='queued')) == 1


def test_post_cutoff_quote_cannot_price_the_month_end(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    assert led.settle_month(F, '2026-01', ms(2026, 2, 2))['status'] == 'pending_valuation'

    # A fresh *February* quote must never be used as the January cutoff price.
    led.mark_account(F, 1200 * U, 0, ms(2026, 2, 2, 10), ms(2026, 2, 2, 10))
    r = led.settle_month(F, '2026-01', ms(2026, 2, 2, 11))
    assert r['status'] == 'pending_valuation'
    assert r['reason'] == 'stale_valuation'


# ----------------------------------------------------------- emergency batches
def test_insufficient_emergency_cash_defers_batch_without_charge(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 10, 17)
    led.mark_account(F, 100 * U, 900 * U, t, t)      # NAV 1.0, but only 100 of cash
    led.request_redemption(F, 'inst', 500 * U, 'emergency', 'ins', t)
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 10, 19, 59, 55), ms(2026, 1, 10, 19, 59, 55))

    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))
    assert res['status'] == 'deferred'
    assert led.pending_payouts(F) == []
    holder = led.holder(F, 'inst')
    assert holder['shares_atoms'] == 1000 * ATOMS
    assert holder['reserved_atoms'] == 500 * ATOMS
    assert led.store.get('redemptions', led.orders(F, 'inst')[0]['id'])['status'] == 'pending'

    # Once the cash is raised the same batch executes at its frozen 20:00 price.
    led.mark_account(F, 600 * U, 400 * U, ms(2026, 1, 10, 21), ms(2026, 1, 10, 21))
    res2 = led.settle_emergency(F, ms(2026, 1, 10, 21, 5))
    assert res2['status'] == 'settled'
    assert led.pending_payouts(F)[0]['amount_units'] == 450 * U


def test_partial_emergency_execution_needs_explicit_consent(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 10, 17)
    led.mark_account(F, 100 * U, 900 * U, t, t)
    led.request_redemption(F, 'inst', 500 * U, 'emergency', 'part', t)
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 10, 19, 59, 55), ms(2026, 1, 10, 19, 59, 55))

    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30), allow_partial=True)
    assert res['status'] == 'settled'
    s = res['settlements'][0]
    assert 0 < s['shares_atoms'] < 500 * ATOMS
    assert s['payout_units'] == 100 * U
    holder = led.holder(F, 'inst')
    assert holder['reserved_atoms'] > 0        # remainder carried, never charged
    assert len(led.pending_payouts(F)) == 1


def test_emergency_fee_tracked_apart_from_trading_capital(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 1000, t0)
    seed(led, 'others', 1000, t0)
    t = ms(2026, 1, 10, 17)
    mark(led, 2000, t)
    led.request_redemption(F, 'others', 100 * U, 'emergency', 'nt', t)
    mark(led, 2000, ms(2026, 1, 10, 19, 59, 55))
    s = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))['settlements'][0]

    st = led.status(F)
    assert s['fee_units'] == 10 * U
    assert st['non_trading_income_units'] == 10 * U
    # Trading capital leaves at the gross value so the trader can normalise by
    # capital_flows + non_trading_income.
    assert st['capital_flows_units'] == 2000 * U - 100 * U


def test_permanent_halt_emergency_exit_waives_fee(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 1000, t0)
    seed(led, 'others', 1000, t0)
    t = ms(2026, 1, 10, 17)
    mark(led, 2000, t)
    assert led.state(F) == 'active'
    led.state(F, 'permanent_halt', t)
    assert led.state(F) == 'permanent_halt'

    led.request_redemption(F, 'others', 100 * U, 'emergency', 'ph', t)
    mark(led, 2000, ms(2026, 1, 10, 19, 59, 55))
    s = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))['settlements'][0]
    assert s['fee_units'] == 0
    assert s['payout_units'] == 100 * U


# ---------------------------------------------------------------- cancellation
def test_cancel_received_subscription_refunds_principal_and_fee(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 'cancelsub', tsub)
    led.receive_transfer(F, {
        'transfer_id': 'tx-cancel', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub,
    }, tsub + 1000)
    mark(led, 1105, tsub + 2000)

    out = led.cancel_order(F, 'u2', sub['id'], tsub + 3000)
    assert out['status'] == 'cancelled'
    st = led.status(F)
    assert st['pending_receipts_units'] == 0
    assert st['fee_balance_units'] == 0
    assert st['equity_units'] == 1000 * U
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1
    assert payouts[0]['kind'] == 'refund'
    assert payouts[0]['amount_units'] == 105 * U


def test_cancel_redemption_releases_reserved_shares(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 6)
    mark(led, 1000, t)
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'cancelred', t)

    out = led.cancel_order(F, 'inst', req['id'], t + 1000)
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['reserved_atoms'] == 0
    assert holder['shares_atoms'] == 1000 * ATOMS
    assert led.pending_payouts(F) == []


def test_cancel_refuses_crossed_deadline_and_settled_orders(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 6)
    mark(led, 1000, t)

    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'latecancel', t)
    with pytest.raises(FundError) as exc:
        led.cancel_order(F, 'inst', req['id'], ms(2026, 2, 1))
    assert exc.value.code == 'deadline_passed'
    assert led.holder(F, 'inst')['reserved_atoms'] == 500 * ATOMS

    sub = led.create_subscription(F, 'u2', 100 * U, 'setsub', t)
    led.receive_transfer(F, {
        'transfer_id': 'tx-set', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': t,
    }, t + 1000)
    mark(led, 1105, ms(2026, 1, 31, 23, 59, 55))
    led.settle_month(F, '2026-01', ms(2026, 2, 2))
    with pytest.raises(FundError) as exc:
        led.cancel_order(F, 'u2', sub['id'], ms(2026, 2, 2, 1))
    assert exc.value.code == 'order_settled'
    assert led.status(F, 'u2')['user_shares_atoms'] == 100 * ATOMS


# ------------------------------------------------- v0.3 dividend choice window
def test_dividend_choice_deadline_binds_to_period(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    # Before the 25th 18:00 deadline -> January; after it -> February.
    c1 = led.set_dividend_choice(F, 'inst', Decimal('0.25'), ms(2026, 1, 20))
    assert c1['effective_period'] == '2026-01'
    c2 = led.set_dividend_choice(F, 'inst', 1, ms(2026, 1, 26))
    assert c2['effective_period'] == '2026-02'

    # January: +100 realised, NAV 1.1000, dividend 100; the in-time 25% choice
    # applies even though a later choice already exists.
    led.trade_realized(F, 'dj1', 100 * U, ms(2026, 1, 20))
    mark(led, 1100, ms(2026, 1, 31, 23, 59, 55))
    jan = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert jan['status'] == 'settled'
    assert jan['dividend_units'] == 10 * U
    assert jan['reinvest_value_units'] == 25_000        # 25% of 10*U
    assert jan['cash_dividend_units'] == 75_000

    # Replaying the settled month cannot be rewritten by the February choice.
    replay = led.settle_month(F, '2026-01', ms(2026, 2, 3))
    assert replay == jan

    # February now uses the full-reinvest choice set after January's deadline.
    led.trade_realized(F, 'dj2', 200 * U, ms(2026, 2, 20))
    mark(led, 1400, ms(2026, 2, 28, 23, 59, 55))
    feb = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    assert feb['status'] == 'settled'
    assert feb['cash_dividend_units'] == 0
    assert feb['reinvest_value_units'] == feb['dividend_units'] > 0


def test_multiple_post_deadline_revisions_do_not_rewrite_earlier_period(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)

    led.set_dividend_choice(F, 'inst', Decimal('0.25'), ms(2026, 1, 20))
    c2 = led.set_dividend_choice(F, 'inst', 1, ms(2026, 1, 26))
    c3 = led.set_dividend_choice(F, 'inst', Decimal('0.5'), ms(2026, 1, 28))
    assert c2['effective_period'] == '2026-02'
    assert c3['effective_period'] == '2026-02'   # same period: latest value wins

    record = led.store.get('dividend_choices', f'{F}:inst')
    assert [(r['effective_period'], r['reinvest_fraction']) for r in record['revisions']] == [
        ('2026-01', '0.25'), ('2026-02', '0.5')]
    # January keeps its own applicable value; the revision persists to later months.
    assert led._dividend_fraction(F, 'inst', '2026-01') == Decimal('0.25')
    assert led._dividend_fraction(F, 'inst', '2026-02') == Decimal('0.5')
    assert led._dividend_fraction(F, 'inst', '2026-03') == Decimal('0.5')
    assert led._dividend_fraction(F, 'inst', '2025-12') == Decimal('0')


def test_dividend_choice_after_month_end_cannot_rewrite_delayed_period(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.set_dividend_choice(F, 'inst', Decimal('0.5'), ms(2026, 1, 20))
    led.trade_realized(F, 'dly1', 100 * U, ms(2026, 1, 20))

    # January hangs on a stale valuation; its applicable choice is already fixed.
    first = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert first['status'] == 'pending_valuation'

    # A revision after month-end but before the delayed settlement only governs
    # February onward and must not rewrite January.
    led.set_dividend_choice(F, 'inst', 1, ms(2026, 2, 1))
    led.mark_account(F, 1100 * U, 0, ms(2026, 2, 2, 0, 30), ms(2026, 1, 31, 23, 59, 55))
    jan = led.settle_month(F, '2026-01', ms(2026, 2, 2, 1))
    assert jan['status'] == 'settled'
    assert jan['reinvest_value_units'] == 5 * U
    assert jan['cash_dividend_units'] == 5 * U


# --------------------------------------- v0.3 deferred emergency cancellation
def test_liquidity_deferred_emergency_cancellable_after_deadline(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 10, 17)
    led.mark_account(F, 100 * U, 900 * U, t, t)
    req = led.request_redemption(F, 'inst', 500 * U, 'emergency', 'defercancel', t)
    led.mark_account(F, 100 * U, 900 * U,
                     ms(2026, 1, 10, 19, 59, 55), ms(2026, 1, 10, 19, 59, 55))

    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))
    assert res['status'] == 'deferred'
    assert req['deadline_ms'] == ms(2026, 1, 10, 18)

    # The batch is past its 18:00 deadline but executed nothing, so the holder may
    # still reclaim the unexecuted reserved shares.
    out = led.cancel_order(F, 'inst', req['id'], ms(2026, 1, 10, 21))
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['shares_atoms'] == 1000 * ATOMS    # shares unchanged
    assert holder['reserved_atoms'] == 0             # reservation released
    assert led.pending_payouts(F) == []              # no payout and no fee
    assert led.status(F)['non_trading_income_units'] == 0
    after = led.settle_emergency(F, ms(2026, 1, 10, 22))
    assert after['status'] == 'nothing_due'
    assert after['settlements'] == []


def test_carried_emergency_remainder_has_next_deadline_and_keeps_executed_part(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    t = ms(2026, 1, 10, 17)
    led.mark_account(F, 100 * U, 900 * U, t, t)
    req = led.request_redemption(F, 'inst', 500 * U, 'emergency', 'partcarry', t)
    led.mark_account(F, 100 * U, 900 * U,
                     ms(2026, 1, 10, 19, 59, 55), ms(2026, 1, 10, 19, 59, 55))

    res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30), allow_partial=True)
    assert res['status'] == 'settled'
    carried = led.store.get('redemptions', req['id'])
    assert carried['status'] == 'pending'
    assert carried['shares_confirmed_atoms'] > 0        # executed slice kept
    assert carried['shares_reserved_atoms'] > 0         # remainder rolled forward
    assert 'liquidity_deferred' not in carried
    # Next batch's own 18:00 cutoff is the remainder's new deadline.
    assert carried['deadline_ms'] == ms(2026, 1, 11, 18)

    out = led.cancel_order(F, 'inst', req['id'], ms(2026, 1, 11, 12))
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['reserved_atoms'] == 0
    assert holder['shares_atoms'] < 1000 * ATOMS        # executed shares stay gone
    assert len(led.pending_payouts(F)) == 1             # executed payout untouched


# ------------------- rule conformance: rolled ordinary redemption deadline
def test_carried_ordinary_remainder_gets_next_deadline_and_cancel_keeps_confirmed(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    mark(led, 1000, ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 1000 * U, 'ordinary', 'carry1', ms(2026, 1, 6))

    mark(led, 1000, ms(2026, 1, 31, 23, 59, 55))
    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['redeemed_shares_atoms'] == 200 * ATOMS    # 20% cap of the 1000 old shares

    carried = led.store.get('redemptions', req['id'])
    assert carried['status'] == 'pending'
    assert carried['shares_confirmed_atoms'] == 200 * ATOMS
    assert carried['shares_reserved_atoms'] == 800 * ATOMS
    # §6.4 lets the deferred part be withdrawn: it carries the next period's own
    # 7th 18:00 deadline, with period and batch kept consistent.
    assert carried['period'] == '2026-02'
    assert carried['batch'] == '2026-02'
    assert carried['deadline_ms'] == ms(2026, 2, 7, 18)
    payout = led.pending_payouts(F)[0]
    assert payout['kind'] == 'redemption' and payout['amount_units'] == 200 * U

    out = led.cancel_order(F, 'inst', req['id'], ms(2026, 2, 6))
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['reserved_atoms'] == 0                 # only the 800 remainder released
    assert holder['shares_atoms'] == 800 * ATOMS
    after = led.store.get('redemptions', req['id'])
    assert after['shares_confirmed_atoms'] == 200 * ATOMS  # confirmed part kept
    assert after['payout_id'] == payout['id']
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1 and payouts[0]['amount_units'] == 200 * U


# ---------------- rule conformance: payment after the window rolls issuance
def test_subscription_paid_just_after_window_rolls_period_and_stays_cancellable(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    created = ms(2026, 1, 7, 17, 59, 30)
    sub = led.create_subscription(F, 'u2', 100 * U, 'rollsub', created)
    assert sub['period'] == '2026-01'
    assert sub['deadline_ms'] == ms(2026, 1, 7, 18)

    occurred = ms(2026, 1, 7, 18, 0, 30)
    assert occurred - created < 180_000                 # still within QR validity
    discovered = ms(2026, 1, 8, 10)                    # scanner notices it next day
    r = led.receive_transfer(F, {
        'transfer_id': 'roll-tx', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': occurred,
    }, discovered)
    assert r['status'] == 'received'
    stored = led.store.get('subscriptions', sub['id'])
    assert stored['occurred_ms'] == occurred            # authority is arrival time
    assert stored['period'] == '2026-02'                # issuance rolls forward
    assert stored['deadline_ms'] == ms(2026, 2, 7, 18)

    rollover = [n for n in led.notices() if n['id'].startswith('sub-rollover:')]
    assert len(rollover) == 1
    again = led.receive_transfer(F, {
        'transfer_id': 'roll-tx', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': occurred,
    }, discovered + 1000)
    assert again['duplicated'] is True
    assert len([n for n in led.notices() if n['id'].startswith('sub-rollover:')]) == 1

    mark(led, 1105, discovered + 5000)
    assert led.status(F)['pending_receipts_units'] == 100 * U
    out = led.cancel_order(F, 'u2', sub['id'], ms(2026, 1, 8, 12))
    assert out['status'] == 'cancelled'
    st = led.status(F)
    assert st['pending_receipts_units'] == 0
    assert st['fee_balance_units'] == 0
    assert st['shares_atoms'] == 1000 * ATOMS           # no shares issued
    assert st['equity_units'] == 1000 * U               # accounting conserved
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1
    assert payouts[0]['kind'] == 'refund' and payouts[0]['amount_units'] == 105 * U


# ----------------- rule conformance: cash-short settlement is not silent
def test_cash_short_dividend_defers_with_one_stable_notice(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.trade_realized(F, 'cs-div', 100 * U, ms(2026, 1, 20))
    # Equity 1100 but only 5 of it is spendable cash: the dividend is eligible yet
    # cannot be paid alongside the (here empty) confirmed queue.
    led.mark_account(F, 5 * U, 1095 * U, ms(2026, 1, 31, 23, 59, 55),
                     ms(2026, 1, 31, 23, 59, 55))

    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert r['distributable_units'] == 100 * U          # otherwise eligible
    assert r['planned_dividend_units'] == 0
    assert r['dividend_units'] == 0
    key = f'settle-dividend-short:{F}:2026-01'
    msgs = [n for n in led.notices() if n['id'] == key]
    assert len(msgs) == 1 and msgs[0]['user_id'] is None
    assert '现金' in msgs[0]['content'] and '2026-01' in msgs[0]['content']
    assert led.pending_payouts(F) == []                 # nothing booked for the dividend

    replay = led.settle_month(F, '2026-01', ms(2026, 2, 3))
    assert replay == r
    assert len([n for n in led.notices() if n['id'] == key]) == 1


def test_cash_short_redemption_scaling_notice_stable_and_amount_unchanged(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 6), ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'cs-red', ms(2026, 1, 6))
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 31, 23, 59, 55),
                     ms(2026, 1, 31, 23, 59, 55))

    r = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    # 20% cap confirms 200 shares, then the 100 of cash scales that to 100 shares.
    assert r['redeemed_shares_atoms'] == 100 * ATOMS
    assert r['redeem_value_units'] == 100 * U
    carried = led.store.get('redemptions', req['id'])
    assert carried['cash_scaled'] is True
    assert carried['shares_confirmed_atoms'] == 100 * ATOMS
    assert carried['shares_reserved_atoms'] == 400 * ATOMS
    assert carried['period'] == '2026-02' and carried['batch'] == '2026-02'
    assert carried['deadline_ms'] == ms(2026, 2, 7, 18)

    key = f'settle-redeem-short:{F}:2026-01:{req["id"]}'
    msgs = [n for n in led.notices() if n['id'] == key]
    assert len(msgs) == 1 and msgs[0]['user_id'] == 'inst'
    assert '现金' in msgs[0]['content'] and '顺延' in msgs[0]['content']
    assert [p['amount_units'] for p in led.pending_payouts(F)] == [100 * U]

    replay = led.settle_month(F, '2026-01', ms(2026, 2, 3))
    assert replay == r
    assert len([n for n in led.notices() if n['id'] == key]) == 1
    assert [p['amount_units'] for p in led.pending_payouts(F)] == [100 * U]


def test_cash_short_redemption_notice_carries_period_across_months(led):
    """A carried request that is cash-short twice gets one notice per period."""
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 6), ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'cs-multi', ms(2026, 1, 6))
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 31, 23, 59, 55),
                     ms(2026, 1, 31, 23, 59, 55))

    jan = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert jan['redeemed_shares_atoms'] == 100 * ATOMS
    jan_key = f'settle-redeem-short:{F}:2026-01:{req["id"]}'
    jan_notice = led.store.get('notices', jan_key)
    assert jan_notice is not None and '2026-01' in jan_notice['content']
    payout = led.pending_payouts(F)[0]
    assert payout['amount_units'] == 100 * U
    led.mark_payout_paid(payout['id'], 'tx-jan', ms(2026, 2, 5))

    # February is cash-short again for the same carried remainder, so a distinct
    # notice keyed by the *February* period must be produced.
    led.mark_account(F, 50 * U, 950 * U, ms(2026, 2, 28, 23, 59, 55),
                     ms(2026, 2, 28, 23, 59, 55))
    feb = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    carried = led.store.get('redemptions', req['id'])
    assert carried['cash_scaled'] is True
    assert carried['shares_confirmed_atoms'] > 0
    assert carried['shares_reserved_atoms'] > 0
    assert carried['period'] == '2026-03'
    feb_key = f'settle-redeem-short:{F}:2026-02:{req["id"]}'
    assert feb_key != jan_key
    feb_notice = led.store.get('notices', feb_key)
    assert feb_notice is not None and '2026-02' in feb_notice['content']
    assert led.store.get('notices', jan_key)['content'] == jan_notice['content']
    # Replaying February produces no third notice and no extra payout.
    payouts_before = len(led.pending_payouts(F))
    assert led.settle_month(F, '2026-02', ms(2026, 3, 3)) == feb
    assert led.store.get('notices', feb_key) is not None
    assert len(led.pending_payouts(F)) == payouts_before
    assert feb['redeemed_shares_atoms'] > 0


# ------------- rule conformance: retained fee must not fake a winning month
def test_retained_emergency_fee_does_not_create_a_positive_investment_month(led):
    t0 = ms(2026, 1, 5)
    mark(led, 2000, t0)
    seed(led, 'inst', 1000, t0)
    seed(led, 'others', 1000, t0)

    # January is a pure mark gain: no realised profit, so no dividend; the
    # benchmark stays 1.0 while February starts at NAV 1.05.
    mark(led, 2100, ms(2026, 1, 31, 23, 59, 55))
    jan = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert jan['dividend_units'] == 0
    assert Decimal(led.status(F)['period_start_nav']) == Decimal('1.05')

    # February realises a net +70 but its marks give ground; the large emergency
    # exit retains a fee whose per-share uplift is bigger than the month's loss.
    led.trade_realized(F, 'feb-win', 100 * U, ms(2026, 2, 10))
    led.trade_realized(F, 'feb-loss', -30 * U, ms(2026, 2, 10))
    t = ms(2026, 2, 10, 17)
    led.mark_account(F, 2010 * U, 0, t, t)              # NAV 1.0050, equity 2010
    led.request_redemption(F, 'others', 1005 * U, 'emergency', 'fee-gate', t)
    led.mark_account(F, 2010 * U, 0, ms(2026, 2, 10, 19, 59, 55),
                     ms(2026, 2, 10, 19, 59, 55))

    res = led.settle_emergency(F, ms(2026, 2, 10, 20, 30))
    assert res['status'] == 'settled'
    assert res['settlements'][0]['fee_units'] == 1005000        # 100.50 retained
    assert res['settlements'][0]['payout_units'] == 9045000     # 90.45 paid
    st = led.status(F)
    assert st['non_trading_income_units'] == 1005000
    assert st['capital_flows_units'] == 2000 * U - 1005 * U     # gross leaves
    # Both baselines carry the same per-share uplift.
    assert Decimal(st['benchmark_nav']) == Decimal('1.1005')
    assert Decimal(st['period_start_nav']) == Decimal('1.1505')

    mark(led, 2010, ms(2026, 2, 28, 23, 59, 55))
    feb = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    assert Decimal(feb['nav_before']) == Decimal('1.1055')
    # The fee uplift is excluded from the month's own profit, so the losing month
    # cannot gate a dividend through the benchmark test.
    assert Decimal(feb['month_profit_per_share']) < 0
    assert feb['distributable_units'] == 0
    assert feb['dividend_units'] == 0


# --------------- rule conformance: unresolved valuation must not lock requests
def test_cancel_unexecuted_ordinary_redemption_when_period_pending_valuation(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.mark_account(F, 1000 * U, 0, ms(2026, 1, 6), ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'pv-red', ms(2026, 1, 6))

    # No genuinely pre-cutoff month-end quote: January stays unpriceable.
    pending = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert pending['status'] == 'pending_valuation'
    assert pending['reason'] == 'stale_valuation'
    assert led.pending_payouts(F) == []

    # The 7th deadline is long past, but nothing executed: the unexecuted
    # reservation must still be reclaimable.
    out = led.cancel_order(F, 'inst', req['id'], ms(2026, 2, 10))
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['shares_atoms'] == 1000 * ATOMS   # no share ever cancelled
    assert holder['reserved_atoms'] == 0            # reservation released
    assert out['shares_confirmed_atoms'] == 0       # nothing was confirmed
    assert led.pending_payouts(F) == []             # and nothing paid
    # A healthy month still enforces its own deadline.
    assert led._period_pending_valuation(F, '2026-01') is True
    assert led._period_pending_valuation(F, '2026-02') is False


def test_pending_valuation_cancel_keeps_confirmed_shares_and_past_payout(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 6), ms(2026, 1, 6))
    req = led.request_redemption(F, 'inst', 500 * U, 'ordinary', 'pv-carry', ms(2026, 1, 6))
    led.mark_account(F, 100 * U, 900 * U, ms(2026, 1, 31, 23, 59, 55),
                     ms(2026, 1, 31, 23, 59, 55))

    jan = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert jan['redeemed_shares_atoms'] == 100 * ATOMS
    payout = led.pending_payouts(F)[0]

    # The carried remainder's February is unpriceable too.
    feb = led.settle_month(F, '2026-02', ms(2026, 3, 2))
    assert feb['status'] == 'pending_valuation'

    out = led.cancel_order(F, 'inst', req['id'], ms(2026, 3, 10))
    assert out['status'] == 'cancelled'
    holder = led.holder(F, 'inst')
    assert holder['reserved_atoms'] == 0               # only the remainder released
    assert holder['shares_atoms'] == 900 * ATOMS       # confirmed shares stay gone
    after = led.store.get('redemptions', req['id'])
    assert after['shares_confirmed_atoms'] == 100 * ATOMS
    assert after['payout_id'] == payout['id']
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1 and payouts[0]['amount_units'] == 100 * U


def test_received_subscription_refunded_once_while_period_pending_valuation(led):
    t0 = ms(2026, 1, 5)
    mark(led, 1000, t0)
    seed(led, 'inst', 1000, t0)
    tsub = ms(2026, 1, 6)
    sub = led.create_subscription(F, 'u2', 100 * U, 'pv-sub', tsub)
    received = led.receive_transfer(F, {
        'transfer_id': 'pv-sub-tx', 'from_user_id': 'u2', 'amount_units': 105 * U,
        'note': sub['payment_note'], 'occurred_ms': tsub,
    }, tsub + 1000)
    assert received['status'] == 'received'
    mark(led, 1105, tsub + 2000)

    pending = led.settle_month(F, '2026-01', ms(2026, 2, 2))
    assert pending['status'] == 'pending_valuation'

    # Paid but unissued, and its month cannot be priced: refund principal + fee
    # in full even though the 7th 18:00 window has long closed.
    out = led.cancel_order(F, 'u2', sub['id'], ms(2026, 2, 10))
    assert out['status'] == 'cancelled'
    st = led.status(F)
    assert st['pending_receipts_units'] == 0
    assert st['fee_balance_units'] == 0
    assert st['shares_atoms'] == 1000 * ATOMS
    assert st['equity_units'] == 1000 * U
    payouts = led.pending_payouts(F)
    assert len(payouts) == 1
    assert payouts[0]['kind'] == 'refund' and payouts[0]['amount_units'] == 105 * U

    again = led.cancel_order(F, 'u2', sub['id'], ms(2026, 2, 11))
    assert again['status'] == 'cancelled'
    assert len(led.pending_payouts(F)) == 1            # refunded exactly once
