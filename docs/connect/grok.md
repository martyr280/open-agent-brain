# Connect Grok (xAI)

Confidence: high for the xAI API (remote MCP tools are documented); **unknown** for custom
connectors in the grok.com consumer app.

## xAI API — remote MCP tool

xAI supports remote MCP servers over Streamable HTTP and SSE. Fields: `server_url`,
`server_label`, optional `authorization` (sent as the Authorization header), `headers`,
`allowed_tools`. `require_approval` is not supported.

```python
from openai import OpenAI
client = OpenAI(api_key=XAI_API_KEY, base_url="https://api.x.ai/v1")
resp = client.responses.create(
    model="<current grok model>",
    input="Load the brain context for acme and list the open tasks.",
    tools=[{
        "type": "mcp",
        "server_url": "https://<host>/mcp",
        "server_label": "brain",
        "authorization": "<grok key>",
    }],
)
```

The gateway accepts the key with or without the `Bearer ` prefix, so this works whether xAI
adds the prefix or not. Alternative: the path-key URL `https://<host>/k/<grok key>/mcp` with no
auth fields.

## Grok bots in other tools

Any Grok-powered bot that can call HTTP can use REST: `GET /v1/customers/<c>/context`,
`POST /v1/events`, `POST /v1/handoff` with `Authorization: Bearer <grok key>`.

## Grok Bot (xAI's personal-agent builder)

Confidence: moderate (from xAI docs and a third-party guide; not tested against this gateway yet).

1. In a Grok Bot chat: "Add a custom MCP server called agent-brain at
   https://<host>/mcp". Say "custom server" or it may look for a
   marketplace plugin.
2. Confirm the name and URL, click **Add it**.
3. Auth: give the bot header name `Authorization`, value `Bearer <grok key>`. It stores the key
   as a header on the server entry. (Fallback: path-key URL `/k/<grok key>/mcp`, no auth.)
4. Check **Settings → Plugins → Yours**: agent-brain is enabled. Attach it in chat with `@`.
5. Test: "@agent-brain load the brain context for acme." House rules must come first.

## grok.com connectors

grok.com/connectors → **New Connector → Custom** → server URL + auth. On Grok Business /
Enterprise an admin provisions it first in the cloud console.
