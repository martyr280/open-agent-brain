"""Secret and sensitive-identifier redaction.

Every write path (gateway, CLI, ingestors) passes text through `redact()`.
The CI validator uses `find_secrets()` to reject commits that slipped past.
Patterns are deliberately conservative: a false positive costs a
"[REDACTED:...]" marker; a false negative leaks a credential into git history.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{20,}")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("google_oauth", re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9\-]{10,}")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("brain_key", re.compile(r"\bbrk_[A-Za-z0-9]{32,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("google_client_secret", re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{20,}")),
    ("basic_auth", re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}={0,2}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s/@]+@")),
    ("us_ssn", re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")),
    ("bank_account", re.compile(r"(?i)\b(?:bank\s+)?(?:account|acct|routing|aba)\s*(?:number|no\.?|#)\s*[:#]?\s*\d{8,17}\b")),
]

# "password: X" style assignments. Skip values that are references, not secrets:
# ENV_VAR names, os.environ[...], ${VAR}, {{template}}, <placeholder>, function calls.
_ASSIGN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(password|passwd|pwd|passcode|secret|client[_-]?secret|api[_-]?key|"
    r"access[_-]?token|refresh[_-]?token|auth[_-]?token|token)"
    r"([ \t]*[:=][ \t]*|[ \t]+is[ \t]+)([\"']?)([^\s\"',;)]{6,})\3")
_REFERENCE = re.compile(r"^(?:\[REDACTED.*|[A-Z][A-Z0-9]*_[A-Z0-9_]+|(?:os|process|env|settings|config)\b.*|\$.*|\{\{.*|<.*|.*\(.*)$")

_PASSWORD_WORDS = {"password", "passwd", "pwd", "passcode"}

# IBANs are only redacted when the ISO 13616 mod-97 checksum is valid.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")


def _iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    s = s[4:] + s[:4]
    digits = "".join(str(int(ch, 36)) for ch in s)
    return int(digits) % 97 == 1

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


@dataclass
class RedactionResult:
    text: str
    kinds: list[str]


def redact(text: str | None) -> RedactionResult:
    if not text:
        return RedactionResult(text or "", [])
    kinds: list[str] = []
    out = text
    for kind, pat in _PATTERNS:
        out, n = pat.subn(f"[REDACTED:{kind}]", out)
        kinds.extend([kind] * n)

    def _card(m: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            kinds.append("payment_card")
            return "[REDACTED:payment_card]"
        return m.group(0)

    def _assign(m: re.Match[str]) -> str:
        if m.group(2).strip().lower() == "is" and m.group(1).lower() not in _PASSWORD_WORDS:
            return m.group(0)  # "token is expired" is prose, not a secret
        name, value = m.group(1).lower(), m.group(4)
        if name not in _PASSWORD_WORDS and not (re.search(r"\d", value) or len(value) >= 20):
            return m.group(0)  # "token: gmail.readonly" is prose; real tokens have digits or length
        if _REFERENCE.match(value):
            return m.group(0)
        kinds.append("password_assignment")
        return f"{m.group(1)}{m.group(2)}[REDACTED:password_assignment]"

    def _iban(m: re.Match[str]) -> str:
        if _iban_ok(m.group(0)):
            kinds.append("iban")
            return "[REDACTED:iban]"
        return m.group(0)

    out = _ASSIGN.sub(_assign, out)
    out = _IBAN.sub(_iban, out)
    out = _CARD.sub(_card, out)
    return RedactionResult(out, kinds)


def find_secrets(text: str) -> list[str]:
    return redact(text).kinds


# Text that tries to steer an agent. Not blocked (it may be a legitimate
# quote of a phishing email), but flagged so readers treat it as data.
_INJECTION = re.compile(
    r"(?i)(ignore (?:all |any )?(?:previous|prior|above) instructions"
    r"|disregard (?:the |your )?(?:system|previous) (?:prompt|instructions)"
    r"|you are now (?:a|an|in) "
    r"|new instructions\s*:"
    r"|system prompt\s*:"
    r"|<\s*/?\s*system\s*>"
    r"|as an ai (?:agent|assistant),? you must"
    r"|send (?:the|all|your) (?:credentials|api keys?|passwords?|tokens?) to)"
)


def looks_like_injection(text: str | None) -> bool:
    return bool(text) and bool(_INJECTION.search(text))
