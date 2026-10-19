"""Minimal ULID implementation (https://github.com/ulid/spec).

48-bit millisecond timestamp + 80 bits of randomness, Crockford base32.
`ulid_for(ts, seed)` produces a deterministic ULID: ingestors use it so the
same source item always maps to the same event id (idempotent re-runs).
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(out))


def _ms(ts: datetime | None) -> int:
    if ts is None:
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    ms = int(ts.timestamp() * 1000)
    if ms < 0 or ms >= 2**48:
        raise ValueError("timestamp out of ULID range")
    return ms


def new_ulid(ts: datetime | None = None) -> str:
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(_ms(ts), 10) + _encode(rand, 16)


def ulid_for(ts: datetime, seed: str) -> str:
    """Deterministic ULID: same (ts, seed) -> same id."""
    rand = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:10], "big")
    return _encode(_ms(ts), 10) + _encode(rand, 16)


def is_ulid(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 26
        and value[0] in "01234567"
        and all(c in _ALPHABET for c in value)
    )
