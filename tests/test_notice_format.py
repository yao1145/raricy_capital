"""Investor-facing notice text: amounts must read as fish/shares, not raw integers.

The four money-bearing public notices used to interpolate stored 1e-4 unit counts
(or 1e-8 share atoms) directly, so an investor was told ``分红 100000`` for a
10.0000-fish dividend.  These cases drive the real :class:`FundLedger` over a
temporary :class:`FundStore` and assert both the human-readable notice and the
underlying integer accounting, so a formatting change can never hide a wrong
amount.  Financial rules, precision and payout values are deliberately untouched.
"""
from datetime import datetime

from raricy_capital.contracts import BEIJING
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore

U = 10_000          # money units per 1.0 fish
ATOMS = 100_000_000  # share atoms per 1.0 share
F = 'capital1'


def ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BEIJING).timestamp() * 1000)


def mark(ledger, fish, when, fund=F):
    ledger.mark_account(fund, int(fish * U), 0, when, when)


def seed(ledger, user, fish, when, fund=F):
    return ledger.seed(fund, user, int(fish * U), when)


def notices_with(ledger, needle, fund=F):
    return [n for n in ledger.notices(fund_id=fund) if needle in n['content']]


# ---------------------------------------------------------------- month dividend
def test_month_dividend_notice_names_fish_not_units(tmp_path):
    store = FundStore(tmp_path / 'fund.db')
    led = FundLedger(store)
    try:
        t0 = ms(2026, 1, 5)
        mark(led, 1000, t0)
        seed(led, 'inst', 1000, t0)
        # +100 realised, NAV 1.1000 -> eligible dividend 10 fish (0.01/share)
        led.trade_realized(F, 'nf-div', 100 * U, ms(2026, 1, 20))
        mark(led, 1100, ms(2026, 1, 20))
        # Period 2026-01 now settles at the 2026-02-07 20:00 batch, so the frozen
        # valuation must be a fresh pre-cutoff quote (within the 15s age limit).
        mark(led, 1100, ms(2026, 2, 7, 19, 59, 55))

        r = led.settle_month(F, '2026-01', ms(2026, 2, 7, 20))
        assert r['status'] == 'settled'
        assert r['dividend_units'] == 10 * U          # still stored as integer units

        msgs = notices_with(led, '分红')
        assert len(msgs) == 1
        assert msgs[0]['user_id'] is None
        assert '分红 10.0000 小鱼干' in msgs[0]['content']
        assert f'分红 {10 * U}' not in msgs[0]['content']   # never the raw 100000

        # The announcement matches what is actually booked for holders.
        assert [(p['kind'], p['amount_units']) for p in led.pending_payouts(F)] == [
            ('dividend', 10 * U)]
        assert led.status(F)['liabilities_units'] == 10 * U
    finally:
        store.close()


# -------------------------------------------------------------- emergency exit
def test_emergency_notice_reports_shares_fee_and_payout_in_human_units(tmp_path):
    store = FundStore(tmp_path / 'fund.db')
    led = FundLedger(store)
    try:
        t0 = ms(2026, 1, 5)
        mark(led, 1000, t0)
        seed(led, 'inst', 1000, t0)

        t = ms(2026, 1, 10, 17)
        mark(led, 1000, t)
        led.request_redemption(F, 'inst', 100 * U, 'emergency', 'nf-em', t)
        mark(led, 1000, ms(2026, 1, 10, 19, 59, 55))

        res = led.settle_emergency(F, ms(2026, 1, 10, 20, 30))
        assert res['status'] == 'settled'
        s = res['settlements'][0]
        assert s['shares_atoms'] == 100 * ATOMS        # 100.0 shares cancelled
        assert s['fee_units'] == 10 * U                # 10% of the 100 fish gross
        assert s['payout_units'] == 90 * U

        msgs = notices_with(led, '紧急赎回已确认')
        assert len(msgs) == 1
        content = msgs[0]['content']
        assert msgs[0]['user_id'] == 'inst'
        assert '注销 100 份额' in content
        assert '费用 10.0000 小鱼干' in content
        assert '实付 90.0000 小鱼干' in content
        assert f'{100 * ATOMS}' not in content         # not raw share atoms
        assert f'费用 {10 * U}' not in content
        assert f'实付 {90 * U}' not in content

        # Accounting/payout integers unchanged by the formatting fix.
        assert [(p['kind'], p['amount_units']) for p in led.pending_payouts(F)] == [
            ('emergency_redemption', 90 * U)]
        assert led.holder(F, 'inst')['shares_atoms'] == 900 * ATOMS
        assert led.status(F)['non_trading_income_units'] == 10 * U
    finally:
        store.close()


# ----------------------------------------------------- expired receipt refund
def test_expired_receipt_refund_notice_shows_full_fish_amount(tmp_path):
    store = FundStore(tmp_path / 'fund.db')
    led = FundLedger(store)
    try:
        t0 = ms(2026, 1, 5)
        mark(led, 1000, t0)
        seed(led, 'inst', 1000, t0)

        tsub = ms(2026, 1, 5, 9)                       # inside the days 1-7 application window
        sub = led.create_subscription(F, 'u2', 100 * U, 'nf-late', tsub)
        r = led.receive_transfer(F, {
            'transfer_id': 'nf-late-tx', 'from_user_id': 'u2', 'amount_units': 105 * U,
            'note': sub['payment_note'], 'occurred_ms': tsub + 200_000,  # past the 180s TTL
        }, tsub + 201_000)
        assert r['status'] == 'refunded'               # judged by authoritative arrival

        msgs = notices_with(led, '退款')
        assert len(msgs) == 1
        assert msgs[0]['user_id'] == 'u2'
        assert '全额退款 105.0000 小鱼干' in msgs[0]['content']
        assert f'退款 {105 * U}' not in msgs[0]['content']   # not raw 1050000

        payouts = led.pending_payouts(F)
        assert [(p['kind'], p['amount_units']) for p in payouts] == [('refund', 105 * U)]
        assert led.status(F)['shares_atoms'] == 1000 * ATOMS
    finally:
        store.close()


# ------------------------------------------------------- unmatched receipt hold
def test_unmatched_transfer_notice_keeps_the_arrived_fish_amount(tmp_path):
    store = FundStore(tmp_path / 'fund.db')
    led = FundLedger(store)
    try:
        t0 = ms(2026, 1, 5)
        mark(led, 1000, t0)
        seed(led, 'inst', 1000, t0)

        amount = 12_345                                # 1.2345 fish
        when = ms(2026, 1, 10, 9)
        r = led.receive_transfer(F, {
            'transfer_id': 'nf-unmatched', 'from_user_id': 'stranger',
            'amount_units': amount, 'note': 'not-an-order', 'occurred_ms': when,
        }, when)
        assert r['status'] == 'unclaimed'

        msgs = notices_with(led, '未认领')
        assert len(msgs) == 1
        assert msgs[0]['user_id'] == 'stranger'
        assert '到账 1.2345 小鱼干' in msgs[0]['content']
        assert f'到账 {amount} ' not in msgs[0]['content']   # not raw 12345

        st = led.status(F)
        assert st['unclaimed_units'] == amount         # held, not counted as equity
        assert st['equity_units'] == 1000 * U - amount
    finally:
        store.close()
