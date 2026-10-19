"""Ingestor framework: pull from a source system, write deduplicated events."""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

STATE_DIR = "_state/ingest"


@dataclass
class Batch:
    payloads: list[dict[str, Any]] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    skipped: int = 0     # items with no customer match
    notes: list[str] = field(default_factory=list)
    cancellations: list[str] = field(default_factory=list)  # dedupe-key prefixes of cancelled items


class Ingestor(ABC):
    name: str = "base"
    agent_id: str = "ingest-base"

    def __init__(self, client: httpx.Client | None = None, now: datetime | None = None):
        self.client = client or httpx.Client(timeout=30.0)
        self.now = now or datetime.now(timezone.utc)

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def fetch(self, registry, cursor: dict[str, Any]) -> Batch: ...

    def default_since(self) -> datetime:
        days = int(os.environ.get("BRAIN_INGEST_BACKFILL_DAYS", "30"))
        return self.now - timedelta(days=days)


def state_path(name: str) -> str:
    return f"{STATE_DIR}/{name}.json"


def load_cursor(store, name: str) -> dict[str, Any]:
    raw = store.read(state_path(name))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def dump_cursor(cursor: dict[str, Any]) -> str:
    return json.dumps(cursor, indent=2, sort_keys=True) + "\n"


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
