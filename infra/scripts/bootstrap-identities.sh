#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# One-time bootstrap: the four CI identities.
#
# Terraform cannot create the identity its own pipeline authenticates as, so
# these live in rg-sentinel-bootstrap beside the state account — same trust root,
# same reason.
#
# ── Why THREE infra identities and not one ───────────────────────────────────
# Dynamic deployments create their own resource groups and destroy must purge
# Key Vaults. Neither is possible with rights scoped to one resource group, so
# CI needs SUBSCRIPTION scope. That reverses R5.
#
# R5's reasoning still holds: the credential is reachable from `pull_request`
# workflows on a PUBLIC repo. R5 answered by narrowing the scope; under a dynamic
# model the scope cannot be narrowed, so the answer moves from SCOPE to
# REACHABILITY.
#
#   gha-plan    Reader + blob READER      pull_request, environment:plan
#   gha-deploy  Contributor + RBAC Admin  environment:production, environment:destroy
#   gha-ops     custom start/stop role    environment:ops
#   gha-app     NO subscription role      Sentinel-deployment environment:sentinel-dev
#
# A job declaring `environment: X` gets the OIDC subject
# `repo:<owner>/<repo>:environment:X` instead of the branch form, and GitHub will
# NOT mint that for a pull_request event. gha-deploy is therefore unreachable
# from a PR BY CONSTRUCTION — there is no rule to misconfigure. Same mechanism
# that failed phase 4's first apply, used deliberately.
#
# ── gha-app — the app pipeline's identity, not the estate's ──────────────────
# Sentinel-deployment's pipeline deploys code into an App Service that already
# exists and records the deploy in Postgres. It needs neither to create nor to
# delete anything, and it runs on every merge to a repo whose branches are
# broken ON PURPOSE. So it is a fourth identity rather than new FICs on
# gha-deploy, which can empty the subscription (decision 2026-10-05, R7).
#
# It lives here, not in the deployment layer, so its client ID survives every
# destroy/recreate of a deployment and the GitHub secret is pushed once. It
# holds NO role from this script: the deployment layer grants it Website
# Contributor on that deployment's App Service only, and scripts/
# grant-db-access.sh makes it a database principal (not a server admin).
#
# ── gha-plan gets blob READER, not Contributor ───────────────────────────────
# `terraform plan` normally takes a state lock, which is a blob WRITE. Granting
# that leaves the "read-only" identity able to corrupt state — nearly harmless is
# not the same as harmless. So plan runs `-lock=false` and this identity holds
# Storage Blob Data Reader, giving it NO write action anywhere in the
# subscription: a property that can be tested rather than asserted.
#
# The cost, stated: an unlocked plan racing a concurrent apply can read state
# mid-write and produce a stale plan. Acceptable — a plan is advisory, apply
# takes its own lock, and applies are dispatch-only. Not acceptable for apply.
#
# Idempotent. Requires: az, Owner on the subscription, and bootstrap-state.sh.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# Git Bash rewrites arguments that look like Unix paths. An Azure resource ID
# starts with `/subscriptions/`, so `--scope` arrives as
# `C:/Program Files/Git/subscriptions/...` and the API answers
# `MissingSubscription` — a message that says nothing about path conversion.
# Cost three failed imports in phase 1 and one failed bootstrap in phase 5.
# Inert on Linux.
export MSYS_NO_PATHCONV=1

# `az` emits CRLF on Windows. Command substitution strips the trailing newline
# but NOT the carriage return, so every captured value silently carries one —
# which breaks string comparisons and, worse, resource IDs. Built with printf
# because writing the escape inline is how it gets mangled by editing tools.
CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

EXPECTED_SUB="174e25ca-ab82-4671-a913-9c2f66e5924d"
LOCATION="${LOCATION:-canadacentral}"
BOOTSTRAP_RG="${BOOTSTRAP_RG:-rg-sentinel-bootstrap}"
GITHUB_OWNER="${GITHUB_OWNER:-Keshav0375}"
INFRA_REPO="${INFRA_REPO:-Sentinel-infra}"
DEPLOYMENT_REPO="${DEPLOYMENT_REPO:-Sentinel-deployment}"
OPS_ROLE_NAME="Sentinel Ops Start Stop"

