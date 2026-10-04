"""Private-chat command handler for one fund (no LLM, fully deterministic).

Five commands are supported — ``/check``, ``/subscription 金额``,
``/redemption 金额``, ``/emergency 金额`` and ``/help``.

Safety rules:
  * identity comes only from ``message.author.id`` on a *private* channel;
  * a message id is claimed durably so a redelivered message is handled once;
  * amounts are parsed strictly (positive, at most 4 decimals) as integer
    1e-4 units — never floats;
  * ordinary ``/check`` reads the caller's holdings; only the configured verified controller sees all fund categories;
  * the reply (and any QR image) is queued in the durable notice outbox before
    any external send, and external sends are gated by ``live`` (default False).
"""
from __future__ import annotations

import inspect

from .contracts import FundError, money_text, money_units, timestamp_text, now_ms as _now_ms
from .payments import NoticeOutbox, get_field, shares_text

HELP_TEXT = (
    '可用命令（私聊）：\n'
    '/check — 查看本人持有的份额与估算价值\n'
    '/subscription 金额 — 申购，返回支付链接与二维码（180 秒内有效）\n'
    '/redemption 金额 — 普通月度赎回申请（免机构服务费）\n'
    '/emergency 金额 — 紧急赎回申请（收取确认总额 10% 费用）\n'
    '/help — 显示本说明\n'
    '申购与普通赎回：每月1日至7日18:00（北京时间，含）；次月7日20:00统一估值结算。\n'
    '截止前撤回：/subscription cancel 订单编号 或 /redemption cancel 订单编号（紧急用 /emergency cancel）。\n'
    '紧急赎回若因流动性不足整批顺延，未执行部分不注销、不计费，仍可在下一批次估值前撤回。\n'
    '金额最多 4 位小数；申请金额均为按当前估值的估算，实际以确认时适用净值为准。'
)

_ERROR_TEXT = {
    'invalid_amount': '金额格式不正确：请输入大于 0、最多 4 位小数的金额。',
    'no_valid_nav': '当前没有可用的有效净值，申请未受理；请稍后再试。',
    'invalid_nav': '当前没有可用的有效净值，申请未受理；请稍后再试。',
    'nav_unavailable': '当前没有可用的有效净值，申请未受理；请稍后再试。',
    'insufficient_shares': '可赎回份额不足，申请未受理。',
    'window_closed': '申购与普通赎回仅在每月1日至7日18:00（北京时间）受理；窗口已关闭，请于次月1日再申请。',
    'account_not_ready': '基金账户尚未就绪，操作未受理。',
    'unknown_order': '未找到对应的订单编号，请核对后重试。',
    'not_owner': '只能操作本人的订单。',
    'order_settled': '该订单已确认或已完成结算，无法撤回。',
    'deadline_passed': '该订单已过可撤回截止时间，无法撤回。',
    'duplicate': '该申请已登记，请勿重复提交。',
}

_ORDINARY = 'ordinary'
_EMERGENCY = 'emergency'

# Durable per-message states. A message is claimed as ``processing`` before any
# ledger call, moves to ``prepared`` only once its reply intent is durably in the
# notice outbox, and to ``completed`` only after the delivery attempt. A restart
# therefore replays unfinished intents (the ledger is idempotent on the message
# key) and never re-runs the ledger for a reply that is already prepared.
_MSG_PROCESSING = 'processing'
_MSG_PREPARED = 'prepared'
_MSG_COMPLETED = 'completed'

_STATE_TEXT = {
    'created': '已创建',
    'active': '运行中',
    'paused': '已暂停',
    'liquidating': '清算中',
    'terminated': '已终止',
    'permanent_halt': '永久停机',
}


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


