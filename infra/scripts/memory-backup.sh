#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Dump a deployment's agent memory to blob storage before it is destroyed.
#
#     bash scripts/memory-backup.sh --scope deployment --deployment sentinel --environment dev
#     bash scripts/memory-backup.sh --scope all
#
# Run by infra_deploy.yml before every deployment or all destroy, and safe to
# run by hand. Decisions 2026-10-25 A: a destroy must not silently take the
# agent's memory with it, so the workflow runs this FIRST and a failure here
# blocks the destroy (fail closed). force_destroy_without_backup is the only
# way past it.
#
# ── What is kept, and where ──────────────────────────────────────────────────
# One `pg_dump -Fc` per deployment database, uploaded to the `memory-backups`
# container of the Terraform state account as
#     <deployment>-<env>/<YYYYMMDDTHHMMSSZ>.dump
# That container is created by bootstrap-state.sh, outside Terraform, so it
# survives every destroy. The newest --keep dumps (default 10) per prefix are
# kept; a failed prune is a warning, never a failure, because the new dump is
# already safe.
#
# The DATA of public.cache_entries is excluded (its schema is not): cached
# Datadog and LLM responses never leave Postgres, and they are worthless after
# a recreate anyway.
#
# ── Who connects ─────────────────────────────────────────────────────────────
# In CI, gha-deploy: a non-admin database principal holding pg_read_all_data,
# granted by grant-db-access.sh (decision 2026-10-28, Q1 = a). Locally, the
# signed-in az user (the human admin who owns the migrated tables). --pg-user
# overrides either.
#
# ── Skips, not failures ──────────────────────────────────────────────────────
# A workspace that does not exist, or whose `database_name` output is empty,
# has nothing to back up: it is skipped and the script still exits 0. Anything
# that goes wrong while there IS a database exits 1 with ::error::.
#
# The token is read into PGPASSWORD (the environment, not argv) and masked in
# CI. Nothing here prints it, and there is no `set -x`.
#
# Requires: az (logged in), terraform (initialised against the remote state),
# pg_dump and pg_restore.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Git Bash rewrites arguments that look like Unix paths; see
# bootstrap-identities.sh. Inert on Linux and macOS.
export MSYS_NO_PATHCONV=1

CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

# Run from infra/, where `terraform output` finds the working directory.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

STATE_STORAGE_ACCOUNT="${STATE_STORAGE_ACCOUNT:-stsentineltfb7fa37}"
CONTAINER="memory-backups"

SCOPE="deployment"
DEPLOYMENT="sentinel"
ENVIRONMENT="dev"
KEEP=10
PG_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --scope)       SCOPE="$2"; shift 2 ;;
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --keep)        KEEP="$2"; shift 2 ;;
    --pg-user)     PG_USER="$2"; shift 2 ;;
    -h|--help)     sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

die() { echo "::error::memory-backup: $*"; exit 1; }

case "${SCOPE}" in
  deployment|all) ;;
  *) die "--scope must be deployment or all, got '${SCOPE}'" ;;
esac
case "${KEEP}" in
  ''|*[!0-9]*|0) die "--keep must be a positive integer, got '${KEEP}'" ;;
esac

for tool in az terraform pg_dump pg_restore; do
  command -v "${tool}" >/dev/null 2>&1 || die "${tool} is not installed"
done

if [ -z "${PG_USER}" ]; then
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    PG_USER="gha-deploy"
  else
    PG_USER="$(az account show --query user.name -o tsv 2>/dev/null | nocr || true)"
  fi
fi
[ -n "${PG_USER}" ] || die "could not resolve the Postgres user; pass --pg-user"

blob() { az storage "$@" --auth-mode login --account-name "${STATE_STORAGE_ACCOUNT}" --only-show-errors; }

