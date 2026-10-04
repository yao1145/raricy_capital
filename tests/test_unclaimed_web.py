"""未认领款人工核对的管理端接口（web.py）。

只钉 HTTP 契约与安全边界：路由形状、过滤总量、鉴权/同源/JSON 守卫、严格请求体
（拒绝任何自称权威的字段）、以及**审计操作人由服务端从已认证凭据派生**（不透明
摘要，永不是原始令牌/Cookie）。服务端用替身，不触碰账簿与站点。
"""
from __future__ import annotations

import json
import re
from contextlib import asynccontextmanager

from aiohttp.test_utils import TestClient, TestServer

from raricy_capital.web import create_app

TOKEN = "test-control-token"
FUND = "capital1"
UID = "abc123def456"
SUB = "capital1:0123456789ab"

ROWS = [
    {"id": UID, "fund_id": FUND, "from_user_id": "u_alice", "amount_units": 42_000,
     "note": "写错的附言", "status": "unclaimed", "occurred_ms": 1_760_000_000_000,
     "transfer_id": "a1b2c3d4e5f60718", "version": 0},
    {"id": "fedcba987654", "fund_id": FUND, "from_user_id": "u_bob", "amount_units": 10_000,
     "note": "", "status": "unclaimed", "occurred_ms": 1_760_000_100_000,
     "transfer_id": "ffffffffffffffff", "version": 0},
]
DETAIL = {
    "id": UID, "fund_id": FUND, "from_user_id": "u_alice", "amount_units": 42_000,
    "note": "写错的附言", "transfer_id": "a1b2c3d4e5f60718", "occurred_ms": 1_760_000_000_000,
    "transaction_row_id": 42, "status": "unclaimed", "version": 0,
    "resolution_status": "unclaimed",
    "candidates": [{"id": SUB, "user_id": "u_alice", "total_units": 42_000, "status": "pending"}],
    "history": [],
}


class MockService:
    def __init__(self, *, live=False, rows=None, detail=None, preview=None, record=None):
        self.calls = []
        self.config = {"control_token": TOKEN, "host": "127.0.0.1", "port": 0,
                       "data_dir": "/tmp/funds", "live": live}
        self.rows = ROWS if rows is None else rows
        self.detail = DETAIL if detail is None else detail
        self.preview = preview if preview is not None else {
            "eligible": True, "errors": [], "action": "link", "record": DETAIL,
            "subscription": DETAIL["candidates"][0]}
        self.record = record if record is not None else {
            "id": UID, "status": "linked", "version": 1}

    def public_status(self):
        return {"funds": [], "events": [], "network": {"state": "up", "ok": True},
                "status": {"detail": "持续监控"}}

    def unclaimed(self, fund_id=None, status=None):
        self.calls.append(("list", fund_id, status))
        return self.rows

    def unclaimed_detail(self, fund_id, unclaimed_id):
        self.calls.append(("detail", fund_id, unclaimed_id))
        return self.detail

    async def preview_unclaimed(self, fund_id, unclaimed_id, action, subscription_id=None):
        self.calls.append(("preview", fund_id, unclaimed_id, action, subscription_id))
        return self.preview

    async def resolve_unclaimed(self, fund_id, unclaimed_id, action, *, version, reason,
                                actor, subscription_id=None):
        self.calls.append(("resolve", fund_id, unclaimed_id, action, version, reason, actor,
                           subscription_id))
        return self.record


@asynccontextmanager
async def console(service):
    client = TestClient(TestServer(create_app(service)))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


def origin_of(client):
    return f"http://{client.server.host}:{client.server.port}"


