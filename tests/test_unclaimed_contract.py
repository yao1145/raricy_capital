"""Coordinator tests: cross-operation financial invariants for manual review."""
from datetime import datetime
from decimal import Decimal
import asyncio
from types import SimpleNamespace

import pytest

from raricy_capital.contracts import BEIJING, FundError
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore
from raricy_capital.payments import PaymentsWorker
from raricy_capital.runtime import FundService

U = 10000
F = 'capital1'
T = int(datetime(2026, 10, 4, 10, tzinfo=BEIJING).timestamp() * 1000)


@pytest.fixture
def case(tmp_path, request):
    store = FundStore(tmp_path / 'manual.db')
    request.addfinalizer(store.close)
    ledger = FundLedger(store)
    ledger.mark_account(F, 1000 * U, 0, T, T)
    ledger.seed(F, 'institution', 1000 * U, T)
    order = ledger.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    tx = {'transfer_id': 'incoming-1', 'from_user_id': 'payer',
          'amount_units': 105 * U, 'note': 'wrong note', 'occurred_ms': T + 1000}
    ledger.receive_transfer(F, tx, T + 2000)
    ledger.mark_account(F, 1105 * U, 0, T + 2000, T + 2000)
    row = ledger.unclaimed(F)[0]
    yield ledger, order, tx, row


def test_preview_is_read_only_and_manual_link_is_nav_neutral(case):
    ledger, order, tx, row = case
    before = ledger.status(F)
    records = ledger.store.list_items('unclaimed')
    preview = ledger.preview_unclaimed(F, row['id'], 'link',
        subscription_id=order['id'], verified_transfer=tx, now_ms=T + 3000)
    assert preview['eligible']
    assert ledger.store.list_items('unclaimed') == records
    ledger.resolve_unclaimed(F, row['id'], 'link', version=row.get('version', 0),
        reason='Verified original payer, arrival and total; note was omitted.',
        actor='session-test', verified_transfer=tx, subscription_id=order['id'], now_ms=T + 4000)
    after = ledger.status(F)
    assert Decimal(after['nav']) == Decimal(before['nav'])
    assert after['shares_atoms'] == before['shares_atoms']
    assert after['equity_units'] == before['equity_units']
    assert after['pending_receipts_units'] == 100 * U
    assert after['fee_balance_units'] == 5 * U
    assert after['unclaimed_units'] == 0


def test_refund_and_link_cannot_both_resolve_one_receipt(case):
    ledger, order, tx, row = case
    before = ledger.status(F)
    ledger.resolve_unclaimed(F, row['id'], 'refund', version=row.get('version', 0),
        reason='Original payer requested full return.', actor='session-a',
        verified_transfer=tx, now_ms=T + 3000)
    with pytest.raises(FundError):
        ledger.resolve_unclaimed(F, row['id'], 'link', version=row.get('version', 0),
            reason='Stale second administrator preview.', actor='session-b',
            verified_transfer=tx, subscription_id=order['id'], now_ms=T + 4000)
    payouts = ledger.pending_payouts(F)
    assert len(payouts) == 1
    assert payouts[0]['user_id'] == 'payer'
    assert payouts[0]['amount_units'] == 105 * U
    assert len(payouts[0]['idempotency_key']) <= 48
    assert ledger.status(F)['equity_units'] == before['equity_units']
    ledger.receive_transfer(F, tx, T + 5000)
    assert ledger.status(F)['unclaimed_units'] == 0
    assert len(ledger.pending_payouts(F)) == 1


def test_ambiguous_legacy_receipt_cannot_be_linked(case):
    ledger, order, tx, row = case
    legacy = ledger.store.get('unclaimed', row['id'])
    legacy.pop('transfer_id')
    ledger.store.put('unclaimed', row['id'], legacy)
    duplicate = dict(ledger.store.get('transfers', tx['transfer_id']),
        transfer_id='incoming-ambiguous', unclaimed_id=None)
    ledger.store.put('transfers', 'incoming-ambiguous', duplicate)
    preview = ledger.preview_unclaimed(F, row['id'], 'link',
        subscription_id=order['id'], verified_transfer=tx, now_ms=T + 3000)
    assert not preview['eligible'], 'Ambiguous receipt identity must block both link and refund'
    with pytest.raises(FundError):
        ledger.resolve_unclaimed(F, row['id'], 'link', version=0,
            reason='Identity still ambiguous.', actor='session-test',
            verified_transfer=tx, subscription_id=order['id'], now_ms=T + 3000)
    assert ledger.status(F)['unclaimed_units'] == 105 * U


