"""Gmail and Google Calendar ingestors (Google REST APIs, OAuth refresh tokens).

Accounts are configured with environment variables, so one deployment can
read several mailboxes (work + personal):

    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET     OAuth client (Desktop type)
    GOOGLE_ACCOUNTS=work,personal              account names
    GOOGLE_WORK_REFRESH_TOKEN=...              per account
    GOOGLE_WORK_ADDRESSES=you@example.com    the account's own addresses

Scopes needed on the refresh token: gmail.readonly and calendar.readonly.
Run `python tools/google_oauth.py` once per account to obtain the token.
"""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any

import httpx

from .base import Batch, Ingestor, iso, parse_iso

TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GCAL = "https://www.googleapis.com/calendar/v3"
MAX_MESSAGES_PER_RUN = 500
MAX_IDS_LISTED = 5000
OVERLAP = timedelta(hours=2)
# Google rate limits (Gmail: per-user query-cost per minute) come back as 429 or as
# 403 with a quota/rate reason. Back off and retry instead of failing the whole run.
RETRY_DELAYS = (2, 5, 10, 20, 40, 60)
RETRY_STATUSES = {429, 502, 503, 504}
RATE_403_MARKERS = ("rateLimitExceeded", "userRateLimitExceeded", "Quota exceeded", "quotaExceeded")
PACE_SECONDS = 0.05  # ~20 requests/s per account keeps a backfill under Gmail's per-user quota
# Calendar rejects updatedMin values that are too old (410 updatedMinTooLongAgo; roughly
# 4 weeks). Older cursors and first runs list by timeMin/timeMax only.
CAL_UPDATED_MIN_MAX_AGE = timedelta(days=20)


@dataclass
class GoogleAccount:
    name: str
    refresh_token: str
    addresses: set[str]


def accounts_from_env() -> list[GoogleAccount]:
    out = []
    for name in [n.strip() for n in os.environ.get("GOOGLE_ACCOUNTS", "").split(",") if n.strip()]:
        key = name.upper().replace("-", "_")
        rt = os.environ.get(f"GOOGLE_{key}_REFRESH_TOKEN")
        if not rt:
            continue
        addrs = {a.strip().lower() for a in os.environ.get(f"GOOGLE_{key}_ADDRESSES", "").split(",") if a.strip()}
        out.append(GoogleAccount(name, rt, addrs))
    return out


