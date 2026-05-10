# ── Stage 1: builder ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Create a venv so the runtime stage gets a clean, self-contained install.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir --upgrade pip

# Copy the build manifest + source so hatchling can produce a wheel.
COPY pyproject.toml .
COPY src/ src/

RUN pip install --no-cache-dir .

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Copy the fully-installed venv from builder — no build tools in the image.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Copy application source (needed by the installed editable-style package path).
COPY src/ src/

# SQLite DB lives here; mount this directory as a volume for persistence.
RUN mkdir -p data

EXPOSE 8000

CMD ["uvicorn", "sentinel.main:app", "--host", "0.0.0.0", "--port", "8000"]