def test_already_consumed_canonical_transfer_cannot_resolve_again(case):
    ledger, order, tx, row = case
    canonical = ledger.store.get('transfers', tx['transfer_id'])
    canonical['status'] = 'received'
    ledger.store.put('transfers', tx['transfer_id'], canonical)
    for action in ('link', 'refund'):
        preview = ledger.preview_unclaimed(F, row['id'], action,
            subscription_id=order['id'] if action == 'link' else None,
            verified_transfer=tx, now_ms=T + 3000)
        assert not preview['eligible'], 'Canonical transfer was already allocated'
    assert ledger.pending_payouts(F) == []


def test_authoritative_money_is_integer_not_truncated_float(case):
    ledger, order, tx, row = case
    bad = dict(tx, amount_units=tx['amount_units'] + 0.5)
    preview = ledger.preview_unclaimed(F, row['id'], 'refund',
        verified_transfer=bad, now_ms=T + 3000)
    assert not preview['eligible']


class ReceiptSite:
    def __init__(self, rows=None, partial=False):
        self.rows = rows or []
        self.partial = partial
        self.sent = 0
        self.cash = 1105 * U

    async def transactions(self, since_id):
        rows = [row for row in self.rows if row['id'] > since_id]
        cursor = rows[-1]['id'] if rows else since_id + 1
        return {'transactions': rows, 'next_cursor': cursor, 'has_more': self.partial}

    async def balance(self):
        return self.cash

    async def transfer(self, *args):
        self.sent += 1
        raise TimeoutError('synthetic response loss')


async def test_unique_legacy_backlink_works_through_service(case, monkeypatch):
    ledger, order, tx, row = case
    legacy = ledger.store.get('unclaimed', row['id'])
    legacy.pop('transfer_id')
    ledger.store.put('unclaimed', row['id'], legacy)
    site = ReceiptSite([dict(tx, id=1, type='transfer_receive')])
    service = FundService.__new__(FundService)
    service.ledger, service.store = ledger, ledger.store
    service.config, service.clients = SimpleNamespace(live=True), {F: site}
    service.mutex = asyncio.Lock()
    monkeypatch.setattr('raricy_capital.runtime.now_ms', lambda: T + 3000)
    original = ledger.store.list_items('unclaimed')
    result = await service.preview_unclaimed(F, row['id'], 'link', order['id'])
    assert result['eligible'], result['errors']
    assert ledger.store.list_items('unclaimed') == original


@pytest.mark.parametrize('partial,recipient,expected', [
    (False, 'payer', True), (True, 'payer', False), (False, None, False)])
async def test_refund_reconciliation_uses_real_signed_complete_fingerprint(case, partial, recipient, expected):
    ledger, order, tx, row = case
    ledger.resolve_unclaimed(F, row['id'], 'refund', version=0,
        reason='Full return requested.', actor='session-test',
        verified_transfer=tx, now_ms=T + 3000)
    payout = ledger.pending_payouts(F)[0]
    site = ReceiptSite()
    worker = PaymentsWorker(ledger.store, ledger, {F: site}, live=True)
    await worker.drain_manual_refunds(T + 4000)
    site.rows = [{'id': 1, 'type': 'transfer', 'transfer_id': 'out-confirmed',
        'from_user_id': 'fund-account', 'to_user_id': recipient,
        'amount_units': -105 * U, 'note': payout['note'], 'occurred_ms': T + 4000}]
    site.partial = partial
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['reconciled'] == int(expected)
    assert site.sent == 1, 'Incomplete identity or an existing outgoing payment must prevent another send'
    assert ledger.unclaimed(F)[0]['resolution_status'] == ('refunded' if expected else 'refund_unknown')


async def test_refund_checks_fresh_cash_without_spending_pending_receipts(case):
    ledger, order, tx, row = case
    other = ledger.create_subscription(F, 'other', 100 * U, 'other-order', T + 2000)
    ledger.receive_transfer(F, dict(tx, transfer_id='other-in', from_user_id='other',
        note=other['payment_note'], occurred_ms=T + 2000), T + 2000)
    ledger.mark_account(F, 1210 * U, 0, T + 2000, T + 2000)
    ledger.resolve_unclaimed(F, row['id'], 'refund', version=0,
        reason='Full return requested.', actor='session-test', verified_transfer=tx, now_ms=T + 3000)
    site = ReceiptSite()
    site.cash = 105 * U  # The only actual cash belongs to the pending subscription.
    worker = PaymentsWorker(ledger.store, ledger, {F: site}, live=True)
    stats = await worker.drain_manual_refunds(T + 4000)
    assert stats['waiting'] == 1 and site.sent == 0


