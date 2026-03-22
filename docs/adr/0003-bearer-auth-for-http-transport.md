# ADR-0003: Bearer token authentication for HTTP MCP transport

**Status:** Accepted

**Date:** 2026-03-22

**Deciders:** DblD

## Context

BrainLayer's MCP server traditionally runs via stdio — spawned by Claude Code as a child process, communicating over stdin/stdout pipes. This has implicit security: only the parent process can read/write the pipes, the process runs as the same user, and it dies when the session ends.

The HTTP MCP transport (`--mcp` flag on the daemon) changes this fundamentally:

| Property | stdio | HTTP |
|----------|-------|------|
| Who can connect | Only parent process | Any process with network access |
| Authentication | Implicit (pipe ownership) | Must be explicit |
| Network surface | None | localhost:8787 (or 0.0.0.0) |
| Data exposure | Single session | All projects, all conversation history |

Without authentication, any local process — a compromised npm package, a rogue VS Code extension, a malicious script — could call `brain_store` to inject false decisions ("Always use `--no-verify`") that Claude would trust in future sessions. This is indirect prompt injection via persistent memory.

## Decision

Use a **file-based bearer token** verified via the MCP SDK's `TokenVerifier` protocol, enforced by a direct ASGI middleware wrapper.

Implementation:

1. **Key generation**: `secrets.token_urlsafe(32)` (256 bits entropy) written to `~/.config/brainlayer/api_key` with `0o600` permissions. Atomic file creation via `O_CREAT | O_EXCL` prevents TOCTOU races.
2. **Env var override**: `BRAINLAYER_API_KEY` takes precedence for CI/containers.
3. **Verification**: `LocalTokenVerifier` implements the `TokenVerifier` protocol using `hmac.compare_digest` for constant-time comparison.
4. **Enforcement**: `BearerAuthASGIMiddleware` wraps `StreamableHTTPSessionManager.handle_request` and rejects requests without a valid `Authorization: Bearer <token>` header (HTTP 401).
5. **DNS rebinding**: `TransportSecuritySettings` with explicit `allowed_hosts` covering `127.0.0.1`, `localhost`, and `[::1]`.

### Alternatives considered

| Alternative | Why rejected |
|-------------|-------------|
| **No authentication** | Unsafe — any localhost process can poison memories |
| **OAuth / OIDC** | Overkill for a local daemon. Requires an authorization server, token refresh, client registration. |
| **mTLS** | Complex key management. Poor developer experience for local use. |
| **SDK's `RequireAuthMiddleware` + `BearerAuthBackend`** | Requires Starlette's `AuthenticationMiddleware` to set `scope["user"]` — doesn't compose cleanly with `app.mount()` for raw ASGI callables. A direct 20-line middleware achieves the same security with no hidden dependencies. |

## Consequences

### Positive

- **Memory poisoning blocked** — unauthorized processes cannot call `brain_store`, `brain_update`, or `brain_digest`.
- **Zero-config for local use** — API key auto-generated on first start, `claude mcp add` command printed to terminal.
- **SDK-compatible** — implements the `TokenVerifier` protocol, so future upgrades to the MCP SDK's auth system can adopt our verifier.
- **Constant-time comparison** — `hmac.compare_digest` prevents timing attacks on the token.

### Negative

- **Same-user trust** — a process running as the same user can read `~/.config/brainlayer/api_key`. This is accepted as equivalent to the stdio trust model (same UID = trusted).
- **No key expiry or hot-reload** — the key is read once at daemon startup. Rotation requires restarting the daemon and updating all clients.
- **Every client needs the token** — adds a setup step compared to stdio (but the startup command is printed for easy copy-paste).

### Neutral

- Rate limiting is unnecessary: 256-bit entropy makes brute force infeasible.
- The `/health` endpoint remains unauthenticated — it exposes chunk count but not content. This is a conscious decision for monitoring tool compatibility.
