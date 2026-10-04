"""Central assembly boundaries between normalized site units and fund workers."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
import pytest
from raricy_capital.adapters import TradingClientAdapter
from raricy_capital.config import FundConfig, CredentialVault
from raricy_capital.contracts import BEIJING, FundError
from raricy_capital.runtime import FundService
from raricy_capital.store import FundStore

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
