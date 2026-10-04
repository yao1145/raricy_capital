"""未认领款人工核对：服务层独立权威回查（runtime.py + client.py 规范化）。

覆盖契约里属于本执行者的部分：

* ``FundService.unclaimed/unclaimed_detail`` 只读、直通账簿；
* ``preview_unclaimed`` 在数据库事务外重新读站点权威到账行；
* ``resolve_unclaimed`` 先过 ``live`` 总闸（未开则不读站点、不落账），再核对，
  最后把 ``verified_transfer`` 交给账簿 CAS；
* 回查用**独立的有界游标扫描**，绝不改 ``pay_cursors``；
* ``transaction_row_id`` 走快速定位，缺失时退回从 0 起的有界扫描，扫不完整就保留；
* 规范化新增 ``to_user_id`` / ``transaction_row_id``，不改既有字段。

前一半用 StubLedger 只钉服务层接线（不依赖尚未落地的账簿方法），最后一段是
真实 ``FundLedger`` 的端到端集成（与假站点客户端配合）。
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from raricy_capital.client import SESSION_COOKIE_NAME, FundSiteClient, FundSiteError
from raricy_capital.config import FundConfig
from raricy_capital.contracts import FundError, now_ms as real_now_ms
from raricy_capital.runtime import FundService, RECEIPT_SCAN_MAX_PAGES

UTC = timezone.utc
FUND = "capital1"
PAYER = "u_alice"
RECEIPT_ID = "a1b2c3d4e5f60718"
ROW_ID = 42
ARRIVED_MS = int(datetime(2026, 9, 14, 3, 52, 3, tzinfo=UTC).timestamp() * 1000)
NOTE = "sub-1"
AMOUNT = 50_000


def _receipt(**overrides) -> dict:
    row = {
        "id": ROW_ID,
        "transaction_row_id": ROW_ID,
        "transfer_id": RECEIPT_ID,
        "from_user_id": PAYER,
        "to_user_id": "u_fund",
        "amount_units": AMOUNT,
        "note": NOTE,
        "occurred_ms": ARRIVED_MS,
        "type": "transfer_receive",
    }
    row.update(overrides)
    return row


def _legacy_detail() -> dict:
    """历史未认领行：没有补上行号，只能从 0 起扫描。"""
    detail = _receipt()
    detail.pop("transaction_row_id")
    return detail


class FakeSite:
    """只提供运行时回查需要的那一个方法，记录每次游标。"""

    def __init__(self, pages: dict[int, dict] | None = None) -> None:
        self.pages = pages or {}
        self.calls: list[int] = []

    async def transactions(self, since_id: int) -> dict:
        self.calls.append(since_id)
        page = self.pages.get(since_id)
        if page is None:
            return {"transactions": [], "next_cursor": since_id, "has_more": False}
        return page


def _page(rows, *, next_cursor=0, has_more=False) -> dict:
    return {"transactions": list(rows), "next_cursor": next_cursor, "has_more": has_more}


class StubLedger:
    """记录调用参数的账簿替身：服务层接线与账簿内部实现解耦。"""

    def __init__(self, detail: dict) -> None:
        self.detail = detail
        self.calls: list[tuple] = []

    def unclaimed(self, fund_id=None, status=None):
        self.calls.append(("unclaimed", fund_id, status))
        return [dict(self.detail)]

    def unclaimed_detail(self, fund_id, unclaimed_id, now_ms):
        self.calls.append(("detail", fund_id, unclaimed_id))
        return dict(self.detail)

    def preview_unclaimed(self, fund_id, unclaimed_id, action, *, subscription_id=None,
                          verified_transfer=None, now_ms=None):
        self.calls.append(("preview", action, subscription_id, verified_transfer))
        return {"eligible": verified_transfer is not None, "errors": [], "action": action,
                "record": dict(self.detail), "subscription": None}

    def resolve_unclaimed(self, fund_id, unclaimed_id, action, *, version, reason, actor,
                          verified_transfer, now_ms, subscription_id=None):
        self.calls.append(("resolve", action, version, reason, actor, subscription_id,
                           verified_transfer))
        return {"id": unclaimed_id, "status": "linked", "version": version + 1}


def _service(tmp_path, *, live: bool) -> FundService:
    return FundService(FundConfig(tmp_path, live=live))


# ── 服务层接线（StubLedger）─────────────────────────────────────────────────


async def test_unclaimed_list_and_detail_are_read_only_passthrough(tmp_path):
    service = _service(tmp_path, live=False)
    stub = StubLedger(_receipt())
    service.ledger = stub
    try:
        assert service.unclaimed(FUND, "unclaimed") == [stub.detail]
        assert ("unclaimed", FUND, "unclaimed") in stub.calls
        assert service.unclaimed() == [stub.detail]
        assert ("unclaimed", None, None) in stub.calls
        detail = service.unclaimed_detail(FUND, "abc123")
        assert detail["transfer_id"] == RECEIPT_ID
        assert ("detail", FUND, "abc123") in stub.calls
    finally:
        service.store.close()


async def test_resolve_refuses_when_live_disabled_before_site_read_or_write(tmp_path):
    service = _service(tmp_path, live=False)
    stub = StubLedger(_receipt())
    site = FakeSite({0: _page([_receipt()])})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        with pytest.raises(FundError) as exc:
            await service.resolve_unclaimed(FUND, "abc123", "link", version=0,
                                            reason="payee confirmed", actor="session-abc")
        assert exc.value.code == "live_required"
        assert site.calls == []          # 未开 live：没有站点读取
        assert stub.calls == []          # 也没有任何账簿写入/写入意图
    finally:
        service.store.close()


async def test_preview_reports_missing_authoritative_receipt(tmp_path):
    service = _service(tmp_path, live=False)
    stub = StubLedger(_legacy_detail())
    site = FakeSite({0: _page([])})      # 扫到尾部仍是空：权威流水确实不存在
    service.ledger = stub
    service.clients[FUND] = site
    try:
        preview = await service.preview_unclaimed(FUND, "abc123", "link")
        assert preview["eligible"] is False
        assert "receipt_missing" in preview["errors"]
        assert site.calls == [0]
        assert ("preview", "link", None, None) in stub.calls
    finally:
        service.store.close()


async def test_preview_reports_incomplete_scan_and_stays_bounded(tmp_path):
    service = _service(tmp_path, live=False)
    # 历史行没有行号：只能从 0 起有界扫描，扫不完整就必须保留。
    service.ledger = StubLedger(_legacy_detail())
    site = FakeSite({})
    cursor = 0
    for step in range(RECEIPT_SCAN_MAX_PAGES + 5):
        site.pages[cursor] = _page([], next_cursor=cursor + 100, has_more=True)
        cursor += 100
    service.clients[FUND] = site
    try:
        preview = await service.preview_unclaimed(FUND, "abc123", "refund")
        assert preview["eligible"] is False
        assert "scan_incomplete" in preview["errors"]
        assert len(site.calls) == RECEIPT_SCAN_MAX_PAGES
        assert site.calls[0] == 0
    finally:
        service.store.close()


async def test_receipt_mismatch_holds_instead_of_resolving(tmp_path):
    service = _service(tmp_path, live=True)
    stub = StubLedger(_receipt())
    # 同一 transfer_id，但上游金额不同：不得当作同一笔到账去关联/退款。
    site = FakeSite({0: _page([_receipt(amount_units=AMOUNT + 1)])})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        with pytest.raises(FundError) as exc:
            await service.resolve_unclaimed(FUND, "abc123", "refund", version=0,
                                            reason="refund payer", actor="token-deadbeef")
        assert exc.value.code == "receipt_mismatch"
        assert not any(call[0] == 'resolve' for call in stub.calls)
    finally:
        service.store.close()


async def test_resolve_passes_authoritative_tuple_and_audit_fields(tmp_path):
    service = _service(tmp_path, live=True)
    stub = StubLedger(_receipt())
    site = FakeSite({0: _page([_receipt()])})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        result = await service.resolve_unclaimed(FUND, "abc123", "link", version=3,
                                                 reason="  附言填错，人工关联  ", actor="session-0123")
        assert result["status"] == "linked"
        name, action, version, reason, actor, subscription_id, verified = stub.calls[-1]
        assert name == "resolve" and action == "link" and version == 3
        assert reason == "附言填错，人工关联"       # 归一但内容不变
        assert actor == "session-0123"             # 服务层原样透传不透明审计 ID
        assert subscription_id is None
        assert (verified["transfer_id"], verified["from_user_id"],
                verified["amount_units"], verified["note"],
                verified["occurred_ms"]) == (RECEIPT_ID, PAYER, AMOUNT, NOTE, ARRIVED_MS)
        assert verified["transaction_row_id"] == ROW_ID
    finally:
        service.store.close()


async def test_transaction_row_id_fast_path_avoids_full_history_scan(tmp_path):
    service = _service(tmp_path, live=True)
    stub = StubLedger(_receipt())
    site = FakeSite({ROW_ID - 1: _page([_receipt()])})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        await service.resolve_unclaimed(FUND, "abc123", "link", version=0,
                                        reason="verified payer", actor="session-0123")
        assert site.calls == [ROW_ID - 1]   # 从行号前一行取一页即可确认
    finally:
        service.store.close()


async def test_legacy_row_without_row_id_scans_from_zero(tmp_path):
    service = _service(tmp_path, live=True)
    detail = _receipt()
    detail.pop("transaction_row_id")
    stub = StubLedger(detail)
    site = FakeSite({0: _page([], next_cursor=40, has_more=True),
                     40: _page([_receipt()], next_cursor=ROW_ID, has_more=False)})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        await service.resolve_unclaimed(FUND, "abc123", "refund", version=0,
                                        reason="duplicate payment", actor="session-0123")
        assert site.calls == [0, 40]
    finally:
        service.store.close()


async def test_incoming_rows_are_the_only_scan_matches(tmp_path):
    service = _service(tmp_path, live=True)
    stub = StubLedger(_receipt())
    outgoing = _receipt(type="transfer", amount_units=-AMOUNT, note=NOTE)
    site = FakeSite({0: _page([outgoing])})
    service.ledger = stub
    service.clients[FUND] = site
    try:
        with pytest.raises(FundError) as exc:
            await service.resolve_unclaimed(FUND, "abc123", "link", version=0,
                                            reason="verified payer", actor="session-0123")
        assert exc.value.code == "receipt_missing"
    finally:
        service.store.close()


async def test_resolve_validates_arguments_before_any_site_read(tmp_path):
    service = _service(tmp_path, live=True)
    service.ledger = StubLedger(_receipt())
    site = FakeSite({0: _page([_receipt()])})
    service.clients[FUND] = site
    cases = [
        {"action": "delete", "version": 0, "reason": "ok", "actor": "session-1"},
        {"action": "link", "version": 0, "reason": "   ", "actor": "session-1"},
        {"action": "link", "version": 0, "reason": "x" * 501, "actor": "session-1"},
        {"action": "link", "version": 0, "reason": "ok", "actor": ""},
        {"action": "link", "version": 0, "reason": "ok", "actor": "s" * 129},
        {"action": "link", "version": True, "reason": "ok", "actor": "session-1"},
        {"action": "link", "version": -1, "reason": "ok", "actor": "session-1"},
    ]
    try:
        for case in cases:
            with pytest.raises(FundError):
                await service.resolve_unclaimed(FUND, "abc123", case["action"],
                                                version=case["version"], reason=case["reason"],
                                                actor=case["actor"])
        assert site.calls == []
    finally:
        service.store.close()


async def test_preview_requires_logged_in_account(tmp_path):
    service = _service(tmp_path, live=False)
    service.ledger = StubLedger(_receipt())
    try:
        with pytest.raises(FundError) as exc:
            await service.preview_unclaimed(FUND, "abc123", "link")
        assert exc.value.code == "account_login_required"
    finally:
        service.store.close()


async def test_site_failure_during_resolve_is_refused_not_assumed(tmp_path):
    service = _service(tmp_path, live=True)
    stub = StubLedger(_receipt())
    service.ledger = stub
    service.clients[FUND] = _FailingSite()
    try:
        with pytest.raises(FundError) as exc:
            await service.resolve_unclaimed(FUND, "abc123", "refund", version=0,
                                            reason="refund payer", actor="session-0123")
        assert exc.value.code == "timeout"
        assert not any(call[0] == 'resolve' for call in stub.calls)
    finally:
        service.store.close()


class _FailingSite:
    async def transactions(self, since_id):
        raise FundSiteError("timeout", retryable=True, reconcile=True)


# ── FundSiteClient 规范化（本执行者负责的增量）──────────────────────────────

LOGIN_ENV = {"code": 200, "message": "ok",
             "user": {"id": "u_fund", "username": "fundbot", "role": "core"}}


def _site_transport(rows) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            return httpx.Response(200, json=LOGIN_ENV,
                                  headers=[("set-cookie", f"{SESSION_COOKIE_NAME}=s1; Path=/")])
        if path == "/api/auth/me":
            return httpx.Response(200, json=LOGIN_ENV)
        return httpx.Response(200, json={"code": 200, "message": "ok", "mode": "cursor",
                                         "next_cursor": 9, "has_more": False,
                                         "transactions": rows})

    return httpx.MockTransport(handler)


async def test_normalization_adds_refund_recipient_and_stable_row_id():
    rows = [
        {"id": 7, "amount": -5, "type": "transfer", "description": "转给「alice」：refund",
         "related_user_id": "u_alice", "transfer_id": "ffffffffffffffff",
         "created_at": "2026-09-14T11:52:03.000Z"},
        {"id": 8, "amount": 5, "type": "transfer_receive", "description": "收到「alice」的转账：sub-1",
         "related_user_id": "u_alice", "transfer_id": "a1b2c3d4e5f60718",
         "created_at": "2026-09-14T11:52:03.000Z"},
    ]
    client = FundSiteClient("https://raricy.test", "fundbot", "hunter2",
                            transport=_site_transport(rows))
    await client.start()
    try:
        await client.login()
        page = await client.transactions(0)
    finally:
        await client.close()
    outgoing, incoming = page["transactions"]
    # 退款对账：支出行的收款人取站点 related_user_id；收款行的收款人是本账户。
    assert outgoing["to_user_id"] == "u_alice"
    assert outgoing["from_user_id"] == "u_fund"
    assert incoming["from_user_id"] == "u_alice"
    assert incoming["to_user_id"] == "u_fund"
    # 稳定行键与既有 id 一致，供未认领款补链与快速回查。
    assert outgoing["transaction_row_id"] == 7
    assert incoming["transaction_row_id"] == incoming["id"] == 8
    assert incoming["note"] == "sub-1"


# ── 端到端集成：真实账簿 + 假站点客户端 ────────────────────────────────────


async def test_wrong_note_link_keeps_equity_neutral_end_to_end(tmp_path):
    service = _service(tmp_path, live=True)
    try:
        ledger = service.ledger
        # 同基金、同付款人、同金额的有效申购单，只是附言填错 -> 落到未认领款。
        # 用真实“现在”为基准：本用例只验证核对流程，不越过任何截止时点。
        stamp = real_now_ms()
        sub = ledger.create_subscription(FUND, PAYER, 40_000, "key-1", stamp - 60_000)
        receipt = _receipt(amount_units=sub["total_units"], note="写错的附言", occurred_ms=stamp)
        result = ledger.receive_transfer(FUND, receipt, stamp)
        assert result["status"] == "unclaimed"

        rows = service.unclaimed(FUND)
        assert len(rows) == 1
        unclaimed_id = rows[0]["id"]
        before = ledger.status(FUND)
        # 收款轮询游标是 payments 工人的状态：人工核对前后都必须原样不动。
        cursor = {"since_id": 7, "online": True, "updated_ms": stamp}
        service.store.put("pay_cursors", FUND, dict(cursor))

        service.clients[FUND] = FakeSite({0: _page([receipt])})
        preview = await service.preview_unclaimed(FUND, unclaimed_id, "link",
                                                  subscription_id=sub["id"])
        assert preview["eligible"] is True
        assert service.store.get("pay_cursors", FUND) == cursor

        detail = service.unclaimed_detail(FUND, unclaimed_id)
        assert detail["version"] == 0
        assert detail["resolution_status"] in ("unclaimed", None)

        resolved = await service.resolve_unclaimed(FUND, unclaimed_id, "link", version=0,
                                                   reason="用户附言填错，核对到账后人工关联",
                                                   actor="session-abcdef",
                                                   subscription_id=sub["id"])
        assert resolved["status"] in ("linked", "received")
        assert service.store.get("pay_cursors", FUND) == cursor

        after = ledger.status(FUND)
        assert after["equity_units"] == before["equity_units"]
        assert after["capital_flows_units"] == before["capital_flows_units"]
        assert after["unclaimed_units"] == before["unclaimed_units"] - receipt["amount_units"]
        assert after["pending_receipts_units"] == before["pending_receipts_units"] + sub["principal_units"]
        assert after["fee_balance_units"] == before["fee_balance_units"] + sub["fee_units"]
        assert ledger.unclaimed(FUND, status='unclaimed') == []
    finally:
        service.store.close()


async def test_resolve_is_single_use_under_version_conflict_end_to_end(tmp_path):
    service = _service(tmp_path, live=True)
    try:
        ledger = service.ledger
        receipt = _receipt(note="无人认领", occurred_ms=real_now_ms())
        ledger.receive_transfer(FUND, receipt, receipt["occurred_ms"])
        unclaimed_id = service.unclaimed(FUND)[0]["id"]
        service.clients[FUND] = FakeSite({0: _page([receipt])})

        first = await service.resolve_unclaimed(FUND, unclaimed_id, "refund", version=0,
                                                reason="重复付款原路退回", actor="session-1")
        assert first["status"] in ("refund_queued", "linked")
        with pytest.raises(FundError):
            await service.resolve_unclaimed(FUND, unclaimed_id, "refund", version=0,
                                            reason="重复点击", actor="session-2")
    finally:
        service.store.close()
