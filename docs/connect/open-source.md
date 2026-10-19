# Connect open-source models

The model itself does not matter. What matters is the client that runs it. Pick the row that
matches your client.

| Client | How | Confidence |
|---|---|---|
| Any MCP client that runs local commands (Claude Desktop, LM Studio, Goose, Continue, Cline, Open WebUI with MCP) | stdio: `brain mcp-stdio --agent local-oss` in a checkout | high for stdio support in general; check your client's MCP config format |
| Any MCP client with remote HTTP support | `https://<host>/mcp` + `Authorization: Bearer <local-oss key>`, or `https://<host>/k/<key>/mcp` | high |
| Agent frameworks (LangGraph, CrewAI, smolagents, AutoGen, LlamaIndex) | MCP adapters, or REST tools from `/openapi.json` | high |
| Aider, OpenHands, any agent with a shell | git checkout + `brain` CLI + push | high |
| Models with no tool use | Paste `brain context <customer>` output into the prompt; log results yourself | high |

## stdio config example (JSON format most MCP clients use)

```json
{
  "mcpServers": {
    "brain": {
      "command": "brain",
      "args": ["mcp-stdio", "--agent", "local-oss"],
      "env": { "BRAIN_ROOT": "C:/Users/<you>/agent-brain", "BRAIN_GIT_COMMIT": "1" }
    }
  }
}
```

stdio mode writes to the local checkout. Run `git pull --rebase && git push` after the
session, or schedule it.

## Smaller models

Small local models follow long protocols badly. Give them only these tools:
`brain_get_context`, `brain_log_event`, `brain_handoff`. Most MCP clients let you disable the
rest per server.
