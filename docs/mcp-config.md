# MCP Configuration for BrainLayer

Add this to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "brainlayer": {
      "command": "python",
      "args": ["-m", "brainlayer.mcp"],
      "cwd": "/path/to/brainlayer"
    }
  }
}
```

Or if you have brainlayer installed globally:

```json
{
  "mcpServers": {
    "brainlayer": {
      "command": "brainlayer-mcp",
      "args": []
    }
  }
}
```

## Testing the MCP Server

1. Start the server manually to test:
   ```bash
   cd /path/to/brainlayer
   source .venv/bin/activate
   python -m brainlayer.mcp
   ```

2. In Claude Code, the tools should appear:
   - `brain_search` - Unified semantic search (query, file_path, chunk_id, filters)
   - `brain_store` - Persist memories (ideas, decisions, learnings)
   - `brain_recall` - Proactive retrieval (context, sessions, summaries)

   *Old `brainlayer_*` names still work as backward-compat aliases.*

## HTTP Transport (Multi-Session)

By default, each Claude Code session spawns its own `brainlayer-mcp` stdio process, loading a ~1.2GB embedding model. For multi-session setups, the HTTP transport shares a single daemon process across all sessions.

### Setup

1. Start the daemon with MCP enabled:
   ```bash
   brainlayer-daemon --http 8787 --mcp
   ```

2. On first start, an API key is auto-generated at `~/.config/brainlayer/api_key`. The daemon prints the exact `claude mcp add` command to copy:
   ```
   claude mcp add -s user --transport http \
     --header "Authorization:Bearer <your-key>" \
     brainlayer http://127.0.0.1:8787/mcp/
   ```

3. Paste that command to configure Claude Code. All sessions now share one daemon.

### Authentication

All `/mcp` requests require a bearer token in the `Authorization` header. This prevents unauthorized local processes from reading your conversation history or injecting false memories.

- **Auto-generated key**: `~/.config/brainlayer/api_key` (file permissions `0600`)
- **Environment override**: Set `BRAINLAYER_API_KEY` for CI/containers
- **Key rotation**: Delete the key file, restart the daemon, update all clients with the new command

### Security

- **Bearer auth required** on all MCP endpoints (HTTP API endpoints like `/health` remain unauthenticated for monitoring)
- **DNS rebinding protection** enabled — only `127.0.0.1`, `localhost`, and `[::1]` allowed in Host headers
- **Localhost-only by default** — daemon binds to `127.0.0.1`

### Network Deployment

For remote deployment (e.g., a shared server):

- Use `--host 0.0.0.0` to bind to all interfaces (the daemon logs a security warning)
- Place a reverse proxy (Caddy, nginx) in front for TLS
- The API key must be distributed to all clients securely