class _GoogleBase(Ingestor):
    def __init__(self, accounts: list[GoogleAccount] | None = None, **kw):
        super().__init__(**kw)
        self.accounts = accounts if accounts is not None else accounts_from_env()
        self._tokens: dict[str, str] = {}

    def configured(self) -> bool:
        return bool(self.accounts and os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET"))

    def _token(self, acct: GoogleAccount) -> str:
        if acct.name in self._tokens:
            return self._tokens[acct.name]
        r = self.client.post(TOKEN_URL, data={
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
            "refresh_token": acct.refresh_token, "grant_type": "refresh_token"})
        if r.status_code != 200:
            raise RuntimeError(f"google token refresh for {acct.name} failed: {r.status_code} {r.text[:200]}")
        tok = r.json()["access_token"]
        self._tokens[acct.name] = tok
        return tok

    _sleep = staticmethod(time.sleep)

    @staticmethod
    def _retryable(r: httpx.Response) -> bool:
        if r.status_code in RETRY_STATUSES:
            return True
        return r.status_code == 403 and any(m in r.text for m in RATE_403_MARKERS)

    def _get(self, acct: GoogleAccount, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        for attempt in range(len(RETRY_DELAYS) + 1):
            if PACE_SECONDS:
                self._sleep(PACE_SECONDS)
            r = self.client.get(url, params=params, headers={"Authorization": f"Bearer {self._token(acct)}"})
            if r.status_code == 200:
                return r.json()
            if attempt < len(RETRY_DELAYS) and self._retryable(r):
                wait = RETRY_DELAYS[attempt]
                ra = r.headers.get("retry-after", "")
                if ra.isdigit():
                    wait = max(wait, min(int(ra), 120))
                self._sleep(wait)
                continue
            raise RuntimeError(f"GET {url} ({acct.name}): {r.status_code} {r.text[:200]}")
        raise AssertionError("unreachable")


class GmailIngestor(_GoogleBase):
    name = "gmail"
    agent_id = "ingest-gmail"

    def fetch(self, registry, cursor: dict[str, Any]) -> Batch:
        batch = Batch(cursor=dict(cursor))
        for acct in self.accounts:
            since = parse_iso(cursor[acct.name]) if acct.name in cursor else self.default_since()
            # Overlap re-reads recent mail (dedupe drops repeats) to catch late arrivals.
            # While working through a backlog, skip the overlap so every run advances.
            backlog = bool(cursor.get(f"{acct.name}:backlog"))
            after = since if backlog else since - OVERLAP
            # A listing that hits MAX_IDS_LISTED only shows the NEWEST mail. Halve the time
            # window until the listing is complete, so the oldest mail is never skipped.
            window_end: datetime | None = None
            while True:
                span_end = window_end or self.now
                tiny = span_end - after <= timedelta(hours=1)
                # At the smallest window, list everything (cheap) so nothing is skipped.
                ids, complete = self._list_ids(acct, after, window_end, cap=None if tiny else MAX_IDS_LISTED)
                if complete or tiny:
                    break
                window_end = after + (span_end - after) / 2
            # Gmail lists newest first: take the OLDEST slice when capped.
            capped = len(ids) > MAX_MESSAGES_PER_RUN
            if capped:
                batch.notes.append(f"{acct.name}: {len(ids)} messages pending in window; processed oldest "
                                   f"{MAX_MESSAGES_PER_RUN}, next run continues")
                ids = ids[-MAX_MESSAGES_PER_RUN:]
            elif window_end is not None:
                batch.notes.append(f"{acct.name}: backlog; processed mail up to {iso(window_end)}")
            newest = since
            msgs = []
            for mid in ids:
                m = self._get(acct, f"{GMAIL}/messages/{mid}", {
                    "format": "metadata", "metadataHeaders": ["From", "To", "Cc", "Subject"]})
                msgs.append(m)
            msgs.sort(key=lambda m: int(m.get("internalDate", "0")))
            for m in msgs:
                ts = datetime.fromtimestamp(int(m.get("internalDate", "0")) / 1000, tz=timezone.utc)
                newest = max(newest, ts)
                labels = set(m.get("labelIds", []))
                if labels & {"SPAM", "TRASH", "CHAT"}:
                    continue
                payloads = self._to_payloads(registry, acct, m, ts)
                if not payloads:
                    batch.skipped += 1
                batch.payloads += payloads
            if window_end is not None and not capped:
                newest = max(newest, window_end)  # whole window done, even if it was empty
            batch.cursor[acct.name] = iso(newest)
            if capped or window_end is not None:
                batch.cursor[f"{acct.name}:backlog"] = True
            else:
                batch.cursor.pop(f"{acct.name}:backlog", None)
        return batch

    def _list_ids(self, acct: GoogleAccount, after: datetime, before: datetime | None,
                  cap: int | None = MAX_IDS_LISTED) -> tuple[list[str], bool]:
        q = f"after:{int(after.timestamp())} -in:chats -in:spam -in:trash"
        if before is not None:
            q += f" before:{int(before.timestamp())}"
        ids: list[str] = []
        page = None
        while True:
            params = {"q": q, "maxResults": 100, "includeSpamTrash": "false"}
            if page:
                params["pageToken"] = page
            data = self._get(acct, f"{GMAIL}/messages", params)
            ids += [m["id"] for m in data.get("messages", [])]
            page = data.get("nextPageToken")
            if not page:
                return ids, True
            if cap is not None and len(ids) >= cap:
                return ids, False

    def _to_payloads(self, registry, acct: GoogleAccount, m: dict[str, Any], ts: datetime) -> list[dict[str, Any]]:
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        frm = [a.lower() for _, a in getaddresses([headers.get("from", "")]) if a]
        to = [a.lower() for _, a in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if a]
        outbound = bool(frm) and frm[0] in acct.addresses
        counterparts = to if outbound else frm + to
        customers: list[str] = []
        for addr in counterparts:
            if addr in acct.addresses:
                continue
            slug = registry.customer_for_email(addr)
            if slug and slug not in customers:
                customers.append(slug)
        if not customers:
            return []
        subject = " ".join((headers.get("subject") or "").split())[:160] or "(no subject)"
        snippet = html.unescape(m.get("snippet", "")).strip()
        out = []
        for slug in customers[:3]:
            limit = registry.customers[slug].snippet_chars
            lines = [f"From: {headers.get('from', '')}", f"To: {headers.get('to', '')}"]
            if headers.get("cc"):
                lines.append(f"Cc: {headers['cc']}")
            lines.append(f"Mailbox: {acct.name}")
            if limit > 0 and snippet:
                lines += ["", snippet[:limit] + ("…" if len(snippet) > limit else "")]
            out.append({
                "customer": slug,
                "type": "email_sent" if outbound else "email_received",
                "ts": iso(ts), "title": subject, "body": "\n".join(lines), "status": "done",
                "refs": [f"gmail:thread/{m.get('threadId')}", f"gmail:msg/{m.get('id')}"],
                "dedupe_key": f"gmail:{acct.name}:{m.get('id')}",
                "sensitivity": "client-confidential",
                "agent": {"surface": f"gmail:{acct.name}"},
            })
        return out


class CalendarIngestor(_GoogleBase):
    name = "gcal"
    agent_id = "ingest-gcal"

    def fetch(self, registry, cursor: dict[str, Any]) -> Batch:
        batch = Batch(cursor=dict(cursor))
        for acct in self.accounts:
            updated_min = parse_iso(cursor[acct.name]) if acct.name in cursor else self.default_since()
            params: dict[str, Any] = {
                "singleEvents": "true", "showDeleted": "true", "maxResults": 250,
                "timeMin": iso(self.now - timedelta(days=60)), "timeMax": iso(self.now + timedelta(days=30)),
            }
            um = updated_min - OVERLAP
            if acct.name in cursor and self.now - um <= CAL_UPDATED_MIN_MAX_AGE:
                params["updatedMin"] = iso(um)
            newest = updated_min
            page = None
            while True:
                if page:
                    params["pageToken"] = page
                data = self._get(acct, f"{GCAL}/calendars/primary/events", params)
                for ev in data.get("items", []):
                    if ev.get("updated"):
                        newest = max(newest, parse_iso(ev["updated"]))
                    if ev.get("status") == "cancelled":
                        # Cancelled items often carry only id + status; the runner finds the
                        # earlier record by calendar id and marks it cancelled.
                        batch.cancellations.append(f"gcal:{acct.name}:{ev.get('id')}:")
                        continue
                    p = self._to_payloads(registry, acct, ev)
                    if not p:
                        batch.skipped += 1
                    batch.payloads += p
                page = data.get("nextPageToken")
                if not page:
                    break
            batch.cursor[acct.name] = iso(min(newest, self.now))
        return batch

    def _to_payloads(self, registry, acct: GoogleAccount, ev: dict[str, Any]) -> list[dict[str, Any]]:
        start = ev.get("start", {})
        if "dateTime" in start:
            ts = parse_iso(start["dateTime"])
        elif "date" in start:
            return []  # all-day items are rarely customer meetings
        else:
            return []
        attendees = [a.get("email", "").lower() for a in ev.get("attendees", []) if a.get("email")]
        summary = " ".join((ev.get("summary") or "").split())[:160] or "(no title)"
        customers: list[str] = []
        for addr in attendees:
            if addr in acct.addresses:
                continue
            slug = registry.customer_for_email(addr)
            if slug and slug not in customers:
                customers.append(slug)
        low = summary.lower()
        for c in registry.customers.values():
            if c.slug not in customers and any(k.lower() in low for k in c.match.calendar_keywords if k):
                customers.append(c.slug)
        out = []
        for slug in customers[:3]:
            body = [f"Attendees: {', '.join(attendees) or '(none listed)'}", f"Calendar: {acct.name}"]
            if ev.get("location"):
                body.append(f"Location: {ev['location'][:200]}")
            refs = [f"gcal:{ev.get('id')}"]
            if ev.get("htmlLink"):
                refs.append(f"url:{ev['htmlLink']}")
            out.append({
                "customer": slug, "type": "meeting", "ts": iso(ts), "title": summary,
                "status": "scheduled" if ts > self.now else "done",
                "body": "\n".join(body), "refs": refs,
                "dedupe_key": f"gcal:{acct.name}:{ev.get('id')}:{iso(ts)}",
                "agent": {"surface": f"gcal:{acct.name}"},
                # Runner: a moved meeting supersedes the earlier record of the same calendar event.
                "_supersede_prefix": f"gcal:{acct.name}:{ev.get('id')}:",
            })
        return out
