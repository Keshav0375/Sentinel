#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-time bootstrap: the resource group Terraform can never own.
#
# ── Why this exists, and why it is ONE group rather than two ─────────────────
# Two things in this estate are structurally impossible for Terraform to create:
#
#   1. the storage account holding its own state — it needs somewhere to put
#      state before it can run at all;
#   2. the managed identities its pipeline authenticates AS — it cannot create
#      the credential it is already using.
#
# Both are the same kind of object: the trust root. Phases 1-4 put state in
# `sentinel-state-rg` and the CI identity in the APPLICATION resource group,
# which meant the application group could not be destroyed without taking CI
# down with it. Under the dynamic model every application group is created and
# destroyed on demand, so that arrangement is untenable.
#
# Hence `rg-sentinel-bootstrap`: one group holding exactly what Terraform cannot
# own, never destroyed by any workflow. Naming it `-tfstate`, as the phase-5 spec
# first did, would have been a lie about half its contents.
#
# Idempotent by design — safe to re-run as a "is my bootstrap healthy?" check.
#
# Requires: az, logged in as an Owner of the subscription below.
# Next:     scripts/bootstrap-identities.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Git Bash rewrites any argument that looks like a Unix path into a Windows path
# before the process sees it. An Azure resource ID starts with `/subscriptions/`,
# so `--scope /subscriptions/...` arrives as `C:/Program Files/Git/subscriptions/...`
# and the API answers `MissingSubscription` — a message that says nothing about
# path conversion and sends you looking at your az context instead.
#
# This cost three failed imports in phase 1 and one failed bootstrap here. Inert
# on Linux, where the variable is simply unread, so it is safe to set
# unconditionally rather than guarding on the platform.
export MSYS_NO_PATHCONV=1

EXPECTED_SUB="174e25ca-ab82-4671-a913-9c2f66e5924d"
LOCATION="${LOCATION:-canadacentral}"
BOOTSTRAP_RG="${BOOTSTRAP_RG:-rg-sentinel-bootstrap}"
CONTAINER="tfstate"

# Derived from the SUBSCRIPTION, not from a deployment — this account exists
# before any deployment does, so the deployment-derived `uid` that modules/naming
# uses is not available here. Same sha1-prefix idea, different input.
uid6="$(printf '%s' "${EXPECTED_SUB}" | sha1sum | cut -c1-6)"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-stsentineltf${uid6}}"

# ── Context assertion ─────────────────────────────────────────────────────────
# `az` keeps ONE context in ~/.azure, shared by every terminal. Sentinel spans
# two tenants, so any `az login --tenant <identity>` silently repoints every
# other session — this happened four times during this build. A script that
# creates the trust root must not run in the wrong subscription.
current_sub="$(az account show --query id -o tsv | tr -d '\r')"
if [ "${current_sub}" != "${EXPECTED_SUB}" ]; then
  echo "REFUSING: az context is subscription ${current_sub}, expected ${EXPECTED_SUB}" >&2
  echo "  az account set --subscription ${EXPECTED_SUB}" >&2
  exit 1
fi

echo "==> subscription  ${current_sub}"
echo "==> bootstrap RG  ${BOOTSTRAP_RG}"
echo "==> storage       ${STORAGE_ACCOUNT}"

# ── Resource providers ────────────────────────────────────────────────────────
# A fresh subscription has these unregistered, and the resulting failure reads
# like a Terraform bug rather than a subscription that has never been used.
# Registration is idempotent and asynchronous; the first apply may still wait.
for ns in Microsoft.Compute Microsoft.ContainerService Microsoft.ContainerRegistry \
          Microsoft.DBforPostgreSQL Microsoft.KeyVault Microsoft.EventGrid \
          Microsoft.Web Microsoft.Storage Microsoft.ManagedIdentity \
          Microsoft.OperationalInsights Microsoft.Authorization; do
  state="$(az provider show --namespace "${ns}" --query registrationState -o tsv 2>/dev/null | tr -d '\r' || echo NotFound)"
  if [ "${state}" != "Registered" ]; then
    echo "==> registering ${ns} (was ${state})"
    az provider register --namespace "${ns}" --only-show-errors >/dev/null
  fi
