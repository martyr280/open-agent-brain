"""Regression tests for the independent QA review findings (numbers match the review)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from brainkit.ingest.base import load_cursor
from brainkit.ingest.github_commits import GitHubCommitsIngestor
from brainkit.ingest.google import CalendarIngestor, GmailIngestor, GoogleAccount
from brainkit.ingest.runner import run_ingestors
from brainkit.jobs import find_gaps, validate_repo
from brainkit.registry import Registry
from brainkit.schema import parse_event
from brainkit.service import BrainError

from .conftest import KEYS
from .test_gateway import h, server  # noqa: F401  (fixture)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
ACCT = GoogleAccount("work", "rt", {"alice@example.com"})


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# 1 ------------------------------------------------------------------------
def test_01_redacted_event_passes_validation(brain, agent, repo):
    r = brain.log_event(agent, {"customer": "acme", "type": "note", "title": "Rotated",
                                "body": "Told Dana the password: hunter2xx was rotated"})
    assert r["redactions"] == ["password_assignment"]
    assert validate_repo(repo).ok, validate_repo(repo).errors


# 2 ------------------------------------------------------------------------
def test_02_every_field_is_redacted(brain, agent, repo):
    gh = "ghp_" + "x" * 36
    r = brain.log_event(agent, {
        "customer": "acme", "type": "doc", "title": "Doc", "refs": [f"url:https://x.io/?k={gh}"],
        "owner": "password=hunter2xyz", "tags": ["sk-ant-" + "y" * 30], "dedupe_key": "AKIA" + "Z" * 16,
        "agent": {"model": "xai-" + "q" * 30}})
    text = (repo / r["path"]).read_text()
    for secret in (gh, "hunter2xyz", "sk-ant-", "AKIA", "xai-q"):
        assert secret not in text
    assert validate_repo(repo).ok


# 3 ------------------------------------------------------------------------
def test_03_global_requires_explicit_grant(brain, agent):
    brain.log_event(agent, {"customer": "_global", "type": "note", "title": "global zeta pricing note"})
    scoped = brain.registry().agents["scoped-bot"]
    assert brain.search("zeta", agent=scoped) == []
    with pytest.raises(BrainError):
        brain.recent("_global", agent=scoped)
    with pytest.raises(BrainError):
        brain.log_event(scoped, {"customer": "_global", "type": "note", "title": "x"})
    granted = Registry.from_yaml("customers: []", "agents:\n  - {id: g, vendor: v, customers: [_global]}").agents["g"]
    assert granted.can("read", "_global") and granted.can("write", "_global")
    assert brain.search("zeta", agent=agent)  # unrestricted agent still sees it


# 4 ------------------------------------------------------------------------
def test_04_rotate_applies_options(repo):
    from .test_cli_and_repo import run
    assert run(repo, "agent", "add", "chatgpt", "--rotate", "--customers", "acme,_global",
               "--scopes", "read").returncode == 0
    reg = Registry.from_yaml((repo / "_registry/customers.yaml").read_text(),
                             (repo / "_registry/agents.yaml").read_text())
    a = reg.agents["chatgpt"]
    assert a.customers == ["acme", "_global"] and a.scopes == ["read"] and a.vendor == "openai"
    assert run(repo, "agent", "add", "brand-new").returncode != 0  # vendor required for new


# 5 ------------------------------------------------------------------------
def test_05_dedupe_without_ts(brain, agent, monkeypatch):
    p = {"customer": "acme", "type": "meeting", "title": "Plaud", "refs": ["plaud:REC1"], "dedupe_key": "plaud:REC1"}
    a = brain.log_event(agent, dict(p))
    t = [NOW + timedelta(seconds=5)]
    monkeypatch.setattr(brain, "_now", lambda: t[0])
    b = brain.log_event(agent, dict(p))
    assert b["duplicate"] and b["id"] == a["id"]
    brain._hist.clear()  # also detected from storage, not only the in-process cache
    c = brain.log_event(agent, dict(p))
    assert c["duplicate"]


# 6 ------------------------------------------------------------------------
def _gmail_mock(msgs):
    def handler(req):
        url = str(req.url)
        if "oauth2" in url:
            return httpx.Response(200, json={"access_token": "at"})
        if "/messages/" in url:
            return httpx.Response(200, json=msgs[req.url.path.rsplit("/", 1)[-1]])
        q = req.url.params["q"]
        after = int(q.split("after:")[1].split()[0])
        before = int(q.split("before:")[1].split()[0]) if "before:" in q else 10**12
        ids = [m for m in sorted(msgs, key=lambda k: -int(msgs[k]["internalDate"]))
               if after < int(msgs[m]["internalDate"]) // 1000 < before]
        start = int(req.url.params.get("pageToken", "0"))
        page = ids[start:start + 2]  # tiny pages
        body = {"messages": [{"id": i} for i in page]}
        if start + 2 < len(ids):
            body["nextPageToken"] = str(start + 2)
        return httpx.Response(200, json=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_06_gmail_large_backlog_skips_nothing(monkeypatch, brain):
    import brainkit.ingest.google as g
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    monkeypatch.setattr(g, "MAX_IDS_LISTED", 6)
    monkeypatch.setattr(g, "MAX_MESSAGES_PER_RUN", 2)
    base = NOW - timedelta(days=3)
    msgs = {f"m{i}": {"id": f"m{i}", "threadId": f"t{i}", "internalDate": str(int((base + timedelta(hours=i)).timestamp() * 1000)),
                      "labelIds": ["INBOX"], "snippet": "",
                      "payload": {"headers": [{"name": "From", "value": "a@acme.com"}, {"name": "Subject", "value": f"mail {i}"}]}}
            for i in range(10)}
    client = _gmail_mock(msgs)
    cursor = {"work": iso(base - timedelta(minutes=1))}
    seen = set()
    for _ in range(30):
        b = GmailIngestor(accounts=[ACCT], client=client, now=NOW).fetch(brain.registry(), cursor)
        seen |= {p["title"] for p in b.payloads}
        cursor = b.cursor
        if not cursor.get("work:backlog"):
            break
    assert seen == {f"mail {i}" for i in range(10)}


# 7 ------------------------------------------------------------------------
def test_07_bad_item_does_not_stall_cursor(monkeypatch, brain, store, repo):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    msgs = {"m1": {"id": "m1", "threadId": "t", "internalDate": str(int((NOW - timedelta(hours=1)).timestamp() * 1000)),
                   "labelIds": ["INBOX"], "snippet": "",
                   "payload": {"headers": [{"name": "From", "value": "a@acme.com"}, {"name": "Subject", "value": "   "}]}}}
    ing = GmailIngestor(accounts=[ACCT], client=_gmail_mock(msgs), now=NOW)
    res = run_ingestors(brain, store, ingestors=[ing])
    assert res["gmail"]["written"] == 1  # blank subject -> "(no subject)"
    assert brain.search("subject")[0]["title"] == "(no subject)"

    class Broken(GmailIngestor):
        def _to_payloads(self, *a, **k):
            return [{"customer": "acme", "type": "bogus", "title": "x", "dedupe_key": "bad"}]

    res = run_ingestors(brain, store, ingestors=[Broken(accounts=[ACCT], client=_gmail_mock(msgs), now=NOW)])
    assert res["gmail"]["errors"] and load_cursor(store, "gmail").get("work")
    assert (repo / "_state/ingest/gmail.errors.json").is_file()


# 8 ------------------------------------------------------------------------
def test_08_moved_and_cancelled_meetings(monkeypatch, brain, store):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    state = {"items": [{"id": "c1", "status": "confirmed", "summary": "Kickoff review", "updated": iso(NOW),
                        "start": {"dateTime": iso(NOW + timedelta(days=3))}, "attendees": [{"email": "bob@acme.com"}]}]}

    def handler(req):
        if "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json=state)

    def run():
        ing = CalendarIngestor(accounts=[ACCT], client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
        return run_ingestors(brain, store, ingestors=[ing])

    run()
    state["items"][0]["start"] = {"dateTime": iso(NOW + timedelta(days=5))}
    run()
    ctx = brain.context("acme")
    upcoming = ctx.split("## Upcoming meetings")[1].split("##")[0]
    assert upcoming.count("Kickoff review") == 1 and (NOW + timedelta(days=5)).strftime("%Y-%m-%d") in upcoming
    state["items"] = [{"id": "c1", "status": "cancelled"}]  # Google sends only id + status
    r = run()
    assert r["gcal"]["written"] == 1
    upcoming = brain.context("acme").split("## Upcoming meetings")[1].split("##")[0]
    assert "Kickoff review" not in upcoming
    assert run()["gcal"]["written"] == 0  # idempotent


# 9 ------------------------------------------------------------------------
def test_09_future_meeting_is_not_activity(brain, agent):
    brain.log_event(agent, {"customer": "acme", "type": "handoff", "title": "Today handoff"})
    brain.log_event(agent, {"customer": "acme", "type": "meeting", "status": "scheduled", "title": "Far meeting",
                            "ts": iso(NOW + timedelta(days=20)), "refs": ["gcal:x"]})
    ctx = brain.context("acme")
    assert "Last activity:" in ctx and "Today handoff" in ctx.split("Last activity:")[1].splitlines()[0]
    assert not [g for g in find_gaps(brain) if g.customer == "acme" and g.kind == "no_handoff"]


# 10 -----------------------------------------------------------------------
@pytest.mark.parametrize("extra", [{"ts": "1960-01-01T00:00:00Z", "dedupe_key": "k"}, {"agent": {"model": 123}},
                                   {"tags": 5}, {"refs": "url:x"}, {"tags": [1, 2]}])
def test_10_bad_input_is_422_not_500(server, extra):  # noqa: F811
    r = httpx.post(f"{server}/v1/events", headers=h(), json={"customer": "acme", "type": "note", "title": "x", **extra})
    assert r.status_code == 422, r.text


# 13 -----------------------------------------------------------------------
def test_13_access_log_masks_path_keys():
    from brainkit.cli import _MaskKeys
    rec = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4", "GET", f"/k/{KEYS['chatgpt']}/v1/me", "1.1", 200), None)
    _MaskKeys().filter(rec)
    assert KEYS["chatgpt"] not in rec.getMessage() and "/k/***/v1/me" in rec.getMessage()


# 14 -----------------------------------------------------------------------
def test_14_cli_on_cp1252_console(repo, brain, agent):
    brain.log_event(agent, {"customer": "acme", "type": "note", "title": "Em dash — and ellipsis …"})
    env = {**os.environ, "BRAIN_ROOT": str(repo), "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run([sys.executable, "-m", "brainkit.cli", "context", "acme"], capture_output=True, env=env)
    assert r.returncode == 0, r.stderr


# 15 + 17 ------------------------------------------------------------------
async def test_15_17_trailing_slash_and_task_status(server):  # noqa: F811
    from mcp import Client
    async with Client(f"{server}/k/{KEYS['chatgpt']}/mcp/") as c:
        r = await c.call_tool("brain_list_tasks", {"customer": "acme", "status": "bogus"})
        assert r.is_error and "status must be" in r.content[0].text


# 19 -----------------------------------------------------------------------
def test_19_supersede_event_written_by_another_process():
    from .test_github_store import FakeGitHub
    from brainkit.service import Brain
    from brainkit.store import GitHubStore
    from .conftest import CUSTOMERS, agents_yaml
    fake = FakeGitHub()
    fake.files["_registry/customers.yaml"] = CUSTOMERS
    fake.files["_registry/agents.yaml"] = agents_yaml()
    mk = lambda: GitHubStore("o/r", "tok", client=httpx.Client(transport=httpx.MockTransport(fake.handler)),
                             tree_ttl=3600)
    a, b = Brain(mk()), Brain(mk())
    b.store.list("")  # warm b's tree cache before a writes
    r = a.log_event(a.registry().agents["chatgpt"], {"customer": "acme", "type": "decision", "title": "v1"})
    ok = b.log_event(b.registry().agents["chatgpt"], {"customer": "acme", "type": "decision", "title": "v2",
                                                      "supersedes": r["id"]})
    assert ok["ok"]
    # gateway commits carry the identity validate.yml keys on
    assert fake.last_put["author"] == {"name": "brain-gateway", "email": "brain-gateway@users.noreply.github.com"}
    assert fake.last_put["committer"] == fake.last_put["author"]


# 20 -----------------------------------------------------------------------
def test_20_github_counts_old_dated_commits(brain, store):
    commits = [{"sha": "b" * 40, "commit": {"message": "B", "committer": {"date": "2026-10-09T10:00:00Z"},
                                            "author": {"name": "x"}}, "author": None}]

    def handler(req):
        return httpx.Response(200, json=commits)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert run_ingestors(brain, store, ingestors=[GitHubCommitsIngestor(token="t", client=client, now=NOW)])["github"]["written"] == 1
    # A rebased commit with an OLDER committer date lands on top of b.
    commits.insert(0, {"sha": "c" * 40, "commit": {"message": "C rebased", "committer": {"date": "2026-09-01T00:00:00Z"},
                                                   "author": {"name": "x"}}, "author": None})
    res = run_ingestors(brain, store, ingestors=[GitHubCommitsIngestor(token="t", client=client, now=NOW)])
    assert res["github"]["written"] == 1
    assert any(e.title.startswith("1 commit to acme/app: C rebased") for e in brain.load_events("acme").events)


# ---------------- second review ----------------
def test_r2_1_meeting_moved_back_and_restored(monkeypatch, brain, store):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    d3, d5 = iso(NOW + timedelta(days=3)), iso(NOW + timedelta(days=5))
    item = {"id": "c1", "status": "confirmed", "summary": "Kickoff review", "updated": iso(NOW),
            "start": {"dateTime": d3}, "attendees": [{"email": "bob@acme.com"}]}
    state = {"items": [dict(item)]}

    def handler(req):
        if "oauth2" in str(req.url):
            return httpx.Response(200, json={"access_token": "at"})
        return httpx.Response(200, json=state)

    def run():
        ing = CalendarIngestor(accounts=[ACCT], client=httpx.Client(transport=httpx.MockTransport(handler)), now=NOW)
        run_ingestors(brain, store, ingestors=[ing])
        up = brain.context("acme").split("## Upcoming meetings")[1].split("##")[0]
        return [ln for ln in up.splitlines() if "Kickoff review" in ln]

    assert len(run()) == 1
    state["items"] = [dict(item, start={"dateTime": d5})]
    assert d5[:10] in run()[0]
    state["items"] = [dict(item)]                       # moved back
    lines = run()
    assert len(lines) == 1 and d3[:10] in lines[0]
    state["items"] = [{"id": "c1", "status": "cancelled"}]
    assert run() == []
    state["items"] = [dict(item)]                       # restored
    lines = run()
    assert len(lines) == 1 and d3[:10] in lines[0]


def test_r2_2_gmail_burst_within_one_hour(monkeypatch, brain):
    import brainkit.ingest.google as g
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "x")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "y")
    monkeypatch.setattr(g, "MAX_IDS_LISTED", 6)
    monkeypatch.setattr(g, "MAX_MESSAGES_PER_RUN", 2)
    base = NOW - timedelta(days=1)
    msgs = {f"m{i}": {"id": f"m{i}", "threadId": f"t{i}", "internalDate": str(int((base + timedelta(minutes=3 * i)).timestamp() * 1000)),
                      "labelIds": ["INBOX"], "snippet": "",
                      "payload": {"headers": [{"name": "From", "value": "a@acme.com"}, {"name": "Subject", "value": f"burst {i}"}]}}
            for i in range(10)}
    client = _gmail_mock(msgs)
    cursor = {"work": iso(base - timedelta(minutes=1))}
    seen = set()
    for _ in range(30):
        b = GmailIngestor(accounts=[ACCT], client=client, now=NOW).fetch(brain.registry(), cursor)
        seen |= {p["title"] for p in b.payloads}
        cursor = b.cursor
        if not cursor.get("work:backlog"):
            break
    assert seen == {f"burst {i}" for i in range(10)}


def test_r2_3_dedupe_across_processes(store):
    from brainkit.service import Brain
    later = lambda: datetime.now(timezone.utc) + timedelta(seconds=3)  # retry lands seconds later
    b1, b2 = Brain(store, registry_ttl=0), Brain(store, registry_ttl=0, now=later)
    ag = b1.registry().agents["chatgpt"]
    b2.log_event(ag, {"customer": "acme", "type": "note", "title": "warm", "dedupe_key": "warm"})  # warms b2 cache
    a = b1.log_event(ag, {"customer": "acme", "type": "note", "title": "zap", "dedupe_key": "zap:R11"})
    b = b2.log_event(ag, {"customer": "acme", "type": "note", "title": "zap", "dedupe_key": "zap:R11"})
    assert b["duplicate"] and b["id"] == a["id"]


@pytest.mark.parametrize("text", ["Account 1234567 renewal invoice attached", "Order number: Routing 004512",
                                  "PO GB12 ABCD 1234 5678 pending", "See ticket FR76 3000 1234", "The pw: changeme flag"])
def test_r2_4_business_text_not_redacted(text):
    from brainkit.redact import redact
    assert redact(text).kinds == []


@pytest.mark.parametrize("text,kind", [("IBAN DE89 3704 0044 0532 0130 00", "iban"),
                                       ("bank account number: 123456789012", "bank_account"),
                                       ("routing number 021000021", "bank_account")])
def test_r2_4_real_bank_data_redacted(text, kind):
    from brainkit.redact import redact
    assert redact(text).kinds == [kind]
