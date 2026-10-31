#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Make gha-app, gha-deploy and the backend role principals on one database (R12).
#
#     bash scripts/grant-db-access.sh --deployment sentinel --environment dev
#     bash scripts/grant-db-access.sh --host <fqdn> --database sentinel_dev
#
# Run by the HUMAN Entra administrator of the Postgres server, after
# `az login` to the subscription's tenant. Not a workflow step, and not
# Terraform:
#
#   - Terraform has no resource for a role INSIDE a database. It attaches
#     server-level Entra administrators and creates the database; mapping an
#     Entra principal to a Postgres role is `pgaadauth_create_principal_with_oid`,
#     a SQL call (infra §3.2).
#   - gha-app is a database principal, NOT a server admin (decision 2026-10-05).
#     The app pipeline runs on every merge to a repo whose branches are broken
#     on purpose; it may record a deploy, it may not drop the database.
#   - Only an Entra admin can create an Entra principal, and the CI identities
#     are deliberately not admins.
#
# ── Every privilege is VERIFIED, not assumed from a GRANT ────────────────────
# A GRANT by someone who neither owns the object nor holds the grant option
# does not fail: Postgres prints "WARNING: no privileges were granted" and
# exits 0. So each GRANT here is an attempt, and what decides the result is
# has_database_privilege / has_schema_privilege / has_table_privilege read
# back afterwards. Any `false` exits non-zero and names what is missing.
#
# ── The table grant belongs to the table's OWNER ─────────────────────────────
# `deployments` is created by backend phase 1's migration and owned by the
# role that runs it, not by the Entra admin running this script. So that
# migration (or its owner role) must itself run
#     GRANT INSERT, SELECT ON deployments TO "gha-app";
# This script still tries the same grant, which works only if the admin
# happens to hold the grant option, and then VERIFIES it. On a deployment
# where the table does not exist yet, it prints a NOTE and exits 0. Re-run it
# after the migration, and after any destroy/recreate of the database.
#
# ── gha-deploy and the backend runtime role (decision 2026-10-25) ────────────
# gha-deploy runs the pre-destroy memory backup and the post-apply restore
# (memory-backup.sh / memory-restore.sh). It is a non-admin principal bound by
# object ID, like gha-app, scoped to THIS deployment's database only
# (decision 2026-10-25, amended 2026-10-28; sentinel-brain 6252a75):
#   CONNECT on the database, USAGE on schema public;
#   SELECT on every table (the dump) and INSERT on every table (the data-only
#   restore), USAGE, SELECT on every sequence;
#   UPDATE (status, resolution) on incidents only — the restore closes the
#   restored in_progress placeholders as escalated in its own transaction;
#   ALTER DEFAULT PRIVILEGES for the SELECT / INSERT / sequence grants, so
#   tables later migrations create are covered.
# NOT the server-wide pg_read_all_data / pg_write_all_data: the platform server
# is shared, and those roles reach every database on it. A membership left by
# an earlier run of this script is revoked and verified gone, and gha-deploy
# must hold no CONNECT grant of its own on any other database (read back; the
# PUBLIC default is the server's, see section 2, and reaches no data). It
# never creates, drops or re-owns a table. Without these grants the backup
# fails and so blocks the destroy (fail closed), so run this once before the
# first destroy that should keep memory, and again after migrating.
#
# The backend runtime role (`id-<deployment>-<env>-backend`, the backend UAMI)
# gets SELECT, INSERT, UPDATE, DELETE on incident_checkpoints and cache_entries
# when they exist, plus ALTER DEFAULT PRIVILEGES for what later migrations
# create. Default privileges belong to the role that CREATES the objects, and
# that is the caller here: the human admin who also runs `alembic upgrade
# head`. Run by anyone else, they would cover nobody's tables. A table that
# does not exist yet is reported PENDING — re-run after migrating, and after
# any platform recreate.
#
# ── Idempotent ───────────────────────────────────────────────────────────────
# The role is created only if it is missing, and a GRANT of a privilege
# already held changes nothing. If the role exists but maps to a different
# Entra object (gha-app was rebuilt), it refuses rather than silently keeping
# a role no token will ever match.
#
# ── The token never leaves this process ──────────────────────────────────────
# psql reads it from PGPASSWORD, which is the environment, not argv: an
# argument is visible in the process table to every user on the machine for as
# long as the call runs. Nothing here prints it, and there is no `set -x`.
#
# Requires: az (logged in as the Postgres Entra admin), psql, and either an
# initialised Terraform working directory (to read the deployment's outputs) or
# --host and --database.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Git Bash rewrites arguments that look like Unix paths; see
# bootstrap-identities.sh. Inert on Linux and macOS.
export MSYS_NO_PATHCONV=1

