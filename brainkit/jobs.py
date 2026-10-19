"""Batch jobs run by CI / cron: rollup, validate, gaps."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from . import rollup
from .redact import find_secrets
from .registry import AGENTS_PATH, CUSTOMERS_PATH, Registry
from .schema import GLOBAL, Event, EventType, ParseError, parse_event
from .service import Brain, _pretty_validation, customer_dir, events_prefix, index_path
from .store import LocalStore

EVENT_PATH_RE = re.compile(r"^(?:customers/([a-z0-9][a-z0-9\-]*)|_global)/events/")


# ---------------- rollup ----------------
def run_rollup(brain: Brain, store: LocalStore, today: date | None = None) -> list[str]:
    today = today or datetime.now(timezone.utc).date()
    reg = brain.registry(fresh=True)
    files: dict[str, str | None] = {}
    summaries = []
    by_customer: dict[str, list[Event]] = {}
    names = {c.slug: c.name for c in reg.customers.values()}
    for slug in [GLOBAL, *sorted(reg.customers)]:
        # Rebuild the index from files, not from the (possibly stale) index.
        res = _load_from_files(store, slug)
        by_customer[slug] = res[0]
        files[index_path(slug)] = rollup.render_index(res[0], res[1])
        if slug == GLOBAL:
            continue
        c = reg.customers[slug]
        files[f"{customer_dir(slug)}/STATE.md"] = rollup.render_state(c.name, slug, res[0], today)
        files[f"{customer_dir(slug)}/RECENT.md"] = rollup.render_recent(c.name, slug, res[0], today)
        summaries.append(rollup.summarize(slug, c.name, c.status, res[0], today))
    files[f"{GLOBAL}/STATE.md"] = rollup.render_global_state(summaries, today)
    files[f"{GLOBAL}/RECENT.md"] = rollup.render_global_recent(by_customer, names, today)
    return store.put_many(files, "brain: rollup [skip ci]")


def _load_from_files(store: LocalStore, slug: str) -> tuple[list[Event], dict[str, str]]:
    events, paths = [], {}
    for p in store.list(events_prefix(slug)):
        if not p.endswith(".md"):
            continue
        try:
            ev = parse_event(store.read(p) or "")
        except (ParseError, ValidationError, ValueError):
            continue  # validate job reports these
        events.append(ev)
        paths[ev.id] = p
    return events, paths


# ---------------- validate ----------------
@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_repo(root: str | Path, immutable_against: str | None = None) -> Report:
    root = Path(root)
    store = LocalStore(root)
    rep = Report()
    try:
        reg = Registry.from_yaml(store.read(CUSTOMERS_PATH) or "", store.read(AGENTS_PATH) or "agents: []")
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"registry: {e}")
        return rep

    for d in (root / "customers").glob("*") if (root / "customers").is_dir() else []:
        if d.is_dir() and d.name not in reg.customers:
            rep.errors.append(f"customers/{d.name}/: no entry in {CUSTOMERS_PATH}")

    ids: dict[str, str] = {}
    supersedes: list[tuple[str, str, str]] = []
    for p in store.list(""):
        m = EVENT_PATH_RE.match(p)
        if not m:
            continue
        if p.endswith(".gitkeep"):
            continue
        if not p.endswith(".md"):
            rep.errors.append(f"{p}: only .md files belong in events/")
            continue
        rep.checked += 1
        text = store.read(p) or ""
        secrets = find_secrets(text)
        if secrets:
            rep.errors.append(f"{p}: contains secret-like content ({', '.join(sorted(set(secrets)))}); "
                              "rewrite the event with the value removed (git history needs a purge too)")
        try:
            ev = parse_event(text)
        except ValidationError as e:
            rep.errors.append(f"{p}: {_pretty_validation(e)}")
            continue
        except (ParseError, ValueError) as e:
            rep.errors.append(f"{p}: {e}")
            continue
        folder_customer = m.group(1) or GLOBAL
        if ev.customer != folder_customer:
            rep.errors.append(f"{p}: customer field {ev.customer!r} does not match folder {folder_customer!r}")
        if ev.customer != GLOBAL and ev.customer not in reg.customers:
            rep.errors.append(f"{p}: unknown customer {ev.customer!r}")
        if p != ev.path():
            rep.errors.append(f"{p}: path should be {ev.path()} (derived from ts, agent.id, id)")
        if ev.id in ids:
            rep.errors.append(f"{p}: duplicate id {ev.id} (also in {ids[ev.id]})")
        ids[ev.id] = p
        if ev.agent.id not in reg.agents:
            rep.warnings.append(f"{p}: agent {ev.agent.id!r} not in {AGENTS_PATH}")
        if ev.supersedes:
            supersedes.append((p, ev.supersedes, ev.customer))
    for p, target, cust in supersedes:
        tp = ids.get(target)
        if not tp:
            rep.errors.append(f"{p}: supersedes unknown id {target}")
        elif not tp.startswith(events_prefix(cust)):
            rep.errors.append(f"{p}: supersedes an event of another customer")

    if immutable_against:
        r = subprocess.run(["git", "-C", str(root), "diff", "--name-status", "--no-renames",
                            f"{immutable_against}...HEAD"], capture_output=True, text=True)
        if r.returncode != 0:
            rep.errors.append(f"git diff against {immutable_against} failed: {r.stderr.strip()}")
        else:
            for line in r.stdout.splitlines():
                status, _, path = line.partition("\t")
                if EVENT_PATH_RE.match(path) and path.endswith(".md") and status in ("M", "D"):
                    verb = "modified" if status == "M" else "deleted"
                    rep.errors.append(f"{path}: existing event {verb}. Events are immutable; "
                                      "write a new event with `supersedes:` instead")
    return rep


# ---------------- gaps ----------------
@dataclass
class Gap:
    customer: str
    kind: str
    detail: str
    since: datetime | None = None


AGENT_SOURCES = ("agent", "human")


def find_gaps(brain: Brain, now: datetime | None = None, stale_days: int = 14,
              reply_hours: int = 48, lookback_days: int = 14) -> list[Gap]:
    now = now or datetime.now(timezone.utc)
    today = now.date()
    reg = brain.registry()
    gaps: list[Gap] = []
    for slug, c in sorted(reg.customers.items()):
        res = brain.load_events(slug)
        act, _ = rollup.active_events(res.events)
        act = [e for e in act if e.ts <= now]  # future (scheduled) items are not activity
        for p, err in res.errors.items():
            gaps.append(Gap(slug, "invalid_event", f"{p}: {err[:200]}"))
        if c.status in ("closed",):
            continue
        last = act[-1] if act else None
        if c.status == "active" and (last is None or (now - last.ts).days >= stale_days):
            when = f"{last.ts:%Y-%m-%d}" if last else "never"
            gaps.append(Gap(slug, "stale", f"no events for {stale_days}+ days (last: {when})",
                            last.ts if last else None))
        window_start = now - timedelta(days=lookback_days)
        for e in act:
            if e.type != EventType.email_received or e.ts < window_start:
                continue
            if now - e.ts < timedelta(hours=reply_hours):
                continue
            followed = any(
                x.ts > e.ts and (x.type == EventType.email_sent or
                                 (x.source in AGENT_SOURCES and x.type != EventType.email_received))
                for x in act
            )
            if not followed:
                gaps.append(Gap(slug, "no_follow_up",
                                f"inbound email '{e.title}' ({e.ts:%Y-%m-%d}) has no reply or agent "
                                f"activity after it for {reply_hours}h+", e.ts))
        for t in rollup.build_tasks(act).values():
            if not t.is_open:
                continue
            if t.due and t.due < today:
                gaps.append(Gap(slug, "overdue_task", f"`{t.task_id}` {t.title} — due {t.due}", t.updated))
            elif t.updated and (now - t.updated).days >= stale_days:
                gaps.append(Gap(slug, "stale_task",
                                f"`{t.task_id}` {t.title} — no update since {t.updated:%Y-%m-%d}", t.updated))
        ingested = [e for e in act if e.source.startswith("ingestor:") and e.ts >= window_start]
        handoffs = [e for e in act if e.type == EventType.handoff]
        if ingested and (not handoffs or handoffs[-1].ts < ingested[-1].ts - timedelta(days=7)):
            gaps.append(Gap(slug, "no_handoff",
                            f"{len(ingested)} source events in {lookback_days}d but no agent handoff in the "
                            "7 days before the latest one", ingested[-1].ts))
    return gaps


def render_gaps(gaps: list[Gap], names: dict[str, str], now: datetime) -> str:
    out = [rollup.GENERATED_BANNER, f"# GAPS — {now:%Y-%m-%d %H:%MZ}", "", rollup.DATA_NOTICE, ""]
    if not gaps:
        out.append("_No gaps found._")
        return "\n".join(out) + "\n"
    kinds = {"invalid_event": "Invalid event files", "no_follow_up": "Inbound email without follow-up",
             "overdue_task": "Overdue tasks", "stale_task": "Stale tasks", "no_handoff": "Activity without handoff",
             "stale": "Quiet active customers"}
    for kind, label in kinds.items():
        rows = [g for g in gaps if g.kind == kind]
        if not rows:
            continue
        out.append(f"## {label} ({len(rows)})")
        for g in rows:
            out.append(f"- **{names.get(g.customer, g.customer)}** (`{g.customer}`): {g.detail}")
        out.append("")
    return "\n".join(out) + "\n"
