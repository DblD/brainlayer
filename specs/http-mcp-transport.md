# Feature: Secure HTTP MCP Transport on Daemon

## Issue
N/A — local feature branch on `origin` (DblD/brainlayer). No upstream PR until rock-solid.

## Description
Mount `StreamableHTTPSessionManager` on the existing FastAPI daemon at `/mcp` so all coding sessions share one embedding model (~1.2GB) + vector store instead of each spawning its own process via stdio.

**This is a security boundary change.** Stdio transport has implicit security: same-UID process, pipe-only communication, dies with the session. HTTP transport breaks all of those guarantees. The feature must ship with authentication and transport security as first-class concerns, not afterthoughts.

### Security model comparison

| Property | stdio | HTTP (proposed) |
|----------|-------|-----------------|
| Who can connect | Only parent process (pipe) | Any process with network access |
| Authentication | Implicit (same UID, same pipe) | **Must be explicit** — bearer token |
| Network surface | None | localhost:8787 (or 0.0.0.0) |
| Lifetime | Dies with session | Long-lived daemon |
| Cross-origin risk | None | DNS rebinding possible |
| Data exposure | Single session | All projects, all history |

### Existing state
Commit `31c7803` on local `main` (unpushed) has a partial implementation — no auth, no transport security, no tests, dual cleanup paths. **This commit will be discarded and replaced.**

## User Story
As a developer running multiple Claude Code sessions, I want them to share a single authenticated brainlayer daemon so that I save 1.2GB RAM per session without exposing my conversation history to unauthorized processes.

## Out of Scope
- OAuth / OIDC (overkill for local daemon — file-based shared secret is sufficient)
- TLS built into the daemon (use reverse proxy for remote deployment)
- Write-scope restrictions (read-only mode) — future enhancement
- IP allowlisting for `--host 0.0.0.0` — future enhancement
- WebSocket transport
- Changes to the stdio MCP entry point
- New MCP tools
- Hot-reload of API key (key is read once at daemon startup; rotation requires restart — documented in ADR)

## Threat Model

### Vectors addressed by this spec

| # | Vector | Severity | Mitigation |
|---|--------|----------|------------|
| 1 | **No authentication** — any localhost process calls MCP tools | HIGH | Bearer token auth via MCP SDK `TokenVerifier` |
| 2 | **DNS rebinding** — malicious webpage reaches localhost via DNS trick | MEDIUM | `TransportSecuritySettings` with explicit `allowed_hosts` |
| 6 | **Memory poisoning** — inject false decisions via `brain_store` | CRITICAL | Blocked by #1 (auth required for all writes) |
| 7 | **Session hijacking** — MCP session UUIDs unbound to identity | MEDIUM | Bearer token binds sessions to authorized clients |

### Vectors deferred (require network deployment, out of scope)

| # | Vector | Severity | Future mitigation |
|---|--------|----------|-------------------|
| 3 | **Network exposure** (`--host 0.0.0.0`) | CRITICAL | IP allowlist + mandatory `--api-key` flag |
| 4 | **No TLS** — cleartext over network | HIGH | Reverse proxy (Caddy/nginx) |
| 5 | **CORS insufficient** — only blocks browsers | LOW | Non-issue once auth is required |

### Accepted risks
- **Same-user trust.** A process running as the same user can read `~/.config/brainlayer/api_key` and impersonate an authorized client. This is equivalent to the stdio trust model (same UID = trusted). We do not defend against same-user attacks.
- **No key expiry.** The API key file has no expiry. Rotation is manual (`rm` the file, restart daemon). Acceptable for local use.
- **No key hot-reload.** The key is read once at startup. Changing the file while the daemon is running has no effect until restart.
- **No rate limiting on auth failures.** `secrets.token_urlsafe(32)` produces 256 bits of entropy — brute force is infeasible. Rate limiting is unnecessary and would add complexity.
- **Health endpoint unauthenticated.** `/health` returns `{"status": "healthy", "chunks": N, "mcp": true}`. This leaks knowledge base size but not content. Acceptable for localhost monitoring. This is a conscious decision — monitoring tools and load balancers need unauthenticated health checks.
- **Starlette as transitive dependency.** `BearerAuthBackend` and `AuthenticationMiddleware` import from `starlette.*`. Starlette is a required dependency of FastAPI (already installed). No new pip dependency, but the chain is: `mcp` SDK → `starlette` types, and `fastapi` → `starlette` runtime.

