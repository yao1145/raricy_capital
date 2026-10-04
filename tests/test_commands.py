"""Focused tests for the private command handler and the payments worker.

Everything external is mocked: no network, no real fund account, no LLM.
Only the command/payment behaviour required by the plan is covered —
amounts, dedupe, access scope, the live write guard, payout retry with the
same idempotency key, late-arriving transfers, and the durable notice outbox.
"""
from __future__ import annotations

import pytest

from raricy_capital.commands import CommandHandler
from raricy_capital.contracts import FundError
from raricy_capital.payments import (
    NOTICE_QUEUED,
    NOTICE_SENT,
    NOTICE_UNCERTAIN,
    NoticeOutbox,
    PaymentsWorker,
    qr_png_bytes,
)
from raricy_capital.store import FundStore

NOW = 1_700_000_000_000
FUND = 'capital1'
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------
class FakeLedger:
    def __init__(self, nav: str = '1.1000'):
        self.nav = nav
        self.created: list[tuple] = []
        self.redemptions: list[tuple] = []
        self.received: list[tuple] = []
        self.expired: list[int] = []
        self.payouts: list[dict] = []
        self.paid: list[tuple] = []
        self.status_calls: list[tuple] = []
        self._seen_transfers: set[str] = set()

    def create_subscription(self, fund_id, user_id, principal_units, message_key, now_ms):
        for _, _, _, key, _ in self.created:
            if key == message_key:
                return self._subscription(user_id, principal_units, message_key, now_ms)
        self.created.append((fund_id, user_id, int(principal_units), message_key, now_ms))
        return self._subscription(user_id, principal_units, message_key, now_ms)

    @staticmethod
    def _subscription(user_id, principal_units, message_key, now_ms):
        principal = int(principal_units)
        fee = principal * 5 // 100
        return {'id': 'SUB-1', 'user_id': user_id, 'principal_units': principal,
                'fee_units': fee, 'total_units': principal + fee,
                'payment_note': 'note-SUB-1', 'expires_ms': now_ms + 180_000,
                'status': 'pending'}

    def request_redemption(self, fund_id, user_id, amount_units, kind, message_key, now_ms):
        self.redemptions.append((fund_id, user_id, int(amount_units), kind, message_key, now_ms))
        return {'id': 'RD-1', 'nav': self.nav, 'kind': kind, 'status': 'pending'}

    def status(self, fund_id, user_id=None):
        self.status_calls.append((fund_id, user_id))
        out = {'fund_id': fund_id, 'label': '温和增长 · ER12', 'state': 'paused',
               'nav': self.nav, 'updated_ms': NOW}
        if user_id:
            out['user_shares_atoms'] = 150_000_000  # 1.5 shares
            out['user_value_units'] = 1_650_000     # 165.0000
        return out

    def receive_transfer(self, fund_id, tx, now_ms):
        if tx['transfer_id'] in self._seen_transfers:
            return {'transfer_id': tx['transfer_id'], 'duplicated': True}
        self._seen_transfers.add(tx['transfer_id'])
        self.received.append((fund_id, dict(tx), now_ms))
        return {'transfer_id': tx['transfer_id'], 'status': 'received'}

    def expire_subscriptions(self, now_ms):
        self.expired.append(now_ms)
        return 0

    def pending_payouts(self, fund_id=None):
        return list(self.payouts)

    def mark_payout_paid(self, payout_id, transfer_id, now_ms):
        self.paid.append((payout_id, transfer_id, now_ms))


