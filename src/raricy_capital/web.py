"""Operator console (aiohttp) for the two funds.

Design contract (frozen):

    create_app(service) -> aiohttp.web.Application

The returned application never binds a socket; the runtime owns the loopback
listener. ``service`` is duck-typed and must provide:

    service.config                         # mapping or object: control_token,
                                           # host, port, data_dir, live
    service.store                          # FundStore
    service.ledger                         # FundLedger
    service.public_status() -> dict        # {funds:[...], events, network, status}
    async service.login(fund_id, username, password) -> dict
    async service.set_running(fund_id, enabled) -> dict
    async service.stop_fund(fund_id) -> dict
    async service.backup() -> path-like

Optional service hooks used when present (graceful empty fallback otherwise):

    async service.seed(fund_id, user_id, principal_units) -> dict
    async service.settle(fund_id, kind, period) -> dict   # kind month|emergency
    async service.set_dividend_choice(fund_id, user_id, reinvest_fraction) -> dict
    async service.orders(fund_id, user_id) -> list[dict]
    async service.holders(fund_id) -> list[dict]
    async service.nav_history(fund_id, limit) -> list[dict]

Security model:
  * every request must carry a loopback Host header (DNS-rebinding guard);
  * every write (non-GET/HEAD/OPTIONS) must present a same-origin ``Origin``
    header and ``application/json`` content type (CSRF guard);
  * all ``/api/*`` routes except login/logout require the control token, either
    as bearer credentials or via a short local session cookie obtained from
    ``POST /api/login``;
  * responses are recursively redacted for secret-looking keys.
"""
from __future__ import annotations

import hmac
import inspect
import json
import logging
import secrets
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .contracts import FundError, POLICIES, money_units, now_ms

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "fund_session"
SESSION_TTL_MS = 8 * 3600 * 1000
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUTH_EXEMPT = frozenset({"/api/login", "/api/logout"})
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "credential",
    "authorization",
    "cookie",
    "private",
    "api_key",
    "apikey",
    "access_key",
)
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
    "frame-ancestors 'none'; object-src 'none'"
)

# Typed application keys. aiohttp raises ``NotAppKeyWarning`` on every string-keyed
# ``app[...]`` access, and this repo runs pytest with ``filterwarnings = ["error"]``,
# so a bare string key fails the suite. Every entry is registered here under a typed
# ``web.AppKey`` and only ever read back through it.
SERVICE_KEY: web.AppKey[object] = web.AppKey("service", object)
SESSIONS_KEY: web.AppKey[dict] = web.AppKey("sessions", dict)
CONTROL_TOKEN_KEY: web.AppKey[str] = web.AppKey("control_token", str)
STARTED_MS_KEY: web.AppKey[int] = web.AppKey("started_ms", int)


class ApiFault(Exception):
    """Handler-level fault rendered as a JSON error response."""

    def __init__(self, status: int, code: str):
        self.status = status
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------- config utils


def _config_object(service):
    cfg = getattr(service, "config", None)
    if cfg is None:
        return {}
    if callable(cfg) and not isinstance(cfg, dict):
        try:
            cfg = cfg()
        except TypeError:
            return {}
    return cfg


def _cfg(cfg, name, default=None):
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def _setting(service, cfg, name, default=None):
    """Read a setting from ``service.config`` first, then from the service."""
    sentinel = object()
    value = _cfg(cfg, name, sentinel)
    if value is not sentinel and value is not None:
        return value
    value = getattr(service, name, sentinel)
    if value is not sentinel and value is not None:
        return value
    return default


# ------------------------------------------------------------------- redaction


def _is_secret_key(key) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(part in lowered for part in SECRET_KEY_PARTS)


def _sanitize(value):
    if isinstance(value, dict):
        return {k: _sanitize(v) for k, v in value.items() if not _is_secret_key(k)}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


# ------------------------------------------------------------------- responses


def _json_error(status: int, code: str, message: str | None = None) -> web.Response:
    return web.json_response({"error": code, "message": message or code}, status=status)


def _apply_headers(response: web.Response) -> None:
    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Content-Security-Policy", CSP)
    headers.setdefault("Cache-Control", "no-store")


def _host_allowed(host: str | None) -> bool:
    if not host:
        return False
    if host.startswith("["):
        end = host.find("]")
        name = host[1:end] if end != -1 else host.strip("[]")
    else:
        name = host.split(":", 1)[0]
    return name.lower() in LOCAL_HOSTS