uid6="$(printf '%s' "${EXPECTED_SUB}" | sha1sum | cut -c1-6)"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-stsentineltf${uid6}}"

ISSUER="https://token.actions.githubusercontent.com"
AUDIENCE="api://AzureADTokenExchange"
SUB_SCOPE="/subscriptions/${EXPECTED_SUB}"

# ── Context assertion ─────────────────────────────────────────────────────────
# `az` keeps ONE context in ~/.azure shared by every terminal, and Sentinel spans
# two tenants — any `az login --tenant <identity>` silently repoints every other
# session. This happened four times during the build. A script that creates the
# trust root must not run in the wrong subscription.
current_sub="$(az account show --query id -o tsv | nocr)"
if [ "${current_sub}" != "${EXPECTED_SUB}" ]; then
  echo "REFUSING: az context is subscription ${current_sub}, expected ${EXPECTED_SUB}" >&2
  echo "  az account set --subscription ${EXPECTED_SUB}" >&2
  exit 1
fi

SA_SCOPE="$(az storage account show --name "${STORAGE_ACCOUNT}" --resource-group "${BOOTSTRAP_RG}" --query id -o tsv 2>/dev/null | nocr)"
if [ -z "${SA_SCOPE}" ]; then
  echo "REFUSING: ${STORAGE_ACCOUNT} not found. Run scripts/bootstrap-state.sh first." >&2
  exit 1
fi

echo "==> subscription ${current_sub}"
echo "==> bootstrap RG ${BOOTSTRAP_RG}"

# ── helpers ───────────────────────────────────────────────────────────────────

ensure_identity() {
  local name="$1"
  if az identity show --name "${name}" --resource-group "${BOOTSTRAP_RG}" >/dev/null 2>&1; then
    echo "==> identity ${name} exists"
    return
  fi
  echo "==> creating identity ${name}"
  az identity create --name "${name}" --resource-group "${BOOTSTRAP_RG}" --location "${LOCATION}" -o none
  # A managed identity's service principal takes a moment to appear in Entra.
  # Role assignments against it fail PrincipalNotFound until it does, and that
  # error names the principal rather than the timing.
  echo "    waiting 20s for the service principal to replicate"
  sleep 20
}

# Built-in roles only — see the custom-role block for why those differ.
ensure_role() {
  local principal_id="$1" role="$2" scope="$3"
  if az role assignment list --assignee "${principal_id}" --scope "${scope}" \
       --role "${role}" --query "[0].id" -o tsv 2>/dev/null | grep -q .; then
    echo "    already has '${role}'"
    return
  fi
  echo "    granting '${role}'"
  az role assignment create \
    --assignee-object-id "${principal_id}" \
    --assignee-principal-type ServicePrincipal \
    --role "${role}" \
    --scope "${scope}" \
    -o none
}

ensure_fic() {
  local identity="$1" name="$2" subject="$3"
  if az identity federated-credential show --name "${name}" \
       --identity-name "${identity}" --resource-group "${BOOTSTRAP_RG}" >/dev/null 2>&1; then
    echo "    fic ${name} exists"
    return
  fi
  echo "    creating fic ${name} -> ${subject}"
  az identity federated-credential create \
    --name "${name}" \
    --identity-name "${identity}" \
    --resource-group "${BOOTSTRAP_RG}" \
    --issuer "${ISSUER}" \
    --subject "${subject}" \
    --audiences "${AUDIENCE}" \
    -o none
}

