# Sentinel backend image (backend.md §7; decision 2026-10-24). The AKS node is arm64:
#   docker buildx build --platform linux/arm64 -t sentinel-backend:<tag> --load .

# ── Stage 1: builder: the poetry.lock main set, installed into /opt/venv ──────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Poetry only exports the lock and never reaches the runtime image. Pins match ci.yml.
RUN pip install "poetry==2.5.1" "poetry-plugin-export==1.10.1"

RUN python -m venv /opt/venv

WORKDIR /build
COPY pyproject.toml poetry.lock ./
COPY scripts/install_locked.py scripts/
# Main group only, hash-checked; torch from the CPU wheel index; then `pip check`.
RUN /opt/venv/bin/python scripts/install_locked.py --main-only

# Bake the embedding model into a fixed HF cache (same id as Settings.sentinel_embedding_model,
# loaded the same way as memory/embeddings.py) so the pod never reaches huggingface.co.
ENV HF_HOME=/opt/hf
RUN /opt/venv/bin/python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# The app runs from src/ on PYTHONPATH and is not pip-installed: the poetry package
# includes data/, which never enters the image.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/hf \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Non-root with a fixed numeric UID (the pod can enforce runAsNonRoot).
RUN groupadd --system --gid 10001 sentinel \
 && useradd --system --uid 10001 --gid sentinel --create-home --home-dir /home/sentinel \
    --shell /usr/sbin/nologin sentinel

COPY --from=builder /opt/venv /opt/venv
# Read-only for the non-root user: the model is only ever read.
COPY --from=builder /opt/hf /opt/hf

WORKDIR /app
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini .
# The app writes reports/ (trajectories, eval report) relative to the working directory.
RUN mkdir reports && chown 10001:10001 reports

USER 10001:10001
EXPOSE 8000

# 5 s for open requests (an SSE stream never ends by itself), then the lifespan teardown
# (worst case 37 s): 42 s fits the pod's 45 s terminationGracePeriodSeconds.
CMD ["uvicorn", "sentinel.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "5"]
