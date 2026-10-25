#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# The kubernetes provider's credential plugin: kubelogin, plus the two things
# a bare `kubelogin` exec could not give us. Called by namespace.tf, not by hand.
#
# ── 1. Its stderr is kept — for failures only, and redacted ──────────────────
# An exec plugin's stderr goes to the provider process, which Terraform only
# logs under TF_LOG. A failure therefore surfaced as nothing more than
#     getting credentials: exec: executable kubelogin failed with exit code 1
# with the reason (an AADSTS code, an az error) thrown away. When
# SENTINEL_KUBELOGIN_LOG names a file, each invocation's stderr is buffered and
# APPENDED there only if the invocation finally fails, so a failure the retry
# recovered from is never reported against some later, unrelated error.
# lifecycle.sh prints the file when Terraform fails. stderr only — the token
# travels on stdout, which is passed through untouched to the provider.
#
# An ::add-mask:: from inside this process never reaches the runner (Terraform
# owns its stdout and stderr), so masking cannot be the safety net here: the
# buffer is redacted — JWT-shaped strings and --federated-token values — before
# it is written, and lifecycle.sh redacts again when it prints.
#
# ── 2. In CI, one refresh and one retry ──────────────────────────────────────
# `--login azurecli` borrows az's session, and that session is minted from a
# GitHub OIDC assertion that expires within minutes (see az-refresh.sh).
# lifecycle.sh refreshes it before every layer; this is the backstop for a
# kubernetes resource first touched late in a long apply. Locally there is
# nothing to refresh from, so a failure is simply returned.
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log="${SENTINEL_KUBELOGIN_LOG:-}"

# No log requested (a local run, the PR plan): plain kubelogin semantics, plus
# the CI retry below.
buf=""
if [ -n "${log}" ]; then
  buf="$(mktemp "${TMPDIR:-/tmp}/kubelogin-attempt.XXXXXX")"
  trap 'rm -f "${buf}"' EXIT
fi

redact() {
  sed -E -e 's/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*/<redacted-jwt>/g' \
         -e 's/(--federated-token[= ]+)[^ ]+/\1<redacted>/g'
}

run_kubelogin() {
  if [ -n "${buf}" ]; then kubelogin "$@" 2>>"${buf}"; else kubelogin "$@"; fi
}

note() {
  if [ -n "${buf}" ]; then echo "kubelogin.sh: $*" >>"${buf}"; else echo "kubelogin.sh: $*" >&2; fi
}

# The invocation failed for good: tag it and hand the redacted buffer to the log.
give_up() {
  local status="$1"
  if [ -n "${buf}" ]; then
    {
      echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] kubelogin get-token failed (exit ${status})"
      redact < "${buf}"
    } >>"${log}"
  fi
  exit "${status}"
}

run_kubelogin "$@" && exit 0
status=$?

if [ -z "${ACTIONS_ID_TOKEN_REQUEST_URL:-}" ] || [ -z "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:-}" ]; then
  give_up "${status}"
fi

note "kubelogin exited ${status}; refreshing the az session and retrying once"
# stdout to /dev/null: this process's stdout IS the credential channel, and
# nothing az-refresh.sh prints belongs on it.
if [ -n "${buf}" ]; then
  bash "${here}/az-refresh.sh" >/dev/null 2>>"${buf}" || { note "refresh failed"; give_up "${status}"; }
else
  bash "${here}/az-refresh.sh" >/dev/null || { note "refresh failed"; give_up "${status}"; }
fi
run_kubelogin "$@" && exit 0
give_up "$?"
