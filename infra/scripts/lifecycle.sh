#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Plan, apply, or destroy — in an order that cannot corrupt the estate.
#
#     bash scripts/lifecycle.sh --action destroy --scope all --confirm destroy-everything
#     bash scripts/lifecycle.sh --action apply   --scope all --deployment sentinel
#     bash scripts/lifecycle.sh --action plan    --scope platform
#
# ── Why a script and not steps in the workflow ───────────────────────────────
# Same three reasons pause.sh was extracted: the gate's shellcheck glob covers
# scripts/*.sh, a two-hundred-line `run:` block is not reviewable, and this runs
# locally where a workflow step does not. The last one matters most here — a
# teardown you can only trigger through a web form is a teardown you cannot
# rehearse.
#
# ── Ordering is the safety property, and it is asymmetric ────────────────────
# A deployment reads the platform through `terraform_remote_state`, puts a
# namespace on its cluster and a database on its Postgres server. So:
#
#     apply    platform FIRST, then deployments   (dependency before dependent)
#     destroy  deployments FIRST, then platform   (dependent before dependency)
#
# Destroying the platform while a deployment still exists does not fail loudly.
# It leaves an orphaned state file describing resources that no longer exist,
# and the next plan against it reports a create for an estate half of which is
# already gone. That is the failure this ordering prevents.
#
# ── Trust nothing, verify separately ─────────────────────────────────────────
# `terraform destroy` exiting 0 means "everything Terraform KNEW ABOUT is gone".
# It says nothing about resources dropped from state by a failed apply. On
# 2026-08-30 an entire deployment — a resource group, a Key Vault, and a
# database on the SHARED Postgres server — was found alive because a destroy had
# never completed and nothing checked. verify-estate.sh is the answer to that,
# and it queries Azure rather than state.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

ACTION=""
SCOPE=""
DEPLOYMENT=""
ENVIRONMENT="dev"
CONFIRM=""
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --action)      ACTION="$2"; shift 2 ;;
    --scope)       SCOPE="$2"; shift 2 ;;
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --confirm)     CONFIRM="$2"; shift 2 ;;
    --target)      TARGET="$2"; shift 2 ;;
    -h|--help)     sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "${ACTION}" in
  plan|apply|destroy) ;;
  *) echo "error: --action must be plan, apply, or destroy (got '${ACTION}')" >&2; exit 2 ;;
esac

case "${SCOPE}" in
  deployment|platform|all) ;;
  *) echo "error: --scope must be deployment, platform, or all (got '${SCOPE}')" >&2; exit 2 ;;
esac

# ── Required configuration, checked up front ─────────────────────────────────
# Every one of these becomes a -var Terraform needs. Discovering a missing one
# eight minutes into an apply is the failure this loop exists to prevent.
for required in TF_SUBSCRIPTION_ID TF_LOCATION TF_PG_ADMIN_OBJECT_ID \
                TF_PG_ADMIN_PRINCIPAL_NAME TF_KV_ADMIN_OBJECT_ID; do
  if [ -z "${!required:-}" ]; then
    echo "error: ${required} is not set. The workflow passes it from a repository variable." >&2
    exit 1
  fi
done

# ── -target is a plan-only escape hatch ──────────────────────────────────────
# `terraform destroy -target=X` removes X and leaves the rest of the workspace
# in state, which is precisely how a half-destroyed deployment is produced. An
# apply with -target is the same hazard pointed the other way: it writes state
# that no full plan ever agreed with.
if [ -n "${TARGET}" ] && [ "${ACTION}" != "plan" ]; then
  echo "error: --target is accepted only with --action plan." >&2
  echo "       Partial applies and partial destroys leave state no plan agrees with." >&2
  echo "       If you need one, run it locally and own the result." >&2
  exit 1
fi

# ── The destroy confirmation, before anything is touched ─────────────────────
# Checked here rather than only in the workflow, so a local run is guarded too.
if [ "${ACTION}" = "destroy" ]; then
  case "${SCOPE}" in
    deployment) expected="${DEPLOYMENT}" ;;
    platform)   expected="platform" ;;
    all)        expected="destroy-everything" ;;
    *)          expected="" ;;
  esac
  if [ "${CONFIRM}" != "${expected}" ]; then
    echo "error: confirm does not match." >&2
    echo "       scope '${SCOPE}' requires exactly: ${expected}" >&2
    echo "       This is the only thing between a mis-click and an irreversible action." >&2
    exit 1
  fi
fi

if [ "${SCOPE}" != "platform" ] && [ -z "${DEPLOYMENT}" ]; then
  echo "error: --deployment is required for scope '${SCOPE}'." >&2
  exit 1
fi

