"""Control-user capital, fee remittance, and private financial reporting."""
from datetime import datetime
import asyncio
from types import SimpleNamespace
import pytest

from raricy_capital.config import FundConfig
from raricy_capital.contracts import BEIJING, FundError
from raricy_capital.store import FundStore
from raricy_capital.ledger import FundLedger
from raricy_capital.payments import PaymentsWorker
from raricy_capital.commands import CommandHandler

U = 10000
F = 'capital1'
C = 'control-user'
def ms(month, day, hour=10, minute=0, second=0):
    return int(datetime(2026, month, day, hour, minute, second, tzinfo=BEIJING).timestamp()*1000)
T = ms(10, 4)

@pytest.fixture
def ledger(tmp_path, request):
    store = FundStore(tmp_path/'control.db')
    request.addfinalizer(store.close)
    led = FundLedger(store, control_user_id=C)
    led.mark_account(F, 1000*U, 0, T, T)
    led.seed(F, C, 1000*U, T)
    yield led

def incoming(led, user='investor', amount=105, note=None, tid='in-1', when=T+1000, row=91):
    order = None
    if note is None:
        order = led.create_subscription(F, user, 100*U, tid+'-order', T)
        note = order['payment_note']
    tx = {'transfer_id':tid,'from_user_id':user,'amount_units':amount*U,
          'note':note,'occurred_ms':when,'transaction_row_id':row}
    receipt = led.receive_transfer(F,tx,when+1000)
    led.mark_account(F,(1000+amount)*U,0,when+1000,when+1000)
    return order,tx,receipt

def settle(led):
    cutoff = ms(10,31,23,59,55)
    led.mark_account(F,led.status(F)['wallet_units'],0,cutoff,cutoff)
    return led.settle_month(F,'2026-10',ms(11,1,0))

def test_control_subscription_has_zero_fee_but_others_pay_five_percent(ledger):
    own=ledger.create_subscription(F,C,100*U,'own',T)
    other=ledger.create_subscription(F,'investor',100*U,'other',T)
    assert own['fee_units']==0 and own['total_units']==100*U
    assert other['fee_units']==5*U and other['total_units']==105*U

def test_any_control_transfer_is_principal_without_note_or_order(ledger):
    _,tx,receipt=incoming(ledger,C,100,note='随时追加本金')
    assert receipt['status']=='received' and receipt['kind']=='institution_capital'
    sub=ledger.store.get('subscriptions',receipt['subscription_id'])
    assert sub['principal_units']==100*U and sub['fee_units']==0
    assert ledger.status(F)['pending_receipts_units']==100*U
    assert ledger.status(F)['equity_units']==1000*U
    assert ledger.status(F)['unclaimed_units']==0
    ledger.receive_transfer(F,tx,T+3000)
    assert len(ledger.store.list('subscriptions'))==1
    result=settle(ledger)
    assert result['status']=='settled'
    assert ledger.status(F,C)['user_value_units']==1100*U
    assert ledger.store.list('payouts')==[]

def test_control_order_is_matched_without_expiry_refund(ledger):
    own=ledger.create_subscription(F,C,100*U,'own',T)
    _,_,receipt=incoming(ledger,C,100,note=own['payment_note'],when=T+180001)
    assert receipt['subscription_id']==own['id']
    assert ledger.store.get('subscriptions',own['id'])['status']=='received'
    assert not ledger.store.list('payouts')

def test_control_capital_after_monthly_window_rolls_forward(ledger):
    _,_,receipt=incoming(ledger,C,100,note='本金',when=ms(10,26))
    assert ledger.store.get('subscriptions',receipt['subscription_id'])['period']=='2026-11'
    settle(ledger)
    assert ledger.status(F)['pending_receipts_units']==100*U

def test_fee_not_remitted_until_shares_issued_and_is_nav_neutral(ledger):
    order,_,_=incoming(ledger)
    assert ledger.queue_institution_fees(T+4000)==[]
    assert not ledger.store.list('payouts')
    settle(ledger)
    status=ledger.status(F)
    assert status['fee_balance_units']==0 and status['liabilities_units']==5*U
    assert status['equity_units']==1100*U and status['nav']=='1.00000000'
    payouts=ledger.store.list('payouts')
    assert len(payouts)==1 and payouts[0]['kind']=='subscription_fee'
    assert payouts[0]['user_id']==C and payouts[0]['amount_units']==5*U
    assert payouts[0]['transaction_row_id']==91
    assert len(payouts[0]['idempotency_key'])<=48 and len(payouts[0]['note'])<=30
    assert ledger.queue_institution_fees(ms(11,2))==[]
    assert len(ledger.store.list('payouts'))==1