class FakeClient:
    def __init__(self, *, pay_url='https://pay.example/x', send_results=None,
                 transfer_results=None, transactions_pages=None, messages=None):
        self.calls: list[tuple] = []
        self.tx_calls: list[int] = []
        self._pay_url = pay_url
        self._send_results = list(send_results or [])
        self._transfer_results = list(transfer_results or [])
        self._tx_pages = list(transactions_pages or [])
        self._messages = list(messages or [])

    async def pay_url(self, user_id, amount_units, note, order_key):
        self.calls.append(('pay_url', user_id, amount_units, note, order_key))
        return f'{self._pay_url}/{order_key}'

    async def send_message(self, channel_id, content, reply_to=None, image_bytes=None):
        self.calls.append(('send_message', channel_id, content, image_bytes))
        if self._send_results:
            return self._send_results.pop(0)
        return {'ok': True}

    async def transactions(self, since_id):
        self.tx_calls.append(since_id)
        if self._tx_pages:
            return self._tx_pages.pop(0)
        return {'transactions': [], 'next_cursor': since_id, 'has_more': False}

    async def transfer(self, user_id, amount_units, note, key):
        self.calls.append(('transfer', user_id, amount_units, key))
        if self._transfer_results:
            result = self._transfer_results.pop(0)
        else:
            result = {'transfer_id': 'TR-AUTO', 'duplicated': False}
        if isinstance(result, Exception):
            raise result
        return result

    async def fetch_messages(self, channel_id, after=None):
        self.calls.append(('fetch_messages', channel_id, after))
        return list(self._messages)

    async def private_channels(self):
        return []

    async def login(self):
        return {'id': 'u_bot', 'username': 'bot'}


class SyncPayClient(FakeClient):
    """The contract does not mark pay_url async; make sure both shapes work."""

    def pay_url(self, user_id, amount_units, note, order_key):  # noqa: D102
        self.calls.append(('pay_url', user_id, amount_units, note, order_key))
        return f'{self._pay_url}/{order_key}'


def message(mid, content, user='u1', channel='d_1'):
    return {'id': mid, 'channel_id': channel, 'content': content,
            'author': {'id': user, 'username': 'user-' + str(user)}}


def sent_messages(client):
    return [c for c in client.calls if c[0] == 'send_message']


def notice_of(store, key):
    return store.get('notices', key)


@pytest.fixture()
def store(tmp_path):
    s = FundStore(tmp_path / 'funds.db')
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
async def test_subscription_delivers_qr_url_and_expiry(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)

    await handler.handle(message(10, '/subscription 100'), now_ms=NOW)

    assert len(ledger.created) == 1
    _, user_id, principal, message_key, _ = ledger.created[0]
    assert (user_id, principal) == ('u1', 1_000_000)  # 100.0000 in 1e-4 units
    assert message_key == f'sub:{FUND}:10'

    sends = sent_messages(client)
    assert len(sends) == 1
    _, channel, content, image = sends[0]
    assert channel == 'd_1'
    assert 'https://pay.example/x/SUB-1' in content
    assert '180 秒' in content and '退款' in content and '105.0000' in content
    assert image is None or image.startswith(PNG_MAGIC)

    record = notice_of(store, f'reply:{FUND}:10')
    assert record is not None and record['status'] == NOTICE_SENT


async def test_subscription_accepts_sync_pay_url(store):
    ledger, client = FakeLedger(), SyncPayClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(11, '/subscription 1.2345'), now_ms=NOW)
    assert ledger.created[0][2] == 12_345
    assert 'https://pay.example/x' in sent_messages(client)[0][2]


@pytest.mark.parametrize('text', ['/subscription 0', '/subscription -5',
                                  '/subscription 1.23456', '/subscription abc', '/subscription'])
async def test_subscription_rejects_bad_amounts(store, text):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(20, text), now_ms=NOW)
    assert ledger.created == []
    assert '金额' in handler.last_reply


async def test_duplicate_message_is_processed_once(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    msg = message(30, '/subscription 100')
    await handler.handle(msg, now_ms=NOW)
    await handler.handle(dict(msg), now_ms=NOW)  # redelivery
    assert len(ledger.created) == 1
    assert len(sent_messages(client)) == 1


async def test_bot_author_is_never_an_investor(store):
    ledger, client = FakeLedger(), FakeClient()
    client.user_id = 'u_bot'  # the logged-in fund account's own stable id
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(35, '/check', user='u_bot'), now_ms=NOW)
    assert ledger.status_calls == []
    assert ledger.created == []
    assert sent_messages(client) == []
    assert store.list('channels') == []