# `az` emits CRLF on Windows and command substitution keeps the CR. A token
# with a trailing CR is rejected as a bad password, which names nothing useful.
CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

# Run from the repo root, where `terraform output` finds the working directory.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

EXPECTED_SUB="174e25ca-ab82-4671-a913-9c2f66e5924d"
BOOTSTRAP_RG="${BOOTSTRAP_RG:-rg-sentinel-bootstrap}"
ROLE="gha-app"
TABLE="public.deployments"
DEPLOY_ROLE="gha-deploy"
GUID_RE='^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$'

DEPLOYMENT="sentinel"
ENVIRONMENT="dev"
PG_HOST=""
PG_DATABASE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --host)        PG_HOST="$2"; shift 2 ;;
    --database)    PG_DATABASE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

for tool in az psql; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is not installed." >&2
    exit 1
  fi
done

# ── Context assertion ─────────────────────────────────────────────────────────
# `az` keeps one context shared by every terminal, and Sentinel spans two
# tenants. A token from the identity tenant is refused by the server with a
# message about the token, not about the tenant.
current_sub="$(az account show --query id -o tsv 2>/dev/null | nocr || true)"
if [ "${current_sub}" != "${EXPECTED_SUB}" ]; then
  echo "REFUSING: az context is subscription '${current_sub}', expected ${EXPECTED_SUB}" >&2
  echo "  az login && az account set --subscription ${EXPECTED_SUB}" >&2
  exit 1
fi

# ── Where: the deployment's own outputs, unless given ─────────────────────────
# TF_WORKSPACE selects the workspace for this one call without rewriting
# .terraform/environment, so a later `terraform plan` in this checkout is not
# silently pointed at a different deployment.
workspace="${DEPLOYMENT}-${ENVIRONMENT}"
tf_output() {
  TF_WORKSPACE="${workspace}" terraform output -raw "$1" 2>/dev/null | nocr || true
}

if [ -z "${PG_HOST}" ] || [ -z "${PG_DATABASE}" ]; then
  if ! command -v terraform >/dev/null 2>&1; then
    echo "error: terraform is not installed; pass --host and --database." >&2
    exit 1
  fi
  [ -n "${PG_HOST}" ]     || PG_HOST="$(tf_output database_host)"
  [ -n "${PG_DATABASE}" ] || PG_DATABASE="$(tf_output database_name)"
fi

# `terraform output -raw` of a null output prints a warning on stderr and
# nothing on stdout, so an empty value here means "no database in this
# deployment" or "this workspace is not initialised" — not a transient error.
if [ -z "${PG_HOST}" ] || [ -z "${PG_DATABASE}" ]; then
  echo "error: could not resolve the database for workspace ${workspace}." >&2
  echo "       Either the deployment has no \`database\` component, or this checkout" >&2
  echo "       has not run \`terraform init\` against the remote state." >&2
  echo "       Pass --host and --database to skip the lookup." >&2
  exit 1
fi

# The Postgres role of an Entra user IS its UPN, verbatim (pg_admin_principal_name).
ADMIN_UPN="$(az account show --query user.name -o tsv | nocr)"

# The principal (object) ID the role is bound to. Binding by ID rather than by
# display name means a same-named object elsewhere in the tenant cannot be
# picked up, and a rebuilt gha-app is detected rather than silently mismatched.
APP_OID="$(az identity show --name "${ROLE}" --resource-group "${BOOTSTRAP_RG}" --query principalId -o tsv 2>/dev/null | nocr || true)"
if ! printf '%s' "${APP_OID}" | grep -Eq "${GUID_RE}"; then
  echo "error: could not resolve ${ROLE}'s principal ID in ${BOOTSTRAP_RG}." >&2
  echo "       Run scripts/bootstrap-identities.sh first." >&2
  exit 1
