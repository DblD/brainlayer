"""Regression test for the `brainlayer digest` CLI.

Would have caught: 2026-02-25 PR #32 introduced `embed_fn=model.embed` in
`cli/__init__.py`, but `EmbeddingModel` has no `.embed` — the correct method is
`.embed_query`. The bug only surfaces at CLI invocation time, not at import,
and the MCP `brain_digest` handler takes a different path that bypasses it,
which is why no existing test catches it.

This test invokes the CLI via subprocess against a throwaway DB and asserts
the digest completes successfully.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


@pytest.mark.skipif(
    shutil.which("brainlayer") is None,
    reason="brainlayer CLI not on PATH (package must be installed: `pip install -e .`)",
)
def test_digest_cli_completes_successfully(tmp_path):
    """`brainlayer digest "<text>"` should exit 0 and emit a digest id."""
    db_path = tmp_path / "regression.db"

    env = os.environ.copy()
    env["BRAINLAYER_DB"] = str(db_path)

    result = subprocess.run(
        ["brainlayer", "digest", "CLI regression smoke text."],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, (
        f"digest CLI exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "Digest complete!" in result.stdout, result.stdout
    assert "digest-" in result.stdout, result.stdout
