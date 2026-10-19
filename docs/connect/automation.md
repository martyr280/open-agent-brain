# Connect automations (n8n, Zapier, Make) — Plaud, Zoom, Slack, forms

Use the `automation` agent key. Send one `POST /v1/events` per item, with a `dedupe_key` so
retries never create duplicates.

```http
POST https://<host>/v1/events
Authorization: Bearer <automation key>
Content-Type: application/json

{
  "customer": "acme",
  "type": "meeting",
  "ts": "2026-10-09T15:00:00Z",
  "title": "Weekly build review (Plaud transcript)",
  "body": "Summary: ...\nDecisions: ...\nAction items: ...",
  "refs": ["plaud:REC123"],
  "dedupe_key": "plaud:REC123",
  "agent": {"surface": "n8n:plaud"}
}
```

Recipes:

| Source | Trigger | type | dedupe_key |
|---|---|---|---|
| Plaud | new transcript / summary | `meeting` or `call` | `plaud:<recording id>` |
| Zoom | recording or summary completed | `meeting` | `zoom:<meeting uuid>` |
| Slack | message in a customer channel | `message` | `slack:<channel>/<ts>` |
| Typeform / Tally | new response | `note` | `form:<response id>` |
| QuickBooks | invoice sent / paid | `doc` / `note` | `qbo:invoice/<id>` |

Map the customer with the customer's slug or any alias. Unknown names return HTTP 404 with
`unknown_customer`; route those to a review queue.

The automation key may only write what you allow: issue it with
`brain agent add automation --vendor system --kind ingestor --customers <list> --rotate`, then
`git push`.

`dedupe_key` is matched against the current (not superseded) events from the same agent and
customer, read fresh from the store on every write, so a retry is ignored even if it carries no
`ts` or lands on a different gateway worker. Send `ts` anyway (the time the source item happened).
