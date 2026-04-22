# BrainLayer daemon — containerized
# Exposes /embed (reused by scott-gpt + others) and full /search + MCP over HTTP.
# Model (BAAI/bge-large-en-v1.5, ~1.3GB) is baked into the image for fast cold start.

ARG PYTHON_VERSION=3.12

# =============================================================================
# Stage 1: Install deps + prefetch model
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for native wheels (apsw, scikit-learn, spacy)
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      curl \
      git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install via pip (uv would be faster; pip keeps Dockerfile simple + auditable).
# Force CPU-only torch — cluster has no GPU; nvidia CUDA wheels (~3GB) are pure bloat.
# The PyTorch CPU index serves a ~170MB torch wheel instead of ~800MB+ cuda variant,
# and strips the entire nvidia-* wheel set pulled transitively by sentence-transformers.
RUN pip install --upgrade pip setuptools wheel && \
    pip install torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install .

# Prefetch bge-large model + spaCy model into image (reproducible cold start)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-large-en-v1.5')" && \
    python -m spacy download en_core_web_sm || true

# =============================================================================
# Stage 2: Runtime
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/root/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/root/.cache/torch/sentence_transformers

# Runtime libs only
RUN apt-get update && apt-get install -y --no-install-recommends \
      libstdc++6 \
      ca-certificates \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed Python packages + brainlayer source
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app
# Pre-downloaded model caches
COPY --from=builder /root/.cache /root/.cache

# Data dir — mount a PVC here for persistence
RUN mkdir -p /data/brainlayer
ENV BRAINLAYER_DATA_DIR=/data/brainlayer

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://localhost:8787/health || exit 1

CMD ["python", "-m", "brainlayer.daemon", "--http", "8787", "--host", "0.0.0.0"]
