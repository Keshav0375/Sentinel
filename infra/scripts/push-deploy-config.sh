#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Push the app pipeline's configuration to Sentinel-deployment (R8).
#
#     bash scripts/push-deploy-config.sh --dry-run    # show what would be pushed
#     bash scripts/push-deploy-config.sh              # push
#     bash scripts/push-deploy-config.sh --env-file ../Sentinel/.env \
#          --deployment sentinel --environment dev --repo Keshav0375/Sentinel-deployment
#
# Target: the GitHub environment `sentinel-dev` on Sentinel-deployment, created
# here if missing. Fixed, not a flag: gha-app federates exactly the subject
# `repo:Keshav0375/Sentinel-deployment:environment:sentinel-dev`, so any other
# environment name would hold credentials no job can use.
#
# The environment is restricted to the `main` branch (custom deployment branch
# policy, exactly `main`), so ONLY A WORKFLOW RUN ON MAIN can enter it and mint
# gha-app's token. A scenario branch can run a workflow; it cannot deploy.
# That is a merge gate only if main itself takes no direct pushes, so `main`
# on Sentinel-deployment is also protected here: a pull request is required
# (0 approvals, sole author), with no force pushes, no deletion, and admins
# not enforced so the owner can recover. There is NO required status check,
# by decision (2026-10-05): the deploy workflow runs only after a merge, so it
# can never report on a PR, and the deliberately broken scenario branches must
# stay mergeable.
#
#   secrets    AZURE_CLIENT_ID (gha-app), AZURE_TENANT_ID, AZURE_SUBSCRIPTION_ID,
#              DD_API_KEY
#   variables  AZURE_RG, APP_NAME, DEPLOYED_APP_URL, PG_HOST, PG_DATABASE,
#              PG_USER (= gha-app), DD_SITE
#
# ── Pushed once, not resolved per run ────────────────────────────────────────
# Every value is deterministic. gha-app lives in rg-sentinel-bootstrap, so its
# client ID survives a destroy/recreate of the deployment, and the deployment's
# names are uid = sha1(sub-dep-env)[0:4]. Re-run only when gha-app is rebuilt,
# the Datadog key rotates, or a different deployment becomes the target.
#
# A separate script from set-gh-secrets.sh, which mirrors a whole .env into
# Sentinel-infra's `production` environment. This one computes most of its
# values, writes variables as well as secrets, and reads one key from a file
# that belongs to another repo.
#
# ── Secret values never touch argv or output ─────────────────────────────────
# `gh secret set --body "$v"` puts the value in the process table; stdin keeps
# it in a pipe. A dry run reports a secret's length, never its value. Variables
# are not secret — GitHub shows them in every log — so they are printed. gh's
# own errors pass through to stderr: they name the API failure, never a value.
#
# ── The .env is parsed, not sourced ──────────────────────────────────────────
# Same rules as set-gh-secrets.sh: split on the first `=`, strip a trailing CR
# and one matched pair of quotes, execute nothing.
#
# Requires: az (logged in to the subscription), gh (authenticated, admin on the
# target repo), and an initialised Terraform working directory for the outputs.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

export MSYS_NO_PATHCONV=1
CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

# Run from the repo root, so `terraform output` and the default ../Sentinel/.env
# resolve the same wherever this is invoked from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

EXPECTED_SUB="174e25ca-ab82-4671-a913-9c2f66e5924d"
BOOTSTRAP_RG="${BOOTSTRAP_RG:-rg-sentinel-bootstrap}"
GH_ENVIRONMENT="sentinel-dev"
APP_IDENTITY="gha-app"

REPO="Keshav0375/Sentinel-deployment"
DEPLOYMENT="sentinel"
ENVIRONMENT="dev"
ENV_FILE="../Sentinel/.env"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     DRY_RUN=1; shift ;;
    --repo)        REPO="$2"; shift 2 ;;
    --deployment)  DEPLOYMENT="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --env-file)    ENV_FILE="$2"; shift 2 ;;
    -h|--help)     sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ── Preconditions, each with the fix in the message ──────────────────────────
