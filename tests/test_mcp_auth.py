"""Tests for MCP bearer token authentication."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from brainlayer.mcp._auth import BearerAuthASGIMiddleware, LocalTokenVerifier

TEST_TOKEN = "test-secret-token-abc123"


# ── LocalTokenVerifier ──────────────────────────────────────────────


class TestLocalTokenVerifier:
    @pytest.fixture
    def verifier(self):
        return LocalTokenVerifier(TEST_TOKEN)

    def test_valid_token_returns_access_token(self, verifier):
        result = asyncio.run(verifier.verify_token(TEST_TOKEN))
        assert result is not None
        assert result.client_id == "local"
        assert result.scopes == ["read", "write"]
        assert result.token == TEST_TOKEN

    def test_invalid_token_returns_none(self, verifier):
        result = asyncio.run(verifier.verify_token("wrong-token"))
        assert result is None

    def test_empty_token_returns_none(self, verifier):
        result = asyncio.run(verifier.verify_token(""))
        assert result is None

    def test_whitespace_token_returns_none(self, verifier):
        result = asyncio.run(verifier.verify_token("   "))
        assert result is None

    def test_token_with_trailing_whitespace_matches(self, verifier):
        result = asyncio.run(verifier.verify_token(f"{TEST_TOKEN}\n"))
        assert result is not None
        assert result.token == TEST_TOKEN

    def test_uses_hmac_compare_digest(self, verifier):
        with patch("brainlayer.mcp._auth.hmac.compare_digest", return_value=True) as mock_cmp:
            asyncio.run(verifier.verify_token("any-token"))
            mock_cmp.assert_called_once()


# ── BearerAuthASGIMiddleware ─────────────────────────────────────────


def _make_scope(headers=None, scope_type="http"):
    """Build a minimal ASGI scope."""
    h = []
    if headers:
        for k, v in headers.items():
            h.append([k.encode(), v.encode()])
    return {"type": scope_type, "headers": h}


class TestBearerAuthASGIMiddleware:
    @pytest.fixture
    def inner_app(self):
        return AsyncMock()

    @pytest.fixture
    def middleware(self, inner_app):
        verifier = LocalTokenVerifier(TEST_TOKEN)
        return BearerAuthASGIMiddleware(inner_app, verifier)

    def test_no_auth_header_returns_401(self, middleware, inner_app):
        scope = _make_scope()
        send = AsyncMock()

        asyncio.run(middleware(scope, AsyncMock(), send))

        inner_app.assert_not_called()
        # Check 401 was sent
        start_call = send.call_args_list[0]
        assert start_call[0][0]["status"] == 401

    def test_wrong_scheme_returns_401(self, middleware, inner_app):
        scope = _make_scope({"authorization": "Basic abc123"})
        send = AsyncMock()

        asyncio.run(middleware(scope, AsyncMock(), send))

        inner_app.assert_not_called()
        start_call = send.call_args_list[0]
        assert start_call[0][0]["status"] == 401

    def test_wrong_token_returns_401(self, middleware, inner_app):
        scope = _make_scope({"authorization": "Bearer wrong-token"})
        send = AsyncMock()

        asyncio.run(middleware(scope, AsyncMock(), send))

        inner_app.assert_not_called()
        start_call = send.call_args_list[0]
        assert start_call[0][0]["status"] == 401

    def test_valid_token_passes_through(self, middleware, inner_app):
        scope = _make_scope({"authorization": f"Bearer {TEST_TOKEN}"})
        receive = AsyncMock()
        send = AsyncMock()

        asyncio.run(middleware(scope, receive, send))

        inner_app.assert_called_once_with(scope, receive, send)

    def test_non_http_scope_passes_through(self, middleware, inner_app):
        scope = _make_scope(scope_type="lifespan")
        receive = AsyncMock()
        send = AsyncMock()

        asyncio.run(middleware(scope, receive, send))

        inner_app.assert_called_once_with(scope, receive, send)

    def test_401_body_is_json(self, middleware, inner_app):
        scope = _make_scope()
        send = AsyncMock()

        asyncio.run(middleware(scope, AsyncMock(), send))

        body_call = send.call_args_list[1]
        body = json.loads(body_call[0][0]["body"])
        assert body == {"error": "unauthorized"}
