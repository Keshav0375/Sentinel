#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Preflight — the checks a `terraform plan` structurally cannot make.
#
# A plan proves auth works, state is readable, config is valid, and what would
# change. It does NOT prove an apply will succeed. Every check below maps to a
# failure this project actually hit, and none of them are visible to a plan:
#
#   quota            the 6-vCPU regional ceiling
#   SKU availability B2ats_v2 failed the system-pool RAM floor; B2s was not in
#                    this subscription's allowed AKS list for canadacentral
#   providers        ten had to be registered by hand on a fresh subscription
#   name availability `sentineltfstate` was already taken by another tenant
#   soft-deleted vault a destroyed vault holds its name for 7 days
#   postgres state   `400 ServerStoppedError` broke a run three times
#   App Service F1   `Current Limit (F1 VMs): 0` killed an apply 14 minutes in,
#                    after the whole platform had already been built
#
# ── Two modes, because the identities differ ─────────────────────────────────
# `plan`  runs under gha-plan (Reader). It gets the checks Reader can perform.
# `apply` runs under gha-deploy. Name availability is a POST `*/action` that
#         Reader cannot call at all, so those checks live here.
#
# The alternative was widening gha-plan so one script could do everything. That
# was rejected: an identity any pull request can trigger should not gain actions
# to satisfy a checklist.
#
# A check that says "no" without saying "run this" has moved the problem rather
# than solved it, so every failure prints its remedy.
#
# Usage: preflight.sh --mode plan|apply [--deployment <d>] [--environment <e>]
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

# Git Bash rewrites `/subscriptions/...` into a Windows path; the API then
# answers MissingSubscription, which says nothing about path conversion.
export MSYS_NO_PATHCONV=1

CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

MODE="plan"
DEPLOYMENT=""
ENVIRONMENT=""
LOCATION="${AZURE_LOCATION:-canadacentral}"

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)        MODE="$2"; shift 2 ;;
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --location)    LOCATION="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

FAILED=0
pass() { printf '  %-42s PASS\n' "$1"; }
warn() { printf '  %-42s WARN  %s\n' "$1" "$2"; }
fail() { printf '  %-42s FAIL  %s\n' "$1" "$2"; FAILED=1; }

echo "preflight (mode=${MODE}, location=${LOCATION})"
echo

# ── Checks available to BOTH identities ──────────────────────────────────────

# The school tenant. If this fails nothing else can run, so it is first.
sub="$(az account show --query id -o tsv 2>/dev/null | nocr)"
if [ -n "${sub}" ]; then
  pass "azure token (school tenant)"
else
  fail "azure token (school tenant)" "az login --tenant <school>"
fi

# A fresh subscription has these unregistered and the resulting Terraform error
# reads like a bug in the config rather than a subscription that has never been
# used.
unregistered=""
for ns in Microsoft.ContainerService Microsoft.ContainerRegistry Microsoft.DBforPostgreSQL \
          Microsoft.KeyVault Microsoft.EventGrid Microsoft.Web Microsoft.Storage \
          Microsoft.ManagedIdentity; do
  state="$(az provider show --namespace "${ns}" --query registrationState -o tsv 2>/dev/null | nocr)"
  [ "${state}" = "Registered" ] || unregistered="${unregistered} ${ns}"
done
if [ -z "${unregistered}" ]; then
  pass "resource providers registered"
else
  fail "resource providers registered" "az provider register --namespace${unregistered}"
fi

# The ceiling that decides the whole architecture: 6 vCPU regionally, and an AKS
# node is 2. It is why deployments share one cluster instead of getting their own.
usage="$(az vm list-usage --location "${LOCATION}" --query "[?name.value=='cores'].{c:currentValue,l:limit}" -o tsv 2>/dev/null | nocr)"
if [ -n "${usage}" ]; then
  cur="$(echo "${usage}" | cut -f1)"
  lim="$(echo "${usage}" | cut -f2)"
  if [ "${cur}" -lt "${lim}" ]; then
    pass "regional vCPU quota (${cur}/${lim} used)"
  else
    fail "regional vCPU quota (${cur}/${lim} used)" "pause or destroy a deployment, or request an increase"
  fi
else
  warn "regional vCPU quota" "could not read usage"
fi

# Three SKUs were rejected before B2pls_v2 was found: the grant SKU failed the
# system pool's RAM floor, and the obvious small SKUs are not in this
# subscription's allowed list for this region.
if az vm list-skus --location "${LOCATION}" --size Standard_B2pls_v2 --query "[0].name" -o tsv 2>/dev/null | grep -q .; then
  pass "AKS node SKU available in region"
