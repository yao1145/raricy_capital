"""Targeted tests for the manual-refund payout dispatch in :mod:`raricy_capital.payments`.

The worker owns the only path that may move an unclaimed receipt back to its
original payer, so these tests pin the money rules: durable intent before the
external call, result validation, available-cash gating, reconciliation before
any same-key replay, and that ordinary payouts and the payment polling cursor
are left alone.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from raricy_capital.client import CLIENT_KEY_RE
from raricy_capital.contracts import BEIJING
from raricy_capital.ledger import FundLedger
from raricy_capital.payments import PaymentsWorker
from raricy_capital.store import FundStore

U = 10_000
F = 'capital1'


def ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BEIJING).timestamp() * 1000)


T = ms(2026, 10, 4, 10)


class FakeClient:
    """A site double: never network, always observable."""

    UNSET = object()

    def __init__(self):
        self.calls = []
        self.rows = []
        self.fail_next = None
        self.result = self.UNSET
        self.always_more = False
        self.cash = 1105 * U

    async def transfer(self, user_id, amount_units, note, key):
        self.calls.append(('transfer', user_id, amount_units, note, key))
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        if self.result is not self.UNSET:
            return self.result
        self.cash -= amount_units
        return {'transfer_id': f'out-{len(self.transfer_calls())}', 'amount_units': amount_units,
                'duplicated': False}

    async def balance(self):
        # Injected outgoing rows represent a debit after a lost response.
        return self.cash - sum(abs(r['amount_units']) for r in self.rows if r.get('type') == 'transfer')

    async def transactions(self, since_id):
        self.calls.append(('transactions', since_id))
        if self.always_more:
            return {'transactions': [], 'next_cursor': since_id + 100, 'has_more': True}
        rows = sorted([r for r in self.rows if int(r.get('id') or 0) > since_id],
                      key=lambda r: r['id'])
        page = rows[:100]
        return {'transactions': page,
                'next_cursor': page[-1]['id'] if page else since_id,
                'has_more': len(rows) > len(page)}

    def transfer_calls(self):
        return [c for c in self.calls if c[0] == 'transfer']


def outgoing(row_id, amount_units, note, transfer_id='out-1', to='payer'):
    """A normalised *outgoing* site row (the fund account paid someone)."""
    return {'id': 100 + row_id, 'type': 'transfer', 'transfer_id': transfer_id,
            'from_user_id': 'fund-account', 'to_user_id': to,
            'amount_units': amount_units, 'note': note, 'occurred_ms': T + 1000}


def receipt_row(row_id=91, transfer_id='in-1'):
    return {'id': row_id, 'type': 'transfer_receive', 'transfer_id': transfer_id,
            'from_user_id': 'payer', 'amount_units': 105 * U, 'note': 'wrong note',
            'occurred_ms': T + 1000}


_OPEN_STORES = []


@pytest.fixture(autouse=True)
def close_refund_stores():
    try:
        yield
    finally:
        while _OPEN_STORES:
            _OPEN_STORES.pop().close()


def refund_case(tmp_path, *, sub_units=None):
    """A verified unclaimed receipt with its durable original-payer refund queued."""
    store = FundStore(tmp_path / 'refund.db')
    _OPEN_STORES.append(store)
    ledger = FundLedger(store)
    ledger.mark_account(F, 1000 * U, 0, T, T)
    ledger.seed(F, 'institution', 1000 * U, T)
    ledger.receive_transfer(F, {
        'transfer_id': 'in-1', 'from_user_id': 'payer', 'amount_units': 105 * U,
        'note': 'wrong note', 'occurred_ms': T + 1000, 'transaction_row_id': 91,
    }, T + 2000)
    ledger.mark_account(F, 1105 * U, 0, T + 2000, T + 2000)
    if sub_units is not None:
        order = ledger.create_subscription(F, 'other', sub_units, 'other-key', T + 2000)
        ledger.receive_transfer(F, {
            'transfer_id': 'other-in', 'from_user_id': 'other',
            'amount_units': order['total_units'], 'note': order['payment_note'],
            'occurred_ms': T + 2000, 'transaction_row_id': 92,
        }, T + 2000)
    row = ledger.unclaimed(F)[0]
    ledger.resolve_unclaimed(
        F, row['id'], 'refund', version=0, reason='已核实付款人与金额，原路全额退回',
        actor='session-a',
        verified_transfer={'transfer_id': 'in-1', 'from_user_id': 'payer',
                           'amount_units': 105 * U, 'note': 'wrong note',
                           'occurred_ms': T + 1000, 'transaction_row_id': 91},
        now_ms=T + 3000)
    payout = store.list('payouts')[0]
    return ledger, payout


def payout_row(ledger, payout):
    return ledger.store.get('payouts', f'{F}:{payout["id"]}')


# ------------------------------------------------------------------- happy path
async def test_manual_refund_is_paid_once_with_its_durable_business_key(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['paid'] == 1

    calls = client.transfer_calls()
    assert len(calls) == 1
    _, user_id, amount_units, note, key = calls[0]
    assert user_id == 'payer'                     # only the original payer
    assert amount_units == 105 * U               # only the full original amount
    assert note == payout['note']
    assert key == payout['idempotency_key']
    assert CLIENT_KEY_RE.fullmatch(key) and len(key) <= 48

    row = payout_row(ledger, payout)
    assert row['status'] == 'paid' and row['transfer_id'] == 'out-1'
    assert ledger.unclaimed_detail(F, ledger.unclaimed(F)[0]['id'])['resolution_status'] == 'refunded'
    status = ledger.status(F)
    assert status['liabilities_units'] == 0
    assert status['wallet_units'] == 1000 * U
    assert status['equity_units'] == 1000 * U

    calls_before = len(client.calls)
    again = await worker.drain_manual_refunds(T + 6000)
    assert again['paid'] == 0 and again['seen'] == 0
    assert len(client.calls) == calls_before             # never a second payout
    assert ledger.status(F)['wallet_units'] == 1000 * U


@pytest.mark.parametrize('result', [
    None,
    {},
    {'transfer_id': ''},
    {'transfer_id': 'out-x', 'amount_units': 1},                 # wrong amount moved
    {'transfer_id': 'out-x', 'amount_units': 105 * U - 1},
    {'amount_units': 105 * U},                                   # no transfer id
])
async def test_an_unconfirmed_transfer_result_is_never_marked_paid(tmp_path, result):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    client.result = result
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['paid'] == 0 and stats['held'] == 1
    row = payout_row(ledger, payout)
    assert row['status'] == 'uncertain'
    assert row['waiting_reason'] == 'unconfirmed_transfer_result'
    assert ledger.status(F)['liabilities_units'] == 105 * U      # still owed, not paid
    assert ledger.unclaimed_detail(F, ledger.unclaimed(F)[0]['id'])['resolution_status'] == 'refund_unknown'


async def test_no_external_write_when_the_worker_is_not_live(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=False)

    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats == {'seen': 1, 'paid': 0, 'reconciled': 0, 'held': 0, 'waiting': 0,
                     'skipped': 0, 'error': None}
    assert client.calls == []
    assert payout_row(ledger, payout)['status'] == 'pending'
    events = [e['event'] for e in ledger.store.events(50)]
    assert 'manual_refunds_held_not_live' in events


# ------------------------------------------------------------------ cash gating
async def test_refund_waits_rather_than_using_pending_subscription_money(tmp_path):
    ledger, payout = refund_case(tmp_path, sub_units=100 * U)
    # Wallet holds exactly one pending subscription (principal + prepaid fee) on top
    # of nothing: paying the refund from it would divert investor money.
    fund = ledger.store.get('funds', F)
    fund['wallet_units'] = 105 * U
    ledger.store.put('funds', F, fund)

    client = FakeClient()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['waiting'] == 1 and stats['paid'] == 0
    assert client.transfer_calls() == []
    row = payout_row(ledger, payout)
    assert row['status'] == 'pending'
    assert row['waiting_reason'] == 'available_cash_shortage'
    assert row['waiting_ms'] == T + 5000

    # Cash restored: the queued refund goes out without a second intent.
    fund = ledger.store.get('funds', F)
    fund['wallet_units'] = 1210 * U
    ledger.store.put('funds', F, fund)
    stats = await worker.drain_manual_refunds(T + 6000)
    assert stats['paid'] == 1
    assert len(client.transfer_calls()) == 1
    assert len(ledger.store.list_items('payouts')) == 1


# ------------------------------------------------------- reconcile before replay
async def test_uncertain_refund_is_reconciled_before_any_replay(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    client.fail_next = TimeoutError('timeout')
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    stats = await worker.drain_manual_refunds(T + 4000)
    assert stats['held'] == 1
    assert payout_row(ledger, payout)['status'] == 'uncertain'
    first = client.transfer_calls()[0]
    assert first[4] == payout['idempotency_key']

    # The site did move the money even though the response was lost: the outgoing
    # ledger row is the only proof, so it is scanned before any resend.
    client.rows = [outgoing(11, 105 * U, payout['note'], 'out-real')]
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['reconciled'] == 1 and stats['paid'] == 0
    assert len(client.transfer_calls()) == 1                  # no second transfer
    row = payout_row(ledger, payout)
    assert row['status'] == 'paid' and row['transfer_id'] == 'out-real'
    assert ledger.status(F)['liabilities_units'] == 0
    assert ledger.status(F)['wallet_units'] == 1000 * U


async def test_complete_empty_scan_replays_only_the_same_business_key(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    client.fail_next = TimeoutError('timeout')
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    await worker.drain_manual_refunds(T + 4000)
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['paid'] == 1
    keys = [c[4] for c in client.transfer_calls()]
    assert keys == [payout['idempotency_key'], payout['idempotency_key']]
    assert payout_row(ledger, payout)['status'] == 'paid'


async def test_ambiguous_and_incomplete_scans_are_held(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    client.fail_next = TimeoutError('timeout')
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)
    await worker.drain_manual_refunds(T + 4000)

    # Two outgoing rows share the amount *and* the deterministic note: the fund may
    # have paid twice, so a human must look before anything else is sent.
    client.rows = [outgoing(11, 105 * U, payout['note'], 'out-a'),
                   outgoing(12, 105 * U, payout['note'], 'out-b')]
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['held'] == 1 and stats['paid'] == 0
    assert len(client.transfer_calls()) == 1
    row = payout_row(ledger, payout)
    assert row['status'] == 'uncertain'
    assert row['waiting_reason'] == 'reconcile_ambiguous'
    events = [e['event'] for e in ledger.store.events(50)]
    assert 'manual_refund_reconcile_ambiguous' in events

    # Incomplete scans remain unknown and must not authorize a replay.
    client.rows = []
    client.always_more = True
    stats = await worker.drain_manual_refunds(T + 6000)
    assert stats['held'] == 1 and stats['paid'] == 0
    assert len(client.transfer_calls()) == 1
    assert payout_row(ledger, payout)['status'] == 'uncertain'
    assert payout_row(ledger, payout)['waiting_reason'] == 'reconcile_incomplete'


async def test_reconciliation_never_moves_the_payment_polling_cursor(tmp_path):
    ledger, payout = refund_case(tmp_path)
    ledger.store.put('pay_cursors', F, {'since_id': 42, 'online': True, 'updated_ms': T})
    client = FakeClient()
    client.fail_next = TimeoutError('timeout')
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    await worker.drain_manual_refunds(T + 4000)
    client.rows = [outgoing(11, 105 * U, payout['note'], 'out-real')]
    await worker.drain_manual_refunds(T + 5000)

    assert ledger.store.get('pay_cursors', F) == {'since_id': 42, 'online': True,
                                                  'updated_ms': T}
    # Reconciled from the outgoing ledger, so the failed call stays the only one.
    assert len(client.transfer_calls()) == 1
    assert payout_row(ledger, payout)['status'] == 'paid'


# ------------------------------------------------- ordinary payouts untouched
async def test_ordinary_payouts_still_flow_and_never_pay_a_manual_refund(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    # The plain drain must not move an unclaimed refund: it has no cash gate and
    # no reconciliation pass.
    assert await worker.drain_payouts(T + 4000) == 0
    assert client.transfer_calls() == []
    assert payout_row(ledger, payout)['status'] == 'pending'

    ledger.store.put('payouts', f'{F}:wd-1', {
        'id': 'wd-1', 'fund_id': F, 'user_id': 'u2', 'amount_units': 10 * U,
        'note': 'dividend 2026-09', 'idempotency_key': 'wd-1', 'status': 'pending',
        'kind': 'dividend', 'created_ms': T,
    })
    assert await worker.drain_payouts(T + 5000) == 1
    calls = client.transfer_calls()
    assert len(calls) == 1 and calls[0][1] == 'u2'
    assert payout_row(ledger, payout)['status'] == 'pending'


# ------------------------------------------------------------------ tick wiring
async def test_tick_drains_manual_refunds_without_raising_a_global_hold(tmp_path):
    ledger, payout = refund_case(tmp_path)
    client = FakeClient()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)

    result = await worker.tick(T + 5000)
    assert result['refunds']['paid'] == 1
    assert [e for e in result['errors'] if e.startswith('refund')] == []
    assert payout_row(ledger, payout)['status'] == 'paid'


async def test_polled_receipt_keeps_the_authoritative_row_id(tmp_path):
    store = FundStore(tmp_path / 'poll.db')
    _OPEN_STORES.append(store)
    ledger = FundLedger(store)
    ledger.mark_account(F, 1000 * U, 0, T, T)
    ledger.seed(F, 'institution', 1000 * U, T)
    client = FakeClient()
    client.rows = [receipt_row(55, 't9')]
    worker = PaymentsWorker(store, ledger, {F: client}, live=True)

    assert await worker.poll_payments(T + 2000) == 1
    row = ledger.unclaimed(F)[0]
    assert row['transfer_id'] == 't9'
    assert row['transaction_row_id'] == 55


def test_transfer_normalizer_keeps_the_row_id_for_fast_lookup():
    row = PaymentsWorker._normalize_transfer(receipt_row(55, 't9'))
    assert row['transfer_id'] == 't9'
    assert row['transaction_row_id'] == 55
    assert row['amount_units'] == 105 * U


async def test_source_anchor_skips_large_history_and_confirms_the_refund(tmp_path):
    ledger, payout = refund_case(tmp_path)
    assert payout['transaction_row_id'] == 91
    client = FakeClient()
    client.fail_next = TimeoutError('lost response')
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)
    await worker.drain_manual_refunds(T + 4000)
    client.rows = [outgoing(11, 105 * U, payout['note'], 'out-real')]
    client.calls.clear()
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['reconciled'] == 1
    assert client.calls == [('transactions', 90)]
    assert ledger.status(F)['equity_units'] == 1000 * U


async def test_balance_failure_before_sending_waits_without_payment(tmp_path):
    ledger, payout = refund_case(tmp_path)
    class NoBalance(FakeClient):
        async def balance(self):
            raise TimeoutError('balance unavailable')
    client = NoBalance()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)
    assert (await worker.drain_manual_refunds(T + 4000))['waiting'] == 1
    assert not client.transfer_calls()
    assert payout_row(ledger, payout)['waiting_reason'] == 'balance_unavailable'


async def test_balance_failure_after_confirmed_payment_never_resends(tmp_path):
    ledger, payout = refund_case(tmp_path)
    class LostBalance(FakeClient):
        async def balance(self):
            if self.transfer_calls():
                raise TimeoutError('balance response lost')
            return await super().balance()
    client = LostBalance()
    worker = PaymentsWorker(ledger.store, ledger, {F: client}, live=True)
    assert (await worker.drain_manual_refunds(T + 4000))['held'] == 1
    client.rows = [outgoing(11, 105 * U, payout['note'], 'out-real')]
    assert (await worker.drain_manual_refunds(T + 5000))['held'] == 1
    assert len(client.transfer_calls()) == 1
    assert payout_row(ledger, payout)['status'] == 'uncertain'