async def test_restart_replay_recovers_unfinished_intent(store):
    # A process that claimed a command but died before its reply intent reached
    # the durable outbox leaves a ``processing`` row: the redelivery must re-run
    # the idempotent ledger call and still deliver the reply.
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    msg = message(120, '/subscription 100')
    store.put('messages', f'{FUND}:120', {
        'fund_id': FUND, 'user_id': 'u1', 'channel_id': 'd_1',
        'state': 'processing', 'created_ms': NOW, 'updated_ms': NOW})

    await handler.handle(msg, now_ms=NOW)

    assert len(ledger.created) == 1          # ledger dispatch is idempotent
    assert len(sent_messages(client)) == 1   # reply is delivered on replay
    assert notice_of(store, f'reply:{FUND}:120')['status'] == NOTICE_SENT
    assert store.get('messages', f'{FUND}:120')['state'] == 'completed'

    # A completed message is a no-op on any later redelivery.
    await handler.handle(dict(msg), now_ms=NOW + 5000)
    assert len(ledger.created) == 1
    assert len(sent_messages(client)) == 1

    # A restart between "outbox prepared" and "completed" must requeue the same
    # stable outbox id and never re-run the ledger dispatch.
    ledger2, client2 = FakeLedger(), FakeClient()
    handler2 = CommandHandler(ledger2, store, client2, FUND, live=True)

    dispatched: list[bool] = []

    def must_not_dispatch(*args, **kwargs):
        dispatched.append(True)
        raise AssertionError('prepared intent must not re-dispatch')

    ledger2.create_subscription = must_not_dispatch
    store.put('messages', f'{FUND}:121', {
        'fund_id': FUND, 'user_id': 'u1', 'channel_id': 'd_1',
        'state': 'prepared', 'created_ms': NOW, 'updated_ms': NOW})
    handler2.outbox.enqueue(FUND, '旧回复', user_id='u1', channel_id='d_1',
                            event_id=f'reply:{FUND}:121', now_ms=NOW)

    await handler2.handle(message(121, '/subscription 50'), now_ms=NOW)

    assert dispatched == []                 # no ledger dispatch at all
    assert len(sent_messages(client2)) == 1
    assert sent_messages(client2)[0][2] == '旧回复'  # same durable outbox content
    assert store.get('messages', f'{FUND}:121')['state'] == 'completed'


async def test_lobby_and_plain_chat_are_ignored(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(40, '/subscription 100', channel='lobby'), now_ms=NOW)
    await handler.handle(message(41, '你好'), now_ms=NOW)
    assert ledger.created == []
    assert sent_messages(client) == []


async def test_opaque_direct_channel_id_is_accepted(store):
    # Regression: the site's direct channels carry opaque uuids, not a "d_"
    # prefix.  Requiring that prefix silently dropped every real command while
    # the poll cursor kept advancing, so nothing was ever retried.
    channel = '7d7946a9-3f3e-481d-8792-7deb029293aa'
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(42, '/check', channel=channel), now_ms=NOW)
    assert ledger.status_calls == [(FUND, 'u1')]
    sends = sent_messages(client)
    assert len(sends) == 1 and sends[0][1] == channel


async def test_check_reads_only_own_holder_view(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(50, '/check'), now_ms=NOW)
    assert ledger.status_calls == [(FUND, 'u1')]
    reply = handler.last_reply
    assert '1.5' in reply and '165.0000' in reply and '仅显示您本人' in reply
    # The actual ledger.state is shown next to the caller's own values.
    assert 'paused' in reply and '已暂停' in reply


