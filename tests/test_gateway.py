"""Gateway tests against a real uvicorn server (REST + MCP over Streamable HTTP)."""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from brainkit.gateway.app import create_app
from brainkit.schema import parse_event

from .conftest import KEYS


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def server(brain, repo):
    port = _free_port()
    app = create_app(brain, public_url=f"http://127.0.0.1:{port}", writes_per_minute=30)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(timeout=5)


def h(agent="chatgpt"):
    return {"Authorization": f"Bearer {KEYS[agent]}"}


def test_health_and_openapi_are_open(server):
    assert httpx.get(f"{server}/health").json()["ok"] is True
    spec = httpx.get(f"{server}/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    assert spec["servers"][0]["url"] == server
    ops = {op["operationId"] for p in spec["paths"].values() for op in p.values()}
    assert ops == {"whoAmI", "listCustomers", "getContext", "recentEvents", "listTasks", "search", "logEvent", "handoff"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer brk_wrong"}, {"Authorization": "Basic x"},
                                     {"Authorization": f"Bearer {KEYS['disabled-bot']}"}])
def test_auth_required(server, headers):
    r = httpx.get(f"{server}/v1/customers", headers=headers)
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers
    assert httpx.post(f"{server}/mcp", headers=headers, json={}).status_code == 401


def test_key_forms(server):
    assert httpx.get(f"{server}/v1/me", headers=h()).json()["id"] == "chatgpt"
    assert httpx.get(f"{server}/v1/me", headers={"Authorization": KEYS["chatgpt"]}).json()["id"] == "chatgpt"
    assert httpx.get(f"{server}/v1/me", headers={"X-Brain-Key": KEYS["chatgpt"]}).json()["id"] == "chatgpt"
    assert httpx.get(f"{server}/k/{KEYS['chatgpt']}/v1/me").json()["id"] == "chatgpt"
    assert httpx.get(f"{server}/k/brk_bad/v1/me").status_code == 401


def test_rest_write_and_read(server, repo):
    r = httpx.post(f"{server}/v1/events", headers={**h(), "x-brain-agent": "alice"}, json={
        "customer": "roadrunner", "type": "email_sent", "title": "Sent SOW", "refs": ["gmail:thread/1"],
        "agent": {"id": "alice", "model": "gpt-5"}})
    assert r.status_code == 200, r.text
    body = r.json()
    e = parse_event((repo / body["path"]).read_text())
    assert e.agent.id == "chatgpt" and e.agent.model == "gpt-5" and e.agent.surface == "rest"
    ctx = httpx.get(f"{server}/v1/customers/acme/context", headers=h())
    assert ctx.headers["content-type"].startswith("text/markdown") and "Sent SOW" in ctx.text
    rec = httpx.get(f"{server}/v1/customers/acme/recent", headers=h(), params={"type": "email_sent"}).json()
    assert rec[0]["title"] == "Sent SOW"
    s = httpx.get(f"{server}/v1/search", headers=h(), params={"q": "sow"}).json()
    assert s and s[0]["customer"] == "acme"
    ho = httpx.post(f"{server}/v1/handoff", headers=h(), json={"customer": "acme", "summary": "Sent SOW",
                                                                "next_steps": ["Wait for signature"]})
    assert ho.status_code == 200
    assert "Wait for signature" in httpx.get(f"{server}/v1/customers/acme/context", headers=h()).text
    t = httpx.get(f"{server}/v1/customers/acme/tasks", headers=h(), params={"status": "all"})
    assert t.status_code == 200 and t.json() == []


@pytest.mark.parametrize("payload,status,code", [
    ({"customer": "nope", "type": "note", "title": "x"}, 404, "unknown_customer"),
    ({"customer": "acme", "type": "email_sent", "title": "x"}, 422, "invalid_event"),
    ({"customer": "acme", "type": "note"}, 422, "invalid_event"),
])
def test_rest_errors(server, payload, status, code):
    r = httpx.post(f"{server}/v1/events", headers=h(), json=payload)
    assert r.status_code == status and r.json()["code"] == code


def test_rest_bad_inputs(server):
    assert httpx.post(f"{server}/v1/events", headers=h(), content=b"not json").status_code == 400
    assert httpx.post(f"{server}/v1/events", headers=h(), json=[1]).status_code == 400
    assert httpx.post(f"{server}/v1/events", headers=h(), content=b"x" * 300_000).status_code == 400
    assert httpx.get(f"{server}/v1/search", headers=h(), params={"q": ""}).status_code == 400
    assert httpx.get(f"{server}/v1/customers/acme/recent", headers=h(), params={"days": "x"}).status_code == 400
    assert httpx.get(f"{server}/v1/customers/acme/tasks", headers=h(), params={"status": "x"}).status_code == 400
    assert httpx.post(f"{server}/v1/handoff", headers=h(), json={"customer": "acme", "summary": ""}).status_code == 400
    assert httpx.post(f"{server}/v1/handoff", headers=h(),
                      json={"customer": "acme", "summary": "s", "next_steps": "x"}).status_code == 400


