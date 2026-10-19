# Security model

## What is protected and how

| Risk | Control |
|---|---|
| Unauthorized read/write | Every gateway request needs an agent key. Keys are stored only as SHA-256 hashes. Per-agent scopes and customer allow-lists. |
| Identity spoofing | The gateway sets `agent.id`, `vendor` and `operator` from the key. Payload values are ignored. The internal identity header is stripped from incoming requests. |
| Secrets in memory | Redaction of every free-text field on every write (title, body, refs, tags, owner, task id, dedupe key, model): API keys and tokens of major vendors, private keys, JWTs, Basic auth, URL credentials, password/secret/token assignments, card numbers (Luhn), SSNs, IBANs, labeled account/routing numbers. CI rejects files that still contain them. |
| Prompt injection via stored content | Instruction-like text is tagged `suspect-injection`. Every context response and generated file states that content is data, not instructions. `AGENTS.md` rule 1. |
| Invented history | Factual event types require evidence refs. Identity is attributable per agent. |
| Tampering / loss | Git history is the audit log. CI blocks modification or deletion of existing events on every PR and push; the hourly job re-checks everything since the `brain-validated` tag. |
| Runaway agent | Per-agent write rate limit (default 120/min). |
| Repo token leak | Gateway PAT is fine-grained: one repository, Contents only. |

## Residual risks (accepted, know them)

1. **Path keys** (`/k/<key>/mcp`) appear in client configs and possibly in logs of proxies in
   front of the gateway. The gateway masks them in its own access log (`/k/***`). Use header
   auth where the client supports it. Rotate if a URL is shared.
2. **Client data lives in GitHub.** Keep the repo private, limit collaborators, turn on 2FA
   and secret scanning. Do not store regulated data (PHI, card data) in events at all.
3. **Every reader sees every customer it is allowed.** Use `--customers` on keys for
   contractors and limited bots. A limited key does not see `_global` notes unless you list
   `_global` explicitly.
4. **Redaction is pattern-based.** It catches common formats, not all.
5. **No OAuth.** Static keys are simple and vendor-neutral. If a client requires OAuth, put
   an OAuth proxy (for example Cloudflare Access) in front of the gateway.