for tool in az terraform; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "error: ${tool} is not installed." >&2
    exit 1
  fi
done

if [ ! -f "${ENV_FILE}" ]; then
  echo "error: ${ENV_FILE} not found. Pass --env-file <path> to the .env holding DD_API_KEY." >&2
  exit 1
fi

# A dry run reads Azure and Terraform but writes nothing to GitHub, so it does
# not need gh; with gh it also reports whether the environment exists.
have_gh=0
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  have_gh=1
elif [ "${DRY_RUN}" -eq 0 ]; then
  echo "error: gh is missing or not authenticated. Run: gh auth login" >&2
  exit 1
fi

current_sub="$(az account show --query id -o tsv 2>/dev/null | nocr || true)"
if [ "${current_sub}" != "${EXPECTED_SUB}" ]; then
  echo "REFUSING: az context is subscription '${current_sub}', expected ${EXPECTED_SUB}" >&2
  echo "  az login && az account set --subscription ${EXPECTED_SUB}" >&2
  exit 1
fi

# ── Value sources ─────────────────────────────────────────────────────────────

# One key from a .env, by the parsing rules in the header. Prints nothing when
# the key is absent or blank.
env_get() {
  local file="$1" want="$2" line key value first last
  while IFS= read -r line || [ -n "${line}" ]; do
    line="${line%"${CR}"}"
    case "${line}" in ''|\#*) continue ;; esac
    key="$(printf '%s' "${line%%=*}" | tr -d '[:space:]')"
    [ "${key}" = "${want}" ] || continue
    value="${line#*=}"
    first="${value%"${value#?}"}"
    last="${value#"${value%?}"}"
    if [ ${#value} -ge 2 ] && [ "${first}" = "${last}" ] \
       && { [ "${first}" = '"' ] || [ "${first}" = "'" ]; }; then
      value="${value#?}"
      value="${value%?}"
    fi
    printf '%s' "${value}"
    return
  done < "${file}"
}

# TF_WORKSPACE selects the workspace for this call only; .terraform/environment
# is left alone, so the checkout's next plan targets what it targeted before.
workspace="${DEPLOYMENT}-${ENVIRONMENT}"
tf_output() {
  TF_WORKSPACE="${workspace}" terraform output -raw "$1" 2>/dev/null | nocr || true
}

# Plain variables read by indirection rather than associative arrays: macOS
# ships bash 3.2, which has no `declare -A`.
# Read below through ${!v}, which shellcheck cannot follow.
# shellcheck disable=SC2034
{
  s_AZURE_CLIENT_ID="$(az identity show --name "${APP_IDENTITY}" --resource-group "${BOOTSTRAP_RG}" --query clientId -o tsv 2>/dev/null | nocr || true)"
  s_AZURE_TENANT_ID="$(az account show --query tenantId -o tsv | nocr)"
  s_AZURE_SUBSCRIPTION_ID="${current_sub}"
  s_DD_API_KEY="$(env_get "${ENV_FILE}" DD_API_KEY)"

  v_AZURE_RG="$(tf_output deployment_resource_group)"
  v_APP_NAME="$(tf_output app_name)"
  v_DEPLOYED_APP_URL="$(tf_output app_url)"
  v_PG_HOST="$(tf_output database_host)"
  v_PG_DATABASE="$(tf_output database_name)"
  v_PG_USER="${APP_IDENTITY}"
  # No default. The org is on US5 (us5.datadoghq.com); a fallback to US1 would
  # push a site where every API call 403s, far from the cause. Missing refuses.
  v_DD_SITE="$(env_get "${ENV_FILE}" DD_SITE)"
}

SECRET_ORDER=(AZURE_CLIENT_ID AZURE_TENANT_ID AZURE_SUBSCRIPTION_ID DD_API_KEY)
VAR_ORDER=(AZURE_RG APP_NAME DEPLOYED_APP_URL PG_HOST PG_DATABASE PG_USER DD_SITE)

# ── All or nothing ────────────────────────────────────────────────────────────
# A partial push produces a pipeline that fails several stages later on a blank
# value, naming the stage rather than the missing variable. Refuse up front.
missing=()
for n in "${SECRET_ORDER[@]}"; do v="s_${n}"; [ -n "${!v}" ] || missing+=("secret   ${n}"); done
for n in "${VAR_ORDER[@]}"; do v="v_${n}"; [ -n "${!v}" ] || missing+=("variable ${n}"); done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "REFUSING: these have no value:" >&2
  for m in "${missing[@]}"; do echo "  - ${m}" >&2; done
  echo >&2
  echo "  AZURE_CLIENT_ID          run scripts/bootstrap-identities.sh (creates gha-app)" >&2
  echo "  DD_API_KEY               set it in ${ENV_FILE}" >&2
  echo "  DD_SITE                  set it in ${ENV_FILE} (us5.datadoghq.com — the org is US5)" >&2
  echo "  AZURE_RG, APP_NAME, ...  apply workspace ${workspace} with the app_service and" >&2
  echo "                           database components, and run \`terraform init\` here" >&2
  exit 1
fi

echo "repo:        ${REPO}"
echo "environment: ${GH_ENVIRONMENT}"
echo "workspace:   ${workspace}"
echo "env file:    ${ENV_FILE}"
[ "${DRY_RUN}" -eq 1 ] && echo "mode:        DRY RUN — nothing will be written"
echo

# ── The environment, and who may enter it ───────────────────────────────────
# Only a workflow run on `main` may enter sentinel-dev, so only a run on main
# can mint gha-app's token. The FIC pins the ENVIRONMENT, not the branch:
# without a branch policy, any branch pushed to Sentinel-deployment (and its
# branches are broken on purpose) could declare `environment: sentinel-dev`
# and deploy. GitHub refuses the job before it starts when the branch is not
# allowed, so the token is never minted.
#
# Converged, not accumulated: custom policies, exactly one rule (`main`, type
# branch), and any other rule removed. Each call is skipped when the state
# already matches, so a re-run writes nothing.
env_api="repos/${REPO}/environments/${GH_ENVIRONMENT}"
WANT_POLICY='{"protected_branches":false,"custom_branch_policies":true}'

# The environment PUT REPLACES its protection rules: a field left out is
# cleared, so a bare {deployment_branch_policy} would silently drop required
# reviewers and the wait timer. The body is therefore built from the current
# environment, resending what is there and changing only the branch policy.
# (can_admins_bypass is tested for null rather than with `//`, which would
# turn an explicit false into true.)
ENV_BODY_JQ='{
  wait_timer: ([.protection_rules[]? | select(.type == "wait_timer") | .wait_timer][0] // 0),
  prevent_self_review: ([.protection_rules[]? | select(.type == "required_reviewers") | .prevent_self_review][0] // false),
  reviewers: ([.protection_rules[]? | select(.type == "required_reviewers") | .reviewers[]?
               | {type: .type, id: .reviewer.id}] | if length == 0 then null else . end),
  can_admins_bypass: (if .can_admins_bypass == null then true else .can_admins_bypass end),
  deployment_branch_policy: {protected_branches: false, custom_branch_policies: true}
} | tojson'

env_state=unknown        # missing | wrong-policy | ok
env_body="{\"deployment_branch_policy\":${WANT_POLICY}}"   # for a new environment
if [ "${have_gh}" -eq 1 ]; then
  # Compared field by field in jq, not as a JSON string: key order is not a
  # contract. A null policy (no restriction at all) compares false.
  if current="$(gh api "${env_api}" --jq '.deployment_branch_policy.protected_branches == false
                and .deployment_branch_policy.custom_branch_policies == true' 2>/dev/null)"; then
    if [ "${current}" = "true" ]; then
      env_state=ok
    else
      env_state=wrong-policy
      env_body="$(gh api "${env_api}" --jq "${ENV_BODY_JQ}")"
    fi
  else
    env_state=missing
  fi
fi

# The rule list exists only once the environment uses CUSTOM branch policies:
# with a null (or protected-branches) policy GitHub answers the list call with
# 404. So it is read only after the PUT has made the policy custom, or when it
# already was, and from then on a failed read stops the script. Paginated: the
# endpoint returns 30 rules a page, and a rule on page two would otherwise
# survive the convergence unseen.
list_policies() {
  gh api --paginate "${env_api}/deployment-branch-policies?per_page=100" \
    --jq '.branch_policies[] | "\(.id) \(.type // "branch") \(.name)"'
}

# Sets has_main and extra_policies from list_policies' output.
sort_policies() {
  local p
  has_main=0
  extra_policies=()
  while IFS= read -r p; do
    [ -n "${p}" ] || continue
    if [ "${p#* }" = "branch main" ]; then has_main=1; else extra_policies+=("${p}"); fi
  done <<< "$1"
}

if [ "${DRY_RUN}" -eq 1 ]; then
  case "${env_state}" in
    ok)           echo "  environment ${GH_ENVIRONMENT} exists, custom branch policies on" ;;
    wrong-policy) echo "  would set   ${GH_ENVIRONMENT} to custom branch policies" ;;
    missing)      echo "  would create environment ${GH_ENVIRONMENT} with custom branch policies" ;;
    *)            echo "  would create/align environment ${GH_ENVIRONMENT} (gh unavailable, not checked)" ;;
  esac
  if [ "${env_state}" = "ok" ]; then
    listed="$(list_policies)"
    sort_policies "${listed}"
    [ "${has_main}" -eq 1 ] && echo "  branch policy main exists" || echo "  would add   branch policy main"
    for p in ${extra_policies[@]+"${extra_policies[@]}"}; do echo "  would remove branch policy ${p#* }"; done
  else
    echo "  would add   branch policy main (no custom rules exist yet)"
  fi
