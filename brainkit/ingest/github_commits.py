"""GitHub commits -> build_change events.

Covers any app whose code lives in GitHub, including app builders with
GitHub sync turned on. Map repos to customers in _registry/customers.yaml
(sources.github_repos). One event per repo per run summarizes new commits,
so busy AI builders do not flood the brain.

Env: BRAIN_GITHUB_INGEST_TOKEN (fine-grained PAT, read-only Contents on the
mapped repos). Falls back to GITHUB_TOKEN.
"""

from __future__ import annotations

import os
from typing import Any

from .base import Batch, Ingestor, iso, parse_iso

API = os.environ.get("BRAIN_GITHUB_API", "https://api.github.com")
MAX_LISTED = 15
MAX_PAGES = 10


class GitHubCommitsIngestor(Ingestor):
    name = "github"
    agent_id = "ingest-github"

    def __init__(self, token: str | None = None, **kw):
        super().__init__(**kw)
        self.token = token or os.environ.get("BRAIN_GITHUB_INGEST_TOKEN") or os.environ.get("GITHUB_TOKEN")

    def configured(self) -> bool:
        return bool(self.token)

    def _get(self, url: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        r = self.client.get(url, params=params, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agent-brain"})
        if r.status_code == 409:  # empty repository
            return []
        if r.status_code != 200:
            raise RuntimeError(f"GET {url}: {r.status_code} {r.text[:200]}")
        return r.json()

    def fetch(self, registry, cursor: dict[str, Any]) -> Batch:
        """Walk back from the default branch head to the last head we saw.

        Uses commit ancestry, not commit dates, so rebased or late-pushed commits with
        old dates are still counted. Cursor per repo: {"head": sha, "ts": iso}.
        """
        batch = Batch(cursor=dict(cursor))
        for c in registry.customers.values():
            for repo in c.sources.github_repos:
                key = repo.lower()
                prev = cursor.get(key)
                last_head = prev.get("head") if isinstance(prev, dict) else None
                new: list[dict[str, Any]] = []
                found = last_head is None
                page = 1
                while page <= MAX_PAGES:
                    params: dict[str, Any] = {"per_page": 100, "page": page}
                    if last_head is None:  # first run: only the backfill window
                        params["since"] = iso(self.default_since())
                    rows = self._get(f"{API}/repos/{repo}/commits", params)
                    for cm in rows:
                        if cm["sha"] == last_head:
                            found = True
                            break
                        new.append(cm)
                    if found and last_head is not None or len(rows) < 100:
                        break
                    page += 1
                if last_head is not None and not found:
                    batch.notes.append(f"{repo}: previous head {last_head[:7]} not found in the last "
                                       f"{len(new)} commits (force-push or very busy repo); summarizing those")
                if not new:
                    continue
                head = new[0]  # API order: newest first
                head_ts = parse_iso(head["commit"]["committer"]["date"])
                lines = []
                for cm in new[:MAX_LISTED]:
                    msg = (cm["commit"]["message"] or "").splitlines()[0][:120] if cm["commit"]["message"] else ""
                    who = (cm.get("author") or {}).get("login") or cm["commit"]["author"].get("name", "?")
                    lines.append(f"- `{cm['sha'][:7]}` {msg} — {who}")
                if len(new) > MAX_LISTED:
                    lines.append(f"- … and {len(new) - MAX_LISTED} earlier commit(s)")
                first_msg = ((head["commit"]["message"] or "").splitlines() or ["(no message)"])[0]
                n = len(new)
                title = f"{n} commit{'s' if n != 1 else ''} to {repo}: {first_msg}"[:160]
                batch.payloads.append({
                    "customer": c.slug, "type": "build_change", "status": "done",
                    "ts": iso(min(head_ts, self.now)), "title": title, "body": "\n".join(lines),
                    "refs": [f"github:{repo}@{head['sha']}", f"url:https://github.com/{repo}/commit/{head['sha']}"],
                    "dedupe_key": f"github:{key}:{head['sha']}",
                    "agent": {"surface": "github"},
                })
                batch.cursor[key] = {"head": head["sha"], "ts": iso(head_ts)}
        return batch
