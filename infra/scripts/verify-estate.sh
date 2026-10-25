#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Ask AZURE what exists, and fail if the answer disagrees with what we intended.
#
#     bash scripts/verify-estate.sh --mode destroyed --scope all
#     bash scripts/verify-estate.sh --mode destroyed --scope deployment --deployment demo1 --environment dev
#     bash scripts/verify-estate.sh --mode applied   --scope platform
#
# ── Why this exists at all ───────────────────────────────────────────────────
# `terraform destroy` exiting 0 proves one thing: everything Terraform had in
# state is gone. It cannot speak for a resource dropped from state by a failed
# apply, created outside Terraform, or left behind because the run died between
# the delete and the state write.
#
# On 2026-08-30 the subscription held rg-demo1-dev-cc, a Key Vault, and a
# database on the SHARED Postgres server, five days after that deployment was
# believed destroyed. Every exit code involved was 0. The gap was that nothing
# ever asked Azure.
#
# So this is the referee, and it is deliberately NOT written in Terraform: a
# check that reads the same state it is checking proves nothing.
#
# ── The contract for `--scope all --mode destroyed` ──────────────────────────
# The subscription contains rg-sentinel-bootstrap and NetworkWatcherRG, and
# nothing else. That is the whole "not a single penny" requirement expressed as
# an assertion rather than a hope. Both survivors are $0:
#
#   rg-sentinel-bootstrap  Terraform state (~4 KB) + the three OIDC identities.
#                          Managed identities are free. Delete this and CI can
#                          no longer authenticate to rebuild anything.
#   NetworkWatcherRG       Azure creates it per region automatically and will
#                          recreate it if deleted. The resource is free; only
#                          its features (flow logs, packet capture) bill, and
#                          none are configured.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

ADVISORY=0
MODE=""
SCOPE=""
DEPLOYMENT=""
ENVIRONMENT="dev"

# Groups that must survive every teardown. Named explicitly rather than matched
# by pattern: a denylist that is a regex is a denylist that eventually matches
# something it should not.
readonly KEEP_BOOTSTRAP="rg-sentinel-bootstrap"
readonly KEEP_NETWATCH="NetworkWatcherRG"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)        MODE="$2"; shift 2 ;;
    --scope)       SCOPE="$2"; shift 2 ;;
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    # Report the findings, do not set the exit code. For a run whose ACTION
    # already failed: the workflow is red on that job's own result, the cause is
    # in that job's log, and a second red here describes a consequence as if it
    # were a cause. See the caller in .github/workflows/ci_infra.yml.
    --advisory)    ADVISORY=1; shift ;;
    -h|--help)     sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "${MODE}" in
  destroyed|applied) ;;
  *) echo "error: --mode must be destroyed or applied (got '${MODE}')" >&2; exit 2 ;;
esac

failures=0

fail() {
  echo "  FAIL  $*"
  failures=$(( failures + 1 ))
}

pass() {
  echo "  ok    $*"
}

# Indent a block of names under a FAIL line. A read loop rather than `sed s/^/`
# so shellcheck stays clean at style level -- the gate treats its own output as
# pass/fail, and a warning nobody is allowed to ignore is a warning that gets
# ignored.
indent() {
  while IFS= read -r line; do
    echo "          ${line}"
  done
}

echo "verifying: mode=${MODE} scope=${SCOPE} deployment=${DEPLOYMENT:-<none>}"
echo

