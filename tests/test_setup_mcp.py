"""Tests for brainlayer setup-mcp CLI command."""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from brainlayer.cli import app

runner = CliRunner()

TEST_TOKEN = "test-setup-token-abc123"


@pytest.fixture(autouse=True)
def mock_api_key():
    with patch("brainlayer.paths.ensure_api_key", return_value=TEST_TOKEN):
        yield


class TestSetupMcpDaemonAlreadyRunning:
    def test_skips_start_if_daemon_running_with_mcp(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "healthy", "chunks": 100, "mcp": True}

        with (
            patch("httpx.get", return_value=mock_resp),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = runner.invoke(app, ["setup-mcp"])

        assert result.exit_code == 0
        assert "already running" in result.output
        assert "Setup complete" in result.output

    def test_exits_if_daemon_running_without_mcp(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "healthy", "chunks": 100}

        with patch("httpx.get", return_value=mock_resp):
            result = runner.invoke(app, ["setup-mcp"])

        assert result.exit_code == 1
        assert "MCP not enabled" in result.output


class TestSetupMcpDaemonStart:
    def test_starts_daemon_and_registers(self):
        mock_healthy = MagicMock()
        mock_healthy.json.return_value = {"status": "healthy", "chunks": 50, "mcp": True}

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("refused")
            return mock_healthy

        with (
            patch("httpx.get", side_effect=side_effect),
            patch("subprocess.Popen") as mock_popen,
            patch("subprocess.run") as mock_run,
            patch("time.sleep"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = runner.invoke(app, ["setup-mcp"])

        assert result.exit_code == 0
        mock_popen.assert_called_once()
        assert "Daemon started" in result.output
        assert "Setup complete" in result.output

    def test_exits_if_daemon_fails_to_start(self):
        with (
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
            patch("subprocess.Popen"),
            patch("time.sleep"),
        ):
            result = runner.invoke(app, ["setup-mcp"])

        assert result.exit_code == 1
        assert "failed to start" in result.output


class TestSetupMcpClaudeRegistration:
    def test_claude_mcp_add_failure_prints_manual_command(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "healthy", "chunks": 100, "mcp": True}

        with (
            patch("httpx.get", return_value=mock_resp),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=1, stderr="some error"),
            ]
            result = runner.invoke(app, ["setup-mcp"])

        assert result.exit_code == 1
        assert "Run manually" in result.output

    def test_custom_port_and_scope(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "healthy", "chunks": 100, "mcp": True}

        with (
            patch("httpx.get", return_value=mock_resp),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            result = runner.invoke(app, ["setup-mcp", "--port", "9999", "--scope", "project"])

        assert result.exit_code == 0
        assert "9999" in result.output

        add_call = mock_run.call_args_list[-1]
        args = add_call[0][0]
        assert "http://127.0.0.1:9999/mcp/" in args
        assert "project" in args
