from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from brainkit.registry import hash_key
from brainkit.service import Brain
from brainkit.store import LocalStore

KEYS = {
    "claude-desktop": "brk_" + "a" * 48,
    "chatgpt": "brk_" + "b" * 48,
    "scoped-bot": "brk_" + "c" * 48,
    "reader": "brk_" + "d" * 48,
    "disabled-bot": "brk_" + "e" * 48,
}

CUSTOMERS = """
customers:
  - slug: acme
    name: Acme Corp
    status: active
    aliases: [acme inc, roadrunner]
    team: [alice]
    match: {email_domains: [acme.com], emails: [wile@gmail.com], calendar_keywords: [roadrunner]}
    sources: {github_repos: [acme/app]}
  - slug: beta-co
    name: Beta Co
    status: prospect
    aliases: [beta]
    match: {email_domains: [beta.io]}
    snippet_chars: 0
  - slug: quiet-co
    name: Quiet Co
    status: active
"""


def agents_yaml() -> str:
    return f"""
agents:
  - {{id: claude-desktop, vendor: anthropic, kind: agent, operator: alice, key_sha256: {hash_key(KEYS['claude-desktop'])}}}
  - {{id: chatgpt, vendor: openai, kind: agent, key_sha256: {hash_key(KEYS['chatgpt'])}}}
  - {{id: scoped-bot, vendor: xai, kind: agent, customers: [beta-co], key_sha256: {hash_key(KEYS['scoped-bot'])}}}
  - {{id: reader, vendor: google, kind: agent, scopes: [read], key_sha256: {hash_key(KEYS['reader'])}}}
  - {{id: disabled-bot, vendor: local, kind: agent, disabled: true, key_sha256: {hash_key(KEYS['disabled-bot'])}}}
  - {{id: alice, vendor: human, kind: human}}
  - {{id: ingest-gmail, vendor: system, kind: ingestor}}
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    (root / "_registry").mkdir(parents=True)
    (root / "_registry/customers.yaml").write_text(CUSTOMERS)
    (root / "_registry/agents.yaml").write_text(agents_yaml())
    for slug in ("acme", "beta-co", "quiet-co"):
        (root / f"customers/{slug}/events").mkdir(parents=True)
        (root / f"customers/{slug}/README.md").write_text(f"# {slug}\nProfile text for {slug}.\n")
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "test")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def store(repo: Path) -> LocalStore:
    return LocalStore(repo, commit=True, author="test <t@example.com>")


@pytest.fixture
def brain(store: LocalStore) -> Brain:
    return Brain(store, registry_ttl=0)


@pytest.fixture
def agent(brain: Brain):
    return brain.registry().agents["claude-desktop"]
