"""Remote cross-encoder reranking via an OpenAI-compatible ``/v1/rerank`` endpoint.

Opt-in second-stage ranking for brain search. When ``BRAINLAYER_RERANK_URL`` is set,
the search path over-fetches vector hits and reorders them with a cross-encoder
reranker (``bge-reranker-v2-m3``) served by llama.cpp/llama-swap on the fleet GPU box.

This is never required: :func:`apply_rerank_to_results` falls back to the original
vector order whenever the endpoint is unreachable, so a down GPU box can only make
search *no better*, never make it fail.
"""

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

RERANK_ENV = "BRAINLAYER_RERANK_URL"
DEFAULT_RERANK_MODEL = "bge-reranker-v2-m3"

# Over-fetch this many vector hits before reranking down to the requested count.
DEFAULT_OVERFETCH = 50

# Short connect timeout so a dead box fails fast and search falls back to vector
# order; generous read timeout tolerates a cold llama-swap model swap on first call.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 120.0


def get_rerank_url() -> Optional[str]:
    """Return the configured rerank endpoint base URL, or ``None`` when disabled."""
    url = os.environ.get(RERANK_ENV)
    return url.rstrip("/") if url else None


def rerank(
    query: str,
    candidates: List[str],
    url: str,
    model: str = DEFAULT_RERANK_MODEL,
    timeout: Optional[tuple] = None,
) -> List[Tuple[int, float]]:
    """Rerank ``candidates`` against ``query`` via a llama.cpp ``/v1/rerank`` endpoint.

    Returns a list of ``(original_index, relevance_score)`` sorted best-first.

    Raises on connection/HTTP error. Callers that must never fail (the search path)
    should use :func:`apply_rerank_to_results`, which catches and falls back to the
    original order.
    """
    if not candidates:
        return []

    base = url.rstrip("/")
    resp = requests.post(
        f"{base}/v1/rerank",
        json={"model": model, "query": query, "documents": candidates},
        timeout=timeout or (_CONNECT_TIMEOUT, _READ_TIMEOUT),
    )
    resp.raise_for_status()
    payload = resp.json()

    ranked: List[Tuple[int, float]] = [
        (int(item["index"]), float(item["relevance_score"])) for item in payload.get("results", [])
    ]
    # llama.cpp returns results in document order, not score order — sort best-first.
    ranked.sort(key=lambda pair: pair[1], reverse=True)
    return ranked


def apply_rerank_to_results(
    query: str,
    results: Dict[str, Any],
    num_results: int,
    url: str,
    model: str = DEFAULT_RERANK_MODEL,
) -> Dict[str, Any]:
    """Reorder a ``hybrid_search`` results dict in place by cross-encoder rerank.

    ``results`` matches the shape returned by ``VectorStore.hybrid_search``::

        {"ids": [[...]], "documents": [[...]], "metadatas": [[...]], "distances": [[...]]}

    On success the four parallel columns are reordered best-first, ``rerank_score``
    is injected into each metadata dict, and every column is truncated to
    ``num_results``. On any failure (endpoint down, malformed response) the results
    are returned unchanged so callers keep the original vector order and never fail.
    """
    columns = ("ids", "documents", "metadatas", "distances")

    docs_col = results.get("documents")
    documents = docs_col[0] if docs_col and docs_col[0] else []
    if not documents:
        return results

    try:
        ranked = rerank(query, list(documents), url=url, model=model)
    except Exception as e:  # noqa: BLE001 — never let a rerank failure break search
        logger.warning("Rerank failed, keeping vector order: %s", e)
        return results

    if not ranked:
        return results

    # Build the new ordering from valid indices, defensively appending any indices
    # the endpoint omitted so we never silently drop candidates.
    order = [idx for idx, _ in ranked if 0 <= idx < len(documents)]
    seen = set(order)
    order += [i for i in range(len(documents)) if i not in seen]

    scores = {idx: score for idx, score in ranked}

    for col in columns:
        column = results.get(col)
        if column and column[0] is not None:
            column[0] = [column[0][i] for i in order]

    metas = results.get("metadatas")
    if metas and metas[0]:
        for pos, orig_idx in enumerate(order):
            meta = metas[0][pos]
            if isinstance(meta, dict) and orig_idx in scores:
                meta["rerank_score"] = round(scores[orig_idx], 4)

    for col in columns:
        column = results.get(col)
        if column and column[0] is not None:
            column[0] = column[0][:num_results]

    return results