async def test_redemption_and_emergency_windows_and_fees(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)

    await handler.handle(message(60, '/redemption 50'), now_ms=NOW)
    assert ledger.redemptions[-1][2] == 500_000
    assert ledger.redemptions[-1][3] == 'ordinary'
    assert '净值变化' in handler.last_reply and '20%' in handler.last_reply

    await handler.handle(message(61, '/emergency 50'), now_ms=NOW)
    assert ledger.redemptions[-1][3] == 'emergency'
    assert '10%' in handler.last_reply and '18:00' in handler.last_reply


async def test_fund_error_from_ledger_is_surfaced(store):
    ledger, client = FakeLedger(), FakeClient()

    def reject(*args, **kwargs):
        from raricy_capital.contracts import FundError
        raise FundError('no_valid_nav')

    ledger.request_redemption = reject
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(70, '/emergency 50'), now_ms=NOW)
    assert '净值' in handler.last_reply


async def test_non_live_persists_intent_but_never_sends(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=False)  # default
    await handler.handle(message(80, '/subscription 100'), now_ms=NOW)

    assert len(ledger.created) == 1          # intent persisted first
    assert sent_messages(client) == []       # no external write
    record = notice_of(store, f'reply:{FUND}:80')
    assert record is not None and record['status'] == NOTICE_QUEUED


async def test_help_is_deterministic(store):
    ledger, client = FakeLedger(), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(90, '/help'), now_ms=NOW)
    assert '/subscription' in handler.last_reply and '/emergency' in handler.last_reply
    # The help explains that a cash-deferred emergency exit may still be withdrawn.
    assert '顺延' in handler.last_reply and '撤回' in handler.last_reply
    await handler.handle(message(91, '/nope'), now_ms=NOW)
    assert '未识别' in handler.last_reply


class ErrorLedger(FakeLedger):
    """A ledger whose order cancellation always fails with a business code."""

    def __init__(self, code):
        super().__init__()
        self.code = code

    def cancel_order(self, fund_id, user_id, order_id, now_ms):
        raise FundError(self.code)


@pytest.mark.parametrize('code,expected', [
    ('nav_unavailable', '有效净值'),
    ('unknown_order', '订单编号'),
    ('not_owner', '本人'),
    ('order_settled', '已确认'),
    ('deadline_passed', '截止'),
])
async def test_cancel_error_codes_are_localized(store, code, expected):
    ledger, client = ErrorLedger(code), FakeClient()
    handler = CommandHandler(ledger, store, client, FUND, live=True)
    await handler.handle(message(200, '/redemption cancel RD-404'), now_ms=NOW)
    reply = handler.last_reply
    assert expected in reply
    assert code not in reply                 # the raw code never leaks to the investor


# --------------------------------------------------------------------------
# payments worker
# --------------------------------------------------------------------------
def transfer_tx(transfer_id='t1', from_user='u1', amount=1_050_000, note='note-SUB-1',
                occurred=NOW - 1000, tx_type=None):
    return {'transfer_id': transfer_id, 'from_user_id': from_user, 'amount_units': amount,
            'note': note, 'occurred_ms': occurred, 'type': tx_type}


async def test_cursor_pagination_ingests_each_transfer(store):
    ledger = FakeLedger()
    pages = [
        {'transactions': [transfer_tx('t1')], 'next_cursor': 5, 'has_more': True},
        {'transactions': [transfer_tx('t2', from_user='u2')], 'next_cursor': 9, 'has_more': False},
    ]
    client = FakeClient(transactions_pages=pages)
    worker = PaymentsWorker(store, ledger, {FUND: client}, live=True)

    await worker.tick(NOW)
    assert [t[1]['transfer_id'] for t in ledger.received] == ['t1', 't2']
    assert client.tx_calls == [0, 5]
    assert store.get('pay_cursors', FUND)['since_id'] == 9

    await worker.tick(NOW + 1000)
    assert client.tx_calls == [0, 5, 9]
    assert len(ledger.received) == 2  # empty page, no duplicates


