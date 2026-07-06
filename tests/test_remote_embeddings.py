"""Unit + live tests for the opt-in remote embedding backend.

The HTTP layer is mocked for the unit tests (no network). A small live suite,
gated behind BRAINLAYER_LIVE_TESTS, exercises the real fleet GPU endpoint.
"""

import os
import types

import numpy as np
import pytest
import requests

from brainlayer import embeddings
from brainlayer.embeddings import EmbeddingModel


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise requests.HTTPError("boom")

    def json(self):
        return self._payload


def _embedding_payload(n, dim=1024, shuffle=False):
    """Build an OpenAI-shaped embeddings response with n items."""
    order = list(range(n))
    if shuffle and n > 1:
        # Return items out of order to prove the client re-sorts by "index".
        order = order[::-1]
    return {
        "model": "bge-large-en",
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": [float(i)] * dim} for i in order],
    }


def _fake_chunks(texts):
    return [types.SimpleNamespace(content=t) for t in texts]


@pytest.fixture(autouse=True)
def _reset_global_model():
    """Ensure a clean global model between tests (routing reads env each call)."""
    embeddings._embedding_model = None
    yield
    embeddings._embedding_model = None


# --- get_backend_info --------------------------------------------------------


def test_backend_info_local_by_default(monkeypatch):
    monkeypatch.delenv(embeddings.REMOTE_EMBED_ENV, raising=False)
    info = embeddings.get_backend_info()
    assert info == {"backend": "local", "url": None, "model": embeddings.DEFAULT_MODEL}


