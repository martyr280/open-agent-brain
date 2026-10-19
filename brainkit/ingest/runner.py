"""Run ingestors: fetch, write events (deduplicated), then save cursors."""

from __future__ import annotations

import logging
from typing import Any

from ..registry import Agent
from ..service import Brain, BrainError
from ..store import LocalStore, StoreError
from .base import Ingestor, dump_cursor, load_cursor, state_path
from .github_commits import GitHubCommitsIngestor
from .google import CalendarIngestor, GmailIngestor

log = logging.getLogger("brainkit.ingest")

ALL = {"gmail": GmailIngestor, "gcal": CalendarIngestor, "github": GitHubCommitsIngestor}


def _agent_for(brain: Brain, ing: Ingestor) -> Agent:
    a = brain.registry().agents.get(ing.agent_id)
    if a is not None:
        return a
    # Ingestors run inside CI with repo write access; they do not need a key.
    return Agent(id=ing.agent_id, vendor="system", kind="ingestor", scopes=["read", "write"])


def _apply_cancellations(brain: Brain, agent: Agent, name: str, prefixes: list[str], errors: list[str]) -> int:
    if not prefixes:
        return 0
    from datetime import datetime, timezone
    from ..rollup import active_events
    now = datetime.now(timezone.utc)
    written = 0
    for slug in brain.registry().customers:
        active, _ = active_events(brain._history(slug))
        for prefix in prefixes:
            prior = [e for e in active if e.dedupe_key and e.dedupe_key.startswith(prefix)
                     and e.status is not None and e.status.value != "cancelled"]
            if not prior:
                continue
            old = prior[-1]
            try:
                r = brain.log_event(agent, {
                    "customer": slug, "type": "meeting", "status": "cancelled",
                    "ts": min(old.ts, now).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "title": ("Cancelled: " + old.title)[:160], "refs": old.refs,
                    "dedupe_key": prefix + "cancelled", "supersedes": old.id,
                    "agent": {"surface": old.agent.surface or name},
                }, source=f"ingestor:{name}")
                written += 0 if r.get("duplicate") else 1
            except BrainError as e:
                errors.append(f"{prefix}cancelled: {e}")
    return written


def run_ingestors(brain: Brain, store, which: str = "all", dry_run: bool = False,
                  ingestors: list[Ingestor] | None = None) -> dict[str, dict[str, Any]]:
    if ingestors is None:
        names = list(ALL) if which == "all" else [which]
        ingestors = [ALL[n]() for n in names]
    reg = brain.registry(fresh=True)
    results: dict[str, dict[str, Any]] = {}
    for ing in ingestors:
        res: dict[str, Any] = {"configured": ing.configured()}
        results[ing.name] = res
        if not res["configured"]:
            res["skipped"] = "not configured (see docs/ingestors.md)"
            continue
        try:
            cursor = load_cursor(store, ing.name)
            batch = ing.fetch(reg, cursor)
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the others
            log.exception("ingestor %s failed", ing.name)
            res["error"] = f"{type(e).__name__}: {e}"
            continue
        res.update(fetched=len(batch.payloads), unmatched=batch.skipped, notes=batch.notes)
        if dry_run:
            res["sample"] = batch.payloads[:5]
            continue
        agent = _agent_for(brain, ing)
        written = dup = 0
        errors: list[str] = []
        try:
            for p in batch.payloads:
                p = dict(p)
                prefix = p.pop("_supersede_prefix", None)
                try:
                    r = brain.log_event(agent, p, source=f"ingestor:{ing.name}", supersede_dedupe_prefix=prefix)
                except BrainError as e:
                    # Deterministic rejection (bad source data): record it and move on, so one
                    # bad item never blocks the cursor.
                    errors.append(f"{p.get('dedupe_key')}: {e}")
                    continue
                if r.get("duplicate"):
                    dup += 1
                else:
                    written += 1
            written += _apply_cancellations(brain, agent, ing.name, batch.cancellations, errors)
        except StoreError as e:
            # Storage failure: keep the old cursor so the next run retries everything.
            res.update(written=written, duplicates=dup, error=f"store: {e}")
            continue
        res.update(written=written, duplicates=dup, errors=errors[:20])
        files = {state_path(ing.name): dump_cursor(batch.cursor)}
        if errors:
            files[state_path(ing.name).replace(".json", ".errors.json")] = dump_cursor(
                {"run_cursor": batch.cursor, "errors": errors[:200]})
        msg = f"brain: ingest cursor {ing.name} [skip ci]"
        if isinstance(store, LocalStore):
            store.put_many(files, msg)
        else:
            for path, text in files.items():
                store.put(path, text, msg)
    return results
