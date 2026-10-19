# Operations runbook

## Daily
- Read `_global/GAPS.md` (or the Slack summary): inbound email with no follow-up, overdue and
  stale tasks, active customers with no activity, invalid event files.

## Correct a wrong event
Events are immutable. Write a new event with `supersedes: <wrong id>`:
```bash
brain log -c acme -t note --title "Correction: demo moved to Oct 14" --supersedes 01J9XQ...
```
The old event drops out of STATE/RECENT; search still finds it, marked lower.

## Remove a secret that got committed
1. `brain agent disable` / rotate the exposed credential at its source first. Assume it leaked.
2. Write a superseding event without it.
3. Purge history: `git filter-repo --path <file> --invert-paths` (or GitHub support for
   cached views), then force-push. Coordinate: everyone re-clones.

## Rotate or revoke an agent key
```bash
brain agent add chatgpt --rotate                  # new key; options you pass (--customers ...) are applied too
brain agent disable chatgpt                       # revoke
git push                                          # both commands commit locally; the push makes it live
```
The old key stops working within about a minute of the push (registry cache).
The gateway reads `_registry/agents.yaml` from GitHub, so a push is enough. No redeploy.

## Add a customer
```bash
brain customer add acme --name "Acme Corp" --alias acme --domain acme.com --repo acme/app --status prospect
git push
```

## Close a customer
Set `status: closed` in `_registry/customers.yaml`. History stays; gaps stop flagging it.

## Growth and archiving
- GitHub tree listing works up to ~100k files. At ~200 events/day that is over a year.
- When it gets near, move events older than 18 months to an archive repo
  (`git mv customers/*/events/2025 ...`) and keep the `_index` files.

## Troubleshooting
| Symptom | Check |
|---|---|
| 401 from the gateway | key wrong, rotated, or agent disabled; `GET /v1/me` |
| 403 `forbidden` | agent's `customers:` list excludes that customer |
| 404 `unknown_customer` | add the alias to `customers.yaml` |
| 422 `invalid_event` | the message names the field; factual types need `refs` |
| 429 | agent loop; raise `BRAIN_WRITES_PER_MINUTE` only if legitimate |
| Ingestor writes nothing | `match.email_domains` empty; run `brain ingest gmail --dry-run` |
| Google `invalid_grant` | refresh token expired/revoked; re-run `tools/google_oauth.py` |
| CI `existing event modified` | someone edited an event; revert and write a superseding event |
| STATE.md stale in git | it updates hourly; API agents get live context |
