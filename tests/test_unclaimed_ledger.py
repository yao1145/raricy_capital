"""Domain tests for the manual unclaimed-receipt review in :mod:`raricy_capital.ledger`.

Only the money-bearing rules are exercised here: the read-only preview, the
atomic version CAS, the NAV-neutral manual link (issued by the *existing*
settlement-batch flow), the durable single-intent original-payer refund, the frozen
period / window boundaries, the conservative historical backlink rule and the
audit trail.  Web/runtime gates are the other workers' tests.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from raricy_capital.contracts import BEIJING, FundError
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore

U = 10_000            # money units per 1.0 fish
ATOMS = 100_000_000   # share atoms per 1.0 share
F = 'capital1'
G = 'capital2'


def ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BEIJING).timestamp() * 1000)


T = ms(2026, 10, 4, 10)   # inside the 2026-09 window (days 1-7 close the prior month)


@pytest.fixture
def led(tmp_path):
    store = FundStore(tmp_path / 'unclaimed.db')
    ledger = FundLedger(store)
    try:
        yield ledger
    finally:
        store.close()


# --------------------------------------------------------------------- helpers
def receipt(transfer_id, payer, amount_units, note, occurred_ms, row_id=None):
    """The authoritative upstream receipt row (normalised client shape)."""
    row = {'transfer_id': transfer_id, 'from_user_id': payer, 'amount_units': amount_units,
           'note': note, 'occurred_ms': occurred_ms}
    if row_id is not None:
        row['transaction_row_id'] = row_id
    return row


def funded(led, fish=1000, when=T, fund=F):
    led.mark_account(fund, fish * U, 0, when, when)
    led.seed(fund, 'institution', fish * U, when)


def misdirected(led, *, transfer_id='in-1', payer='payer', amount=105 * U, note='wrong note',
                occurred=T + 1000, now=T + 2000, row_id=91, fund=F):
    """A receipt that cannot be auto-matched (wrong note) -> held as unclaimed."""
    return led.receive_transfer(
        fund, receipt(transfer_id, payer, amount, note, occurred, row_id), now)


def row_for(led, transfer_id, fund=F):
    rows = [r for r in led.unclaimed(fund) if r['transfer_id'] == transfer_id]
    assert len(rows) == 1, f'{transfer_id} not held exactly once'
    return rows[0]


def fund_record(led, fund=F):
    return dict(led.store.get('funds', fund))


# ------------------------------------------------------------------ manual link
def test_manual_link_keeps_nav_neutral_and_issues_only_at_month_end(led):
    funded(led)
    order = led.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    assert misdirected(led)['status'] == 'unclaimed'
    led.mark_account(F, 1105 * U, 0, T + 2000, T + 2000)

    row = led.unclaimed(F)[0]
    assert row['transfer_id'] == 'in-1'
    assert row['transaction_row_id'] == 91
    assert row['version'] == 0
    assert row['resolution_status'] == 'unclaimed'

    before = led.status(F)
    assert before['unclaimed_units'] == 105 * U
    assert before['equity_units'] == 1000 * U
    verified = receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91)

    detail = led.unclaimed_detail(F, row['id'], T + 3000)
    assert [c['subscription_id'] for c in detail['candidates']] == [order['id']]
    assert detail['candidates'][0]['note_matches'] is False
    assert detail['candidates'][0]['eligible'] is True
    assert detail['history'] == [] and detail['version'] == 0
    assert detail['receipt']['transfer_id'] == 'in-1'

    preview = led.preview_unclaimed(F, row['id'], 'link', subscription_id=order['id'],
                                    verified_transfer=verified, now_ms=T + 3000)
    assert preview['eligible'] is True
    assert preview['errors'] == []
    assert preview['action'] == 'link'
    assert preview['record']['note'] == 'wrong note'      # original note is preserved
    assert preview['subscription']['id'] == order['id']

    resolved = led.resolve_unclaimed(
        F, row['id'], 'link', version=0,
        reason='付款人漏填附言，付款人、金额与到账时间均已核实',
        actor='session-abc', verified_transfer=verified,
        subscription_id=order['id'], now_ms=T + 4000)
    assert resolved['resolution_status'] == 'linked'

    after = led.status(F)
    assert after['nav'] == before['nav']
    assert after['equity_units'] == before['equity_units']
    assert after['shares_atoms'] == before['shares_atoms']       # nothing issued early
    assert after['unclaimed_units'] == 0
    assert after['pending_receipts_units'] == 100 * U
    assert after['fee_balance_units'] == 5 * U
    assert after['capital_flows_units'] == before['capital_flows_units']

    sub = led.store.get('subscriptions', order['id'])
    assert sub['status'] == 'received'
    assert sub['occurred_ms'] == T + 1000
    assert sub['payment_note'] == order['payment_note']          # never rewritten
    assert sub['period'] == '2026-09'
    assert sub['manual_review']['original_note'] == 'wrong note'
    assert sub['manual_review']['actor'] == 'session-abc'
    assert led.store.get('transfers', 'in-1')['status'] == 'linked'
    assert led.store.get('transfers', 'in-1')['subscription_id'] == order['id']

    detail = led.unclaimed_detail(F, row['id'])
    assert detail['resolution_status'] == 'linked' and detail['version'] == 1
    entry = detail['history'][-1]
    assert entry['action'] == 'link' and entry['actor'] == 'session-abc'
    assert entry['before']['unclaimed_units'] == 105 * U
    assert entry['after']['unclaimed_units'] == 0
    assert entry['after']['pending_receipts_units'] == 100 * U

    # Settlement batch: the linked receipt is issued by that period's 7th-20:00
    # batch at NAV 1.  The order's 2026-09 batch runs on 2026-10-07 20:00; the mark
    # five seconds earlier is the fresh pre-cutoff quote the batch prices against.
    when = ms(2026, 10, 7, 19, 59, 55)
    led.mark_account(F, 1105 * U, 0, when, when)
    settled = led.settle_month(F, '2026-09', ms(2026, 10, 7, 20))
    assert settled['status'] == 'settled'
    assert settled['issued_external_shares_atoms'] == 100 * ATOMS
    final = led.status(F)
    assert final['unclaimed_units'] == 0
    assert final['pending_receipts_units'] == 0
    assert final['shares_atoms'] == 1100 * ATOMS
    assert final['nav'] == before['nav']


def test_link_window_and_period_boundaries(led):
    funded(led)
    cases = [(0, True, None), (180_000, True, None), (180_001, False, 'arrival_too_late'),
             (-1, False, 'arrival_before_order')]
    for index, (offset, expected, code) in enumerate(cases):
        order = led.create_subscription(F, 'payer', 100 * U, f'key-{index}', T)
        occurred = T + offset
        verified = receipt(f'in-{index}', 'payer', 105 * U, 'wrong', occurred, 90 + index)
        misdirected(led, transfer_id=f'in-{index}', occurred=occurred, note='wrong',
                    now=T + 400_000 + index, row_id=90 + index)
        row = row_for(led, f'in-{index}')
        preview = led.preview_unclaimed(F, row['id'], 'link', subscription_id=order['id'],
                                        verified_transfer=verified,
                                        now_ms=T + 400_000 + index)
        assert preview['eligible'] is expected, (offset, preview['errors'])
        if expected:
            assert preview['errors'] == []
            continue
        assert code in preview['errors']
        with pytest.raises(FundError) as exc:
            led.resolve_unclaimed(F, row['id'], 'link', version=0, reason='late/early',
                                  actor='session-a', verified_transfer=verified,
                                  subscription_id=order['id'], now_ms=T + 400_000 + index)
        assert exc.value.code == code
        assert row_for(led, f'in-{index}')['version'] == 0


def test_link_rejects_wrong_payer_amount_fund_and_settled_order(led):
    funded(led)
    amount = 105 * U
    order = led.create_subscription(F, 'payer', 100 * U, 'k1', T)
    order_big = led.create_subscription(F, 'payer', 200 * U, 'k2', T)
    order_other_fund = led.create_subscription(G, 'payer', 100 * U, 'k3', T)

    # wrong payer
    misdirected(led, transfer_id='p-wrong', payer='mallory', row_id=1)
    wrong_payer = row_for(led, 'p-wrong')
    preview = led.preview_unclaimed(F, wrong_payer['id'], 'link', subscription_id=order['id'],
                                    verified_transfer=receipt('p-wrong', 'mallory', amount,
                                                              'wrong note', T + 1000, 1),
                                    now_ms=T + 3000)
    assert preview['eligible'] is False and 'payer_mismatch' in preview['errors']
    assert led.unclaimed_detail(F, wrong_payer['id'])['candidates'] == []

    # wrong amount (same payer, different order total)
    misdirected(led, transfer_id='a-wrong', row_id=2)
    wrong_amount = row_for(led, 'a-wrong')
    verified = receipt('a-wrong', 'payer', amount, 'wrong note', T + 1000, 2)
    preview = led.preview_unclaimed(F, wrong_amount['id'], 'link',
                                    subscription_id=order_big['id'],
                                    verified_transfer=verified, now_ms=T + 3000)
    assert preview['eligible'] is False and 'amount_mismatch' in preview['errors']

    # wrong fund (the order lives in the other fund)
    preview = led.preview_unclaimed(F, wrong_amount['id'], 'link',
                                    subscription_id=order_other_fund['id'],
                                    verified_transfer=verified, now_ms=T + 3000)
    assert preview['eligible'] is False and 'subscription_fund_mismatch' in preview['errors']
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, wrong_amount['id'], 'link', version=0, reason='cross fund',
                              actor='session-a', verified_transfer=verified,
                              subscription_id=order_other_fund['id'], now_ms=T + 3000)
    assert exc.value.code == 'subscription_fund_mismatch'

    # already settled order (paid by its own matching receipt)
    order_paid = led.create_subscription(F, 'payer', 100 * U, 'k4', T)
    matched = led.receive_transfer(
        F, receipt('ok-1', 'payer', amount, order_paid['payment_note'], T + 1000, 3), T + 2000)
    assert matched['status'] == 'received'
    misdirected(led, transfer_id='s-wrong', row_id=4)
    settled_row = row_for(led, 's-wrong')
    verified = receipt('s-wrong', 'payer', amount, 'wrong note', T + 1000, 4)
    preview = led.preview_unclaimed(F, settled_row['id'], 'link',
                                    subscription_id=order_paid['id'],
                                    verified_transfer=verified, now_ms=T + 3000)
    assert preview['eligible'] is False and 'subscription_settled' in preview['errors']
    assert led.unclaimed_detail(F, settled_row['id'])['version'] == 0


def test_link_refuses_an_issuance_period_whose_cutoff_already_passed(led):
    when = ms(2026, 9, 4)
    led.mark_account(F, 1000 * U, 0, when, when)
    led.seed(F, 'institution', 1000 * U, when)
    order = led.create_subscription(F, 'payer', 100 * U, 'sept', when)
    misdirected(led, occurred=when + 1000, now=when + 2000,
                transfer_id='sept-in', row_id=7)
    led.mark_account(F, 1105 * U, 0, ms(2026, 9, 30, 23, 59, 55), ms(2026, 9, 30, 23, 59, 55))

    row = row_for(led, 'sept-in')
    verified = receipt('sept-in', 'payer', 105 * U, 'wrong note', when + 1000, 7)
    now = T                                   # 2026-10-04: the September book is closed
    preview = led.preview_unclaimed(F, row['id'], 'link', subscription_id=order['id'],
                                    verified_transfer=verified, now_ms=now)
    assert preview['eligible'] is False
    assert 'period_closed' in preview['errors']
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, row['id'], 'link', version=0, reason='backdate attempt',
                              actor='session-a', verified_transfer=verified,
                              subscription_id=order['id'], now_ms=now)
    assert exc.value.code == 'period_closed'
    # Nothing was backdated and nobody was paid.
    assert led.store.get('subscriptions', order['id'])['status'] == 'pending'
    assert led.status(F)['unclaimed_units'] == 105 * U
    assert led.store.list_items('payouts') == []

    # The same verified receipt may still be returned to its original payer.
    refund = led.preview_unclaimed(F, row['id'], 'refund', verified_transfer=verified,
                                   now_ms=now)
    assert refund['eligible'] is True
    assert refund['errors'] == [] and refund['subscription'] is None


# ------------------------------------------------------------------- stale CAS
def test_stale_version_and_double_click_resolve_exactly_once(led):
    funded(led)
    order = led.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    misdirected(led)
    row = led.unclaimed(F)[0]
    verified = receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91)

    led.resolve_unclaimed(F, row['id'], 'link', version=0, reason='verified link',
                          actor='session-a', verified_transfer=verified,
                          subscription_id=order['id'], now_ms=T + 3000)
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, row['id'], 'refund', version=0, reason='stale second admin',
                              actor='session-b', verified_transfer=verified, now_ms=T + 4000)
    assert exc.value.code == 'stale_version'
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, row['id'], 'refund', version=1, reason='double click',
                              actor='session-b', verified_transfer=verified, now_ms=T + 4000)
    assert exc.value.code == 'already_resolved'

    assert led.store.list_items('payouts') == []
    assert len(led.pending_payouts(F)) == 0
    detail = led.unclaimed_detail(F, row['id'])
    assert detail['version'] == 1
    assert len(detail['history']) == 1


# --------------------------------------------------- refund: durable and neutral
def _refund_case(tmp_path):
    store = FundStore(tmp_path / 'refund.db')
    ledger = FundLedger(store)
    funded(ledger)
    order = ledger.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    misdirected(ledger)
    ledger.mark_account(F, 1105 * U, 0, T + 2000, T + 2000)
    return ledger, order


def test_refund_is_one_durable_intent_and_keeps_nav_neutral(tmp_path):
    ledger, order = _refund_case(tmp_path)
    try:
        row = ledger.unclaimed(F)[0]
        verified = receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91)
        before = ledger.status(F)

        preview = ledger.preview_unclaimed(F, row['id'], 'refund', verified_transfer=verified,
                                           now_ms=T + 3000)
        assert preview['eligible'] is True
        assert preview['subscription'] is None

        resolved = ledger.resolve_unclaimed(
            F, row['id'], 'refund', version=0,
            reason='付款人明确要求原路全额退回，金额与收款人已核实',
            actor='session-refund', verified_transfer=verified, now_ms=T + 4000)
        assert resolved['resolution_status'] == 'refund_queued'
        assert resolved['refund_payout_id']

        payouts = ledger.store.list_items('payouts')
        assert len(payouts) == 1
        payout_id, payout = payouts[0]
        assert payout['id'] == resolved['refund_payout_id']
        assert payout_id == f'{F}:{payout["id"]}'
        assert payout['kind'] == 'unclaimed_refund'
        assert payout['fund_id'] == F
        assert payout['user_id'] == 'payer'          # original payer only
        assert payout['amount_units'] == 105 * U     # full original amount, not editable
        assert payout['status'] == 'pending'
        assert len(payout['idempotency_key']) <= 48
        assert payout['idempotency_key'] and 'in-1' not in payout['note']
        assert ledger.pending_payouts(F) == [payout]

        after = ledger.status(F)
        assert after['unclaimed_units'] == 0
        assert after['liabilities_units'] == 105 * U
        assert after['equity_units'] == before['equity_units']
        assert after['nav'] == before['nav']
        assert after['shares_atoms'] == before['shares_atoms']
        assert ledger.store.get('subscriptions', order['id'])['status'] == 'pending'

        transfers = ledger.store.get('transfers', 'in-1')
        assert transfers['status'] == 'refund_queued'
        assert transfers['refund_payout_id'] == payout['id']

        # A replay of the original receipt cannot re-credit or match again.
        replay = ledger.receive_transfer(
            F, receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91), T + 5000)
        assert replay.get('duplicated') is True
        assert ledger.status(F)['unclaimed_units'] == 0
        assert len(ledger.store.list_items('payouts')) == 1

        # Queue -> unknown -> paid is what the operator sees, never "already refunded".
        assert ledger.unclaimed_detail(F, row['id'])['resolution_status'] == 'refund_queued'
        ledger.mark_payout_uncertain(payout['id'], 'timeout', T + 6000)
        assert ledger.unclaimed_detail(F, row['id'])['resolution_status'] == 'refund_unknown'
        ledger.mark_payout_paid(payout['id'], 'out-1', T + 7000)
        detail = ledger.unclaimed_detail(F, row['id'])
        assert detail['resolution_status'] == 'refunded'
        assert detail['payout_status'] == 'paid'
        paid_status = ledger.status(F)
        assert paid_status['liabilities_units'] == 0
        assert paid_status['wallet_units'] == 1000 * U
        assert paid_status['equity_units'] == before['equity_units']
    finally:
        ledger.store.close()


def test_refund_key_is_deterministic_for_the_same_receipt(tmp_path):
    keys = []
    for name in ('a', 'b'):
        ledger, _order = _refund_case(tmp_path / name)
        try:
            row = ledger.unclaimed(F)[0]
            ledger.resolve_unclaimed(
                F, row['id'], 'refund', version=0, reason='deterministic key',
                actor='session-a',
                verified_transfer=receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91),
                now_ms=T + 4000)
            payout = ledger.store.list('payouts')[0]
            keys.append((payout['id'], payout['idempotency_key'], payout['note']))
        finally:
            ledger.store.close()
    assert keys[0] == keys[1]
    # A different receipt of the same amount never reuses the business key.
    ledger, _order = _refund_case(tmp_path / 'c')
    try:
        misdirected(ledger, transfer_id='in-2', now=T + 9000, row_id=92)
        row = row_for(ledger, 'in-2')
        ledger.resolve_unclaimed(
            F, row['id'], 'refund', version=0, reason='second receipt', actor='session-a',
            verified_transfer=receipt('in-2', 'payer', 105 * U, 'wrong note', T + 1000, 92),
            now_ms=T + 10_000)
        second = [p for p in ledger.store.list('payouts')
                  if p.get('unclaimed_id') == row['id']][0]
        assert second['idempotency_key'] != keys[0][1]
        assert second['note'] != keys[0][2]
    finally:
        ledger.store.close()


# ------------------------------------------------------- historical backlink rule
def _legacy(led, uid, *, occurred=T + 1000):
    led.store.put('unclaimed', uid, {
        'id': uid, 'fund_id': F, 'from_user_id': 'payer', 'amount_units': 105 * U,
        'note': 'legacy', 'occurred_ms': occurred, 'status': 'unclaimed',
        'created_ms': T + 2000, 'version': 0,
    })
    fund = fund_record(led)
    fund['unclaimed_units'] += 105 * U
    led.store.put('funds', F, fund)


def _legacy_transfer(led, transfer_id, *, occurred=T + 1000):
    led.store.put('transfers', transfer_id, {
        'transfer_id': transfer_id, 'fund_id': F, 'from_user_id': 'payer',
        'amount_units': 105 * U, 'note': 'legacy', 'occurred_ms': occurred,
        'subscription_id': None, 'status': 'unclaimed', 'refund_payout_id': None,
    })


def test_historical_row_is_backlinked_only_when_unique(led):
    funded(led)
    _legacy(led, 'legacy-unique')
    _legacy_transfer(led, 'old-tx')
    verified = receipt('old-tx', 'payer', 105 * U, 'legacy', T + 1000)

    detail = led.unclaimed_detail(F, 'legacy-unique', T + 3000)
    assert detail['receipt']['transfer_id'] == 'old-tx'
    assert detail['backlink_pending'] is True
    assert led.store.get('unclaimed', 'legacy-unique').get('transfer_id') is None
    preview = led.preview_unclaimed(F, 'legacy-unique', 'refund', verified_transfer=verified,
                                    now_ms=T + 3000)
    assert preview['eligible'] is True
    assert preview['backfill_transfer_id'] == 'old-tx'

    ledger_state = led.store.list_items('unclaimed')
    led.preview_unclaimed(F, 'legacy-unique', 'refund', verified_transfer=verified,
                          now_ms=T + 3000)
    assert led.store.list_items('unclaimed') == ledger_state      # preview still read-only

    resolved = led.resolve_unclaimed(F, 'legacy-unique', 'refund', version=0,
                                     reason='唯一对应上游流水，已确认付款人与金额',
                                     actor='session-a', verified_transfer=verified,
                                     now_ms=T + 4000)
    assert resolved['transfer_id'] == 'old-tx'
    assert resolved['resolution_status'] == 'refund_queued'
    assert led.store.get('transfers', 'old-tx')['status'] == 'refund_queued'

    # Two candidate rows for one unclaimed record: stay isolated, no payout.
    _legacy(led, 'legacy-ambiguous')
    _legacy_transfer(led, 'old-a')
    _legacy_transfer(led, 'old-b')
    ambiguous = led.preview_unclaimed(F, 'legacy-ambiguous', 'refund',
                                      verified_transfer=receipt('old-a', 'payer', 105 * U,
                                                                'legacy', T + 1000),
                                      now_ms=T + 5000)
    assert ambiguous['eligible'] is False
    assert 'missing_backlink' in ambiguous['errors']
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, 'legacy-ambiguous', 'refund', version=0,
                              reason='ambiguous backlink', actor='session-a',
                              verified_transfer=receipt('old-a', 'payer', 105 * U, 'legacy',
                                                        T + 1000),
                              now_ms=T + 6000)
    assert exc.value.code == 'missing_backlink'
    assert led.unclaimed_detail(F, 'legacy-ambiguous')['version'] == 0
    assert len(led.store.list_items('payouts')) == 1          # only the unique one


# -------------------------------------------------------------- verification
def test_resolve_requires_authoritative_receipt_and_bounded_reason_actor(led):
    funded(led)
    order = led.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    misdirected(led)
    row = led.unclaimed(F)[0]
    verified = receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91)

    def resolve(**kwargs):
        base = dict(version=0, reason='verified', actor='session-a',
                    verified_transfer=verified, now_ms=T + 3000)
        base.update(kwargs)
        return led.resolve_unclaimed(F, row['id'], 'link', subscription_id=order['id'], **base)

    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=None)
    assert exc.value.code == 'verification_required'
    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=receipt('in-1', 'payer', 999 * U, 'wrong note', T + 1000, 91))
    assert exc.value.code == 'verification_mismatch'
    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=receipt('in-1', 'mallory', 105 * U, 'wrong note', T + 1000, 91))
    assert exc.value.code == 'verification_mismatch'
    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=receipt('other-tx', 'payer', 105 * U, 'wrong note', T + 1000, 91))
    assert exc.value.code == 'verification_mismatch'
    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=receipt('in-1', 'payer', 105 * U, 'forged note', T + 1000, 91))
    assert exc.value.code == 'verification_mismatch'
    with pytest.raises(FundError) as exc:
        resolve(verified_transfer=receipt('in-1', 'payer', 105 * U, 'wrong note', T + 999_000, 91))
    assert exc.value.code == 'verification_mismatch'

    with pytest.raises(FundError) as exc:
        resolve(reason='   ')
    assert exc.value.code == 'reason_required'
    with pytest.raises(FundError) as exc:
        resolve(reason='x' * 501)
    assert exc.value.code == 'reason_too_long'
    with pytest.raises(FundError) as exc:
        resolve(actor='')
    assert exc.value.code == 'actor_required'
    with pytest.raises(FundError) as exc:
        resolve(actor='a' * 129)
    assert exc.value.code == 'actor_too_long'
    with pytest.raises(FundError) as exc:
        resolve(actor='raricy_session=deadbeef; Path=/')     # a raw session, not an opaque hash
    assert exc.value.code == 'invalid_actor'
    with pytest.raises(FundError) as exc:
        resolve(version='0')
    assert exc.value.code == 'invalid_version'

    # Nothing above may have moved money or burned the version.
    detail = led.unclaimed_detail(F, row['id'])
    assert detail['version'] == 0 and detail['resolution_status'] == 'unclaimed'
    assert led.store.list_items('payouts') == []
    assert led.store.get('subscriptions', order['id'])['status'] == 'pending'
    assert led.status(F)['unclaimed_units'] == 105 * U


def test_forged_authority_fields_are_not_accepted_by_the_domain(led):
    funded(led)
    led.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    misdirected(led)
    row = led.unclaimed(F)[0]
    # The domain takes no amount/payer/time from the caller: an operator payload that
    # tries to smuggle one in cannot even be passed, and no `live` switch lives here.
    with pytest.raises(TypeError):
        led.resolve_unclaimed(F, row['id'], 'refund', version=0, reason='r', actor='a',
                              verified_transfer=receipt('in-1', 'payer', 105 * U, 'wrong note',
                                                        T + 1000, 91),
                              amount_units=105 * U, now_ms=T + 3000)
    with pytest.raises(TypeError):
        led.resolve_unclaimed(F, row['id'], 'refund', version=0, reason='r', actor='a',
                              verified_transfer=receipt('in-1', 'payer', 105 * U, 'wrong note',
                                                        T + 1000, 91),
                              live=False, now_ms=T + 3000)
    # An unknown action is reported, never guessed into link/refund.
    preview = led.preview_unclaimed(F, row['id'], 'wipe', now_ms=T + 3000)
    assert preview['eligible'] is False
    assert 'invalid_action' in preview['errors']
    with pytest.raises(FundError) as exc:
        led.resolve_unclaimed(F, row['id'], 'wipe', version=0, reason='r', actor='a',
                              verified_transfer=receipt('in-1', 'payer', 105 * U, 'wrong note',
                                                        T + 1000, 91),
                              now_ms=T + 3000)
    assert exc.value.code == 'invalid_action'


# ------------------------------------------------------------------ read-only
def test_preview_and_detail_have_no_side_effects(led):
    when = ms(2026, 9, 4)
    led.mark_account(F, 1000 * U, 0, when, when)
    led.seed(F, 'institution', 1000 * U, when)
    order = led.create_subscription(F, 'payer', 100 * U, 'sept', when)
    misdirected(led, occurred=when + 1000, now=when + 2000,
                transfer_id='sept-in', row_id=7)
    row = row_for(led, 'sept-in')
    verified = receipt('sept-in', 'payer', 105 * U, 'wrong note', when + 1000, 7)

    before = {name: led.store.list_items(name)
              for name in ('unclaimed', 'subscriptions', 'transfers', 'payouts',
                           'notices', 'cutoffs', 'periods', 'funds')}
    before_events = led.store.events(50)
    now = T     # past September's cutoff: a writing preview would freeze a snapshot
    led.preview_unclaimed(F, row['id'], 'link', subscription_id=order['id'],
                          verified_transfer=verified, now_ms=now)
    led.preview_unclaimed(F, row['id'], 'refund', verified_transfer=verified, now_ms=now)
    led.unclaimed_detail(F, row['id'], now)
    led.unclaimed(F)
    assert {name: led.store.list_items(name) for name in before} == before
    assert led.store.events(50) == before_events


# --------------------------------------------------------------------- listing
def test_unclaimed_listing_filters_by_fund_and_resolution_status(led):
    funded(led)
    order = led.create_subscription(F, 'payer', 100 * U, 'sub-key', T)
    misdirected(led, transfer_id='in-1', row_id=91)
    misdirected(led, transfer_id='g-1', row_id=92, fund=G, now=T + 2000)
    led.create_subscription(G, 'payer2', 100 * U, 'sub-g', T)   # unrelated order

    assert len(led.unclaimed()) == 2
    assert [r['transfer_id'] for r in led.unclaimed(F)] == ['in-1']
    assert [r['transfer_id'] for r in led.unclaimed(G)] == ['g-1']
    assert len(led.unclaimed(F, status='unclaimed')) == 1
    assert led.unclaimed(F, status='linked') == []

    row = row_for(led, 'in-1')
    verified = receipt('in-1', 'payer', 105 * U, 'wrong note', T + 1000, 91)
    ledger_resolved = led.resolve_unclaimed(F, row['id'], 'link', version=0, reason='link it',
                                           actor='session-a', verified_transfer=verified,
                                           subscription_id=order['id'], now_ms=T + 3000)
    assert ledger_resolved['status'] == 'linked'
    assert [r['transfer_id'] for r in led.unclaimed(F, status='linked')] == ['in-1']
    assert led.unclaimed(F, status='unclaimed') == []
    with pytest.raises(FundError) as exc:
        led.unclaimed_detail(G, row['id'])
    assert exc.value.code == 'unclaimed_not_found'
    with pytest.raises(FundError) as exc:
        led.unclaimed_detail(F, 'no-such-id')
    assert exc.value.code == 'unclaimed_not_found'
