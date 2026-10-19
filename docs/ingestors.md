# Ingestors

Ingestors write events from source systems, so the brain is complete even when an agent forgets
to log. They run in the hourly `brain-cron` GitHub Action. Each run is idempotent: the same
source item always maps to the same event id (`dedupe_key`), so re-runs and overlaps write nothing.

| Ingestor | Writes | Customer matching | Config |
|---|---|---|---|
| `gmail` | `email_sent` / `email_received`: subject, addresses, mailbox, first `snippet_chars` of the preview | sender/recipient address or domain in `match.emails` / `match.email_domains` | `GOOGLE_*` |
| `gcal` | `meeting` (`scheduled` if future, `done` if past). A moved meeting supersedes its earlier record; a cancelled one is marked `cancelled` | attendee address/domain, or `match.calendar_keywords` in the title | `GOOGLE_*` |
| `github` | one `build_change` per repo per run, listing the commits added since the last head seen (default branch only) | `sources.github_repos` | `BRAIN_GITHUB_INGEST_TOKEN` |

An item that fails validation (bad source data) is skipped and listed in
`_state/ingest/<name>.errors.json`; it does not block the cursor. A storage failure keeps the old
cursor, so the next run retries. A large Gmail backlog is processed oldest-first over several runs.

Email bodies are never stored, only the preview snippet (set `snippet_chars: 0` per customer to
store subject and addresses only). Secrets in subjects and snippets are redacted.

## Google setup (once)

1. Google Cloud Console → create a project → **APIs & Services → Library**: enable
   **Gmail API** and **Google Calendar API**.
2. **OAuth consent screen**: if your mail domain is a Google Workspace domain, choose
   **Internal**. With **External** in "Testing" status, Google expires refresh tokens after
   7 days; publish the app or use Internal to avoid that.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.** Copy the client ID and secret.
4. On your computer, once per mailbox:
   ```bash
   pip install httpx
   python tools/google_oauth.py --client-id <id> --client-secret <secret>
   ```
   Sign in with that mailbox. Copy the printed refresh token into the GitHub secret
   `GOOGLE_WORK_REFRESH_TOKEN` (or `GOOGLE_PERSONAL_REFRESH_TOKEN`).
5. Set the variables `GOOGLE_ACCOUNTS` and `GOOGLE_<ACCOUNT>_ADDRESSES` (see setup.md).

Account names other than `work` and `personal` need matching `env:` lines in
`.github/workflows/brain-cron.yml`.

## GitHub commits

1. Put each customer app in a GitHub repo (AI app builders usually offer a GitHub sync).
2. Add the repo to the customer: `sources: {github_repos: [owner/repo]}`.
3. Create a fine-grained PAT with **read-only Contents** on those repos; store it as
   `BRAIN_GITHUB_INGEST_TOKEN`.

## Run by hand

```bash
brain ingest gmail --dry-run     # shows what it would write, writes nothing
brain ingest all
```

The first run backfills `BRAIN_INGEST_BACKFILL_DAYS` (default 30). Cursors live in
`_state/ingest/*.json`; delete one to re-scan (dedupe prevents duplicates).