fi
DEPLOY_OID="$(az identity show --name "${DEPLOY_ROLE}" --resource-group "${BOOTSTRAP_RG}" --query principalId -o tsv 2>/dev/null | nocr || true)"
if ! printf '%s' "${DEPLOY_OID}" | grep -Eq "${GUID_RE}"; then
  echo "error: could not resolve ${DEPLOY_ROLE}'s principal ID in ${BOOTSTRAP_RG}." >&2
  echo "       Run scripts/bootstrap-identities.sh first." >&2
  exit 1
fi

echo "==> workspace ${workspace}"
echo "==> server    ${PG_HOST}"
echo "==> database  ${PG_DATABASE}"
echo "==> role      ${ROLE}"

# --resource-type oss-rdbms is the audience https://ossrdbms-aad.database.windows.net.
PGPASSWORD="$(az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv | nocr)"
export PGPASSWORD
export PGSSLMODE=require

# -X ignores ~/.psqlrc, which could otherwise echo queries or change output.
pg() {
  local db="$1"; shift
  psql -X -q -v ON_ERROR_STOP=1 -h "${PG_HOST}" -p 5432 -U "${ADMIN_UPN}" -d "${db}" "$@"
}

# One read-back query, printed as `t`/`f`.
check() { pg "$1" -tA -c "$2"; }

failed=()
pending=()

# ── 1. The role ───────────────────────────────────────────────────────────────
# Roles are server-wide, so each is created once, from the `postgres` database.
# pgaadauth_create_principal_with_oid(roleName, objectId, objectType, isAdmin,
# isMfa): `service` covers managed identities; not admin, no MFA claim (a
# workload token never carries one).
# The binding is the role's pgaadauth security label, 'aadauth,oid=<id>,type=…'
# (the documented format). Read from the shared catalog pg_shseclabel joined to
# pg_roles by OID, not from the pg_seclabels view: that view's objname is
# quote_ident(rolname), i.e. `"gha-app"` WITH the quotes, so matching it on the
# bare name never finds the row and every re-run would refuse. Lowercased on
# both sides, because a GUID's case is not part of its identity.
ensure_principal() {
  local role="$1" oid="$2" role_oid role_exists
  role_oid="$(check postgres "SELECT lower(substring(s.label from 'oid=([0-9a-fA-F-]+)')) FROM pg_catalog.pg_shseclabel s JOIN pg_catalog.pg_roles r ON r.oid = s.objoid WHERE s.classoid = 'pg_catalog.pg_authid'::regclass AND s.provider = 'pgaadauth' AND r.rolname = '${role}'")"
  role_exists="$(check postgres "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${role}')")"
  if [ "${role_exists}" = "t" ]; then
    if [ "${role_oid}" != "$(printf '%s' "${oid}" | tr '[:upper:]' '[:lower:]')" ]; then
      echo "REFUSING: role ${role} exists but is bound to Entra object '${role_oid:-none}'," >&2
      echo "  not ${role}'s current principal ${oid}. No token from ${role} will match it." >&2
      echo "  Drop it as the admin (DROP ROLE \"${role}\";) and re-run this script." >&2
      exit 1
    fi
    echo "    role ${role} exists, bound to ${oid}"
  else
    pg postgres -tA -c "SELECT * FROM pg_catalog.pgaadauth_create_principal_with_oid('${role}', '${oid}', 'service', false, false)" >/dev/null
    echo "    created role ${role} for Entra object ${oid} (service, not admin, no MFA)"
  fi
}

# CONNECT on the database and USAGE on public, attempted then read back (see 2).
grant_connect_usage() {
  local role="$1"
  pg "${PG_DATABASE}" -v db="${PG_DATABASE}" -v role="${role}" <<'SQL'
GRANT CONNECT ON DATABASE :"db" TO :"role";
GRANT USAGE ON SCHEMA public TO :"role";
SQL
  if [ "$(check "${PG_DATABASE}" "SELECT has_database_privilege('${role}', current_database(), 'CONNECT')")" = "t" ]; then
    echo "    verified ${role} CONNECT on ${PG_DATABASE}"
  else
    failed+=("${role}: CONNECT on database ${PG_DATABASE}")
  fi
  if [ "$(check "${PG_DATABASE}" "SELECT has_schema_privilege('${role}', 'public', 'USAGE')")" = "t" ]; then
    echo "    verified ${role} USAGE on schema public"
  else
    failed+=("${role}: USAGE on schema public")
  fi
}