## Relevant Files

| File | Action | Reason |
|------|--------|--------|
| `src/brainlayer/mcp/_shared.py` | Modify | `set_shared_state()` locking + `clear_shared_state()` |
| `src/brainlayer/mcp/_auth.py` | **Create** | `LocalTokenVerifier` + `BearerAuthASGIMiddleware` |
| `src/brainlayer/daemon.py` | Modify | Lifespan with auth + transport security, single cleanup path |
| `src/brainlayer/mcp/__init__.py` | Modify | Export `set_shared_state`, `clear_shared_state` |
| `src/brainlayer/paths.py` | Modify | Add `API_KEY_PATH`, `ensure_api_key()` |
| `tests/test_api_key.py` | **Create** | API key generation, permissions, env var override |
| `tests/test_shared_state.py` | **Create** | Thread safety tests for shared state |
| `tests/test_mcp_auth.py` | **Create** | Token verifier + ASGI middleware tests |
| `tests/test_daemon_mcp_transport.py` | **Create** | Integration tests: mount, health, auth rejection, DNS rebinding, cleanup |
| `docs/mcp-config.md` | Modify | HTTP transport setup with auth |
| `docs/adr/0003-bearer-auth-for-http-transport.md` | **Create** | ADR documenting auth decision |

## Step by Step Tasks

### 1. API key generation and path resolution
- **Task ID**: api-key-path
- **Depends On**: none
- **Assigned To**: worker
- **Parallel**: false
- In `src/brainlayer/paths.py`:
  - Add `API_KEY_DIR = Path.home() / ".config" / "brainlayer"`
  - Add `API_KEY_PATH = API_KEY_DIR / "api_key"`
  - Add `def ensure_api_key() -> str` with this precedence:
    1. `BRAINLAYER_API_KEY` env var — if set, return immediately (for CI/containers)
    2. `API_KEY_PATH` exists and non-empty — read and return (stripped)
    3. Generate new key — `secrets.token_urlsafe(32)` (256 bits entropy)
  - **Atomic file creation** to prevent TOCTOU race (two daemon starts simultaneously):
    ```python
    import os, secrets
    API_KEY_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = secrets.token_urlsafe(32)
    # O_CREAT | O_EXCL = atomic create-if-not-exists
    try:
        fd = os.open(str(API_KEY_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, key.encode())
        os.close(fd)
        logger.info(f"Generated API key at {API_KEY_PATH}")
    except FileExistsError:
        # Another process created it first — read theirs
        key = API_KEY_PATH.read_text().strip()
    return key
    ```
  - This eliminates the TOCTOU race: `O_CREAT | O_EXCL` is atomic on all POSIX systems
- Create `tests/test_api_key.py`:
  - Test generates key on first call (use tmp_path, patch `API_KEY_DIR` and `API_KEY_PATH`)
  - Test reads existing key on second call (same value returned)
  - Test env var override (`BRAINLAYER_API_KEY` takes precedence over file)
  - Test file permissions are 0o600 (`os.stat().st_mode & 0o777`, skip on Windows)
  - Test empty file triggers regeneration
  - Test concurrent creation (two threads calling `ensure_api_key` simultaneously — both get the same key)

