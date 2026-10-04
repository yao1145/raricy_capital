"""Payment polling, durable payout retries and the durable notice outbox.

Money rules (v0.3, frozen contracts):
  * amounts are integer 1e-4 units, never floats;
  * every external write (transfer / chat message) is persisted as an intent
    *before* the request goes out;
  * an outgoing transfer is always retried with the *same* idempotency key;
  * a chat message that returns ``None`` must never be marked as sent;
  * a failed / timed out payout is announced as "uncertain" and reconciled,
    never silently dropped and never paid twice.

The worker/handler ``live`` flag deliberately defaults to ``False``: a
standalone call must not move fish-credits or post chat messages by accident.
The runtime owns the master switch and turns it on only once config and real
fund accounts are in place.
"""
from __future__ import annotations

import hashlib
import io
from decimal import Decimal

from .contracts import POLICIES, FundError, money_units, now_ms as _now_ms

# Notice lifecycle. "uncertain" is a real state: the external call may or may
# not have happened, so it must be reconciled before any resend.
NOTICE_QUEUED = 'queued'
NOTICE_SENDING = 'sending'
NOTICE_UNCERTAIN = 'uncertain'
NOTICE_SENT = 'sent'

# Payout rows we are willing to re-drive on a later tick.
PAYOUT_OPEN_STATUSES = {'pending', 'approved', 'ready', None}

# Manual unclaimed-receipt refunds. They are ordinary payout rows of a fixed
# kind, driven by their own guarded path: available-cash gate, persisted
# sending/uncertain intent and outgoing-ledger reconciliation before any replay.
MANUAL_REFUND_KIND = 'unclaimed_refund'
INSTITUTION_FEE_KIND = 'subscription_fee'
GUARDED_PAYOUT_KINDS = {MANUAL_REFUND_KIND, INSTITUTION_FEE_KIND}
MANUAL_REFUND_DONE = {'paid', 'cancelled'}
MANUAL_REFUND_UNSETTLED = {'sending', 'uncertain'}

MAX_PAGES = 20  # per fund, per tick


def _as_int(value):
    if value is None or isinstance(value, bool):
        return None
    return value if type(value) is int else None


def manual_refund_payouts(store, fund_id: str | None = None) -> list[dict]:
    """Queued manual refunds that have not reached a final state yet."""
    prefix = f'{fund_id}:' if fund_id else ''
    out = []
    for _, payout in store.list_items('payouts', prefix):
        if get_field(payout, 'kind') != MANUAL_REFUND_KIND:
            continue
        if get_field(payout, 'status') in MANUAL_REFUND_DONE:
            continue
        out.append(payout)
    return sorted(out, key=lambda r: get_field(r, 'created_ms') or 0)