else
  if [ "${env_state}" = "ok" ]; then
    echo "  environment ${GH_ENVIRONMENT} exists, custom branch policies on"
  else
    printf '%s' "${env_body}" | gh api -X PUT "${env_api}" --input - >/dev/null
    echo "  set         environment ${GH_ENVIRONMENT}: custom branch policies"
  fi
  # An assignment, not `sort_policies "$(list_policies)"`: set -e ignores a
  # failed substitution inside a command's arguments, and a failed read here
  # must stop the script rather than converge against an empty list.
  listed="$(list_policies)"
  sort_policies "${listed}"
  if [ "${has_main}" -eq 1 ]; then
    echo "  branch policy main exists"
  else
    gh api -X POST "${env_api}/deployment-branch-policies" -f name=main -f type=branch >/dev/null
    echo "  added       branch policy main"
  fi
  for p in ${extra_policies[@]+"${extra_policies[@]}"}; do
    gh api -X DELETE "${env_api}/deployment-branch-policies/${p%% *}" >/dev/null
    echo "  removed     branch policy ${p#* }"
  done
fi

# ── Branch protection on main ────────────────────────────────────────────────
# The environment's main-only policy is a MERGE gate only if nothing reaches
# main without a pull request; if a direct push to main were allowed, "only a
# run on main can deploy" would mean "anyone who can push can deploy". So main
# requires a PR (0 approvals: Keshav is the sole author and cannot approve his
# own PR), with no force pushes and no deletion. enforce_admins stays false
# so the owner can still recover a broken main by hand.
#
# No required status checks, and none are planned (decision 2026-10-05). The
# deploy workflow runs on push to main, AFTER the merge, so it never reports on
# a PR; requiring it would block every merge. And a check that did gate PRs
# would refuse the deployfail/* scenario branches, which are broken on purpose
# and must merge. Any checks already required (added by hand) are still RESENT
# below, because this PUT replaces the whole protection and a re-run must not
# silently drop them.
prot_api="repos/${REPO}/branches/main/protection"
PROT_OK_JQ='(.required_pull_request_reviews != null)
  and (.required_pull_request_reviews.required_approving_review_count == 0)
  and (.enforce_admins.enabled == false)
  and (.allow_force_pushes.enabled == false)
  and (.allow_deletions.enabled == false)'