### 2. Token verifier and auth middleware
- **Task ID**: token-verifier
- **Depends On**: api-key-path
- **Assigned To**: worker
- **Parallel**: false
- Create `src/brainlayer/mcp/_auth.py` with two components:

  **Component A: `LocalTokenVerifier`**
  - Implements `mcp.server.auth.provider.TokenVerifier` protocol
  - `__init__(self, expected_token: str)` — stores expected token
  - `async def verify_token(self, token: str) -> AccessToken | None`:
    - Strip whitespace from token (handles trailing newlines from file reads)
    - Constant-time comparison: `hmac.compare_digest(token.encode(), self.expected_token.encode())`
    - Match → `AccessToken(token=token, client_id="local", scopes=["read", "write"])`
    - Mismatch → `None`

  **Component B: `BearerAuthASGIMiddleware`**
  - Simple ASGI middleware (~20 lines) that checks `Authorization: Bearer <token>` header
  - **Why not use SDK's `RequireAuthMiddleware` + `BearerAuthBackend`?** Because `RequireAuthMiddleware` expects `scope["user"]` to be set by Starlette's `AuthenticationMiddleware`, which requires being wired as Starlette middleware — not as a wrapper around a mounted ASGI app. Since `session_manager.handle_request` is a raw ASGI callable mounted via `app.mount()`, the Starlette middleware stack doesn't apply. A direct ASGI middleware is simpler, has no hidden dependencies, and is easier to test.
  - Implementation:
    ```python
    class BearerAuthASGIMiddleware:
        """ASGI middleware that requires a valid Bearer token."""
        def __init__(self, app, verifier: LocalTokenVerifier):
            self.app = app
            self.verifier = verifier

        async def __call__(self, scope, receive, send):
            if scope["type"] != "http":
                await self.app(scope, receive, send)
                return

            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()

            if not auth.startswith("Bearer "):
                await self._send_401(send)
                return

            token = auth[7:]
            result = await self.verifier.verify_token(token)
            if result is None:
                await self._send_401(send)
                return

            await self.app(scope, receive, send)

        async def _send_401(self, send):
            await send({"type": "http.response.start", "status": 401,
                        "headers": [[b"content-type", b"application/json"]]})
            await send({"type": "http.response.body",
                        "body": b'{"error":"unauthorized"}'})
    ```
  - This is self-contained, testable without Starlette middleware stack, and has zero hidden dependencies

- Create `tests/test_mcp_auth.py`:
  - **LocalTokenVerifier tests:**
    - Valid token → AccessToken with `client_id="local"`, `scopes=["read", "write"]`
    - Invalid token → None
    - Empty token → None
    - Token with trailing whitespace/newlines → still matches (stripped)
    - Verify `hmac.compare_digest` is used (mock `hmac.compare_digest`, assert it was called)
  - **BearerAuthASGIMiddleware tests:**
    - No `Authorization` header → 401
    - `Authorization: Basic xxx` (wrong scheme) → 401
    - `Authorization: Bearer wrong` → 401
    - `Authorization: Bearer <valid>` → passes through to inner app
    - Non-HTTP scope (e.g. `lifespan`) → passes through without auth check

### 3. Shared state: locking and cleanup
- **Task ID**: shared-state
- **Depends On**: none
- **Assigned To**: worker
- **Parallel**: true (independent of tasks 1-2)
- In `src/brainlayer/mcp/_shared.py`:
  - Verify `set_shared_state()` acquires `_store_lock` and `_model_lock` (already fixed)
  - Add `clear_shared_state()` — acquires both locks, sets globals to `None`
- In `src/brainlayer/mcp/__init__.py`:
  - Add `clear_shared_state` to imports from `_shared`
- Create `tests/test_shared_state.py`:
  - Test `set_shared_state` sets both globals
  - Test `clear_shared_state` resets to None
  - Test concurrent set + get (10 threads each, no crashes or partial state)
  - Test `clear_shared_state` → `_get_vector_store` falls back to lazy init