ensure_principal "${ROLE}" "${APP_OID}"

# ── 2. Connect and schema usage ───────────────────────────────────────────────
# Attempted through stdin with psql variables: `:"db"` quotes the name as an
# identifier, and -c would not interpolate it at all. Then read back.
#
# ACCEPTED, recorded (PR #16 review): PUBLIC keeps the default CONNECT and TEMP
# on the shared server's databases, so CONNECT is not what keeps one
# deployment's principal out of another's database. That is acceptable because
# CONNECT alone reaches no data: in PG16 the public schema grants PUBLIC no
# CREATE and no table privileges, so a principal sees only what it is granted.
pg "${PG_DATABASE}" -v db="${PG_DATABASE}" -v role="${ROLE}" <<'SQL'
GRANT CONNECT ON DATABASE :"db" TO :"role";
GRANT USAGE ON SCHEMA public TO :"role";
SQL
if [ "$(check "${PG_DATABASE}" "SELECT has_database_privilege('${ROLE}', current_database(), 'CONNECT')")" = "t" ]; then
  echo "    verified CONNECT on ${PG_DATABASE}"
else
  failed+=("${ROLE}: CONNECT on database ${PG_DATABASE}")
fi
if [ "$(check "${PG_DATABASE}" "SELECT has_schema_privilege('${ROLE}', 'public', 'USAGE')")" = "t" ]; then
  echo "    verified USAGE on schema public"
else
  failed+=("${ROLE}: USAGE on schema public")
fi

# ── 3. The table, if the backend has created it ───────────────────────────────
table_exists="$(check "${PG_DATABASE}" "SELECT to_regclass('${TABLE}') IS NOT NULL")"
if [ "${table_exists}" = "t" ]; then
  # An attempt only (see the header). A non-owner without the grant option
  # gets a WARNING here, which psql passes through to stderr.
  pg "${PG_DATABASE}" -v role="${ROLE}" <<'SQL'
GRANT INSERT, SELECT ON public.deployments TO :"role";
SQL
  for priv in INSERT SELECT; do
    if [ "$(check "${PG_DATABASE}" "SELECT has_table_privilege('${ROLE}', '${TABLE}', '${priv}')")" = "t" ]; then
      echo "    verified ${priv} on ${TABLE}"
    else
      failed+=("${ROLE}: ${priv} on ${TABLE}")
    fi
  done
fi

# ── 4. gha-deploy (memory backup/restore) ─────────────────────────────────────
# Scoped to this database (see the header): table and sequence grants on what
# exists now, default privileges for what later migrations create, all read
# back. Default privileges belong to the role that creates the tables — the
# caller, who also runs `alembic upgrade head` (as in section 5).
echo "==> role      ${DEPLOY_ROLE}"
ensure_principal "${DEPLOY_ROLE}" "${DEPLOY_OID}"
grant_connect_usage "${DEPLOY_ROLE}"
# The server-wide predefined roles an earlier version of this script granted.
pg "${PG_DATABASE}" -v role="${DEPLOY_ROLE}" <<'SQL'
REVOKE pg_read_all_data, pg_write_all_data FROM :"role";
SQL
for member in pg_read_all_data pg_write_all_data; do
  if [ "$(check "${PG_DATABASE}" "SELECT pg_has_role('${DEPLOY_ROLE}', '${member}', 'MEMBER')")" = "f" ]; then
    echo "    verified ${DEPLOY_ROLE} is NOT a member of ${member} (server-wide)"
  else
    failed+=("${DEPLOY_ROLE}: still a member of the server-wide ${member}; REVOKE ${member} FROM \"${DEPLOY_ROLE}\" as its grantor")
  fi