def test_backend_info_remote_when_env_set(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://10.41.90.20:8080/")
    info = embeddings.get_backend_info()
    assert info["backend"] == "remote"
    assert info["url"] == "http://10.41.90.20:8080"  # trailing slash trimmed
    assert info["model"] == embeddings.REMOTE_EMBED_MODEL


# --- prefix semantics --------------------------------------------------------


def test_embed_query_remote_applies_bge_prefix(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return _FakeResponse(_embedding_payload(1))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    vec = embeddings.embed_query("what GPU is this")

    assert captured["url"] == "http://fake:8080/v1/embeddings"
    assert captured["json"]["model"] == embeddings.REMOTE_EMBED_MODEL
    # The BGE query prefix MUST be applied client-side before the POST.
    assert captured["json"]["input"] == [embeddings.BGE_QUERY_PREFIX + "what GPU is this"]
    # Short connect timeout so a dead box fails fast.
    assert captured["timeout"][0] == pytest.approx(5.0)
    assert len(vec) == 1024


def test_embed_chunks_remote_has_no_prefix(monkeypatch):
    """Chunk embedding must NOT prepend the query prefix (parity with local)."""
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")
    seen_inputs = []

    def fake_post(url, json, timeout):
        seen_inputs.append(json["input"])
        return _FakeResponse(_embedding_payload(len(json["input"])))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    embeddings.embed_chunks(_fake_chunks(["alpha", "beta"]))

    assert seen_inputs == [["alpha", "beta"]]
    for text in seen_inputs[0]:
        assert not text.startswith(embeddings.BGE_QUERY_PREFIX)


# --- request order preservation ---------------------------------------------


def test_remote_embed_preserves_request_order(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")

    def fake_post(url, json, timeout):
        # Endpoint returns items shuffled; client must re-sort by index.
        return _FakeResponse(_embedding_payload(len(json["input"]), shuffle=True))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    result = embeddings.embed_chunks(_fake_chunks(["a", "b", "c"]))
    # index 0 → [0.0]*, index 1 → [1.0]*, index 2 → [2.0]* after re-sort
    assert [ec.embedding[0] for ec in result] == [0.0, 1.0, 2.0]


# --- batching ----------------------------------------------------------------


def test_embed_chunks_remote_batches_by_32(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")
    batch_sizes = []

    def fake_post(url, json, timeout):
        batch_sizes.append(len(json["input"]))
        return _FakeResponse(_embedding_payload(len(json["input"])))

    monkeypatch.setattr(embeddings.requests, "post", fake_post)

    chunks = _fake_chunks([f"text {i}" for i in range(70)])
    result = embeddings.embed_chunks(chunks)

    # 70 texts → 32 + 32 + 6 across three POSTs.
    assert batch_sizes == [32, 32, 6]
    assert len(result) == 70


# --- fallback on error -------------------------------------------------------


def _fake_local_model(vec):
    model = types.SimpleNamespace()
    model.encode = lambda texts, convert_to_numpy=True, show_progress_bar=False: np.array([vec for _ in texts])
    return model


def test_embed_query_falls_back_to_local_on_connection_error(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")

    def boom(url, json, timeout):
        raise requests.ConnectionError("gpu box down")

    monkeypatch.setattr(embeddings.requests, "post", boom)

    captured = {}

    def fake_encode(texts, convert_to_numpy=True, show_progress_bar=False):
        captured["texts"] = list(texts)
        return np.array([[0.25] * 1024 for _ in texts])

    fake_model = types.SimpleNamespace(encode=fake_encode)
    monkeypatch.setattr(EmbeddingModel, "_load_model", lambda self: fake_model)

    vec = embeddings.embed_query("hello")

    # Fell back to local, still with the prefix applied.
    assert captured["texts"] == [embeddings.BGE_QUERY_PREFIX + "hello"]
    assert vec == [0.25] * 1024


def test_embed_chunks_falls_back_to_local_on_error(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, "http://fake:8080")

    def boom(url, json, timeout):
        raise requests.Timeout("connect timeout")

    monkeypatch.setattr(embeddings.requests, "post", boom)

    encoded = {}

    def fake_encode(texts, convert_to_numpy=True, show_progress_bar=False):
        encoded["texts"] = list(texts)
        return np.array([[0.75] * 1024 for _ in texts])

    fake_model = types.SimpleNamespace(encode=fake_encode)
    monkeypatch.setattr(EmbeddingModel, "_load_model", lambda self: fake_model)

    result = embeddings.embed_chunks(_fake_chunks(["one", "two"]))

    assert encoded["texts"] == ["one", "two"]
    assert len(result) == 2
    assert result[0].embedding == [0.75] * 1024


def test_local_path_untouched_when_env_unset(monkeypatch):
    """With the env var unset, embed_query must never touch the network."""
    monkeypatch.delenv(embeddings.REMOTE_EMBED_ENV, raising=False)

    def fail_post(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("remote path used with env unset")

    monkeypatch.setattr(embeddings.requests, "post", fail_post)

    captured = {}

    def fake_encode(texts, convert_to_numpy=True, show_progress_bar=False):
        captured["texts"] = list(texts)
        return np.array([[0.1] * 1024 for _ in texts])

    monkeypatch.setattr(EmbeddingModel, "_load_model", lambda self: types.SimpleNamespace(encode=fake_encode))

    vec = embeddings.embed_query("local only")
    assert captured["texts"] == [embeddings.BGE_QUERY_PREFIX + "local only"]
    assert len(vec) == 1024


# --- live integration (opt-in) ----------------------------------------------

LIVE = os.environ.get("BRAINLAYER_LIVE_TESTS")
LIVE_URL = os.environ.get("BRAINLAYER_LIVE_EMBED_URL", "http://10.41.90.20:8080")


@pytest.mark.skipif(not LIVE, reason="set BRAINLAYER_LIVE_TESTS=1 to run live GPU endpoint tests")
def test_live_remote_embed_query(monkeypatch):
    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, LIVE_URL)
    embeddings._embedding_model = None
    assert embeddings.get_backend_info()["backend"] == "remote"
    vec = embeddings.embed_query("the Tesla P100 serves fleet embeddings")
    assert len(vec) == 1024
    assert sum(x * x for x in vec) > 0


@pytest.mark.skipif(not LIVE, reason="set BRAINLAYER_LIVE_TESTS=1 to run live GPU endpoint tests")
def test_live_remote_matches_local_cosine(monkeypatch):
    """Remote vectors must be drop-in compatible with the local model (cosine ~1)."""
    text = "cross-encoder reranking improves retrieval precision"

    monkeypatch.setenv(embeddings.REMOTE_EMBED_ENV, LIVE_URL)
    embeddings._embedding_model = None
    remote_vec = np.array(embeddings.embed_query(text))

    monkeypatch.delenv(embeddings.REMOTE_EMBED_ENV, raising=False)
    embeddings._embedding_model = None
    local_vec = np.array(embeddings.embed_query(text))

    cos = float(remote_vec @ local_vec / (np.linalg.norm(remote_vec) * np.linalg.norm(local_vec)))
    assert cos > 0.999, f"remote/local cosine too low: {cos}"
