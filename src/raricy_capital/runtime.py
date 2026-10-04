"""One service owner coordinates command, cash, trade and backup workers."""
from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
import time
from datetime import datetime, timedelta

from .client import FundSiteClient, FundSiteError
from .adapters import TradingClientAdapter
from .commands import CommandHandler
from .config import CredentialVault, FundConfig
from .contracts import BEIJING, FundError, POLICIES, money_units, now_ms
from .ledger import FundLedger, _period_cutoff_ms
from .operations import ServiceOperations
from .payments import PaymentsWorker
from .store import FundStore
from .trader import FundTrader
from .site_protocol import site_time_ms

#: Account-login retry schedule for a configured-but-unreachable fund.  The
#: target list lives in memory only and is never written to disk: a restart
#: rebuilds it from the vault and env, exactly like any other session state.
LOGIN_RETRY_BASE_MS = 10_000
LOGIN_RETRY_MAX_MS = 60_000
LOGIN_RETRY_AFTER_CAP_MS = 3_600_000

#: Login failures that must never be hammered automatically.  A wrong password,
#: a mismatched identity or a fund account clash is an operator problem; retrying
#: cannot fix it and could lock the account or cross identities.
PERMANENT_LOGIN_CODES = frozenset({
    'unauthorized', 'identity_mismatch', 'fund_accounts_must_differ', 'control_user_cannot_be_fund_account',
    'account_identity_changed', 'credentials_required', 'invalid_base_url',
})

_PERIOD_RE = re.compile(r'\d{4}-\d{2}\Z')


def _login_retry_delay_ms(attempts: int, retry_after: float | None) -> int:
    """Backoff for the ``attempts``-th failed login, honouring a 429 header.

    Exponential from 10s, capped at 60s.  A finite, non-negative ``Retry-After``
    raises the wait to whatever the server asked for (bounded to one hour) so a
    rate limit is respected instead of hammered.
    """
    try:
        count = max(1, int(attempts))
    except (TypeError, ValueError):
        count = 1
    delay = min(LOGIN_RETRY_MAX_MS, LOGIN_RETRY_BASE_MS * (2 ** (count - 1)))
    if (isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool)
            and math.isfinite(retry_after) and retry_after >= 0):
        delay = max(delay, min(LOGIN_RETRY_AFTER_CAP_MS, int(retry_after * 1000)))
    return delay


def _login_failure_is_permanent(exc: BaseException) -> bool:
    """Permanent failures must not be retried; transient ones should."""
    code = getattr(exc, 'code', None)
    if code in PERMANENT_LOGIN_CODES:
        return True
    if isinstance(exc, FundSiteError):
        if getattr(exc, 'retryable', False):
            return False
        status = getattr(exc, 'status', 0) or 0
        # A 4xx that is not explicitly retryable is a rejection, not a blip.
        return 400 <= status < 500
    return False


def _is_past_period(period: object, stamp: int) -> bool:
    """True for a well-formed ``YYYY-MM`` whose month end has already passed."""
    if not isinstance(period, str) or _PERIOD_RE.match(period) is None:
        return False
    try:
        cutoff = _period_cutoff_ms(period)
    except FundError:
        return False
    return cutoff <= stamp


# ── Unclaimed-receipt review: manual entry points, audit boundary, re-read ─────

#: The two approved manual outcomes for an unclaimed receipt; there is no third.
UNCLAIMED_ACTIONS = ('link', 'refund')
#: Bound for the independent receipt re-read (pages x 100 rows).  The review owns
#: this bounded scan and **never** touches the receipt-polling cursor: ``pay_cursors``
#: belongs to the payments worker alone.
RECEIPT_SCAN_MAX_PAGES = 20
_UNCLAIMED_REASON_MAX = 500
_UNCLAIMED_ACTOR_MAX = 128
_SUBSCRIPTION_RE = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')

# Stable refusal codes for the manual review.  ``receipt_*`` codes mean "hold for
# review": either the upstream row could not be confirmed or it disagrees with the
# stored record, and neither may be resolved on an assumption.
RECEIPT_UNIDENTIFIED = 'receipt_unidentified'
RECEIPT_MISMATCH = 'receipt_mismatch'
RECEIPT_MISSING = 'receipt_missing'
SCAN_INCOMPLETE = 'scan_incomplete'


def _unclaimed_action(value: object) -> str:
    if not isinstance(value, str) or value not in UNCLAIMED_ACTIONS:
        raise FundError('invalid_action')
    return value


def _unclaimed_reason(value: object) -> str:
    if not isinstance(value, str):
        raise FundError('invalid_reason')
    reason = value.strip()
    if not reason or len(reason) > _UNCLAIMED_REASON_MAX:
        raise FundError('invalid_reason')
    return reason