done
other_connect="$(check "${PG_DATABASE}" "SELECT coalesce(string_agg(d.datname, ', ' ORDER BY d.datname), '') FROM pg_database d, aclexplode(d.datacl) a WHERE d.datname <> current_database() AND a.privilege_type = 'CONNECT' AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '${DEPLOY_ROLE}')")"
if [ -z "${other_connect}" ]; then
  echo "    verified ${DEPLOY_ROLE} holds no CONNECT grant on any other database"
else
  failed+=("${DEPLOY_ROLE}: CONNECT granted on other databases (${other_connect}); REVOKE CONNECT ON DATABASE … FROM \"${DEPLOY_ROLE}\"")
fi
pg "${PG_DATABASE}" -v role="${DEPLOY_ROLE}" <<'SQL'
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO :"role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"role";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT ON TABLES TO :"role";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"role";
SQL
deploy_defaults="$(check "${PG_DATABASE}" "SELECT count(DISTINCT a.privilege_type) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, aclexplode(d.defaclacl) a WHERE n.nspname = 'public' AND d.defaclobjtype = 'r' AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '${DEPLOY_ROLE}') AND a.privilege_type IN ('SELECT','INSERT')")"
if [ "${deploy_defaults}" = "2" ]; then
  echo "    verified ${DEPLOY_ROLE} default SELECT, INSERT on future tables in public"
else
  failed+=("${DEPLOY_ROLE}: default SELECT, INSERT on future tables (found ${deploy_defaults:-0} of 2)")
fi
# Every table in public must be dumpable and restorable; read back per table.
deploy_tables="$(check "${PG_DATABASE}" "SELECT quote_ident(schemaname) || '.' || quote_ident(tablename) FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")"
if [ -z "${deploy_tables}" ]; then
  echo "NOTE: no tables in public yet; ${DEPLOY_ROLE}'s default privileges cover them once migrated"
fi
for t in ${deploy_tables}; do
  for priv in SELECT INSERT; do
    if [ "$(check "${PG_DATABASE}" "SELECT has_table_privilege('${DEPLOY_ROLE}', '${t}', '${priv}')")" = "t" ]; then
      echo "    verified ${DEPLOY_ROLE} ${priv} on ${t}"
    else
      failed+=("${DEPLOY_ROLE}: ${priv} on ${t}")
    fi
  done
done
for seq in $(check "${PG_DATABASE}" "SELECT quote_ident(schemaname) || '.' || quote_ident(sequencename) FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename"); do
  for priv in USAGE SELECT; do
    if [ "$(check "${PG_DATABASE}" "SELECT has_sequence_privilege('${DEPLOY_ROLE}', '${seq}', '${priv}')")" = "t" ]; then
      echo "    verified ${DEPLOY_ROLE} ${priv} on sequence ${seq}"
    else
      failed+=("${DEPLOY_ROLE}: ${priv} on sequence ${seq}")
    fi
  done
done
# The restore closes restored in_progress placeholders: two columns of incidents.
if [ "$(check "${PG_DATABASE}" "SELECT to_regclass('public.incidents') IS NOT NULL")" = "t" ]; then
  pg "${PG_DATABASE}" -v role="${DEPLOY_ROLE}" <<'SQL'
GRANT UPDATE (status, resolution) ON public.incidents TO :"role";
SQL
  for col in status resolution; do
    if [ "$(check "${PG_DATABASE}" "SELECT has_column_privilege('${DEPLOY_ROLE}', 'public.incidents', '${col}', 'UPDATE')")" = "t" ]; then
      echo "    verified ${DEPLOY_ROLE} UPDATE (${col}) on public.incidents"
    else
      failed+=("${DEPLOY_ROLE}: UPDATE (${col}) on public.incidents")
    fi
  done
  if [ "$(check "${PG_DATABASE}" "SELECT has_table_privilege('${DEPLOY_ROLE}', 'public.incidents', 'UPDATE')")" = "f" ]; then
    echo "    verified ${DEPLOY_ROLE} has no table-wide UPDATE on public.incidents"
  else
    failed+=("${DEPLOY_ROLE}: table-wide UPDATE on public.incidents (only status, resolution)")
  fi
fi

