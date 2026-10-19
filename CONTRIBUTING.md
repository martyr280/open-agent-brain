# Contributing

## Setup

```bash
pip install -e ".[dev]"     # Python 3.11+
pytest -q                   # the full suite must pass
brain validate              # the shipped template must validate
```

## Rules

- **No real customer data.** Test fixtures use `example.com`, `acme`, `beta-co`. Never paste
  events, emails, names or IDs from a live brain into an issue, PR or test.
- **Keep storage vendor-neutral.** Events stay Markdown + YAML that any model and any human can
  read. New features must work through all three doors: MCP, REST/OpenAPI, and the CLI/files.
- **Events are immutable.** Do not add code paths that edit or delete event files. Corrections
  use `supersedes`.
- **Security first.** Changes to auth, scopes, redaction or the validator need a test that
  shows the attack they block. See `docs/security.md`.
- **Plain language in docs.** Short sentences, one instruction per step, active voice.

## Good first contributions

- New ingestors (Slack, Microsoft 365 mail/calendar, Linear, Jira) following `brainkit/ingest/base.py`.
- Connection guides for more MCP clients in `docs/connect/`.
- More secret formats in `brainkit/redact.py` (with tests for true and false positives).