class CommandHandler:
    """Handle private commands for ``fund_id``.

    ``live=False`` (default) persists every intent and reply but never performs
    an external chat write; the runtime flips it on for real operation.
    """

    def __init__(self, ledger, store, client, fund_id: str, *, live: bool = False,
                 outbox: NoticeOutbox | None = None):
        self.ledger = ledger
        self.store = store
        self.client = client
        self.fund_id = fund_id
        self.live = live
        self.outbox = outbox or NoticeOutbox(store)
        self.last_reply: str | None = None

    # ---- helpers --------------------------------------------------------
    def _client_for(self, fund_id: str):
        return self.client if fund_id == self.fund_id else None

    def _msg_key(self, kind: str, message_id) -> str:
        return f'{kind}:{self.fund_id}:{message_id}'

    @staticmethod
    def _error_text(exc: FundError) -> str:
        code = getattr(exc, 'code', None) or 'error'
        return _ERROR_TEXT.get(code, f'操作未完成（{code}）。')

    # ---- entry point ----------------------------------------------------
    async def handle(self, message, now_ms: int | None = None) -> None:
        now = int(now_ms) if now_ms is not None else _now_ms()
        channel_id = get_field(message, 'channel_id')
        author = get_field(message, 'author')
        user_id = get_field(author, 'id')
        message_id = get_field(message, 'id')
        content = get_field(message, 'content') or ''
        # Private chat only; identity is the authoritative author.id.
        # Direct channel ids are opaque (the site hands out bare uuids), so the
        # guard names the one non-private channel instead of guessing a prefix:
        # the shared ``lobby``.  ``client.private_channels`` already filters to
        # ``kind == "direct"``, which is the primary gate.
        if not isinstance(channel_id, str) or not channel_id or channel_id == 'lobby':
            return
        if not user_id or message_id is None:
            return
        # The fund's own account is never an investor: its messages must not
        # open an intent, remember a channel or reach any ledger.
        bot_id = get_field(self.client, 'user_id')
        if bot_id is not None and str(user_id) == str(bot_id):
            return
        self.outbox.remember_channel(self.fund_id, str(user_id), channel_id,
                                     last_message_id=message_id, now_ms=now)
        text = str(content).strip()
        if not text.startswith('/'):
            return
        state_key = f'{self.fund_id}:{message_id}'
        state = self._begin_message(state_key, str(user_id), channel_id, message_id, now)
        if state == _MSG_COMPLETED:
            return  # durable message-key dedupe
        if state == _MSG_PREPARED:
            # The reply intent is already durable: requeue the *same* stable
            # outbox id and never re-run the ledger dispatch.
            await self._replay_prepared(state_key, str(user_id), channel_id, message_id, now)
            return
        head, _, argument = text.partition(' ')
        command = head.lower()
        argument = argument.strip()
        try:
            reply, image_payload = await self._dispatch(command, argument, str(user_id), message_id, now)
        except FundError as exc:
            reply, image_payload = self._error_text(exc), None
        except Exception as exc:  # never lose the reply on an unexpected error
            self.store.append_event('command_failed', fund_id=self.fund_id, level='error',
                                    details={'command': command, 'error': type(exc).__name__},
                                    created_ms=now)
            reply, image_payload = f'处理命令时出现异常（{type(exc).__name__}），请稍后再试。', None
        if not reply:
            self._mark_message(state_key, _MSG_COMPLETED, now)
            return
        self.last_reply = reply
        self.outbox.enqueue(self.fund_id, reply, user_id=str(user_id), channel_id=channel_id,
                            event_id=self._msg_key('reply', message_id),
                            image_payload=image_payload, kind=f'command:{command}', now_ms=now)
        # The durable intent exists: only now is the message ``prepared``, and
        # only after the delivery attempt may it become ``completed``.
        self._mark_message(state_key, _MSG_PREPARED, now)
        try:
            await self.outbox.deliver(self._client_for, live=self.live, now_ms=now)
        except Exception as exc:
            self.store.append_event('command_deliver_failed', fund_id=self.fund_id, level='warning',
                                    details={'error': type(exc).__name__}, created_ms=now)
            return
        self._mark_message(state_key, _MSG_COMPLETED, now)

    # ---- durable message state (no await inside a transaction) ----------
    def _begin_message(self, key: str, user_id: str, channel_id: str,
                       message_id, now: int) -> str:
        event_id = self._msg_key('reply', message_id)
        with self.store.transaction():
            record = self.store.get('messages', key) or {}
            state = record.get('state')
            if state == _MSG_COMPLETED:
                return _MSG_COMPLETED
            if state == _MSG_PREPARED and self.store.get('notices', event_id) is not None:
                return _MSG_PREPARED
            self.store.put('messages', key, {
                'fund_id': self.fund_id, 'user_id': user_id, 'channel_id': channel_id,
                'event_id': event_id, 'state': _MSG_PROCESSING,
                'created_ms': int(record.get('created_ms') or now), 'updated_ms': now,
            })
            return _MSG_PROCESSING

    def _mark_message(self, key: str, state: str, now: int) -> None:
        with self.store.transaction():
            record = self.store.get('messages', key) or {}
            record['state'] = state
            record['updated_ms'] = now
            self.store.put('messages', key, record)

    async def _replay_prepared(self, key: str, user_id: str, channel_id: str,
                               message_id, now: int) -> None:
        event_id = self._msg_key('reply', message_id)
        record = self.store.get('notices', event_id)
        if record is None:
            # Defensive: a prepared state without its outbox row is re-dispatched
            # on the next poll instead of silently dropping the reply.
            self._mark_message(key, _MSG_PROCESSING, now)
            return
        self.outbox.enqueue(self.fund_id, record.get('content') or '', user_id=user_id,
                            channel_id=channel_id, event_id=event_id,
                            image_payload=record.get('image_payload'),
                            kind=record.get('kind') or 'notice', now_ms=now)
        try:
            await self.outbox.deliver(self._client_for, live=self.live, now_ms=now)
        except Exception as exc:
            self.store.append_event('command_deliver_failed', fund_id=self.fund_id, level='warning',
                                    details={'error': type(exc).__name__}, created_ms=now)
            return
        self._mark_message(key, _MSG_COMPLETED, now)

    async def _dispatch(self, command: str, argument: str, user_id: str, message_id, now: int):
        if command in ('/subscription', '/redemption', '/emergency') and argument.startswith('cancel '):
            order_id = argument[7:].strip()
            result = self.ledger.cancel_order(self.fund_id, user_id, order_id, now)
            return f'订单 {order_id} 已撤回。已到账但未确认的申购款将原路全额退回。', None
        if command in ('/redemption', '/emergency'):
            trade = self.store.get('fund_trader', self.fund_id, {})
            if self.store.get('reconciliation_holds', self.fund_id, {}).get('hold', False) or trade.get('blocked') or trade.get('pending'):
                raise FundError('account_not_ready')
        if command == '/help':
            return HELP_TEXT, None
        if command == '/check':
            return self._check(user_id, now), None
        if command == '/subscription':
            return await self._subscription(argument, user_id, message_id, now)
        if command == '/redemption':
            return self._redemption(argument, user_id, message_id, now, _ORDINARY), None
        if command == '/emergency':
            return self._redemption(argument, user_id, message_id, now, _EMERGENCY), None
        return f'未识别的命令。\n\n{HELP_TEXT}', None

    # ---- /check ---------------------------------------------------------
    def _check(self, user_id: str, now: int) -> str:
        if callable(getattr(self.ledger, 'is_control_user', None)) and self.ledger.is_control_user(user_id):
            lines = ['【控制用户资金总览】', '金额单位：小鱼干；依据最近账簿快照，不查询控制账户外部钱包。']
            for value in self.ledger.control_finances(user_id):
                lines += [f"\n【{value['label']} · {value['fund_id']}】",
                          f"状态：{_STATE_TEXT.get(value['state'], value['state'])} · 净值 {value['nav']}",
                          f"在外份额：{shares_text(value['shares_atoms'])}",
                          f"控制用户份额：{shares_text(value['user_shares_atoms'])} · 估值 {money_text(value['user_value_units'])}"]
                fields = [('账户钱包', 'wallet_units'), ('持仓净价值', 'position_value_units'),
                          ('基金净资产', 'equity_units'), ('可用现金', 'available_cash_units'),
                          ('控制用户累计登记本金', 'control_capital_units'), ('初始登记本金', 'seeded_units'),
                          ('待确认本金', 'pending_receipts_units'), ('未转出预收/历史费用', 'fee_balance_units'),
                          ('应付款负债', 'liabilities_units'), ('未认领款', 'unclaimed_units'),
                          ('已实现交易利润', 'realized_profit_units'), ('累计净资本流入', 'capital_flows_units'),
                          ('机构手续费已转出', 'institution_fee_paid_units'),
                          ('机构手续费待转出', 'institution_fee_pending_units'),
                          ('其中结果未知', 'institution_fee_unknown_units')]
                lines += [f"{label}：{money_text(value[key])}" for label, key in fields]
                lines.append(f"估值时间：{timestamp_text(value['quote_ms']) if value['quote_ms'] else '暂无有效估值'}")
            lines += ['\n预收费用可退款；待转出手续费包含结果未知部分。累计登记本金不代表当前可取余额。']
            return '\n'.join(lines)
        status = self.ledger.status(self.fund_id, user_id=user_id) or {}
        label = get_field(status, 'label', self.fund_id)
        nav = get_field(status, 'nav', '-')
        state = str(get_field(status, 'state', '') or 'unknown')
        state_text = _STATE_TEXT.get(state)
        state_line = f'基金状态：{state_text}（{state}）' if state_text else f'基金状态：{state}'
        atoms = get_field(status, 'user_shares_atoms', 0) or 0
        value = get_field(status, 'user_value_units', 0) or 0
        updated = get_field(status, 'updated_ms') or now
        return (
            f'【{label}】\n'
            f'{state_line}\n'
            f'单位净值：{nav}\n'
            f'您的份额：{shares_text(atoms)}\n'
            f'估算价值：{money_text(int(value))} 小鱼干\n'
            f'数据时间：{timestamp_text(int(updated))}\n'
            '仅显示您本人的持仓。'
        )

    # ---- /subscription --------------------------------------------------
    async def _subscription(self, argument: str, user_id: str, message_id, now: int):
        if not argument:
            return '用法：/subscription 金额（净本金，最多 4 位小数）', None
        units = money_units(argument, positive=True)
        order = self.ledger.create_subscription(
            self.fund_id, user_id, units, self._msg_key('sub', message_id), now) or {}
        principal = int(get_field(order, 'principal_units', units) or units)
        fee = int(get_field(order, 'fee_units', 0) or 0)
        total = int(get_field(order, 'total_units', principal + fee) or (principal + fee))
        note = get_field(order, 'payment_note', '') or ''
        expires = int(get_field(order, 'expires_ms', now + 180000) or (now + 180000))
        order_id = get_field(order, 'id', '') or ''
        remaining = max(0, (expires - now) // 1000)
        pay_url = None
        try:
            pay_url = await _maybe_await(
                self.client.pay_url(user_id, total, note, str(order_id or self._msg_key('sub', message_id))))
        except Exception:
            pay_url = None
        lines = [
            f'【申购订单 {order_id}】',
            f'净本金：{money_text(principal)} 小鱼干',
            f'申购服务费（5%）：{money_text(fee)} 小鱼干',
            f'合计支付：{money_text(total)} 小鱼干',
            f'付款备注：{note}',
            '',
            f'请在 {remaining} 秒内（截至 {timestamp_text(expires)}）完成支付。',
        ]
        if pay_url:
            lines += ['', f'支付链接：{pay_url}', '二维码见下图。']
        else:
            lines += ['', '支付链接获取失败，请稍后再试或联系管理员。']
        lines += [
            '',
            '超时到账将原路全额退款，不发行份额。',
            '实际份额按确认时适用净值计算，当前金额仅为估算。',
        ]
        return '\n'.join(lines), pay_url

    # ---- /redemption, /emergency ---------------------------------------
    def _redemption(self, argument: str, user_id: str, message_id, now: int, kind: str) -> str:
        usage = '用法：/redemption 金额（最多 4 位小数）' if kind == _ORDINARY \
            else '用法：/emergency 金额（最多 4 位小数）'
        if not argument:
            return usage
        units = money_units(argument, positive=True)
        order = self.ledger.request_redemption(
            self.fund_id, user_id, units, kind, self._msg_key(kind, message_id), now) or {}
        nav = get_field(order, 'nav_used') or get_field(order, 'nav') or get_field(order, 'nav_text') or '-'
        order_line = f'订单编号：{get_field(order, "id", "-")}；预留份额：{shares_text(get_field(order, "shares_reserved_atoms", 0))}\n'
        if kind == _ORDINARY:
            return (
                '【普通赎回申请已登记】\n'
                + order_line +
                f'申请金额（估算）：{money_text(units)} 小鱼干\n'
                f'参考净值：{nav}\n'
                '实际赎回金额＝确认份额×结算批次适用净值，会随净值变化，与申请时估算不同。\n'
                '普通赎回免收机构服务费；每月普通赎回上限为旧份额的 20%，'
                '超出部分按申请份额比例确认并顺延下月。\n'
                '确认后目标 5 个工作日内付款。'
            )
        fee_units = units // 10  # 10% fee, integer units only
        return (
            '【紧急赎回申请已登记】\n'
            + order_line +
            f'申请金额（估算）：{money_text(units)} 小鱼干\n'
            f'参考净值：{nav}\n'
            f'紧急赎回费（确认总额的 10%）：约 {money_text(fee_units)} 小鱼干，归原基金；'
            '实际到账约为确认总额的 90%。\n'
            '每日 18:00 为申请截止、20:00 估值；确认后目标 1–3 个工作日内付款。\n'
            '实际金额＝确认份额×适用净值，会随净值变化。\n'
            '流动性不足时整批延期，未确认部分不注销、不计费；分批执行需另行取得同意。'
        )
