#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Build and push the Sentinel CI runner image.  (architecture/infra.md §6.2)
#
# ── Why this script exists at all ────────────────────────────────────────────
# It is a bootstrap seam, the same shape as scripts/bootstrap-state.sh.
# ci_runners.yml automates every subsequent rebuild — but that workflow pulls
# ACR credentials that only exist after `terraform apply`, and the FIRST apply
# wants a runner image that does not exist yet. Someone has to break the loop by
# hand exactly once. This is that once.
#
# After the first push, do not run this: edit ci-images/** and let ci_runners.yml
# do it, so the image in ACR always corresponds to a commit on main.
#
# Idempotent — re-running just overwrites :latest with an identical build.
#
# Requires: az (logged in as an identity with AcrPush on the registry), docker.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Overridable so a fork or a second environment does not need a patch, but
# defaulted so the common case is a bare `./ci-images/build-push.sh`.
ACR_NAME="${ACR_NAME:-sentinelacr0375}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE="${SCRIPT_DIR}/ci-runner.Dockerfile"

# `az acr login` needs the short registry name; the image reference needs the
# FQDN. Deriving the second from the registry rather than hardcoding
# "${ACR_NAME}.azurecr.io" keeps this correct in sovereign clouds, where the
# suffix is not azurecr.io.
echo "==> Resolving login server for ${ACR_NAME}"
LOGIN_SERVER="$(az acr show --name "${ACR_NAME}" --query loginServer -o tsv | tr -d '\r')"
IMAGE="${LOGIN_SERVER}/ci-runner:${IMAGE_TAG}"

echo "==> Logging in to ${ACR_NAME}"
az acr login --name "${ACR_NAME}"

# --platform is load-bearing, not decoration. This image runs as a job
# `container:` on ubuntu-latest (x86-64); it is NOT an AKS pod, so the ARM64
# constraint that governs the backend image does not apply. A build on an Apple
# Silicon machine defaults to arm64 and produces an image that pushes fine and
# then dies with `exec format error` on the first CI job that uses it.
echo "==> Building ${IMAGE} (linux/amd64)"
docker build \
    --platform linux/amd64 \
    --file "${DOCKERFILE}" \
    --tag "${IMAGE}" \
    "${SCRIPT_DIR}"

echo "==> Pushing ${IMAGE}"
docker push "${IMAGE}"

echo "==> Done. Verify with:"
echo "    az acr repository show-tags --name ${ACR_NAME} --repository ci-runner -o tsv"