def write_headers(client, *, token=TOKEN, origin=None):
    headers = {"Origin": origin if origin is not None else origin_of(client)}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def read_headers(*, token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def last_call(service, name):
    return [call for call in service.calls if call[0] == name][-1]


# ── 列表与详情（只读）───────────────────────────────────────────────────────


async def test_unclaimed_list_passes_filters_and_returns_filtered_totals():
    service = MockService()
    async with console(service) as client:
        response = await client.get(f"/api/unclaimed?fund_id={FUND}&status=unclaimed",
                                    headers=read_headers())
        assert response.status == 200
        body = await response.json()
        assert body["count"] == 2
        assert body["total_units"] == 52_000
        assert [row["id"] for row in body["unclaimed"]] == [UID, "fedcba987654"]
        assert last_call(service, "list") == ("list", FUND, "unclaimed")

        response = await client.get("/api/unclaimed", headers=read_headers())
        assert response.status == 200
        assert last_call(service, "list") == ("list", None, None)


async def test_unclaimed_list_rejects_unknown_fund_and_junk_status():
    service = MockService()
    async with console(service) as client:
        response = await client.get("/api/unclaimed?fund_id=nope", headers=read_headers())
        assert response.status == 404
        assert (await response.json())["error"] == "unknown_fund"

        response = await client.get("/api/unclaimed?status=UNCLAIMED%20OR%201",
                                    headers=read_headers())
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_field"
        assert not [call for call in service.calls if call[0] == "list"]


async def test_unclaimed_reads_require_auth():
    service = MockService()
    async with console(service) as client:
        assert (await client.get("/api/unclaimed")).status == 401
        assert (await client.get(f"/api/funds/{FUND}/unclaimed/{UID}")).status == 401


async def test_unclaimed_detail_wraps_record_and_validates_id():
    service = MockService()
    async with console(service) as client:
        response = await client.get(f"/api/funds/{FUND}/unclaimed/{UID}", headers=read_headers())
        assert response.status == 200
        body = await response.json()
        assert body["record"]["transfer_id"] == "a1b2c3d4e5f60718"
        assert body["record"]["version"] == 0
        assert last_call(service, "detail") == ("detail", FUND, UID)

        bad = await client.get(f"/api/funds/{FUND}/unclaimed/{UID}%20x", headers=read_headers())
        assert bad.status == 400
        unknown = await client.get("/api/funds/nope/unclaimed/abc", headers=read_headers())
        assert unknown.status == 404


# ── 预览 ────────────────────────────────────────────────────────────────────


async def test_preview_requires_auth_same_origin_and_json():
    service = MockService()
    async with console(service) as client:
        path = f"/api/funds/{FUND}/unclaimed/{UID}/preview"
        response = await client.post(path, json={"action": "link"},
                                     headers={"Origin": origin_of(client)})
        assert response.status == 401

        response = await client.post(path, json={"action": "link"}, headers=read_headers())
        assert response.status == 403
        assert (await response.json())["error"] == "forbidden_origin"

        response = await client.post(path, data=json.dumps({"action": "link"}),
                                     headers=write_headers(client))
        assert response.status == 415
        assert (await response.json())["error"] == "unsupported_media_type"


async def test_preview_response_shape_and_live_flag():
    service = MockService(live=True)
    async with console(service) as client:
        response = await client.post(
            f"/api/funds/{FUND}/unclaimed/{UID}/preview",
            json={"action": "link", "subscription_id": SUB}, headers=write_headers(client))
        assert response.status == 200
        body = await response.json()
        assert body["live"] is True
        assert body["preview"]["eligible"] is True
        assert body["preview"]["subscription"]["id"] == SUB
        assert last_call(service, "preview") == ("preview", FUND, UID, "link", SUB)


async def test_preview_validates_action_and_rejects_authority_fields():
    service = MockService()
    async with console(service) as client:
        path = f"/api/funds/{FUND}/unclaimed/{UID}/preview"
        headers = write_headers(client)

        response = await client.post(path, json={"action": "delete"}, headers=headers)
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_action"

        response = await client.post(path, json={"subscription_id": SUB}, headers=headers)
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_action"

        # 伪造的“权威字段”一律拒绝，且不落任何处理意图。
        for forged in ({"action": "link", "amount_units": 1},
                       {"action": "refund", "verified_transfer": {"transfer_id": "x"}},
                       {"action": "link", "actor": "boss"},
                       {"action": "link", "live": True},
                       {"action": "link", "note": "x"},
                       {"action": "link", "subscription_id": "a b"}):
            response = await client.post(path, json=forged, headers=headers)
            assert response.status == 400, forged
            assert (await response.json())["error"] == "invalid_field", forged
        assert not [call for call in service.calls if call[0] == "preview"]


# ── 确认处理 ────────────────────────────────────────────────────────────────


async def test_resolve_derives_opaque_actor_from_authenticated_session():
    service = MockService(live=True)
    async with console(service) as client:
        login = await client.post("/api/login", json={"token": TOKEN},
                                  headers={"Origin": origin_of(client)})
        session = login.cookies["fund_session"].value
        response = await client.post(
            f"/api/funds/{FUND}/unclaimed/{UID}/resolve",
            json={"action": "link", "version": 0, "reason": "附言填错，核对到账后关联",
                  "subscription_id": SUB},
            headers=write_headers(client, token=None))
        assert response.status == 200
        assert (await response.json())["record"]["status"] == "linked"

        call = last_call(service, "resolve")
        actor = call[6]
        assert re.fullmatch(r"session-[0-9a-f]{24}", actor)
        assert actor != session and session not in actor
        assert call[4] == 0 and call[5] == "附言填错，核对到账后关联" and call[7] == SUB

        # 同一会话派生同一个审计 ID（可追踪），换会话即不同。
        await client.post(f"/api/funds/{FUND}/unclaimed/{UID}/resolve",
                          json={"action": "link", "version": 1, "reason": "再次确认"},
                          headers=write_headers(client, token=None))
        assert last_call(service, "resolve")[6] == actor
        await client.post("/api/logout", json={}, headers={"Origin": origin_of(client)})
        await client.post("/api/login", json={"token": TOKEN},
                          headers={"Origin": origin_of(client)})
        await client.post(f"/api/funds/{FUND}/unclaimed/{UID}/resolve",
                          json={"action": "link", "version": 2, "reason": "新会话确认"},
                          headers=write_headers(client, token=None))
        assert last_call(service, "resolve")[6] != actor


async def test_resolve_via_bearer_token_uses_token_derived_actor():
    service = MockService(live=True)
    async with console(service) as client:
        response = await client.post(
            f"/api/funds/{FUND}/unclaimed/{UID}/resolve",
            json={"action": "refund", "version": 0, "reason": "重复付款原路退回"},
            headers=write_headers(client))
        assert response.status == 200
        actor = last_call(service, "resolve")[6]
        assert re.fullmatch(r"token-[0-9a-f]{24}", actor)
        body = await response.text()
        assert TOKEN not in body and actor not in body


async def test_resolve_rejects_forged_actor_and_authority_fields():
    service = MockService(live=True)
    async with console(service) as client:
        path = f"/api/funds/{FUND}/unclaimed/{UID}/resolve"
        headers = write_headers(client)
        for forged in ({"action": "refund", "version": 0, "reason": "ok", "actor": "boss"},
                       {"action": "refund", "version": 0, "reason": "ok", "amount_units": 1},
                       {"action": "refund", "version": 0, "reason": "ok",
                        "verified_transfer": {"from_user_id": "u_alice"}},
                       {"action": "refund", "version": 0, "reason": "ok", "user_id": "u_alice"}):
            response = await client.post(path, json=forged, headers=headers)
            assert response.status == 400, forged
            assert (await response.json())["error"] == "invalid_field"
        assert not [call for call in service.calls if call[0] == "resolve"]


async def test_resolve_validates_action_version_reason_and_subscription():
    service = MockService(live=True)
    async with console(service) as client:
        path = f"/api/funds/{FUND}/unclaimed/{UID}/resolve"
        headers = write_headers(client)
        cases = [
            ({"action": "delete", "version": 0, "reason": "ok"}, "invalid_action"),
            ({"action": "refund", "version": True, "reason": "ok"}, "invalid_field"),
            ({"action": "refund", "version": -1, "reason": "ok"}, "invalid_field"),
            ({"action": "refund", "reason": "ok"}, "invalid_field"),
            ({"action": "refund", "version": 0, "reason": ""}, "invalid_field"),
            ({"action": "refund", "version": 0, "reason": "   "}, "invalid_field"),
            ({"action": "refund", "version": 0, "reason": "x" * 501}, "invalid_field"),
            ({"action": "refund", "version": 0, "reason": "ok", "subscription_id": "a b"},
             "invalid_field"),
            ({"action": "refund", "version": 0, "reason": "ok", "subscription_id": 5},
             "invalid_field"),
        ]
        for payload, code in cases:
            response = await client.post(path, json=payload, headers=headers)
            assert response.status == 400, payload
            assert (await response.json())["error"] == code, payload
        assert not [call for call in service.calls if call[0] == "resolve"]


async def test_service_fault_maps_to_stable_json_error_without_leaking_details():
    service = MockService(live=False)

    async def explode(*_args, **_kwargs):
        from raricy_capital.contracts import FundError
        raise FundError("live_required")

    service.resolve_unclaimed = explode
    async with console(service) as client:
        response = await client.post(
            f"/api/funds/{FUND}/unclaimed/{UID}/resolve",
            json={"action": "refund", "version": 0, "reason": "退回"},
            headers=write_headers(client))
        assert response.status == 400
        assert (await response.json())["error"] == "live_required"
        assert TOKEN not in await response.text()
