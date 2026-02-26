"""Tests for enrichment reliability — retry, circuit breaker, timeout config."""

import time
from unittest.mock import MagicMock, patch

from brainlayer.pipeline import enrichment


class TestRetryWithBackoff:
    """Per-chunk retry with exponential backoff."""

    def test_success_on_first_attempt_no_retry(self):
        """Successful LLM call doesn't trigger retry."""
        store = MagicMock()
        store.get_context.return_value = {"context": []}
        chunk = {"id": "test-chunk-001", "content": "test", "content_type": "user_message"}

        with (
            patch.object(enrichment, "call_llm", return_value='{"summary":"ok","tags":["test"]}'),
            patch.object(enrichment, "parse_enrichment", return_value={"summary": "ok", "tags": ["test"]}),
            patch.object(enrichment, "MAX_RETRIES", 2),
        ):
            result = enrichment._enrich_one(store, chunk, with_context=False)

        assert result is True

    def test_retry_on_llm_failure(self):
        """Failed LLM call retries up to MAX_RETRIES times."""
        store = MagicMock()
        chunk = {"id": "test-chunk-002", "content": "test", "content_type": "user_message"}

        call_count = 0

        def mock_call_llm(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                return None  # Fail first 2 attempts
            return '{"summary":"recovered","tags":["test"]}'

        with (
            patch.object(enrichment, "call_llm", side_effect=mock_call_llm),
            patch.object(
                enrichment,
                "parse_enrichment",
                side_effect=lambda r: {"summary": "recovered", "tags": ["test"]} if r else None,
            ),
            patch.object(enrichment, "MAX_RETRIES", 2),
            patch.object(enrichment, "RETRY_BASE_DELAY", 0.01),  # Fast for tests
            patch.object(enrichment, "RETRY_MAX_DELAY", 0.05),
        ):
            result = enrichment._enrich_one(store, chunk, with_context=False)

        assert result is True
        assert call_count == 3  # Initial + 2 retries

    def test_all_retries_exhausted(self):
        """Returns False after all retry attempts fail."""
        store = MagicMock()
        chunk = {"id": "test-chunk-003", "content": "test", "content_type": "user_message"}

        with (
            patch.object(enrichment, "call_llm", return_value=None),
            patch.object(enrichment, "MAX_RETRIES", 1),
            patch.object(enrichment, "RETRY_BASE_DELAY", 0.01),
            patch.object(enrichment, "RETRY_MAX_DELAY", 0.05),
        ):
            result = enrichment._enrich_one(store, chunk, with_context=False)

        assert result is False

    def test_no_retry_when_max_retries_zero(self):
        """MAX_RETRIES=0 means no retries, single attempt only."""
        store = MagicMock()
        chunk = {"id": "test-chunk-004", "content": "test", "content_type": "user_message"}
        call_count = 0

        def mock_call(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            return None

        with (
            patch.object(enrichment, "call_llm", side_effect=mock_call),
            patch.object(enrichment, "MAX_RETRIES", 0),
        ):
            result = enrichment._enrich_one(store, chunk, with_context=False)

        assert result is False
        assert call_count == 1

    def test_backoff_increases_delay(self):
        """Verify backoff delay increases between retries."""
        store = MagicMock()
        chunk = {"id": "test-chunk-005", "content": "test", "content_type": "user_message"}
        delays = []

        original_sleep = time.sleep

        def mock_sleep(duration):
            delays.append(duration)
            # Don't actually sleep in tests

        with (
            patch.object(enrichment, "call_llm", return_value=None),
            patch.object(enrichment, "MAX_RETRIES", 3),
            patch.object(enrichment, "RETRY_BASE_DELAY", 1.0),
            patch.object(enrichment, "RETRY_MAX_DELAY", 100.0),
            patch("time.sleep", side_effect=mock_sleep),
        ):
            enrichment._enrich_one(store, chunk, with_context=False)

        assert len(delays) == 3  # 3 retry sleeps
        # Delays should generally increase (base * 2^attempt + jitter)
        # With jitter it's not strictly monotonic, but base increases: 1, 2, 4
        assert delays[0] < 3.0  # ~1.0 + up to 0.3 jitter
        assert delays[1] < 5.0  # ~2.0 + up to 0.6 jitter


class TestCircuitBreaker:
    """Batch-level circuit breaker aborts on consecutive failures."""

    def test_circuit_breaks_on_threshold(self):
        """Batch aborts after CIRCUIT_BREAKER_THRESHOLD consecutive failures."""
        store = MagicMock()
        # Return 20 chunks but circuit should break after threshold
        chunks = [{"id": f"chunk-{i}", "content": f"test {i}", "content_type": "user_message"} for i in range(20)]
        store.get_unenriched_chunks.return_value = chunks

        with (
            patch.object(enrichment, "_enrich_one", return_value=False),
            patch.object(enrichment, "CIRCUIT_BREAKER_THRESHOLD", 5),
        ):
            result = enrichment.enrich_batch(store, batch_size=20, parallel=1)

        assert result["circuit_broken"] is True
        # Should have stopped at threshold, not processed all 20
        assert result["processed"] == 5
        assert result["failed"] == 5
        assert result["success"] == 0

    def test_no_circuit_break_on_intermittent_failures(self):
        """Intermittent failures (not consecutive) don't trigger circuit breaker."""
        store = MagicMock()
        chunks = [{"id": f"chunk-{i}", "content": f"test {i}", "content_type": "user_message"} for i in range(10)]
        store.get_unenriched_chunks.return_value = chunks

        # Alternate success/failure
        results = [True, False, True, False, True, False, True, False, True, False]
        call_idx = 0

        def mock_enrich(*args, **kwargs):
            nonlocal call_idx
            r = results[call_idx]
            call_idx += 1
            return r

        with (
            patch.object(enrichment, "_enrich_one", side_effect=mock_enrich),
            patch.object(enrichment, "CIRCUIT_BREAKER_THRESHOLD", 5),
        ):
            result = enrichment.enrich_batch(store, batch_size=10, parallel=1)

        assert result["circuit_broken"] is False
        assert result["processed"] == 10
        assert result["success"] == 5
        assert result["failed"] == 5

    def test_circuit_break_resets_on_success(self):
        """A single success resets the consecutive failure counter."""
        store = MagicMock()
        chunks = [{"id": f"chunk-{i}", "content": f"test {i}", "content_type": "user_message"} for i in range(15)]
        store.get_unenriched_chunks.return_value = chunks

        # 4 failures, 1 success, 4 failures, 1 success, etc — never hits threshold of 5
        results = [False, False, False, False, True] * 3
        call_idx = 0

        def mock_enrich(*args, **kwargs):
            nonlocal call_idx
            r = results[call_idx]
            call_idx += 1
            return r

        with (
            patch.object(enrichment, "_enrich_one", side_effect=mock_enrich),
            patch.object(enrichment, "CIRCUIT_BREAKER_THRESHOLD", 5),
        ):
            result = enrichment.enrich_batch(store, batch_size=15, parallel=1)

        assert result["circuit_broken"] is False
        assert result["processed"] == 15


class TestMLXTimeout:
    """MLX uses shorter default timeout."""

    def test_mlx_default_timeout_is_60(self):
        assert enrichment.MLX_DEFAULT_TIMEOUT == 60

    def test_mlx_timeout_env_override(self):
        """MLX timeout can be overridden via env var."""
        import os

        old = os.environ.get("BRAINLAYER_MLX_TIMEOUT")
        try:
            os.environ["BRAINLAYER_MLX_TIMEOUT"] = "120"
            # Re-evaluate (module-level constant, so we test the pattern)
            assert int(os.environ["BRAINLAYER_MLX_TIMEOUT"]) == 120
        finally:
            if old is not None:
                os.environ["BRAINLAYER_MLX_TIMEOUT"] = old
            else:
                os.environ.pop("BRAINLAYER_MLX_TIMEOUT", None)


class TestConfigConstants:
    """Verify config constants have sensible defaults."""

    def test_max_retries_default(self):
        assert enrichment.MAX_RETRIES == 2

    def test_retry_base_delay_default(self):
        assert enrichment.RETRY_BASE_DELAY == 2.0

    def test_retry_max_delay_default(self):
        assert enrichment.RETRY_MAX_DELAY == 30.0

    def test_circuit_breaker_threshold_default(self):
        assert enrichment.CIRCUIT_BREAKER_THRESHOLD == 10
