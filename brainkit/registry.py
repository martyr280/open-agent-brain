"""Customer and agent registries (_registry/*.yaml)."""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .schema import AGENT_ID_RE, GLOBAL, SLUG_RE

CUSTOMERS_PATH = "_registry/customers.yaml"
AGENTS_PATH = "_registry/agents.yaml"

CUSTOMER_STATUSES = {"active", "prospect", "internal", "paused", "closed"}
SCOPES = {"read", "write", "admin"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class Match(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email_domains: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    calendar_keywords: list[str] = Field(default_factory=list)

    @field_validator("email_domains", "emails")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [x.strip().lower() for x in v if x.strip()]


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    github_repos: list[str] = Field(default_factory=list)  # owner/repo


class Customer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str
    name: str
    status: str = "active"
    aliases: list[str] = Field(default_factory=list)
    team: list[str] = Field(default_factory=list)
    match: Match = Field(default_factory=Match)
    sources: Sources = Field(default_factory=Sources)
    snippet_chars: int = 200
    notes: str | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v) or v == GLOBAL:
            raise ValueError(f"bad customer slug {v!r}")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        if v not in CUSTOMER_STATUSES:
            raise ValueError(f"status must be one of {sorted(CUSTOMER_STATUSES)}")
        return v


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    vendor: str
    kind: str = "agent"  # agent | ingestor | human
    operator: str | None = None
    description: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["read", "write"])
    customers: list[str] = Field(default_factory=lambda: ["*"])
    key_sha256: str | None = None
    disabled: bool = False

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not AGENT_ID_RE.match(v):
            raise ValueError(f"bad agent id {v!r}")
        return v

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        bad = set(v) - SCOPES
        if bad:
            raise ValueError(f"unknown scopes {sorted(bad)}")
        return v

    def can(self, scope: str, customer: str | None = None) -> bool:
        if self.disabled:
            return False
        if scope not in self.scopes and "admin" not in self.scopes:
            return False
        if customer is None or "*" in self.customers:
            return True
        # Cross-customer notes (_global) may mention any customer, so a key limited to
        # some customers sees them only when "_global" is listed explicitly.
        return customer in self.customers


class Registry:
    def __init__(self, customers: list[Customer], agents: list[Agent]):
        self.customers = {c.slug: c for c in customers}
        self.agents = {a.id: a for a in agents}
        if len(self.customers) != len(customers):
            raise ValueError("duplicate customer slug in customers.yaml")
        if len(self.agents) != len(agents):
            raise ValueError("duplicate agent id in agents.yaml")
        self._alias: dict[str, str] = {}
        for c in customers:
            for key in [c.slug, c.name, *c.aliases]:
                k = norm(key)
                if not k:
                    continue
                prev = self._alias.get(k)
                if prev and prev != c.slug:
                    raise ValueError(f"alias {key!r} maps to both {prev} and {c.slug}")
                self._alias[k] = c.slug
        self._key_index = {a.key_sha256: a for a in agents if a.key_sha256}

    @classmethod
    def from_yaml(cls, customers_text: str, agents_text: str) -> "Registry":
        cdata = yaml.safe_load(customers_text) or {}
        adata = yaml.safe_load(agents_text) or {}
        customers = [Customer.model_validate(x) for x in (cdata.get("customers") or [])]
        agents = [Agent.model_validate(x) for x in (adata.get("agents") or [])]
        return cls(customers, agents)

    def resolve(self, name: str) -> str | None:
        if name == GLOBAL:
            return GLOBAL
        return self._alias.get(norm(name))

    def require(self, name: str) -> str:
        slug = self.resolve(name)
        if not slug:
            raise KeyError(f"unknown customer {name!r}; see {CUSTOMERS_PATH} or call brain_list_customers")
        return slug

    def agent_for_key(self, key: str) -> Agent | None:
        if not key:
            return None
        a = self._key_index.get(hash_key(key))
        return a if a and not a.disabled else None

    def customer_for_email(self, address: str) -> str | None:
        address = address.strip().lower()
        domain = address.rsplit("@", 1)[-1] if "@" in address else ""
        for c in self.customers.values():
            if address in c.match.emails:
                return c.slug
        for c in self.customers.values():
            for d in c.match.email_domains:
                if domain == d or domain.endswith("." + d):
                    return c.slug
        return None

    def customer_for_repo(self, repo: str) -> str | None:
        repo = repo.lower()
        for c in self.customers.values():
            if repo in [r.lower() for r in c.sources.github_repos]:
                return c.slug
        return None


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_key() -> str:
    return "brk_" + secrets.token_hex(24)


def dump_yaml(obj: dict[str, Any]) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=1000)