# The counterpart to ensure_fic. A bootstrap script that only ever ADDS cannot
# retire a subject: dropping the `ensure_fic` line stops a fresh tenant from
# getting the credential and leaves every existing one in place, which is the
# half of the change that does not matter. Converge, do not accumulate.
remove_fic() {
  local identity="$1" name="$2"
  if ! az identity federated-credential show --name "${name}"          --identity-name "${identity}" --resource-group "${BOOTSTRAP_RG}" >/dev/null 2>&1; then
    return
  fi
  echo "    removing fic ${name} (retired)"
  az identity federated-credential delete --name "${name}"     --identity-name "${identity}" --resource-group "${BOOTSTRAP_RG}" --yes -o none
}

principal_of() {
  az identity show --name "$1" --resource-group "${BOOTSTRAP_RG}" --query principalId -o tsv | nocr
}

client_of() {
  az identity show --name "$1" --resource-group "${BOOTSTRAP_RG}" --query clientId -o tsv | nocr
}

# ── gha-plan — reads everything, writes nothing ───────────────────────────────
echo
echo "-- gha-plan ------------------------------------------"
ensure_identity "gha-plan"
plan_pid="$(principal_of gha-plan)"
ensure_role "${plan_pid}" "Reader" "${SUB_SCOPE}"
ensure_role "${plan_pid}" "Storage Blob Data Reader" "${SA_SCOPE}"
# ── Cluster access, split control-plane vs data-plane ────────────────────────
# Cluster User Role is CONTROL plane: it permits `listClusterUserCredential`,
# i.e. fetching a kubeconfig. On a cluster WITHOUT Entra RBAC that kubeconfig is
# effectively cluster-wide access, and granting it here would be the same mistake
# as granting `listCredentials` — which phase 5 refused.
#
# It is safe only because the platform cluster sets `azure_rbac_enabled = true`.
# The kubeconfig then carries an exec plugin that mints an Entra token for THIS
# identity, and the DATA-plane role below decides what that token may do.
ensure_role "${plan_pid}" "Azure Kubernetes Service Cluster User Role" "${SUB_SCOPE}"
ensure_role "${plan_pid}" "Azure Kubernetes Service RBAC Reader" "${SUB_SCOPE}"

ensure_fic "gha-plan" "plan-environment" "repo:${GITHUB_OWNER}/${INFRA_REPO}:environment:plan"

# ── The `pull_request` subject is retired ─────────────────────────────────────
# It was never presented. Every job that authenticates as gha-plan declares
# `environment: plan` — run-preflight and run-plan in ci_infra_dry.yml, and
# verify in ci_infra.yml — and declaring an environment REPLACES the subject
# with `environment:<name>`. The branch/pull_request form is never sent, so this
# credential authorised nothing that the estate uses.
#
# It was, however, the only subject a fork pull request could ever match, and
# this repository is PUBLIC. gha-plan holds subscription-wide Reader plus
# Storage Blob Data Reader on the state account, and Terraform state carries the
# ACR admin password in plaintext (modules/acr sets admin_enabled = true). The
# grant bought nothing and its worst case is a registry credential, so it goes.
#
# If a future job needs gha-plan WITHOUT an environment, give that job
# `environment: plan` rather than restoring this — the environment is what makes
# the subject unmintable from a pull_request event in the first place.
remove_fic "gha-plan" "plan-pull-request"

# ── gha-deploy — creates and destroys ─────────────────────────────────────────
echo
echo "-- gha-deploy ----------------------------------------"
ensure_identity "gha-deploy"
deploy_pid="$(principal_of gha-deploy)"

# Contributor creates and destroys every resource type including resource
# groups. It ALSO already carries
# `Microsoft.KeyVault/locations/deletedVaults/purge/action` — that is not in its
# notActions — so no separate purge grant is needed. (Key Vault Contributor DOES
# notAction it, which is where that confusion comes from.) The destroy -> recreate
# cycle depends on purge working, so the acceptance test purges a real vault
# rather than trusting this paragraph.
ensure_role "${deploy_pid}" "Contributor" "${SUB_SCOPE}"

