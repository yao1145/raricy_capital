"""FundLedger: single-writer fund accounting, shares, dividends and reservations.

All money is stored as integer ``*_units`` at 1e-4 precision (MONEY_SCALE) and all
shares as integer ``*_shares_atoms`` at 1e-8 precision (SHARE_SCALE).  NAV is a
Decimal expressed in money per share.  Every state transition is persisted inside
one :class:`FundStore` transaction before any external side effect is scheduled.

Net asset value model
---------------------
``FundStore`` holds the upstream wallet/position marks as authoritative absolute
values.  Several balances sit inside that wallet but do not belong to the fund, so
they are removed when computing net assets::

    equity_units = wallet + position
                   - pending_receipts     # subscription money, shares not issued yet
                   - fee_balance          # institution 5% service fee
                   - unclaimed            # money that could not be matched
                   - liabilities          # declared dividends / confirmed payouts

Declared-but-unpaid distributions are modelled as liabilities, so paying them only
clears the liability and does not reduce NAV a second time.

External-flow model
-------------------
``capital_flows_units`` moves only on a real external flow: seeded and issued
subscription principal enters as positive, gross redemptions and cash dividends
leave as negative.  A receipt that has not been issued yet is *not* a flow.  The
emergency fee retained by the fund is not trading profit and is kept apart in
``non_trading_income_units``; the trader normalises by the sum of that field and
``capital_flows_units``.  Period bookkeeping (``period_start_nav``,
``period_trade_income_units``) lets a month's own investment profit gate its
dividend, and the month-end/20:00 cutoffs freeze the book they price against.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR, localcontext
import uuid

from .contracts import (
    BEIJING,
    FundError,
    MONEY_SCALE,
    money_text,
    POLICIES,
    SHARE_SCALE,
    FundPolicy,
)

__all__ = ['FundLedger']

# Namespaces of the shared store.
_FUNDS = 'funds'
_HOLDERS = 'holders'
_SUBS = 'subscriptions'
_TRANSFERS = 'transfers'
_REDEMPTIONS = 'redemptions'
_PAYOUTS = 'payouts'
_NOTICES = 'notices'
_PERIODS = 'periods'
_CUTOFFS = 'cutoffs'
_TRADES = 'trades'
_UNCLAIMED = 'unclaimed'
_MSG = 'msg_keys'
_SEEDS = 'seeds'

_SUB_TTL_MS = 180 * 1000  # 180 second QR/payment-link validity


def _as_units(value: object, *, positive: bool = False) -> int:
    """Validate an integer 1e-4 money amount (already in units, never float)."""
    if isinstance(value, bool):
        raise FundError('invalid_amount')
    if isinstance(value, int):
        out = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise FundError('invalid_amount')
        out = int(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s.lstrip('-').isdigit():
            raise FundError('invalid_amount')
        out = int(s)
    else:
        raise FundError('invalid_amount')
    if out < 0 or (positive and out <= 0):
        raise FundError('invalid_amount')
    if out > 10 ** 18:
        raise FundError('invalid_amount')
    return out


def _as_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FundError('invalid_time')
    return value


def _as_fraction(value: object) -> Decimal:
    try:
        d = Decimal(str(value))
    except Exception:  # noqa: BLE001 - normalise to a business error
        raise FundError('invalid_fraction') from None
    if not d.is_finite() or d < 0 or d > 1:
        raise FundError('invalid_fraction')
    return d


def _nav_from(equity_units: int, shares_atoms: int) -> Decimal:
    if shares_atoms <= 0:
        return Decimal('1')
    with localcontext() as ctx:
        ctx.prec = 60
        return Decimal(equity_units) * MONEY_SCALE / Decimal(shares_atoms)


def _shares_for(amount_units: int, nav: Decimal) -> int:
    if nav <= 0:
        raise FundError('invalid_valuation')
    with localcontext() as ctx:
        ctx.prec = 60
        return int((Decimal(amount_units) * MONEY_SCALE / nav).to_integral_value(rounding=ROUND_FLOOR))


def _value_units(shares_atoms: int, nav: Decimal) -> int:
    if shares_atoms <= 0 or nav <= 0:
        return 0
    with localcontext() as ctx:
        ctx.prec = 60
        return int((Decimal(shares_atoms) * nav / MONEY_SCALE).to_integral_value(rounding=ROUND_FLOOR))


def _nav_text(nav: Decimal) -> str:
    with localcontext() as ctx:
        ctx.prec = 40
        return format(nav.quantize(Decimal('0.00000001')), 'f')


def _shares_text(atoms: int) -> str:
    with localcontext() as ctx:
        ctx.prec = 40
        return format((Decimal(atoms) / SHARE_SCALE).normalize(), 'f')


def _month_start(dt: datetime) -> datetime:
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _period_from_ms(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, BEIJING)
    return f'{dt.year:04d}-{dt.month:02d}'


def _period_cutoff_ms(period: str) -> int:
    try:
        year, month = (int(part) for part in period.split('-'))
        if month < 1 or month > 12:
            raise ValueError
        start = datetime(year, month, 1, tzinfo=BEIJING)
    except (ValueError, TypeError, AttributeError):
        raise FundError('invalid_period') from None
    if month == 12:
        nxt = datetime(year + 1, 1, 1, tzinfo=BEIJING)
    else:
        nxt = datetime(year, month + 1, 1, tzinfo=BEIJING)
    return int(nxt.timestamp() * 1000)


def _next_period(period: str) -> str:
    year, month = (int(part) for part in period.split('-'))
    return f'{year + 1:04d}-01' if month == 12 else f'{year:04d}-{month + 1:02d}'


def _period_window_ms(period: str) -> int:
    """The 25th 18:00 (Beijing) application/payment deadline of a period."""
    year, month = (int(part) for part in period.split('-'))
    return int(datetime(year, month, 25, 18, tzinfo=BEIJING).timestamp() * 1000)


def _cutoff_for_now(now_ms: int) -> tuple[str, int]:
    """Most recent month-end cutoff that ``now_ms`` has passed."""
    dt = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    cutoff_ms = int(_month_start(dt).timestamp() * 1000)
    return _period_from_ms(cutoff_ms - 1), cutoff_ms


def _ordinary_period(now_ms: int) -> tuple[str, int]:
    """Subscription/redemption period for an application at ``now_ms``.

    The window closes on the 25th at 18:00 Beijing time; later applications roll to
    the next calendar month.
    """
    dt = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    deadline = dt.replace(day=25, hour=18, minute=0, second=0, microsecond=0)
    if dt <= deadline:
        return _period_from_ms(now_ms), int(deadline.timestamp() * 1000)
    nxt = _month_start(_month_start(dt) + timedelta(days=32))
    next_deadline = nxt.replace(day=25, hour=18, minute=0, second=0, microsecond=0)
    return f'{nxt.year:04d}-{nxt.month:02d}', int(next_deadline.timestamp() * 1000)


def _emergency_batch(now_ms: int) -> tuple[str, int, int]:
    """Return the emergency batch key, its 18:00 cutoff and 20:00 valuation."""
    dt = datetime.fromtimestamp(now_ms / 1000, BEIJING)
    day = dt
    if dt > dt.replace(hour=18, minute=0, second=0, microsecond=0):
        day = dt + timedelta(days=1)
    day0 = day.replace(hour=0, minute=0, second=0, microsecond=0)
    key = f'{day0.year:04d}-{day0.month:02d}-{day0.day:02d}'
    cutoff_ms = int(day0.replace(hour=18).timestamp() * 1000)
    valuation_ms = int(day0.replace(hour=20).timestamp() * 1000)
    return key, cutoff_ms, valuation_ms


class FundLedger:
    """Accounting owner for both funds.  One instance per process/writer."""

    #: A settlement cutoff price must be no older than this.  The live service
    #: marks the account every few seconds, so 15s still admits the last mark
    #: before a cutoff while rejecting multi-hour stale quotes outright.
    max_quote_age_ms = 15 * 1000

    def __init__(self, store, policies: dict[str, FundPolicy] | None = None):
        self.store = store
        self.policies = policies or POLICIES

    # ------------------------------------------------------------------ helpers
    def _policy(self, fund_id: str) -> FundPolicy:
        policy = self.policies.get(fund_id)
        if policy is None:
            raise FundError('invalid_fund')
        return policy

    def _default_fund(self, fund_id: str) -> dict:
        policy = self._policy(fund_id)
        return {
            'fund_id': fund_id,
            'label': policy.label,
            'state': 'created',
            'wallet_units': 0,
            'position_value_units': 0,
            'quote_ms': 0,
            'updated_ms': 0,
            'pending_receipts_units': 0,
            'fee_balance_units': 0,
            'unclaimed_units': 0,
            'liabilities_units': 0,
            'shares_atoms': 0,
            'realized_profit_units': 0,
            'benchmark_nav': '1',
            'capital_flows_units': 0,
            'non_trading_income_units': 0,
            'period_trade_income_units': 0,
            'period_start_nav': '1',
            'seeded_units': 0,
            'seq': 0,
            'last_cutoff_period': None,
        }

    def _fund(self, fund_id: str) -> dict:
        self._policy(fund_id)
        f = self.store.get(_FUNDS, fund_id)
        if f is None:
            f = self._default_fund(fund_id)
        return f

    def _save_fund(self, fund: dict) -> None:
        self.store.put(_FUNDS, fund['fund_id'], fund)

    def _holder(self, fund_id: str, user_id: str) -> dict:
        key = f'{fund_id}:{user_id}'
        h = self.store.get(_HOLDERS, key)
        if h is None:
            h = {'fund_id': fund_id, 'user_id': user_id, 'shares_atoms': 0, 'reserved_atoms': 0}
        return h

    def _save_holder(self, holder: dict) -> None:
        self.store.put(_HOLDERS, f"{holder['fund_id']}:{holder['user_id']}", holder)

    @staticmethod
    def _equity(fund: dict) -> int:
        return (
            fund['wallet_units']
            + fund['position_value_units']
            - fund['pending_receipts_units']
            - fund['fee_balance_units']
            - fund['unclaimed_units']
            - fund['liabilities_units']
        )

    @staticmethod
    def _available_cash(fund: dict) -> int:
        return (
            fund['wallet_units']
            - fund['pending_receipts_units']
            - fund['fee_balance_units']
            - fund['unclaimed_units']
            - fund['liabilities_units']
        )

    @staticmethod
    def _snap_equity(snap: dict) -> int:
        return (
            snap['wallet_units']
            + snap['position_value_units']
            - snap['pending_receipts_units']
            - snap['fee_balance_units']
            - snap['unclaimed_units']
            - snap['liabilities_units']
        )

    @staticmethod
    def _snap_cash(snap: dict) -> int:
        return (
            snap['wallet_units']
            - snap['pending_receipts_units']
            - snap['fee_balance_units']
            - snap['unclaimed_units']
            - snap['liabilities_units']
        )

    def _require_nav(self, fund: dict, now_ms: int) -> tuple[Decimal, int]:
        quote_ms = fund['quote_ms']
        if quote_ms <= 0 or quote_ms > now_ms or now_ms - quote_ms > self.max_quote_age_ms:
            raise FundError('nav_unavailable')
        return _nav_from(self._equity(fund), fund['shares_atoms']), quote_ms

    def _next_seq(self, fund: dict) -> int:
        fund['seq'] = int(fund.get('seq', 0)) + 1
        return fund['seq']

    def _notify(self, fund_id: str, user_id: str | None, content: str, now_ms: int,
                notice_id: str | None = None) -> str:
        # Callers that need a *durable, single* notice pass a deterministic
        # ``notice_id``; a replay then finds the existing record and never
        # duplicates the message.  Ordinary notices keep a random id.
        notice_id = notice_id or uuid.uuid4().hex[:16]
        if self.store.get(_NOTICES, notice_id) is not None:
            return notice_id
        self.store.put(_NOTICES, notice_id, {
            'id': notice_id,
            'fund_id': fund_id,
            'user_id': user_id,
            'content': content,
            'created_ms': now_ms,
            'status': 'queued',
        })
        self.store.append_event('notice', fund_id=fund_id, details={
            'notice_id': notice_id,
            'user_id': user_id,
            'content': content,
        })
        return notice_id

    def _add_payout(self, fund: dict, user_id: str, amount_units: int, note: str,
                    kind: str, now_ms: int) -> dict:
        if amount_units <= 0:
            raise FundError('invalid_amount')
        payout_id = f'{kind}-{uuid.uuid4().hex[:12]}'
        record = {
            'id': payout_id,
            'fund_id': fund['fund_id'],
            'user_id': user_id,
            'amount_units': amount_units,
            'note': note[:120],
            'idempotency_key': f'{kind}:{fund["fund_id"]}:{uuid.uuid4().hex[:16]}'[:48],
            'status': 'pending',
            'kind': kind,
            'created_ms': now_ms,
        }
        self.store.put(_PAYOUTS, f"{fund['fund_id']}:{payout_id}", record)
        fund['liabilities_units'] += amount_units
        return record

    def _ensure_cutoff_snapshot(self, fund_id: str, now_ms: int) -> None:
        """Freeze holder/account marks before the first write after month end.

        The guard runs before any mutation, so the very first post-cutoff operation
        captures the state as it stood at the cutoff; later post-cutoff flows can no
        longer distort that period's settlement.  A later mark that *carries* a
        genuinely pre-cutoff, fresh quote may refresh only the frozen valuation; that
        is what makes a deferred ``pending_valuation`` period retriable without ever
        letting a post-cutoff price masquerade as the cutoff price.
        """
        fund = self._fund(fund_id)
        period, cutoff_ms = _cutoff_for_now(now_ms)
        snapshot_key = f'{fund_id}:{period}'
        snapshot = self.store.get(_CUTOFFS, snapshot_key)
        if snapshot is not None:
            self._refresh_cutoff_valuation(snapshot_key, snapshot, fund, cutoff_ms)
            if fund.get('last_cutoff_period') != period:
                fund['last_cutoff_period'] = period
                self._save_fund(fund)
            return
        last = fund.get('last_cutoff_period')
        if last is not None and period <= last:
            return
        holders = {
            key.split(':', 1)[1]: value['shares_atoms']
            for key, value in self.store.list_items(_HOLDERS, f'{fund_id}:')
        }
        snapshot = {
            'fund_id': fund_id,
            'period': period,
            'cutoff_ms': cutoff_ms,
            'captured_ms': now_ms,
            'wallet_units': fund['wallet_units'],
            'position_value_units': fund['position_value_units'],
            'quote_ms': fund['quote_ms'],
            'updated_ms': fund['updated_ms'],
            'pending_receipts_units': fund['pending_receipts_units'],
            'fee_balance_units': fund['fee_balance_units'],
            'unclaimed_units': fund['unclaimed_units'],
            'liabilities_units': fund['liabilities_units'],
            'shares_atoms': fund['shares_atoms'],
            'realized_profit_units': fund['realized_profit_units'],
            'benchmark_nav': fund['benchmark_nav'],
            'capital_flows_units': fund.get('capital_flows_units', 0),
            'non_trading_income_units': fund.get('non_trading_income_units', 0),
            'period_trade_income_units': fund.get('period_trade_income_units', 0),
            'period_start_nav': fund.get('period_start_nav', '1'),
            'holders': holders,
        }
        self.store.put(_CUTOFFS, snapshot_key, snapshot)
        fund['last_cutoff_period'] = period
        self._save_fund(fund)

    def _refresh_cutoff_valuation(self, snapshot_key: str, snapshot: dict, fund: dict,
                                  cutoff_ms: int) -> None:
        """Upgrade a missing/stale frozen valuation from a pre-cutoff mark only.

        The ledger fields and holder list stay exactly as frozen at the cutoff; only
        the account marks are replaced, and only by a mark whose quote is itself at
        or before the cutoff.  A later (post-cutoff) price can never impersonate the
        cutoff price, so an unusable period just stays pending.
        """
        quote = int(snapshot.get('quote_ms', 0))
        if 0 < quote <= cutoff_ms and cutoff_ms - quote <= self.max_quote_age_ms:
            return
        fresh = int(fund.get('quote_ms', 0))
        if not quote < fresh <= cutoff_ms:
            return
        if cutoff_ms - fresh > self.max_quote_age_ms:
            return
        for field in ('wallet_units', 'position_value_units', 'quote_ms', 'updated_ms'):
            snapshot[field] = fund[field]
        snapshot['refreshed_ms'] = fund.get('updated_ms', 0)
        self.store.put(_CUTOFFS, snapshot_key, snapshot)

    def _ensure_emergency_snapshot(self, fund_id: str, now_ms: int) -> None:
        """Freeze eligible holder/account marks as of each due batch's 20:00 price.

        Mirrors the month-end freeze: the first write *after* a batch's valuation
        captures the book the 20:00 batch price must be applied to, so a later mark
        (for example a 22:00 quote) can neither re-price the batch nor move the
        eligible holders.  A mark exactly at 20:00 is the batch price itself and is
        still allowed to settle in the same instant.
        """
        due: dict[tuple, int] = {}
        for _, value in self.store.list_items(_REDEMPTIONS, f'{fund_id}:'):
            if value['kind'] != 'emergency' or value['status'] != 'pending':
                continue
            ready_ms = int(value.get('ready_ms') or 0)
            if ready_ms and now_ms > ready_ms:
                due[(value.get('batch'), ready_ms)] = ready_ms
        if not due:
            return
        fund = None
        holders = None
        for (batch, _ready_ms), valuation_ms in due.items():
            snapshot_key = f'{fund_id}:E:{batch}'
            if self.store.get(_CUTOFFS, snapshot_key) is not None:
                continue
            if fund is None:
                fund = self._fund(fund_id)
                holders = {
                    key.split(':', 1)[1]: value['shares_atoms']
                    for key, value in self.store.list_items(_HOLDERS, f'{fund_id}:')
                }
            snapshot = {
                'fund_id': fund_id,
                'batch': batch,
                'cutoff_ms': valuation_ms - 2 * 60 * 60 * 1000,
                'valuation_ms': valuation_ms,
                'captured_ms': now_ms,
                'wallet_units': fund['wallet_units'],
                'position_value_units': fund['position_value_units'],
                'quote_ms': fund['quote_ms'],
                'updated_ms': fund['updated_ms'],
                'pending_receipts_units': fund['pending_receipts_units'],
                'fee_balance_units': fund['fee_balance_units'],
                'unclaimed_units': fund['unclaimed_units'],
                'liabilities_units': fund['liabilities_units'],
                'shares_atoms': fund['shares_atoms'],
                'realized_profit_units': fund['realized_profit_units'],
                'benchmark_nav': fund['benchmark_nav'],
                'holders': dict(holders),
            }
            self.store.put(_CUTOFFS, snapshot_key, snapshot)

    def _emergency_view(self, fund: dict, batch: str) -> dict:
        """The frozen 20:00 book for ``batch``, or the live book when none exists."""
        snapshot = self.store.get(_CUTOFFS, f"{fund['fund_id']}:E:{batch}")
        if snapshot is not None:
            return snapshot
        view = dict(fund)
        view['holders'] = {
            value['user_id']: value['shares_atoms']
            for _, value in self.store.list_items(_HOLDERS, f"{fund['fund_id']}:")
        }
        return view

    def _carry_emergency(self, req: dict, now_ms: int) -> None:
        """Re-price a deferred emergency request in the next 20:00 batch.

        The carried (unexecuted) shares get the next batch's 18:00 cutoff as their
        new cancellation deadline, so a holder who was simply rolled forward can
        still withdraw before that batch is priced.  Executed shares/payouts are
        untouched.  The request is no longer "liquidity deferred": it has a fresh,
        ordinary deadline of its own.
        """
        batch, cutoff_ms, valuation_ms = _emergency_batch(now_ms)
        req['batch'] = batch
        req['ready_ms'] = valuation_ms
        req['deadline_ms'] = cutoff_ms
        req['status'] = 'pending'
        req['carried'] = True
        req.pop('liquidity_deferred', None)
        self.store.put(_REDEMPTIONS, req['id'], req)

    def _mark_liquidity_deferred(self, req: dict, now_ms: int) -> None:
        """Flag an unexecuted emergency request as deferred for lack of liquidity.

        The batch keeps its frozen 20:00 valuation and retries once the trader has
        raised cash, but nothing was executed.  The draft lets holders reclaim the
        unexecuted part -- no shares are cancelled and no fee is charged -- so the
        flag makes the reservation releasable through :meth:`cancel_order` even
        after the batch's original deadline.  A confirmed payout is never marked.
        """
        req['liquidity_deferred'] = True
        req['deferred_ms'] = now_ms
        self.store.put(_REDEMPTIONS, req['id'], req)

    def _period_pending_valuation(self, fund_id: str, period: object) -> bool:
        """Whether ``period``'s settlement is persisted as pending valuation.

        A month whose cutoff price could not be trusted is frozen in
        ``pending_valuation``; nothing in it has been executed.  That is the
        narrow condition under which a stale application deadline must not lock
        an investor's unexecuted request forever.
        """
        if not isinstance(period, str) or not period:
            return False
        record = self.store.get(_PERIODS, f'{fund_id}:{period}')
        return bool(record) and record.get('status') == 'pending_valuation'

    # ------------------------------------------------------------------- public
    def status(self, fund_id: str, user_id: str | None = None) -> dict:
        fund = self._fund(fund_id)
        policy = self._policy(fund_id)
        equity = self._equity(fund)
        shares_atoms = fund['shares_atoms']
        nav = _nav_from(equity, shares_atoms)
        out = {
            'fund_id': fund_id,
            'label': fund['label'],
            'state': fund['state'],
            'equity_units': equity,
            'wallet_units': fund['wallet_units'],
            'position_value_units': fund['position_value_units'],
            'available_cash_units': self._available_cash(fund),
            'shares_atoms': shares_atoms,
            'nav': _nav_text(nav),
            'realized_profit_units': fund['realized_profit_units'],
            'benchmark_nav': _nav_text(Decimal(str(fund['benchmark_nav']))),
            'fee_balance_units': fund['fee_balance_units'],
            'pending_receipts_units': fund['pending_receipts_units'],
            'liabilities_units': fund['liabilities_units'],
            'unclaimed_units': fund['unclaimed_units'],
            'capital_flows_units': fund['capital_flows_units'],
            'non_trading_income_units': fund.get('non_trading_income_units', 0),
            'period_trade_income_units': fund.get('period_trade_income_units', 0),
            'period_start_nav': _nav_text(Decimal(str(fund.get('period_start_nav', '1')))),
            'seeded_units': fund.get('seeded_units', 0),
            'updated_ms': fund['updated_ms'],
            'quote_ms': fund['quote_ms'],
            'policy': policy.public(),
        }
        if user_id is not None:
            holder = self._holder(fund_id, user_id)
            out['user_shares_atoms'] = holder['shares_atoms']
            out['user_value_units'] = _value_units(holder['shares_atoms'], nav)
        return out

    def mark_account(self, fund_id: str, wallet_units, position_value_units,
                     now_ms, quote_ms) -> dict:
        wallet = _as_units(wallet_units)
        position = _as_units(position_value_units)
        now = _as_ms(now_ms)
        quote = _as_ms(quote_ms)
        if quote > now + 1000:
            raise FundError('invalid_valuation')
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            fund = self._fund(fund_id)
            if fund['updated_ms'] and now < fund['updated_ms']:
                raise FundError('stale_valuation')
            fund['wallet_units'] = wallet
            fund['position_value_units'] = position
            fund['quote_ms'] = quote
            fund['updated_ms'] = now
            self._save_fund(fund)
            return self.status(fund_id)

    # -------------------------------------------------------------- subscription
    def create_subscription(self, fund_id: str, user_id: str, principal_units,
                            message_key: str, now_ms) -> dict:
        policy = self._policy(fund_id)
        principal = _as_units(principal_units, positive=True)
        if not message_key:
            raise FundError('invalid_key')
        now = _as_ms(now_ms)
        fee = int(Decimal(principal) * Decimal(str(policy.subscription_fee)))
        total = principal + fee
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            idem_key = f'sub:{fund_id}:{message_key}'
            existing_id = self.store.get(_MSG, idem_key)
            if existing_id:
                record = self.store.get(_SUBS, existing_id)
                if record is not None:
                    return record
            fund = self._fund(fund_id)
            seq = self._next_seq(fund)
            sub_id = f'{fund_id}:{uuid.uuid4().hex[:12]}'
            note = f'{fund_id}-{seq:08d}'[:30]
            sub_period, _payment_deadline = _ordinary_period(now)
            record = {
                'id': sub_id,
                'fund_id': fund_id,
                'user_id': user_id,
                'principal_units': principal,
                'fee_units': fee,
                'total_units': total,
                'payment_note': note,
                'expires_ms': now + _SUB_TTL_MS,
                # The period the order applies to at creation; a payment that
                # arrives after this period's window rolls issuance to the next
                # period (see receive_transfer).
                'period': sub_period,
                # The order may be cancelled until its shares are actually issued
                # (the month's application deadline), not merely until payment.
                'deadline_ms': _period_window_ms(sub_period),
                'status': 'pending',
                'message_key': message_key,
                'created_ms': now,
                'occurred_ms': None,
            }
            self.store.put(_SUBS, sub_id, record)
            self.store.put(_MSG, idem_key, sub_id)
            self._save_fund(fund)
            return record

    def expire_subscriptions(self, now_ms) -> list[dict]:
        now = _as_ms(now_ms)
        expired = []
        with self.store.transaction():
            for key, sub in self.store.list_items(_SUBS):
                if sub['status'] == 'pending' and sub['expires_ms'] < now:
                    sub['status'] = 'expired'
                    self.store.put(_SUBS, key, sub)
                    expired.append(sub)
        return expired

    def receive_transfer(self, fund_id: str, tx: dict, now_ms) -> dict:
        self._policy(fund_id)
        now = _as_ms(now_ms)
        if not isinstance(tx, dict):
            raise FundError('invalid_transfer')
        try:
            transfer_id = str(tx['transfer_id'])
            from_user_id = str(tx['from_user_id'])
            amount = _as_units(tx['amount_units'])
            note = str(tx.get('note') or '')
            occurred = _as_ms(tx['occurred_ms'])
        except (KeyError, TypeError):
            raise FundError('invalid_transfer') from None
        if not transfer_id:
            raise FundError('invalid_transfer')

        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            prior = self.store.get(_TRANSFERS, transfer_id)
            if prior is not None:
                prior = dict(prior)
                prior['duplicated'] = True
                return prior

            fund = self._fund(fund_id)
            match = None
            for _, sub in self.store.list_items(_SUBS, f'{fund_id}:'):
                # 'expired' is still matchable: a payment that arrived in time but was
                # only discovered after the local expiry must not be wrongly refunded.
                if (sub['status'] in ('pending', 'expired') and sub['user_id'] == from_user_id
                        and sub['payment_note'] == note and sub['total_units'] == amount):
                    match = sub
                    break

            result = {
                'transfer_id': transfer_id,
                'fund_id': fund_id,
                'from_user_id': from_user_id,
                'amount_units': amount,
                'note': note,
                'occurred_ms': occurred,
                'subscription_id': None,
                'status': 'unclaimed',
                'refund_payout_id': None,
            }

            if match is None:
                # Wrong amount / note / unknown payer: hold as an unclaimed liability.
                fund['unclaimed_units'] += amount
                self._save_fund(fund)
                unclaimed_id = uuid.uuid4().hex[:16]
                self.store.put(_UNCLAIMED, unclaimed_id, {
                    'id': unclaimed_id,
                    'fund_id': fund_id,
                    'from_user_id': from_user_id,
                    'amount_units': amount,
                    'note': note,
                    'occurred_ms': occurred,
                    'status': 'unclaimed',
                    'created_ms': now,
                })
                self._notify(fund_id, from_user_id,
                             f'到账 {money_text(amount)} 小鱼干 未能匹配有效订单，已记为未认领款待人工核对', now)
                result['status'] = 'unclaimed'
            elif occurred > match['expires_ms']:
                # Timely payment is judged by the authoritative upstream arrival time.
                match['status'] = 'refunded'
                match['occurred_ms'] = occurred
                self.store.put(_SUBS, match['id'], match)
                payout = self._add_payout(fund, from_user_id, amount,
                                          f'{note} expired refund', 'refund', now)
                self._save_fund(fund)
                result['subscription_id'] = match['id']
                result['status'] = 'refunded'
                result['refund_payout_id'] = payout['id']
                self._notify(fund_id, from_user_id,
                             f'订单 {note} 超时到账，已按原路全额退款 {money_text(amount)} 小鱼干', now)
            else:
                # Issuance follows the authoritative arrival time, not the discovery
                # time.  A payment that arrives after this month's 25th 18:00
                # payment deadline is issued in the *next* period even though it
                # arrived within the QR's 180-second validity.  Keep whichever is
                # later -- the order's own period or the arrival period -- and give
                # the order that period's own cancellation deadline, so an unissued
                # order stays withdrawable (full principal + fee, no shares).
                application_period = match.get('period') or _period_from_ms(
                    int(match.get('deadline_ms') or occurred) - 1)
                arrival_period = _ordinary_period(occurred)[0]
                issue_period = max(application_period, arrival_period)
                rolled = issue_period != application_period
                match['status'] = 'received'
                match['occurred_ms'] = occurred
                match['received_ms'] = now
                match['period'] = issue_period
                match['deadline_ms'] = _period_window_ms(issue_period)
                if rolled:
                    match['rollover_notice_period'] = issue_period
                self.store.put(_SUBS, match['id'], match)
                fund['pending_receipts_units'] += match['principal_units']
                fund['fee_balance_units'] += match['fee_units']
                # A receipt is not yet a capital flow: it only becomes one when the
                # shares are actually issued (see settle_month), so NAV-normalising
                # flows stay aligned with equity.
                self._save_fund(fund)
                result['subscription_id'] = match['id']
                result['status'] = 'received'
                if rolled:
                    # One durable rollover notice per order; nothing is issued here
                    # and a later cancellation refunds principal + fee in full.
                    self._notify(
                        fund_id, from_user_id,
                        f'订单 {note} 已到账，但到账时间已过本月申购截止时点，'
                        f'份额将于 {issue_period} 月末估值后确认；在此之前仍可撤回，'
                        '撤回时净本金与申购服务费全额返还。', now,
                        notice_id=f'sub-rollover:{fund_id}:{match["id"]}')
                else:
                    self._notify(fund_id, from_user_id,
                                 f'订单 {note} 已到账，将于月末估值后确认份额', now)

            self.store.put(_TRANSFERS, transfer_id, result)
            return result

    # ---------------------------------------------------------------- redemption
    def request_redemption(self, fund_id: str, user_id: str, amount_units, kind: str,
                           message_key: str, now_ms, *, exempt: bool = False) -> dict:
        self._policy(fund_id)
        if kind not in ('ordinary', 'emergency'):
            raise FundError('invalid_kind')
        amount = _as_units(amount_units, positive=True)
        if not message_key:
            raise FundError('invalid_key')
        now = _as_ms(now_ms)
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            idem_key = f'red:{fund_id}:{message_key}'
            existing_id = self.store.get(_MSG, idem_key)
            if existing_id:
                record = self.store.get(_REDEMPTIONS, existing_id)
                if record is not None:
                    return record

            fund = self._fund(fund_id)
            nav, _ = self._require_nav(fund, now)
            holder = self._holder(fund_id, user_id)
            free = holder['shares_atoms'] - holder['reserved_atoms']
            shares = _shares_for(amount, nav)
            if shares <= 0 or shares > free:
                raise FundError('insufficient_shares')

            if kind == 'ordinary':
                period, deadline_ms = _ordinary_period(now)
                batch = period
                ready_ms = None
            else:
                batch, deadline_ms, valuation_ms = _emergency_batch(now)
                period = None
                ready_ms = valuation_ms

            red_id = f'{fund_id}:{uuid.uuid4().hex[:12]}'
            record = {
                'id': red_id,
                'fund_id': fund_id,
                'user_id': user_id,
                'kind': kind,
                'amount_units': amount,
                'nav_used': _nav_text(nav),
                'shares_reserved_atoms': shares,
                'shares_confirmed_atoms': 0,
                'period': period,
                'batch': batch,
                'deadline_ms': deadline_ms,
                'ready_ms': ready_ms,
                'status': 'pending',
                'exempt': bool(exempt),
                'message_key': message_key,
                'created_ms': now,
            }
            holder['reserved_atoms'] += shares
            self._save_holder(holder)
            self.store.put(_REDEMPTIONS, red_id, record)
            self.store.put(_MSG, idem_key, red_id)
            self._save_fund(fund)
            return record

    # ---------------------------------------------------------------------- seed
    def seed(self, fund_id: str, user_id: str, principal_units, now_ms) -> dict:
        self._policy(fund_id)
        principal = _as_units(principal_units, positive=True)
        now = _as_ms(now_ms)
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            fund = self._fund(fund_id)
            seeded = int(fund.get('seeded_units', 0))
            if principal > self._available_cash(fund) - seeded:
                # Never invent capital: only cash that has not already been allocated
                # to shareholders may be seeded.  A repeat seed round cannot re-mint
                # the same wallet cash that earlier rounds already turned into shares
                # (later contributions go through ordinary subscriptions instead).
                raise FundError('insufficient_liquidity')
            shares_atoms = fund['shares_atoms']
            if shares_atoms <= 0:
                nav = Decimal('1')
            else:
                equity_ex = self._equity(fund) - principal
                if equity_ex <= 0:
                    raise FundError('invalid_valuation')
                nav = _nav_from(equity_ex, shares_atoms)
            shares = _shares_for(principal, nav)

            holder = self._holder(fund_id, user_id)
            holder['shares_atoms'] += shares
            self._save_holder(holder)
            fund['shares_atoms'] += shares
            fund['capital_flows_units'] += principal
            fund['seeded_units'] = seeded + principal
            if fund['state'] == 'created':
                fund['state'] = 'active'
            seed_id = f'{fund_id}:{uuid.uuid4().hex[:12]}'
            record = {
                'id': seed_id,
                'fund_id': fund_id,
                'user_id': user_id,
                'principal_units': principal,
                'internal_fee_units': 0,
                'shares_atoms': shares,
                'nav': _nav_text(nav),
                'created_ms': now,
            }
            self.store.put(_SEEDS, seed_id, record)
            self._save_fund(fund)
            return record

    # ------------------------------------------------------------------- trading
    def trade_realized(self, fund_id: str, trade_id: str, pnl_units, now_ms) -> dict:
        self._policy(fund_id)
        pnl = _signed_units(pnl_units)
        now = _as_ms(now_ms)
        if not trade_id:
            raise FundError('invalid_key')
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            key = f'{fund_id}:{trade_id}'
            if not self.store.claim(_TRADES, key, {
                'fund_id': fund_id, 'trade_id': trade_id, 'pnl_units': pnl, 'created_ms': now,
            }):
                return self.store.get(_TRADES, key)
            fund = self._fund(fund_id)
            fund['realized_profit_units'] += pnl
            fund['period_trade_income_units'] = (
                fund.get('period_trade_income_units', 0) + pnl)
            self._save_fund(fund)
            return self.store.get(_TRADES, key)

    # ------------------------------------------------------------- dividend choice
    def set_dividend_choice(self, fund_id: str, user_id: str, reinvest_fraction,
                            now_ms=None) -> dict:
        """Record a holder's reinvestment preference for the next eligible period.

        The choice window closes on the 25th at 18:00 Beijing time, the same
        deadline as an ordinary application: a submission on or before it governs
        the current month, and a later one only takes effect from the following
        month.  Each revision is stored together with the period it governs (and
        stays effective until the next eligible revision), so settling a named
        period always uses the last preference that was already in force for that
        period -- a later choice can never rewrite it.  ``now_ms`` defaults to the
        fund's ``updated_ms`` so callers that do not have a clock remain
        deterministic.
        """
        self._policy(fund_id)
        fraction = _as_fraction(reinvest_fraction)
        with self.store.transaction():
            fund = self._fund(fund_id)
            now = _as_ms(now_ms) if now_ms is not None else int(fund.get('updated_ms') or 0)
            effective_period = _ordinary_period(now)[0]
            key = f'{fund_id}:{user_id}'
            record = self.store.get('dividend_choices', key)
            if record is None:
                record = {'fund_id': fund_id, 'user_id': user_id}
            revisions = list(record.get('revisions') or [])
            if revisions:
                last_period = str(revisions[-1].get('effective_period') or '')
                # Time only moves forward: never let an out-of-order revision claim
                # a period that an earlier submission already governs.
                if last_period and effective_period < last_period:
                    effective_period = last_period
            entry = {
                'reinvest_fraction': format(fraction, 'f'),
                'effective_period': effective_period,
                'recorded_ms': now,
            }
            if revisions and revisions[-1].get('effective_period') == effective_period:
                revisions[-1] = entry  # same period: the latest value wins
            else:
                revisions.append(entry)
            record.update({
                'fund_id': fund_id,
                'user_id': user_id,
                'reinvest_fraction': format(fraction, 'f'),
                'effective_period': effective_period,
                'updated_ms': now,
                'revisions': revisions,
            })
            self.store.put('dividend_choices', key, record)
            return record

    def _dividend_fraction(self, fund_id: str, user_id: str,
                           period: str | None = None) -> Decimal:
        """The reinvest fraction a holder chose for ``period`` (cash ``0`` if none).

        Settlement selects the most recent revision whose ``effective_period`` is
        at or before the settled period, so a preference set after that period's
        deadline is never applied to it.  A legacy single-value record predates
        period tracking and keeps its old period-agnostic meaning.
        """
        record = self.store.get('dividend_choices', f'{fund_id}:{user_id}')
        if not record:
            return Decimal('0')
        revisions = record.get('revisions')
        if not revisions:
            return Decimal(str(record.get('reinvest_fraction', '0')))
        chosen = None
        for revision in revisions:
            effective = revision.get('effective_period')
            if effective is None:
                continue
            if period is None or effective <= period:
                chosen = revision
        if chosen is None:
            return Decimal('0')
        return Decimal(str(chosen['reinvest_fraction']))

    # ------------------------------------------------------------------ month end
    def settle_month(self, fund_id: str, period: str, now_ms) -> dict:
        policy = self._policy(fund_id)
        cutoff = _period_cutoff_ms(period)
        now = _as_ms(now_ms)
        if now < cutoff:
            raise FundError('month_not_ended')  # no month-end look-ahead
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            key = f'{fund_id}:{period}'
            existing = self.store.get(_PERIODS, key)
            if existing and existing.get('status') == 'settled':
                return existing

            snapshot = self.store.get(_CUTOFFS, key)
            if snapshot is None:
                return self._pending_period(fund_id, period, now, 'missing_snapshot')
            quote_ms = snapshot['quote_ms']
            if quote_ms <= 0 or quote_ms > cutoff or cutoff - quote_ms > self.max_quote_age_ms:
                return self._pending_period(fund_id, period, now, 'stale_valuation')

            fund = self._fund(fund_id)
            S = snapshot['shares_atoms']
            window_ms = _period_window_ms(period)
            if S <= 0:
                # A fund with no shares yet must still issue its timely received
                # subscriptions at the initial NAV of 1 instead of silently closing
                # the period as ``no_shares`` and stranding the receipts.
                return self._settle_first_issuance(fund_id, period, now, snapshot, key)

            equity = self._snap_equity(snapshot)
            available = self._snap_cash(snapshot)
            nav_before = _nav_from(equity, S)
            benchmark_before = Decimal(str(snapshot['benchmark_nav']))
            realized = snapshot['realized_profit_units']
            frac = Decimal(str(policy.dividend_fraction))
            period_start_nav = Decimal(str(snapshot.get('period_start_nav', '1')))
            # ``R`` and ``H`` alone must not pay out a losing month: the month's own
            # investment profit has to be positive as well.
            month_profit = nav_before - period_start_nav

            # -------- ordinary redemption confirmations under the 20% window cap
            requests = [
                value for _, value in self.store.list_items(_REDEMPTIONS, f'{fund_id}:')
                if value['kind'] == 'ordinary' and value['period'] == period
                and value['status'] == 'pending'
            ]
            total_requested = sum(r['shares_reserved_atoms'] for r in requests)
            cap_atoms = int(Decimal(S) * Decimal(str(policy.monthly_redemption_fraction)))
            factor = Decimal(1) if total_requested <= cap_atoms or total_requested == 0 else \
                (Decimal(cap_atoms) / Decimal(total_requested))
            confirmed: dict[str, int] = {}
            for req in requests:
                atoms = int(Decimal(req['shares_reserved_atoms']) * factor) if factor < 1 else req['shares_reserved_atoms']
                confirmed[req['id']] = max(0, atoms)

            # -------- dividend sizing
            if month_profit > 0 and nav_before > benchmark_before:
                distributable_cap = _value_units(S, nav_before - benchmark_before)
            else:
                distributable_cap = 0
            distributable = max(0, min(realized, distributable_cap))
            planned_dividend = int(Decimal(distributable) * frac)
            dividend_deferred_for_cash = False

            def redemption_value(exdiv_nav: Decimal) -> int:
                return sum(_value_units(atoms, exdiv_nav) for atoms in confirmed.values())

            exdiv_nav = nav_before - (Decimal(planned_dividend) * MONEY_SCALE / Decimal(S))
            if available < planned_dividend + redemption_value(exdiv_nav):
                # Cash cannot cover both the full dividend and confirmed redemptions.
                # The month is otherwise eligible, so record why the dividend is
                # deferred instead of silently dropping it.
                dividend_deferred_for_cash = planned_dividend > 0
                planned_dividend = 0
                exdiv_nav = nav_before
            if dividend_deferred_for_cash:
                self._notify(
                    fund_id, None,
                    f'{period} 已满足分红评估条件，但现金留存不足以同时支付本次分红与'
                    '已确认赎回；本月暂缓分红，下月重新评估，不承诺具体付款日期。',
                    now, notice_id=f'settle-dividend-short:{fund_id}:{period}')

            # -------- per-holder dividend and reinvestment allocation
            per_share = Decimal(planned_dividend) * MONEY_SCALE / Decimal(S)
            holder_dividend: dict[str, int] = {}
            for user_id, atoms in snapshot['holders'].items():
                holder_dividend[user_id] = _value_units(atoms, per_share)
            declared_total = sum(holder_dividend.values())

            confirmed_by_user: dict[str, int] = {}
            for req in requests:
                atoms = confirmed[req['id']]
                if atoms:
                    confirmed_by_user[req['user_id']] = confirmed_by_user.get(req['user_id'], 0) + atoms
            redeem_value_total = redemption_value(exdiv_nav)

            # Liquidate redemptions if the cash after dividend is still insufficient.
            cash_scaled_ids: set[str] = set()
            if redemption_value(exdiv_nav) > available - declared_total:
                budget = max(0, available - declared_total)
                need = redemption_value(exdiv_nav)
                scale = Decimal(budget) / Decimal(need) if need else Decimal(0)
                pre_scale = dict(confirmed)
                for red_id in list(confirmed):
                    confirmed[red_id] = int(Decimal(confirmed[red_id]) * scale)
                cash_scaled_ids = {red_id for red_id, atoms in confirmed.items()
                                   if atoms < pre_scale.get(red_id, 0)}
                confirmed_by_user = {}
                for req in requests:
                    atoms = confirmed[req['id']]
                    if atoms:
                        confirmed_by_user[req['user_id']] = confirmed_by_user.get(req['user_id'], 0) + atoms
                redeem_value_total = redemption_value(exdiv_nav)

            redeeming_total = sum(confirmed.values())

            # -------- apply share movements
            cash_dividend_total = 0
            reinvest_value_total = 0
            issued_external = 0
            issued_reinvest = 0

            # external subscriptions received before the period's payment deadline
            # are issued first; later arrivals roll to the next period
            issued_subs = []
            for sub_key, sub in self.store.list_items(_SUBS, f'{fund_id}:'):
                if (sub['status'] == 'received'
                        and (sub['occurred_ms'] or 0) <= window_ms):
                    shares = _shares_for(sub['principal_units'], exdiv_nav)
                    holder = self._holder(fund_id, sub['user_id'])
                    holder['shares_atoms'] += shares
                    self._save_holder(holder)
                    fund['pending_receipts_units'] -= sub['principal_units']
                    # The receipt becomes an external capital flow only now, when the
                    # principal actually enters equity as issued shares.
                    fund['capital_flows_units'] += sub['principal_units']
                    issued_external += shares
                    sub['status'] = 'issued'
                    sub['issued_shares_atoms'] = shares
                    self.store.put(_SUBS, sub_key, sub)
                    issued_subs.append({'subscription_id': sub['id'], 'user_id': sub['user_id'],
                                        'shares_atoms': shares,
                                        'principal_units': sub['principal_units']})

            # redemptions: remove shares, clear reservations, create payouts
            next_p = _next_period(period)
            redemption_allocations = []
            for req in requests:
                atoms = confirmed[req['id']]
                holder = self._holder(fund_id, req['user_id'])
                reserved = holder['reserved_atoms']
                deferred = req['shares_reserved_atoms'] - atoms
                # keep the reservation for the carried (deferred) portion
                holder['reserved_atoms'] = max(0, reserved - atoms)
                if atoms:
                    holder['shares_atoms'] -= atoms
                    fund['shares_atoms'] -= atoms
                    payout = self._add_payout(fund, req['user_id'], _value_units(atoms, exdiv_nav),
                                              f'{req["kind"]} redemption {period}', 'redemption', now)
                    req['status'] = 'confirmed'
                    # Confirmed slices accumulate: a later period must never erase
                    # the shares/payout already executed in an earlier one.
                    req['shares_confirmed_atoms'] = req.get('shares_confirmed_atoms', 0) + atoms
                    req['payout_ids'] = list(req.get('payout_ids') or []) + [payout['id']]
                    req['payout_id'] = payout['id']
                    req['nav_used'] = _nav_text(exdiv_nav)
                    req['settled_ms'] = now
                    redemption_allocations.append({
                        'redemption_id': req['id'], 'user_id': req['user_id'],
                        'shares_atoms': atoms, 'payout_id': payout['id'],
                    })
                else:
                    req['shares_confirmed_atoms'] = req.get('shares_confirmed_atoms', 0)
                if deferred:
                    # Remaining shares carry into the next period at the next NAV,
                    # under *that* period's own 25th 18:00 application deadline, so
                    # the holder can still withdraw the unconfirmed remainder.
                    req['status'] = 'pending'
                    req['period'] = next_p
                    req['batch'] = next_p
                    req['deadline_ms'] = _period_window_ms(next_p)
                    req['deferred_shares_atoms'] = deferred
                    req['shares_reserved_atoms'] = deferred
                    req['carried'] = True
                    if req['id'] in cash_scaled_ids:
                        # Cash, not the 20% window, cut this request down: the
                        # holder is told the actual confirmation and that the rest
                        # rolls to the next period (no fee, no payment date).
                        req['cash_scaled'] = True
                        self._notify(
                            fund_id, req['user_id'],
                            f'{period} 普通赎回因当月可用现金不足以支付全部确认额度，'
                            f'本次按可用现金确认 {_shares_text(atoms)} 份额，'
                            f'其余 {_shares_text(deferred)} 份额顺延至 {next_p} 批次，'
                            '未确认部分不注销、不计费。', now,
                            notice_id=f'settle-redeem-short:{fund_id}:{period}:{req["id"]}')
                self._save_holder(holder)
                self.store.put(_REDEMPTIONS, req['id'], req)

            # dividends: cash liability + optional same-fund reinvestment at ex-div NAV
            holder_allocations = []
            for user_id, div_units in holder_dividend.items():
                if div_units <= 0:
                    continue
                holder = self._holder(fund_id, user_id)
                retained = holder['shares_atoms']
                choice = self._dividend_fraction(fund_id, user_id, period)
                reinvestable = _value_units(retained, per_share)
                reinvest_units = int(Decimal(min(div_units, reinvestable)) * choice)
                if reinvest_units > 0:
                    shares = _shares_for(reinvest_units, exdiv_nav)
                    holder['shares_atoms'] += shares
                    self._save_holder(holder)
                    issued_reinvest += shares
                    reinvest_value_total += reinvest_units
                cash_units = div_units - reinvest_units
                if cash_units > 0:
                    payout = self._add_payout(fund, user_id, cash_units,
                                              f'dividend {period}', 'dividend', now)
                    payout_id = payout['id']
                    cash_dividend_total += cash_units
                else:
                    payout_id = None
                holder_allocations.append({
                    'user_id': user_id,
                    'dividend_units': div_units,
                    'reinvest_units': reinvest_units,
                    'cash_units': cash_units,
                    'reinvest_shares_atoms': _shares_for(reinvest_units, exdiv_nav) if reinvest_units else 0,
                    'payout_id': payout_id,
                })

            # -------- ledger deltas on the live fund record
            reduction = realized - int(
                (Decimal(realized - declared_total) * Decimal(S - redeeming_total) / Decimal(S))
            ) if S > 0 else 0
            fund['realized_profit_units'] -= reduction
            fund['shares_atoms'] += issued_external + issued_reinvest
            if declared_total > 0:
                fund['benchmark_nav'] = _nav_text(exdiv_nav)
            fund['capital_flows_units'] -= redeem_value_total + cash_dividend_total
            # Close the period's trade-income window and start the next period at the
            # post-settlement NAV, so the next month's profit test is computed from
            # that month's own marks rather than the fund's lifetime totals.
            fund['period_trade_income_units'] = (
                fund.get('period_trade_income_units', 0)
                - snapshot.get('period_trade_income_units', 0))
            fund['period_start_nav'] = _nav_text(
                _nav_from(self._equity(fund), fund['shares_atoms']))
            if fund['state'] == 'created':
                fund['state'] = 'active'
            self._save_fund(fund)

            record = self._period_record(fund_id, period, now, snapshot, {
                'status': 'settled',
                'settled': True,
                'settled_ms': now,
                'cutoff_ms': cutoff,
                'nav_before': _nav_text(nav_before),
                'period_start_nav': _nav_text(period_start_nav),
                'month_profit_per_share': _nav_text(month_profit),
                'period_trade_income_units': snapshot.get('period_trade_income_units', 0),
                'benchmark_before': _nav_text(benchmark_before),
                'benchmark_after': _nav_text(Decimal(str(fund['benchmark_nav']))),
                'distributable_units': distributable,
                'dividend_units': declared_total,
                'planned_dividend_units': planned_dividend,
                'dividend_per_share': _nav_text(per_share),
                'exdiv_nav': _nav_text(exdiv_nav),
                'shares_at_cutoff_atoms': S,
                'redeemed_shares_atoms': redeeming_total,
                'redeem_value_units': redeem_value_total,
                'reinvest_value_units': reinvest_value_total,
                'cash_dividend_units': cash_dividend_total,
                'issued_external_shares_atoms': issued_external,
                'issued_reinvest_shares_atoms': issued_reinvest,
                'shares_after_atoms': fund['shares_atoms'],
                'holder_allocations': holder_allocations,
                'redemptions': redemption_allocations,
                'subscriptions_issued': issued_subs,
            })
            self.store.put(_PERIODS, key, record)
            if declared_total > 0:
                self._notify(fund_id, None,
                             f'{period} 分红 {money_text(declared_total)} 小鱼干，除息净值 {record["exdiv_nav"]}', now)
            return record

    def _pending_period(self, fund_id: str, period: str, now: int, reason: str) -> dict:
        """Leave a period pending with one durable notice, retriable later.

        The record is reused across retries (keeping the original notice) and is
        never advanced to ``settled`` while the cutoff valuation is unusable; a
        later genuinely pre-cutoff mark can still let :meth:`settle_month` succeed.
        """
        key = f'{fund_id}:{period}'
        record = self.store.get(_PERIODS, key)
        if record is not None and record.get('status') == 'pending_valuation':
            record['reason'] = reason
            record['retried_ms'] = now
            record['retry_count'] = int(record.get('retry_count', 0)) + 1
            self.store.put(_PERIODS, key, record)
            return record
        record = {
            'id': key,
            'fund_id': fund_id,
            'period': period,
            'status': 'pending_valuation',
            'reason': reason,
            'created_ms': now,
            'retry_count': 0,
        }
        self.store.put(_PERIODS, key, record)
        self._notify(fund_id, None, f'{period} 月末估值无效（{reason}），结算挂起', now)
        return record

    def _period_record(self, fund_id: str, period: str, now: int, snapshot: dict,
                       extra: dict) -> dict:
        record = {
            'id': f'{fund_id}:{period}',
            'fund_id': fund_id,
            'period': period,
            'snapshot_captured_ms': snapshot.get('captured_ms'),
            'created_ms': now,
        }
        record.update(extra)
        return record

    def _settle_first_issuance(self, fund_id: str, period: str, now: int, snapshot: dict,
                               key: str) -> dict:
        """Issue a shareless fund's first timely subscriptions at NAV 1.

        Called when the cutoff snapshot has no outstanding shares.  If timely paid
        subscriptions are waiting they are issued at the initial price of 1 (the
        fund's first capital cannot be priced off a dividend history it does not
        have); only a period with nothing to issue settles as ``no_shares``.
        """
        window_ms = _period_window_ms(period)
        subs = [
            (sub_key, sub) for sub_key, sub in self.store.list_items(_SUBS, f'{fund_id}:')
            if sub['status'] == 'received' and (sub['occurred_ms'] or 0) <= window_ms
        ]
        if not subs:
            record = self._period_record(fund_id, period, now, snapshot, {
                'status': 'settled', 'settled': True, 'reason': 'no_shares',
                'settled_ms': now, 'cutoff_ms': snapshot.get('cutoff_ms'),
            })
            self.store.put(_PERIODS, key, record)
            return record

        fund = self._fund(fund_id)
        nav = Decimal('1')
        issued_external = 0
        issued_subs = []
        for sub_key, sub in subs:
            shares = _shares_for(sub['principal_units'], nav)
            holder = self._holder(fund_id, sub['user_id'])
            holder['shares_atoms'] += shares
            self._save_holder(holder)
            fund['pending_receipts_units'] -= sub['principal_units']
            fund['capital_flows_units'] = (
                fund.get('capital_flows_units', 0) + sub['principal_units'])
            issued_external += shares
            sub['status'] = 'issued'
            sub['issued_shares_atoms'] = shares
            self.store.put(_SUBS, sub_key, sub)
            issued_subs.append({'subscription_id': sub['id'], 'user_id': sub['user_id'],
                                'shares_atoms': shares,
                                'principal_units': sub['principal_units']})
        fund['shares_atoms'] += issued_external
        fund['period_trade_income_units'] = (
            fund.get('period_trade_income_units', 0)
            - snapshot.get('period_trade_income_units', 0))
        fund['period_start_nav'] = _nav_text(
            _nav_from(self._equity(fund), fund['shares_atoms']))
        if fund['state'] == 'created':
            fund['state'] = 'active'
        self._save_fund(fund)
        record = self._period_record(fund_id, period, now, snapshot, {
            'status': 'settled', 'settled': True, 'settled_ms': now,
            'cutoff_ms': snapshot.get('cutoff_ms'),
            'nav_before': '1.00000000', 'exdiv_nav': '1.00000000',
            'period_start_nav': _nav_text(Decimal(str(snapshot.get('period_start_nav', '1')))),
            'dividend_units': 0, 'distributable_units': 0,
            'shares_at_cutoff_atoms': 0, 'shares_after_atoms': fund['shares_atoms'],
            'issued_external_shares_atoms': issued_external,
            'subscriptions_issued': issued_subs,
            'first_issuance': True,
        })
        self.store.put(_PERIODS, key, record)
        self._notify(fund_id, None, f'{period} 首笔申购按净值 1 发行份额', now)
        return record

    # --------------------------------------------------------------- emergency
    def settle_emergency(self, fund_id: str, now_ms, *, exempt: bool = False,
                         allow_partial: bool = False) -> dict:
        """Execute emergency batches whose 20:00 valuation has passed.

        Each batch is priced from the frozen 20:00 book, so a later mark can neither
        re-price it nor change the eligible holders.  When the frozen cash cannot
        cover a batch the whole batch is deferred -- no share is cancelled and no
        fee is charged -- until it can; proportional partial execution only happens
        when the caller explicitly passes ``allow_partial=True`` (investor consent).
        A batch that redeems every outstanding share is a unified liquidation and
        its emergency fee is waived.
        """
        policy = self._policy(fund_id)
        now = _as_ms(now_ms)
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            fund = self._fund(fund_id)
            due = [
                value for _, value in self.store.list_items(_REDEMPTIONS, f'{fund_id}:')
                if value['kind'] == 'emergency' and value['status'] == 'pending'
                and now >= (value.get('ready_ms') or 0)
            ]
            if not due:
                return {'fund_id': fund_id, 'status': 'nothing_due', 'settled_ms': now,
                        'settlements': [], 'deferred': []}

            groups: dict[tuple, list[dict]] = {}
            for req in sorted(due, key=lambda r: r['created_ms']):
                groups.setdefault((req.get('batch'), int(req.get('ready_ms') or 0)), []).append(req)

            state_exempt = fund['state'] in ('liquidating', 'terminated', 'permanent_halt')
            results: list[dict] = []
            deferred: list[dict] = []
            for (batch, valuation_ms), reqs in sorted(groups.items(), key=lambda kv: kv[0][1]):
                cutoff_ms = valuation_ms - 2 * 60 * 60 * 1000
                view = self._emergency_view(fund, batch)
                quote_ms = int(view.get('quote_ms', 0))
                # The batch price must be fresh for *this* batch: at or after its
                # 18:00 cutoff, at or before its 20:00 valuation, and within the
                # freshness window.  A later (e.g. 22:00) quote can therefore never
                # impersonate the 20:00 price.
                if not (cutoff_ms <= quote_ms <= valuation_ms
                        and valuation_ms - quote_ms <= self.max_quote_age_ms):
                    for req in reqs:
                        self._carry_emergency(req, now)
                        self._notify(fund_id, req['user_id'],
                                     '紧急赎回批次估值无效，顺延至下一批次', now)
                        deferred.append(req)
                    continue

                nav = _nav_from(self._snap_equity(view), view['shares_atoms'])
                outstanding = int(view['shares_atoms'])
                holder_at_cutoff = view.get('holders', {})
                plans = []
                for req in reqs:
                    shares = min(int(req['shares_reserved_atoms']),
                                 int(holder_at_cutoff.get(req['user_id'], 0)))
                    if shares <= 0:
                        # Nothing eligible at this batch's frozen registration;
                        # roll to the next batch, which re-captures holders.
                        self._carry_emergency(req, now)
                        self._notify(fund_id, req['user_id'],
                                     '紧急赎回本批次无可执行份额，顺延至下一批次', now)
                        deferred.append(req)
                        continue
                    plans.append({'req': req, 'shares': shares})
                if not plans:
                    continue

                # A batch that takes out every outstanding share is a unified
                # liquidation: no emergency fee may be retained from it.
                unified = outstanding > 0 and sum(p['shares'] for p in plans) >= outstanding
                for plan in plans:
                    plan['exempt'] = bool(unified or exempt or state_exempt
                                          or plan['req'].get('exempt'))
                    plan['gross'] = _value_units(plan['shares'], nav)
                    plan['fee'] = 0 if plan['exempt'] else int(
                        Decimal(plan['gross']) * Decimal(str(policy.emergency_fee)))
                    plan['payout'] = plan['gross'] - plan['fee']

                # Solvency is judged on the *current* spendable cash (the trader
                # raises it before the batch can execute), while price and eligible
                # holders stay frozen at 20:00.
                budget = self._available_cash(fund)
                total_payout = sum(plan['payout'] for plan in plans)
                if total_payout > budget and not allow_partial:
                    # Executing a slice would charge a fee the investor never
                    # consented to; defer the whole batch instead.  Nothing is
                    # executed, so the unexecuted reservation stays releasable.
                    for plan in plans:
                        self._mark_liquidity_deferred(plan['req'], now)
                        self._notify(fund_id, plan['req']['user_id'],
                                     '基金流动性不足，紧急赎回整批顺延：不注销、不计费，待资金充足后执行',
                                     now)
                        deferred.append(plan['req'])
                    continue
                factor = Decimal(1)
                if total_payout > budget:
                    factor = Decimal(budget) / Decimal(total_payout) if total_payout else Decimal(0)

                for plan in plans:
                    req = plan['req']
                    shares = plan['shares'] if factor >= 1 else int(Decimal(plan['shares']) * factor)
                    if shares <= 0:
                        self._mark_liquidity_deferred(req, now)
                        self._notify(fund_id, req['user_id'],
                                     '基金流动性不足，紧急赎回顺延，未执行部分不注销不计费', now)
                        deferred.append(req)
                        continue
                    gross = _value_units(shares, nav)
                    fee = 0 if plan['exempt'] else int(
                        Decimal(gross) * Decimal(str(policy.emergency_fee)))
                    payout_units = gross - fee
                    if payout_units <= 0 or payout_units > budget:
                        self._mark_liquidity_deferred(req, now)
                        self._notify(fund_id, req['user_id'],
                                     '基金流动性不足，紧急赎回顺延，未执行部分不注销不计费', now)
                        deferred.append(req)
                        continue
                    budget -= payout_units

                    holder = self._holder(fund_id, req['user_id'])
                    holder['shares_atoms'] = max(0, holder['shares_atoms'] - shares)
                    holder['reserved_atoms'] = max(0, holder['reserved_atoms'] - shares)
                    self._save_holder(holder)

                    before = fund['shares_atoms']
                    fund['shares_atoms'] = max(0, before - shares)
                    after = fund['shares_atoms']
                    if before > 0:
                        fund['realized_profit_units'] = int(
                            Decimal(fund['realized_profit_units']) * Decimal(after) / Decimal(before))
                    if fee > 0:
                        # The retained fee is non-investment income: lift the benchmark
                        # so its NAV uplift is not mistaken for distributable trading
                        # profit, and track it apart from trading capital flows.
                        if after > 0:
                            uplift = Decimal(fee) * MONEY_SCALE / Decimal(after)
                            fund['benchmark_nav'] = _nav_text(
                                Decimal(str(fund['benchmark_nav'])) + uplift)
                            # Lift the period baseline by the same per-share amount,
                            # so the month's own investment-profit gate also excludes
                            # the retained non-trading income instead of reading the
                            # fee uplift as a positive trading month.
                            fund['period_start_nav'] = _nav_text(
                                Decimal(str(fund.get('period_start_nav', '1'))) + uplift)
                        fund['non_trading_income_units'] = (
                            fund.get('non_trading_income_units', 0) + fee)
                    payout = self._add_payout(fund, req['user_id'], payout_units,
                                              f'emergency redemption {req["batch"]}',
                                              'emergency_redemption', now)
                    # Trading capital leaves at the *gross* value; the retained fee is
                    # reported separately so the trader can normalise by their sum.
                    fund['capital_flows_units'] = (
                        fund.get('capital_flows_units', 0) - gross)

                    remaining = int(req['shares_reserved_atoms']) - shares
                    req['shares_reserved_atoms'] = remaining
                    req['shares_confirmed_atoms'] = req.get('shares_confirmed_atoms', 0) + shares
                    req['fee_units'] = req.get('fee_units', 0) + fee
                    req['payout_units'] = req.get('payout_units', 0) + payout_units
                    req['nav_used'] = _nav_text(nav)
                    req['last_settled_ms'] = now
                    if remaining > 0:
                        self._carry_emergency(req, now)
                    else:
                        req['status'] = 'confirmed'
                        req['payout_id'] = payout['id']
                        req['settled_ms'] = now
                        self.store.put(_REDEMPTIONS, req['id'], req)

                    results.append({'redemption_id': req['id'], 'user_id': req['user_id'],
                                    'shares_atoms': shares, 'gross_units': gross,
                                    'fee_units': fee, 'payout_units': payout_units,
                                    'exempt': plan['exempt'], 'payout_id': payout['id'],
                                    'remaining_shares_atoms': remaining})
                    self._notify(fund_id, req['user_id'],
                                 f'紧急赎回已确认：注销 {_shares_text(shares)} 份额，费用 {money_text(fee)} 小鱼干，实付 {money_text(payout_units)} 小鱼干', now)

            self._save_fund(fund)
            if results:
                self.store.append_event('emergency_settled', fund_id=fund_id,
                                        details={'settlements': results})
            status = 'settled' if results else ('deferred' if deferred else 'nothing_due')
            return {'fund_id': fund_id, 'status': status, 'settled_ms': now,
                    'settlements': results,
                    'deferred': [req['id'] for req in deferred]}

    # ------------------------------------------------------------------- payouts
    def pending_payouts(self, fund_id: str | None = None) -> list[dict]:
        prefix = f'{fund_id}:' if fund_id else ''
        out = []
        for _, value in self.store.list_items(_PAYOUTS, prefix):
            if value['status'] == 'pending':
                out.append(value)
        return sorted(out, key=lambda r: r['created_ms'])

    def mark_payout_paid(self, payout_id: str, transfer_id: str, now_ms) -> dict:
        now = _as_ms(now_ms)
        with self.store.transaction():
            found_key = None
            record = None
            for key, value in self.store.list_items(_PAYOUTS):
                if value['id'] == payout_id:
                    found_key, record = key, value
                    break
            if record is None:
                raise FundError('unknown_payout')
            if record['status'] == 'paid':
                return record
            # Paying a payout moves the wallet/liabilities, so the cutoff books must
            # already be frozen before this write.
            self._ensure_cutoff_snapshot(record['fund_id'], now)
            self._ensure_emergency_snapshot(record['fund_id'], now)
            fund = self._fund(record['fund_id'])
            record['status'] = 'paid'
            record['paid_ms'] = now
            record['transfer_id'] = transfer_id
            fund['liabilities_units'] = max(0, fund['liabilities_units'] - record['amount_units'])
            fund['wallet_units'] = max(0, fund['wallet_units'] - record['amount_units'])
            self.store.put(_PAYOUTS, found_key, record)
            self._save_fund(fund)
            return record

    # -------------------------------------------------------------------- orders
    def orders(self, fund_id: str | None = None, user_id: str | None = None) -> list[dict]:
        prefix = f'{fund_id}:' if fund_id else ''
        out = []
        for _, sub in self.store.list_items(_SUBS, prefix):
            if user_id and sub['user_id'] != user_id:
                continue
            out.append({'order_type': 'subscription', **sub})
        for _, req in self.store.list_items(_REDEMPTIONS, prefix):
            if user_id and req['user_id'] != user_id:
                continue
            out.append({'order_type': 'redemption', **req})
        return sorted(out, key=lambda r: r['created_ms'])

    def cancel_order(self, fund_id: str, user_id: str, order_id: str, now_ms) -> dict:
        """Cancel a pending order before its deadline, atomically.

        Subscriptions that were paid but not yet issued are refunded in full
        (principal + prepaid institution fee).  Redemptions release their reserved
        shares.  Already issued subscriptions, confirmed/payed redemptions and any
        deadline already crossed can never be undone; the call is idempotent for an
        order that is already cancelled.
        """
        self._policy(fund_id)
        now = _as_ms(now_ms)
        if not order_id:
            raise FundError('unknown_order')
        with self.store.transaction():
            self._ensure_cutoff_snapshot(fund_id, now)
            self._ensure_emergency_snapshot(fund_id, now)
            fund = self._fund(fund_id)
            sub = self.store.get(_SUBS, order_id)
            if sub is not None and sub['fund_id'] == fund_id:
                return self._cancel_subscription(fund, sub, user_id, now)
            req = self.store.get(_REDEMPTIONS, order_id)
            if req is not None and req['fund_id'] == fund_id:
                return self._cancel_redemption(fund, req, user_id, now)
            raise FundError('unknown_order')

    def _cancel_subscription(self, fund: dict, sub: dict, user_id: str, now: int) -> dict:
        if sub['user_id'] != user_id:
            raise FundError('not_owner')
        if sub['status'] == 'cancelled':
            return sub
        if sub['status'] in ('issued', 'refunded'):
            raise FundError('order_settled')  # no undo of issued shares / paid refunds
        deadline = int(sub.get('deadline_ms', sub.get('expires_ms', 0)))
        # A received-but-unissued subscription whose month is stuck in
        # ``pending_valuation`` may still be withdrawn: the money never became
        # shares, so refunding principal + fee in full is the only way an
        # unresolved valuation must not lock the request forever.  Issued shares
        # and already-refunded orders stay untouched.
        stuck = (sub['status'] == 'received'
                 and self._period_pending_valuation(fund['fund_id'], sub.get('period')))
        if now > deadline and not stuck:
            raise FundError('deadline_passed')
        if sub['status'] == 'received':
            principal = int(sub['principal_units'])
            fee = int(sub['fee_units'])
            fund['pending_receipts_units'] = max(0, fund['pending_receipts_units'] - principal)
            fund['fee_balance_units'] = max(0, fund['fee_balance_units'] - fee)
            payout = self._add_payout(fund, user_id, principal + fee,
                                      f'{sub["payment_note"]} cancelled refund', 'refund', now)
            sub['refund_payout_id'] = payout['id']
        sub['status'] = 'cancelled'
        sub['cancelled_ms'] = now
        self.store.put(_SUBS, sub['id'], sub)
        self._save_fund(fund)
        self._notify(fund['fund_id'], user_id,
                     f'订单 {sub["payment_note"]} 已取消', now)
        return sub

    def _cancel_redemption(self, fund: dict, req: dict, user_id: str, now: int) -> dict:
        if req['user_id'] != user_id:
            raise FundError('not_owner')
        if req['status'] == 'cancelled':
            return req
        if req['status'] == 'confirmed':
            raise FundError('order_settled')  # no undo of a confirmed payout
        deadline = int(req.get('deadline_ms') or 0)
        # A request whose batch was deferred for lack of liquidity has executed
        # nothing, so the holder may still reclaim the reserved shares even after
        # the batch's original deadline; the draft explicitly allows withdrawing
        # unexecuted deferred parts.  The same holds while the request's month is
        # stuck in ``pending_valuation``: no share of it was confirmed, so the
        # unexecuted reservation must not be locked by an unpriceable month.  An
        # ordinary request in a healthy month (and a request already rolled to a
        # fresh batch, which carries that batch's own deadline) keeps the normal
        # deadline.  Executed shares/payouts are never reversed here: only the
        # still-reserved remainder is released.
        if (not req.get('liquidity_deferred')
                and not self._period_pending_valuation(fund['fund_id'], req.get('period'))
                and (not deadline or now > deadline)):
            raise FundError('deadline_passed')
        holder = self._holder(fund['fund_id'], user_id)
        remaining = int(req.get('shares_reserved_atoms', 0))
        holder['reserved_atoms'] = max(0, holder['reserved_atoms'] - remaining)
        self._save_holder(holder)
        req['status'] = 'cancelled'
        req['cancelled_ms'] = now
        req['shares_reserved_atoms'] = 0
        self.store.put(_REDEMPTIONS, req['id'], req)
        self._save_fund(fund)
        self._notify(fund['fund_id'], user_id,
                     f'{req["kind"]} 赎回订单已取消，预留份额已释放', now)
        return req

    # ----------------------------------------------------------------------- state
    def state(self, fund_id: str, value: str | None = None, now_ms=None):
        """Read or write the fund lifecycle state (``state()`` / ``set_state()``).

        The operator/trader writes the state (for example ``permanent_halt``); the
        ledger only persists it, so an emergency exit can treat a permanently
        halted fund as a unified liquidation and waive its fee.
        """
        self._policy(fund_id)
        if value is None:
            return self._fund(fund_id)['state']
        if not isinstance(value, str) or not value:
            raise FundError('invalid_state')
        now = _as_ms(now_ms) if now_ms is not None else 0
        with self.store.transaction():
            if now:
                self._ensure_cutoff_snapshot(fund_id, now)
                self._ensure_emergency_snapshot(fund_id, now)
            fund = self._fund(fund_id)
            fund['state'] = value
            fund['state_ms'] = now
            self._save_fund(fund)
            self.store.append_event('state_changed', fund_id=fund_id,
                                    details={'state': value}, created_ms=now or None)
            return {'fund_id': fund_id, 'state': value, 'state_ms': now}

    def set_state(self, fund_id: str, value: str, now_ms=None) -> dict:
        """Alias for :meth:`state` used by the trader/operator side."""
        return self.state(fund_id, value, now_ms)

    # ------------------------------------------------------------------- notices
    def notices(self, status: str | None = None, fund_id: str | None = None,
                limit: int = 1000) -> list[dict]:
        out = []
        for _, value in self.store.list_items(_NOTICES):
            if status and value['status'] != status:
                continue
            if fund_id and value['fund_id'] != fund_id:
                continue
            out.append(value)
        return sorted(out, key=lambda r: r['created_ms'])[:limit]

    def mark_notice_sent(self, notice_id: str) -> dict:
        with self.store.transaction():
            record = self.store.get(_NOTICES, notice_id)
            if record is None:
                raise FundError('unknown_notice')
            record['status'] = 'sent'
            self.store.put(_NOTICES, notice_id, record)
            return record

    # --------------------------------------------------------------------- views
    def holder(self, fund_id: str, user_id: str) -> dict:
        self._policy(fund_id)
        return self._holder(fund_id, user_id)


def _signed_units(value: object) -> int:
    """Signed integer amount for realised PnL (may be negative)."""
    if isinstance(value, bool):
        raise FundError('invalid_amount')
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise FundError('invalid_amount')
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if s.lstrip('-').isdigit():
            return int(s)
    raise FundError('invalid_amount')
