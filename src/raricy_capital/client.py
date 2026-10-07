"""站点客户端：登录、资金转账、私聊/图片、二维码与练手盘交易。

本模块只服务本资金服务，是独立包的一部分。

可复用的上游协议片段按「原样复用、不复制实现」处理：聊天 DTO 取
:class:`raricy_capital.chat_models.ChatMessage`；练手盘 HTML 快照解析与站点时间
换算取 :func:`raricy_capital.site_protocol.parse_trade_page` /
:func:`raricy_capital.site_protocol.site_time_ms`。两处都与研究口径逐字一致，
不得为迁就本模块而改。

站点服务端（``route.ts`` / ``service.ts`` / ``db-time.ts`` / ``market-price.ts``
等）是字段与路由的唯一权威，文档只是口径说明。

硬约束（与上游契约一致，违反即静默亏钱）：

* **不自设 ``Origin`` / ``Referer``**：站的 CSRF 校验对同时缺失这两头的原生客户端
  放行，伪造一个错的反而 403。
* **不自行设置 ``Accept-Encoding``**。
* 会话 Cookie 只存在内存里（进程内属性），不落盘、不进日志；密码只用于登录请求，
  不带进任何业务请求。
* 金额一律用整数 1e-4 单位（``contracts.MONEY_SCALE``）；站点时才用
  ``money_text`` 转成 4 位小数字符串。转账幂等键跟着「这笔业务」走，未知结果用
  同一个键重发是安全的；平仓的结果未知必须对账（见下）。
"""

from __future__ import annotations

import asyncio
import io
import re
import urllib.parse
from collections.abc import Mapping

import httpx

from .contracts import FundError, money_text, money_units
from .chat_models import ChatMessage
from .site_protocol import parse_trade_page, site_time_ms

# ── 站点路由（逐条对照 data/raricy_fund_source 的 route.ts）───────────────────
_LOGIN_PATH = "/api/auth/login"
_ME_PATH = "/api/auth/me"
_BALANCE_PATH = "/api/fish/market/balance"
_TRANSACTIONS_PATH = "/api/fish/market/transactions"
_TRANSFER_PATH = "/api/fish/market/transfer"
_QUOTE_PATH = "/api/fish/trade/quote"
_CANDLES_PATH = "/api/fish/trade/candles"
_BUY_PATH = "/api/fish/trade/buy"
_SELL_PATH = "/api/fish/trade/sell"
_TRADE_PAGE_PATH = "/fish/trade"
_POLL_PATH = "/api/chat/poll"
_CHANNELS_PREFIX = "/api/chat/channels"
_IMAGES_PATH = "/api/images"
_PAY_PATH = "/fish/pay"

SESSION_COOKIE_NAME = "raricy_session"

# 练手盘标的与默认 K 线周期（market-service.ts / market-candles.ts）。
SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "1h"
MARKET_INTERVALS = ("1m", "5m", "15m", "1h", "4h", "1d")
# 站点 2026-10-07（d2331679）起把杠杆从固定白名单放宽为 **1–100 整数**，快捷按钮
# 不再是合法值白名单；服务端只做范围校验。本客户端同步接受整个区间（与 research
# 的整数多空口径一致）。capital1=3、capital2=5 都在区间内，实际下单不变。
LEVERAGE_MIN = 1
LEVERAGE_MAX = 100

# 与站点一致的上限（fish-market-service.ts / order 参数口径）。
NOTE_MAX = 30
CLIENT_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{1,48}")
ORDER_RE = re.compile(r"[A-Za-z0-9_.:-]{1,32}")
_CHANNEL_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TRANSFER_TYPES = ("transfer", "transfer_receive")
_CANDLE_AGE_MS_MAX = 10_000
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024


