"""CLI (subprocess), stdio MCP, and checks on the shipped repository itself."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from brainkit.jobs import validate_repo
from brainkit.registry import Registry

ROOT = Path(__file__).resolve().parents[1]


def run(repo: Path, *args: str, agent: str = "chatgpt", stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "BRAIN_ROOT": str(repo), "BRAIN_AGENT_ID": agent, "BRAIN_STORE": "local",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run([sys.executable, "-m", "brainkit.cli", *args], capture_output=True, text=True,
                          env=env, input=stdin, timeout=60)


def test_cli_flow(repo):
    assert run(repo, "resolve", "Road Runner").stdout.strip() == "acme"
    assert run(repo, "resolve", "nobody").returncode == 1
    r = run(repo, "log", "-c", "beta", "-t", "doc", "--title", "Sent proposal", "-r", "url:https://x.y/p")
    assert r.returncode == 0, r.stderr
    r = run(repo, "log", "-c", "beta", "-t", "handoff", "--title", "Wrap", "--body-file", "-", stdin="Next: wait")
    assert r.returncode == 0
    r = run(repo, "log", "-c", "beta", "-t", "doc", "--title", "no refs")
    assert r.returncode == 2 and "needs at least one ref" in r.stderr
    r = run(repo, "log", "-c", "beta", "-t", "note", "--title", "x", agent="ghost")
    assert r.returncode != 0 and "not registered" in r.stderr
    assert "Sent proposal" in run(repo, "context", "beta").stdout
    assert "Sent proposal" in run(repo, "search", "proposal").stdout
    assert "Wrap" in run(repo, "recent", "beta").stdout
    assert run(repo, "rollup").returncode == 0
    v = run(repo, "validate")
    assert v.returncode == 0 and "0 error(s)" in v.stdout
    g = run(repo, "gaps", "--write")
    assert g.returncode == 0 and (repo / "_global/GAPS.md").is_file()
    assert "beta-co" in run(repo, "customers").stdout


def test_cli_admin(repo):
    tpl = repo / "_templates/customer"
    tpl.mkdir(parents=True)
    (tpl / "README.md").write_text("# {name}\nslug {slug}\n")
    r = run(repo, "customer", "add", "newco", "--name", "New Co", "--alias", "nc", "--domain", "newco.com")
    assert r.returncode == 0, r.stderr
    assert (repo / "customers/newco/README.md").read_text() == "# New Co\nslug newco\n"
    r = run(repo, "customer", "add", "dup", "--name", "Dup", "--alias", "beta")
    assert r.returncode != 0  # alias collision
    r = run(repo, "agent", "add", "newbot", "--vendor", "mistral", "--customers", "newco")
    key = r.stdout.strip().splitlines()[-1]
    assert r.returncode == 0 and key.startswith("brk_") and len(key) == 52
    assert key not in (repo / "_registry/agents.yaml").read_text()
    reg = Registry.from_yaml((repo / "_registry/customers.yaml").read_text(),
                             (repo / "_registry/agents.yaml").read_text())
    assert reg.agent_for_key(key).id == "newbot"
    assert run(repo, "agent", "add", "newbot", "--vendor", "mistral").returncode != 0
    r2 = run(repo, "agent", "add", "newbot", "--vendor", "mistral", "--rotate")
    key2 = r2.stdout.strip().splitlines()[-1]
    reg = Registry.from_yaml((repo / "_registry/customers.yaml").read_text(),
                             (repo / "_registry/agents.yaml").read_text())
    assert reg.agent_for_key(key) is None and reg.agent_for_key(key2).id == "newbot"
    assert run(repo, "agent", "disable", "newbot").returncode == 0
    reg = Registry.from_yaml((repo / "_registry/customers.yaml").read_text(),
                             (repo / "_registry/agents.yaml").read_text())
    assert reg.agent_for_key(key2) is None
    assert validate_repo(repo).ok


async def test_mcp_stdio(repo):
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable, args=["-m", "brainkit.cli", "mcp-stdio", "--agent", "claude-desktop"],
        env={**os.environ, "BRAIN_ROOT": str(repo), "BRAIN_STORE": "local", "GIT_AUTHOR_NAME": "t",
             "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
    async with Client(params) as c:
        r = await c.call_tool("brain_log_event", {"customer": "acme", "type": "note", "title": "from stdio"})
        assert not r.is_error, r
        ctx = (await c.call_tool("brain_get_context", {"customer": "acme"})).content[0].text
        assert "from stdio" in ctx and "claude-desktop" in ctx


# ---------------- the shipped repository ----------------
def test_shipped_repo_validates():
    rep = validate_repo(ROOT)
    assert rep.ok, rep.errors


def test_shipped_registry_and_folders():
    reg = Registry.from_yaml((ROOT / "_registry/customers.yaml").read_text(),
                             (ROOT / "_registry/agents.yaml").read_text())
    folders = {p.name for p in (ROOT / "customers").iterdir() if p.is_dir()}
    assert folders == set(reg.customers)
    for slug in reg.customers:
        assert (ROOT / f"customers/{slug}/README.md").is_file()
    # Live repo: issued agents carry key HASHES (never raw keys). Check the format only.
    for a in reg.agents.values():
        if a.key_sha256:
            assert re.fullmatch(r"[0-9a-f]{64}", a.key_sha256), f"{a.id}: key_sha256 must be a sha256 hex digest"
            assert not a.key_sha256.startswith("brk_"), f"{a.id}: raw key in registry"
    for i in ("ingest-gmail", "ingest-gcal", "ingest-github"):
        assert reg.agents[i].kind == "ingestor"


def test_workflows_parse_and_reference_real_commands():
    for wf in (ROOT / ".github/workflows").glob("*.yml"):
        data = yaml.safe_load(wf.read_text())
        assert "jobs" in data
    cron = (ROOT / ".github/workflows/brain-cron.yml").read_text()
    for cmd in ("brain ingest all", "brain rollup", "brain gaps --write", "tools/commit_push.sh"):
        assert cmd in cron
    assert (ROOT / "tools/commit_push.sh").stat().st_mode & 0o111


def test_docs_links_resolve():
    for md in [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]:
        for link in re.findall(r"\]\(([^)#:]+\.md)\)", md.read_text()):
            assert (md.parent / link).resolve().is_file(), f"{md.name} -> {link}"


def test_agents_md_mentions_every_mcp_tool():
    from brainkit.gateway import mcp_tools
    src = Path(mcp_tools.__file__).read_text()
    tools = set(re.findall(r"async def (brain_\w+)\(", src))
    text = (ROOT / "AGENTS.md").read_text() + (ROOT / "docs/connect/open-source.md").read_text()
    for t in ("brain_get_context", "brain_log_event", "brain_handoff"):
        assert t in tools and t in text


def test_no_secrets_in_shipped_files():
    from brainkit.redact import find_secrets
    skip = {"tests", ".git", "__pycache__", "brainkit.egg-info"}
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and p.suffix in {".md", ".yaml", ".yml", ".py", ".toml", ".sh", ".example"}:
            if p.name == "redact.py":
                continue
            assert find_secrets(p.read_text(errors="ignore")) == [], p
