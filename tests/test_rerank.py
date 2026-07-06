"""Unit + live tests for the remote rerank stage.

The HTTP layer is mocked for the unit tests. A live suite gated behind
BRAINLAYER_LIVE_TESTS exercises the real fleet GPU rerank endpoint.
"""

import os

import pytest
import requests

from brainlayer import rerank as rerank_mod
from brainlayer.rerank import apply_rerank_to_results, get_rerank_url, rerank


class _FakeResponse:
    def __init__(self, payload, status_ok=True):
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self):
        if not self._status_ok:
            raise requests.HTTPError("boom")

    def json(self):
        return self._payload


def _rerank_payload(scores):
    """scores: dict {index: relevance_score} → llama.cpp /v1/rerank shape."""
    return {
        "model": "bge-reranker-v2-m3",
        "object": "list",
        "results": [{"index": i, "relevance_score": s} for i, s in scores.items()],
    }


# --- get_rerank_url ----------------------------------------------------------


def test_get_rerank_url_none_by_default(monkeypatch):
    monkeypatch.delenv(rerank_mod.RERANK_ENV, raising=False)
    assert get_rerank_url() is None


def test_get_rerank_url_trims_trailing_slash(monkeypatch):
    monkeypatch.setenv(rerank_mod.RERANK_ENV, "http://10.41.90.20:8080/")
    assert get_rerank_url() == "http://10.41.90.20:8080"


# --- rerank() core -----------------------------------------------------------


def test_rerank_orders_best_first(monkeypatch):
    # Endpoint returns results in document order, not score order.
    payload = _rerank_payload({0: 1.21, 1: -10.6})

    def fake_post(url, json, timeout):
        assert url == "http://fake:8080/v1/rerank"
        assert json["model"] == "bge-reranker-v2-m3"
        assert json["query"] == "what GPU is this"
        assert json["documents"] == ["the Tesla P100 is a Pascal GPU", "bananas are yellow"]
        return _FakeResponse(payload)

    monkeypatch.setattr(rerank_mod.requests, "post", fake_post)

    res = rerank(
        "what GPU is this",
        ["the Tesla P100 is a Pascal GPU", "bananas are yellow"],
        url="http://fake:8080",
    )
    order = [i for i, _ in res]
    assert order[0] == 0  # the P100 doc must rank first
    assert order == [0, 1]
    assert res[0][1] == pytest.approx(1.21)


def test_rerank_reorders_when_endpoint_unsorted(monkeypatch):
    # doc index 2 is most relevant, then 0, then 1.
    payload = _rerank_payload({0: 0.5, 1: -2.0, 2: 3.0})
    monkeypatch.setattr(rerank_mod.requests, "post", lambda url, json, timeout: _FakeResponse(payload))

    res = rerank("q", ["a", "b", "c"], url="http://fake:8080")
    assert [i for i, _ in res] == [2, 0, 1]


def test_rerank_empty_candidates_no_call(monkeypatch):
    def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("should not POST for empty candidates")

    monkeypatch.setattr(rerank_mod.requests, "post", fail)
    assert rerank("q", [], url="http://fake:8080") == []


def test_rerank_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        rerank_mod.requests,
        "post",
        lambda url, json, timeout: _FakeResponse({}, status_ok=False),
    )
    with pytest.raises(requests.HTTPError):
        rerank("q", ["a"], url="http://fake:8080")


# --- apply_rerank_to_results -------------------------------------------------


def _sample_results():
    return {
        "ids": [["c0", "c1", "c2"]],
        "documents": [["doc a", "doc b", "doc c"]],
        "metadatas": [[{"m": 0}, {"m": 1}, {"m": 2}]],
        "distances": [[0.1, 0.2, 0.3]],
    }


def test_apply_rerank_reorders_injects_score_and_truncates(monkeypatch):
    payload = _rerank_payload({0: 0.5, 1: -2.0, 2: 3.0})  # best order: 2, 0, 1
    monkeypatch.setattr(rerank_mod.requests, "post", lambda url, json, timeout: _FakeResponse(payload))

    results = apply_rerank_to_results("q", _sample_results(), num_results=2, url="http://fake:8080")

    # Reordered best-first and truncated to 2.
    assert results["ids"][0] == ["c2", "c0"]
    assert results["documents"][0] == ["doc c", "doc a"]
    assert results["distances"][0] == [0.3, 0.1]
    # rerank_score injected into surviving metadata, aligned to new order.
    assert results["metadatas"][0][0]["rerank_score"] == pytest.approx(3.0)
    assert results["metadatas"][0][1]["rerank_score"] == pytest.approx(0.5)
    assert results["metadatas"][0][0]["m"] == 2  # original metadata preserved


def test_apply_rerank_falls_back_to_vector_order_on_error(monkeypatch):
    def boom(url, json, timeout):
        raise requests.ConnectionError("rerank box down")

    monkeypatch.setattr(rerank_mod.requests, "post", boom)

    results = apply_rerank_to_results("q", _sample_results(), num_results=3, url="http://fake:8080")

    # Unchanged: original vector order preserved, no rerank_score, no failure.
    assert results["ids"][0] == ["c0", "c1", "c2"]
    assert "rerank_score" not in results["metadatas"][0][0]


def test_apply_rerank_empty_results_noop(monkeypatch):
    def fail(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("should not POST for empty results")

    monkeypatch.setattr(rerank_mod.requests, "post", fail)
    empty = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    out = apply_rerank_to_results("q", empty, num_results=5, url="http://fake:8080")
    assert out["documents"][0] == []


# --- live integration (opt-in) ----------------------------------------------

LIVE = os.environ.get("BRAINLAYER_LIVE_TESTS")
LIVE_URL = os.environ.get("BRAINLAYER_LIVE_RERANK_URL", "http://10.41.90.20:8080")


@pytest.mark.skipif(not LIVE, reason="set BRAINLAYER_LIVE_TESTS=1 to run live GPU endpoint tests")
def test_live_rerank_orders_gpu_doc_first():
    res = rerank(
        "what GPU is this",
        ["the Tesla P100 is a Pascal GPU", "bananas are yellow"],
        url=LIVE_URL,
    )
    order = [i for i, _ in res]
    assert order[0] == 0
    # The relevant doc must score far above the irrelevant one.
    scores = {i: s for i, s in res}
    assert scores[0] > scores[1] + 1.0
