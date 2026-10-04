"""Monthly application boundaries, replay safety and legacy order terms."""
from datetime import datetime
import pytest
from raricy_capital.contracts import BEIJING,FundError
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore
U=10000
F='capital1'
def ms(month,day,hour=0,minute=0,second=0):
    return int(datetime(2026,month,day,hour,minute,second,tzinfo=BEIJING).timestamp()*1000)
@pytest.fixture
def ledger(tmp_path):
    store=FundStore(tmp_path/'window.sqlite3')
    led=FundLedger(store)
    led.mark_account(F,1000*U,0,ms(1,1),ms(1,1))
    led.seed(F,'holder',1000*U,ms(1,1))
    yield led
    store.close()
def apply(led,kind,when,key='request'):
    led.mark_account(F,1000*U,0,when,when)
    if kind=='subscription':return led.create_subscription(F,'holder',100*U,key,when)
    return led.request_redemption(F,'holder',100*U,'ordinary',key,when)
@pytest.mark.parametrize('kind',['subscription','redemption'])
@pytest.mark.parametrize('when,period,deadline',[
    (ms(1,1),'2026-01',ms(1,7,18)),
    (ms(1,7,18),'2026-01',ms(1,7,18)),
    (ms(2,1),'2026-02',ms(2,7,18)),
])
def test_open_at_month_start_and_includes_exact_deadline(ledger,kind,when,period,deadline):
    order=apply(ledger,kind,when)
    assert order['period']==period and order['deadline_ms']==deadline
@pytest.mark.parametrize('kind',['subscription','redemption'])
@pytest.mark.parametrize('when',[ms(1,7,18)+1,ms(1,8),ms(1,31,23,59,59)])
def test_closed_window_creates_no_order_or_reservation(ledger,kind,when):
    with pytest.raises(FundError,match='window_closed'):apply(ledger,kind,when)
    assert ledger.store.list('subscriptions')==[]
    assert ledger.store.list('redemptions')==[]
    assert ledger.holder(F,'holder')['reserved_atoms']==0
@pytest.mark.parametrize('kind',['subscription','redemption'])
def test_original_message_replay_after_deadline_returns_original(ledger,kind):
    order=apply(ledger,kind,ms(1,7,17))
    assert apply(ledger,kind,ms(1,8))['id']==order['id']
def test_emergency_and_dividend_preferences_keep_original_dates(ledger):
    when=ms(1,20,17)
    ledger.mark_account(F,1000*U,0,when,when)
    red=ledger.request_redemption(F,'holder',100*U,'emergency','emergency',when)
    assert red['deadline_ms']==ms(1,20,18)
    assert ledger.set_dividend_choice(F,'holder',1,when)['effective_period']=='2026-01'
    assert ledger.set_dividend_choice(F,'holder',0,ms(1,26))['effective_period']=='2026-02'
def test_preexisting_received_order_keeps_recorded_payment_deadline(ledger):
    order=ledger.create_subscription(F,'investor',100*U,'legacy',ms(1,4))
    order.update(deadline_ms=ms(1,25,18),occurred_ms=ms(1,20),status='received')
    ledger.store.put('subscriptions',order['id'],order)
    fund=ledger.store.get('funds',F)
    fund.update(pending_receipts_units=100*U,fee_balance_units=5*U)
    ledger.store.put('funds',F,fund)
    cutoff=ms(1,31,23,59,55)
    ledger.mark_account(F,1105*U,0,cutoff,cutoff)
    result=ledger.settle_month(F,'2026-01',ms(2,1))
    assert result['issued_external_shares_atoms']==100*100000000
    assert ledger.status(F)['pending_receipts_units']==0