# ── 5. The backend runtime role ───────────────────────────────────────────────
# The backend UAMI's role (PGUSER in the pod). Its tables are created by later
# migrations, so default privileges cover the future and explicit grants cover
# what exists now. Both are verified.
BACKEND_ROLE="id-${DEPLOYMENT}-${ENVIRONMENT}-backend"
BACKEND_TABLES="public.incident_checkpoints public.cache_entries"
echo "==> role      ${BACKEND_ROLE}"
BACKEND_OID="$(az identity list --query "[?name=='${BACKEND_ROLE}'].principalId | [0]" -o tsv 2>/dev/null | nocr || true)"
if ! printf '%s' "${BACKEND_OID}" | grep -Eq "${GUID_RE}"; then
  failed+=("${BACKEND_ROLE}: backend UAMI not found; apply the deployment first")
else
  ensure_principal "${BACKEND_ROLE}" "${BACKEND_OID}"
  grant_connect_usage "${BACKEND_ROLE}"
  pg "${PG_DATABASE}" -v role="${BACKEND_ROLE}" <<'SQL'
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"role";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"role";
SQL
  defaults="$(check "${PG_DATABASE}" "SELECT count(DISTINCT a.privilege_type) FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace, aclexplode(d.defaclacl) a WHERE n.nspname = 'public' AND d.defaclobjtype = 'r' AND d.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND a.grantee = (SELECT oid FROM pg_roles WHERE rolname = '${BACKEND_ROLE}') AND a.privilege_type IN ('SELECT','INSERT','UPDATE','DELETE')")"
  if [ "${defaults}" = "4" ]; then
    echo "    verified ${BACKEND_ROLE} default privileges on future tables in public"
  else
    failed+=("${BACKEND_ROLE}: default SELECT, INSERT, UPDATE, DELETE on future tables (found ${defaults:-0} of 4)")
  fi
  for t in ${BACKEND_TABLES}; do
    if [ "$(check "${PG_DATABASE}" "SELECT to_regclass('${t}') IS NOT NULL")" != "t" ]; then
      pending+=("${t}")
      continue
    fi
    pg "${PG_DATABASE}" -v role="${BACKEND_ROLE}" -v tbl="${t}" <<'SQL'
GRANT SELECT, INSERT, UPDATE, DELETE ON :tbl TO :"role";
SQL
    for priv in SELECT INSERT UPDATE DELETE; do
      if [ "$(check "${PG_DATABASE}" "SELECT has_table_privilege('${BACKEND_ROLE}', '${t}', '${priv}')")" = "t" ]; then
        echo "    verified ${BACKEND_ROLE} ${priv} on ${t}"
      else
        failed+=("${BACKEND_ROLE}: ${priv} on ${t}")
      fi
    done
  done
fi

if [ "${#failed[@]}" -gt 0 ]; then
  echo >&2
  echo "FAILED: these privileges are NOT held:" >&2
  for f in "${failed[@]}"; do echo "  - ${f}" >&2; done
  echo >&2
  echo "  A table privilege must be granted by the table's owner: backend phase 1's" >&2
  echo "  migration (or its owner role) runs" >&2
  echo "      GRANT INSERT, SELECT ON deployments TO \"${ROLE}\";" >&2
  echo "  then re-run this script to verify." >&2
  exit 1
fi

echo
if [ "${table_exists}" = "t" ]; then
  echo "==> done. ${ROLE} can record deploys in ${PG_DATABASE} (verified)."
else
  echo "NOTE: ${TABLE} does not exist yet in ${PG_DATABASE}, so the table grant is PENDING."
  echo "      Backend phase 1's migration creates it and must grant INSERT, SELECT to"
  echo "      \"${ROLE}\" as the table's owner. Re-run this script after that migration"
  echo "      to verify; until then the app pipeline's record stage fails with"
  echo "      \"relation does not exist\" (decision 2026-10-05, record-stage policy)."
fi
echo "==> done. ${DEPLOY_ROLE} can back up and restore ${PG_DATABASE} (verified)."
if [ "${#pending[@]}" -gt 0 ]; then
  echo "NOTE: PENDING for ${BACKEND_ROLE} (not created yet): ${pending[*]}"
  echo "      re-run grant-db-access.sh after migrating (and after any platform recreate)."
else
  echo "==> done. ${BACKEND_ROLE} can use its tables in ${PG_DATABASE} (verified)."
fi
