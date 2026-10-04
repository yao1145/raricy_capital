"""One service owner coordinates command, cash, trade and backup workers."""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from datetime import datetime, timedelta

from .client import FundSiteClient
from .adapters import TradingClientAdapter
from .commands import CommandHandler
from .config import CredentialVault, FundConfig
from .contracts import BEIJING, FundError, POLICIES, money_units, now_ms
from .ledger import FundLedger
from .operations import ServiceOperations
from .payments import PaymentsWorker
from .store import FundStore
from .trader import FundTrader
from .site_protocol import site_time_ms


class FundService:
    def __init__(self, config: FundConfig):
        self.config = config
        self.store = FundStore(config.data_dir / 'funds.sqlite3')
        self.ledger = FundLedger(self.store)
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
            try:
                await self._login(fund_id, value['username'], value['password'], persist=False)
            except Exception as exc:
                self.operations.record_event('account.restore_failed', fund_id=fund_id, error=exc)
        self._task = asyncio.create_task(self._loop(), name='capital-funds-service')
        self.operations.record_event('service.started', details={'live': self.config.live})

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

    async def _settlements(self, stamp: int) -> None:
        if not self.config.live:
            return
        local = datetime.fromtimestamp(stamp / 1000, BEIJING)
        last_month = (local.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
        for fund_id in tuple(self.clients):
            trade = self.trader.public_status()['funds'][fund_id]
            if trade['blocked'] or trade['pending'] or trade['mark_failed']:
                continue
            for kind, period in (('month', last_month), ('emergency', local.strftime('%Y-%m-%d'))):
                if kind == 'emergency' and local.hour < 20:
                    continue
                key = f'{fund_id}:{kind}:{period}'
                if kind == 'month' and self.store.get('runtime_settlements', key):
                    continue
                try:
                    result = (self.ledger.settle_month(fund_id, period, stamp) if kind == 'month'
                              else self.ledger.settle_emergency(fund_id, stamp))
                    if kind == 'month' and result.get('status') == 'settled':
                        self.store.put('runtime_settlements', key, {'result': result, 'completed_ms': stamp})
                        self.store.put('valuation_holds', fund_id, {'hold': False})
                    elif kind == 'month':
                        self.store.put('valuation_holds', fund_id, {'hold': True, 'period': period, 'reason': result.get('reason')})
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
            if not self.clients:
                self.last_tick_ms = stamp
                self.store.put('service', 'heartbeat', {'active': True, 'last_tick_ms': stamp})
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
                self.operations.health_tick(stamp, True)
                self.last_tick_error = None
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
            item.update({'logged_in': fund_id in self.clients,
                         'running': self.store.get('fund_controls', fund_id, {}).get('running', False),
                         'policy': POLICIES[fund_id].public(), 'trader': trade.get(fund_id, {})})
            funds.append(item)
        health = self.operations.status()
        health.update({'state': health['network'], 'ok': health['healthy']})
        detail = ('等待基金账号登录' if not self.clients else
                  '持续监控' if self.last_tick_ms and stamp - self.last_tick_ms < 15000 else '循环未更新')
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
