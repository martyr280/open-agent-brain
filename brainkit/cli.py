"""`brain` command-line interface.

Local mode (default) works on a git checkout: coding agents (Claude Code,
Codex CLI, Gemini CLI, Aider, any OSS agent with a shell) use this instead
of the gateway. Set BRAIN_STORE=github to act on the remote repo directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .registry import AGENTS_PATH, CUSTOMERS_PATH, Agent, dump_yaml, hash_key, new_key
from .schema import GLOBAL, EventType
from .service import Brain, BrainError
from .store import LocalStore, StoreError, store_from_env


def _brain() -> Brain:
    return Brain(store_from_env())


def _local(args) -> LocalStore:
    st = store_from_env()
    if not isinstance(st, LocalStore):
        sys.exit("this command needs a local checkout (BRAIN_STORE=local)")
    return st


def _acting_agent(brain: Brain, agent_id: str | None) -> Agent:
    agent_id = agent_id or os.environ.get("BRAIN_AGENT_ID")
    if not agent_id:
        sys.exit("set --agent or BRAIN_AGENT_ID (must exist in _registry/agents.yaml)")
    reg = brain.registry()
    a = reg.agents.get(agent_id)
    if not a:
        sys.exit(f"agent {agent_id!r} is not registered; run: brain agent add {agent_id} --vendor <vendor>")
    return a


def cmd_log(args) -> int:
    brain = _brain()
    agent = _acting_agent(brain, args.agent)
    body = args.body or ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8") if args.body_file != "-" else sys.stdin.read()
    payload = {
        "customer": args.customer, "type": args.type, "title": args.title, "body": body,
        "refs": args.ref or [], "tags": args.tag or [],
        "agent": {"model": args.model, "surface": args.surface or "cli"},
    }
    for k in ("ts", "status", "task_id", "owner", "due", "supersedes", "dedupe_key"):
        v = getattr(args, k)
        if v:
            payload[k] = v
    if args.sensitivity:
        payload["sensitivity"] = args.sensitivity
    res = brain.log_event(agent, payload)
    print(json.dumps(res, indent=2))
    return 0


def cmd_context(args) -> int:
    print(_brain().context(args.customer, budget_chars=args.budget))
    return 0


def cmd_search(args) -> int:
    rows = _brain().search(args.query, customer=args.customer, types=args.type, since=args.since, limit=args.limit)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"{r['ts']}  [{r['customer']}] {r['type']}: {r['title']}  ({r['agent']})  {r.get('path', '')}")
        if not rows:
            print("no matches")
    return 0


def cmd_recent(args) -> int:
    rows = _brain().recent(args.customer, days=args.days, limit=args.limit, types=args.type)
    print(json.dumps(rows, indent=2) if args.json else
          "\n".join(f"{r['ts']} {r['type']}: {r['title']} ({r['agent']})" for r in rows) or "no events")
    return 0


def cmd_tasks(args) -> int:
    rows = _brain().tasks(args.customer, status=args.status)
    print(json.dumps(rows, indent=2))
    return 0


def cmd_customers(args) -> int:
    for c in _brain().list_customers():
        print(f"{c['slug']:<28} {c['status']:<9} {c['name']}")
    return 0


def cmd_resolve(args) -> int:
    slug = _brain().registry().resolve(args.name)
    print(slug or "")
    return 0 if slug else 1


def cmd_rollup(args) -> int:
    from .jobs import run_rollup
    st = _local(args)
    changed = run_rollup(Brain(st), st)
    print(f"rollup: {len(changed)} file(s) changed")
    for p in changed:
        print("  " + p)
    return 0


def cmd_validate(args) -> int:
    from .jobs import validate_repo
    st = _local(args)
    rep = validate_repo(st.root, immutable_against=args.immutable_against)
    for w in rep.warnings:
        print(f"WARN  {w}")
    for e in rep.errors:
        print(f"ERROR {e}")
    print(f"validate: {rep.checked} event file(s) checked, {len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
    return 0 if rep.ok else 1


def cmd_gaps(args) -> int:
    from .jobs import find_gaps, render_gaps
    st = store_from_env()
    brain = Brain(st)
    now = datetime.now(timezone.utc)
    gaps = find_gaps(brain, now=now)
    names = {s: c.name for s, c in brain.registry().customers.items()}
    text = render_gaps(gaps, names, now)
    if args.write:
        if isinstance(st, LocalStore):
            st.put_many({f"{GLOBAL}/GAPS.md": text}, "brain: gaps report [skip ci]")
        else:
            st.put(f"{GLOBAL}/GAPS.md", text, "brain: gaps report [skip ci]")
    print(text)
    hook = os.environ.get("BRAIN_GAPS_WEBHOOK")
    if hook and gaps:
        import httpx
        summary = f"Brain gaps: {len(gaps)} item(s). " + "; ".join(
            f"{g.customer}: {g.kind}" for g in gaps[:15])
        httpx.post(hook, json={"text": summary}, timeout=15)
    return 0


def cmd_ingest(args) -> int:
    from .ingest.runner import run_ingestors
    st = store_from_env()
    results = run_ingestors(Brain(st), st, args.source, dry_run=args.dry_run)
    print(json.dumps(results, indent=2, default=str))
    return 1 if any(r.get("error") for r in results.values()) else 0


class _MaskKeys:
    """Access-log filter: never write path keys (/k/<key>/...) to logs."""

    import re as _re
    _pat = _re.compile(r"/k/[^/\s]+")

    def filter(self, record) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._pat.sub("/k/***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._pat.sub("/k/***", record.msg)
        return True


def cmd_serve(args) -> int:
    import logging

    import uvicorn
    from .gateway.app import create_app
    logging.getLogger("uvicorn.access").addFilter(_MaskKeys())
    uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    return 0


def cmd_mcp_stdio(args) -> int:
    """MCP over stdio for local clients (Claude Desktop, Gemini CLI, Ollama front ends, LM Studio...)."""
    from .gateway.mcp_tools import build_mcp
    brain = _brain()
    agent = _acting_agent(brain, args.agent)
    build_mcp(brain, lambda ctx: agent).run("stdio")
    return 0


def cmd_agent_add(args) -> int:
    st = _local(args)
    data = yaml.safe_load(st.read(AGENTS_PATH) or "agents: []") or {}
    agents = data.get("agents") or []
    data["agents"] = agents
    existing = next((a for a in agents if a.get("id") == args.id), None)
    if not existing and not args.vendor:
        sys.exit("--vendor is required for a new agent")
    if existing and not args.rotate:
        sys.exit(f"agent {args.id} exists; use --rotate to issue a new key (and apply any changed options)")
    key = new_key()
    given = {"vendor": args.vendor, "kind": args.kind, "operator": args.operator, "description": args.description,
             "scopes": args.scopes.split(",") if args.scopes else None,
             "customers": args.customers.split(",") if args.customers else None}
    given = {k: v for k, v in given.items() if v is not None}
    if existing:
        rec = {**existing, **given, "key_sha256": hash_key(key), "disabled": False}
    else:
        rec = {"id": args.id, "kind": "agent", "scopes": ["read", "write"], "customers": ["*"], **given,
               "key_sha256": hash_key(key), "disabled": False}
    Agent.model_validate(rec)
    if existing:
        agents[agents.index(existing)] = rec
    else:
        agents.append(rec)
    st.put_many({AGENTS_PATH: dump_yaml(data)}, f"brain: {'rotate' if existing else 'register'} agent {args.id}")
    print(f"agent {args.id}: scopes={rec['scopes']} customers={rec['customers']}")
    print("KEY (shown once; store it in the agent's secret settings):")
    print(key)
    return 0


def cmd_agent_disable(args) -> int:
    st = _local(args)
    data = yaml.safe_load(st.read(AGENTS_PATH) or "agents: []") or {}
    for a in data.get("agents") or []:
        if a.get("id") == args.id:
            a["disabled"] = True
            st.put_many({AGENTS_PATH: dump_yaml(data)}, f"brain: disable agent {args.id}")
            print(f"agent {args.id} disabled")
            return 0
    sys.exit(f"agent {args.id} not found")


def cmd_customer_add(args) -> int:
    st = _local(args)
    data = yaml.safe_load(st.read(CUSTOMERS_PATH) or "customers: []") or {}
    custs = data.setdefault("customers", []) or []
    data["customers"] = custs
    if any(c.get("slug") == args.slug for c in custs):
        sys.exit(f"customer {args.slug} exists")
    rec = {"slug": args.slug, "name": args.name, "status": args.status,
           "aliases": [a for a in (args.alias or [])], "team": args.team.split(",") if args.team else [],
           "match": {"email_domains": args.domain or [], "emails": [], "calendar_keywords": []},
           "sources": {"github_repos": args.repo or []}}
    from .registry import Customer, Registry
    Customer.model_validate(rec)
    custs.append(rec)
    Registry.from_yaml(dump_yaml(data), st.read(AGENTS_PATH) or "agents: []")  # alias collision check
    tpl = Path(st.root, "_templates/customer/README.md")
    readme = tpl.read_text(encoding="utf-8") if tpl.is_file() else "# {name}\n"
    readme = readme.replace("{name}", args.name).replace("{slug}", args.slug)
    st.put_many({
        CUSTOMERS_PATH: dump_yaml(data),
        f"customers/{args.slug}/README.md": readme,
        f"customers/{args.slug}/events/.gitkeep": "",
    }, f"brain: add customer {args.slug}")
    print(f"customer {args.slug} added")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="brain", description="Shared, model-agnostic memory for agent teams.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("log", help="write one event")
    s.add_argument("--customer", "-c", required=True)
    s.add_argument("--type", "-t", required=True, choices=[t.value for t in EventType])
    s.add_argument("--title", required=True)
    s.add_argument("--body", default="")
    s.add_argument("--body-file", help="path, or - for stdin")
    s.add_argument("--ref", "-r", action="append", help="scheme:value, repeatable")
    s.add_argument("--tag", action="append")
    s.add_argument("--status")
    s.add_argument("--task-id", dest="task_id")
    s.add_argument("--owner")
    s.add_argument("--due")
    s.add_argument("--ts", help="ISO-8601 UTC time the action happened (default now)")
    s.add_argument("--supersedes")
    s.add_argument("--dedupe-key", dest="dedupe_key")
    s.add_argument("--sensitivity", choices=["internal", "client-confidential", "restricted"])
    s.add_argument("--agent")
    s.add_argument("--model")
    s.add_argument("--surface")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("context", help="print the boot context for a customer")
    s.add_argument("customer")
    s.add_argument("--budget", type=int, default=14000)
    s.set_defaults(fn=cmd_context)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--customer", "-c")
    s.add_argument("--type", "-t", action="append")
    s.add_argument("--since")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("recent")
    s.add_argument("customer")
    s.add_argument("--days", type=int, default=14)
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--type", "-t", action="append")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_recent)

    s = sub.add_parser("tasks")
    s.add_argument("customer")
    s.add_argument("--status", choices=["open", "closed", "all"], default="open")
    s.set_defaults(fn=cmd_tasks)

    sub.add_parser("customers").set_defaults(fn=cmd_customers)
    s = sub.add_parser("resolve", help="alias -> customer slug")
    s.add_argument("name")
    s.set_defaults(fn=cmd_resolve)

    sub.add_parser("rollup", help="regenerate STATE/RECENT/index files").set_defaults(fn=cmd_rollup)
    s = sub.add_parser("validate", help="check every event file (CI)")
    s.add_argument("--immutable-against", help="git ref; fail if existing events were modified/deleted")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("gaps", help="report missing follow-ups, overdue tasks, quiet customers")
    s.add_argument("--write", action="store_true", help="write _global/GAPS.md")
    s.set_defaults(fn=cmd_gaps)
    s = sub.add_parser("ingest", help="pull Gmail / Calendar / GitHub into events")
    s.add_argument("source", nargs="?", default="all", choices=["all", "gmail", "gcal", "github"])
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_ingest)
    s = sub.add_parser("serve", help="run the gateway (MCP + REST)")
    s.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("mcp-stdio", help="run the MCP tools over stdio for a local client")
    s.add_argument("--agent")
    s.set_defaults(fn=cmd_mcp_stdio)

    ag = sub.add_parser("agent", help="manage agent identities and keys").add_subparsers(dest="sub", required=True)
    s = ag.add_parser("add")
    s.add_argument("id")
    s.add_argument("--vendor", help="anthropic | openai | google | xai | meta | mistral | local | ... (required for a new agent)")
    s.add_argument("--kind", choices=["agent", "ingestor", "human"], help="default: agent")
    s.add_argument("--operator")
    s.add_argument("--description")
    s.add_argument("--scopes", help="comma list of read,write,admin (default read,write)")
    s.add_argument("--customers", help="comma list of slugs (add _global for cross-customer notes), or * (default)")
    s.add_argument("--rotate", action="store_true")
    s.set_defaults(fn=cmd_agent_add)
    s = ag.add_parser("disable")
    s.add_argument("id")
    s.set_defaults(fn=cmd_agent_disable)

    cu = sub.add_parser("customer", help="manage customers").add_subparsers(dest="sub", required=True)
    s = cu.add_parser("add")
    s.add_argument("slug")
    s.add_argument("--name", required=True)
    s.add_argument("--status", default="active")
    s.add_argument("--alias", action="append")
    s.add_argument("--domain", action="append", help="email domain for ingestion matching")
    s.add_argument("--repo", action="append", help="owner/repo whose commits map to this customer")
    s.add_argument("--team")
    s.set_defaults(fn=cmd_customer_add)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except BrainError as e:
        print(f"error ({e.code}): {e}", file=sys.stderr)
        return 2
    except StoreError as e:
        print(f"store error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