TF_COMMON=(
  -no-color -input=false
  -var "subscription_id=${TF_SUBSCRIPTION_ID}"
  -var "location=${TF_LOCATION}"
  -var "pg_admin_object_id=${TF_PG_ADMIN_OBJECT_ID}"
  -var "pg_admin_principal_name=${TF_PG_ADMIN_PRINCIPAL_NAME}"
  -var "kv_admin_object_id=${TF_KV_ADMIN_OBJECT_ID}"
  -var "kv_seeder_object_id=${TF_KV_SEEDER_OBJECT_ID:-}"
  -var "gha_app_object_id=${TF_GHA_APP_OBJECT_ID:-}"
)

# ── A fresh az session before every long phase ──────────────────────────────
# az's CI session rests on a GitHub OIDC assertion that expires minutes after
# azure/login; kubelogin (via namespace.tf) and the vault purge below both lean
# on az long after that. Terraform itself is unaffected (ARM_USE_OIDC). A no-op
# outside GitHub Actions — see az-refresh.sh.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
refresh_az() { bash "${SCRIPT_DIR}/az-refresh.sh"; }

# kubelogin's stderr, which Terraform would otherwise drop — scripts/kubelogin.sh
# appends it here, and run_layer prints it when Terraform fails.
if [ -z "${SENTINEL_KUBELOGIN_LOG:-}" ]; then
  SENTINEL_KUBELOGIN_LOG="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/kubelogin.XXXXXX")"
  trap 'rm -f "${SENTINEL_KUBELOGIN_LOG}"' EXIT
fi
export SENTINEL_KUBELOGIN_LOG

# Only invocations that finally FAILED are in the log (kubelogin.sh discards
# the stderr of one its retry recovered). Redacted again here: an ::add-mask::
# issued inside the exec plugin never reached the runner, so nothing else would
# keep a JWT or a --federated-token value out of this public log.
show_kubelogin_log() {
  [ -s "${SENTINEL_KUBELOGIN_LOG}" ] || return 0
  echo "──── kubelogin stderr (the kubernetes provider does not show it) ────" >&2
  sed -E -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/<redacted-jwt>/g' \
         -e 's/(--federated-token[= ]+)[^ ]+/\1<redacted>/g' \
         "${SENTINEL_KUBELOGIN_LOG}" >&2
  echo "──── end kubelogin stderr ────" >&2
}

# ── Workspace helpers ────────────────────────────────────────────────────────

# `terraform workspace list` marks the current workspace with '*' and indents
# the rest. Strip both, then drop `default` (never used for real state) and
# `platform` (handled explicitly, never as a deployment).
list_deployment_workspaces() {
  terraform workspace list \
    | sed 's/^[* ]*//' \
    | grep -v '^$' \
    | grep -vx 'default' \
    | grep -vx 'platform' \
    || true
}

select_workspace() {
  terraform workspace select "$1" 2>/dev/null || terraform workspace new "$1"
}

# Has the platform been APPLIED, not merely configured?
#
# A deployment cannot be PLANNED before the platform exists, and that is a
# property of the design rather than a bug to route around: deployment.tf reads
# the platform through `terraform_remote_state`, so the cluster name, the OIDC
# issuer URL and the Postgres FQDN are not inputs it can compute -- they are the
# platform's OUTPUTS, and outputs do not exist until an apply has produced them.
# Terraform reports this as four unrelated-looking errors about an "object with
# no attributes"; this check exists so the reason is stated once, plainly, before
# that happens.
#
# `apply --scope all` is unaffected: the platform is applied FIRST, so by the
# time the deployment runs its outputs are real. Only `plan` can reach a
# deployment while the platform is still hypothetical.
platform_is_applied() {
  local current count
  current="$(terraform workspace show)"
  terraform workspace select platform >/dev/null 2>&1 || { echo "no"; return; }
  count="$(terraform state list 2>/dev/null | grep -c . || true)"
  terraform workspace select "${current}" >/dev/null 2>&1 || true
  if [ "${count}" -gt 0 ]; then echo "yes"; else echo "no"; fi
}

# Non-empty state means the workspace still owns resources.
workspace_resource_count() {
  terraform workspace select "$1" >/dev/null 2>&1 || { echo 0; return; }
  terraform state list 2>/dev/null | grep -c . || true
}