# Contributor cannot create role assignments: its notActions include
# Microsoft.Authorization/*/Write. The modules assign AcrPull, Key Vault roles
# and Postgres admins, so without this every apply touching a grant dies
# AuthorizationFailed.
#
# RBAC Administrator over Owner or User Access Administrator: its built-in ABAC
# condition forbids assigning Owner, UAA and itself, so this identity can grant
# what the modules need and cannot escalate its own privileges.
ensure_role "${deploy_pid}" "Role Based Access Control Administrator" "${SUB_SCOPE}"

# Apply writes state, so Contributor here rather than the Reader gha-plan gets.
ensure_role "${deploy_pid}" "Storage Blob Data Contributor" "${SA_SCOPE}"

# The CONTROL-plane role, deliberately. "Azure Kubernetes Service RBAC Cluster
# Admin" is a DATA-plane role and is inert unless the cluster enables
# Entra-integrated Kubernetes RBAC, which ours does not. This one returns the
# admin kubeconfig via `az aks get-credentials --admin`, which is what the
# kubernetes provider needs in order to create namespaces.
ensure_role "${deploy_pid}" "Azure Kubernetes Service Cluster Admin Role" "${SUB_SCOPE}"

# The data-plane half. gha-deploy already holds "Cluster Admin Role" (control
# plane, fetches the kubeconfig); this is what lets the token it mints actually
# create namespaces, quotas and service accounts.
ensure_role "${deploy_pid}" "Azure Kubernetes Service RBAC Cluster Admin" "${SUB_SCOPE}"

ensure_fic "gha-deploy" "deploy-production" "repo:${GITHUB_OWNER}/${INFRA_REPO}:environment:production"
ensure_fic "gha-deploy" "deploy-destroy" "repo:${GITHUB_OWNER}/${INFRA_REPO}:environment:destroy"

# ── gha-ops — starts and stops, nothing else ──────────────────────────────────
# Pause/resume is the most frequently run operation here, so it must not require
# the identity that can delete the subscription. Contributor would work and is
# far too much: it can delete every resource it can stop.
echo
echo "-- gha-ops -------------------------------------------"
ensure_identity "gha-ops"
ops_pid="$(principal_of gha-ops)"

# `--name` filters on the DISPLAY name and `[0].name` returns the GUID. The
# JMESPath alternative, `[?roleName=='...']`, silently matches nothing in this
# CLI version — worth knowing because it fails as an empty result, not an error.
ops_role_guid="$(az role definition list --custom-role-only true --name "${OPS_ROLE_NAME}" --query "[0].name" -o tsv 2>/dev/null | nocr)"

if [ -z "${ops_role_guid}" ]; then
  echo "==> creating custom role ${OPS_ROLE_NAME}"
  # A RELATIVE path, not mktemp: `az` on Windows cannot resolve a Git Bash
  # `/tmp/...` path, and `--role-definition` needs the `@file` form — a bare path
  # is parsed as a JSON string, giving "Expecting value: line 1 column 1".
  role_json="./.ops-role.json"
  cat > "${role_json}" <<ROLE
{
  "Name": "${OPS_ROLE_NAME}",
  "Description": "Start and stop Sentinel compute. Cannot create, delete, or modify anything.",
  "Actions": [
    "Microsoft.Resources/subscriptions/resourceGroups/read",
    "Microsoft.ContainerService/managedClusters/read",
    "Microsoft.ContainerService/managedClusters/start/action",
    "Microsoft.ContainerService/managedClusters/stop/action",
    "Microsoft.DBforPostgreSQL/flexibleServers/read",
    "Microsoft.DBforPostgreSQL/flexibleServers/start/action",
    "Microsoft.DBforPostgreSQL/flexibleServers/stop/action",
    "Microsoft.Web/sites/read",
    "Microsoft.Web/sites/start/action",
    "Microsoft.Web/sites/stop/action"
  ],
  "NotActions": [],
  "DataActions": [],
  "NotDataActions": [],
  "AssignableScopes": ["${SUB_SCOPE}"]
}
ROLE
  az role definition create --role-definition "@${role_json}" -o none
  rm -f "${role_json}"
  echo "    waiting 30s for the role definition to replicate"
  sleep 30
  ops_role_guid="$(az role definition list --custom-role-only true --name "${OPS_ROLE_NAME}" --query "[0].name" -o tsv | nocr)"