PROT_BODY_JQ='{
  required_status_checks: (if .required_status_checks == null then null else {
      strict: .required_status_checks.strict,
      checks: [.required_status_checks.checks[]?
               | {context: .context} + (if .app_id then {app_id: .app_id} else {} end)]
    } end),
  enforce_admins: false,
  required_pull_request_reviews: {
    required_approving_review_count: 0,
    dismiss_stale_reviews: (.required_pull_request_reviews.dismiss_stale_reviews // false),
    require_code_owner_reviews: (.required_pull_request_reviews.require_code_owner_reviews // false)
  },
  restrictions: null,
  allow_force_pushes: false,
  allow_deletions: false,
  required_linear_history: (.required_linear_history.enabled // false),
  required_conversation_resolution: (.required_conversation_resolution.enabled // false)
} | tojson'
prot_body='{"required_status_checks":null,"enforce_admins":false,"required_pull_request_reviews":{"required_approving_review_count":0},"restrictions":null,"allow_force_pushes":false,"allow_deletions":false}'

prot_state=unknown       # unprotected | drifted | ok
if [ "${have_gh}" -eq 1 ]; then
  # 404 "Branch not protected" is the expected answer on a fresh repo.
  if prot_ok="$(gh api "${prot_api}" --jq "${PROT_OK_JQ}" 2>/dev/null)"; then
    if [ "${prot_ok}" = "true" ]; then
      prot_state=ok
    else
      prot_state=drifted
      prot_body="$(gh api "${prot_api}" --jq "${PROT_BODY_JQ}")"
    fi
  else
    prot_state=unprotected
  fi
fi

if [ "${DRY_RUN}" -eq 1 ]; then
  case "${prot_state}" in
    ok)          echo "  main is protected (PR required, no force push, no deletion)" ;;
    drifted)     echo "  would align main's protection: PR required (0 approvals), no force push, no deletion" ;;
    unprotected) echo "  would protect main: PR required (0 approvals), no force push, no deletion" ;;
    *)           echo "  would protect main if needed (gh unavailable, not checked)" ;;
  esac
