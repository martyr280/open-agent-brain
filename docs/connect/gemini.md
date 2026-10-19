# Connect Gemini

Confidence: high for Gemini CLI; moderate for the Gemini API SDK; **unknown** for custom MCP
servers in the consumer Gemini app (not documented when this was written — use the CLI or API).

## Gemini CLI

Inside this repo, Gemini CLI reads `GEMINI.md` → `AGENTS.md`. For MCP from any folder, add to
`~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "brain": {
      "httpUrl": "https://<host>/mcp",
      "headers": { "Authorization": "Bearer <gemini key>" }
    }
  }
}
```

Run `/mcp` in Gemini CLI to confirm the `brain_*` tools are listed.

## Gemini API (Python, google-genai)

The `google-genai` SDK accepts an MCP `ClientSession` as a tool. Open the session with the MCP
Python client against `https://<host>/mcp` and the bearer header, then pass it in `tools`.
Or call the REST endpoints as function tools (`/openapi.json` lists them).

## Git-only

Any Gemini agent with a shell: `BRAIN_AGENT_ID=gemini brain log ...`, then push.
