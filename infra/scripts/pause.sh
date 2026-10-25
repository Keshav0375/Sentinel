#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Pause / resume Sentinel compute.  (task 6.8)
#
# Lives here rather than inline in ci_pause.yml for three reasons: the gate lints
# every scripts/*.sh, a hundred-line `run:` block is not reviewable, and a script
# runs locally where a workflow step does not. Inline, it also hung actionlint's
# shell parser outright.
#
# (A line beginning `# shellcheck ...` is read as a DIRECTIVE, not a comment —
# which is how the first draft of this header failed to parse.)
#
# ── Pause is NOT zero cost, and the numbers are on the summary ───────────────
# running ~CAD 47/mo · paused ~CAD 14/mo · destroyed CAD 0, measured from the
# 2026-08-30 cost report. A pause button that only says "not free" leaves the
# reader to guess whether the remainder is pennies or most of it. It is most of
# it once the node stops: the disk and the IP do not care about power state.
# Stopping ends the COMPUTE charge, which dominates. These continue regardless:
# OS disks, storage, the AKS load balancer, and ACR's flat daily charge. Only
# destroy is zero. The summary says so, because a pause button that implies
# otherwise is worse than no button.
#
# ── Two limits worth knowing before you rely on it ───────────────────────────
#   Postgres auto-restarts after 7 DAYS — Azure forces it, no opt-out. A pause
#   is a lease, not a switch.
#   AKS stop/start takes 5-10 minutes each way.
#
# Idempotent: starting an already-running resource is an ERROR in Azure, not a
# no-op, so every action checks state first.
#
# Usage: pause.sh --action pause|resume --scope platform|all|<deployment>
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail
export MSYS_NO_PATHCONV=1

CR="$(printf '\r')"
nocr() { tr -d "${CR}"; }

ACTION="pause"
SCOPE="platform"

while [ $# -gt 0 ]; do
  case "$1" in
    --action) ACTION="$2"; shift 2 ;;
    --scope)  SCOPE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "${ACTION}" in
  pause|resume) ;;
  *) echo "--action must be pause or resume" >&2; exit 2 ;;
esac

echo "action=${ACTION} scope=${SCOPE}"
echo

report=""
note() { echo "  $1"; report="${report}
  $1"; }

# Does a resource name belong to the requested scope?
#   platform  only the shared layer (names carry -plat-)
#   all       everything
#   <name>    that deployment's resources
in_scope() {
  case "${SCOPE}" in
    platform) case "$1" in *-plat-*|*plat*) return 0 ;; *) return 1 ;; esac ;;
    all)      return 0 ;;
    *)        case "$1" in *"${SCOPE}"*) return 0 ;; *) return 1 ;; esac ;;
  esac
}

# ── AKS ──────────────────────────────────────────────────────────────────────
# Only the platform holds a cluster, and pausing it stops EVERY deployment's
# workloads at once. Same button, very different blast radius — hence the note.
while IFS=$'\t' read -r name rg; do
  [ -n "${name}" ] || continue
  in_scope "${name}" || continue
  state="$(az aks show -g "${rg}" -n "${name}" --query powerState.code -o tsv | nocr)"
  if [ "${ACTION}" = "pause" ] && [ "${state}" = "Running" ]; then
    az aks stop -g "${rg}" -n "${name}" --no-wait
    note "aks ${name}: stopping (5-10 min) — this stops EVERY deployment's workloads"
  elif [ "${ACTION}" = "resume" ] && [ "${state}" != "Running" ]; then
    az aks start -g "${rg}" -n "${name}" --no-wait
    note "aks ${name}: starting (5-10 min)"
  else
    note "aks ${name}: already ${state}"
  fi
done <<EOF
$(az aks list --query "[].[name,resourceGroup]" -o tsv | nocr)
EOF

# ── Postgres ─────────────────────────────────────────────────────────────────
# Iterates every server rather than taking the first: database.mode=dedicated
# gives a deployment its own, so there can be several.
while IFS=$'\t' read -r name rg; do
  [ -n "${name}" ] || continue
  in_scope "${name}" || continue
  state="$(az postgres flexible-server show -n "${name}" -g "${rg}" --query state -o tsv | nocr)"
  if [ "${ACTION}" = "pause" ] && [ "${state}" = "Ready" ]; then
    az postgres flexible-server stop -n "${name}" -g "${rg}" -o none
    note "postgres ${name}: stopped — Azure force-restarts it in 7 days"
  elif [ "${ACTION}" = "resume" ] && [ "${state}" != "Ready" ]; then
    az postgres flexible-server start -n "${name}" -g "${rg}" -o none
    note "postgres ${name}: started"
  else
    note "postgres ${name}: already ${state}"
  fi
done <<EOF
$(az postgres flexible-server list --query "[].[name,resourceGroup]" -o tsv | nocr)
EOF

# ── App Service ──────────────────────────────────────────────────────────────
# F1 is free either way, so this is not about money — a stopped app also stops
# generating Datadog signal, which matters when you are not watching it.
while IFS=$'\t' read -r name rg; do
  [ -n "${name}" ] || continue
  in_scope "${name}" || continue
  if [ "${ACTION}" = "pause" ]; then
    az webapp stop -n "${name}" -g "${rg}" -o none && note "webapp ${name}: stopped"
  else
    az webapp start -n "${name}" -g "${rg}" -o none && note "webapp ${name}: started"
  fi
done <<EOF
$(az webapp list --query "[?kind!='functionapp'].[name,resourceGroup]" -o tsv | nocr)
EOF

echo
echo "Cannot be paused - these keep billing:"
echo "  node OS disk        ~CAD 10/mo   provisioned Premium SSD, P6 at 64 GB"
echo "  public IP           ~CAD  4/mo   the cluster's egress address"
echo "  ACR                 flat daily; covered by the AzureForStudents grant"
echo "  AKS load balancer   persists while the cluster exists"
echo "  state storage       a few KB; rounds to zero"
echo
echo "  running   ~CAD 47/mo      paused   ~CAD 14/mo      destroyed   CAD 0"
echo
echo "Measured from the 2026-08-30 cost report, not estimated. Pause is the right"
echo "call for hours or a day. For anything longer, destroy: apply rebuilds the"
echo "platform in ~10 minutes and re-seeds the vault automatically."

if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  {
    echo "### ${ACTION} · scope \`${SCOPE}\`"
    echo ""
    echo '```'
    echo "${report}"
    echo '```'
    echo "**Still billing while paused:** the node OS disk (~CAD 10/mo), the public IP"
    echo "(~CAD 4/mo), ACR, and the AKS load balancer. Stopping compute ends the"
    echo "largest charge and not the others."
    echo ""
    echo "| state | cost |"
    echo "|---|---|"
    echo "| running | ~CAD 47/mo |"
    echo "| paused | ~CAD 14/mo |"
    echo "| destroyed | **CAD 0** |"
    echo ""
    echo "Pause for hours. For longer, destroy — \`apply\` with scope \`all\` rebuilds in"
    echo "~10 minutes and re-seeds the vault, so the rebuild is cheap enough to prefer."
    if [ "${ACTION}" = "pause" ]; then
      echo ""
      echo "> Postgres auto-restarts after **7 days** — re-run to re-pause."
    else
      echo ""
      echo "> AKS takes 5-10 minutes to start. Wait before applying anything that touches it:"
      echo "> a stopped cluster plans fine and then fails the apply with \`OperationNotAllowed\`."
    fi
  } >> "${GITHUB_STEP_SUMMARY}"
fi
