"""The gateway: one HTTP service, three doors into the same brain.

- /mcp            MCP Streamable HTTP (Claude, ChatGPT, Gemini, Grok, any MCP client)
- /v1/...         REST + /openapi.json (Custom GPT Actions, n8n, Zapier, curl)
- /k/<key>/...    same routes with the key in the path, for clients that only accept a URL

Every request (except /health and /openapi.json) must carry an agent key.
The middleware maps the key to a registered agent and passes the identity on
in a header it controls. Identity is never taken from the request body.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import anyio
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from ..registry import Agent
from ..service import Brain, BrainError
from ..store import store_from_env
from .mcp_tools import build_mcp, handoff_payload
from .openapi import build_openapi

log = logging.getLogger("brainkit.gateway")

AGENT_HEADER = b"x-brain-agent"
OPEN_PATHS = {"/health", "/openapi.json"}
MAX_BODY = 256 * 1024
STATUS = {"bad_request": 400, "invalid_event": 422, "unknown_customer": 404, "forbidden": 403,
          "config": 500, "rate_limited": 429}


class RateLimiter:
    """Per-agent token bucket on writes; stops a looping agent from flooding the repo."""

    def __init__(self, per_minute: int):
        self.rate = per_minute / 60.0
        self.cap = float(per_minute)
        self._b: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.cap <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            tokens, last = self._b.get(key, (self.cap, now))
            tokens = min(self.cap, tokens + (now - last) * self.rate)
            if tokens < 1:
                self._b[key] = (tokens, now)
                return False
            self._b[key] = (tokens - 1, now)
            return True


class AuthMiddleware:
    def __init__(self, app, brain: Brain):
        self.app = app
        self.brain = brain

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope.get("path", "")
        key = None
        if path.startswith("/k/"):
            _, _, rest = path.partition("/k/")
            key, _, tail = rest.partition("/")
            new_path = "/" + tail
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode()
            path = new_path
        if path == "/mcp/":  # avoid a redirect that would drop a /k/<key>/ prefix
            scope = dict(scope, path="/mcp", raw_path=b"/mcp")
            path = "/mcp"
        headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != AGENT_HEADER]
        if path in OPEN_PATHS or (path == "/" and scope.get("method") == "GET"):
            scope = dict(scope, headers=headers)
            return await self.app(scope, receive, send)
        if key is None:
            for k, v in headers:
                lk = k.lower()
                if lk == b"authorization":
                    val = v.decode("latin-1").strip()
                    if val[:7].lower() == "bearer ":
                        key = val[7:].strip()
                    elif val.startswith("brk_"):  # some clients send the raw token
                        key = val
                    if key:
                        break
                if lk == b"x-brain-key":
                    key = v.decode("latin-1").strip()
                    break
        agent = None
        if key:
            try:
                agent = await anyio.to_thread.run_sync(lambda: self.brain.registry().agent_for_key(key))
            except BrainError as e:
                return await JSONResponse({"error": str(e), "code": e.code}, 500)(scope, receive, send)
        if agent is None:
            resp = JSONResponse({"error": "missing or invalid agent key", "code": "unauthorized"}, 401,
                                headers={"WWW-Authenticate": 'Bearer realm="agent-brain"'})
            return await resp(scope, receive, send)
        headers.append((AGENT_HEADER, agent.id.encode()))
        scope = dict(scope, headers=headers)
        scope.setdefault("state", {})
        scope["state"] = dict(scope["state"], agent=agent)
        return await self.app(scope, receive, send)


def create_app(brain: Brain | None = None, public_url: str | None = None,
               writes_per_minute: int | None = None):
    brain = brain or Brain(store_from_env())
    public_url = public_url or os.environ.get("BRAIN_PUBLIC_URL", "")
    limiter = RateLimiter(writes_per_minute if writes_per_minute is not None
                          else int(os.environ.get("BRAIN_WRITES_PER_MINUTE", "120")))

    def agent_from_ctx(ctx: Context | None) -> Agent:
        hdrs = ctx.headers if ctx is not None else None
        agent_id = hdrs.get("x-brain-agent") if hdrs is not None else None
        agent = brain.registry().agents.get(agent_id or "")
        if agent is None or agent.disabled:
            raise BrainError("unauthenticated", "forbidden")
        return agent

    def mcp_agent(ctx: Context | None) -> Agent:
        try:
            agent = agent_from_ctx(ctx)
        except BrainError as e:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(str(e)) from None
        return agent

    mcp = build_mcp(brain, mcp_agent, limiter=limiter)

    def req_agent(request: Request) -> Agent:
        agent = request.scope.get("state", {}).get("agent")
        if agent is None:
            raise BrainError("unauthenticated", "forbidden")
        return agent

    def err(e: BrainError) -> JSONResponse:
        return JSONResponse({"error": str(e), "code": e.code}, STATUS.get(e.code, 400))

    async def run(fn):
        return await anyio.to_thread.run_sync(fn)

    async def json_body(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise BrainError("request body too large", "bad_request")
        try:
            data = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise BrainError("body must be JSON", "bad_request") from None
        if not isinstance(data, dict):
            raise BrainError("body must be a JSON object", "bad_request")
        return data

    def _types(request: Request) -> list[str] | None:
        t = request.query_params.get("type")
        return [x.strip() for x in t.split(",") if x.strip()] if t else None

    def _int(request: Request, name: str, default: int, cap: int) -> int:
        try:
            return max(1, min(int(request.query_params.get(name, default)), cap))
        except ValueError:
            raise BrainError(f"{name} must be an integer") from None

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> Response:
        try:
            n = len(await run(lambda: brain.registry().customers))
            return JSONResponse({"ok": True, "customers": n, "version": "1.0.0"})
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": type(e).__name__}, 503)

    @mcp.custom_route("/", methods=["GET"])
    async def root(request: Request) -> Response:
        return PlainTextResponse("agent-brain gateway. MCP: /mcp  REST: /v1  spec: /openapi.json\n")

    @mcp.custom_route("/openapi.json", methods=["GET"])
    async def openapi(request: Request) -> Response:
        base = public_url or str(request.base_url).rstrip("/")
        return JSONResponse(build_openapi(base))

    def route(fn):
        async def wrapper(request: Request) -> Response:
            try:
                return await fn(request)
            except BrainError as e:
                return err(e)
        wrapper.__name__ = fn.__name__
        return wrapper

    @mcp.custom_route("/v1/me", methods=["GET"])
    @route
    async def me(request: Request) -> Response:
        a = req_agent(request)
        return JSONResponse({"id": a.id, "vendor": a.vendor, "kind": a.kind, "scopes": a.scopes,
                             "customers": a.customers})

    @mcp.custom_route("/v1/customers", methods=["GET"])
    @route
    async def customers(request: Request) -> Response:
        a = req_agent(request)
        return JSONResponse(await run(lambda: brain.list_customers(a)))

    @mcp.custom_route("/v1/customers/{customer}/context", methods=["GET"])
    @route
    async def context(request: Request) -> Response:
        a = req_agent(request)
        c = request.path_params["customer"]
        text = await run(lambda: brain.context(c, agent=a))
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @mcp.custom_route("/v1/customers/{customer}/recent", methods=["GET"])
    @route
    async def recent(request: Request) -> Response:
        a = req_agent(request)
        c = request.path_params["customer"]
        days, limit, types = _int(request, "days", 14, 365), _int(request, "limit", 30, 200), _types(request)
        return JSONResponse(await run(lambda: brain.recent(c, days=days, limit=limit, types=types, agent=a)))

    @mcp.custom_route("/v1/customers/{customer}/tasks", methods=["GET"])
    @route
    async def tasks(request: Request) -> Response:
        a = req_agent(request)
        c = request.path_params["customer"]
        status = request.query_params.get("status", "open")
        if status not in ("open", "closed", "all"):
            raise BrainError("status must be open, closed or all")
        return JSONResponse(await run(lambda: brain.tasks(c, status=status, agent=a)))

    @mcp.custom_route("/v1/search", methods=["GET"])
    @route
    async def search(request: Request) -> Response:
        a = req_agent(request)
        q = request.query_params.get("q", "")
        cust = request.query_params.get("customer")
        since = request.query_params.get("since")
        limit, types = _int(request, "limit", 20, 100), _types(request)
        return JSONResponse(await run(lambda: brain.search(q, customer=cust, types=types, since=since,
                                                           limit=limit, agent=a)))

    @mcp.custom_route("/v1/events", methods=["POST"])
    @route
    async def post_event(request: Request) -> Response:
        a = req_agent(request)
        if not limiter.allow(a.id):
            raise BrainError("write rate limit exceeded; slow down", "rate_limited")
        data = await json_body(request)
        agent_meta = data.get("agent") if isinstance(data.get("agent"), dict) else {}
        agent_meta.setdefault("surface", "rest")
        data["agent"] = agent_meta
        return JSONResponse(await run(lambda: brain.log_event(a, data)))

    @mcp.custom_route("/v1/handoff", methods=["POST"])
    @route
    async def post_handoff(request: Request) -> Response:
        a = req_agent(request)
        if not limiter.allow(a.id):
            raise BrainError("write rate limit exceeded; slow down", "rate_limited")
        d = await json_body(request)
        for k in ("next_steps", "open_questions", "refs"):
            if d.get(k) is not None and not isinstance(d[k], list):
                raise BrainError(f"{k} must be a list of strings")
        return JSONResponse(await run(lambda: brain.log_event(a, handoff_payload(
            str(d.get("customer") or ""), str(d.get("summary") or ""), d.get("next_steps"),
            d.get("open_questions"), d.get("refs"), d.get("model"), "rest"))))

    allowed = [h.strip() for h in os.environ.get("BRAIN_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed), allowed_hosts=allowed,
        allowed_origins=[o.strip() for o in os.environ.get("BRAIN_ALLOWED_ORIGINS", "").split(",") if o.strip()])
    inner = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                    transport_security=security)
    return AuthMiddleware(inner, brain)
