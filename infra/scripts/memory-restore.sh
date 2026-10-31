#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Restore a deployment's agent memory from its latest pre-destroy dump.
#
#     bash scripts/memory-restore.sh --deployment sentinel --environment dev
#     bash scripts/memory-restore.sh --deployment sentinel --environment dev --pg-user you@school.ca
#
# Run by infra_deploy.yml after every deployment or all apply, and by hand after
# migrating a fresh database. The counterpart of memory-backup.sh.
#
# ── DATA only, into a schema that already exists ─────────────────────────────
# Decision 2026-10-25 (amended 2026-10-28): owners never change. The tables
# belong to whoever ran `alembic upgrade head` (the human admin), and
# gha-deploy is a non-admin principal scoped to this one database (SELECT and
# INSERT on its tables, UPDATE of incidents.status / .resolution) — it can
# write rows, it cannot create or re-own tables. So the restore is DATA only:
# `pg_restore --data-only` renders the rows as SQL and `psql
# --single-transaction` applies them: all of it or none of it.
#
# Data NOT restored:
#   alembic_version, services   the migration seeds both; restoring them would
#                               collide with the rows already there
#   cache_entries               cached tool / LLM replies stay in the database
#                               they were made in (R14); never dumped anyway
#   incident_checkpoints        the in-flight runs of the destroyed estate
#
# ── Restored placeholders are closed, in the same transaction ────────────────
# An incident that was in flight when the estate was destroyed comes back as
# its `in_progress` placeholder row, with no checkpoint. The same transaction
# closes every such row as `escalated` ("restored from backup"), so the
# backend's startup sweep never resumes a stale incident against a new estate.
#
# ── When it does nothing (exit 0, with a notice) ─────────────────────────────
#   no workspace / no database     nothing to restore into
#   no dump in memory-backups      nothing to restore from
#   public.incidents missing       a fresh database: migrations are human-run,
#                                  so the notice says what to run, in order
#   public.incidents has rows      the database is live; restoring would merge
#                                  two histories
#
# The token is read into PGPASSWORD and masked in CI; nothing prints it.
#
# Requires: az (logged in), terraform (initialised against the remote state),
# pg_restore and psql.
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

DEPLOYMENT="sentinel"
ENVIRONMENT="dev"
PG_USER=""

while [ $# -gt 0 ]; do
  case "$1" in
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --pg-user)     PG_USER="$2"; shift 2 ;;
    -h|--help)     sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

ws="${DEPLOYMENT}-${ENVIRONMENT}"
die() { echo "::error::memory-restore: ${ws}: $*"; exit 1; }

for tool in az terraform pg_restore psql; do
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

if ! blob container show --name "${CONTAINER}" >/dev/null 2>&1; then
  die "container ${CONTAINER} not found in ${STATE_STORAGE_ACCOUNT}; run infra/scripts/bootstrap-state.sh"
fi

# ── Where ─────────────────────────────────────────────────────────────────────
if ! terraform workspace select "${ws}" >/dev/null 2>&1; then
  echo "::notice::memory-restore: ${ws}: no such workspace; skipped"
  exit 0
fi
host="$(TF_WORKSPACE="${ws}" terraform output -raw database_host 2>/dev/null | nocr || true)"
db="$(TF_WORKSPACE="${ws}" terraform output -raw database_name 2>/dev/null | nocr || true)"
if [ -z "${host}" ] || [ -z "${db}" ]; then
  echo "::notice::memory-restore: ${ws}: no database; skipped"
  exit 0
fi

# ── From what: the newest dump (the timestamp sorts lexically) ────────────────
names="$(blob blob list -c "${CONTAINER}" --prefix "${ws}/" --query "[].name" -o tsv | nocr | sort)" ||
  die "could not list ${CONTAINER}/${ws}/"
latest="$(printf '%s\n' "${names}" | grep . | tail -n 1 || true)"
if [ -z "${latest}" ]; then
  echo "::notice::memory-restore: ${ws}: no dump in ${CONTAINER}; nothing to restore"
  exit 0
fi

# --resource-type oss-rdbms is the audience https://ossrdbms-aad.database.windows.net.
token="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv 2>/dev/null | nocr)" ||
  die "could not get a Postgres access token"
[ -n "${token}" ] || die "could not get a Postgres access token"
if [ "${GITHUB_ACTIONS:-}" = "true" ]; then echo "::add-mask::${token}"; fi
export PGPASSWORD="${token}"
export PGSSLMODE=require
export PGCONNECT_TIMEOUT=30

# -X ignores ~/.psqlrc, which could otherwise echo queries or change output.
q() { psql -X -tA -v ON_ERROR_STOP=1 -h "${host}" -p 5432 -U "${PG_USER}" -d "${db}" -c "$1" | nocr; }

# ── Into what: only an empty, migrated schema ─────────────────────────────────
migrated="$(q "SELECT to_regclass('public.incidents') IS NOT NULL")" || die "could not query ${db}"
if [ "${migrated}" != "t" ]; then
  echo "::notice::memory-restore: ${ws}: schema not migrated in ${db}: run \`poetry run alembic upgrade head\`," \
    "then \`bash infra/scripts/grant-db-access.sh --deployment ${DEPLOYMENT} --environment ${ENVIRONMENT}\`," \
    "then \`bash infra/scripts/memory-restore.sh --deployment ${DEPLOYMENT} --environment ${ENVIRONMENT}\`"
  exit 0
fi
rows="$(q "SELECT count(*) FROM public.incidents")" || die "could not count public.incidents"
if [ "${rows}" != "0" ]; then
  echo "::notice::memory-restore: ${ws}: incidents has ${rows} rows; not restoring"
  exit 0
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/memory-restore.XXXXXX")"
trap 'rm -rf "${tmp}"' EXIT
dump="${tmp}/latest.dump"
list="${tmp}/restore.list"

echo "==> ${ws}: restoring ${latest} into ${db} on ${host} as ${PG_USER}"
blob blob download -c "${CONTAINER}" -n "${latest}" -f "${dump}" >/dev/null || die "download of ${latest} failed"

# The data left out (see the header): seeded, cached, or the old estate's runs.
pg_restore --list "${dump}" > "${tmp}/full.list" || die "${latest} is not a readable archive"
grep -v -E '^[0-9]+; [0-9]+ [0-9]+ TABLE DATA public (alembic_version|services|cache_entries|incident_checkpoints) ' \
  "${tmp}/full.list" > "${list}" || true

# Render the kept data as SQL, then apply it AND close the restored
# placeholders in ONE transaction (-1 wraps every -f / -c in BEGIN … COMMIT;
# ON_ERROR_STOP aborts it on the first error).
sql="${tmp}/restore.sql"
pg_restore --data-only -L "${list}" -f "${sql}" "${dump}" || die "pg_restore could not render ${latest}"
psql -X -q -v ON_ERROR_STOP=1 --single-transaction \
  -h "${host}" -p 5432 -U "${PG_USER}" -d "${db}" \
  -f "${sql}" \
  -c "UPDATE public.incidents SET status = 'escalated', resolution = 'restored from backup' WHERE status = 'in_progress'" \
  >/dev/null ||
  die "restore failed (nothing was written; single transaction)"

unset PGPASSWORD
echo "RESTORED ${ws} ${latest}"