def test_cancellation_returns_principal_and_fee_without_institution_payout(ledger):
    order,_,_=incoming(ledger)
    ledger.cancel_order(F,'investor',order['id'],T+3000)
    assert ledger.queue_institution_fees(T+4000)==[]
    payout=ledger.store.list('payouts')[0]
    assert payout['user_id']=='investor' and payout['amount_units']==105*U
    assert ledger.status(F)['fee_balance_units']==0

def test_first_issuance_also_queues_fee(tmp_path):
    store=FundStore(tmp_path/'first.db')
    try:
        led=FundLedger(store,control_user_id=C)
        led.mark_account(F,0,0,T,T)
        order=led.create_subscription(F,'investor',100*U,'first',T)
        led.receive_transfer(F,{'transfer_id':'first-in','from_user_id':'investor',
            'amount_units':105*U,'note':order['payment_note'],'occurred_ms':T+1},T+2)
        led.mark_account(F,105*U,0,ms(10,31,23,59,55),ms(10,31,23,59,55))
        led.settle_month(F,'2026-10',ms(11,1,0))
        assert led.status(F)['equity_units']==100*U
        assert len(store.list('payouts'))==1
    finally:store.close()

class Site:
    user_id='fund-site-account'
    def __init__(self,cash): self.cash,self.sent,self.rows,self.lost=cash,[],[],False
    async def balance(self):return self.cash
    async def transactions(self,since):
        rows=[r for r in self.rows if r['id']>since]
        return {'transactions':rows,'next_cursor':rows[-1]['id'] if rows else since,'has_more':False}
    async def transfer(self,user,amount,note,key):
        self.sent.append((user,amount,note,key));self.cash-=amount
        self.rows.append({'id':101,'type':'transfer','transfer_id':'fee-out',
            'to_user_id':user,'amount_units':-amount,'note':note})
        if self.lost: raise TimeoutError('response lost')
        return {'transfer_id':'fee-out','amount_units':amount}

@pytest.mark.parametrize('lost',[False,True])
async def test_fee_transfer_is_reconciled_without_double_payment_or_nav_loss(ledger,lost):
    incoming(ledger);settle(ledger)
    site=Site(1105*U);site.lost=lost
    worker=PaymentsWorker(ledger.store,ledger,{F:site},live=True)
    assert await worker.drain_payouts(ms(11,1,0))==0
    result=await worker.drain_institution_fees(ms(11,1,0,1))
    assert result['held' if lost else 'paid']==1
    if lost:
        ledger.mark_account(F,1100*U,0,ms(11,1,0,2),ms(11,1,0,2))
        assert (await worker.drain_institution_fees(ms(11,1,0,3)))['reconciled']==1
    assert len(site.sent)==1 and site.sent[0][:2]==(C,5*U)
    assert ledger.status(F)['equity_units']==1100*U
    assert ledger.status(F)['wallet_units']==1100*U
    assert ledger.status(F)['liabilities_units']==0
    await worker.drain_institution_fees(ms(11,1,0,4))
    assert len(site.sent)==1

async def test_readonly_worker_does_not_queue_legacy_fees_or_transfer(ledger):
    legacy=FundLedger(ledger.store)
    incoming(legacy);settle(legacy)
    assert legacy.status(F)['fee_balance_units']==5*U
    site=Site(1105*U)
    worker=PaymentsWorker(ledger.store,ledger,{F:site},live=False)
    await worker.drain_institution_fees(ms(11,1,0,1))
    assert not site.sent and not ledger.store.list('payouts')

def test_existing_issued_fees_are_queued_once_and_receiver_is_pinned(ledger):
    legacy=FundLedger(ledger.store)
    incoming(legacy);settle(legacy)
    queued=ledger.queue_institution_fees(ms(11,1,0,1))
    assert len(queued)==1 and queued[0]['user_id']==C
    changed=FundLedger(ledger.store,control_user_id='another-controller')
    assert changed.queue_institution_fees(ms(11,1,0,2))==[]
    assert ledger.store.list('payouts')[0]['user_id']==C