def get_field(obj, name, default=None):
    """Read ``name`` from a mapping or an object attribute (client DTOs)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def shares_text(atoms) -> str:
    """Render integer share atoms (1e-8) as a plain decimal string."""
    try:
        value = Decimal(int(atoms)) / Decimal(10 ** 8)
    except (TypeError, ValueError):
        return '0'
    return format(value.normalize(), 'f')


def qr_png_bytes(payload: str) -> bytes | None:
    """Encode ``payload`` as a PNG QR image, or return ``None`` if unavailable.

    ``qrcode[pil]`` is an optional funds dependency; when it is missing we
    still deliver the payment URL as text instead of failing the order.
    """
    if not payload:
        return None
    try:
        import qrcode  # type: ignore
    except Exception:
        return None
    try:
        qr = qrcode.QRCode(border=1, box_size=8)
        qr.add_data(payload)
        qr.make(fit=True)
        image = qr.make_image(fill_color='black', back_color='white')
        buffer = io.BytesIO()
        image.save(buffer, format='PNG')
        return buffer.getvalue()
    except Exception:
        return None


def notice_key(fund_id: str, user_id: str | None, content: str, event_id: str | None = None) -> str:
    """Stable, persistent notice id used as the ``notices`` record key."""
    if event_id:
        return str(event_id)
    digest = hashlib.sha1(f'{fund_id}|{user_id or "*"}|{content}'.encode('utf-8')).hexdigest()[:16]
    return f'{fund_id}:{digest}'


def _aggregate(deliveries: dict) -> str:
    if not deliveries:
        return NOTICE_QUEUED
    statuses = {d.get('status') for d in deliveries.values()}
    if statuses == {NOTICE_SENT}:
        return NOTICE_SENT
    if NOTICE_UNCERTAIN in statuses:
        return NOTICE_UNCERTAIN
    if NOTICE_SENDING in statuses:
        return NOTICE_SENDING
    return NOTICE_QUEUED


class NoticeOutbox:
    """Durable queued notices fanned out over stored private channels.

    Record layout in the ``notices`` namespace (key = persistent event id)::

        {id, fund_id, user_id, content, image_payload, kind, created_ms,
         status, deliveries: {key: {user_id, channel_id, status, attempts,
                                    since_id, last_error, updated_ms}}}

    ``user_id`` is ``None`` for notices that fan out to every stored holder
    channel of the fund. ``image_payload`` is the raw QR payload (the payment
    URL), not bytes — the image is regenerated at send time so the record stays
    JSON-serialisable and the intent survives a restart.
    """

    def __init__(self, store):
        self.store = store

    # ---- channel bookkeeping -------------------------------------------
    def remember_channel(self, fund_id: str, user_id: str, channel_id: str, *,
                         last_message_id=None, now_ms=None) -> None:
        key = f'{fund_id}:{user_id}'
        with self.store.transaction():
            rec = self.store.get('channels', key) or {
                'fund_id': fund_id, 'user_id': user_id, 'channel_id': channel_id,
                'last_message_id': 0,
            }
            rec['channel_id'] = channel_id
            previous = int(rec.get('last_message_id') or 0)
            if last_message_id is not None:
                try:
                    seen = int(last_message_id)
                except (TypeError, ValueError):
                    seen = previous
                rec['last_message_id'] = max(previous, seen)
            rec['updated_ms'] = int(now_ms) if now_ms is not None else _now_ms()
            self.store.put('channels', key, rec)

    def channel_for(self, fund_id: str, user_id: str | None) -> str | None:
        if not user_id:
            return None
        rec = self.store.get('channels', f'{fund_id}:{user_id}')
        return (rec or {}).get('channel_id')

    def holder_channels(self, fund_id: str) -> list[dict]:
        out = []
        for _, value in self.store.list_items('channels', prefix=f'{fund_id}:'):
            channel_id = value.get('channel_id')
            if channel_id:
                out.append({
                    'user_id': value.get('user_id'),
                    'channel_id': channel_id,
                    'last_message_id': value.get('last_message_id'),
                })
        return out

    def holder_user_ids(self, fund_id: str) -> set[str]:
        """Stable user ids that actually hold shares (``shares_atoms > 0``).

        Read straight from the ledger's ``holders`` records: a private channel is
        not proof of a holding, so fan-out must not treat every correspondent as
        an investor.
        """
        users: set[str] = set()
        for key, value in self.store.list_items('holders', prefix=f'{fund_id}:'):
            if not isinstance(value, dict):
                continue
            try:
                shares = int(value.get('shares_atoms') or 0)
            except (TypeError, ValueError):
                shares = 0
            if shares <= 0:
                continue
            user_id = value.get('user_id') or key.split(':', 1)[-1]
            if user_id:
                users.add(str(user_id))
        return users

    def _fanout_channels(self, fund_id: str) -> list[dict]:
        """Channels eligible for holder-wide notices (holders with shares only)."""
        holders = self.holder_user_ids(fund_id)
        if not holders:
            return []
        return [row for row in self.holder_channels(fund_id)
                if str(row.get('user_id')) in holders]

    def _channel_last_id(self, fund_id: str, user_id: str | None, channel_id: str | None):
        if user_id:
            rec = self.store.get('channels', f'{fund_id}:{user_id}')
            if rec:
                return rec.get('last_message_id')
        if channel_id:
            for _, value in self.store.list_items('channels', prefix=f'{fund_id}:'):
                if value.get('channel_id') == channel_id:
                    return value.get('last_message_id')
        return None

    def _expand_holders(self, record: dict) -> None:
        if record.get('user_id') is not None:
            return
        holders = self._fanout_channels(record['fund_id'])
        if not holders:
            return
        deliveries = record['deliveries']
        placeholder = deliveries.get('__holders__')
        for holder in holders:
            key = holder['user_id'] or holder['channel_id']
            if key not in deliveries:
                deliveries[key] = {
                    'user_id': holder['user_id'],
                    'channel_id': holder['channel_id'],
                    'status': NOTICE_QUEUED,
                    'attempts': 0,
                    'since_id': holder.get('last_message_id'),
                    'last_error': None,
                    'updated_ms': record['created_ms'],
                }
        if placeholder is not None and not placeholder.get('channel_id'):
            deliveries.pop('__holders__', None)

    def _ensure_deliveries(self, record: dict) -> None:
        """Upgrade a frozen-schema notice record to per-delivery tracking.

        Other modules may persist ``{id, fund_id, user_id, content, created_ms,
        status}`` notices directly; those must still be deliverable here.
        """
        if record.get('deliveries'):
            return
        record.setdefault('deliveries', {})
        record.setdefault('created_ms', _now_ms())
        user_id = record.get('user_id')
        if user_id is not None:
            record['deliveries'][user_id] = {
                'user_id': user_id,
                'channel_id': self.channel_for(record['fund_id'], user_id),
                'status': NOTICE_QUEUED,
                'attempts': 0,
                'since_id': self._channel_last_id(record['fund_id'], user_id, None),
                'last_error': None,
                'updated_ms': record['created_ms'],
            }
        else:
            record['deliveries']['__holders__'] = {
                'user_id': None, 'channel_id': None, 'status': NOTICE_QUEUED,
                'attempts': 0, 'since_id': None, 'last_error': None,
                'updated_ms': record['created_ms'],
            }

    # ---- enqueue --------------------------------------------------------
    def enqueue(self, fund_id: str, content: str, *, user_id: str | None = None,
                event_id: str | None = None, image_payload: str | None = None,
                channel_id: str | None = None, kind: str = 'notice',
                now_ms=None) -> list[str]:
        """Persist a notice intent (idempotent on ``event_id``)."""
        created = int(now_ms) if now_ms is not None else _now_ms()
        key = notice_key(fund_id, user_id, content, event_id)
        with self.store.transaction():
            record = self.store.get('notices', key)
            if record is None:
                record = {
                    'id': key, 'fund_id': fund_id, 'user_id': user_id,
                    'content': content, 'image_payload': image_payload,
                    'kind': kind, 'created_ms': created,
                    'status': NOTICE_QUEUED, 'deliveries': {},
                }
            elif record.get('status') == NOTICE_SENT:
                return [key]
            record.setdefault('deliveries', {})
            if user_id is not None:
                targets = [{
                    'user_id': user_id,
                    'channel_id': channel_id or self.channel_for(fund_id, user_id),
                }]
            elif channel_id:
                targets = [{'user_id': None, 'channel_id': channel_id}]
            else:
                holders = self._fanout_channels(fund_id)
                targets = [
                    {'user_id': h['user_id'], 'channel_id': h['channel_id']}
                    for h in holders
                ] or [{'user_id': None, 'channel_id': None}]
            for target in targets:
                delivery_key = target['user_id'] or target['channel_id'] or '__holders__'
                existing = record['deliveries'].get(delivery_key)
                if existing is not None:
                    if target['channel_id'] and not existing.get('channel_id'):
                        existing['channel_id'] = target['channel_id']
                    continue
                record['deliveries'][delivery_key] = {
                    'user_id': target['user_id'],
                    'channel_id': target['channel_id'],
                    'status': NOTICE_QUEUED,
                    'attempts': 0,
                    'since_id': self._channel_last_id(
                        fund_id, target['user_id'], target['channel_id']),
                    'last_error': None,
                    'updated_ms': created,
                }
            record['status'] = _aggregate(record['deliveries'])
            self.store.put('notices', key, record)
        return [key]

    # ---- delivery -------------------------------------------------------
    def _persist(self, key: str, record: dict) -> None:
        record['status'] = _aggregate(record.get('deliveries') or {})
        self.store.put('notices', key, record)

    @staticmethod
    def _normalized(text) -> str:
        return ' '.join(str(text or '').split())

    async def _reconcile(self, client, channel_id: str, record: dict,
                         delivery: dict, now_ms: int) -> bool:
        """Best-effort: did the (uncertain) message already reach the channel?

        We cannot promise exactly-once chat delivery; a failed probe falls back
        to a resend (at-least-once) on the next tick.
        """
        try:
            messages = await client.fetch_messages(channel_id, after=delivery.get('since_id'))
        except Exception:
            return False
        if not messages:
            return False
        target = self._normalized(record.get('content'))
        high = delivery.get('since_id')
        for message in messages:
            message_id = get_field(message, 'id')
            if isinstance(message_id, int) and (high is None or message_id > high):
                high = message_id
            if self._normalized(get_field(message, 'content', '')) == target:
                if high is not None:
                    delivery['since_id'] = high
                return True
        if high is not None:
            delivery['since_id'] = high
        return False

    async def deliver(self, client_for, *, live: bool, now_ms: int, limit: int = 50) -> dict:
        """Drain queued / uncertain notices. Returns counters for logging."""
        stats = {'sent': 0, 'queued': 0, 'uncertain': 0, 'skipped': 0, 'notices': 0}
        processed = 0
        for key, record in self.store.list_items('notices'):
            if processed >= limit:
                break
            if record.get('status') == NOTICE_SENT:
                continue
            processed += 1
            client = client_for(record['fund_id'])
            if client is None:
                stats['skipped'] += 1
                continue
            self._ensure_deliveries(record)
            self._expand_holders(record)
            image = None
            if live and record.get('image_payload'):
                image = qr_png_bytes(record['image_payload'])
            for delivery_key, delivery in list((record.get('deliveries') or {}).items()):
                if delivery.get('status') == NOTICE_SENT:
                    continue
                channel_id = delivery.get('channel_id') or self.channel_for(
                    record['fund_id'], delivery.get('user_id'))
                if channel_id:
                    delivery['channel_id'] = channel_id
                if not channel_id:
                    stats['queued'] += 1
                    continue
                if not live:
                    stats['skipped'] += 1
                    continue
                if delivery.get('status') in (NOTICE_UNCERTAIN, NOTICE_SENDING):
                    if await self._reconcile(client, channel_id, record, delivery, now_ms):
                        delivery['status'] = NOTICE_SENT
                        delivery['updated_ms'] = now_ms
                        stats['sent'] += 1
                        self._persist(key, record)
                        continue
                # Persist the intent before the external write.
                delivery['status'] = NOTICE_SENDING
                delivery['attempts'] = int(delivery.get('attempts') or 0) + 1
                delivery['updated_ms'] = now_ms
                self._persist(key, record)
                try:
                    result = await client.send_message(
                        channel_id, record['content'], reply_to=None, image_bytes=image)
                except Exception as exc:  # timeout / transport / 5xx
                    delivery['status'] = NOTICE_UNCERTAIN
                    delivery['last_error'] = type(exc).__name__
                    delivery['updated_ms'] = now_ms
                    stats['uncertain'] += 1
                    self._persist(key, record)
                    continue
                if result:
                    delivery['status'] = NOTICE_SENT
                    delivery['sent_ms'] = now_ms
                    stats['sent'] += 1
                else:
                    # A successful-looking send that returns None is NOT proof
                    # of delivery: keep it for reconciliation.
                    delivery['status'] = NOTICE_UNCERTAIN
                    delivery['last_error'] = 'empty_response'
                    stats['uncertain'] += 1
                delivery['updated_ms'] = now_ms
                self._persist(key, record)
            self._persist(key, record)
            stats['notices'] += 1
        return stats


class PaymentsWorker:
    """Poll fund-site payments, expire orders, pay out and flush notices.

    ``clients`` is a ``{fund_id: FundSiteClient}`` mapping (a single client or
    a ``for_fund(fund_id)`` provider is also accepted). All external writes are
    gated by ``live`` (default ``False``).
    """

    def __init__(self, store, ledger, clients, *, live: bool = False, outbox: NoticeOutbox | None = None):
        self.store = store
        self.ledger = ledger
        self.clients = clients
        self.live = live
        self.outbox = outbox or NoticeOutbox(store)

    # ---- wiring ---------------------------------------------------------
    def _fund_ids(self) -> list[str]:
        if isinstance(self.clients, dict):
            return list(self.clients.keys())
        keys = getattr(self.clients, 'keys', None)
        if callable(keys):
            try:
                return list(keys())
            except Exception:
                pass
        return list(POLICIES.keys())

    def _client_for(self, fund_id: str):
        clients = self.clients
        if isinstance(clients, dict):
            return clients.get(fund_id)
        provider = getattr(clients, 'for_fund', None)
        if callable(provider):
            return provider(fund_id)
        getter = getattr(clients, 'get', None)
        if callable(getter):
            try:
                return getter(fund_id)
            except Exception:
                return None
        return clients

    def enqueue_notice(self, fund_id: str, content: str, **kwargs) -> list[str]:
        return self.outbox.enqueue(fund_id, content, **kwargs)

    # ---- tick -----------------------------------------------------------
    async def tick(self, now_ms: int) -> dict:
        result = {'received': 0, 'expired': None, 'paid': 0, 'notices': None,
                  'refunds': None, 'errors': [], 'online': True}
        try:
            result['received'] = await self.poll_payments(now_ms, result['errors'])
        except Exception as exc:
            result['errors'].append(f'poll:{type(exc).__name__}')
        try:
            result['expired'] = self.ledger.expire_subscriptions(now_ms)
        except Exception as exc:
            result['errors'].append(f'expire:{type(exc).__name__}')
        try:
            result['paid'] = await self.drain_payouts(now_ms)
        except Exception as exc:
            result['errors'].append(f'payout:{type(exc).__name__}')
        try:
            result['refunds'] = await self.drain_manual_refunds(now_ms)
        except Exception as exc:
            # A stuck manual refund is surfaced on its own row and in the audit
            # trail; it must not raise a fund-wide trading hold by landing in
            # ``errors`` (the runtime turns those into ``reconciliation_holds``).
            self.store.append_event('manual_refunds_drain_failed', level='error',
                                    details={'error': type(exc).__name__}, created_ms=now_ms)
            result['refunds'] = {'seen': 0, 'paid': 0, 'reconciled': 0, 'held': 0,
                                 'waiting': 0, 'skipped': 0, 'error': type(exc).__name__}
        try:
            result['institution_fees'] = await self.drain_institution_fees(now_ms)
        except Exception as exc:
            self.store.append_event('institution_fees_failed', level='error',
                                    details={'error': type(exc).__name__}, created_ms=now_ms)
            result['institution_fees'] = {'error': type(exc).__name__}
        try:
            result['notices'] = await self.outbox.deliver(
                self._client_for, live=self.live, now_ms=now_ms)
        except Exception as exc:
            result['errors'].append(f'notice:{type(exc).__name__}')
        # A fund that could not be polled is a real failure even though it never
        # raised out of the loop: surface it instead of reporting a healthy tick.
        offline = self.offline_funds()
        if offline:
            result['online'] = False
            result['errors'].append('payments_offline:' + ','.join(offline))
        return result

    def offline_funds(self) -> list[str]:
        """Funds whose pay cursor is explicitly offline after the last poll.

        The root runtime reads the tick result (``online`` / ``errors``) to mark
        overall health; an unreachable fund must not be thrown away.
        """
        offline: list[str] = []
        for fund_id in self._fund_ids():
            state = self.store.get('pay_cursors', fund_id)
            if isinstance(state, dict) and state.get('online') is False:
                offline.append(fund_id)
        return sorted(offline)

    # ---- incoming payments ---------------------------------------------
    @staticmethod
    def _normalize_transfer(tx) -> dict | None:
        """Keep only incoming user transfers the ledger can match on."""
        if tx is None:
            return None
        tx_type = get_field(tx, 'type')
        if tx_type not in (None, 'transfer_receive'):
            return None
        transfer_id = get_field(tx, 'transfer_id')
        from_user_id = get_field(tx, 'from_user_id')
        if not transfer_id or not from_user_id:
            return None
        amount = get_field(tx, 'amount_units')
        if amount is None:
            raw = get_field(tx, 'amount')
            if raw is None:
                return None
            try:
                amount = money_units(str(raw))
            except FundError:
                return None
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return None
        if amount <= 0:
            return None
        occurred = get_field(tx, 'occurred_ms')
        try:
            occurred = int(occurred)
        except (TypeError, ValueError):
            occurred = 0
        return {
            'transfer_id': str(transfer_id),
            'from_user_id': str(from_user_id),
            'amount_units': amount,
            'note': get_field(tx, 'note') or '',
            'occurred_ms': occurred,
            # The authoritative site row id is kept so a later verification can
            # look the receipt up directly instead of rescanning the ledger.
            'transaction_row_id': _as_int(get_field(tx, 'id')),
        }

    def _ingest(self, fund_id: str, tx, now_ms: int) -> bool:
        row = self._normalize_transfer(tx)
        if row is None:
            return False
        activation = self.store.get('accounts', fund_id, {}).get('activation_ms', 0)
        if activation and row['occurred_ms'] < activation:
            return False  # Pre-service money is explicitly allocated by initial seed, never reimported.
        try:
            self.ledger.receive_transfer(fund_id, row, now_ms)
        except FundError as exc:
            # Ledger rejected the row (wrong amount etc.); nothing to credit.
            self.store.append_event('receive_transfer_rejected', fund_id=fund_id,
                                    level='warning', details={'code': exc.code},
                                    created_ms=now_ms)
            return False
        return True

    async def poll_payments(self, now_ms: int, errors: list[str] | None = None) -> int:
        received = 0
        for fund_id in self._fund_ids():
            client = self._client_for(fund_id)
            if client is None:
                continue
            try:
                received += await self._poll_fund(fund_id, client, now_ms)
            except Exception as exc:
                # Unexpected ledger failure: keep the cursor and surface it.
                self.store.append_event('payments_ingest_failed', fund_id=fund_id,
                                        level='error', details={'error': type(exc).__name__},
                                        created_ms=now_ms)
                if errors is not None:
                    errors.append(f'poll:{fund_id}:{type(exc).__name__}')
        return received

    async def _poll_fund(self, fund_id: str, client, now_ms: int) -> int:
        state = self.store.get('pay_cursors', fund_id, {}) or {}
        cursor = int(state.get('since_id') or 0)
        was_online = state.get('online', True)
        received = 0
        for _ in range(MAX_PAGES):
            try:
                page = await client.transactions(cursor)
            except Exception as exc:  # transport / auth / rate limit
                self.store.put('pay_cursors', fund_id,
                               {'since_id': cursor, 'online': False, 'updated_ms': now_ms})
                self.store.append_event('payments_poll_failed', fund_id=fund_id,
                                        level='warning', details={'error': type(exc).__name__},
                                        created_ms=now_ms)
                return received
            rows = list(get_field(page, 'transactions', []) or [])
            for tx in rows:
                # Unexpected ledger failures propagate: the cursor must not
                # advance past a transfer that was not credited.
                if self._ingest(fund_id, tx, now_ms):
                    received += 1
            next_cursor = get_field(page, 'next_cursor', cursor)
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                next_cursor = cursor
            if next_cursor < cursor:
                next_cursor = cursor
            cursor = next_cursor
            self.store.put('pay_cursors', fund_id,
                           {'since_id': cursor, 'online': True, 'updated_ms': now_ms})
            if not get_field(page, 'has_more', False):
                break
        if not was_online:
            self.outbox.enqueue(
                fund_id, '网络已恢复，支付对账与付款已继续处理。',
                event_id=f'net-recovered:{fund_id}:{now_ms // 60000}', now_ms=now_ms)
        return received

    # ---- outgoing payouts ----------------------------------------------
    async def drain_payouts(self, now_ms: int) -> int:
        try:
            payouts = list(self.ledger.pending_payouts() or [])
        except Exception as exc:
            self.store.append_event('pending_payouts_failed', level='error',
                                    details={'error': type(exc).__name__}, created_ms=now_ms)
            return 0
        if not self.live:
            if payouts:
                self.store.append_event('payouts_held_not_live', level='info',
                                        details={'pending': len(payouts)}, created_ms=now_ms)
            return 0
        paid = 0
        for payout in payouts:
            if get_field(payout, 'status') not in PAYOUT_OPEN_STATUSES:
                continue
            if get_field(payout, 'kind') in GUARDED_PAYOUT_KINDS:
                # Only the guarded manual-refund path may drive these: it adds the
                # available-cash gate and the reconciliation pass this loop lacks.
                continue
            fund_id = get_field(payout, 'fund_id')
            client = self._client_for(fund_id)
            if client is None:
                continue
            payout_id = get_field(payout, 'id')
            user_id = get_field(payout, 'user_id')
            amount_units = int(get_field(payout, 'amount_units') or 0)
            note = str(get_field(payout, 'note') or '')[:30]
            key = get_field(payout, 'idempotency_key') or str(payout_id)
            try:
                result = await client.transfer(user_id, amount_units, note, key)
            except Exception as exc:
                # Same key on the next tick makes the retry safe.
                self.store.append_event('payout_uncertain', fund_id=fund_id, level='warning',
                                        details={'payout_id': payout_id,
                                                 'error': type(exc).__name__},
                                        created_ms=now_ms)
                self.outbox.enqueue(
                    fund_id, '您的付款处理状态待确认，我们正在核对，不会重复支付。',
                    user_id=user_id, event_id=f'pay-uncertain:{payout_id}', now_ms=now_ms)
                continue
            if not result:
                self.store.append_event('payout_uncertain', fund_id=fund_id, level='warning',
                                        details={'payout_id': payout_id, 'error': 'empty_response'},
                                        created_ms=now_ms)
                self.outbox.enqueue(
                    fund_id, '您的付款处理状态待确认，我们正在核对，不会重复支付。',
                    user_id=user_id, event_id=f'pay-uncertain:{payout_id}', now_ms=now_ms)
                continue
            transfer_id = get_field(result, 'transfer_id')
            try:
                self.ledger.mark_payout_paid(payout_id, transfer_id, now_ms)
                paid += 1
            except Exception as exc:
                self.store.append_event('payout_mark_failed', fund_id=fund_id, level='error',
                                        details={'payout_id': payout_id,
                                                 'error': type(exc).__name__},
                                        created_ms=now_ms)
                # Leave the payout open: the identical key makes the retry safe.
        return paid

    # ---- manual unclaimed-receipt refunds -------------------------------
    async def drain_manual_refunds(self, now_ms: int) -> dict:
        """Drive the queued original-payer refunds of reviewed unclaimed receipts.

        Deliberately separate from :meth:`drain_payouts`: a manual refund is only
        sent when the fund's *free* cash can cover it, the sending intent is
        persisted before the external call, and an unsettled attempt is
        reconciled against the outgoing ledger before the same business key may
        be replayed (never a new key).
        """
        return await self._drain_guarded_payouts(now_ms, MANUAL_REFUND_KIND)

    async def drain_institution_fees(self, now_ms: int) -> dict:
        if self.live:
            self.ledger.queue_institution_fees(now_ms)
        return await self._drain_guarded_payouts(now_ms, INSTITUTION_FEE_KIND)

    async def _drain_guarded_payouts(self, now_ms: int, kind: str) -> dict:
        stats = {'seen': 0, 'paid': 0, 'reconciled': 0, 'held': 0, 'waiting': 0,
                 'skipped': 0, 'error': None}
        try:
            payouts = [p for _, p in self.store.list_items('payouts', limit=None)
                       if p.get('kind') == kind and p.get('status') not in MANUAL_REFUND_DONE]
        except Exception as exc:
            self.store.append_event('manual_refunds_failed', level='error',
                                    details={'error': type(exc).__name__}, created_ms=now_ms)
            stats['error'] = type(exc).__name__
            return stats
        stats['seen'] = len(payouts)
        if not payouts:
            return stats
        if not self.live:
            self.store.append_event('manual_refunds_held_not_live', level='info',
                                    details={'pending': len(payouts)}, created_ms=now_ms)
            return stats
        for payout in payouts:
            try:
                outcome = await self._drive_manual_refund(payout, now_ms)
            except Exception as exc:
                # One broken payout must not stop the others; it stays open.
                self.store.append_event('manual_refund_failed',
                                        fund_id=get_field(payout, 'fund_id'), level='error',
                                        details={'payout_id': get_field(payout, 'id'),
                                                 'error': type(exc).__name__},
                                        created_ms=now_ms)
                outcome = 'held'
            if outcome in stats:
                stats[outcome] += 1
        return stats

    @staticmethod
    async def _manual_balance(client) -> int:
        wallet = await client.balance()
        if isinstance(wallet, bool) or not isinstance(wallet, int) or wallet < 0:
            raise ValueError('invalid_balance')
        return wallet

    async def _drive_manual_refund(self, payout, now_ms: int) -> str:
        fund_id = get_field(payout, 'fund_id')
        payout_id = get_field(payout, 'id')
        client = self._client_for(fund_id)
        if client is None:
            return 'skipped'
        if get_field(payout, 'status') in MANUAL_REFUND_UNSETTLED:
            verdict = await self._reconcile_manual_refund(client, payout, now_ms)
            if verdict == 'paid':
                return 'reconciled'
            if verdict != 'retry_ok':
                # Either two rows carry this refund's exact fingerprint, or the
                # confirmed result could not be recorded: a human must look before
                # anything else is sent.
                return 'held'
            # Not confirmed: the payout stays unknown and the *same* key may be
            # replayed (the site de-duplicates it, so this can never pay twice).
        try:
            actual_cash = await self._manual_balance(client)
        except Exception:
            self.ledger.mark_payout_waiting(payout_id, 'balance_unavailable', now_ms)
            return 'waiting'
        cash = self.ledger.manual_refund_cash(payout_id, wallet_units=actual_cash)
        if not cash.get('ok'):
            self.ledger.mark_payout_waiting(payout_id, cash.get('reason') or 'cash_unavailable',
                                            now_ms)
            return 'waiting'
        # Durable intent before the external write, so a crash is recoverable.
        self.ledger.mark_payout_sending(payout_id, now_ms)
        return await self._send_manual_refund(client, payout, now_ms)

    async def _send_manual_refund(self, client, payout, now_ms: int) -> str:
        fund_id = get_field(payout, 'fund_id')
        payout_id = get_field(payout, 'id')
        user_id = get_field(payout, 'user_id')
        amount_units = _as_int(get_field(payout, 'amount_units')) or 0
        note = str(get_field(payout, 'note') or '')
        key = get_field(payout, 'idempotency_key') or str(payout_id)
        try:
            result = await client.transfer(user_id, amount_units, note, key)
        except Exception as exc:
            self._manual_refund_uncertain(payout, 'transfer_error', now_ms,
                                          error=type(exc).__name__)
            return 'held'
        transfer_id = get_field(result, 'transfer_id')
        paid_units = _as_int(get_field(result, 'amount_units'))
        if (not result or not isinstance(transfer_id, str) or not transfer_id
                or paid_units != amount_units):
            # An empty or mismatched result is not proof of payment: leave the
            # intent open instead of clearing the liability.
            self._manual_refund_uncertain(payout, 'unconfirmed_transfer_result', now_ms)
            return 'held'
        try:
            wallet = await self._manual_balance(client)
            self.ledger.mark_payout_paid(payout_id, transfer_id, now_ms, wallet_units=wallet)
        except Exception as exc:
            self.ledger.mark_payout_uncertain(payout_id, 'confirmation_unavailable', now_ms)
            self.store.append_event('manual_refund_mark_failed', fund_id=fund_id, level='error',
                                    details={'payout_id': payout_id,
                                             'error': type(exc).__name__}, created_ms=now_ms)
            return 'held'
        return 'paid'

    def _manual_refund_uncertain(self, payout, reason: str, now_ms: int,
                                 error: str | None = None) -> None:
        payout_id = get_field(payout, 'id')
        fund_id = get_field(payout, 'fund_id')
        user_id = get_field(payout, 'user_id')
        self.ledger.mark_payout_uncertain(payout_id, reason, now_ms)
        details = {'payout_id': payout_id, 'reason': reason}
        if error:
            details['error'] = error
        self.store.append_event('manual_refund_uncertain', fund_id=fund_id, level='warning',
                                details=details, created_ms=now_ms)
        self.outbox.enqueue(
            fund_id, ('机构手续费转账状态待确认，我们正在核对，不会重复支付。'
                      if get_field(payout, 'kind') == INSTITUTION_FEE_KIND else
                      '未认领款退款处理状态待确认，我们正在核对，不会重复支付。'),
            user_id=user_id, event_id=f'manual-refund-uncertain:{payout_id}', now_ms=now_ms)

    # ---- outgoing-ledger reconciliation ---------------------------------
    async def _reconcile_manual_refund(self, client, payout, now_ms: int) -> str:
        """Did the uncertain refund already leave the fund account?

        Only a *complete* bounded scan with exactly one outgoing row matching the
        recipient, the amount and the deterministic note confirms payment.  The
        payment polling cursor is never read or advanced here, and no result is
        ever treated as proof of *absence* beyond what a finished scan shows.
        """
        row_id = _as_int(get_field(payout, 'transaction_row_id'))
        if row_id and row_id > 0:
            scan = await self._scan_outgoing(client, row_id - 1)
            return await self._judge_refund_scan(client, payout, scan, now_ms)
        scan = await self._scan_outgoing(client, 0)
        return await self._judge_refund_scan(client, payout, scan, now_ms)

    async def _scan_outgoing(self, client, since_id: int) -> dict:
        cursor = max(0, int(since_id))
        rows: list = []
        complete = False
        error = None
        for _ in range(MAX_PAGES):
            try:
                page = await client.transactions(cursor)
            except Exception as exc:
                error = type(exc).__name__
                break
            for tx in list(get_field(page, 'transactions', []) or []):
                rows.append(tx)
            nxt = _as_int(get_field(page, 'next_cursor', cursor))
            if nxt is None or nxt < cursor:
                nxt = cursor
            cursor = nxt
            if not get_field(page, 'has_more', False):
                complete = True
                break
        return {'rows': rows, 'complete': complete, 'error': error}

    @staticmethod
    def _refund_matches(row, payout) -> bool:
        """The immutable fingerprint of one outgoing refund (never the note alone).

        Amount magnitude and note identify possible candidates. Confirmation then
        requires a complete scan and the exact recipient; a missing recipient is
        held as uncertainty rather than treated as proof of absence.
        """
        if get_field(row, 'type') != 'transfer':
            return False
        amount = _as_int(get_field(row, 'amount_units'))
        expected_units = _as_int(get_field(payout, 'amount_units')) or 0
        if amount is None or abs(amount) != expected_units:
            return False
        expected = ' '.join(str(get_field(payout, 'note') or '').split())
        if not expected or ' '.join(str(get_field(row, 'note') or '').split()) != expected:
            return False
        recipient = get_field(row, 'to_user_id')
        if recipient is not None and str(recipient) != str(get_field(payout, 'user_id')):
            return False
        return True

    async def _judge_refund_scan(self, client, payout, scan: dict, now_ms: int) -> str:
        """Confirm only a complete unique fingerprint; otherwise hold uncertainty."""
        payout_id = get_field(payout, 'id')
        if scan.get('error'):
            self.ledger.mark_payout_uncertain(payout_id, 'reconcile_scan_failed', now_ms)
            return 'error'
        if not scan.get('complete'):
            self.ledger.mark_payout_uncertain(payout_id, 'reconcile_incomplete', now_ms)
            return 'incomplete'
        candidates = [row for row in scan['rows'] if self._refund_matches(row, payout)]
        if len(candidates) > 1:
            self.ledger.mark_payout_uncertain(payout_id, 'reconcile_ambiguous', now_ms)
            self.store.append_event('manual_refund_reconcile_ambiguous',
                fund_id=get_field(payout, 'fund_id'), level='error',
                details={'payout_id': payout_id, 'matches': len(candidates)}, created_ms=now_ms)
            return 'ambiguous'
        if candidates:
            row = candidates[0]
            recipient = get_field(row, 'to_user_id')
            transfer_id = get_field(row, 'transfer_id')
            if (not isinstance(recipient, str) or not recipient
                    or recipient != get_field(payout, 'user_id')
                    or not isinstance(transfer_id, str) or not transfer_id):
                self.ledger.mark_payout_uncertain(payout_id, 'reconcile_unidentified', now_ms)
                return 'incomplete'
            try:
                wallet = await self._manual_balance(client)
                self.ledger.mark_payout_paid(payout_id, transfer_id, now_ms, wallet_units=wallet)
            except Exception as exc:
                self.ledger.mark_payout_uncertain(payout_id, 'confirmation_unavailable', now_ms)
                self.store.append_event('manual_refund_mark_failed',
                    fund_id=get_field(payout, 'fund_id'), level='error',
                    details={'payout_id': payout_id, 'error': type(exc).__name__}, created_ms=now_ms)
                return 'error'
            return 'paid'
        return 'retry_ok'
