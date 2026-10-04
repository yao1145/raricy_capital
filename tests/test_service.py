"""Central assembly boundaries between normalized site units and fund workers."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import pytest
from raricy_capital import runtime
from raricy_capital.adapters import TradingClientAdapter
from raricy_capital.client import FundSiteError
from raricy_capital.config import FundConfig, CredentialVault
from raricy_capital.contracts import BEIJING, FundError, now_ms as real_now_ms
from raricy_capital.runtime import FundService
from raricy_capital.store import FundStore

U = 10_000


def _ms(year, month, day, hour=0, minute=0, second=0):
    return int(datetime(year, month, day, hour, minute, second, tzinfo=BEIJING).timestamp() * 1000)

class NormalizedSite:
    def __init__(self): self.calls=[]
    async def snapshot(self):
        return {'balance_units':123450000,'fee_rate':.0002,'min_stake_units':10000,
                'leverage_options':[1,2,3,5],'leverage_enabled':True,'positions':[
                    {'position_id':'owned','symbol':'BTCUSDT','stake_units':23450000,
                     'entry_price':100000,'leverage':3,'opened_ms':1700000000000}]}
    async def quote(self): return (100001,.0002)
    async def buy(self,amount,leverage,key):
        self.calls.append((amount,leverage,key))
        return {'position_id':'new','symbol':'BTCUSDT','stake_units':amount,
                'entry_price':100000,'leverage':leverage,'opened_ms':1700000000000,
                'balance_units':5000000,'replayed':False}
    async def sell(self,pid):
        return {'position_id':pid,'payout_units':23456789,'profit_units':6789,
                'exit_price':100010,'liquidated':False,'replayed':False,'balance_units':23456789}

async def test_adapter_preserves_units_and_write_gate(tmp_path):
    store=FundStore(tmp_path/'adapter.sqlite3')
    try:
        site=NormalizedSite()
        adapter=TradingClientAdapter(site,store,'capital1',live=True)
        snapshot=await adapter.snapshot()
        assert snapshot['positions'][0]['id']=='owned'
        assert snapshot['minStake']=='1.0000'
        with pytest.raises(FundError): await adapter.buy('2345.0000',3,'stable-key')
        assert not site.calls
        store.put('fund_controls','capital1',{'running':True})
        buy=await adapter.buy('2345.0000',3,'stable-key')
        assert site.calls==[(23450000,3,'stable-key')]
        assert buy['position']['stake']=='2345.0000'
        assert buy['position']['entry_price']==100000
        assert (await adapter.sell('new'))['payout']=='2345.6789'
        store.put('settlement_holds','capital1',{'hold':True})
        with pytest.raises(FundError): await adapter.buy('1.0000',3,'other-key')
    finally: store.close()

def test_vault_portable_without_plaintext_or_token_in_public_config(tmp_path):
    cfg=FundConfig(tmp_path)
    vault=CredentialVault(tmp_path)
    vault.save('capital1','account','sensitive-test-value')
    assert b'sensitive-test-value' not in (tmp_path/'credentials.enc').read_bytes()
    assert CredentialVault(tmp_path).read()['capital1']['password']=='sensitive-test-value'
    assert cfg.control_token not in str(cfg.public())
    with pytest.raises(FundError): FundConfig(tmp_path/'bad',host='0.0.0.0')

async def test_service_starts_without_accounts_and_keeps_backup(tmp_path):
    service=FundService(FundConfig(tmp_path,port=8199))
    try:
        await service.start()
        await asyncio.sleep(.05)
        status=service.public_status()
        assert len(status['funds'])==2
        assert status['status']['detail']=='等待基金账号登录'
        assert status['last_tick_ms']>0
        assert all(not f['running'] for f in status['funds'])
        backup=await service.backup()
        assert backup.is_file()
    finally: await service.close()

async def test_dividend_choice_uses_request_time_after_deadline(tmp_path, monkeypatch):
    service=FundService(FundConfig(tmp_path))
    before=int(datetime(2026,1,20,tzinfo=BEIJING).timestamp()*1000)
    after=int(datetime(2026,1,26,tzinfo=BEIJING).timestamp()*1000)
    try:
        service.ledger.mark_account('capital1',10000000,0,before,before)
        monkeypatch.setattr('raricy_capital.runtime.now_ms',lambda:after)
        result=await service.set_dividend_choice('capital1','investor',1)
        assert result['effective_period']=='2026-02'
    finally: await service.close()


# ------------------------------------------------------- account login retry
class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeSite:
    """Deterministic, no-network stand-in for :class:`FundSiteClient`."""

    offline: set[str] = set()
    permanent: set[str] = set()
    rate_limited: dict[str, float] = {}
    permanent_code = 'unauthorized'
    instances: list['FakeSite'] = []

    def __init__(self, base_url, username, password, *, timeout=20.0):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.user_id = None
        self.quote_ms = 0
        self.closed = False
        FakeSite.instances.append(self)

    async def close(self):
        self.closed = True

    async def login(self):
        if self.username in FakeSite.permanent:
            raise FundSiteError(FakeSite.permanent_code, status=401)
        if self.username in FakeSite.rate_limited:
            raise FundSiteError('rate_limited', status=429, retryable=True,
                                retry_after=FakeSite.rate_limited[self.username])
        if self.username in FakeSite.offline:
            raise FundSiteError('timeout', retryable=True)
        self.user_id = f'id-{self.username}'
        return {'id': self.user_id, 'username': self.username}

    async def private_channels(self):
        return []

    async def fetch_messages(self, channel_id, after=None):
        return []

    async def transactions(self, since_id):
        return {'transactions': [], 'next_cursor': since_id, 'has_more': False}

    async def snapshot(self):
        return {'balance_units': 0, 'fee_rate': 0.0002, 'min_stake_units': 10000,
                'leverage_options': [1, 2, 3, 5], 'leverage_enabled': True, 'positions': []}

    async def quote(self):
        return (100000.0, 0.0002)

    async def balance(self):
        return 0

    async def candles(self):
        return []

    async def transfer(self, *args, **kwargs):
        return {'transfer_id': 'tx', 'amount_units': 1, 'duplicated': False}

    async def send_message(self, *args, **kwargs):
        return {'id': 1}


@pytest.fixture
def fake_site(monkeypatch):
    FakeSite.offline = set()
    FakeSite.permanent = set()
    FakeSite.rate_limited = {}
    FakeSite.instances = []

    async def noop_loop(self):
        return None

    monkeypatch.setattr(runtime, 'FundSiteClient', FakeSite)
    monkeypatch.setattr(FundService, '_loop', noop_loop)
    return FakeSite


class _StubClient:
    async def close(self):
        return None


def _live_service(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(runtime, 'now_ms', clock)
    service = FundService(FundConfig(tmp_path, live=True))
    service.clients['capital1'] = _StubClient()  # _settlements never uses the handle
    return service


async def _close(service):
    service.clients.clear()
    await service.close()


def _recovered(site):
    return [n for n in site.store.list('notices')
            if str(n['id']).startswith('login-recovered:capital1:')]


# --- transient startup login failures retry inside the running service ------

async def test_startup_transient_login_failure_is_retried_and_recovers(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    CredentialVault(tmp_path).save('capital1', 'alice', 'pw-alice')
    fake_site.offline = {'alice'}
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        target = service._login_retry.get('capital1')
        assert target is not None
        assert target['attempts'] == 1 and target['permanent'] is False
        assert target['next_ms'] == clock.value + 10_000     # first backoff is 10s
        assert 'capital1' not in service.clients
        assert service.public_status()['status']['detail'] == '账号连接失败，等待重试'
        assert len(fake_site.instances) == 1                  # one startup attempt only

        # Before its own deadline the loop must not touch the network again.
        clock.value += 5_000
        await service.tick()
        assert len(fake_site.instances) == 1
        assert service.public_status()['network']['ok'] is False

        # Site comes back: the next due tick restores the account and clears it.
        fake_site.offline.clear()
        clock.value += 5_000                                   # exactly the deadline
        await service.tick()
        assert 'capital1' in service.clients
        assert 'capital1' not in service._login_retry
        assert len(fake_site.instances) == 2
        assert len(_recovered(service)) == 1                  # exactly one recovery notice
        assert service.public_status()['status']['detail'] == '持续监控'
    finally:
        await service.close()


async def test_one_fund_recovery_does_not_erase_the_other_failed_fund(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    vault = CredentialVault(tmp_path)
    vault.save('capital1', 'alice', 'pw-alice')
    vault.save('capital2', 'bob', 'pw-bob')
    fake_site.offline = {'bob'}
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        assert 'capital1' in service.clients      # partial start: one fund is up
        assert 'capital2' not in service.clients
        assert set(service._login_retry) == {'capital2'}
        assert service.public_status()['status']['detail'] == '账号连接失败，等待重试'

        fake_site.offline.clear()
        clock.value += 10_000
        await service.tick()
        assert {'capital1', 'capital2'} <= set(service.clients)
        assert service._login_retry == {}
        assert _recovered(service) == []          # capital1 never failed
        recovered2 = [n for n in service.store.list('notices')
                      if str(n['id']).startswith('login-recovered:capital2:')]
        assert len(recovered2) == 1
        assert service.public_status()['status']['detail'] == '持续监控'
    finally:
        await service.close()


async def test_permanent_login_failure_is_not_hammered_and_manual_login_clears(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    CredentialVault(tmp_path).save('capital1', 'alice', 'wrong-pw')
    fake_site.permanent = {'alice'}
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        target = service._login_retry['capital1']
        assert target['permanent'] is True and target['next_ms'] is None
        assert target['code'] == 'unauthorized'
        assert service.public_status()['status']['detail'] == '账号连接失败，需要重新登录'

        # A wrong password must never be retried, however long the service runs.
        for _ in range(7):
            clock.value += 60_000
            await service.tick()
        assert len(fake_site.instances) == 1

        # The operator supplies the right password: the target clears and exactly
        # one durable recovery notice is queued.
        fake_site.permanent.clear()
        await service.login('capital1', 'alice', 'right-pw')
        assert 'capital1' in service.clients
        assert 'capital1' not in service._login_retry
        clock.value += 1
        await service.tick()
        assert service.public_status()['status']['detail'] == '持续监控'
        assert len(_recovered(service)) == 1
    finally:
        await service.close()


async def test_missing_credentials_do_not_fabricate_healthy_network(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        await service.tick()
        status = service.public_status()
        assert status['status']['detail'] == '等待基金账号登录'
        assert status['network']['ok'] is False
        assert status['network']['state'] != 'up'
        assert service._login_retry == {}
        assert not service.clients
        assert fake_site.instances == []          # never fabricated a login probe
    finally:
        await service.close()


def test_login_backoff_boundaries_and_retry_after():
    delay = runtime._login_retry_delay_ms
    assert [delay(n, None) for n in (1, 2, 3, 4, 5, 9)] == [
        10_000, 20_000, 40_000, 60_000, 60_000, 60_000]
    assert delay(1, 0) == 10_000                  # zero is finite/non-negative
    assert delay(1, 45.0) == 45_000               # a 429 Retry-After is honoured
    assert delay(4, 90.0) == 90_000
    assert delay(1, -5.0) == 10_000               # invalid headers are ignored
    assert delay(1, float('nan')) == 10_000
    assert delay(1, float('inf')) == 10_000
    assert delay(0, None) == 10_000 and delay('x', None) == 10_000


async def test_rate_limited_login_honours_retry_after(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    CredentialVault(tmp_path).save('capital1', 'alice', 'pw')
    fake_site.rate_limited = {'alice': 45.0}
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        target = service._login_retry['capital1']
        assert target['code'] == 'rate_limited'
        assert target['permanent'] is False
        # A 429 Retry-After of 45s beats the 10s base and is honoured exactly.
        assert target['next_ms'] == clock.value + 45_000
        clock.value += 44_999
        await service.tick()
        assert len(fake_site.instances) == 1
        clock.value += 1
        await service.tick()
        assert len(fake_site.instances) == 2
    finally:
        await service.close()


async def test_login_retry_deadline_is_compared_each_tick(tmp_path, fake_site, monkeypatch):
    clock = Clock(real_now_ms() + 60_000)
    monkeypatch.setattr(runtime, 'now_ms', clock)
    CredentialVault(tmp_path).save('capital1', 'alice', 'pw')
    fake_site.offline = {'alice'}
    service = FundService(FundConfig(tmp_path))
    try:
        await service.start()
        assert service._login_retry['capital1']['next_ms'] == clock.value + 10_000
        clock.value += 9_999                  # one millisecond short of the deadline
        await service.tick()
        assert len(fake_site.instances) == 1
        clock.value += 1                      # exactly due
        await service.tick()
        assert len(fake_site.instances) == 2
        assert service._login_retry['capital1']['next_ms'] == clock.value + 20_000
        clock.value += 19_999
        await service.tick()
        assert len(fake_site.instances) == 2
        clock.value += 1
        await service.tick()
        assert len(fake_site.instances) == 3
    finally:
        await service.close()


# ------------------------------------------- backlog of unfinished settlements

async def test_older_pending_month_is_retried_and_blocks_newer_months(tmp_path, monkeypatch):
    service = _live_service(tmp_path, monkeypatch, Clock(real_now_ms() + 60_000))
    led = service.ledger
    try:
        t0 = _ms(2026, 1, 5)
        led.mark_account('capital1', 1000 * U, 0, t0, t0)
        led.seed('capital1', 'inst', 1000 * U, t0)
        first = led.settle_month('capital1', '2026-01', _ms(2026, 2, 2))
        assert first['status'] == 'pending_valuation'

        # February's own book is valid, but January is still unresolved.
        led.mark_account('capital1', 1000 * U, 0, _ms(2026, 2, 28, 23, 59, 55),
                         _ms(2026, 2, 28, 23, 59, 55))
        await service._settlements(_ms(2026, 3, 2))

        jan = service.store.get('periods', 'capital1:2026-01')
        assert jan['status'] == 'pending_valuation'
        assert jan['retry_count'] > first['retry_count']         # older month retried
        assert service.store.get('periods', 'capital1:2026-02') is None
        assert service.store.get('runtime_settlements', 'capital1:month:2026-02') is None
        hold = service.store.get('valuation_holds', 'capital1')
        assert hold == {'hold': True, 'period': '2026-01', 'reason': jan['reason']}
        assert led.pending_payouts('capital1') == []             # unpriceable: no payout

        # A missing/stale valuation notifies once, not once per tick.
        stuck = [n for n in service.store.list('notices')
                 if '结算挂起' in str(n.get('content', ''))]
        assert len(stuck) == 1

        # The backlog is inspectable from the public status.
        view = service.public_status()['funds'][0]['settlement']
        assert view['hold'] is True and view['period'] == '2026-01'
        assert any(row['period'] == '2026-01' for row in view['pending'])
    finally:
        await _close(service)


async def test_valid_older_snapshot_settles_in_sequence_and_dedups(tmp_path, monkeypatch):
    service = _live_service(tmp_path, monkeypatch, Clock(real_now_ms() + 60_000))
    led = service.ledger
    try:
        t0 = _ms(2026, 1, 5)
        led.mark_account('capital1', 1000 * U, 0, t0, t0)
        led.seed('capital1', 'inst', 1000 * U, t0)
        assert led.settle_month('capital1', '2026-01', _ms(2026, 2, 2))['status'] == 'pending_valuation'

        # A genuinely pre-cutoff frozen book becomes available for January; the
        # ledger replays that snapshot rather than repricing it with a later quote.
        snapshot = service.store.get('cutoffs', 'capital1:2026-01')
        snapshot['quote_ms'] = _ms(2026, 1, 31, 23, 59, 55)
        snapshot['updated_ms'] = snapshot['quote_ms']
        snapshot['captured_ms'] = snapshot['quote_ms']
        service.store.put('cutoffs', 'capital1:2026-01', snapshot)

        await service._settlements(_ms(2026, 2, 2, 1))
        jan = service.store.get('periods', 'capital1:2026-01')
        assert jan['status'] == 'settled'
        assert service.store.get('runtime_settlements',
                                 'capital1:month:2026-01')['result']['status'] == 'settled'
        shares = led.status('capital1')['shares_atoms']
        payouts = len(led.pending_payouts('capital1'))

        await service._settlements(_ms(2026, 2, 2, 2))
        assert led.status('capital1')['shares_atoms'] == shares   # settled exactly once
        assert len(led.pending_payouts('capital1')) == payouts
    finally:
        await _close(service)


async def test_settlements_ignore_malformed_and_future_periods(tmp_path, monkeypatch):
    service = _live_service(tmp_path, monkeypatch, Clock(real_now_ms() + 60_000))
    led = service.ledger
    try:
        t0 = _ms(2026, 1, 5)
        led.mark_account('capital1', 1000 * U, 0, t0, t0)
        led.seed('capital1', 'inst', 1000 * U, t0)
        service.store.put('periods', 'capital1:not-a-period',
                          {'period': 'not-a-period', 'status': 'pending_valuation'})
        service.store.put('periods', 'capital1:2999-13',
                          {'period': '2999-13', 'status': 'pending_valuation'})
        service.store.put('periods', 'capital1:2999-01',
                          {'period': '2999-01', 'status': 'pending_valuation'})

        periods = service._settlement_periods('capital1', '2026-02', _ms(2026, 3, 2))
        assert 'not-a-period' not in periods and '2999-13' not in periods
        assert '2999-01' not in periods                      # future month ignored
        assert periods == ['2026-02']                        # only the previous month
    finally:
        await _close(service)
