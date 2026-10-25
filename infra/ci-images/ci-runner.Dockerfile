# ─────────────────────────────────────────────────────────────────────────────
# Sentinel CI runner image  (architecture/infra.md §6.1)
#
# Consumed by workflows in the OTHER two repos as a job `container:` (§6.4):
#
#     container:
#       image: ${{ secrets.ACR_LOGIN_SERVER }}/ci-runner:latest
#       credentials: { username: …ACR_USERNAME, password: …ACR_PASSWORD }
#
# The point is determinism: `pip install` on every job is a network dependency
# and a version-drift source. Baking the toolchain means a backend CI run is the
# same run in March and in September unless someone edits this file.
#
# ── PLATFORM: linux/amd64, deliberately ──────────────────────────────────────
# AKS is ARM64 (`B2pls_v2`), which forces the backend APPLICATION image to
# `linux/arm64`. That constraint does NOT apply here and the distinction matters:
# this image never runs as a pod. It runs as a container ON the GitHub-hosted
# runner, and `ubuntu-latest` is x86-64. An arm64 build of this file would be
# pulled successfully and then die with `exec format error` before the first
# step — a failure that reads like a broken workflow, not a wrong architecture.
#
# `docker build` on ubuntu-latest already produces amd64, so ci_runners.yml gets
# it right by default. The hazard is a local build on an Apple Silicon laptop,
# which silently produces arm64. build-push.sh therefore passes
# `--platform linux/amd64` explicitly rather than trusting the host.
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

# bash with pipefail, so the `curl | bash` below cannot succeed on a failed
# download. The default /bin/sh here is dash, which has no pipefail: a 404 page
# would be piped to bash, do nothing, exit 0, and bake an image with no Azure
# CLI that fails at azure/login in whichever workflow used it next.
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Debian-based on purpose: the Azure CLI install below is a .deb script, and
# libpq (for asyncpg/psycopg builds) is an apt package. Alpine would need musl
# wheels for asyncpg and a completely different CLI install path.

# PYRIGHT_PYTHON_CACHE_DIR: pyright-python caches its Node runtime under $HOME.
# Actions sets HOME=/github/home for container jobs, so a warm-up landing in
# /root/.cache is invisible at run time and every job re-downloads Node. An
# absolute path makes the warm-up at the bottom of this file actually pay off.
#
# NOTE: this comment sits ABOVE the ENV, and each ENV is its own line with no
# backslash continuation at all. Docker does not allow a
# comment within a line continuation — it parses the `#` as an env key and fails
# with `Syntax error - can't find = in "#"`. That mistake shipped and broke the
# first CI build of this image, because nothing lints Dockerfiles: the quality
# gate has no hadolint check and the Docker daemon was not running locally.
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV DEBIAN_FRONTEND=noninteractive
ENV PYRIGHT_PYTHON_CACHE_DIR=/opt/pyright-cache

# ── System packages (§6.1) ───────────────────────────────────────────────────
#   curl        — required BY the Azure CLI installer, and used by health checks
#   ca-certificates — the installer fetches over https; slim images ship a
#                     minimal store and the CLI install fails without this
#   gcc, libpq-dev  — build deps for asyncpg/psycopg when no wheel matches
#   git         — actions/checkout runs on the host, but tooling inside the
#                 container still shells out to git (e.g. `git diff` in gates)
#   docker.io   — image builds from inside CI jobs
#
# One RUN layer with the apt lists deleted in the same layer: splitting them
# leaves the ~40 MB index baked into an earlier layer where `rm` cannot reach it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        gcc \
        libpq-dev \
        git \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

# ── Azure CLI (§6.1) ─────────────────────────────────────────────────────────
# The vendor .deb script rather than `pip install azure-cli`: the pip
# distribution routinely conflicts with other packages' dependency pins, and
# `azure/login@v2` expects the real CLI on PATH.
RUN curl -fsSL https://aka.ms/InstallAzureCLIDeb | bash \
    && rm -rf /var/lib/apt/lists/*

# ── Python toolchain (§6.1) ──────────────────────────────────────────────────
# Unpinned, matching the architecture. That is a real trade-off worth naming:
# unpinned means a rebuild can pick up a new ruff that fails code which passed
# yesterday. The counterweight is that this image is rebuilt only when
# ci-images/** changes, so drift happens on a deliberate push rather than on
# every CI run — which is the property that actually matters. Pin here if a
# tool upgrade ever breaks a build.
RUN pip install --no-cache-dir \
        ruff \
        pyright \
        pytest \
        pytest-asyncio \
        asyncpg \
        pgvector \
        alembic

# pyright is a Node program behind a Python shim: it downloads a Node runtime on
# FIRST invocation, not at install. Left cold, every CI job that type-checks pays
# that download. Running it once here bakes the runtime into this layer.
RUN pyright --version

WORKDIR /workspace