def _origin_allowed(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return False
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    return parts.netloc.lower() == (request.host or "").lower()


def _is_json(request: web.Request) -> bool:
    return request.content_type == "application/json"


# ------------------------------------------------------------------- sessions


def _prune_sessions(app: web.Application) -> None:
    sessions = app[SESSIONS_KEY]
    current = now_ms()
    for token in [t for t, exp in sessions.items() if exp <= current]:
        sessions.pop(token, None)


def _session_valid(app: web.Application, token: str) -> bool:
    _prune_sessions(app)
    expiry = app[SESSIONS_KEY].get(token)
    return bool(expiry and expiry > now_ms())


def _token_matches(candidate: str, expected: str) -> bool:
    if not candidate or not expected:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _authorized(app: web.Application, request: web.Request) -> bool:
    control = app[CONTROL_TOKEN_KEY]
    if not control:
        return False
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        candidate = header[7:].strip()
        if _token_matches(candidate, control) or _session_valid(app, candidate):
            return True
    cookie = request.cookies.get(COOKIE_NAME)
    return bool(cookie) and _session_valid(app, cookie)


# --------------------------------------------------------------- api dispatch


async def _invoke(target, name, *args, **kwargs):
    fn = getattr(target, name, None)
    if fn is None:
        raise ApiFault(501, "unsupported")
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


async def _invoke_optional(target, name, *args, **kwargs):
    if target is None or not hasattr(target, name):
        return None
    return await _invoke(target, name, *args, **kwargs)


async def _json_body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        raise ApiFault(400, "invalid_json") from None
    except Exception:
        raise ApiFault(400, "invalid_json") from None
    if not isinstance(data, dict):
        raise ApiFault(400, "invalid_json")
    return data


def _require_fund(fund_id: str) -> str:
    if fund_id not in POLICIES:
        raise ApiFault(404, "unknown_fund")
    return fund_id


def _text_field(body: dict, key: str, *, max_len: int = 200) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise FundError("invalid_field")
    return value


def _fraction_field(body: dict, key: str) -> float:
    raw = body.get(key)
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        raise FundError("invalid_fraction") from None
    if not value.is_finite() or value < 0 or value > 1:
        raise FundError("invalid_fraction")
    return float(value)


# --------------------------------------------------------------------- routes


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def _session_login(request: web.Request) -> web.StreamResponse:
    app = request.app
    body = await _json_body(request)
    token = body.get("token")
    if not isinstance(token, str) or not token:
        raise ApiFault(400, "invalid_field")
    control = app[CONTROL_TOKEN_KEY]
    if not control:
        raise ApiFault(503, "token_not_configured")
    if not _token_matches(token, control):
        raise ApiFault(401, "unauthorized")
    session = secrets.token_urlsafe(32)
    app[SESSIONS_KEY][session] = now_ms() + SESSION_TTL_MS
    response = web.json_response({"ok": True, "expires_ms": SESSION_TTL_MS})
    response.set_cookie(
        COOKIE_NAME,
        session,
        max_age=SESSION_TTL_MS // 1000,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


async def _session_logout(request: web.Request) -> web.StreamResponse:
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        request.app[SESSIONS_KEY].pop(cookie, None)
    response = web.json_response({"ok": True})
    response.del_cookie(COOKIE_NAME, path="/")
    return response


async def _api_status(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    payload = await _invoke(service, "public_status")
    if not isinstance(payload, dict):
        payload = {"funds": [], "events": [], "network": {}, "status": {}}
    payload = _sanitize(payload)
    payload.setdefault("funds", [])
    payload.setdefault("events", [])
    payload.setdefault("network", {})
    payload.setdefault("status", {})
    cfg = _config_object(service)
    payload["live"] = bool(_setting(service, cfg, "live", False))
    return web.json_response(payload)


async def _api_orders(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = request.query.get("fund_id") or None
    user_id = request.query.get("user_id") or None
    target = service if hasattr(service, "orders") else getattr(service, "ledger", None)
    try:
        orders = await _invoke_optional(target, "orders", fund_id, user_id)
    except FundError as exc:
        raise ApiFault(400, exc.code) from None
    return web.json_response({"orders": _sanitize(orders or [])})


async def _api_events(request: web.Request) -> web.StreamResponse:
    store = request.app[SERVICE_KEY].store
    fund_id = request.query.get("fund_id") or None
    try:
        limit = int(request.query.get("limit", "100"))
    except ValueError:
        limit = 100
    events = store.events(limit=limit, fund_id=fund_id)
    return web.json_response({"events": _sanitize(events)})


async def _api_holders(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.query.get("fund_id") or "")
    holders = await _invoke_optional(service, "holders", fund_id)
    return web.json_response({"fund_id": fund_id, "holders": _sanitize(holders or [])})


async def _api_nav_history(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info.get("fund_id", ""))
    try:
        limit = int(request.query.get("limit", "365"))
    except ValueError:
        limit = 365
    target = service if hasattr(service, "nav_history") else getattr(service, "ledger", None)
    history = await _invoke_optional(target, "nav_history", fund_id, limit)
    return web.json_response({"fund_id": fund_id, "history": _sanitize(history or [])})


async def _api_login(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info["fund_id"])
    body = await _json_body(request)
    username = _text_field(body, "username")
    password = _text_field(body, "password", max_len=512)
    result = await _invoke(service, "login", fund_id, username, password)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_seed(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info["fund_id"])
    body = await _json_body(request)
    user_id = _text_field(body, "user_id")
    principal_units = money_units(body.get("principal"), positive=True)
    result = await _invoke(service, "seed", fund_id, user_id, principal_units)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_running(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info["fund_id"])
    body = await _json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise FundError("invalid_field")
    result = await _invoke(service, "set_running", fund_id, enabled)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_stop(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info["fund_id"])
    await _json_body(request)
    result = await _invoke(service, "stop_fund", fund_id)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_settle(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    fund_id = _require_fund(request.match_info["fund_id"])
    body = await _json_body(request)
    kind = body.get("kind")
    if kind not in ("month", "emergency"):
        raise FundError("invalid_kind")
    period = body.get("period")
    if period is not None:
        if not isinstance(period, str) or not period or len(period) > 16:
            raise FundError("invalid_period")
    else:
        period = None
    result = await _invoke(service, "settle", fund_id, kind, period)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_dividend_choice(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    body = await _json_body(request)
    fund_id = _require_fund(body.get("fund_id"))
    user_id = _text_field(body, "user_id")
    fraction = _fraction_field(body, "reinvest_fraction")
    result = await _invoke(service, "set_dividend_choice", fund_id, user_id, fraction)
    return web.json_response(_sanitize(result if isinstance(result, dict) else {"ok": True}))


async def _api_backup(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    await _json_body(request)
    result = await _invoke(service, "backup")
    name = None
    if result is not None:
        try:
            name = Path(result).name
        except TypeError:
            name = None
    return web.json_response({"ok": True, "backup": name})


def _add_routes(app: web.Application) -> None:
    app.router.add_get("/", _index)
    app.router.add_static("/static/", STATIC_DIR, name="static", show_index=False)

    app.router.add_post("/api/login", _session_login)
    app.router.add_post("/api/logout", _session_logout)
    app.router.add_get("/api/status", _api_status)
    app.router.add_get("/api/orders", _api_orders)
    app.router.add_get("/api/events", _api_events)
    app.router.add_get("/api/holders", _api_holders)
    app.router.add_get("/api/funds/{fund_id}/nav-history", _api_nav_history)
    app.router.add_post("/api/funds/{fund_id}/login", _api_login)
    app.router.add_post("/api/funds/{fund_id}/seed", _api_seed)
    app.router.add_post("/api/funds/{fund_id}/running", _api_running)
    app.router.add_post("/api/funds/{fund_id}/stop", _api_stop)
    app.router.add_post("/api/funds/{fund_id}/settle", _api_settle)
    app.router.add_post("/api/dividend-choice", _api_dividend_choice)
    app.router.add_post("/api/backup", _api_backup)


# ------------------------------------------------------------------ middleware


@web.middleware
async def _guard(request: web.Request, handler):
    try:
        response = await _dispatch(request, handler)
    except ApiFault as exc:
        response = _json_error(exc.status, exc.code)
    except FundError as exc:
        response = _json_error(400, exc.code)
    except web.HTTPException as exc:
        response = exc
    except Exception:
        log.exception("funds console request failed: %s %s", request.method, request.path)
        response = _json_error(500, "internal_error")
    if not isinstance(response, web.StreamResponse):
        response = _json_error(500, "internal_error")
    _apply_headers(response)
    return response


async def _dispatch(request: web.Request, handler):
    if not _host_allowed(request.host):
        return _json_error(403, "forbidden_host")

    path = request.path
    if path.startswith("/api/"):
        if request.method in WRITE_METHODS:
            if not _origin_allowed(request):
                return _json_error(403, "forbidden_origin")
            if not _is_json(request):
                return _json_error(415, "unsupported_media_type")
        if path not in AUTH_EXEMPT and not _authorized(request.app, request):
            return _json_error(401, "unauthorized")

    return await handler(request)


def create_app(service) -> web.Application:
    """Build the aiohttp application for the operator console (no bind)."""
    app = web.Application(middlewares=[_guard])
    app[SERVICE_KEY] = service
    app[SESSIONS_KEY] = {}
    cfg = _config_object(service)
    app[CONTROL_TOKEN_KEY] = str(_setting(service, cfg, "control_token", "") or "")
    app[STARTED_MS_KEY] = now_ms()
    _add_routes(app)
    return app
