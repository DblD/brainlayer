"""Tests for API key generation and resolution."""

import os
import sys
import threading
from unittest.mock import patch

import pytest


@pytest.fixture
def key_dir(tmp_path):
    """Provide a temporary API key directory."""
    key_dir = tmp_path / "config" / "brainlayer"
    key_path = key_dir / "api_key"
    with (
        patch("brainlayer.paths.API_KEY_DIR", key_dir),
        patch("brainlayer.paths.API_KEY_PATH", key_path),
    ):
        yield key_dir, key_path


class TestEnsureApiKey:
    def test_generates_key_on_first_call(self, key_dir):
        from brainlayer.paths import ensure_api_key

        key_dir, key_path = key_dir
        key = ensure_api_key()

        assert len(key) > 20
        assert key_path.exists()
        assert key_path.read_text() == key

    def test_reads_existing_key(self, key_dir):
        from brainlayer.paths import ensure_api_key

        key_dir, key_path = key_dir
        key1 = ensure_api_key()
        key2 = ensure_api_key()

        assert key1 == key2

    def test_env_var_overrides_file(self, key_dir):
        from brainlayer.paths import ensure_api_key

        with patch.dict(os.environ, {"BRAINLAYER_API_KEY": "env-test-key-123"}):
            key = ensure_api_key()

        assert key == "env-test-key-123"

    def test_env_var_stripped(self, key_dir):
        from brainlayer.paths import ensure_api_key

        with patch.dict(os.environ, {"BRAINLAYER_API_KEY": "  key-with-spaces  \n"}):
            key = ensure_api_key()

        assert key == "key-with-spaces"

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions only")
    def test_file_permissions_0600(self, key_dir):
        from brainlayer.paths import ensure_api_key

        key_dir, key_path = key_dir
        ensure_api_key()

        mode = os.stat(key_path).st_mode & 0o777
        assert mode == 0o600

    def test_empty_file_triggers_regeneration(self, key_dir):
        from brainlayer.paths import ensure_api_key

        key_dir, key_path = key_dir
        key_dir.mkdir(parents=True, exist_ok=True)
        key_path.write_text("")

        key = ensure_api_key()

        assert len(key) > 20
        assert key_path.read_text() == key

    def test_concurrent_creation_same_key(self, key_dir):
        from brainlayer.paths import ensure_api_key

        results = []

        def worker():
            results.append(ensure_api_key())

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(set(results)) == 1, f"Expected all threads to get same key, got {set(results)}"