### 4. Daemon lifespan: auth + transport security + single cleanup
- **Task ID**: daemon-lifespan
- **Depends On**: api-key-path, token-verifier, shared-state
- **Assigned To**: lead
- **Parallel**: false
- In `src/brainlayer/daemon.py`, refactor `lifespan()`:
  - Single cleanup path via `try/finally`
  - When `mcp_enabled`:
    - Call `ensure_api_key()` to get/generate the bearer token
    - Create `TransportSecuritySettings` with comprehensive `allowed_hosts`:
      ```python
      host = http_host or "127.0.0.1"
      allowed_hosts = [f"{host}:{http_port}"]
      # Always allow both localhost representations to prevent
      # client/server hostname mismatch rejections
      if host in ("127.0.0.1", "localhost", "0.0.0.0"):
          allowed_hosts.extend([
              f"127.0.0.1:{http_port}",
              f"localhost:{http_port}",
              f"[::1]:{http_port}",
          ])
      ```
    - Create `LocalTokenVerifier(api_key)`
    - Create `StreamableHTTPSessionManager(mcp_server, security_settings=...)`
    - Wrap with `BearerAuthASGIMiddleware(session_manager.handle_request, verifier)`
    - Mount at `/mcp`
    - Log setup command **without exposing the full token**:
      ```python
      logger.info(
          f"MCP transport ready on :{http_port}/mcp/ "
          f"(API key: {API_KEY_PATH})"
      )
      # Print the full command to stdout (not logger) for easy copy-paste
      # stdout is local terminal, not shipped to log aggregators
      print(
          f"\n  claude mcp add -s user --transport http "
          f'--header "Authorization:Bearer {api_key}" '
          f"brainlayer http://{host}:{http_port}/mcp/\n"
      )
      ```
    - On failure, log and continue without MCP
  - `finally` block: `clear_shared_state()` if MCP was enabled, then `vector_store.close()`
  - Add `http_host` global (alongside existing `http_port`, `mcp_enabled`)
  - `--host 0.0.0.0` → log security warning:
    ```python
    if host == "0.0.0.0":
        logger.warning(
            "SECURITY: --host 0.0.0.0 exposes the daemon to the network. "
            "All MCP tools (including brain_store) are accessible to any "
            "host that has the API key. Use a reverse proxy with TLS for "
            "production network deployments."
        )
    ```

### 5. Transport and auth integration tests
- **Task ID**: integration-tests
- **Depends On**: daemon-lifespan
- **Assigned To**: worker
- **Parallel**: false
- Create `tests/test_daemon_mcp_transport.py`:
  - **Fixture**: mock `vector_store` and `embedding_model` on `daemon_mod`, set `mcp_enabled=True`, set `http_port=8787`, set `http_host="127.0.0.1"`, patch `ensure_api_key` to return a known test token
  - **Test health reports MCP**: `/health` includes `"mcp": true` (health is unauthenticated — conscious decision)
  - **Test `/mcp/` rejects no auth**: POST with no `Authorization` header → 401
  - **Test `/mcp/` rejects wrong token**: `Authorization: Bearer wrong` → 401
  - **Test `/mcp/` accepts valid token**: correct bearer → not 401 (may be 400 for malformed MCP body — we test auth, not protocol)
  - **Test DNS rebinding blocked**: request with `Host: evil.com:8787` → rejected by transport security
  - **Test graceful degradation**: patch `StreamableHTTPSessionManager` to raise `ImportError` → daemon starts, `/health` works, no `/mcp` route
  - **Test cleanup**: after lifespan exit, `clear_shared_state` called, `vector_store` is None
  - **Test `--mcp` requires `--http`**: argparse validation
  - **Test `--host 0.0.0.0` logs security warning**: capture log output, assert warning present
  - **Test token not in log output**: capture logger output during startup, assert the full API key does NOT appear (only the file path)
- All tests use mocks — no real DB, no real model, no real MCP sessions

### 6. Documentation and ADR
- **Task ID**: docs
- **Depends On**: integration-tests
- **Assigned To**: worker
- **Parallel**: false
- Update `docs/mcp-config.md` with "HTTP Transport" section:
  - When to use (multi-session, resource sharing)
  - How to start: `brainlayer-daemon --http 8787 --mcp`
  - API key: auto-generated at `~/.config/brainlayer/api_key`, override with `BRAINLAYER_API_KEY`
  - Client setup: copy the `claude mcp add` command printed at startup
  - Security: bearer auth required, DNS rebinding protection, localhost-only by default
  - Network deployment: reverse proxy for TLS, `--host 0.0.0.0` security implications
  - Key rotation: delete `~/.config/brainlayer/api_key`, restart daemon, update all clients
- Create `docs/adr/0003-bearer-auth-for-http-transport.md`:
  - Context: stdio → HTTP changes the security boundary fundamentally
  - Decision: simple bearer token via file-based shared secret, using MCP SDK's `TokenVerifier` protocol with a direct ASGI middleware wrapper
  - Alternatives considered:
    - No auth (unsafe — memory poisoning via any localhost process)
    - OAuth/OIDC (overkill for local daemon)
    - mTLS (complex key management, poor DX for local use)
    - SDK's `RequireAuthMiddleware` + `BearerAuthBackend` stack (doesn't compose with `app.mount()` — requires Starlette `AuthenticationMiddleware` to set `scope["user"]`)
  - Consequences: every HTTP MCP client needs the bearer token; same-user processes can read the key file (accepted, equivalent to stdio trust model)
  - Rate limiting unnecessary: 256-bit key entropy makes brute force infeasible

