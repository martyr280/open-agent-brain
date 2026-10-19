"""Event schema: the single unit of memory in the brain.

An event is one immutable Markdown file with YAML frontmatter. SCHEMA.md is
the human/agent-facing spec of this module; keep the two in sync (a test
checks that every EventType appears in SCHEMA.md).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ulid import is_ulid, new_ulid

SCHEMA_VERSION = 1
GLOBAL = "_global"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,62}$")
AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]{0,62}$")
TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-_.]{0,80}$")
REF_RE = re.compile(r"^[a-z][a-z0-9_\-]{0,30}:\S{1,500}$")

TITLE_MAX = 160
BODY_MAX = 8000


class EventType(str, Enum):
    email_sent = "email_sent"
    email_received = "email_received"
    message = "message"          # Slack / SMS / chat with a person
    meeting = "meeting"
    call = "call"
    build_change = "build_change"  # code / app change
    deploy = "deploy"
    doc = "doc"                  # document, deck, proposal, SOW created or sent
    decision = "decision"
    task_open = "task_open"
    task_update = "task_update"
    task_close = "task_close"
    handoff = "handoff"          # end-of-session summary for the next agent
    risk = "risk"                # blocker, issue, client concern
    note = "note"


# Types that describe something that happened in an external system.
# They must point at evidence (refs), which blocks invented history.
FACTUAL_TYPES = {
    EventType.email_sent,
    EventType.email_received,
    EventType.message,
    EventType.meeting,
    EventType.call,
    EventType.build_change,
    EventType.deploy,
    EventType.doc,
}
TASK_TYPES = {EventType.task_open, EventType.task_update, EventType.task_close}


class Status(str, Enum):
    done = "done"
    in_progress = "in_progress"
    open = "open"
    blocked = "blocked"
    scheduled = "scheduled"
    cancelled = "cancelled"


class Sensitivity(str, Enum):
    internal = "internal"
    client_confidential = "client-confidential"
    restricted = "restricted"


class AgentInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    vendor: str = "unknown"
    model: str | None = None
    surface: str | None = None
    operator: str | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not AGENT_ID_RE.match(v):
            raise ValueError(f"agent.id {v!r} must match {AGENT_ID_RE.pattern}")
        return v


def _utc(v: Any) -> datetime:
    if isinstance(v, str):
        s = v.strip().replace("Z", "+00:00")
        v = datetime.fromisoformat(s)
    if isinstance(v, date) and not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day, tzinfo=timezone.utc)
    if not isinstance(v, datetime):
        raise ValueError("expected ISO-8601 datetime")
    if v.tzinfo is None:
        raise ValueError("datetime must include a timezone (use Z for UTC)")
    return v.astimezone(timezone.utc).replace(microsecond=0)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    schema_version: int = SCHEMA_VERSION
    id: str = Field(default_factory=new_ulid)
    ts: datetime                       # when it happened
    logged_at: datetime | None = None  # when it was written (server sets)
    customer: str
    agent: AgentInfo
    type: EventType
    title: str
    status: Status | None = None
    refs: list[str] = Field(default_factory=list)
    task_id: str | None = None
    owner: str | None = None
    due: date | None = None
    supersedes: str | None = None
    sensitivity: Sensitivity = Sensitivity.client_confidential
    source: str = "agent"              # agent | ingestor:<name> | human
    dedupe_key: str | None = None
    tags: list[str] = Field(default_factory=list)
    body: str = ""

    @field_validator("ts", "logged_at", mode="before")
    @classmethod
    def _ts(cls, v: Any) -> Any:
        return None if v is None else _utc(v)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not is_ulid(v):
            raise ValueError("id must be a ULID (26 chars, Crockford base32)")
        return v

    @field_validator("supersedes")
    @classmethod
    def _sup(cls, v: str | None) -> str | None:
        if v is not None and not is_ulid(v):
            raise ValueError("supersedes must be the ULID of an earlier event")
        return v

    @field_validator("customer")
    @classmethod
    def _cust(cls, v: str) -> str:
        if v != GLOBAL and not SLUG_RE.match(v):
            raise ValueError(f"customer {v!r} must be a lowercase slug or {GLOBAL!r}")
        return v

    @field_validator("title")
    @classmethod
    def _title(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("title is required")
        if len(v) > TITLE_MAX:
            raise ValueError(f"title longer than {TITLE_MAX} chars")
        return v

    @field_validator("body")
    @classmethod
    def _body(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) > BODY_MAX:
            raise ValueError(f"body longer than {BODY_MAX} chars; link the full text in refs")
        return v

    @field_validator("refs")
    @classmethod
    def _refs(cls, v: list[str]) -> list[str]:
        out = []
        for r in v:
            r = r.strip()
            if not REF_RE.match(r):
                raise ValueError(f"ref {r!r} must look like 'scheme:value' (e.g. gmail:thread/18f2a, url:https://...)")
            out.append(r)
        return out

    @field_validator("task_id")
    @classmethod
    def _task(cls, v: str | None) -> str | None:
        if v is not None and not TASK_ID_RE.match(v):
            raise ValueError(f"task_id {v!r} must match {TASK_ID_RE.pattern}")
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return sorted({t.strip().lower() for t in v if t.strip()})

    @model_validator(mode="after")
    def _rules(self) -> "Event":
        if self.type in FACTUAL_TYPES and not self.refs:
            raise ValueError(
                f"type {self.type.value} needs at least one ref (evidence link): "
                "gmail:thread/<id>, github:<owner/repo>@<sha>, url:<link>, cal:<id>, file:<path> ..."
            )
        if self.type in TASK_TYPES and not self.task_id:
            raise ValueError(f"type {self.type.value} needs task_id")
        if self.type == EventType.task_close and self.status is None:
            self.status = Status.done
        if self.type == EventType.task_open and self.status is None:
            self.status = Status.open
        if self.supersedes == self.id:
            raise ValueError("an event cannot supersede itself")
        return self

    # ---------- file mapping ----------
    def path(self) -> str:
        base = GLOBAL if self.customer == GLOBAL else f"customers/{self.customer}"
        stamp = self.ts.strftime("%Y%m%dT%H%M%SZ")
        return f"{base}/events/{self.ts:%Y}/{self.ts:%m}/{stamp}_{self.agent.id}_{self.id}.md"

    def to_markdown(self) -> str:
        data = self.model_dump(mode="json", exclude_none=True, exclude={"body"})
        # Keep defaults that carry meaning; drop empty lists for readability.
        for k in ("refs", "tags"):
            if not data.get(k):
                data.pop(k, None)
        data["ts"] = _iso(self.ts)
        if self.logged_at:
            data["logged_at"] = _iso(self.logged_at)
        fm = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000).strip()
        body = self.body.strip()
        return f"---\n{fm}\n---\n{body}\n" if body else f"---\n{fm}\n---\n"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ParseError(ValueError):
    pass


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ParseError("file must start with '---' YAML frontmatter")
    parts = re.split(r"^---[ \t]*$", text, maxsplit=2, flags=re.M)
    if len(parts) < 3:
        raise ParseError("frontmatter not closed with '---'")
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as e:
        raise ParseError(f"invalid YAML frontmatter: {e}") from e
    if not isinstance(meta, dict):
        raise ParseError("frontmatter must be a YAML mapping")
    return meta, parts[2].strip("\n")


def parse_event(text: str) -> Event:
    meta, body = split_frontmatter(text)
    if "body" in meta:
        raise ParseError("'body' is not a frontmatter field; write it below the closing ---")
    meta["body"] = body
    return Event.model_validate(meta)


def expected_path_matches(ev: Event, path: str) -> bool:
    return path.replace("\\", "/").endswith(ev.path())