async def test_reconciled_refund_does_not_subtract_an_already_reflected_debit(case):
    ledger, order, tx, row = case
    ledger.resolve_unclaimed(F, row['id'], 'refund', version=0,
        reason='Full return requested.', actor='session-test', verified_transfer=tx, now_ms=T + 3000)
    payout = ledger.pending_payouts(F)[0]
    site = ReceiptSite()
    worker = PaymentsWorker(ledger.store, ledger, {F: site}, live=True)
    await worker.drain_manual_refunds(T + 4000)
    # An account snapshot already observed the actual debit after a lost response.
    ledger.mark_account(F, 1000 * U, 0, T + 4500, T + 4500)
    site.cash = 1000 * U
    site.rows = [{'id': 1, 'type': 'transfer', 'transfer_id': 'out-confirmed',
        'to_user_id': 'payer', 'amount_units': -105 * U, 'note': payout['note']}]
    stats = await worker.drain_manual_refunds(T + 5000)
    assert stats['reconciled'] == 1
    assert ledger.status(F)['wallet_units'] == 1000 * U
    assert ledger.status(F)['equity_units'] == 1000 * U


async def test_manual_site_lookup_does_not_block_monitor_mutex(case, monkeypatch):
    ledger, order, tx, row = case
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowSite(ReceiptSite):
        async def transactions(self, since_id):
            entered.set()
            await release.wait()
            return await super().transactions(since_id)

    service = FundService.__new__(FundService)
    service.ledger, service.store = ledger, ledger.store
    service.config = SimpleNamespace(live=True)
    service.clients = {F: SlowSite([dict(tx, id=1, type='transfer_receive')])}
    service.mutex = asyncio.Lock()
    monkeypatch.setattr('raricy_capital.runtime.now_ms', lambda: T + 3000)
    task = asyncio.create_task(service.resolve_unclaimed(F, row['id'], 'link',
        version=0, reason='Verified original payer.', actor='session-test', subscription_id=order['id']))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(service.mutex.acquire(), .3)
        service.mutex.release()
    finally:
        release.set()
        await task


def test_identity_checks_do_not_truncate_at_ten_thousand_rows(case):
    import json
    ledger, order, tx, row = case
    store = ledger.store
    legacy = store.get('unclaimed', row['id'])
    legacy.pop('transfer_id')
    store.put('unclaimed', row['id'], legacy)
    unrelated = json.dumps({'fund_id': 'capital2', 'status': 'unclaimed'})
    with store.transaction():
        store.db.executemany('INSERT INTO records VALUES(?,?,?,?)',
            [('transfers', f'a-{i:05d}', unrelated, T) for i in range(10001)])
    duplicate = dict(store.get('transfers', tx['transfer_id']),
        transfer_id='zz-duplicate', unclaimed_id=None)
    store.put('transfers', 'zz-duplicate', duplicate)
    preview = ledger.preview_unclaimed(F, row['id'], 'link',
        subscription_id=order['id'], verified_transfer=tx, now_ms=T + 3000)
    assert not preview['eligible'] and 'ambiguous_backlink' in preview['errors']
    store.db.execute("DELETE FROM records WHERE namespace='transfers' AND key='zz-duplicate'")
    # A duplicate unclaimed owner after the display cap is also disqualifying.
    with store.transaction():
        store.db.executemany('INSERT INTO records VALUES(?,?,?,?)',
            [('unclaimed', f'a-{i:05d}', unrelated, T) for i in range(10001)])
    store.put('unclaimed', 'zz-owner', dict(legacy, id='zz-owner'))
    preview = ledger.preview_unclaimed(F, row['id'], 'refund',
        verified_transfer=tx, now_ms=T + 3000)
    assert not preview['eligible']


async def test_changed_account_during_manual_lookup_cannot_commit(case, monkeypatch):
    ledger, order, tx, row = case
    service = FundService.__new__(FundService)
    service.ledger, service.store = ledger, ledger.store
    service.config = SimpleNamespace(live=True)
    service.mutex = asyncio.Lock()
    class ReplacedSite(ReceiptSite):
        async def transactions(self, since_id):
            service.clients[F] = ReceiptSite()
            return await super().transactions(since_id)
    service.clients = {F: ReplacedSite([dict(tx, id=1, type='transfer_receive')])}
    monkeypatch.setattr('raricy_capital.runtime.now_ms', lambda: T + 3000)
    with pytest.raises(FundError, match='account_changed'):
        await service.resolve_unclaimed(F, row['id'], 'link', version=0,
            reason='Original payer verified.', actor='session-test', subscription_id=order['id'])
    assert ledger.status(F)['unclaimed_units'] == 105 * U