# ── One Terraform run against one workspace ──────────────────────────────────
run_layer() {
  local layer="$1" dep="$2" env="$3"
  local workspace verb vault remaining
  local args=("${TF_COMMON[@]}" -var "layer=${layer}" -var "deployment=${dep}" -var "environment=${env}")

  if [ "${layer}" = "platform" ]; then workspace="platform"; else workspace="${dep}-${env}"; fi

  echo
  echo "──────────────────────────────────────────────────────────────────────"
  echo "  ${ACTION} · layer=${layer} · workspace=${workspace}"
  echo "──────────────────────────────────────────────────────────────────────"

  # A destroy against a workspace that does not exist is a no-op, not a failure:
  # `--scope all` enumerates what is there, and re-running a partially failed
  # teardown must be safe.
  if [ "${ACTION}" = "destroy" ] && ! terraform workspace select "${workspace}" >/dev/null 2>&1; then
    echo "workspace ${workspace} does not exist — nothing to destroy"
    return 0
  fi

  select_workspace "${workspace}"

  # ── Capture the vault name BEFORE destroy removes the state holding it ────
  vault=""
  if [ "${ACTION}" = "destroy" ]; then
    vault="$(terraform output -json names 2>/dev/null | jq -r '.key_vault // empty' || true)"
    if [ -n "${vault}" ]; then echo "vault to purge after destroy: ${vault}"; fi
  fi

  case "${ACTION}" in
    plan)    verb="plan" ;;
    destroy) verb="destroy"; args+=(-auto-approve) ;;
    apply)   verb="apply";   args+=(-auto-approve) ;;
  esac
  if [ -n "${TARGET}" ]; then args+=("-target=${TARGET}"); fi

  # Immediately before Terraform, not once per script: the platform layer alone
  # can outlive the assertion the previous refresh was minted from.
  refresh_az
  : > "${SENTINEL_KUBELOGIN_LOG}"
  if ! terraform "${verb}" "${args[@]}"; then
    show_kubelogin_log
    return 1
  fi

  if [ "${ACTION}" != "destroy" ]; then return 0; fi

  # The destroy above can run for many minutes; the purge sweep is az.
  refresh_az

  # ── Post-destroy: purge, then PROVE the workspace is empty ────────────────
  # The provider purges a vault on destroy, so this is the sweep for one left
  # soft-deleted by an interrupted run. Without it, recreating the same
  # deployment fails on a globally unique name nobody can see.
  if [ -n "${vault}" ] \
     && az keyvault list-deleted --query "[?name=='${vault}'].name" -o tsv 2>/dev/null | grep -q .; then
    echo "purging soft-deleted vault ${vault}"
    az keyvault purge --name "${vault}" --location "${TF_LOCATION}" -o none
  fi

  # This assertion used to be `|| echo "::warning::"`. A swallowed warning here
  # is exactly how rg-demo1-dev-cc survived a destroy for five days: state was
  # never empty, the workspace was never removed, and nothing said so loudly
  # enough to stop the run being reported as a success. It is a failure now.
  remaining="$(terraform state list 2>/dev/null | grep -c . || true)"
  if [ "${remaining}" -ne 0 ]; then
    echo "::error::workspace ${workspace} still holds ${remaining} resource(s) after destroy." >&2
    terraform state list >&2
    return 1
  fi

  # Deleting a workspace requires standing somewhere else.
  terraform workspace select default >/dev/null
  terraform workspace delete "${workspace}"
  echo "workspace ${workspace} destroyed and removed"
}

# ── The ordered plan of work ─────────────────────────────────────────────────
echo "action=${ACTION} scope=${SCOPE} deployment=${DEPLOYMENT:-<none>} environment=${ENVIRONMENT}"