class FundSiteError(Exception):
    """稳定的失败类别；``str(exc)`` 就是类别码，**绝不包含上游正文、金额或凭据**。

    字段语义：

    * ``code``：固定类别码（见下方映射表），可直接用于日志与分支。
    * ``status``：站点的 ``code``（HTTP 状态与信封 code 一致）；``0`` 表示网络层失败
      —— 此时结果不确定，可能是「没发出」也可能是「成交了」。
    * ``retryable``：同一个**幂等键**原样重发是否安全。转账在同类失败下为真
      （服务端按客户端幂等键去重，超时后同键重发不会重复付款）；参数类错误为假。
    * ``reconcile``：结果未知，必须先去查流水/持仓确认再决定，**不能**假定未成交。
      平仓（sell）的未知结果一律置真；买入与转账的未知结果同样置真，因为它们都
      可能已经成交。
    * ``retry_after``：429 时响应 ``Retry-After`` 的秒数（非负有限值），否则 None。
    """

    def __init__(
        self,
        code: str,
        *,
        status: int = 0,
        retryable: bool = False,
        reconcile: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.reconcile = reconcile
        self.retry_after = retry_after

    def __repr__(self) -> str:  # 不依赖上游文案，避免异常链里带出正文
        return f"FundSiteError({self.code!r}, status={self.status})"


def _parse_retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def _clean_note(note: str | None) -> str:
    """与站点 ``transferFish`` 同一口径：trim 后把连续空白压成单个空格。

    **重试必须字节一致**：同名幂等键换了 description 会被服务端判 409，所以这里
    在发请求之前就把留言归一，调用方重发同一业务时算出的 description 必然相同。
    """
    return " ".join((note or "").split())


def _as_units(value: object, *, positive: bool = False) -> int:
    """把站点的鱼干数值（JSON number / 数字字符串）转成整数 1e-4 单位。"""
    try:
        return money_units(value, positive=positive)
    except FundError:
        raise FundSiteError("amount_invalid") from None


def _price(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FundSiteError("invalid_response")
    return float(value)


def _extract_note(description: object, row_type: str) -> str | None:
    """从流水描述里取出原始留言。

    站点把留言拼进人类可读的描述（``转给「alice」：备注`` /
    ``收到「alice」的转账：备注``），**这是流水里唯一携带备注的字段**。取第一个
    ``：`` 之后的部分；没有冒号且有备注语境的转账行说明本就没写留言，返回空串。
    非转账行不承载留言语义，返回 None。
    """
    if row_type not in _TRANSFER_TYPES:
        return None
    if not isinstance(description, str):
        return ""
    if "：" in description:
        return description.split("：", 1)[1].strip()
    return ""


def _occurred_ms(created_at: object) -> int | None:
    """把站点时间戳转成真实 UTC 毫秒。

    ``created_at`` / ``occurred_at`` / 练手盘 ``opened_at`` 都是「UTC+8 墙上时间贴 Z 标签」的
    **假 UTC**（``nowForDb()``，见 docs/bot/fish-bot.md §3.3.1 与 db-time.ts），必须减 8
    小时；只有 K 线的 ``openTime`` 是交易所给的真 UTC，不能走这里。解析不出来时返回
    None，由调用方按 transfer_id 兜底对账，不因此丢掉整行。
    """
    if not isinstance(created_at, str) or not created_at:
        return None
    try:
        return site_time_ms(created_at, wall_clock=True)
    except ValueError:
        return None


class FundSiteClient:
    """一条内存会话打通资金、讨论与练手盘。

    构造签名按冻结契约：``FundSiteClient(base_url, username, password, *, transport=None)``。
    ``transport`` 注入 ``httpx.MockTransport`` 供隔离测试；生产不得传真实的外部
    请求替身。
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise FundSiteError("invalid_base_url")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FundSiteError("invalid_base_url")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise FundSiteError("invalid_base_url")
        self.base_url = base_url.rstrip("/")
        self._login_username = username
        self._password = password
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        # 会话与身份**只存内存**：进程重启即失效，绝不落盘。
        self._session_cookie: str | None = None
        self._user_id: str | None = None
        self._site_username: str | None = None
        self._login_lock = asyncio.Lock()

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    async def __aenter__(self) -> "FundSiteClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        """创建底层 httpx 客户端；重复调用不会重复创建。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
                transport=self._transport,
                follow_redirects=False,
            )

    async def close(self) -> None:
        """关闭底层客户端；未启动或重复调用都安全。"""
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @property
    def user_id(self) -> str | None:
        """登录后站点返回的稳定用户 ID（未登录为 None）。"""
        return self._user_id

    @property
    def username(self) -> str | None:
        """登录后的站点用户名（收款链接的 ``to`` 用它，未登录为 None）。"""
        return self._site_username

    @property
    def session_cookie(self) -> str | None:
        """当前会话 Cookie 值；仅供测试与装配核对，**不得写日志**。"""
        return self._session_cookie

    # ── 登录 ─────────────────────────────────────────────────────────────────

    async def login(self) -> dict:
        """站点凭据登录，返回 ``{'id', 'username'}``。

        成功后会话 Cookie 只留在内存属性里；紧接着用 ``GET /api/auth/me`` 复核身份
        —— 登录响应的 user 与 me 的 user 必须是同一个 ID，不同即
        ``FundSiteError('identity_mismatch')``（双基金各自账号，错登等于资金串号）。
        被禁言 / 非 core+ 不会在这里失败（那是各业务接口的 403），如实由调用方处理。
        """
        await self.start()
        response = await self._request(
            "POST",
            _LOGIN_PATH,
            json_body={"username": self._login_username, "password": self._password},
        )
        payload = self._decode(response)
        if payload.get("code") != 200:
            raise self._error_from(response, payload["code"])
        self._store_cookie(response)
        user = payload.get("user")
        if not isinstance(user, Mapping) or not isinstance(user.get("id"), str) or not user["id"]:
            raise FundSiteError("login_failed", status=payload["code"])

        me = await self._api("GET", _ME_PATH, retry_auth=False)
        me_user = me.get("user")
        if not isinstance(me_user, Mapping) or not isinstance(me_user.get("id"), str):
            # /api/auth/me 未登录时回 user:null（200），不是 401。
            raise FundSiteError("login_failed", status=200)
        if me_user["id"] != user["id"]:
            raise FundSiteError("identity_mismatch", status=200)
        self._user_id = user["id"]
        self._site_username = me_user.get("username") if isinstance(me_user.get("username"), str) else ""
        return {"id": self._user_id, "username": self._site_username}

    async def _ensure_login(self) -> None:
        if self._user_id is not None:
            return
        async with self._login_lock:
            if self._user_id is None:
                await self.login()

    async def _relogin(self) -> None:
        """会话失效后的强制重登：先丢弃本地会话，避免单飞复检返回旧身份。"""
        self._session_cookie = None
        self._user_id = None
        self._site_username = None
        await self.login()

    # ── 资金：余额 / 流水 / 转账 ─────────────────────────────────────────────

    async def balance(self) -> int:
        """当前账户余额，整数 1e-4 单位。"""
        await self._ensure_login()
        payload = await self._api("POST", _BALANCE_PATH, json_body={})
        self._assert_self(payload)
        return _as_units(payload.get("balance"))

    async def transactions(self, since_id: int) -> dict:
        """游标拉取流水（``id > since_id``，升序，最多 100 行）。

        返回 ``{'transactions', 'next_cursor', 'has_more'}``；每行是规范化后的
        dict：``id`` / ``transaction_row_id`` / ``transfer_id`` / ``from_user_id``
        / ``to_user_id`` / ``amount_units``（有符号）/ ``note`` / ``occurred_ms``
        （真实 UTC 毫秒）/ ``type``。

        ``from_user_id`` 是**权威**的对方身份：收款行取站点填的
        ``related_user_id``；支出行取本账户自身 ID。绝不从用户名或备注猜人
        （docs/bot/fish-bank-example.md §5 第 2 条）。``to_user_id`` 同源：支出行取
        ``related_user_id``（退款对账要核对收款人），收款行就是本账户。
        ``transaction_row_id`` 与 ``id`` 同值，是未认领款补链与权威回查的稳定行键
        —— 站点不提供反向查询，有行号就不必从 0 重扫全量历史。
        """
        await self._ensure_login()
        if isinstance(since_id, bool) or not isinstance(since_id, int) or since_id < 0:
            raise FundSiteError("invalid_cursor")
        payload = await self._api(
            "POST", _TRANSACTIONS_PATH, json_body={"since_id": since_id, "limit": 100}
        )
        self._assert_self(payload)
        rows = payload.get("transactions")
        normalized: list[dict] = []
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                normalized.append(self._normalize_transaction(row))
        next_cursor = payload.get("next_cursor", since_id)
        if isinstance(next_cursor, bool) or not isinstance(next_cursor, int):
            next_cursor = since_id
        return {
            "transactions": normalized,
            "next_cursor": next_cursor,
            "has_more": bool(payload.get("has_more")),
        }

    def _normalize_transaction(self, row: Mapping) -> dict:
        row_type = row.get("type") if isinstance(row.get("type"), str) else ""
        related = row.get("related_user_id")
        related_id = related if isinstance(related, str) and related else None
        if row_type == "transfer_receive":
            # 收款行：对方是付款人，收款人是本账户。
            from_user_id = related_id
            to_user_id = self._user_id
        elif row_type == "transfer":
            # 支出行：付款人是本账户，对方（related_user_id）是收款人。
            # 退款对账必须核对收款人是不是原付款人，所以这里必须保留。
            from_user_id = self._user_id
            to_user_id = related_id
        else:
            from_user_id = related_id
            to_user_id = None
        transfer_id = row.get("transfer_id")
        tx_id = row.get("id")
        row_id = tx_id if isinstance(tx_id, int) and not isinstance(tx_id, bool) else None
        return {
            "id": row_id,
            # 同一个稳定行键：补链/回查都按它快速定位，缺失时才退回有界扫描。
            "transaction_row_id": row_id,
            "transfer_id": transfer_id if isinstance(transfer_id, str) and transfer_id else None,
            "from_user_id": from_user_id,
            "to_user_id": to_user_id,
            "amount_units": _as_units(row.get("amount")),
            "note": _extract_note(row.get("description"), row_type),
            "occurred_ms": _occurred_ms(row.get("created_at")),
            "type": row_type,
        }

    async def transfer(self, user_id: str, amount_units: int, note: str | None, key: str) -> dict:
        """向 ``user_id`` 转账 ``amount_units``（整数 1e-4 单位）。

        返回 ``{'transfer_id', 'amount_units', 'duplicated'}``。

        **幂等**：``key`` 必须跟着这笔业务走（1–48 位 ``[A-Za-z0-9_.:-]``），重试
        原样重发同一键 + 同一收款人/金额/留言即可，服务端会回报原单而不重复扣款。
        网络超时 / 5xx 的失败结果未知（``reconcile=True``）但**同键重发安全**
        （``retryable=True``）；同键换参数会被服务端 409（``conflict``），绝不能
        换个业务复用同一个键。
        """
        await self._ensure_login()
        if not isinstance(user_id, str) or not user_id.strip():
            raise FundSiteError("invalid_recipient")
        if user_id == self._user_id:
            raise FundSiteError("self_transfer")
        units = self._validate_units(amount_units)
        if not isinstance(key, str) or CLIENT_KEY_RE.fullmatch(key) is None:
            raise FundSiteError("key_invalid")
        clean = _clean_note(note)
        if len(clean) > NOTE_MAX:
            raise FundSiteError("note_too_long")
        body: dict = {
            "to_user_id": user_id.strip(),
            "amount": money_text(units),
            "idempotency_key": key,
        }
        if clean:
            body["note"] = clean

        payload = await self._api("POST", _TRANSFER_PATH, json_body=body)
        transfer_id = payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not transfer_id:
            # 200 但缺共享单号：钱可能已经动了，必须对账而不是当作失败。
            raise FundSiteError("transfer_unconfirmed", status=200, reconcile=True)
        return {
            "transfer_id": transfer_id,
            "amount_units": _as_units(payload.get("amount")),
            "duplicated": bool(payload.get("duplicated")),
        }

    def _validate_units(self, amount_units: object) -> int:
        if isinstance(amount_units, bool) or not isinstance(amount_units, int) or amount_units <= 0:
            raise FundSiteError("amount_invalid")
        if amount_units > 10**15:
            raise FundSiteError("amount_invalid")
        return amount_units

    def _assert_self(self, payload: Mapping) -> None:
        """响应里的身份字段若出现，必须与已登录身份一致。"""
        uid = payload.get("user_id")
        if isinstance(uid, str) and uid and uid != self._user_id:
            raise FundSiteError("identity_mismatch", status=200)

    # ── 讨论：私聊频道、消息、图片 ───────────────────────────────────────────

    async def private_channels(self) -> list[str]:
        """当前账户可见的私聊频道 ID 列表（大区 ``lobby`` 不在内）。"""
        await self._ensure_login()
        payload = await self._api("GET", _POLL_PATH)
        channels = payload.get("channels")
        result: list[str] = []
        if isinstance(channels, list):
            for channel in channels:
                if not isinstance(channel, Mapping):
                    continue
                if channel.get("kind") != "direct":
                    continue
                channel_id = channel.get("id")
                if isinstance(channel_id, str) and channel_id:
                    result.append(channel_id)
        return result

    async def fetch_messages(self, channel_id: str, after: int | None = None) -> list[ChatMessage]:
        """读频道消息（按 id 升序），返回站内 DTO :class:`ChatMessage` 列表。"""
        await self._ensure_login()
        path = self._channel_messages_path(channel_id)
        params: dict = {"limit": 100}
        if after is not None:
            if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                raise FundSiteError("invalid_cursor")
            params["after"] = after
        payload = await self._api("GET", path, params=params)
        raw = payload.get("messages")
        messages: list[ChatMessage] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                try:
                    messages.append(ChatMessage.from_dict(item))
                except ValueError:
                    continue
        messages.sort(key=lambda message: message.id)
        return messages

    async def send_message(
        self,
        channel_id: str,
        content: str,
        reply_to: int | None = None,
        image_bytes: bytes | None = None,
    ) -> dict:
        """发消息；带 ``image_bytes`` 时先上传图床再以 ``image_id`` 引用发图。

        返回站点返回的消息对象（dict）。服务端成功时 ``message`` 字段本身就是消息
        对象（docs/bot/chat-bot.md §7.2），本方法直接交回该映射；200 但缺消息体时
        抛 ``send_unconfirmed``（``reconcile=True``）—— 消息可能已经发出去了。
        """
        await self._ensure_login()
        path = self._channel_messages_path(channel_id)
        if not isinstance(content, str):
            raise FundSiteError("invalid_message")
        body: dict = {"content": content}
        image_id: str | None = None
        if image_bytes is not None:
            image_id = (await self.upload_qr(image_bytes))["id"]
            body["image_id"] = image_id
        if reply_to is not None:
            if isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to <= 0:
                raise FundSiteError("invalid_message")
            body["reply_to"] = reply_to
        if not content and image_id is None:
            raise FundSiteError("empty_message")

        payload = await self._api("POST", path, json_body=body)
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise FundSiteError("send_unconfirmed", status=200, reconcile=True)
        return dict(message)

    async def upload_image(
        self,
        image_bytes: bytes,
        *,
        filename: str | None = None,
        mime: str | None = None,
    ) -> dict:
        """上传一张图到图床，返回 ``{'id', 'url'}``（``url`` 是相对路径）。

        只允许 PNG / JPEG / GIF / WebP / SVG（站点白名单）。``compress='0'`` 请求
        原图入库 —— 二维码重压缩会糊边，扫码识别率下降。
        """
        await self._ensure_login()
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            raise FundSiteError("invalid_image")
        data = bytes(image_bytes)
        if len(data) > _MAX_IMAGE_BYTES:
            raise FundSiteError("image_too_large")
        stripped = data.lstrip()
        if mime is None:
            # 站点按内容嗅探真实格式（verifyImageMime），声明的 MIME 必须与字节一致，
            # 否则整张被判「文件内容与声明的格式不匹配」。这里只区分 PNG 与 SVG 两种
            # 本模块会产出的格式。
            mime = (
                "image/svg+xml"
                if stripped[:4].lower() == b"<svg" or stripped[:5] == b"<?xml"
                else "image/png"
            )
        if filename is None:
            filename = "qr.svg" if mime == "image/svg+xml" else "qr.png"
        payload = await self._api(
            "POST",
            _IMAGES_PATH,
            files={"file": (filename, data, mime)},
            data={"compress": "0"},
        )
        image_id = payload.get("id")
        url = payload.get("url")
        if not isinstance(image_id, str) or not image_id:
            items = payload.get("items")
            if isinstance(items, list) and items and isinstance(items[0], Mapping):
                image_id = items[0].get("id")
                url = items[0].get("url")
        if not isinstance(image_id, str) or not image_id:
            raise FundSiteError("image_upload_failed", status=200)
        if not isinstance(url, str) or not url:
            url = f"{_IMAGES_PATH}/{image_id}/raw"
        return {"id": image_id, "url": url}

    async def upload_qr(
        self,
        image_bytes: bytes,
        *,
        filename: str | None = None,
        mime: str | None = None,
    ) -> dict:
        """上传二维码图片字节（PNG 或 SVG，源码决定实际格式），返回图片引用。

        契约里的 ``upload_qr(svg_bytes 或 png_bytes)``：站点图床白名单同时收
        ``image/png`` 与 ``image/svg+xml``，格式由字节内容判断（也可显式传 ``mime``）。
        """
        return await self.upload_image(image_bytes, filename=filename, mime=mime)

    # ── 收银台：支付链接与二维码 ─────────────────────────────────────────────

    async def pay_url(
        self,
        user_id: str,
        amount_units: int,
        note: str | None,
        order_key: str,
    ) -> str:
        """生成站内收银台支付链接（用户在自己浏览器里打开）。

        目标收款人恒为**本客户端登录的基金账号**（``to`` 取登录用户名）—— 双基金
        各自独立账号，链接绝不能指向另一个基金。

        ``user_id`` 是付款会员的稳定站点 ID。**它不写进 URL**：收银台只认
        ``to/amount/order/note``，而会员身份必须由到账流水的 ``from_user_id`` 认定
        （备注与用户名都可伪造）。这里只做两项校验：必须是非空字符串，且不能等于
        基金账号自身（站点对「给自己付款」同样是明确拒绝）。

        ``order_key`` 是收银台防重的唯一手段（1–32 位 ``[A-Za-z0-9_.:-]``），必须是
        「一笔业务一个订单号」；同一订单号 + 同一金额刷新重付不会重复扣款，换了金额
        则明确报错。``amount_units`` 是净本金 + 费用的含费总额，整数 1e-4 单位。
        """
        await self._ensure_login()
        if not isinstance(user_id, str) or not user_id.strip():
            raise FundSiteError("invalid_payer")
        if user_id == self._user_id:
            raise FundSiteError("self_payment")
        units = self._validate_units(amount_units)
        if not isinstance(order_key, str) or ORDER_RE.fullmatch(order_key) is None:
            raise FundSiteError("order_key_invalid")
        clean = _clean_note(note)
        if len(clean) > NOTE_MAX:
            raise FundSiteError("note_too_long")
        params = {"to": self._site_username or "", "amount": money_text(units), "order": order_key}
        if clean:
            params["note"] = clean
        return f"{self.base_url}{_PAY_PATH}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def qr_png(text: str, *, box_size: int = 8, border: int = 2) -> bytes:
        """把字符串（典型的是一条支付链接）渲染成 PNG 二维码字节。

        站点只有 ``GET /api/poster/collect`` 这一处服务端二维码，且它**没有参数** ——
        只能生成登录账号自己的静态收款码，带金额/订单号的收银台链接它渲染不了。因此
        订单级二维码在本地渲染，再经 :meth:`upload_qr` 上传。
        """
        try:
            import qrcode  # 延迟导入：未装 funds extra 时本模块仍可导入与使用
            from qrcode.constants import ERROR_CORRECT_M
        except ImportError:
            raise FundSiteError("qrcode_unavailable") from None
        try:
            code = qrcode.QRCode(
                version=None,
                error_correction=ERROR_CORRECT_M,
                box_size=int(box_size),
                border=int(border),
            )
            code.add_data(text)
            code.make(fit=True)
            image = code.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        except Exception:  # qrcode/PIL 的失败不向调用方透传渲染库正文
            raise FundSiteError("qr_render_failed") from None
        return buffer.getvalue()

    # ── 练手盘：行情、快照、买卖 ─────────────────────────────────────────────

    async def quote(self) -> tuple[float, float]:
        """BTC 展示报价与平仓费率 ``(price, fee_rate)``（均为 float）。

        **只用于风控判断，不用于成交价**：真实成交价由服务端在下单那一刻现取，并出现在
        ``buy``/``sell`` 的 ``entry_price``/``exit_price`` 里（docs/bot/trade-bot.md §4）。
        站点这份展示缓存的阈值是 ``QUOTE_STALE_MS=40s``（REST 轮询 15s、WS 帧信任窗
        ``STREAM_TRUST_MS=10s``，见 market-price.ts）；本客户端再收紧一档，只接受
        ``age_ms <= 10_000`` —— 宁可多抛一次可重试的 ``quote_unavailable``，也不拿
        抖动过的价去判断风控。报价不可用、陈旧或年龄超限时抛 ``quote_unavailable``
        （``retryable=True``）。
        """
        await self._ensure_login()
        payload = await self._api("GET", _QUOTE_PATH)
        if not payload.get("ok"):
            raise FundSiteError("quote_unavailable", status=200, retryable=True)
        for item in payload.get("quotes") or []:
            if not isinstance(item, Mapping) or item.get("symbol") != SYMBOL:
                continue
            if item.get("stale"):
                raise FundSiteError("quote_unavailable", status=200, retryable=True)
            age = item.get("age_ms")
            if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 <= age <= _CANDLE_AGE_MS_MAX:
                raise FundSiteError("quote_unavailable", status=200, retryable=True)
            return _price(item.get("price")), _price(payload.get("fee_rate"))
        raise FundSiteError("quote_unavailable", status=200, retryable=True)

    async def candles(self, symbol: str = SYMBOL, interval: str = DEFAULT_INTERVAL) -> list[tuple]:
        """读 K 线，返回 ``[(open_ms, open, high, low, close, volume), ...]``。

        ``open_ms`` 是**交易所给的真实 UTC 毫秒**（与 ``Date.now()`` 同一把尺子），
        **绝不能**像流水时间戳那样减 8 小时。路由固定取 ``CANDLE_LIMIT=1000`` 根
        （见 market-candles.ts），且**末根是当前尚未走完的桶**（站点只保证末根落在
        当前桶里，见 market-price.ts 的 ``behindCurrentBucket``）：EMA/ATR 等只用已
        收盘的小时，调用方必须丢弃末根。
        """
        await self._ensure_login()
        if interval not in MARKET_INTERVALS:
            raise FundSiteError("interval_invalid")
        payload = await self._api("GET", _CANDLES_PATH, params={"symbol": symbol, "interval": interval})
        raw = payload.get("candles")
        result: list[tuple] = []
        if isinstance(raw, list):
            for candle in raw:
                if not isinstance(candle, list) or len(candle) < 6:
                    continue
                try:
                    open_ms = int(candle[0])
                    values = tuple(float(part) for part in candle[1:6])
                except (TypeError, ValueError):
                    continue
                result.append((open_ms, *values))
        return result

    async def snapshot(self) -> dict:
        """读练手盘页面快照（持仓 + 余额 + 费率/杠杆能力）。

        数据来自服务端渲染的 ``/fish/trade`` 页面 props（站点**没有**「列出我的持仓」
        的 JSON 接口，docs/bot/trade-bot.md §8.2），复用既有
        :func:`parse_trade_page` 解析。页面的 ``openedAt`` 与流水时间戳同族：站点用
        ``nowForDb()``（UTC+8 墙钟贴 Z，db-time.ts）落库，所以这里减 8 小时换算成
        **真实 UTC 毫秒**。

        返回规范化 dict::

            {
              'balance_units': int,
              'fee_rate': float,
              'min_stake_units': int,
              # 空列表表示站点不再下发白名单：杠杆是 1–100 整数（LEVERAGE_MIN..MAX）。
              'leverage_options': list[int],
              'leverage_enabled': bool,
              'positions': [
                 {'position_id', 'symbol', 'stake_units', 'entry_price',
                  'liquidation_price', 'leverage', 'opened_ms'},
              ],
            }
        """
        await self._ensure_login()
        text = await self._get_text(_TRADE_PAGE_PATH)
        try:
            panel = parse_trade_page(text)
        except ValueError:
            raise FundSiteError("snapshot_unavailable", status=200) from None
        positions: list[dict] = []
        for pos in panel.get("positions") or []:
            if not isinstance(pos, Mapping):
                continue
            opened = pos.get("openedAt")
            opened_ms = None
            if isinstance(opened, str):
                try:
                    # 站点 opened_at 来自 nowForDb()（UTC+8 墙钟贴 Z，db-time.ts），
                    # 与流水 created_at 同族 —— 必须减 8 小时才是真实 UTC。
                    opened_ms = site_time_ms(opened, wall_clock=True)
                except ValueError:
                    opened_ms = None
            liq = pos.get("liquidationPrice")
            positions.append(
                {
                    "position_id": pos.get("id"),
                    "symbol": pos.get("symbol"),
                    "stake_units": _as_units(pos.get("stake")),
                    "entry_price": _price(pos.get("entryPrice")),
                    "liquidation_price": _price(liq) if isinstance(liq, (int, float)) and not isinstance(liq, bool) else None,
                    "leverage": pos.get("leverage"),
                    "opened_ms": opened_ms,
                }
            )
        options = [int(x) for x in (panel.get("leverageOptions") or []) if isinstance(x, int) and not isinstance(x, bool)]
        return {
            "balance_units": _as_units(panel.get("balance")),
            "fee_rate": _price(panel.get("feeRate")),
            "min_stake_units": _as_units(panel.get("minStake")),
            "leverage_options": options,
            "leverage_enabled": bool(panel.get("leverageEnabled")),
            "positions": positions,
        }

    async def buy(self, amount: int, leverage: int, key: str) -> dict:
        """开仓：投入 ``amount``（整数 1e-4 单位）鱼干、``leverage`` 倍、幂等键 ``key``。

        服务端按 ``key`` 去重（同一笔重试回报原仓位，不重复扣款、不消耗额度）；
        服务端**不用参数去重**，所以键必须跟着这笔业务走。未知结果（超时 / 5xx）
        ``reconcile=True`` 且 ``retryable=True``（同键重发安全）。

        返回 ``{'position_id', 'symbol', 'stake_units', 'entry_price', 'leverage',
        'liquidation_price', 'opened_ms', 'balance_units', 'replayed'}``；``entry_price``
        是**真实成交价**（不是展示价，由服务端下单时现取），``opened_ms`` 已从站点
        ``nowForDb()`` 的 UTC+8 墙钟换算成**真实 UTC 毫秒**。
        """
        await self._ensure_login()
        units = self._validate_units(amount)
        if (isinstance(leverage, bool) or not isinstance(leverage, int)
                or not LEVERAGE_MIN <= leverage <= LEVERAGE_MAX):
            raise FundSiteError("leverage_invalid")
        if not isinstance(key, str) or CLIENT_KEY_RE.fullmatch(key) is None:
            raise FundSiteError("key_invalid")
        payload = await self._api(
            "POST",
            _BUY_PATH,
            json_body={
                "symbol": SYMBOL,
                "amount": money_text(units),
                "leverage": leverage,
                "idempotency_key": key,
            },
        )
        position = payload.get("position")
        if not isinstance(position, Mapping):
            raise FundSiteError("buy_unconfirmed", status=200, reconcile=True, retryable=True)
        position_id = position.get("id")
        if not isinstance(position_id, str) or not position_id:
            raise FundSiteError("buy_unconfirmed", status=200, reconcile=True, retryable=True)
        opened_ms = None
        opened = position.get("opened_at")
        if isinstance(opened, str):
            try:
                # 同流水：nowForDb() 是 UTC+8 墙钟贴 Z，减 8 小时才是真实 UTC 毫秒。
                opened_ms = site_time_ms(opened, wall_clock=True)
            except ValueError:
                opened_ms = None
        liq = position.get("liquidation_price")
        return {
            "position_id": position_id,
            "symbol": position.get("symbol"),
            "stake_units": _as_units(position.get("stake")),
            "entry_price": _price(position.get("entry_price")),
            "leverage": position.get("leverage"),
            "liquidation_price": _price(liq) if isinstance(liq, (int, float)) and not isinstance(liq, bool) else None,
            "opened_ms": opened_ms,
            "balance_units": _as_units(payload.get("balance")),
            "replayed": bool(payload.get("replayed")),
        }

    async def sell(self, position_id: str) -> dict:
        """整仓平仓（无幂等键 —— 服务端天然幂等：已结清再平是重放）。

        返回 ``{'position_id', 'symbol', 'payout_units', 'profit_units', 'exit_price',
        'liquidated', 'replayed', 'balance_units'}``。

        **未知结果必须对账**：网络超时 / 5xx 时抛 ``reconcile=True`` 的
        ``FundSiteError``，调用方必须先查流水与快照确认仓位是否已结清，再决定要不要
        用同一 ``position_id`` 重发（服务端幂等，重发本身不会多结算，但对账不能省）。
        ``liquidated=True`` 且 ``payout_units=0`` 表示仓位早已被强平，不是本次卖出
        成交 —— 两者不能并进同一档。
        """
        await self._ensure_login()
        if not isinstance(position_id, str) or not position_id.strip():
            raise FundSiteError("invalid_position")
        payload = await self._api("POST", _SELL_PATH, json_body={"position_id": position_id.strip()})
        result_id = payload.get("position_id")
        if not isinstance(result_id, str) or not result_id:
            raise FundSiteError("sell_unconfirmed", status=200, reconcile=True)
        return {
            "position_id": result_id,
            "symbol": payload.get("symbol"),
            "payout_units": _as_units(payload.get("payout")),
            "profit_units": _as_units(payload.get("profit")),
            "exit_price": _price(payload.get("exit_price")),
            "liquidated": bool(payload.get("liquidated")),
            "replayed": bool(payload.get("replayed")),
            "balance_units": _as_units(payload.get("balance")),
        }

    # ── 传输与错误分类 ───────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        params: Mapping | None = None,
        files: object | None = None,
        data: Mapping | None = None,
    ) -> httpx.Response:
        """发一次请求；网络层失败映射为 ``network`` / ``timeout``（结果未知）。"""
        client = self._client
        if client is None:
            raise RuntimeError("FundSiteClient 尚未 start()")
        headers = {"Accept": "application/json"}
        if self._session_cookie:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={self._session_cookie}"
        kwargs: dict = {}
        if files is not None or data is not None:
            # multipart 与 JSON 互斥：同时给 json=None 与 files 容易踩 httpx 的组合语义
            kwargs["files"] = files
            kwargs["data"] = data
        elif json_body is not None:
            kwargs["json"] = json_body
        try:
            return await client.request(method, path, headers=headers, params=params, **kwargs)
        except httpx.TimeoutException:
            raise FundSiteError("timeout", retryable=True, reconcile=True) from None
        except httpx.HTTPError:
            raise FundSiteError("network", retryable=True, reconcile=True) from None

    def _decode(self, response: httpx.Response) -> Mapping:
        """解析统一信封；成功判据只看 ``code == 200``（非 JSON / 非对象一律拒绝）。"""
        try:
            payload = response.json()
        except ValueError:
            raise self._error_from(response, response.status_code) from None
        if not isinstance(payload, Mapping):
            raise FundSiteError("invalid_response", status=int(response.status_code))
        code = payload.get("code")
        if isinstance(code, bool) or not isinstance(code, int):
            raise FundSiteError("invalid_response", status=int(response.status_code))
        return payload

    async def _api(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        params: Mapping | None = None,
        files: object | None = None,
        data: Mapping | None = None,
        retry_auth: bool = True,
    ) -> Mapping:
        """请求并在 ``code != 200`` 时抛错；401 时重登一次再重试一次。

        401 在服务端是**执行之前**的拒绝，重登后重放同一请求不会造成重复副作用；
        其余错误一律如实抛给调用方（未知结果带 ``reconcile=True``，由调用方对账，
        本层绝不自动重试有副作用的请求）。
        """
        response = await self._request(
            method, path, json_body=json_body, params=params, files=files, data=data
        )
        payload = self._decode(response)
        if payload["code"] == 401 and retry_auth:
            await self._relogin()
            response = await self._request(
                method, path, json_body=json_body, params=params, files=files, data=data
            )
            payload = self._decode(response)
        if payload["code"] != 200:
            raise self._error_from(response, payload["code"])
        return payload

    async def _get_text(self, path: str) -> str:
        """取页面 HTML（非信封）；用于练手盘快照。"""
        response = await self._request("GET", path)
        status = int(response.status_code)
        if status != 200:
            raise self._error_from(response, status)
        content = response.content
        if len(content) > _MAX_SNAPSHOT_BYTES:
            raise FundSiteError("snapshot_too_large", status=status)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            raise FundSiteError("snapshot_unavailable", status=status) from None

    def _error_from(self, response: httpx.Response, code: int) -> FundSiteError:
        """把站点的 code 映射成稳定的失败类别。

        ``str(exc)`` 与 repr 都不含上游 ``message``：错误文案可能回显金额、备注甚至
        凭据，绝不能进异常链（docs/bot/fish-bot.md §5）。429 带 ``Retry-After``。
        """
        status = int(response.status_code)
        retry_after = _parse_retry_after(response) if code == 429 else None
        if code == 0:
            return FundSiteError("network", retryable=True, reconcile=True)
        if code == 400:
            return FundSiteError("invalid_request", status=code)
        if code == 401:
            return FundSiteError("unauthorized", status=code)
        if code == 403:
            return FundSiteError("forbidden", status=code)
        if code == 404:
            return FundSiteError("not_found", status=code)
        if code == 409:
            return FundSiteError("conflict", status=code)
        if code == 429:
            return FundSiteError("rate_limited", status=code, retryable=True, retry_after=retry_after)
        if code >= 500:
            # 5xx 不代表「这笔没发生」：服务端可能已经动过账、动过仓。
            return FundSiteError("server_error", status=code, retryable=True, reconcile=True)
        return FundSiteError("unexpected_status", status=code if code else status)

    def _store_cookie(self, response: httpx.Response) -> None:
        """从 ``Set-Cookie`` 取出会话值；不依赖 httpx 的 cookie jar。

        会话值**只留在内存属性**；同时清空即可丢弃 jar 里的副本，避免两处来源互相
        覆盖。"""
        # MockTransport 的响应可能没有绑定 request，此时 .cookies 不可用；直接解析
        # Set-Cookie 头是两条路都成立的做法（本方法唯一的取 Cookie 口径）。
        cookie: str | None = None
        for header in response.headers.get_list("set-cookie"):
            match = re.search(rf"{SESSION_COOKIE_NAME}=([^;]+)", header)
            if match:
                cookie = match.group(1)
                break
        if not cookie:
            raise FundSiteError("missing_session_cookie", status=200)
        self._session_cookie = cookie
        if self._client is not None:
            self._client.cookies.clear()

    @staticmethod
    def _channel_messages_path(channel_id: str) -> str:
        if not isinstance(channel_id, str) or _CHANNEL_RE.fullmatch(channel_id) is None:
            raise FundSiteError("invalid_channel")
        return f"{_CHANNELS_PREFIX}/{channel_id}/messages"
