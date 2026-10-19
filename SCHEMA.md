# SCHEMA.md — Event format (schema_version 1)

One event = one immutable Markdown file: YAML frontmatter, then a Markdown body.
The code that enforces this is `brainkit/schema.py`.

## Example

```markdown
---
schema_version: 1
id: 01J9XQ7K2M3N4P5Q6R7S8T9V0W
ts: 2026-10-10T11:57:00Z
logged_at: 2026-10-10T11:58:12Z
customer: acme
agent:
  id: claude-desktop
  vendor: anthropic
  model: claude-opus-5-5
  surface: mcp
  operator: owner
type: build_change
title: Added PDF export to the reports page
status: done
refs:
- github:acme-org/acme-portal@a1b2c3d
sensitivity: client-confidential
source: agent
---
Verified against the live database (3 rows).
Next: the customer reviews the export layout.
```

## File path

```
customers/<customer>/events/<YYYY>/<MM>/<YYYYMMDDTHHMMSSZ>_<agent.id>_<id>.md
_global/events/<YYYY>/<MM>/<YYYYMMDDTHHMMSSZ>_<agent.id>_<id>.md
```

Year, month and stamp come from `ts`. The validator rejects a file whose path does not
match its frontmatter.

## Fields

| Field | Required | Rule |
|---|---|---|
| `schema_version` | no | `1` |
| `id` | yes | ULID: 26 characters, Crockford base32. The gateway and CLI assign it. By hand: any ULID generator. |
| `ts` | yes | When it happened. ISO-8601 with timezone, stored as UTC `Z`. Not in the future, except a `meeting` with `status: scheduled`. |
| `logged_at` | no | When it was written. Set by the gateway/CLI. |
| `customer` | yes | Customer slug from `_registry/customers.yaml`, or `_global`. |
| `agent` | yes | `id` (registered in `_registry/agents.yaml`), `vendor`; optional `model`, `surface`, `operator`. The gateway sets `id`/`vendor`/`operator` from the key; a payload cannot spoof them. |
| `type` | yes | See the type table. |
| `title` | yes | One line, max 160 characters. |
| `status` | no | `done`, `in_progress`, `open`, `blocked`, `scheduled`, `cancelled`. |
| `refs` | see types | List of `scheme:value` evidence links. |
| `task_id` | task types | Stable slug you choose: `[a-z0-9][a-z0-9-_.]*`. |
| `owner` | no | Person or agent responsible (tasks). |
| `due` | no | `YYYY-MM-DD` (tasks). |
| `supersedes` | no | `id` of an earlier event of the same customer that this one corrects. The old event drops out of STATE/RECENT. |
| `sensitivity` | no | `internal`, `client-confidential` (default), `restricted`. |
| `source` | no | `agent`, `human`, or `ingestor:<name>`. Set by the writer. |
| `dedupe_key` | no | Stable id from the source system. The same key always maps to the same event id, so a repeat write is ignored. |
| `tags` | no | Lowercase labels. The gateway adds `redacted` and `suspect-injection` when they apply. |
| body | no | Markdown under the closing `---`. Max 8,000 characters. Link long content with `refs`. |

## Event types

| type | Use it for | refs required |
|---|---|---|
| `email_sent` | An email that left our side | yes |
| `email_received` | An email from the customer | yes |
| `message` | Slack, Teams, SMS, WhatsApp, chat with a person | yes |
| `meeting` | A meeting (past `done`, future `scheduled`) | yes |
| `call` | A phone or video call | yes |
| `build_change` | A code or app change | yes |
| `deploy` | A release to production | yes |
| `doc` | A document, deck, proposal, SOW or report created or sent | yes |
| `decision` | A decision and who made it | no |
| `task_open` | A new task (`task_id` required) | no |
| `task_update` | Status, owner or due change (`task_id` required) | no |
| `task_close` | Task finished or cancelled (`task_id` required) | no |
| `handoff` | End-of-session summary: done, next, open questions | no |
| `risk` | A blocker, issue or customer concern | no |
| `note` | Anything else worth remembering | no |

## Ref schemes in use

`gmail:thread/<id>`, `gmail:msg/<id>`, `gcal:<id>`, `cal:<id>`, `url:<https link>`,
`github:<owner/repo>@<sha>`, `file:<path or drive id>`,
`drive:<file id>`, `slack:<channel>/<ts>`, `plaud:<recording id>`, `zoom:<meeting id>`,
`qbo:<entity>/<id>`, `invoice:<number>`. Any `lowercase-scheme:value` is valid; reuse
these when they fit.

## Corrections

Events never change. To fix an event, write a new one with `supersedes: <old id>` and
the correct content. To cancel a task, write `task_close` with `status: cancelled`.

## Generated files

`STATE.md`, `RECENT.md` (per customer and in `_global`), `_global/GAPS.md` and `_index/*`
are rebuilt from events by `brain rollup` / `brain gaps`. Do not edit them.