elif [ "${prot_state}" = "ok" ]; then
  echo "  main is protected (PR required, no force push, no deletion)"
else
  printf '%s' "${prot_body}" | gh api -X PUT "${prot_api}" --input - >/dev/null
  echo "  protected   main: PR required (0 approvals), no force push, no deletion"
fi

# ── Secrets, by stdin ─────────────────────────────────────────────────────────
for n in "${SECRET_ORDER[@]}"; do
  v="s_${n}"
  if [ "${DRY_RUN}" -eq 1 ]; then
    value="${!v}"
    echo "  would set   secret   ${n}  (${#value} chars)"
    continue
  fi
  if printf '%s' "${!v}" \
       | gh secret set "${n}" --env "${GH_ENVIRONMENT}" --repo "${REPO}" >/dev/null; then
    echo "  set         secret   ${n}"
  else
    echo "  FAILED      secret   ${n}" >&2
    exit 1
  fi
done

# ── Variables ─────────────────────────────────────────────────────────────────
for n in "${VAR_ORDER[@]}"; do
  v="v_${n}"
  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "  would set   variable ${n} = ${!v}"
    continue
  fi
  if gh variable set "${n}" --env "${GH_ENVIRONMENT}" --repo "${REPO}" \
       --body "${!v}" >/dev/null; then
    echo "  set         variable ${n} = ${!v}"
  else
    echo "  FAILED      variable ${n}" >&2
    exit 1
  fi
done

echo
if [ "${DRY_RUN}" -eq 1 ]; then
  echo "dry run complete: ${#SECRET_ORDER[@]} secrets, ${#VAR_ORDER[@]} variables would be set."
else
  echo "pushed: ${#SECRET_ORDER[@]} secrets, ${#VAR_ORDER[@]} variables to ${REPO} / ${GH_ENVIRONMENT}."
fi
