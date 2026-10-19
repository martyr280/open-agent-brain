"""GitHubStore against an in-memory fake of the GitHub REST API."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx
import pytest

from brainkit.registry import hash_key
from brainkit.service import Brain
from brainkit.store import GitHubStore, StoreError

from .conftest import CUSTOMERS, KEYS, agents_yaml


class FakeGitHub:
    def __init__(self):
        self.files: dict[str, str] = {}
        self.commits = 0
        self.fail_409_once: set[str] = set()
        self.calls: list[tuple[str, str]] = []

    def sha(self, text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        assert req.headers["Authorization"] == "Bearer tok"
        m = re.match(r"^/repos/o/r/contents/(.+)$", req.url.path)
        if m:
            path = m.group(1)
            if req.method == "GET":
                if path not in self.files:
                    return httpx.Response(404, json={"message": "Not Found"})
                t = self.files[path]
                return httpx.Response(200, json={"type": "file", "encoding": "base64", "sha": self.sha(t),
                                                 "content": base64.b64encode(t.encode()).decode()})
            if req.method == "PUT":
                body = json.loads(req.content)
                assert body["branch"] == "main"
                self.last_put = body
                if path in self.fail_409_once:
                    self.fail_409_once.discard(path)
                    return httpx.Response(409, json={"message": "conflict"})
                text = base64.b64decode(body["content"]).decode()
                if path in self.files:
                    if "sha" not in body:
                        return httpx.Response(422, json={"message": "Invalid request.\n\n\"sha\" wasn't supplied."})
                    if body["sha"] != self.sha(self.files[path]):
                        return httpx.Response(409, json={"message": "sha mismatch"})
                    status = 200
                else:
                    status = 201
                self.files[path] = text
                self.commits += 1
                return httpx.Response(status, json={"content": {"path": path}})
        if req.url.path == "/repos/o/r/git/trees/main":
            return httpx.Response(200, json={"truncated": False, "tree": [
                {"path": p, "type": "blob"} for p in sorted(self.files)]})
        return httpx.Response(500, json={"message": f"unexpected {req.method} {req.url.path}"})


@pytest.fixture
def fake():
    f = FakeGitHub()
    f.files["_registry/customers.yaml"] = CUSTOMERS
    f.files["_registry/agents.yaml"] = agents_yaml()
    f.files["customers/acme/README.md"] = "# Acme\nRemote profile.\n"
    return f


@pytest.fixture
def gh(fake):
    client = httpx.Client(transport=httpx.MockTransport(fake.handler), base_url="https://api.github.com")
    return GitHubStore("o/r", "tok", client=client, api="https://api.github.com", tree_ttl=0)


def test_read_create_put_list(gh, fake):
    assert gh.read("nope.md") is None
    gh.create("customers/acme/events/2026/10/a.md", "A", "msg")
    assert fake.files["customers/acme/events/2026/10/a.md"] == "A"
    with pytest.raises(FileExistsError):
        gh.create("customers/acme/events/2026/10/a.md", "B", "msg")
    gh.put("_state/x.json", "1", "m")
    gh.put("_state/x.json", "2", "m")
    assert fake.files["_state/x.json"] == "2"
    assert gh.list("customers/acme") == ["customers/acme/README.md", "customers/acme/events/2026/10/a.md"]
    assert "_state/x.json" in gh.list("")


def test_create_retries_on_409(gh, fake):
    fake.fail_409_once.add("e.md")
    gh.create("e.md", "x", "m")
    assert fake.files["e.md"] == "x"


@pytest.mark.parametrize("bad", ["../x", "/a/../b", ".git/config", "a//b"])
def test_rejects_unsafe_paths(gh, bad):
    with pytest.raises(StoreError):
        gh.read(bad)


def test_brain_over_github(gh, fake):
    brain = Brain(gh, registry_ttl=0)
    agent = brain.registry().agent_for_key(KEYS["chatgpt"])
    assert agent.id == "chatgpt"
    r = brain.log_event(agent, {"customer": "roadrunner", "type": "decision", "title": "Remote decision"})
    assert r["path"] in fake.files and fake.commits == 1
    dup = brain.log_event(agent, {"customer": "acme", "type": "note", "title": "d", "dedupe_key": "k1",
                                  "ts": "2026-10-01T00:00:00Z"})
    again = brain.log_event(agent, {"customer": "acme", "type": "note", "title": "d", "dedupe_key": "k1",
                                    "ts": "2026-10-01T00:00:00Z"})
    assert again["duplicate"] and again["id"] == dup["id"]
    ctx = brain.context("acme")
    assert "Remote profile." in ctx and "Remote decision" in ctx


def test_index_avoids_per_file_reads(gh, fake):
    """With an up-to-date index, loading events must not fetch each event file."""
    from brainkit import rollup
    brain = Brain(gh, registry_ttl=0)
    agent = brain.registry().agents["chatgpt"]
    for i in range(5):
        brain.log_event(agent, {"customer": "acme", "type": "note", "title": f"n{i}"})
    res = brain.load_events("acme")
    fake.files["_index/acme.jsonl"] = rollup.render_index(res.events, res.paths)
    gh._cache.clear()
    fake.calls.clear()
    assert len(brain.load_events("acme").events) == 5
    event_reads = [c for c in fake.calls if c[0] == "GET" and "/events/" in c[1]]
    assert event_reads == []