async def test_late_arrival_keeps_occurred_ms(store):
    ledger = FakeLedger()
    late = transfer_tx('t-late', occurred=NOW - 250_000)
    client = FakeClient(transactions_pages=[
        {'transactions': [late, transfer_tx('t-out', amount=-5_000, tx_type='transfer')],
         'next_cursor': 3, 'has_more': False}])
    worker = PaymentsWorker(store, ledger, {FUND: client}, live=True)

    await worker.tick(NOW)
    assert len(ledger.received) == 1
    _, row, _ = ledger.received[0]
    assert row['transfer_id'] == 't-late'
    assert row['occurred_ms'] == NOW - 250_000  # authority is the transfer time


async def test_payout_retries_with_same_key_then_marks_paid(store):
    ledger = FakeLedger()
    ledger.payouts = [{'id': 'P1', 'fund_id': FUND, 'user_id': 'u1',
                       'amount_units': 1_050_000, 'note': 'payout',
                       'idempotency_key': 'wd-1', 'status': 'pending'}]
    client = FakeClient(transfer_results=[RuntimeError('timeout'),
                                          {'transfer_id': 'TR-9', 'duplicated': False}])
    worker = PaymentsWorker(store, ledger, {FUND: client}, live=True)

    await worker.tick(NOW)
    assert ledger.paid == []
    assert notice_of(store, 'pay-uncertain:P1') is not None  # durable + notified

    await worker.tick(NOW + 1000)
    assert ledger.paid == [('P1', 'TR-9', NOW + 1000)]
    keys = [c[3] for c in client.calls if c[0] == 'transfer']
    assert keys == ['wd-1', 'wd-1']  # identical idempotency key on retry


async def test_payout_never_transfers_when_not_live(store):
    ledger = FakeLedger()
    ledger.payouts = [{'id': 'P2', 'fund_id': FUND, 'user_id': 'u1',
                       'amount_units': 1_000_000, 'note': 'payout',
                       'idempotency_key': 'wd-2', 'status': 'pending'}]
    client = FakeClient()
    worker = PaymentsWorker(store, ledger, {FUND: client}, live=False)

    await worker.tick(NOW)
    assert [c for c in client.calls if c[0] == 'transfer'] == []
    assert ledger.paid == []
    events = [e['event'] for e in store.events(50)]
    assert 'payouts_held_not_live' in events


# --------------------------------------------------------------------------
# notice outbox
# --------------------------------------------------------------------------
async def test_send_returning_none_is_not_marked_sent(store):
    outbox = NoticeOutbox(store)
    client = FakeClient(send_results=[None, {'ok': True}])
    outbox.remember_channel(FUND, 'u1', 'd_1', now_ms=NOW)
    outbox.enqueue(FUND, '分红公告', user_id='u1', event_id='E-NONE', now_ms=NOW)

    await outbox.deliver(lambda f: client, live=True, now_ms=NOW)
    record = notice_of(store, 'E-NONE')
    assert record['deliveries']['u1']['status'] == NOTICE_UNCERTAIN

    await outbox.deliver(lambda f: client, live=True, now_ms=NOW + 1000)
    assert notice_of(store, 'E-NONE')['deliveries']['u1']['status'] == NOTICE_SENT
    assert len(sent_messages(client)) == 2


async def test_uncertain_notice_reconciled_from_channel_history(store):
    outbox = NoticeOutbox(store)
    client = FakeClient(messages=[{'id': 8, 'content': '网络已恢复，支付对账与付款已继续处理。',
                                   'author': {'id': 'u_bot'}}])
    outbox.remember_channel(FUND, 'u1', 'd_1', last_message_id=5, now_ms=NOW)
    outbox.enqueue(FUND, '网络已恢复，支付对账与付款已继续处理。', user_id='u1',
                   event_id='E-REC', now_ms=NOW)
    record = notice_of(store, 'E-REC')
    record['deliveries']['u1']['status'] = NOTICE_UNCERTAIN
    store.put('notices', 'E-REC', record)

    await outbox.deliver(lambda f: client, live=True, now_ms=NOW)
    assert notice_of(store, 'E-REC')['deliveries']['u1']['status'] == NOTICE_SENT
    assert sent_messages(client) == []  # reconciled, no resend
    assert ('fetch_messages', 'd_1', 5) in client.calls