else
  echo "==> custom role ${OPS_ROLE_NAME} exists"
fi

if [ -z "${ops_role_guid}" ]; then
  echo "REFUSING: ${OPS_ROLE_NAME} is not listable. Role definitions are eventually" >&2
  echo "  consistent — re-run in a minute." >&2
  exit 1
fi

# ── Assigning a CUSTOM role needs the FULL ARM id ────────────────────────────
# `--role` takes a built-in role's display name, or a role definition ID. For a
# custom role neither the display name nor the bare GUID works — both fail with
# "Role <x> doesn't exist", which reads as though the role were missing when it
# is present, correctly scoped and listable. It wants the full path.
#
# Worth the comment because the message points at existence rather than at the
# argument form, so the obvious next move — verifying the role exists — confirms
# it does and gets you no closer.
ops_role_id="${SUB_SCOPE}/providers/Microsoft.Authorization/roleDefinitions/${ops_role_guid}"

# Checked by roleDefinitionId rather than name: `az role assignment list --role`
# has the same custom-role resolution problem as create.
if az role assignment list --assignee "${ops_pid}" --scope "${SUB_SCOPE}" \
     --query "[?roleDefinitionId=='${ops_role_id}'] | [0].id" -o tsv 2>/dev/null | grep -q .; then
  echo "    already has ${OPS_ROLE_NAME}"
else
  echo "    granting ${OPS_ROLE_NAME}"
  az role assignment create \
    --assignee-object-id "${ops_pid}" \
    --assignee-principal-type ServicePrincipal \
    --role "${ops_role_id}" \
    --scope "${SUB_SCOPE}" \
    -o none
fi

ensure_fic "gha-ops" "ops-environment" "repo:${GITHUB_OWNER}/${INFRA_REPO}:environment:ops"

# ── gha-app — deploys app code, holds nothing at subscription scope ───────────
# Deliberately no ensure_role here; see the header. One subject only: the
# environment form means a pull_request event can never mint it.
echo
echo "-- gha-app -------------------------------------------"
ensure_identity "gha-app"
ensure_fic "gha-app" "app-sentinel-dev" "repo:${GITHUB_OWNER}/${DEPLOYMENT_REPO}:environment:sentinel-dev"

# ── Summary ───────────────────────────────────────────────────────────────────
cat <<SUMMARY

==> Identities ready. Set these as GitHub repository VARIABLES — not secrets.
    Under OIDC these are public identifiers, and masking them turns
    AADSTS700213's "subject ***" into an unreadable failure.

      AZURE_CLIENT_ID_PLAN    $(client_of gha-plan)
      AZURE_CLIENT_ID_DEPLOY  $(client_of gha-deploy)
      AZURE_CLIENT_ID_OPS     $(client_of gha-ops)
      GHA_APP_OBJECT_ID       $(principal_of gha-app)

    GHA_APP_OBJECT_ID is the principal (object) ID, not the client ID: the
    deployment layer assigns it Website Contributor. gha-app's CLIENT ID goes to
    Sentinel-deployment, not here — scripts/push-deploy-config.sh pushes it.

==> STILL MANUAL — the identity tenant (R4).

    sentinel-tf-identity needs federated credentials for the same environment
    subjects, because the azuread provider authenticates separately there. The
    two-tenant split doubles the credential surface; phase 4 learned this when a
    PR plan completed the entire azurerm refresh and then died on the azuread
    client, with an error naming the subject but not the tenant.

      needed:     environment:plan, environment:production, environment:destroy
      NOT needed: environment:ops - pause/resume never runs Terraform
      NOT needed: gha-app - the app pipeline never runs Terraform

    See docs/BOOTSTRAP.md.
SUMMARY
