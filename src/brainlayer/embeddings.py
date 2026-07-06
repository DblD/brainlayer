"""Fast embeddings using sentence-transformers with bge-large-en-v1.5.

By default embeddings are computed in-process with a local SentenceTransformer.
Setting ``BRAINLAYER_EMBED_URL`` opts into a remote OpenAI-compatible embedding
endpoint (llama.cpp/llama-swap serving bge-large-en-v1.5-f16 on the fleet GPU box).
The remote vectors are drop-in compatible with the local model (cosine 0.9999+,
1024 dims), so no migration or schema change is needed.

Failure handling: if the remote endpoint is unreachable the code transparently
falls back to the local model with a logged warning — a memory write must never
fail just because the GPU box is down. With the env var unset the behaviour is
byte-for-byte identical to the original local-only implementation.
"""

import logging
import os
from dataclasses import dataclass
from typing import Callable, List, Optional

import requests
import torch
from sentence_transformers import SentenceTransformer

from .pipeline.chunk import Chunk

logger = logging.getLogger(__name__)

# Use bge-large-en-v1.5 for high-quality embeddings (1024 dims, 63.5 MTEB score)
DEFAULT_MODEL = "BAAI/bge-large-en-v1.5"
EMBEDDING_DIM = 1024  # bge-large dimension
# bge-large-en-v1.5 supports 512 tokens (~2000+ characters).
# sentence-transformers handles token-level truncation natively — no char truncation needed.
MAX_QUERY_CHARS = 2000  # generous cap for query strings only (avoids degenerate inputs)
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# --- Remote backend (opt-in) -------------------------------------------------
# Set BRAINLAYER_EMBED_URL (e.g. http://10.41.90.20:8080) to route embeddings to
# an OpenAI-compatible /v1/embeddings endpoint instead of the in-process model.
REMOTE_EMBED_ENV = "BRAINLAYER_EMBED_URL"
# Model name advertised by the llama-swap endpoint (bge-large-en-v1.5-f16).
REMOTE_EMBED_MODEL = "bge-large-en"
# Short connect timeout so a dead box fails fast into the local fallback; longer
# read timeout tolerates a cold model / large batch.
_REMOTE_CONNECT_TIMEOUT = 5.0
_REMOTE_READ_TIMEOUT = 60.0
# Batch size for remote chunk embedding requests (the endpoint accepts list input).
REMOTE_BATCH_SIZE = 32


def get_remote_embed_url() -> Optional[str]:
    """Return the configured remote embedding base URL, or ``None`` when unset."""
    url = os.environ.get(REMOTE_EMBED_ENV)
    return url.rstrip("/") if url else None


def get_backend_info() -> dict:
    """Report the active embedding backend so the path is machine-checkable.

    Returns ``{"backend": "remote"|"local", "url": <str|None>, "model": ...}``.
    The backend is decided purely by the ``BRAINLAYER_EMBED_URL`` env var at call
    time, so this reflects what the next embed call will actually do.
    """
    url = get_remote_embed_url()
    if url:
        return {"backend": "remote", "url": url, "model": REMOTE_EMBED_MODEL}
    return {"backend": "local", "url": None, "model": DEFAULT_MODEL}


def _remote_embed(texts: List[str], url: str) -> List[List[float]]:
    """POST texts to the remote /v1/embeddings endpoint, returning vectors in order.

    Raises on connection/HTTP error so callers can fall back to the local model.
    """
    resp = requests.post(
        f"{url}/v1/embeddings",
        json={"model": REMOTE_EMBED_MODEL, "input": texts},
        timeout=(_REMOTE_CONNECT_TIMEOUT, _REMOTE_READ_TIMEOUT),
    )
    resp.raise_for_status()
    payload = resp.json()
    # Preserve request order: the OpenAI schema returns an "index" per item.
    data = sorted(payload["data"], key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in data]


@dataclass
class EmbeddedChunk:
    """A chunk with its embedding vector."""

    chunk: Chunk
    embedding: List[float]