# ─────────────────────────────────────────────────────────────────────────────
# destroyed
# ─────────────────────────────────────────────────────────────────────────────
if [ "${MODE}" = "destroyed" ]; then

  if [ "${SCOPE}" = "all" ]; then
    echo "── the subscription holds nothing but the bootstrap ──"
    extra="$(az group list --query "[?name!='${KEEP_BOOTSTRAP}' && name!='${KEEP_NETWATCH}'].name" -o tsv 2>/dev/null)"
    if [ -n "${extra}" ]; then
      fail "resource groups survive:"
      echo "${extra}" | indent
    else
      pass "only ${KEEP_BOOTSTRAP} and ${KEEP_NETWATCH} remain"
    fi

    # Belt to that braces. A resource group can be gone while a
    # subscription-level or differently-grouped resource is not, and these are
    # the types that actually bill.
    echo
    echo "── nothing billable anywhere in the subscription ──"
    for pair in \
      "aks:Microsoft.ContainerService/managedClusters" \
      "postgres:Microsoft.DBforPostgreSQL/flexibleServers" \
      "acr:Microsoft.ContainerRegistry/registries" \
      "vmss:Microsoft.Compute/virtualMachineScaleSets" \
      "disks:Microsoft.Compute/disks" \
      "public-ips:Microsoft.Network/publicIPAddresses" \
      "load-balancers:Microsoft.Network/loadBalancers" \
      "app-services:Microsoft.Web/sites" \
      "app-service-plans:Microsoft.Web/serverfarms" \
      "key-vaults:Microsoft.KeyVault/vaults" \
      "event-grid:Microsoft.EventGrid/topics" \
      ; do
      label="${pair%%:*}"
      type="${pair#*:}"
      found="$(az resource list --resource-type "${type}" --query "[].name" -o tsv 2>/dev/null)"
      if [ -n "${found}" ]; then
        fail "${label} still exist:"
        echo "${found}" | indent
      else
        pass "${label}"
      fi
    done

    # The state account is the one storage account allowed to survive.
    echo
    stray="$(az storage account list --query "[?resourceGroup!='${KEEP_BOOTSTRAP}'].name" -o tsv 2>/dev/null)"
    if [ -n "${stray}" ]; then
      fail "storage accounts outside ${KEEP_BOOTSTRAP}:"
      echo "${stray}" | indent
    else
      pass "no storage accounts outside the bootstrap group"
    fi

  else
    # Scoped teardown: the estate's own names, derived the same way
    # modules/naming derives them, so this cannot drift from what was built.
    echo "── this deployment's groups are gone ──"
    for group in "rg-${DEPLOYMENT}-${ENVIRONMENT}-cc" "rg-${DEPLOYMENT}-${ENVIRONMENT}-func-cc"; do
      if [ "$(az group exists -n "${group}" 2>/dev/null)" = "true" ]; then
        fail "${group} still exists"
      else
        pass "${group}"
      fi
    done

    # A database on the SHARED server outlives the deployment's own resource
    # group, so a group-only check would have missed exactly the leak that
    # prompted this script.
    echo
    echo "── no leftover database on the shared server ──"
    server="$(az postgres flexible-server list --query "[?starts_with(name,'psql-sentinel-plat')].name | [0]" -o tsv 2>/dev/null)"
    if [ -z "${server}" ]; then
      pass "no shared server (platform not applied) — nothing to check"
    else
      rg="$(az postgres flexible-server list --query "[?name=='${server}'].resourceGroup | [0]" -o tsv 2>/dev/null)"
      dbname="${DEPLOYMENT}_${ENVIRONMENT}"
      if az postgres flexible-server db show -s "${server}" -g "${rg}" -d "${dbname}" -o none 2>/dev/null; then
        fail "database ${dbname} still exists on ${server}"
      else
        pass "no ${dbname} on ${server}"
      fi
    fi
  fi

  # A soft-deleted vault holds its GLOBALLY UNIQUE name for 7 days, so the next
  # apply of the same deployment fails on a name nobody can see.
  echo
  echo "── no soft-deleted Key Vaults holding a name hostage ──"
  deleted="$(az keyvault list-deleted --query "[].name" -o tsv 2>/dev/null)"
  if [ -n "${deleted}" ]; then
    fail "soft-deleted vaults remain (purge them):"
    echo "${deleted}" | indent
  else
    pass "none"
  fi
fi

# ─────────────────────────────────────────────────────────────────────────────
# applied — the weaker direction, and honest about being so
# ─────────────────────────────────────────────────────────────────────────────
# An apply that reports success has already been checked by Terraform against
# its own plan. This asserts only that the things which COST money exist and are
# in a usable state, because "apply succeeded but the cluster is stopped" is a
# real outcome that the exit code does not describe.
if [ "${MODE}" = "applied" ]; then
  if [ "${SCOPE}" = "platform" ] || [ "${SCOPE}" = "all" ]; then
    echo "── platform is up ──"
    power="$(az aks list --query "[?starts_with(name,'aks-sentinel-plat')].powerState.code | [0]" -o tsv 2>/dev/null)"
    case "${power}" in
      Running) pass "cluster Running" ;;
      "")      fail "no platform cluster found" ;;
      *)       fail "cluster is ${power}, not Running" ;;
    esac

    state="$(az postgres flexible-server list --query "[?starts_with(name,'psql-sentinel-plat')].state | [0]" -o tsv 2>/dev/null)"
    case "${state}" in
      Ready) pass "postgres Ready" ;;
      "")    fail "no platform postgres found" ;;
      *)     fail "postgres is ${state}, not Ready" ;;
    esac
  fi

  if [ "${SCOPE}" = "deployment" ] || [ "${SCOPE}" = "all" ]; then
    echo
    echo "── deployment is present ──"
    group="rg-${DEPLOYMENT}-${ENVIRONMENT}-cc"
    if [ "$(az group exists -n "${group}" 2>/dev/null)" = "true" ]; then
      pass "${group}"
    else
      fail "${group} does not exist"
    fi
  fi
fi

echo
if [ "${failures}" -ne 0 ]; then
  if [ "${ADVISORY}" -eq 1 ]; then
    echo "::warning::verify-estate found ${failures} problem(s), reported but not failed."
    echo "The action itself already failed — this is the damage report for it, not"
    echo "a separate fault. Read the action's job for the cause, then re-run it;"
    echo "the same action is safe and idempotent."
    exit 0
  fi
  echo "::error::verify-estate found ${failures} problem(s). The run is NOT clean."
  echo "Re-running the same action is safe and idempotent; do that before"
  echo "investigating by hand."
  exit 1
fi

echo "verify-estate: clean."
