#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Push .env into the GitHub `production` environment secrets.
#
# One direction only: .env -> GitHub. There is no pull, because GitHub secrets
# are write-only by design and pretending otherwise would invite someone to
# treat this repo as the backup. It is not. Keep the filled .env in a password
# manager.
#
#     bash scripts/set-gh-secrets.sh              # push
#     bash scripts/set-gh-secrets.sh --dry-run    # show what would be pushed
#     bash scripts/set-gh-secrets.sh --file x.env # a different source file
#
# Invoked through `bash` rather than executed: the exec bit does not survive a
# Windows checkout, the same reason the workflows call scripts/*.sh that way.
#
# ── Values never touch argv ──────────────────────────────────────────────────
# `gh secret set NAME --body "$v"` puts the secret in the process table, where
# any other process on the box can read it for as long as the call runs. Piping
# to stdin keeps it in a pipe buffer instead. Nothing here echoes a value, and
# the summary counts rather than lists.
#
# ── Parsed, not sourced ──────────────────────────────────────────────────────
# `source .env` would EXECUTE it — a value containing `$(...)` runs. It also
# mangles anything with a space or a quote. So this splits on the first `=` and
# treats the remainder as literal, which is what a webhook URL full of `?` and
# `&` needs.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO="Keshav0375/Sentinel-infra"
ENVIRONMENT="production"
ENV_FILE=".env"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)     DRY_RUN=1; shift ;;
    --file)        ENV_FILE="$2"; shift 2 ;;
    --environment) ENVIRONMENT="$2"; shift 2 ;;
    --repo)        REPO="$2"; shift 2 ;;
    -h|--help)     sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# ── Preconditions, each with the fix in the message ──────────────────────────
if [ ! -f "${ENV_FILE}" ]; then
  echo "error: ${ENV_FILE} not found." >&2
  echo "       cp .env.example .env   and fill it in." >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "error: the GitHub CLI (gh) is not installed." >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "error: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

# The environment must already exist. `gh secret set --env` against a missing
# environment fails with a 404 that names neither the environment nor the fix.
if ! gh api "repos/${REPO}/environments/${ENVIRONMENT}" >/dev/null 2>&1; then
  echo "error: environment '${ENVIRONMENT}' does not exist in ${REPO}." >&2
  echo "       Create it under Settings -> Environments." >&2
  exit 1
fi

echo "source:      ${ENV_FILE}"
echo "repo:        ${REPO}"
echo "environment: ${ENVIRONMENT}"
[ "${DRY_RUN}" -eq 1 ] && echo "mode:        DRY RUN — nothing will be written"
echo

set_count=0
skip_count=0
skipped_names=()

while IFS= read -r line || [ -n "${line}" ]; do
  # Strip a trailing CR so a file saved by a Windows editor does not append an
  # invisible character to every secret — a failure that surfaces days later as
  # an API rejecting a key that looks correct in every log.
  line="${line%$'\r'}"

  # Comments and blanks.
  case "${line}" in ''|\#*) continue ;; esac

  # Split on the FIRST `=` only. Everything after it is literal: a webhook URL
  # is full of `=` and must survive intact.
  key="${line%%=*}"
  value="${line#*=}"

  # Trim surrounding whitespace from the key.
  key="$(printf '%s' "${key}" | tr -d '[:space:]')"

  # Strip ONE matched pair of surrounding quotes. Every .env convention treats
  # them as delimiters, not content, so `URL="https://x"` means the URL without
  # the quotes. Keeping them literal is a failure that survives every check here
  # -- the push succeeds, the seed succeeds, the vault holds a value that LOOKS
  # right in the portal, and the first request fails on a malformed URL.
  # Caught live: a 29-character URL arrived as 31 characters.
  #
  # Only a MATCHED pair is removed, so a value that legitimately begins or ends
  # with a lone quote survives intact. Interior quotes are never touched.
  first="${value%"${value#?}"}"
  last="${value#"${value%?}"}"
  if [ ${#value} -ge 2 ] && [ "${first}" = "${last}" ]      && { [ "${first}" = '"' ] || [ "${first}" = "'" ]; }; then
    value="${value#?}"
    value="${value%?}"
  fi

  [ -n "${key}" ] || continue

  if [ -z "${value}" ]; then
    skip_count=$(( skip_count + 1 ))
    skipped_names+=("${key}")
    continue
  fi

  if [ "${DRY_RUN}" -eq 1 ]; then
    echo "  would set  ${key}  (${#value} chars)"
    set_count=$(( set_count + 1 ))
    continue
  fi

  # stdin, not --body: see the header note on argv.
  if printf '%s' "${value}" \
       | gh secret set "${key}" --env "${ENVIRONMENT}" --repo "${REPO}" >/dev/null 2>&1; then
    echo "  set        ${key}"
    set_count=$(( set_count + 1 ))
  else
    echo "  FAILED     ${key}" >&2
    exit 1
  fi
done < "${ENV_FILE}"

echo
echo "set: ${set_count}   skipped (empty): ${skip_count}"

if [ "${skip_count}" -gt 0 ]; then
  echo
  echo "Not set — the vault will come up without these:"
  for n in "${skipped_names[@]}"; do echo "  - ${n}"; done
  echo
  echo "That is a valid state. Fill them in and re-run this, then re-run apply;"
  echo "the seed step adds what is newly available and leaves the rest alone."
fi
