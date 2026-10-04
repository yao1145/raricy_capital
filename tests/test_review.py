"""Review probes: cash/share conservation and restart/expiry boundaries."""
from datetime import datetime
from decimal import Decimal
import pytest
from raricy_capital.contracts import BEIJING
from raricy_capital.ledger import FundLedger
from raricy_capital.store import FundStore

U=10000
A=100000000
F='capital1'

def ms(month,day,hour=0,minute=0,second=0):
    return int(datetime(2026,month,day,hour,minute,second,tzinfo=BEIJING).timestamp()*1000)

@pytest.fixture
def led(tmp_path):
    store=FundStore(tmp_path/'review.sqlite3')
    ledger=FundLedger(store)
    stamp=ms(1,5)
    ledger.mark_account(F,1000*U,0,stamp,stamp)
    ledger.seed(F,'sponsor',1000*U,stamp)
    yield ledger
    store.close()

def paid(led,sub,occurred,observed,transfer_id='review-tx'):
    return led.receive_transfer(F,{'transfer_id':transfer_id,'from_user_id':sub['user_id'],
         'amount_units':sub['total_units'],'note':sub['payment_note'],'occurred_ms':occurred},observed)

def assert_shares(led):
    holders=led.store.list('holders',F+':')
    assert led.status(F)['shares_atoms']==sum(h['shares_atoms'] for h in holders)
    assert all(0<=h['reserved_atoms']<=h['shares_atoms'] for h in holders)

def test_expired_locally_timely_receipt_still_accepted(led):
    t=ms(1,10)
    sub=led.create_subscription(F,'u',100*U,'review',t)
    led.expire_subscriptions(t+200000)
    result=paid(led,sub,t+30000,t+201000)
    assert result['status']=='received'

def test_expired_locally_late_receipt_full_refund(led):
    t=ms(1,10)
    sub=led.create_subscription(F,'u',100*U,'review',t)
    led.expire_subscriptions(t+200000)
    result=paid(led,sub,t+190000,t+201000)
    assert result['status']=='refunded'
    assert led.pending_payouts(F)[0]['amount_units']==105*U

def test_external_subscription_shares_conserved_and_nav_flat(led):
    t=ms(1,10)
    sub=led.create_subscription(F,'u',1000*U,'review',t)
    paid(led,sub,t+30000,t+31000)
    end=ms(1,31,23,59,55)
    led.mark_account(F,2050*U,0,end,end)
    led.settle_month(F,'2026-01',ms(2,1))
    assert_shares(led)
    assert led.status(F)['nav']=='1.00000000'
    assert led.status(F)['shares_atoms']==2000*A

def test_monthly_gate_preserves_reserved_remainder(led):
    t=ms(1,10)
    led.mark_account(F,1000*U,0,t,t)
    led.request_redemption(F,'sponsor',1000*U,'ordinary','review',t)
    end=ms(1,31,23,59,55)
    led.mark_account(F,1000*U,0,end,end)
    led.settle_month(F,'2026-01',ms(2,1))
    assert_shares(led)
    holder=led.holder(F,'sponsor')
    assert holder['shares_atoms']==800*A
    assert holder['reserved_atoms']==800*A

def test_subscription_after_window_not_issued_this_month(led):
    t=ms(1,26)
    sub=led.create_subscription(F,'u',100*U,'review',t)
    paid(led,sub,t+30000,t+31000)
    end=ms(1,31,23,59,55)
    led.mark_account(F,1105*U,0,end,end)
    result=led.settle_month(F,'2026-01',ms(2,1))
    assert result['issued_external_shares_atoms']==0
    assert led.status(F)['pending_receipts_units']==100*U

def test_complete_emergency_liquidation_does_not_strand_fee(led):
    t=ms(1,10,17)
    led.mark_account(F,1000*U,0,t,t)
    led.request_redemption(F,'sponsor',1000*U,'emergency','review',t)
    batch=ms(1,10,20)
    led.mark_account(F,1000*U,0,batch,batch)
    led.settle_emergency(F,batch)
    payouts=led.pending_payouts(F)
    assert sum(p['amount_units'] for p in payouts)==1000*U
    assert led.status(F)['shares_atoms']==0

def test_later_emergency_quote_cannot_impersonate_batch_price(led):
    t=ms(1,10,17)
    led.mark_account(F,1000*U,0,t,t)
    led.request_redemption(F,'sponsor',100*U,'emergency','review',t)
    late=ms(1,10,22)
    led.mark_account(F,2000*U,0,late,late)
    result=led.settle_emergency(F,late)
    assert not led.pending_payouts(F)
    assert led.holder(F,'sponsor')['shares_atoms']==1000*A