done

# ── The group ─────────────────────────────────────────────────────────────────
if ! az group show --name "${BOOTSTRAP_RG}" >/dev/null 2>&1; then
  echo "==> creating ${BOOTSTRAP_RG}"
  az group create --name "${BOOTSTRAP_RG}" --location "${LOCATION}" -o none
else
  echo "==> ${BOOTSTRAP_RG} exists"
fi

# ── Storage account ───────────────────────────────────────────────────────────
# Globally unique across every Azure tenant. A taken name fails with a message
# that does not say "taken", so check first and fail legibly. `sentineltfstate`
# was already lost this way in phase 1.
if ! az storage account show --name "${STORAGE_ACCOUNT}" --resource-group "${BOOTSTRAP_RG}" >/dev/null 2>&1; then
  available="$(az storage account check-name --name "${STORAGE_ACCOUNT}" --query nameAvailable -o tsv | tr -d '\r')"
  if [ "${available}" != "true" ]; then
    echo "REFUSING: storage account name ${STORAGE_ACCOUNT} is not available globally." >&2
    echo "  Override with STORAGE_ACCOUNT=<name>, and change it in backend.tf and" >&2
    echo "  docs/BOOTSTRAP.md in the same commit — a backend block cannot interpolate" >&2
    echo "  variables, so the name is necessarily written in more than one place." >&2
    exit 1
  fi
  echo "==> creating ${STORAGE_ACCOUNT}"
  az storage account create \
    --name "${STORAGE_ACCOUNT}" \
    --resource-group "${BOOTSTRAP_RG}" \
    --location "${LOCATION}" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --min-tls-version TLS1_2 \
    --allow-blob-public-access false \
    -o none
else
  echo "==> ${STORAGE_ACCOUNT} exists"
fi

# ── Operator data-plane access, BEFORE the container ──────────────────────────
# CONTROL PLANE IS NOT DATA PLANE, and this is the most confusing failure in the
# whole bootstrap. Owner of the subscription does NOT let you read a blob.
# Without this, `terraform init` returns a 403 that reads like broken backend
# configuration rather than a missing role.
#
# Granted before the container is created because the container creation below
# uses `--auth-mode login`, which is itself a data-plane call.
operator_id="$(az ad signed-in-user show --query id -o tsv | tr -d '\r')"
sa_id="$(az storage account show --name "${STORAGE_ACCOUNT}" --resource-group "${BOOTSTRAP_RG}" --query id -o tsv | tr -d '\r')"

if ! az role assignment list --assignee "${operator_id}" --scope "${sa_id}" \
      --role "Storage Blob Data Contributor" --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
  echo "==> granting Storage Blob Data Contributor to the operator"
  az role assignment create \
    --assignee-object-id "${operator_id}" \
    --assignee-principal-type User \
    --role "Storage Blob Data Contributor" \
    --scope "${sa_id}" \
    -o none
  # Entra role assignments are eventually consistent. The container create below
  # can 403 on a grant made moments earlier.
  echo "==> waiting 30s for the role assignment to replicate"
  sleep 30
else
  echo "==> operator already has Storage Blob Data Contributor"
fi

# ── Blob container ────────────────────────────────────────────────────────────
if ! az storage container show --name "${CONTAINER}" --account-name "${STORAGE_ACCOUNT}" --auth-mode login >/dev/null 2>&1; then
  echo "==> creating container ${CONTAINER}"
  az storage container create \
    --name "${CONTAINER}" \
    --account-name "${STORAGE_ACCOUNT}" \
    --auth-mode login \
    -o none
else
  echo "==> container ${CONTAINER} exists"
fi

cat <<SUMMARY

==> Bootstrap complete.

    resource group   ${BOOTSTRAP_RG}
    storage account  ${STORAGE_ACCOUNT}
    container        ${CONTAINER}

    backend.tf must match these exactly.

    Next: scripts/bootstrap-identities.sh
SUMMARY