async def test_control_check_lists_both_funds_and_private_users_cannot_see_it(ledger):
    ledger.mark_account('capital2',200*U,0,T,T)
    ledger.seed('capital2',C,200*U,T)
    handler=CommandHandler(ledger,ledger.store,Site(1000*U),F,live=False)
    await handler.handle({'id':1,'channel_id':'d_control','author':{'id':C},'content':'/check'},T)
    text=handler.last_reply
    assert '控制用户资金总览' in text and 'capital1' in text and 'capital2' in text
    assert '账户钱包' in text and '待确认本金' in text and '机构手续费' in text
    other=CommandHandler(ledger,ledger.store,Site(1000*U),F,live=False)
    await other.handle({'id':2,'channel_id':'d_other','author':{'id':'investor','username':C},'content':'/check'},T)
    assert '控制用户资金总览' not in other.last_reply and 'capital2' not in other.last_reply
    assert '仅显示您本人的持仓' in other.last_reply
    other.last_reply=None
    await other.handle({'id':3,'channel_id':'lobby','author':{'id':C},'content':'/check'},T)
    assert other.last_reply is None

@pytest.mark.parametrize('bad',[True,False,None,0,123,'control user','x'*129])
def test_control_user_configuration_rejects_invalid_identity(tmp_path,bad):
    with pytest.raises(FundError,match='invalid_control_user'):
        FundConfig(tmp_path,control_user_id=bad)

def test_control_identity_is_loaded_from_yaml_and_not_from_username(tmp_path):
    config=tmp_path/'funds.yaml'
    config.write_text('control_user_id: control-user\ndata_dir: '+str(tmp_path/'state'),encoding='utf-8')
    assert FundConfig.load(config).control_user_id==C


async def test_control_user_cannot_log_in_as_a_fund_account(tmp_path,monkeypatch):
    from raricy_capital import runtime
    class OwnAccount:
        closed=False
        async def login(self): return {'id':C}
        async def close(self): self.closed=True
    client=OwnAccount()
    monkeypatch.setattr(runtime,'FundSiteClient',lambda *a,**kw:client)
    service=runtime.FundService(FundConfig(tmp_path,control_user_id=C))
    try:
        with pytest.raises(FundError,match='control_user_cannot_be_fund_account'):
            await service._login(F,'u','p',persist=False)
        assert client.closed and not service.clients
        assert runtime._login_failure_is_permanent(FundError('control_user_cannot_be_fund_account'))
    finally: await service.close()


def test_configured_control_user_cannot_equal_a_previously_registered_fund(tmp_path):
    from raricy_capital.runtime import FundService
    store=FundStore(tmp_path/'funds.sqlite3')
    store.put('accounts',F,{'user_id':C})
    store.close()
    with pytest.raises(FundError,match='control_user_cannot_be_fund_account'):
        FundService(FundConfig(tmp_path,control_user_id=C))


def test_control_config_environment_fallback_and_yaml_precedence(tmp_path,monkeypatch):
    monkeypatch.setenv('FUNDS_CONTROL_USER_ID',C)
    assert FundConfig(tmp_path/'env').control_user_id==C
    assert FundConfig(tmp_path/'yaml',control_user_id='yaml-controller').control_user_id=='yaml-controller'


async def test_fee_waits_when_fresh_balance_cannot_cover_pending_capital(ledger):
    incoming(ledger);settle(ledger)
    incoming(ledger,C,1200,note='追加本金',tid='own-new',when=ms(11,2))
    site=Site(1200*U)
    worker=PaymentsWorker(ledger.store,ledger,{F:site},live=True)
    result=await worker.drain_institution_fees(ms(11,2,10,1))
    assert result['waiting']==1 and not site.sent
    assert ledger.status(F)['pending_receipts_units']==1200*U
    assert ledger.status(F)['liabilities_units']==5*U
    payout=ledger.store.list('payouts')[0]
    assert payout['status']=='pending'
    assert payout['waiting_reason']=='available_cash_shortage'