case "${SCOPE}:${ACTION}" in

  platform:destroy)
    # Refuse to pull the floor out from under a live deployment. `--scope all`
    # is the supported way to destroy the platform when deployments exist,
    # because it destroys them first.
    live=""
    while IFS= read -r workspace; do
      [ -n "${workspace}" ] || continue
      count="$(workspace_resource_count "${workspace}")"
      if [ "${count}" -gt 0 ]; then live="${live} ${workspace}(${count})"; fi
    done <<< "$(list_deployment_workspaces)"
    if [ -n "${live}" ]; then
      echo "::error::deployments still exist:${live}" >&2
      echo "::error::Destroying the platform now would orphan their state — they read it" >&2
      echo "::error::through terraform_remote_state and hold resources on its cluster and" >&2
      echo "::error::database. Use scope 'all', which destroys them first, in order." >&2
      exit 1
    fi
    run_layer platform sentinel plat
    ;;

  platform:*)
    run_layer platform sentinel plat
    ;;

  # ── A deployment cannot be planned OR applied before the platform ──────────
  # `plan` was guarded here on 2026-08-30 and `apply` was not, which left the
  # more expensive half of the pair unprotected: on 2026-09-13 an
  # `apply --scope deployment` against an unbuilt platform spent two minutes on
  # preflight, waking nothing, and a terraform init before dying inside the plan
  # with three separate provider errors —
  #   `local.platform is object with no attributes`
  # — none of which name the actual cause. The preconditions in deployment.tf
  # DID fire and their messages are correct, but they fire deep in a plan, after
  # the cost has been paid, and they arrive three at a time.
  #
  # Guarding here instead fails in seconds with one sentence. Both verbs read the
  # same outputs from the same remote state, so both need the same precondition.
  #
  # `destroy` is deliberately NOT guarded: refusing to tear a deployment down
  # because its dependency is already gone would strand resources that cost
  # money, which is the opposite of what a teardown path is for.
  deployment:plan|deployment:apply)
    if [ "$(platform_is_applied)" = "no" ]; then
      # "${ACTION}ed" reads "applyed" / "planed". Name the verb, do not inflect it.
      case "${ACTION}" in
        apply) verb="applied" ;;
        plan)  verb="planned" ;;
        *)     verb="${ACTION}" ;;
      esac
      echo "::error::the platform has not been applied, so this deployment cannot be ${verb}." >&2
      echo "::error::deployment.tf reads the cluster name, OIDC issuer and Postgres FQDN from" >&2
      echo "::error::the platform's terraform_remote_state. Those are OUTPUTS — they do not" >&2
      echo "::error::exist until the platform has been applied, so there is nothing to build" >&2
      echo "::error::against. Run apply with scope 'platform' (or scope 'all') first." >&2
      if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        {
          echo "### Refused — the platform does not exist yet"
          echo ""
          echo "**Nothing was created.** \`${DEPLOYMENT}-${ENVIRONMENT}\` reads the cluster name,"
          echo "OIDC issuer URL and Postgres FQDN from the platform's \`terraform_remote_state\`."
          echo "Those are *outputs*: they do not exist until the platform layer has been applied."
          echo ""
          echo "Run **Sentinel Infra — Deploy** with:"
          echo ""
          echo "| input | value |"
          echo "|---|---|"
          echo "| action | \`apply\` |"
          echo "| scope | \`platform\` |"
          echo "| environment_name | \`plat\` |"
          echo ""
          echo "Then re-run this deployment. Or use \`scope: all\`, which applies the"
          echo "platform first and the deployment second, in one run."
        } >> "$GITHUB_STEP_SUMMARY"
      fi
      exit 1
    fi
    run_layer deployment "${DEPLOYMENT}" "${ENVIRONMENT}"
    ;;

  deployment:*)
    run_layer deployment "${DEPLOYMENT}" "${ENVIRONMENT}"
    ;;

  all:destroy)
    # Dependents first, enumerated from the state store rather than from a list
    # someone maintains — a deployment nobody remembers creating is exactly the
    # one that gets left behind.
    found=0
    while IFS= read -r workspace; do
      [ -n "${workspace}" ] || continue
      found=1
      echo "found deployment workspace: ${workspace}"
      run_layer deployment "${workspace%-*}" "${workspace##*-}"
    done <<< "$(list_deployment_workspaces)"
    if [ "${found}" -eq 0 ]; then
      echo "no deployment workspaces — going straight to the platform"
    fi
    run_layer platform sentinel plat
    ;;

  all:plan)
    # The platform half is always plannable. The deployment half is only
    # plannable once the platform's outputs exist.
    run_layer platform sentinel plat

    if [ "$(platform_is_applied)" = "yes" ]; then
      run_layer deployment "${DEPLOYMENT}" "${ENVIRONMENT}"
    else
      echo
      echo "──────────────────────────────────────────────────────────────────────"
      echo "  SKIPPED: the deployment half cannot be planned yet"
      echo "──────────────────────────────────────────────────────────────────────"
      echo "The platform has not been applied, so it has no outputs, and"
      echo "deployment.tf reads its cluster name, OIDC issuer URL and Postgres FQDN"
      echo "from exactly those outputs. There is nothing to plan the deployment"
      echo "against — not a misconfiguration, a property of splitting the two"
      echo "layers into separate state files."
      echo
      echo "The platform plan above is complete and is the whole reviewable change."
      echo "Run 'apply' with scope 'all': it applies the platform FIRST, so the"
      echo "deployment then plans and applies against outputs that exist."
      if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
        {
          echo "### Deployment plan skipped — platform not applied"
          echo ""
          echo "The platform plan above is complete. The deployment half reads the"
          echo "platform's **outputs** through \`terraform_remote_state\`, and outputs do"
          echo "not exist until an apply has produced them, so there is nothing to plan"
          echo "it against yet."
          echo ""
          echo "Run \`apply\` with scope \`all\` — it applies the platform first, so the"
          echo "deployment plans against real values."
        } >> "${GITHUB_STEP_SUMMARY}"
      fi
    fi
    ;;

  all:*)
    # Dependency first: the deployment's remote-state read fails without it.
    run_layer platform sentinel plat
    run_layer deployment "${DEPLOYMENT}" "${ENVIRONMENT}"
    ;;
esac

echo
echo "lifecycle complete: ${ACTION} ${SCOPE}"