def test_rest_scopes(server):
    hs = {"Authorization": f"Bearer {KEYS['scoped-bot']}"}
    assert [c["slug"] for c in httpx.get(f"{server}/v1/customers", headers=hs).json()] == ["beta-co"]
    assert httpx.get(f"{server}/v1/customers/acme/context", headers=hs).status_code == 403
    r = httpx.post(f"{server}/v1/events", headers=hs, json={"customer": "acme", "type": "note", "title": "x"})
    assert r.status_code == 403
    ro = {"Authorization": f"Bearer {KEYS['reader']}"}
    assert httpx.post(f"{server}/v1/events", headers=ro, json={"customer": "acme", "type": "note",
                                                               "title": "x"}).status_code == 403


def test_rate_limit(server):
    # Limit is 30/min; each write takes time, so a few tokens refill during the loop.
    codes = [httpx.post(f"{server}/v1/events", headers=h("claude-desktop"),
                        json={"customer": "acme", "type": "note", "title": f"n{i}"}).status_code for i in range(45)]
    assert 30 <= codes.count(200) < 45 and 429 in codes
    assert httpx.post(f"{server}/v1/events", headers=h("chatgpt"),
                      json={"customer": "acme", "type": "note", "title": "other agent"}).status_code == 200


def test_rate_limiter_unit(monkeypatch):
    from brainkit.gateway import app as appmod
    t = [1000.0]
    monkeypatch.setattr(appmod.time, "monotonic", lambda: t[0])
    rl = appmod.RateLimiter(60)
    assert all(rl.allow("a") for _ in range(60))
    assert not rl.allow("a")
    assert rl.allow("b")
    t[0] += 1.0
    assert rl.allow("a") and not rl.allow("a")
    assert appmod.RateLimiter(0).allow("x")


async def test_mcp_end_to_end(server, repo):
    from mcp import Client

    async with Client(f"{server}/k/{KEYS['claude-desktop']}/mcp") as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == {"brain_list_customers", "brain_get_context", "brain_recent_events", "brain_search",
                         "brain_list_tasks", "brain_log_event", "brain_open_task", "brain_update_task",
                         "brain_close_task", "brain_handoff"}
        r = await c.call_tool("brain_open_task", {"customer": "acme", "task_id": "pdf", "title": "Review PDF",
                                                  "owner": "dana", "model": "claude-opus-5-5"})
        assert not r.is_error, r
        r = await c.call_tool("brain_update_task", {"customer": "acme", "task_id": "pdf", "status": "blocked"})
        assert not r.is_error
        tasks = (await c.call_tool("brain_list_tasks", {"customer": "acme"})).structured_content["result"]
        assert tasks[0]["status"] == "blocked"
        r = await c.call_tool("brain_close_task", {"customer": "acme", "task_id": "pdf"})
        assert not r.is_error
        r = await c.call_tool("brain_log_event", {"customer": "acme", "type": "decision", "title": "Ship Friday"})
        out = r.structured_content
        assert out["ok"] and out["customer"] == "acme"
        e = parse_event((repo / out["path"]).read_text())
        assert e.agent.id == "claude-desktop" and e.agent.surface == "mcp"
        r = await c.call_tool("brain_handoff", {"customer": "acme", "summary": "Closed PDF task",
                                                "next_steps": ["Ship"], "open_questions": ["Date?"]})
        assert not r.is_error
        ctx = (await c.call_tool("brain_get_context", {"customer": "roadrunner"})).content[0].text
        assert "Closed PDF task" in ctx and "Ship Friday" in ctx
        hits = (await c.call_tool("brain_search", {"query": "friday"})).structured_content["result"]
        assert hits[0]["title"] == "Ship Friday"
        rec = (await c.call_tool("brain_recent_events", {"customer": "acme", "limit": 2})).structured_content
        assert len(rec["result"]) == 2
        bad = await c.call_tool("brain_log_event", {"customer": "nope", "type": "note", "title": "x"})
        assert bad.is_error and "unknown_customer" in bad.content[0].text
        bad = await c.call_tool("brain_handoff", {"customer": "acme", "summary": "  "})
        assert bad.is_error and "summary is required" in bad.content[0].text


async def test_mcp_bearer_and_scope(server):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {KEYS['scoped-bot']}"}) as hc:
        async with Client(streamable_http_client(f"{server}/mcp", http_client=hc)) as c:
            lst = (await c.call_tool("brain_list_customers", {})).structured_content["result"]
            assert [x["slug"] for x in lst] == ["beta-co"]
            r = await c.call_tool("brain_get_context", {"customer": "acme"})
            assert r.is_error and "forbidden" in r.content[0].text
