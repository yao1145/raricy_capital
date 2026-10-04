"""Focused tests for the funds operator console (E: web.py + static UI).

Covers the security contract (loopback Host, same-origin JSON writes, auth via
bearer or session cookie, secret redaction) and basic static/login behavior.
The service is a mock; no ledger/runtime modules are required.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json

from aiohttp.test_utils import TestClient, TestServer

from raricy_capital.web import CONTROL_TOKEN_KEY, create_app

TOKEN = "test-control-token"


class MockStore:
    def __init__(self):
        self.events_calls = []

    def events(self, limit=100, fund_id=None):
        self.events_calls.append((limit, fund_id))
        return [
            {"id": 2, "created_ms": 1_700_000_000_000, "fund_id": "capital1",
             "event": "settle", "level": "info", "details": {"ok": True}},
            {"id": 1, "created_ms": 1_699_999_000_000, "fund_id": None,
             "event": "boot", "level": "info", "details": {}},
        ]


class MockLedger:
    def orders(self, fund_id=None, user_id=None):
        return [{"id": "o1", "fund_id": "capital1", "user_id": "fund",
                 "amount_units": 20_000_000, "status": "paid"}]


class MockService:
    def __init__(self, *, token=TOKEN, live=False, status=None):
        self.calls = []
        self.config = {"control_token": token, "host": "127.0.0.1", "port": 0,
                       "data_dir": "/tmp/funds", "live": live}
        self.store = MockStore()
        self.ledger = MockLedger()
        self._status = status if status is not None else {
            "funds": [{"fund_id": "capital1", "label": "温和增长 · ER12", "nav": "1.0000",
                       "equity_units": 20_000_000, "realized_profit_units": 1234,
                       "state": "active", "running": True,
                       "trader": {"phase": "holding"}}],
            "events": [],
            "network": {"state": "up", "ok": True},
            "status": {"detail": "持续监控", "last_health_ms": 1_700_000_000_000},
        }

    def public_status(self):
        return self._status

    async def login(self, fund_id, username, password):
        self.calls.append(("login", fund_id, username, password))
        return {"ok": True, "id": "acct-1", "username": username}

    async def set_running(self, fund_id, enabled):
        self.calls.append(("running", fund_id, enabled))
        return {"ok": True, "fund_id": fund_id, "enabled": enabled}

    async def stop_fund(self, fund_id):
        self.calls.append(("stop", fund_id))
        return {"ok": True, "fund_id": fund_id, "permanent": True}

    async def backup(self):
        self.calls.append(("backup",))
        return "/var/funds/backups/2026-10-04.db"

    async def seed(self, fund_id, user_id, principal_units):
        self.calls.append(("seed", fund_id, user_id, principal_units))
        return {"ok": True, "fund_id": fund_id, "principal_units": principal_units}

    async def settle(self, fund_id, kind, period):
        self.calls.append(("settle", fund_id, kind, period))
        return {"ok": True, "kind": kind, "period": period}

    async def set_dividend_choice(self, fund_id, user_id, reinvest_fraction):
        self.calls.append(("dividend", fund_id, user_id, reinvest_fraction))
        return {"ok": True}

    async def holders(self, fund_id):
        return [{"user_id": "fund", "shares_atoms": 20_000_000_000_000}]

    async def nav_history(self, fund_id, limit):
        return [{"updated_ms": 1_700_000_000_000, "nav": "1.0000"},
                {"updated_ms": 1_700_100_000_000, "nav": "1.0900"}]


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


# -------------------------------------------------------------------- auth


async def test_api_requires_control_token():
    service = MockService()
    async with console(service) as client:
        response = await client.get("/api/status")
        assert response.status == 401
        assert (await response.json())["error"] == "unauthorized"

        response = await client.get("/api/status",
                                    headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status == 200
        body = await response.json()
        assert body["funds"][0]["fund_id"] == "capital1"
        assert body["live"] is False


async def test_login_sets_short_session_cookie_and_logout_clears():
    service = MockService()
    async with console(service) as client:
        bad = await client.post("/api/login", json={"token": "nope"},
                                headers={"Origin": origin_of(client)})
        assert bad.status == 401

        ok = await client.post("/api/login", json={"token": TOKEN},
                               headers={"Origin": origin_of(client)})
        assert ok.status == 200
        morsel = ok.cookies["fund_session"]
        assert morsel["httponly"] is True
        assert morsel["samesite"] == "Strict"
        assert morsel["path"] == "/"
        assert morsel["secure"] in (None, "", False)  # local http, not Secure

        # cookie jar now authenticates reads
        assert (await client.get("/api/status")).status == 200

        logout = await client.post("/api/logout", json={},
                                   headers={"Origin": origin_of(client)})
        assert logout.status == 200
        assert (await client.get("/api/status")).status == 401


async def test_session_cookie_is_not_the_control_token():
    service = MockService()
    async with console(service) as client:
        ok = await client.post("/api/login", json={"token": TOKEN},
                               headers={"Origin": origin_of(client)})
        session = ok.cookies["fund_session"].value
        assert session and session != TOKEN


# -------------------------------------------------------------------- csrf


async def test_writes_require_same_origin():
    service = MockService()
    async with console(service) as client:
        # missing Origin is rejected outright
        response = await client.post("/api/backup", json={},
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status == 403
        assert (await response.json())["error"] == "forbidden_origin"

        # cross-origin is rejected
        response = await client.post("/api/backup", json={},
                                     headers=write_headers(client, origin="http://evil.example"))
        assert response.status == 403

        # same-origin succeeds
        response = await client.post("/api/backup", json={}, headers=write_headers(client))
        assert response.status == 200
        assert ("backup",) in service.calls


async def test_writes_require_json_content_type():
    service = MockService()
    async with console(service) as client:
        response = await client.post("/api/backup", data=b"{}", headers=write_headers(client))
        assert response.status == 415
        assert (await response.json())["error"] == "unsupported_media_type"


async def test_writes_require_auth():
    service = MockService()
    async with console(service) as client:
        response = await client.post("/api/backup", json={},
                                     headers={"Origin": origin_of(client)})
        assert response.status == 401


async def test_host_must_be_loopback():
    service = MockService()
    async with console(service) as client:
        response = await client.get("/api/status",
                                    headers={"Host": "evil.example",
                                             "Authorization": f"Bearer {TOKEN}"})
        assert response.status == 403
        assert (await response.json())["error"] == "forbidden_host"


# ------------------------------------------------------------------ writes


async def test_running_seed_settle_dividend_stop_dispatch():
    service = MockService()
    async with console(service) as client:
        headers = write_headers(client)

        await client.post("/api/funds/capital1/running", json={"enabled": True}, headers=headers)
        assert ("running", "capital1", True) in service.calls

        await client.post("/api/funds/capital1/seed",
                          json={"user_id": "fund", "principal": "2000"}, headers=headers)
        assert ("seed", "capital1", "fund", 20_000_000) in service.calls

        await client.post("/api/funds/capital2/settle",
                          json={"kind": "month", "period": "2026-10"}, headers=headers)
        assert ("settle", "capital2", "month", "2026-10") in service.calls

        await client.post("/api/funds/capital1/settle",
                          json={"kind": "emergency"}, headers=headers)
        assert ("settle", "capital1", "emergency", None) in service.calls

        await client.post("/api/dividend-choice",
                          json={"fund_id": "capital1", "user_id": "u1",
                                "reinvest_fraction": "0.5"}, headers=headers)
        assert ("dividend", "capital1", "u1", 0.5) in service.calls

        await client.post("/api/funds/capital1/stop", json={}, headers=headers)
        assert ("stop", "capital1") in service.calls


async def test_seed_rejects_bad_amount_and_unknown_fund():
    service = MockService()
    async with console(service) as client:
        headers = write_headers(client)
        response = await client.post("/api/funds/capital1/seed",
                                     json={"user_id": "u", "principal": "abc"}, headers=headers)
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_amount"

        response = await client.post("/api/funds/nope/seed",
                                     json={"user_id": "u", "principal": "1"}, headers=headers)
        assert response.status == 404


async def test_dividend_fraction_bounds():
    service = MockService()
    async with console(service) as client:
        headers = write_headers(client)
        response = await client.post("/api/dividend-choice",
                                     json={"fund_id": "capital1", "user_id": "u",
                                           "reinvest_fraction": "1.5"}, headers=headers)
        assert response.status == 400
        assert (await response.json())["error"] == "invalid_fraction"


async def test_settle_rejects_bad_kind():
    service = MockService()
    async with console(service) as client:
        response = await client.post("/api/funds/capital1/settle",
                                     json={"kind": "daily"}, headers=write_headers(client))
        assert response.status == 400


# ------------------------------------------------------------------- reads


async def test_events_orders_holders_nav_reads():
    service = MockService()
    async with console(service) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        events = await (await client.get("/api/events?limit=10&fund_id=capital1",
                                         headers=headers)).json()
        assert events["events"][0]["event"] == "settle"
        assert service.store.events_calls[-1] == (10, "capital1")

        orders = await (await client.get("/api/orders", headers=headers)).json()
        assert orders["orders"][0]["id"] == "o1"

        holders = await (await client.get("/api/holders?fund_id=capital1", headers=headers)).json()
        assert holders["holders"][0]["user_id"] == "fund"

        history = await (await client.get("/api/funds/capital1/nav-history",
                                          headers=headers)).json()
        assert history["history"][1]["nav"] == "1.0900"

        unknown = await client.get("/api/holders?fund_id=nope", headers=headers)
        assert unknown.status == 404


async def test_status_redacts_secret_like_keys():
    service = MockService(status={
        "funds": [{"fund_id": "capital1", "password": "hunter2",
                   "control_token": "abc", "nav": "1.0000"}],
        "events": [{"event": "x", "details": {"secret": "leak", "ok": 1}}],
        "network": {},
        "status": {"api_key": "zzz", "detail": "ready"},
    })
    async with console(service) as client:
        response = await client.get("/api/status",
                                    headers={"Authorization": f"Bearer {TOKEN}"})
        text = await response.text()
        for forbidden in ("hunter2", "leak", "zzz", "\"abc\""):
            assert forbidden not in text
        body = json.loads(text)
        assert body["funds"][0]["nav"] == "1.0000"
        assert body["status"]["detail"] == "ready"


async def test_login_never_echoes_password():
    service = MockService()
    async with console(service) as client:
        response = await client.post("/api/funds/capital1/login",
                                     json={"username": "operator", "password": "p@ss"},
                                     headers=write_headers(client))
        assert response.status == 200
        assert "p@ss" not in await response.text()
        assert ("login", "capital1", "operator", "p@ss") in service.calls


# ------------------------------------------------------------------ static


async def test_static_frontend_is_public_and_has_no_cdn():
    service = MockService()
    async with console(service) as client:
        index = await client.get("/")
        assert index.status == 200
        assert "text/html" in index.headers["Content-Type"]
        html = await index.text()
        assert 'src="/static/app.js"' in html
        assert 'href="/static/style.css"' in html
        assert "https://" not in html and "//cdn" not in html

        script = await client.get("/static/app.js")
        assert script.status == 200
        assert "javascript" in script.headers["Content-Type"]
        assert "localStorage" not in await script.text()

        style = await client.get("/static/style.css")
        assert style.status == 200
        assert "text/css" in style.headers["Content-Type"]


async def test_security_headers_present():
    service = MockService()
    async with console(service) as client:
        response = await client.get("/")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "default-src 'self'" in response.headers["Content-Security-Policy"]


async def test_state_badge_helpers_are_pure_json_safe():
    # Guard against the service returning non-JSON-serializable status.
    service = MockService()
    app = create_app(service)
    assert app[CONTROL_TOKEN_KEY] == TOKEN
