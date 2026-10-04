"""Shared fund precision, fixed strategy policies and timestamps."""
from __future__ import annotations
from dataclasses import dataclass,asdict
from decimal import Decimal,InvalidOperation
from datetime import datetime,timezone,timedelta
import time
import re

def validate_control_user_id(value: object) -> str:
    if not isinstance(value, str) or (value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value) is None):
        raise FundError("invalid_control_user")
    return value


MONEY_SCALE=10000
SHARE_SCALE=100000000
BEIJING=timezone(timedelta(hours=8))

class FundError(ValueError):
    def __init__(self,code:str,message:str|None=None):
        self.code=code;super().__init__(message or code)

def now_ms()->int:return time.time_ns()//1000000

def money_units(value:object,*,positive:bool=False)->int:
    if isinstance(value,bool):raise FundError('invalid_amount')
    try:d=Decimal(str(value))
    except (InvalidOperation,ValueError):raise FundError('invalid_amount') from None
    if not d.is_finite() or abs(d)>Decimal('1000000000000'):raise FundError('invalid_amount')
    scaled=d*MONEY_SCALE
    if scaled!=scaled.to_integral_value() or (positive and d<=0):raise FundError('invalid_amount')
    return int(scaled)

def money_text(units:int)->str:return f'{Decimal(units)/MONEY_SCALE:.4f}'

def timestamp_text(ms:int)->str:return datetime.fromtimestamp(ms/1000,BEIJING).isoformat()

@dataclass(frozen=True)
class FundPolicy:
    fund_id:str
    label:str
    leverage:int
    risk:float
    cap:float
    daily_loss:float
    drawdown_limit:float
    loss_gate_hours:int=0
    cooldown_hours:int=4
    max_hold_days:int=14
    ema_hours:int=96
    slope_hours:int=24
    atr_period:int=14
    atr_stop:float=2.
    subscription_fee:float=.05
    emergency_fee:float=.10
    dividend_fraction:float=.10
    monthly_redemption_fraction:float=.20
    def public(self)->dict:return asdict(self)

POLICIES={
    'capital1':FundPolicy('capital1','温和增长 · ER12',3,.0125,2.,.015,.25,12),
    'capital2':FundPolicy('capital2','激进增长 · 5倍',5,.05,5.,.05,.70),
}