def _unclaimed_actor(value: object) -> str:
    """Audit actor: an opaque server-derived id, never a raw session or token."""
    if not isinstance(value, str):
        raise FundError('invalid_actor')
    actor = value.strip()
    if not actor or len(actor) > _UNCLAIMED_ACTOR_MAX:
        raise FundError('invalid_actor')
    return actor


def _unclaimed_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FundError('invalid_version')
    return value


def _unclaimed_subscription(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SUBSCRIPTION_RE.fullmatch(value.strip()) is None:
        raise FundError('invalid_subscription_id')
    return value.strip()


def _receipt_view(detail: object) -> dict:
    """Receipt fields of a detail, tolerating a nested ``record``/``receipt``."""
    if not isinstance(detail, dict):
        return {}
    fields = dict(detail)
    for key in ('record', 'receipt'):
        inner = detail.get(key)
        if isinstance(inner, dict):
            for name, value in inner.items():
                fields.setdefault(name, value)
    return fields


def _receipt_units(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _receipt_note(value: object) -> str:
    return value if isinstance(value, str) else ''


def _receipt_matches(row: dict, receipt: dict) -> bool:
    """The upstream row and the stored record must be the same immutable tuple.

    Transfer id, payer, integer amount, original note and (when both sides parsed
    one) the authoritative arrival time all have to agree; anything else is a hold.
    """
    if row.get('transfer_id') != receipt.get('transfer_id'):
        return False
    if _receipt_units(row.get('amount_units')) != _receipt_units(receipt.get('amount_units')):
        return False
    if row.get('from_user_id') != receipt.get('from_user_id'):
        return False
    if _receipt_note(row.get('note')) != _receipt_note(receipt.get('note')):
        return False
    arrived = _receipt_units(row.get('occurred_ms'))
    recorded = _receipt_units(receipt.get('occurred_ms'))
    if arrived is not None and recorded is not None and arrived != recorded:
        return False
    return True


def _transaction_rows(page: object) -> list[dict]:
    if not isinstance(page, dict):
        return []
    rows = page.get('transactions')
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _row_outcome(row: dict, receipt: dict, transfer_id: str) -> tuple[dict | None, str | None]:
    """Turn a matched upstream row into ``verified_transfer`` (site is authority)."""
    if row.get('type') != 'transfer_receive' or not _receipt_matches(row, receipt):
        return None, RECEIPT_MISMATCH
    verified = dict(row)
    verified['transfer_id'] = transfer_id
    if verified.get('transaction_row_id') is None:
        verified['transaction_row_id'] = _receipt_units(row.get('id'))
    return verified, None


def _mark_ineligible(preview: object, error: str) -> dict:
    """Fold a re-read failure into the preview: better ineligible than mislabelled."""
    if not isinstance(preview, dict):
        return {'eligible': False, 'errors': [error]}
    result = dict(preview)
    errors = [code for code in (result.get('errors') or []) if isinstance(code, str)]
    if error not in errors:
        errors.append(error)
    result['errors'] = errors
    result['eligible'] = False
    return result


class FundService:
    def __init__(self, config: FundConfig):
        self.config = config
        self.store = FundStore(config.data_dir / 'funds.sqlite3')
        if config.control_user_id and any(str(a.get('user_id')) == config.control_user_id
                for _, a in self.store.list_items('accounts')):
            self.store.close()
            raise FundError('control_user_cannot_be_fund_account')
        self.ledger = FundLedger(self.store, control_user_id=config.control_user_id)
        self.vault = CredentialVault(config.data_dir)
        self.clients: dict[str, FundSiteClient] = {}
        self.handlers: dict[str, CommandHandler] = {}
        self.operations = ServiceOperations(self.store, config.data_dir, config)
        self.payments = PaymentsWorker(self.store, self.ledger, self.clients, live=config.live)
        self.trader = FundTrader(self.store, self.ledger, self.clients, live=config.live)
        self.mutex = asyncio.Lock()
        self.started_ms = now_ms()
        self.last_tick_ms = 0
        self.last_tick_error: str | None = None
        self._task: asyncio.Task | None = None
        self._closed = False
        self._last_backup = 0.0
        # Configured accounts that failed their last login, keyed by fund id.
        # Memory only: credentials are what the vault/env already gave us, and a
        # restart re-reads them from there rather than any new on-disk format.
        self._login_retry: dict[str, dict] = {}

    def _fund(self, fund_id: str) -> None:
        if fund_id not in POLICIES:
            raise FundError('unknown_fund')

    async def login(self, fund_id: str, username: str, password: str) -> dict:
        self._fund(fund_id)
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            raise FundError('credentials_required')
        async with self.mutex:
            return await self._login(fund_id, username, password)

    async def _login(self, fund_id: str, username: str, password: str, *, persist: bool = True) -> dict:
        client = FundSiteClient(self.config.site_url, username, password, timeout=4.0)
        try:
            account = await client.login()
            stable_id = str(account['id'])
            if self.config.control_user_id and stable_id == self.config.control_user_id:
                raise FundError('control_user_cannot_be_fund_account')
            for other, record in self.store.list_items('accounts'):
                if other != fund_id and str(record['user_id']) == stable_id:
                    raise FundError('fund_accounts_must_differ')
            previous = self.store.get('accounts', fund_id)
            if previous and str(previous['user_id']) != stable_id:
                raise FundError('account_identity_changed')
            if persist:
                self.vault.save(fund_id, username, password)
            old = self.clients.get(fund_id)
            self.clients[fund_id] = client
            self.trader.clients[fund_id] = TradingClientAdapter(client, self.store, fund_id, live=self.config.live)
            self.handlers[fund_id] = CommandHandler(self.ledger, self.store, client, fund_id, live=self.config.live)
            self.store.put('accounts', fund_id, {'user_id': stable_id, 'logged_in_ms': now_ms(),
                                                'activation_ms': (previous or {}).get('activation_ms', now_ms())})
            if old:
                await old.close()
            self.store.append_event('account.logged_in', fund_id=fund_id)
            self._clear_login_retry(fund_id, now_ms())
            return {'fund_id': fund_id, 'user_id': stable_id, 'logged_in': True}
        except BaseException:
            if self.clients.get(fund_id) is not client:
                await client.close()
            raise

    async def start(self) -> None:
        previous = self.store.get('service', 'heartbeat', {})
        if previous.get('active') and self.started_ms - previous.get('last_tick_ms', self.started_ms) > 15000:
            gap = (self.started_ms - previous['last_tick_ms']) // 1000
            self.operations.record_event('service.restarted_after_gap', level='warning', details={'gap_seconds': gap})
            for fund_id in POLICIES:
                self.payments.enqueue_notice(fund_id, f'基金服务已重启，上一轮监控距今 {gap} 秒；正在重新核对账户和持仓。',
                                             event_id=f'restart:{fund_id}:{self.started_ms}', now_ms=self.started_ms)
        credentials = self.vault.read()
        for fund_id in POLICIES:
            username = os.environ.get(f'FUNDS_{fund_id.upper()}_USERNAME', '')
            password = os.environ.get(f'FUNDS_{fund_id.upper()}_PASSWORD', '')
            if username and password and not username.startswith('changeme'):
                credentials[fund_id] = {'username': username, 'password': password}
        for fund_id, value in credentials.items():
            target = {
                'username': value['username'],
                'password': value['password'],
                'attempts': 0,
                'next_ms': self.started_ms,
                'permanent': False,
                'code': None,
                'first_ms': self.started_ms,
                'notified': False,
            }
            self._login_retry[fund_id] = target
            await self._attempt_restore(fund_id, target, self.started_ms)
        self._task = asyncio.create_task(self._loop(), name='capital-funds-service')
        self.operations.record_event('service.started', details={'live': self.config.live})

    async def _attempt_restore(self, fund_id: str, target: dict, stamp: int) -> None:
        """Try one configured account; a success clears the retry target."""
        try:
            await self._login(fund_id, target['username'], target['password'], persist=False)
        except Exception as exc:  # noqa: BLE001 - classified into a stable code
            self._record_login_failure(fund_id, target, exc, stamp)

    def _record_login_failure(self, fund_id: str, target: dict, exc: BaseException,
                              stamp: int) -> None:
        code = getattr(exc, 'code', None) or type(exc).__name__
        permanent = _login_failure_is_permanent(exc)
        target['attempts'] = int(target.get('attempts', 0)) + 1
        target['code'] = code
        target['permanent'] = permanent
        target['failed_ms'] = stamp
        if permanent:
            target['next_ms'] = None  # never hammer a wrong password or wrong identity
        else:
            target['next_ms'] = stamp + _login_retry_delay_ms(
                target['attempts'], getattr(exc, 'retry_after', None))
        self._login_retry[fund_id] = target
        # One durable notice per failure episode; every actual attempt still gets
        # its own rate-limited event so the log tracks attempts, not ticks.
        if not target.get('notified'):
            target['notified'] = True
            self.payments.enqueue_notice(
                fund_id,
                f'{fund_id} 账号连接失败（{"需要人工重新登录" if permanent else "将自动重试"}）。',
                event_id=f'login-failed:{fund_id}:{target["first_ms"]}',
                kind='account_login', now_ms=stamp)
        self.operations.record_event(
            'account.login_failed', fund_id=fund_id, level='warning',
            details={'code': code, 'permanent': permanent, 'attempts': target['attempts']},
            event_key=f'account.login_failed:{fund_id}:{target["first_ms"]}:{target["attempts"]}',
            created_ms=stamp)

    def _clear_login_retry(self, fund_id: str, stamp: int) -> None:
        record = self._login_retry.pop(fund_id, None)
        if record is None or int(record.get('attempts', 0)) <= 0:
            return  # a first-time login has no failure to announce recovery from
        self.payments.enqueue_notice(
            fund_id, '账号连接已恢复，服务将继续核对账户与持仓。',
            event_id=f'login-recovered:{fund_id}:{record.get("first_ms")}',
            kind='account_login', now_ms=stamp)

    async def _retry_pending_logins(self, stamp: int) -> None:
        """Retry configured accounts whose backoff has elapsed.

        Runs under the service mutex; it never sleeps here, it only compares each
        target's own next-deadline against ``stamp`` so a slow retry cannot delay
        the loop and one fund's failure cannot crowd out the other's schedule.
        """
        for fund_id in list(self._login_retry):
            target = self._login_retry[fund_id]
            if fund_id in self.clients:
                self._login_retry.pop(fund_id, None)
                continue
            if target.get('permanent'):
                continue
            next_ms = target.get('next_ms')
            if next_ms is None or stamp < int(next_ms):
                continue
            await self._attempt_restore(fund_id, target, stamp)

    def _reflect_login_health(self, stamp: int) -> None:
        """Keep health degraded while a configured account cannot log in.

        No target and no client means nothing was configured: health is left
        untouched rather than reported healthy off a probe that never ran.
        """
        if not self._login_retry:
            self.last_tick_error = None
            return
        record = next(iter(self._login_retry.values()))
        code = record.get('code') or 'account_login_failed'
        self.last_tick_error = code
        self.operations.health_tick(stamp, False, error=code)

    def _settlement_view(self, fund_id: str) -> dict:
        hold = self.store.get('valuation_holds', fund_id, {}) or {}
        pending = []
        for _, record in self.store.list_items('periods', f'{fund_id}:'):
            if record.get('status') == 'settled':
                continue
            pending.append({'period': record.get('period'), 'status': record.get('status'),
                            'reason': record.get('reason')})
        pending.sort(key=lambda row: row.get('period') or '')
        return {'hold': bool(hold.get('hold')), 'period': hold.get('period'),
                'reason': hold.get('reason'), 'pending': pending}

    async def _private_messages(self, stamp: int) -> None:
        for fund_id, client in tuple(self.clients.items()):
            channels = await client.private_channels()
            for channel_id in channels:
                key = f'{fund_id}:{channel_id}'
                cursor = self.store.get('command_cursors', key)
                messages = await client.fetch_messages(channel_id, after=cursor)
                for message in sorted(messages, key=lambda m: m.id):
                    if str(message.author.id) == str(client.user_id):
                        continue
                    # First contact must not execute commands from pre-service history.
                    if cursor is None and site_time_ms(message.created_at) < self.started_ms:
                        continue
                    await self.handlers[fund_id].handle(message, stamp)
                if messages:
                    self.store.put('command_cursors', key, max(m.id for m in messages))

    def _settlement_periods(self, fund_id: str, prev_month: str, stamp: int) -> list[str]:
        """Every month this fund still owes a settlement for, oldest first.

        Sources: explicit unfinished period records, still-pending ordinary
        redemptions, received-but-unissued subscriptions that are actually past
        their month end, and the immediately previous month.  Malformed or future
        ``YYYY-MM`` strings are ignored rather than allowed to jam the queue.
        """
        periods: set[str] = set()
        for _, record in self.store.list_items('periods', f'{fund_id}:'):
            period = record.get('period')
            if record.get('status') != 'settled' and _is_past_period(period, stamp):
                periods.add(period)
        for order in self.ledger.orders(fund_id):
            period = order.get('period')
            if order.get('order_type') == 'redemption':
                if order.get('kind') == 'ordinary' and order.get('status') == 'pending' \
                        and _is_past_period(period, stamp):
                    periods.add(period)
            elif order.get('order_type') == 'subscription':
                if order.get('status') == 'received' and _is_past_period(period, stamp):
                    periods.add(period)
        if _is_past_period(prev_month, stamp):
            periods.add(prev_month)
        return sorted(periods)

    def _settle_months(self, fund_id: str, prev_month: str, stamp: int) -> None:
        """Settle a fund's outstanding months in chronological order.

        An older month stuck in ``pending_valuation`` blocks newer months: the
        earlier batch must be resolved (from its own frozen cutoff book) before a
        later one is attempted, so a newer settlement can never paper over an
        older unresolved valuation.
        """
        blocked = False
        for period in self._settlement_periods(fund_id, prev_month, stamp):
            key = f'{fund_id}:month:{period}'
            existing = self.store.get('runtime_settlements', key)
            if isinstance(existing, dict) and (existing.get('result') or {}).get('status') == 'settled':
                continue  # only a true settled batch is deduplicated
            try:
                result = self.ledger.settle_month(fund_id, period, stamp)
            except FundError as exc:
                self.store.append_event(
                    'settlement.deferred', fund_id=fund_id, level='warning',
                    details={'period': period, 'reason': exc.code},
                    event_key=f'settlement:{fund_id}:month:{period}:{exc.code}')
                blocked = True
                break
            if result.get('status') == 'settled':
                self.store.put('runtime_settlements', key, {'result': result, 'completed_ms': stamp})
                continue
            self.store.put('valuation_holds', fund_id,
                           {'hold': True, 'period': period, 'reason': result.get('reason')})
            blocked = True
            break
        if not blocked:
            hold = self.store.get('valuation_holds', fund_id, {}) or {}
            if hold.get('hold'):
                # No unresolved month remains; a later settled month may now clear
                # the hold, but never an earlier one still waiting.
                self.store.put('valuation_holds', fund_id, {'hold': False})

    async def _settlements(self, stamp: int) -> None:
        if not self.config.live:
            return
        local = datetime.fromtimestamp(stamp / 1000, BEIJING)
        prev_month = (local.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
        for fund_id in tuple(self.clients):
            trade = self.trader.public_status()['funds'][fund_id]
            if trade['blocked'] or trade['pending'] or trade['mark_failed']:
                continue
            self._settle_months(fund_id, prev_month, stamp)
            # An emergency batch prices off its own frozen 20:00 book, so an
            # unresolved ordinary month must not block an otherwise eligible exit.
            if local.hour >= 20:
                key = f'{fund_id}:emergency:{local.strftime("%Y-%m-%d")}'
                try:
                    self.ledger.settle_emergency(fund_id, stamp)
                except FundError as exc:
                    event_key = f'settlement:{key}:{exc.code}'
                    self.store.append_event('settlement.deferred', fund_id=fund_id, level='warning',
                                            details={'reason': exc.code}, event_key=event_key)

    async def _prepare_liquidity(self, stamp: int) -> None:
        if not self.config.live:
            return
        local = datetime.fromtimestamp(stamp / 1000, BEIJING)
        today20 = int(local.replace(hour=20, minute=0, second=0, microsecond=0).timestamp() * 1000)
        next_month = (local.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_end = int(next_month.timestamp() * 1000)
        for fund_id in self.clients:
            state = self.ledger.status(fund_id)
            required = 0
            for order in self.ledger.orders(fund_id):
                if order.get('status') != 'pending' or order.get('kind') not in ('ordinary', 'emergency'):
                    continue
                due = (order.get('ready_ms', today20) if order['kind'] == 'emergency' else month_end)
                if order['kind'] == 'ordinary' and order.get('period') != local.strftime('%Y-%m'):
                    continue
                if stamp >= due - 120000:
                    required += int(order.get('amount_units', 0))
            if stamp >= month_end - 120000:
                required = min(required, max(0, state['equity_units']) // 5) + max(0, state['realized_profit_units']) // 10
            self.store.put('settlement_holds', fund_id, {'hold': required > 0, 'required_units': required})
            if required > 0:
                await self.trader.close_for_liquidity(fund_id, required, stamp)

    async def tick(self) -> None:
        async with self.mutex:
            stamp = now_ms()
            try:
                await self._retry_pending_logins(stamp)
            except Exception as exc:  # a retry bookkeeping failure must not kill the loop
                self.last_tick_error = getattr(exc, 'code', type(exc).__name__)
                self.operations.record_event('account.retry_failed', error=exc)
            if not self.clients:
                self.last_tick_ms = stamp
                self.store.put('service', 'heartbeat', {'active': True, 'last_tick_ms': stamp})
                # A degraded configured account must not be forgotten just because
                # there is no client to run workers for yet.
                self._reflect_login_health(stamp)
                return
            try:
                private_error = None
                try:
                    await self._private_messages(stamp)
                except Exception as exc:
                    private_error = type(exc).__name__
                    self.operations.record_event('commands.poll_failed', error=exc)
                # Receipt records and reserved cash are visible before any trading decision.
                payments = await self.payments.tick(stamp)
                for fund_id in self.clients:
                    self.store.put('reconciliation_holds', fund_id, {'hold': bool(payments.get('errors')), 'updated_ms': stamp})
                try:
                    await self._prepare_liquidity(stamp)
                except Exception as exc:
                    self.operations.record_event('liquidity.prepare_failed', error=exc)
                trades = await self.trader.tick(stamp)
                if not payments.get('errors'):
                    await self._settlements(stamp)
                if private_error or payments.get('errors') or any(v.get('mark_failed') for k,v in trades['funds'].items() if k in self.clients):
                    raise FundError('worker_failed')
                for fund_id in self.clients:
                    value = self.ledger.status(fund_id)
                    if value['shares_atoms'] and value['quote_ms']:
                        self.store.put('nav_history', f'{fund_id}:{value["updated_ms"] // 3600000:012d}',
                                       {'updated_ms': value['updated_ms'], 'nav': value['nav'], 'equity_units': value['equity_units']})
                if self._login_retry:
                    # Some client is up but a configured fund is still not: health
                    # stays degraded while the other fund keeps working normally.
                    self._reflect_login_health(stamp)
                else:
                    self.last_tick_error = None
                    self.operations.health_tick(stamp, True)
            except Exception as exc:
                self.last_tick_error = getattr(exc, 'code', type(exc).__name__)
                self.operations.health_tick(stamp, False, error=exc)
                self.operations.record_event('service.tick_failed', error=exc)
            finally:
                self.last_tick_ms = now_ms()
                self.store.put('service', 'heartbeat', {'active': True, 'last_tick_ms': self.last_tick_ms})

    async def _loop(self) -> None:
        while not self._closed:
            begin = time.monotonic()
            await self.tick()
            if begin - self._last_backup >= self.config.backup_interval_seconds:
                try:
                    await self.backup()
                    self._last_backup = begin
                except Exception as exc:
                    self.operations.record_event('backup.failed', error=exc)
            await asyncio.sleep(max(.1, self.config.tick_seconds - (time.monotonic() - begin)))

    async def seed(self, fund_id: str, user_id: str, principal_units: int) -> dict:
        self._fund(fund_id)
        async with self.mutex:
            if fund_id not in self.clients:
                raise FundError('account_login_required')
            await self.trader.tick(now_ms())
            trade = self.trader.public_status()['funds'][fund_id]
            if trade['blocked'] or trade['pending'] or trade['mark_failed']:
                raise FundError('account_reconciliation_required')
            if isinstance(principal_units, bool) or not isinstance(principal_units, int) or principal_units <= 0:
                raise FundError('invalid_amount')
            if self.ledger.status(fund_id)['shares_atoms']:
                raise FundError('seed_already_initialized')
            initial = self.ledger.status(fund_id)
            if principal_units != initial['available_cash_units'] or initial['position_value_units']:
                raise FundError('initial_seed_must_allocate_available_balance')
            return self.ledger.seed(fund_id, user_id, principal_units, now_ms())

    async def settle(self, fund_id: str, kind: str, period: str | None = None) -> dict:
        self._fund(fund_id)
        async with self.mutex:
            if not self.config.live:
                raise FundError('live_required')
            trade = self.trader.public_status()['funds'][fund_id]
            if trade['blocked'] or trade['pending'] or trade['mark_failed']:
                raise FundError('account_reconciliation_required')
            stamp = now_ms()
            if kind not in ('month', 'emergency'):
                raise FundError('invalid_kind')
            return (self.ledger.settle_emergency(fund_id, stamp) if kind == 'emergency' else
                    self.ledger.settle_month(fund_id, period or '', stamp))

    async def set_dividend_choice(self, fund_id: str, user_id: str, fraction: object) -> dict:
        async with self.mutex:
            return self.ledger.set_dividend_choice(fund_id, user_id, fraction, now_ms=now_ms())

    async def set_running(self, fund_id: str, enabled: bool) -> dict:
        self._fund(fund_id)
        async with self.mutex:
            state = self.ledger.status(fund_id)
            if enabled and (not self.config.live or fund_id not in self.clients):
                raise FundError('live_account_required')
            if enabled and (state['state'] == 'permanent_halt' or self.trader.public_status()['funds'][fund_id]['phase'] == 'halted'):
                raise FundError('permanent_halt')
            # The trader owns the risk state; it must never be replaced to restart.
            self.store.put('fund_controls', fund_id, {'running': bool(enabled), 'updated_ms': now_ms()})
            await self.trader.set_running(fund_id, bool(enabled), now_ms())
            self.store.append_event('fund.running_changed', fund_id=fund_id, details={'running': bool(enabled)})
            return {'fund_id': fund_id, 'running': bool(enabled)}

    async def stop_fund(self, fund_id: str) -> dict:
        self._fund(fund_id)
        async with self.mutex:
            self.store.put('fund_controls', fund_id, {'running': False, 'updated_ms': now_ms()})
            await self.trader.stop(fund_id, 'operator_stop', now_ms())
            return {'fund_id': fund_id, 'running': False}

    async def backup(self):
        result = await asyncio.to_thread(self.operations.backup)
        if inspect.isawaitable(result):
            result = await result
        return result

    def public_status(self) -> dict:
        stamp = now_ms()
        trade = self.trader.public_status()['funds']
        funds = []
        for fund_id in POLICIES:
            item = self.ledger.status(fund_id)
            retry = self._login_retry.get(fund_id)
            if fund_id in self.clients:
                account_state = 'logged_in'
            elif retry and retry.get('permanent'):
                account_state = 'relogin_required'
            elif retry:
                account_state = 'retrying'
            else:
                account_state = 'no_credentials'
            next_ms = (retry or {}).get('next_ms')
            item.update({'logged_in': fund_id in self.clients,
                         'running': self.store.get('fund_controls', fund_id, {}).get('running', False),
                         'account': {'state': account_state,
                                     'error': (retry or {}).get('code'),
                                     'attempts': int((retry or {}).get('attempts', 0)),
                                     'retry_in_ms': (max(0, int(next_ms) - stamp)
                                                     if next_ms is not None else None)},
                         'settlement': self._settlement_view(fund_id),
                         'policy': POLICIES[fund_id].public(), 'trader': trade.get(fund_id, {})})
            funds.append(item)
        health = self.operations.status()
        health.update({'state': health['network'], 'ok': health['healthy']})
        if self._login_retry:
            permanent = any(record.get('permanent') for record in self._login_retry.values())
            detail = '账号连接失败，需要重新登录' if permanent else '账号连接失败，等待重试'
        elif not self.clients:
            detail = '等待基金账号登录'
        elif self.last_tick_ms and stamp - self.last_tick_ms < 15000:
            detail = '持续监控'
        else:
            detail = '循环未更新'
        return {'funds': funds, 'events': self.store.events(50), 'live': self.config.live,
                'status': {'detail': detail, 'last_health_ms': self.last_tick_ms},
                'last_tick_ms': self.last_tick_ms, 'last_error': self.last_tick_error,
                'uptime_seconds': (stamp - self.started_ms) // 1000,
                'network': health,
                'config': self.config.public()}

    def holders(self, fund_id: str) -> list:
        self._fund(fund_id)
        return self.store.list('holders', f'{fund_id}:')

    def nav_history(self, fund_id: str, limit: int = 365) -> list:
        self._fund(fund_id)
        return self.store.list('nav_history', f'{fund_id}:')[-min(max(int(limit), 1), 10000):]

    # ── unclaimed-receipt manual review ──────────────────────────────────────

    def unclaimed(self, fund_id: str | None = None, status: str | None = None) -> list:
        """Per-receipt unclaimed list (filterable); a pure read, no site, no write."""
        if fund_id is not None:
            self._fund(fund_id)
        return self.ledger.unclaimed(fund_id, status)

    def unclaimed_detail(self, fund_id: str, unclaimed_id: str) -> dict:
        """One receipt's detail (fields, version, candidates, history); pure read."""
        self._fund(fund_id)
        return self.ledger.unclaimed_detail(fund_id, unclaimed_id, now_ms())

    async def preview_unclaimed(self, fund_id: str, unclaimed_id: str, action: str,
                                subscription_id: str | None = None) -> dict:
        """Preview a manual outcome: re-read the receipt outside any DB transaction.

        Read-only and permitted while ``live`` is off (the console still shows the
        verification result).  A failed or disagreeing re-read reports
        ``eligible=False`` with a stable code, never an optimistic preview.
        """
        self._fund(fund_id)
        action = _unclaimed_action(action)
        subscription_id = _unclaimed_subscription(subscription_id)
        client = self._client_for(fund_id)
        detail = self.ledger.unclaimed_detail(fund_id, unclaimed_id, now_ms())
        verified, error = await self._authoritative_receipt(client, detail)
        preview = self.ledger.preview_unclaimed(
            fund_id, unclaimed_id, action, subscription_id=subscription_id,
            verified_transfer=verified, now_ms=now_ms())
        if error:
            preview = _mark_ineligible(preview, error)
        return preview

    async def resolve_unclaimed(self, fund_id: str, unclaimed_id: str, action: str, *,
                                version: int, reason: str, actor: str,
                                subscription_id: str | None = None) -> dict:
        """Commit a manual outcome: live gate, fresh re-read, then atomic ledger CAS.

        Every authority field except ``action`` is server-side: ``actor`` is the
        opaque id the web layer derived from the authenticated credential, and this
        method only bounds and passes it through.  ``live=False`` is refused before
        any site read, state change or payout intent.
        """
        self._fund(fund_id)
        action = _unclaimed_action(action)
        reason = _unclaimed_reason(reason)
        actor = _unclaimed_actor(actor)
        subscription_id = _unclaimed_subscription(subscription_id)
        version = _unclaimed_version(version)
        if not self.config.live:
            raise FundError('live_required')
        client = self._client_for(fund_id)
        detail = self.ledger.unclaimed_detail(fund_id, unclaimed_id, now_ms())
        verified, error = await self._authoritative_receipt(client, detail)
        if verified is None:
            raise FundError(error or 'receipt_unverified')
        async with self.mutex:
            if not self.config.live:
                raise FundError('live_required')
            if self.clients.get(fund_id) is not client:
                raise FundError('account_changed')
            return self.ledger.resolve_unclaimed(
                fund_id, unclaimed_id, action, version=version, reason=reason, actor=actor,
                verified_transfer=verified, now_ms=now_ms(), subscription_id=subscription_id)

    def _client_for(self, fund_id: str) -> FundSiteClient:
        client = self.clients.get(fund_id)
        if client is None:
            raise FundError('account_login_required')
        return client

    async def _authoritative_receipt(self, client: FundSiteClient,
                                     detail: object) -> tuple[dict | None, str | None]:
        """Independently re-read one receipt: ``(upstream row, stable error code)``.

        Only normalized ``transfer_receive`` rows count, and the row has to form the
        exact same immutable tuple as the stored record (id, payer, integer amount,
        note, arrival time).  Any conflict or upstream failure is returned as a
        stable code so the caller holds the row instead of assuming.
        """
        receipt = _receipt_view(detail)
        transfer_id = receipt.get('transfer_id')
        if not isinstance(transfer_id, str) or not transfer_id:
            return None, RECEIPT_UNIDENTIFIED
        try:
            return await self._lookup_receipt(client, receipt, transfer_id)
        except FundSiteError as exc:
            return None, exc.code
        except FundError as exc:
            return None, exc.code

    async def _lookup_receipt(self, client: FundSiteClient, receipt: dict,
                              transfer_id: str) -> tuple[dict | None, str | None]:
        row_id = _receipt_units(receipt.get('transaction_row_id'))
        if row_id is not None and row_id > 0:
            # Fast path: row ids are immutable, so one page after ``row_id - 1``
            # settles it without walking the whole history.
            page = await client.transactions(row_id - 1)
            for row in _transaction_rows(page):
                if row.get('id') != row_id:
                    continue
                return _row_outcome(row, receipt, transfer_id)
        return await self._scan_receipt(client, receipt, transfer_id)

    async def _scan_receipt(self, client: FundSiteClient, receipt: dict,
                            transfer_id: str) -> tuple[dict | None, str | None]:
        """Bounded legacy scan from 0, for rows stored before row ids existed."""
        cursor = 0
        for _ in range(RECEIPT_SCAN_MAX_PAGES):
            page = await client.transactions(cursor)
            for row in _transaction_rows(page):
                if row.get('type') != 'transfer_receive':
                    continue
                if row.get('transfer_id') != transfer_id:
                    continue
                return _row_outcome(row, receipt, transfer_id)
            if not isinstance(page, dict):
                return None, SCAN_INCOMPLETE
            next_cursor = page.get('next_cursor', cursor)
            if not page.get('has_more'):
                # Scanned to the end without finding it: the receipt is genuinely
                # absent upstream, so the row stays held for review.
                return None, RECEIPT_MISSING
            if isinstance(next_cursor, bool) or not isinstance(next_cursor, int) \
                    or next_cursor <= cursor:
                return None, SCAN_INCOMPLETE
            cursor = next_cursor
        # An unfinished scan never proves absence: hold rather than conclude.
        return None, SCAN_INCOMPLETE

    async def close(self) -> None:
        self._closed = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for client in tuple(self.clients.values()):
            await client.close()
        self.clients.clear()
        self.store.put('service', 'heartbeat', {'active': False, 'last_tick_ms': self.last_tick_ms, 'stopped_ms': now_ms()})
        self.operations.record_event('service.stopped')
        self.store.close()
