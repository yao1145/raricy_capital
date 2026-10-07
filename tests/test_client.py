"""FundSiteClient 聚焦测试：用 httpx.MockTransport 钉住真实路由形状与幂等语义。

不发起任何真实网络请求（§18 测试约定）。断言的是**站点源码里的字段与状态码**，
不是文档抄本：数据 shapes 逐条对照 data/raricy_fund_source 的 route.ts / service.ts。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from raricy_capital.client import (
    SESSION_COOKIE_NAME,
    SYMBOL,
    FundSiteClient,
    FundSiteError,
)

BASE = "https://raricy.test"
TOKEN = "session-token-abc"
FUND_ID = "u_fund"
PAYER_ID = "u_alice"

UTC = timezone.utc


def _login_env(user_id: str = FUND_ID, username: str = "fundbot") -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": 200, "message": "登录成功", "user": {"id": user_id, "username": username, "role": "core"}},
        headers=[("set-cookie", f"{SESSION_COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; SameSite=Lax")],
    )


def _me_env(user_id: str = FUND_ID, username: str = "fundbot") -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "message": "ok", "user": {"id": user_id, "username": username, "role": "core"}})


def _env(code: int = 200, **fields) -> httpx.Response:
    return httpx.Response(code, json={"code": code, "message": "ok", **fields})


class Site:
    """极小的假站点：按 (method, path) 路由，记录每个请求。"""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], object] = {}
        self.requests: list[httpx.Request] = []
        self.logins = 0

    def on(self, method: str, path: str):
        def deco(fn):
            self.routes[(method, path)] = fn
            return fn

        return deco

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/auth/login":
            self.logins += 1
        fn = self.routes.get((request.method, request.url.path))
        if fn is None:
            return httpx.Response(404, json={"code": 404, "message": "no route"})
        return fn(request)

    def add_login(self, user_id: str = FUND_ID, username: str = "fundbot") -> None:
        self.routes[("POST", "/api/auth/login")] = lambda _r: _login_env(user_id, username)
        self.routes[("GET", "/api/auth/me")] = lambda _r: _me_env(user_id, username)

    def bodies(self) -> list[bytes]:
        return [r.content for r in self.requests]


async def make_client(site: Site, *, login: bool = True) -> FundSiteClient:
    if login:
        site.add_login()
    client = FundSiteClient(BASE, "fundbot", "hunter2", transport=httpx.MockTransport(site))
    await client.start()
    if login:
        await client.login()
    return client


def last_json(site: Site) -> dict:
    return json.loads(site.requests[-1].content.decode("utf-8"))


# ── 登录 / 会话 ──────────────────────────────────────────────────────────────


async def test_login_returns_identity_and_keeps_cookie_in_memory():
    site = Site()
    client = await make_client(site)
    try:
        assert client.user_id == FUND_ID
        assert client.username == "fundbot"
        assert client.session_cookie == TOKEN  # 只存内存，不落盘
        # /api/auth/me 带上了显式 Cookie 头
        me_request = site.requests[-1]
        assert me_request.headers["cookie"] == f"{SESSION_COOKIE_NAME}={TOKEN}"
    finally:
        await client.close()


async def test_login_identity_mismatch_is_rejected():
    site = Site()
    site.routes[("POST", "/api/auth/login")] = lambda _r: _login_env(FUND_ID)
    site.routes[("GET", "/api/auth/me")] = lambda _r: _me_env("u_other")
    client = FundSiteClient(BASE, "fundbot", "hunter2", transport=httpx.MockTransport(site))
    await client.start()
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.login()
        assert excinfo.value.code == "identity_mismatch"
    finally:
        await client.close()


async def test_401_relogins_once_and_retries():
    site = Site()
    site.add_login()
    state = {"n": 0}

    def balance(_r):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(401, json={"code": 401, "message": "请先登录"})
        return _env(200, user_id=FUND_ID, username="fundbot", balance=42.5)

    site.on("POST", "/api/fish/market/balance")(balance)
    client = await make_client(site)
    try:
        assert await client.balance() == 425_000
        assert site.logins == 2  # 初次登录 + 401 后重登一次
    finally:
        await client.close()


# ── 余额 / 流水 / 转账 ───────────────────────────────────────────────────────


async def test_balance_converts_to_integer_units():
    site = Site()
    site.on("POST", "/api/fish/market/balance")(
        lambda _r: _env(200, user_id=FUND_ID, username="fundbot", balance=42.5)
    )
    client = await make_client(site)
    try:
        assert await client.balance() == 425_000
        # 业务请求体里没有密码
        assert b"hunter2" not in site.requests[-1].content
    finally:
        await client.close()


async def test_transactions_normalizes_sender_and_wall_clock():
    site = Site()
    site.on("POST", "/api/fish/market/transactions")(
        lambda _r: _env(
            200,
            mode="cursor",
            next_cursor=123,
            has_more=False,
            transactions=[
                {
                    "id": 123,
                    "amount": 5,
                    "type": "transfer_receive",
                    "description": "收到「alice」的转账：sub-1",
                    "reference_type": "user",
                    "reference_id": "u_alice",
                    "related_user_id": "u_alice",
                    "transfer_id": "a1b2c3d4e5f60718",
                    "created_at": "2026-09-14T11:52:03.000Z",
                },
                {
                    "id": 124,
                    "amount": -1,
                    "type": "transfer",
                    "description": "转给「alice」：wd-1",
                    "related_user_id": "u_alice",
                    "transfer_id": "ffffffffffffffff",
                    "created_at": None,
                },
            ],
        )
    )
    client = await make_client(site)
    try:
        result = await client.transactions(0)
    finally:
        await client.close()

    assert result["next_cursor"] == 123 and result["has_more"] is False
    incoming, outgoing = result["transactions"]
    # 假 Z（UTC+8 墙钟）必须减 8 小时
    expected = int(datetime(2026, 9, 14, 11, 52, 3, tzinfo=UTC).timestamp() * 1000) - 8 * 3600 * 1000
    assert incoming["occurred_ms"] == expected
    assert incoming["from_user_id"] == "u_alice"
    assert incoming["amount_units"] == 50_000
    assert incoming["note"] == "sub-1"
    assert incoming["transfer_id"] == "a1b2c3d4e5f60718"
    # 支出行的 sender 就是本账户；无时间戳允许为 None
    assert outgoing["from_user_id"] == FUND_ID
    assert outgoing["amount_units"] == -10_000
    assert outgoing["occurred_ms"] is None


async def test_transfer_body_and_result():
    site = Site()
    site.on("POST", "/api/fish/market/transfer")(
        lambda _r: _env(
            200,
            amount=1.5,
            balance=41.5,
            recipient={"id": PAYER_ID, "username": "alice"},
            transfer_id="a1b2c3d4e5f60718",
            duplicated=False,
        )
    )
    client = await make_client(site)
    try:
        result = await client.transfer(PAYER_ID, 15000, "  订单  A  ", "wd-20260915-0007")
    finally:
        await client.close()

    assert result == {"transfer_id": "a1b2c3d4e5f60718", "amount_units": 15000, "duplicated": False}
    body = last_json(site)
    assert body == {
        "to_user_id": PAYER_ID,
        "amount": "1.5000",
        "idempotency_key": "wd-20260915-0007",
        "note": "订单 A",  # 空白已按站点口径归一，重试必然字节一致
    }


async def test_transfer_same_key_retry_is_idempotent():
    site = Site()
    calls = {"n": 0}

    def transfer(_r):
        calls["n"] += 1
        return _env(
            200,
            amount=1.5,
            balance=41.5,
            recipient={"id": PAYER_ID, "username": "alice"},
            transfer_id="a1b2c3d4e5f60718",
            duplicated=calls["n"] > 1,
        )

    site.on("POST", "/api/fish/market/transfer")(transfer)
    client = await make_client(site)
    try:
        first = await client.transfer(PAYER_ID, 15000, "订单", "wd-1")
        second = await client.transfer(PAYER_ID, 15000, "订单", "wd-1")
    finally:
        await client.close()

    assert first["duplicated"] is False and second["duplicated"] is True
    # 两次请求体逐字节相同：同键重试的唯一安全前提
    bodies = [r.content for r in site.requests if r.url.path.endswith("/transfer")]
    assert bodies[0] == bodies[1]


async def test_transfer_unknown_result_is_retryable_with_same_key():
    site = Site()
    seen = {"n": 0}

    def transfer(request):
        seen["n"] += 1
        if seen["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return _env(
            200, amount=1.5, balance=41.5, recipient={"id": PAYER_ID, "username": "alice"},
            transfer_id="a1b2c3d4e5f60718", duplicated=False,
        )

    site.on("POST", "/api/fish/market/transfer")(transfer)
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.transfer(PAYER_ID, 15000, "订单", "wd-1")
        err = excinfo.value
        assert err.code == "network" and err.retryable is True and err.reconcile is True
        # 同键重发是安全的 —— 这里模拟对账后重试
        assert (await client.transfer(PAYER_ID, 15000, "订单", "wd-1"))["transfer_id"] == "a1b2c3d4e5f60718"
    finally:
        await client.close()

    bodies = [r.content for r in site.requests if r.url.path.endswith("/transfer")]
    assert bodies[0] == bodies[1]


async def test_transfer_conflict_is_not_retryable():
    site = Site()
    site.on("POST", "/api/fish/market/transfer")(
        lambda _r: httpx.Response(409, json={"code": 409, "message": "该幂等键已用于另一笔转账"})
    )
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.transfer(PAYER_ID, 15000, None, "wd-1")
        assert excinfo.value.code == "conflict" and excinfo.value.retryable is False
    finally:
        await client.close()


# ── 讨论 / 图片 ──────────────────────────────────────────────────────────────


async def test_private_channels_filters_direct():
    site = Site()
    site.on("GET", "/api/chat/poll")(
        lambda _r: _env(
            200,
            channels=[
                {"id": "lobby", "kind": "lobby"},
                {"id": "d_1", "kind": "direct"},
                {"id": "d_2", "kind": "direct"},
            ],
        )
    )
    client = await make_client(site)
    try:
        assert await client.private_channels() == ["d_1", "d_2"]
    finally:
        await client.close()


async def test_fetch_messages_returns_typed_dto():
    site = Site()
    site.on("GET", "/api/chat/channels/d_1/messages")(
        lambda _r: _env(
            200,
            messages=[
                {
                    "id": 10,
                    "channel_id": "d_1",
                    "author": {"id": PAYER_ID, "username": "alice", "avatar_url": "", "is_admin": False},
                    "content": "hi",
                    "created_at": "2026-09-11T20:31:05.000Z",
                }
            ],
        )
    )
    client = await make_client(site)
    try:
        messages = await client.fetch_messages("d_1", after=5)
        assert [m.id for m in messages] == [10]
        assert messages[0].author.id == PAYER_ID
        assert site.requests[-1].url.params["after"] == "5"
    finally:
        await client.close()


async def test_send_message_text_posts_content():
    site = Site()
    site.on("POST", "/api/chat/channels/d_1/messages")(
        lambda _r: _env(200, message={"id": 11, "channel_id": "d_1", "content": "hello"})
    )
    client = await make_client(site)
    try:
        result = await client.send_message("d_1", "hello")
    finally:
        await client.close()
    assert result["id"] == 11
    assert last_json(site) == {"content": "hello"}


async def test_send_message_with_image_uploads_then_references():
    site = Site()
    site.on("POST", "/api/images")(
        lambda _r: _env(200, items=[], failed=[], id="AbCdEf1234", url="/api/images/AbCdEf1234/raw")
    )
    site.on("POST", "/api/chat/channels/d_1/messages")(
        lambda _r: _env(200, message={"id": 12, "channel_id": "d_1", "image": {"id": "AbCdEf1234"}})
    )
    client = await make_client(site)
    try:
        result = await client.send_message("d_1", "支付链接见二维码", image_bytes=b"\x89PNG\r\n\x1a\nfake")
    finally:
        await client.close()
    assert result["id"] == 12
    upload = next(r for r in site.requests if r.url.path == "/api/images")
    assert upload.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in upload.content and b"compress" in upload.content
    assert last_json(site) == {"content": "支付链接见二维码", "image_id": "AbCdEf1234"}


async def test_upload_qr_returns_image_reference():
    site = Site()
    site.on("POST", "/api/images")(
        lambda _r: _env(200, items=[], failed=[], id="AbCdEf1234", url="/api/images/AbCdEf1234/raw")
    )
    client = await make_client(site)
    try:
        ref = await client.upload_qr(b"<svg></svg>", filename="qr.svg")
    finally:
        await client.close()
    assert ref == {"id": "AbCdEf1234", "url": "/api/images/AbCdEf1234/raw"}


# ── 收银台 / 二维码 ──────────────────────────────────────────────────────────


async def test_pay_url_builds_cashier_link():
    site = Site()
    client = await make_client(site)
    try:
        url = await client.pay_url(PAYER_ID, 10_500_000, "订阅 7 月", "sub-202607-0001")
    finally:
        await client.close()
    assert url == (
        f"{BASE}/fish/pay?to=fundbot&amount=1050.0000&order=sub-202607-0001&note=%E8%AE%A2%E9%98%85+7+%E6%9C%88"
    )


async def test_pay_url_rejects_bad_order_note_and_self():
    site = Site()
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as e1:
            await client.pay_url(PAYER_ID, 10_000, None, "bad order")
        assert e1.value.code == "order_key_invalid"
        with pytest.raises(FundSiteError) as e2:
            await client.pay_url(PAYER_ID, 10_000, "x" * 31, "ord-1")
        assert e2.value.code == "note_too_long"
        with pytest.raises(FundSiteError) as e3:
            await client.pay_url(FUND_ID, 10_000, None, "ord-1")
        assert e3.value.code == "self_payment"
    finally:
        await client.close()


def test_qr_png_renders_png_bytes():
    pytest.importorskip("qrcode")
    pytest.importorskip("PIL")
    png = FundSiteClient.qr_png("https://raricy.test/fish/pay?to=fundbot&amount=1")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")


# ── 练手盘 ───────────────────────────────────────────────────────────────────


async def test_quote_returns_price_and_fee():
    site = Site()
    site.on("GET", "/api/fish/trade/quote")(
        lambda _r: _env(
            200, ok=True,
            quotes=[{"symbol": SYMBOL, "price": 68412.35, "stale": False, "age_ms": 3200}],
            fee_rate=0.0002, min_stake=1,
        )
    )
    client = await make_client(site)
    try:
        price, fee = await client.quote()
    finally:
        await client.close()
    assert price == 68412.35 and fee == 0.0002


async def test_quote_stale_is_unavailable():
    site = Site()
    site.on("GET", "/api/fish/trade/quote")(
        lambda _r: _env(
            200, ok=True,
            quotes=[{"symbol": SYMBOL, "price": 1.0, "stale": True, "age_ms": 99999}],
            fee_rate=0.0002, min_stake=1,
        )
    )
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.quote()
        assert excinfo.value.code == "quote_unavailable" and excinfo.value.retryable is True
    finally:
        await client.close()


async def test_candles_keep_exchange_utc_ms():
    open_ms = 1759456800000
    site = Site()
    site.on("GET", "/api/fish/trade/candles")(
        lambda _r: _env(200, ok=True, symbol=SYMBOL, interval="1h", candles=[[open_ms, 68321.5, 68530.0, 68100.2, 68412.35, 128.441]])
    )
    client = await make_client(site)
    try:
        candles = await client.candles()
    finally:
        await client.close()
    assert candles == [(open_ms, 68321.5, 68530.0, 68100.2, 68412.35, 128.441)]  # 不减 8 小时


async def test_buy_parses_position_with_real_utc():
    site = Site()
    site.on("POST", "/api/fish/trade/buy")(
        lambda _r: _env(
            200, balance=32.5, replayed=False,
            position={
                "id": "c1a2b3d4-e5f6", "symbol": SYMBOL, "stake": 10,
                "entry_price": 68412.35, "leverage": 5, "liquidation_price": 54729.88,
                "opened_at": "2026-10-03T14:22:03.000Z",
            },
        )
    )
    client = await make_client(site)
    try:
        result = await client.buy(100_000, 5, "mrk-20261003-0001")
    finally:
        await client.close()
    assert result["position_id"] == "c1a2b3d4-e5f6"
    assert result["stake_units"] == 100_000
    assert result["entry_price"] == 68412.35
    # 站点的 opened_at 来自 nowForDb()（UTC+8 墙钟贴 Z，同流水 created_at），
    # 必须减 8 小时才是真实 UTC 毫秒。
    assert result["opened_ms"] == int(datetime(2026, 10, 3, 14, 22, 3, tzinfo=UTC).timestamp() * 1000) - 8 * 3600 * 1000
    assert last_json(site) == {
        "symbol": SYMBOL, "amount": "10.0000", "leverage": 5, "idempotency_key": "mrk-20261003-0001"
    }


async def test_buy_leverage_only_range_checked():
    # 2026-10-07（d2331679）起服务端不再认白名单，只校验 1–100 整数；4 这种非阶梯值必须放行。
    site = Site()
    site.on("POST", "/api/fish/trade/buy")(
        lambda _r: _env(200, balance=10, replayed=False,
                        position={"id": "c1", "symbol": SYMBOL, "stake": 1,
                                  "entry_price": 1.0, "leverage": 4,
                                  "liquidation_price": 0.0,
                                  "opened_at": "2026-10-03T14:22:03.000Z"})
    )
    client = await make_client(site)
    try:
        result = await client.buy(10_000, 4, "mrk-lev4")
    finally:
        await client.close()
    assert result["leverage"] == 4
    assert last_json(site)["leverage"] == 4


async def test_buy_rejects_leverage_outside_range():
    site = Site()
    client = await make_client(site)
    try:
        for bad in (0, 101, 1000, -3, 2.5, True):
            with pytest.raises(FundSiteError) as excinfo:
                await client.buy(100_000, bad, "mrk-1")
            assert excinfo.value.code == "leverage_invalid"
    finally:
        await client.close()


async def test_sell_parses_liquidated_and_payout():
    site = Site()

    def sell(_r):
        return _env(
            200, position_id="c1", symbol=SYMBOL, payout=0, profit=-10,
            exit_price=54729.88, liquidated=True, balance=22.5, replayed=False,
        )

    site.on("POST", "/api/fish/trade/sell")(sell)
    client = await make_client(site)
    try:
        result = await client.sell("c1")
    finally:
        await client.close()
    assert result["liquidated"] is True
    assert result["payout_units"] == 0 and result["profit_units"] == -100_000
    assert last_json(site) == {"position_id": "c1"}


async def test_sell_unknown_requires_reconcile():
    site = Site()

    def sell(request):
        raise httpx.ReadTimeout("slow", request=request)

    site.on("POST", "/api/fish/trade/sell")(sell)
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.sell("c1")
        assert excinfo.value.code == "timeout"
        assert excinfo.value.reconcile is True  # 未知结果必须对账，不能假定未成交
    finally:
        await client.close()


async def test_snapshot_parses_trade_page_props():
    panel = {
        "balance": 42.5,
        "positions": [
            {
                "id": "p1", "symbol": SYMBOL, "stake": 10, "entryPrice": 68412.35,
                "leverage": 5, "liquidationPrice": 54729.88, "openedAt": "2026-10-03T14:22:03.000Z",
            }
        ],
        "feeRate": 0.0002,
        "minStake": 1,
        "leverageEnabled": True,
    }
    line = "3:" + json.dumps(panel, separators=(",", ":"))
    payload = json.dumps([1, line])
    html = f"<html><script>self.__next_f.push({payload})</script></html>"
    site = Site()
    site.on("GET", "/fish/trade")(lambda _r: httpx.Response(200, text=html))
    client = await make_client(site)
    try:
        snap = await client.snapshot()
    finally:
        await client.close()
    assert snap["balance_units"] == 425_000
    assert snap["min_stake_units"] == 10_000
    # 站点不再下发杠杆白名单：空列表代表「1–100 整数」而不是「无可用杠杆」。
    assert snap["leverage_options"] == []
    assert snap["leverage_enabled"] is True
    pos = snap["positions"][0]
    assert pos["stake_units"] == 100_000
    assert pos["liquidation_price"] == 54729.88
    # 页面 props 里的 openedAt 与流水 created_at 同族（nowForDb 墙钟），减 8 小时。
    assert pos["opened_ms"] == int(datetime(2026, 10, 3, 14, 22, 3, tzinfo=UTC).timestamp() * 1000) - 8 * 3600 * 1000


# ── 错误安全 ─────────────────────────────────────────────────────────────────


async def test_errors_never_leak_secrets_or_upstream_body():
    site = Site()
    site.on("POST", "/api/fish/market/balance")(
        lambda _r: httpx.Response(500, json={"code": 500, "message": "服务器开小差了 hunter2 转账备注"})
    )
    client = await make_client(site)
    try:
        with pytest.raises(FundSiteError) as excinfo:
            await client.balance()
    finally:
        await client.close()
    err = excinfo.value
    assert err.code == "server_error"
    assert err.reconcile is True and err.retryable is True
    assert "hunter2" not in str(err) and "hunter2" not in repr(err)
    assert "转账备注" not in str(err)
