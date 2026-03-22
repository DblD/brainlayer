"""Bearer token authentication for HTTP MCP transport."""

import hmac
import json
import logging
from typing import Optional

from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class LocalTokenVerifier:
    """Verifies bearer tokens against a local shared secret.

    Implements the mcp.server.auth.provider.TokenVerifier protocol.
    """

    def __init__(self, expected_token: str):
        self._expected = expected_token.strip()

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Verify a bearer token. Returns AccessToken on match, None on mismatch."""
        cleaned = token.strip()
        if not cleaned:
            return None

        if hmac.compare_digest(cleaned.encode(), self._expected.encode()):
            return AccessToken(token=cleaned, client_id="local", scopes=["read", "write"])

        return None


class BearerAuthASGIMiddleware:
    """ASGI middleware that requires a valid Bearer token on HTTP requests.

    Wraps a raw ASGI app (e.g. StreamableHTTPSessionManager.handle_request)
    and rejects requests without a valid Authorization: Bearer <token> header.
    """

    def __init__(self, app, verifier: LocalTokenVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract Authorization header from raw ASGI headers
        auth_value = None
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                auth_value = value.decode()
                break

        if not auth_value or not auth_value.startswith("Bearer "):
            await self._send_401(send)
            return

        token = auth_value[7:]
        result = await self.verifier.verify_token(token)
        if result is None:
            await self._send_401(send)
            return

        await self.app(scope, receive, send)

    async def _send_401(self, send):
        body = json.dumps({"error": "unauthorized"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(body)).encode()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
