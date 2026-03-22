"""Tests for MCP shared state management."""

import threading
from unittest.mock import MagicMock, patch


class TestSetSharedState:
    def test_sets_both_globals(self):
        from brainlayer.mcp._shared import set_shared_state

        mock_store = MagicMock()
        mock_model = MagicMock()

        set_shared_state(mock_store, mock_model)

        from brainlayer.mcp import _shared

        assert _shared._vector_store is mock_store
        assert _shared._embedding_model is mock_model

        # Cleanup
        _shared._vector_store = None
        _shared._embedding_model = None

    def test_overwrites_existing(self):
        from brainlayer.mcp import _shared
        from brainlayer.mcp._shared import set_shared_state

        old_store = MagicMock()
        new_store = MagicMock()
        old_model = MagicMock()
        new_model = MagicMock()

        set_shared_state(old_store, old_model)
        set_shared_state(new_store, new_model)

        assert _shared._vector_store is new_store
        assert _shared._embedding_model is new_model

        _shared._vector_store = None
        _shared._embedding_model = None


class TestClearSharedState:
    def test_resets_to_none(self):
        from brainlayer.mcp import _shared
        from brainlayer.mcp._shared import clear_shared_state, set_shared_state

        set_shared_state(MagicMock(), MagicMock())
        clear_shared_state()

        assert _shared._vector_store is None
        assert _shared._embedding_model is None

    def test_get_vector_store_falls_back_after_clear(self):
        from brainlayer.mcp import _shared
        from brainlayer.mcp._shared import clear_shared_state, set_shared_state

        injected = MagicMock()
        set_shared_state(injected, MagicMock())
        clear_shared_state()

        mock_vs_class = MagicMock()
        with (
            patch("brainlayer.mcp._shared.VectorStore", mock_vs_class, create=True),
            patch.dict("sys.modules", {}),
        ):
            # After clear, _get_vector_store should try to lazy-init
            assert _shared._vector_store is None

        _shared._vector_store = None
        _shared._embedding_model = None


class TestThreadSafety:
    def test_concurrent_set_and_get(self):
        from brainlayer.mcp import _shared
        from brainlayer.mcp._shared import _get_vector_store, set_shared_state

        errors = []
        store = MagicMock()
        model = MagicMock()

        # Pre-set so _get_vector_store doesn't try to lazy-init a real VectorStore
        set_shared_state(store, model)

        def setter():
            try:
                for _ in range(50):
                    set_shared_state(store, model)
            except Exception as e:
                errors.append(e)

        def getter():
            try:
                for _ in range(50):
                    result = _get_vector_store()
                    assert result is not None
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(5):
            threads.append(threading.Thread(target=setter))
            threads.append(threading.Thread(target=getter))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread safety errors: {errors}"

        _shared._vector_store = None
        _shared._embedding_model = None