async def test_fanout_uses_only_actual_holder_channels(store):
    outbox = NoticeOutbox(store)
    client = FakeClient()
    outbox.remember_channel(FUND, 'u1', 'd_1', now_ms=NOW)
    outbox.remember_channel(FUND, 'u2', 'd_2', now_ms=NOW)  # correspondent, zero shares
    outbox.remember_channel(FUND, 'u3', 'd_3', now_ms=NOW)  # correspondent, no holding
    outbox.remember_channel(FUND, 'u9', 'd_9', now_ms=NOW)  # refund recipient, no holding
    store.put('holders', f'{FUND}:u1', {'fund_id': FUND, 'user_id': 'u1', 'shares_atoms': 10})
    store.put('holders', f'{FUND}:u2', {'fund_id': FUND, 'user_id': 'u2', 'shares_atoms': 0})
    # A holder-wide notice reaches only the real holder …
    outbox.enqueue(FUND, '基金净值更新', user_id=None, event_id='E-FAN', now_ms=NOW)
    # … while an explicitly addressed non-holder (a refund) is still delivered.
    outbox.enqueue(FUND, '超时到账，已原路全额退款', user_id='u9', event_id='E-REFUND', now_ms=NOW)

    await outbox.deliver(lambda f: client, live=True, now_ms=NOW)
    channels = sorted(c[1] for c in sent_messages(client))
    assert channels == ['d_1', 'd_9']
    assert notice_of(store, 'E-FAN')['status'] == NOTICE_SENT
    assert notice_of(store, 'E-REFUND')['status'] == NOTICE_SENT


async def test_notice_intent_is_durable_before_any_send(store):
    outbox = NoticeOutbox(store)
    keys = outbox.enqueue(FUND, '永久停机', user_id=None, event_id='E-DUR', now_ms=NOW)
    assert keys == ['E-DUR']
    # A fresh outbox over the same store still sees the queued intent.
    reopened = NoticeOutbox(store)
    assert reopened.store.get('notices', 'E-DUR')['status'] == NOTICE_QUEUED


async def test_network_recovery_notice_after_offline_poll(store):
    ledger = FakeLedger()
    store.put('pay_cursors', FUND, {'since_id': 0, 'online': False, 'updated_ms': NOW - 5000})
    client = FakeClient()
    worker = PaymentsWorker(store, ledger, {FUND: client}, live=True)

    await worker.tick(NOW)
    assert store.get('pay_cursors', FUND)['online'] is True
    notices = store.list('notices')
    assert any('网络已恢复' in n['content'] for n in notices)


async def test_tick_surfaces_offline_fund_without_dropping_others(store):
    ledger = FakeLedger()

    class FailingClient(FakeClient):
        async def transactions(self, since_id):
            raise RuntimeError('transport down')

    good = FakeClient(transactions_pages=[
        {'transactions': [transfer_tx('t1', from_user='u7')],
         'next_cursor': 2, 'has_more': False}])
    worker = PaymentsWorker(store, ledger, {FUND: FailingClient(), 'capital2': good}, live=True)

    result = await worker.tick(NOW)

    # The failing fund is observable instead of being silently swallowed …
    assert result['online'] is False
    assert any(e.startswith('payments_offline:') and FUND in e for e in result['errors'])
    assert store.get('pay_cursors', FUND)['online'] is False
    # … and the healthy fund is still polled in the same tick.
    assert result['received'] == 1
    assert good.tx_calls == [0]


async def test_qr_png_bytes_encodes_payload():
    pytest.importorskip('qrcode')  # noqa: F841  (Codex adds the dependency)
    pytest.importorskip('PIL')
    data = qr_png_bytes('https://pay.example/x/SUB-1')
    assert data and data.startswith(PNG_MAGIC)


def test_qr_png_bytes_never_raises_on_empty():
    assert qr_png_bytes('') is None