class EmbeddingModel:
    """Sentence-transformers embedding model."""

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self.model_name = model_name
        self._model: Optional[SentenceTransformer] = None

    def _load_model(self) -> SentenceTransformer:
        """Load model on first use."""
        if self._model is None:
            logger.info(f"Loading embedding model: {self.model_name}")
            device = "mps" if torch.backends.mps.is_available() else "cpu"
            self._model = SentenceTransformer(self.model_name, device=device)
        return self._model

    def embed_chunks(
        self,
        chunks: List[Chunk],
        batch_size: int = 32,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[EmbeddedChunk]:
        """Generate embeddings for chunks."""
        if not chunks:
            return []

        # Remote backend (opt-in): batch chunk texts to the endpoint. Chunk
        # embeddings get NO query prefix — matching the local path exactly.
        remote_url = get_remote_embed_url()
        if remote_url:
            try:
                return self._embed_chunks_remote(chunks, remote_url, on_progress)
            except Exception as e:
                logger.warning("Remote chunk embedding failed (%s); falling back to local model", e)

        model = self._load_model()
        results = []
        total = len(chunks)

        # Pass full content — sentence-transformers tokenizes and truncates at the
        # model's actual token limit (512 tokens ≈ 2000+ chars), so content beyond
        # 512 chars is now included in the embedding instead of being discarded.
        texts = [chunk.content for chunk in chunks]

        # Generate embeddings in batches
        for i in range(0, len(texts), batch_size):
            batch_texts = texts[i : i + batch_size]
            batch_chunks = chunks[i : i + batch_size]

            try:
                embeddings = model.encode(batch_texts, convert_to_numpy=True, show_progress_bar=False)

                for chunk, embedding in zip(batch_chunks, embeddings):
                    results.append(EmbeddedChunk(chunk=chunk, embedding=embedding.tolist()))

                if on_progress:
                    on_progress(len(results), total)

            except Exception as e:
                logger.error(f"Failed to embed batch: {e}")
                continue

        return results

    def _embed_chunks_remote(
        self,
        chunks: List[Chunk],
        url: str,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> List[EmbeddedChunk]:
        """Embed chunks via the remote endpoint, batching list inputs.

        Raises on any request failure so :meth:`embed_chunks` can fall back to the
        local model for the whole set (never a partial/inconsistent result).
        """
        texts = [chunk.content for chunk in chunks]
        results: List[EmbeddedChunk] = []
        total = len(chunks)

        for i in range(0, len(texts), REMOTE_BATCH_SIZE):
            batch_texts = texts[i : i + REMOTE_BATCH_SIZE]
            batch_chunks = chunks[i : i + REMOTE_BATCH_SIZE]
            vectors = _remote_embed(batch_texts, url)
            for chunk, embedding in zip(batch_chunks, vectors):
                results.append(EmbeddedChunk(chunk=chunk, embedding=embedding))
            if on_progress:
                on_progress(len(results), total)

        return results

    def embed_query(self, query: str) -> List[float]:
        """Generate embedding for search query with BGE prefix."""
        # Cap degenerate query inputs; model handles token truncation internally
        if len(query) > MAX_QUERY_CHARS:
            query = query[:MAX_QUERY_CHARS]

        # BGE models need query prefix for optimal retrieval. The prefix is applied
        # client-side BEFORE encoding for both the remote and local paths, so remote
        # queries carry identical prefix semantics to local ones.
        prefixed_query = f"{BGE_QUERY_PREFIX}{query}"

        # Remote backend (opt-in) with transparent fallback to local on failure.
        remote_url = get_remote_embed_url()
        if remote_url:
            try:
                return _remote_embed([prefixed_query], remote_url)[0]
            except Exception as e:
                logger.warning("Remote query embedding failed (%s); falling back to local model", e)

        model = self._load_model()
        try:
            embedding = model.encode([prefixed_query], convert_to_numpy=True)[0]
            return embedding.tolist()
        except Exception as e:
            raise RuntimeError(f"Failed to embed query: {e}") from e


# Global model instance
_embedding_model: Optional[EmbeddingModel] = None


def get_embedding_model(model_name: str = DEFAULT_MODEL) -> EmbeddingModel:
    """Get global embedding model instance."""
    global _embedding_model
    if _embedding_model is None or _embedding_model.model_name != model_name:
        _embedding_model = EmbeddingModel(model_name)
    return _embedding_model


def embed_chunks(
    chunks: List[Chunk],
    model_name: str = DEFAULT_MODEL,
    batch_size: int = 32,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> List[EmbeddedChunk]:
    """Generate embeddings for chunks using global model."""
    model = get_embedding_model(model_name)
    return model.embed_chunks(chunks, batch_size, on_progress)


def embed_query(query: str, model_name: str = DEFAULT_MODEL) -> List[float]:
    """Generate embedding for search query using global model."""
    model = get_embedding_model(model_name)
    return model.embed_query(query)
