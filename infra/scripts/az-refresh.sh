#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Re-log the az CLI in from a FRESH GitHub OIDC token. CI only; locally a no-op.
#
#     bash scripts/az-refresh.sh
#
# ── Why the session azure/login made does not last a long run ────────────────
# azure/login@v2 logs az in with `--federated-token`: a GitHub OIDC assertion
# that lives for minutes, not hours. az keeps the ARM access token it minted at
# login (good for an hour), so ARM calls keep working. But a token for a
# DIFFERENT resource has to be minted later from that same assertion, and once
# the assertion has expired Entra refuses it (AADSTS700024).
#
# Two resources are first asked for long after login:
#
#   kubelogin   `--login azurecli` asks az for a token for the AKS server app
#               the first time Terraform touches a namespace — AFTER the
#               platform apply. Run 37399746578 (2026-10-06): the platform took
#               ~10 min, kubelogin ran 10m46s after login and exited 1.
#   Key Vault   seed-vault.sh asks for a vault.azure.net token after both
#               applies.
#
# The azurerm provider and the backend are unaffected: ARM_USE_OIDC makes them
# request a fresh assertion from GitHub themselves, every time. This does the
# same thing for az.
#
# ── The token is never printed ───────────────────────────────────────────────
# It is registered with ::add-mask:: before it is used anywhere, nothing here
# runs under `set -x`, az's stdout is discarded and its stderr is redacted
# before it is shown. The REQUEST token GitHub hands the job travels to curl on
# stdin, never on argv. The OIDC token itself does sit in az's argv for the
# length of the login — exactly as azure/login puts it there — on a runner that
# is torn down with the job.
#
# ── Retried, with a NEW token each time ──────────────────────────────────────
# This runs before Terraform starts; a transient AAD error here would end a run
# that has not done anything yet. Three attempts, each with a freshly minted
# assertion (an expired or replayed one is precisely the failure being fixed).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Not GitHub Actions, or a job without `id-token: write`: the caller's own az
# login is the session, and there is nothing to refresh it from.
if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  exit 0
fi

# The step's identity, from the same ARM_* the azurerm provider reads — so the
# refreshed az session is the identity Terraform is, never a different one.
for required in ARM_CLIENT_ID ARM_TENANT_ID ARM_SUBSCRIPTION_ID; do
  if [ -z "${!required:-}" ]; then
    echo "::error::az-refresh: ${required} is not set. Pass it in the step's env: block." >&2
    exit 1
  fi
done

# A logged-in az that is NOT the step's identity means the env and the login
# disagree about who this job is. Re-logging in would silently switch it, so
# refuse instead.
current="$(az account show --query user.name -o tsv 2>/dev/null | tr -d '\r' || true)"
if [ -n "${current}" ] \
   && [ "$(printf '%s' "${current}" | tr '[:upper:]' '[:lower:]')" != "$(printf '%s' "${ARM_CLIENT_ID}" | tr '[:upper:]' '[:lower:]')" ]; then
  echo "::error::az-refresh: az is logged in as ${current}, but ARM_CLIENT_ID is ${ARM_CLIENT_ID}." >&2
  echo "::error::Refusing to switch identity mid-job — fix the step's env: block." >&2
  exit 1
fi

# Strip anything JWT-shaped and any --federated-token value from text that is
# about to be shown.
redact() {
  sed -E -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/<redacted-jwt>/g' \
         -e 's/(--federated-token[= ]+)[^ ]+/\1<redacted>/g'
}

# `audience` is the value Entra's federated credentials are configured for —
# the same one azure/login requests. The request token reaches curl through
# stdin (`-H @-`), so it never appears in a process listing.
mint_token() {
  local response
  response="$(printf 'Authorization: Bearer %s\n' "${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
    | curl -sSf --retry 3 --max-time 30 -H @- \
        "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=api://AzureADTokenExchange")" || {
    echo "::error::az-refresh: GitHub refused an OIDC token. Does the job grant 'id-token: write'?" >&2
    return 1
  }
  jq -r '.value // empty' <<< "${response}"
}

attempts=3
for attempt in $(seq 1 "${attempts}"); do
  token="$(mint_token)" || exit 1
  if [ -z "${token}" ]; then
    echo "::error::az-refresh: the OIDC response carried no token." >&2
    exit 1
  fi
  echo "::add-mask::${token}"

  if err="$(az login --service-principal \
              --username "${ARM_CLIENT_ID}" \
              --tenant "${ARM_TENANT_ID}" \
              --federated-token "${token}" \
              --allow-no-subscriptions -o none 2>&1)"; then
    unset token err
    az account set --subscription "${ARM_SUBSCRIPTION_ID}"
    echo "az session refreshed from a new GitHub OIDC token"
    exit 0
  fi
  unset token
  printf '%s\n' "${err}" | redact >&2
  if [ "${attempt}" -lt "${attempts}" ]; then
    echo "az-refresh: az login failed (attempt ${attempt}/${attempts}); retrying with a new token" >&2
    sleep $((attempt * 5))
  fi
done

echo "::error::az-refresh: az login failed ${attempts} times; the az session is still the stale one." >&2
exit 1