else
  fail "AKS node SKU available in region" "az vm list-skus --location ${LOCATION} --resource-type virtualMachines -o table"
fi

# Stopped is Postgres' NORMAL idle state, and a stopped server ERRORS a plan —
# the provider must refresh admins, database and extensions, none of which read
# while it is down.
servers="$(az postgres flexible-server list --query "[].{n:name,g:resourceGroup,s:state}" -o tsv 2>/dev/null | nocr)"
if [ -z "${servers}" ]; then
  pass "postgres servers (none yet)"
else
  stopped=""
  while IFS=$'\t' read -r n _ s; do
    [ "${s}" = "Ready" ] || stopped="${stopped} ${n}"
  done <<< "${servers}"
  if [ -z "${stopped}" ]; then
    pass "postgres servers running"
  elif [ "${MODE}" = "plan" ]; then
    # gha-plan holds no write action anywhere and CANNOT start them. Reporting is
    # the correct behaviour; starting is the Pause/Resume workflow's job.
    warn "postgres servers running" "stopped:${stopped} — run Pause/Resume (resume), the plan will fail otherwise"
  else
    fail "postgres servers running" "az postgres flexible-server start -n <name> -g <rg>"
  fi
fi

# ── Checks that need `*/action`, so apply-mode only ──────────────────────────
if [ "${MODE}" = "apply" ] && [ -n "${DEPLOYMENT}" ] && [ -n "${ENVIRONMENT}" ]; then
  echo
  uid="$(printf '%s' "${sub}-${DEPLOYMENT}-${ENVIRONMENT}" | sha1sum | cut -c1-4)"
  kv="kv-${DEPLOYMENT}-${ENVIRONMENT}-${uid}"
  st="st${DEPLOYMENT}${ENVIRONMENT}${uid}"

  # ── A name WE already hold is a re-apply, not a conflict ─────────────────
  # Every name check below asks "is it free?", and after a partially failed
  # apply the honest answer is "no — you made it". On 2026-10-06 run
  # 37402021269 was refused because stsentineldevb136 existed in
  # rg-sentinel-dev-func-cc, created by the failed run before it. Terraform
  # would have adopted it from state and carried on. So each check first asks
  # whether the holder is THIS subscription, and only a name held elsewhere
  # fails.
  #
  # `ours <az list … --query …>` prints the first match, or nothing.
  ours() { "$@" -o tsv 2>/dev/null | nocr | head -n 1; }

  # Vault names are global too (vault.azure.net). In order:
  #   live in this subscription   -> the deployment's own: re-apply
  #   soft-deleted here           -> a destroyed vault holds its name for 7
  #                                  days; without a purge, destroy -> recreate
  #                                  of the same deployment fails on a vault
  #                                  nobody can see
  #   otherwise                   -> ask the global namespace. Microsoft.KeyVault
  #                                  checkNameAvailability is a POST */action,
  #                                  which is why this is apply-mode only.
  kv_rg="$(ours az keyvault list --query "[?name=='${kv}'].resourceGroup")"
  if [ -n "${kv_rg}" ]; then
    pass "key vault (${kv}) already ours in ${kv_rg}, re-apply"
  elif az keyvault list-deleted --query "[?name=='${kv}'].name" -o tsv 2>/dev/null | grep -q .; then
    fail "key vault name free (${kv})" "az keyvault purge --name ${kv} --location ${LOCATION}"
  else
    kv_avail="$(az rest --method post \
      --url "https://management.azure.com/subscriptions/${sub}/providers/Microsoft.KeyVault/checkNameAvailability?api-version=2023-07-01" \
      --body "{\"name\":\"${kv}\",\"type\":\"Microsoft.KeyVault/vaults\"}" \
      --query nameAvailable -o tsv 2>/dev/null | nocr)"
    case "${kv_avail}" in
      true)  pass "key vault name free (${kv})" ;;
      false) fail "key vault name free (${kv})" "another tenant holds it — change the deployment name" ;;
      *)     fail "key vault name free (${kv})" "could not check — az keyvault check-name --name ${kv}" ;;
    esac
  fi

  # Globally unique across every Azure tenant, and a taken name fails with a
  # message that does not say "taken". check-name answers `false` for our OWN
  # account too, so ownership is asked before blaming another tenant.
  avail="$(az storage account check-name --name "${st}" --query nameAvailable -o tsv 2>/dev/null | nocr)"
  st_rg="$(ours az storage account list --query "[?name=='${st}'].resourceGroup")"
  if [ "${avail}" = "true" ]; then
    pass "storage account name free (${st})"
  elif [ -n "${st_rg}" ]; then
    pass "storage account (${st}) already ours in ${st_rg}, re-apply"
  elif [ "${avail}" = "false" ]; then
    fail "storage account name free (${st})" "another tenant holds it — change the deployment name"
  else
    fail "storage account name free (${st})" "could not check — az storage account check-name --name ${st}"
  fi

  # ── App Service F1 quota, by live probe ───────────────────────────────────
  # On 2026-08-30 an apply ran for fourteen minutes, built the entire platform,
  # and then died on:
  #
  #   creating App Service Plan ... 401 Unauthorized
  #   "Operation cannot be completed without additional quota.
  #    Current Limit (F1 VMs): 0  Current Usage: 0  Amount required: 1"
  #
  # This is a quota refusal wearing a 401, and nothing above catches it: the
  # vCPU check reads Microsoft.Compute cores, and F1 is a Microsoft.Web counter
  # that shares none of that budget.
  #
  # There is no read-only way to ask. Three were tried and all three lie:
  #   Microsoft.Web/locations/<loc>/usages      -> 501 MethodNotAllowed
  #   Microsoft.Web/.../validate (serverfarms)  -> {"status":"Success"} at limit 0
  #   az appservice list-locations --sku F1     -> lists the region regardless
  #
  # So the check is the operation. Create a Free plan, read the answer, delete
  # it. It costs ~20 seconds against fourteen minutes and a half-built estate,
  # and F1 is free, so a probe that briefly exists is billed nothing.
  #
  # ── The probe's own failure mode, and the mitigation ──────────────────────
  # A leaked probe would consume the single F1 slot it exists to protect —
  # turning the check into the outage. Two defences: the name is FIXED, not
  # random, so a leak is findable and self-healing (the next run deletes it
  # before creating); and the delete also runs from an EXIT trap, so a runner
  # cancellation still cleans up. Apply-mode only — gha-plan holds no write
  # action and would fail this on RBAC rather than on quota, which would be a
  # false alarm of exactly the kind this file is trying to end.
  probe_rg="rg-sentinel-bootstrap"
  probe_plan="asp-sentinel-preflight-probe"

  drop_probe() {
    az appservice plan delete -n "${probe_plan}" -g "${probe_rg}" --yes -o none 2>/dev/null || true
  }

  # This deployment's own F1 plan, if a previous apply already made one — the
  # app name is global (uid-suffixed), so it identifies the plan exactly.
  app="app-${DEPLOYMENT}-${ENVIRONMENT}-${uid}"
  own_f1=""
  app_plan="$(ours az webapp list --query "[?name=='${app}'].serverFarmId")"
  if [ -n "${app_plan}" ] \
     && [ "$(az appservice plan show --ids "${app_plan}" --query sku.name -o tsv 2>/dev/null | nocr)" = "F1" ]; then
    own_f1="${app_plan}"
  fi

  if az group show -n "${probe_rg}" -o none 2>/dev/null; then
    drop_probe                 # clear a leak from a cancelled run, then arm the trap
    trap drop_probe EXIT

    if probe_err="$(az appservice plan create \
                      --name "${probe_plan}" --resource-group "${probe_rg}" \
                      --location "${LOCATION}" --sku F1 --is-linux -o none 2>&1)"; then
      pass "App Service F1 quota (probe created)"
    elif printf '%s' "${probe_err}" | grep -qi 'quota' && [ -n "${own_f1}" ]; then
      # The slot is taken by this deployment's OWN plan (a partial apply made
      # it). A re-apply adopts that plan and needs no new slot.
      pass "App Service F1 quota (${app} already holds one, re-apply)"
    elif printf '%s' "${probe_err}" | grep -qi 'quota'; then
      fail "App Service F1 quota" "limit is 0 — raise it in Portal > Subscription > Usage + quotas (Microsoft.Web, ${LOCATION}), or set the App Service SKU to a paid tier"
    else
      # Not a quota answer. Report it rather than guessing — a probe that
      # reinterprets an unrelated error is worse than no probe.
      warn "App Service F1 quota" "probe inconclusive: $(printf '%s' "${probe_err}" | tr '
' ' ' | cut -c1-140)"
    fi

    drop_probe
    trap - EXIT
  else
    warn "App Service F1 quota" "no ${probe_rg} to probe in — skipped"
  fi
fi

echo
if [ "${FAILED}" -eq 0 ]; then
  echo "preflight: OK"
  exit 0
fi
echo "preflight: FAILED — see remedies above"
exit 1