### 7. Lint and validate
- **Task ID**: validate
- **Depends On**: docs
- **Assigned To**: lead
- **Parallel**: false
- `ruff check src/ tests/ && ruff format --check src/ tests/`
- `pytest tests/ -m "not integration" -x -v`
- `pytest tests/test_mcp_auth.py tests/test_shared_state.py tests/test_daemon_mcp_transport.py tests/test_api_key.py -v`
- Manual smoke test: start daemon with `--http 8787 --mcp`, verify:
  - `curl localhost:8787/health` → 200 (no auth required)
  - `curl -X POST localhost:8787/mcp/` → 401
  - `curl -X POST -H "Authorization: Bearer $(cat ~/.config/brainlayer/api_key)" localhost:8787/mcp/` → not 401

## Acceptance Criteria
- [ ] API key auto-generated on first start at `~/.config/brainlayer/api_key` with `0o600` permissions
- [ ] Key file creation is atomic (`O_CREAT | O_EXCL`) — no TOCTOU race
- [ ] `BRAINLAYER_API_KEY` env var overrides file-based key
- [ ] All `/mcp` requests require valid `Authorization: Bearer <token>` header
- [ ] Invalid/missing bearer token returns 401
- [ ] Token comparison uses `hmac.compare_digest` (constant-time)
- [ ] DNS rebinding protection enabled with `allowed_hosts` covering `127.0.0.1`, `localhost`, and `[::1]`
- [ ] `set_shared_state()` and `clear_shared_state()` are lock-safe
- [ ] Daemon lifespan has exactly one cleanup path
- [ ] MCP transport init failure → daemon continues serving HTTP API
- [ ] `/health` reports `"mcp": true` when transport is active (unauthenticated — conscious decision)
- [ ] `--mcp` without `--http` → parser error
- [ ] `--host 0.0.0.0` prints security warning to log
- [ ] Full API key appears only on stdout (for copy-paste), NOT in logger output
- [ ] All new code has corresponding tests
- [ ] `ruff check` and `pytest` pass clean
- [ ] ADR documents auth decision, alternatives, threat model, and accepted risks
- [ ] `docs/mcp-config.md` covers setup, auth, security, and key rotation

## Validation Commands
```bash
ruff check src/ tests/
ruff format --check src/ tests/
pytest tests/ -m "not integration" -x -v
pytest tests/test_mcp_auth.py tests/test_shared_state.py tests/test_daemon_mcp_transport.py tests/test_api_key.py -v
```

## Notes
- **Auth middleware: direct ASGI, not SDK stack.** The MCP SDK provides `RequireAuthMiddleware` + `BearerAuthBackend`, but these require Starlette's `AuthenticationMiddleware` to set `scope["user"]` before `RequireAuthMiddleware` reads it. Since we mount via `app.mount()` (raw ASGI), the Starlette middleware stack doesn't apply. A 20-line `BearerAuthASGIMiddleware` achieves the same security with zero hidden dependencies and is trivially testable. The ADR documents this decision.
- **Token in stdout vs logger.** The full `claude mcp add` command (containing the API key) is printed to stdout via `print()`, not through `logging`. Logger output may be shipped to aggregators, rotated to disk, or captured by monitoring. Stdout is the local terminal session only. This is a deliberate split.
- **No new pip dependencies.** All imports come from `mcp` SDK (already a dependency) or Python stdlib (`hmac`, `secrets`, `os`). Starlette types are transitive via FastAPI.
- **Env var precedence.** `BRAINLAYER_API_KEY` env var wins over file. Allows containers and CI to inject the key without filesystem. For local dev, auto-generated file is zero-config.
- **The unpushed commit `31c7803` must be discarded.** Before implementing: `git checkout main && git reset --hard origin/main`, then create feature branch from clean main.
- **All work on `origin` (DblD/brainlayer), feature branches only. No upstream PRs until reviewed, tested, and explicitly approved.**