# ── The container must already exist ──────────────────────────────────────────
# Created by bootstrap-state.sh, never here: a container this script made would
# look like a backup target that bootstrap knows nothing about.
if ! blob container show --name "${CONTAINER}" >/dev/null 2>&1; then
  die "container ${CONTAINER} not found in ${STATE_STORAGE_ACCOUNT}; run infra/scripts/bootstrap-state.sh"
fi

# ── Which workspaces ──────────────────────────────────────────────────────────
# `all` uses the same filter as lifecycle.sh list_deployment_workspaces.
if [ "${SCOPE}" = "deployment" ]; then
  workspaces="${DEPLOYMENT}-${ENVIRONMENT}"
else
  workspaces="$(terraform workspace list | sed 's/^[* ]*//' | grep -v '^$' | grep -vx 'default' | grep -vx 'platform' || true)"
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/memory-backup.XXXXXX")"
trap 'rm -rf "${tmp}"' EXIT

export PGSSLMODE=require
export PGCONNECT_TIMEOUT=30

backup_one() {
  local ws="$1" host db token dump ts name names count drop
  if ! terraform workspace select "${ws}" >/dev/null 2>&1; then
    echo "::notice::memory-backup: ${ws}: no such workspace; skipped"
    return 0
  fi
  host="$(TF_WORKSPACE="${ws}" terraform output -raw database_host 2>/dev/null | nocr || true)"
  db="$(TF_WORKSPACE="${ws}" terraform output -raw database_name 2>/dev/null | nocr || true)"
  if [ -z "${host}" ] || [ -z "${db}" ]; then
    echo "::notice::memory-backup: ${ws}: no database; skipped"
    return 0
  fi

  # --resource-type oss-rdbms is the audience https://ossrdbms-aad.database.windows.net.
  token="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv 2>/dev/null | nocr)" ||
    die "${ws}: could not get a Postgres access token"
  [ -n "${token}" ] || die "${ws}: could not get a Postgres access token"
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::add-mask::${token}"; fi
  export PGPASSWORD="${token}"

  dump="${tmp}/${ws}.dump"
  echo "==> ${ws}: dumping ${db} on ${host} as ${PG_USER}"
  pg_dump -Fc --exclude-table-data=public.cache_entries \
    -h "${host}" -p 5432 -U "${PG_USER}" -d "${db}" -f "${dump}" || die "${ws}: pg_dump failed"
  [ -s "${dump}" ] || die "${ws}: pg_dump wrote an empty file"
  pg_restore --list "${dump}" >/dev/null || die "${ws}: the dump is not a readable archive"

  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  name="${ws}/${ts}.dump"
  blob blob upload -c "${CONTAINER}" -n "${name}" -f "${dump}" --overwrite false >/dev/null ||
    die "${ws}: upload of ${name} failed"
  blob blob show -c "${CONTAINER}" -n "${name}" >/dev/null || die "${ws}: ${name} is not readable after upload"

  # ── Keep the newest KEEP ────────────────────────────────────────────────────
  # The timestamp sorts lexically, so the oldest names come first.
  if names="$(blob blob list -c "${CONTAINER}" --prefix "${ws}/" --query "[].name" -o tsv | nocr | sort)"; then
    count="$(printf '%s\n' "${names}" | grep -c . || true)"
    if [ "${count}" -gt "${KEEP}" ]; then
      while IFS= read -r drop; do
        [ -n "${drop}" ] || continue
        blob blob delete -c "${CONTAINER}" -n "${drop}" >/dev/null ||
          echo "::warning::memory-backup: ${ws}: could not prune ${drop}"
      done <<< "$(printf '%s\n' "${names}" | head -n "$((count - KEEP))")"
    fi
  else
    echo "::warning::memory-backup: ${ws}: could not list old dumps; nothing pruned"
  fi

  unset PGPASSWORD
  echo "BACKED_UP ${ws} ${name}"
}

while IFS= read -r ws; do
  [ -n "${ws}" ] || continue
  backup_one "${ws}"
done <<< "${workspaces}"
