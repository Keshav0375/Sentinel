"""Offline behaviour of infra/scripts/memory-backup.sh and memory-restore.sh
(decisions 2026-10-25 A, amended 2026-10-28: fail-closed backup, scoped role, restore exclusions).

The scripts run for real under the local bash with stubbed az, terraform, pg_dump, pg_restore and
psql; each stub logs "<name> <args>" to $STUB_LOG and behaves per STUB_* env vars, the pattern of
test_composite_actions.py. Nothing here reaches Azure, Terraform state or a database.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "infra" / "scripts"
BACKUP = SCRIPTS / "memory-backup.sh"
RESTORE = SCRIPTS / "memory-restore.sh"
GRANT = SCRIPTS / "grant-db-access.sh"

TOKEN = "tok-SECRET"
DUMP_NAME = re.compile(r"^sentinel-dev/\d{8}T\d{6}Z\.dump$")

# Stub bodies (run after the stub has logged "<name> <args>" to $STUB_LOG).
AZ_BODY = r"""
arg() {
  local want="$1"; shift
  while [ $# -gt 0 ]; do [ "$1" = "$want" ] && { echo "$2"; return; }; shift; done
}
case "$*" in
  "storage container show"*) exit "${STUB_CONTAINER_RC:-0}";;
  "account get-access-token"*) printf '%s\n' "$STUB_TOKEN";;
  "account show"*) echo "you@school.ca";;
  "storage blob upload"*)
    [ "${STUB_UPLOAD_RC:-0}" = 0 ] || exit "$STUB_UPLOAD_RC"
    arg -n "$@" >> "$STUB_LOG.uploaded";;
  "storage blob show"*) exit 0;;
  "storage blob list"*)
    [ -f "${STUB_BLOBS:-}" ] && cat "$STUB_BLOBS"
    [ -f "$STUB_LOG.uploaded" ] && cat "$STUB_LOG.uploaded"
    exit 0;;
  "storage blob delete"*) exit "${STUB_DELETE_RC:-0}";;
  "storage blob download"*) printf 'PGDMP' > "$(arg -f "$@")";;
  *) exit 9;;
esac
"""
TERRAFORM_BODY = r"""
output() {  # output <name> <value>: as Terraform behaves for STUB_OUTPUT_MISSING / _ERROR
  case " ${STUB_OUTPUT_MISSING:-} " in *" $1 "*)
    printf '\u2577\n\u2502 Error: Output "%s" not found\n\u2502\n' "$1" >&2; exit 1;; esac
  case " ${STUB_OUTPUT_ERROR:-} " in *" $1 "*)
    printf 'Error: Error acquiring the state lock\n' >&2; exit 1;; esac
  printf '%s' "$2"
}
case "$*" in
  "workspace list")
    [ "${STUB_LIST_RC:-0}" = 0 ] || {
      echo "Error: Failed to get existing workspaces: AuthorizationFailed" >&2
      exit "$STUB_LIST_RC"; }
    printf '%b' "${STUB_WORKSPACES-  default\n* sentinel-dev\n}";;
  "workspace select "*) case " ${STUB_SELECT_FAIL:-} " in *" $3 "*)
    echo "Error: Error acquiring the state lock" >&2; exit 1;; esac;;
  "output "*"-raw database_host")
    output database_host "${STUB_HOST-psql-x.postgres.database.azure.com}";;
  "output "*"-raw database_name")
    output database_name "${STUB_DB-sentinel_dev}";;
  *) exit 9;;
esac
"""
PG_DUMP_BODY = r"""
[ "${STUB_PGDUMP_RC:-0}" = 0 ] || exit "$STUB_PGDUMP_RC"
prev=""; for a in "$@"; do [ "$prev" = "-f" ] && printf 'PGDMP' > "$a"; prev="$a"; done
"""
PG_RESTORE_BODY = r"""
if [ "$1" = "--list" ]; then
  [ -f "${STUB_TOC:-}" ] && cat "$STUB_TOC"
  exit 0
fi
[ "${STUB_RESTORE_RC:-0}" = 0 ] || exit "$STUB_RESTORE_RC"
prev=""; for a in "$@"; do
  [ "$prev" = "-L" ] && cp "$a" "$STUB_LOG.list"
  [ "$prev" = "-f" ] && printf 'COPY public.incidents (id, status) FROM stdin;\n\\.\n' > "$a"
  prev="$a"
done
exit 0
"""
PSQL_BODY = r"""
case "$*" in
  *to_regclass*) echo "${STUB_MIGRATED:-t}";;
  *"count(*)"*) echo "${STUB_ROWS:-0}";;
  *"UPDATE public.incidents"*)
    prev=""; for a in "$@"; do [ "$prev" = "-f" ] && cp "$a" "$STUB_LOG.sql"; prev="$a"; done
    exit "${STUB_PSQL_RC:-0}";;
  *) exit 9;;
esac
"""

TOC = """\
;
; Archive created at 2026-10-28 00:00:00 UTC
;
3410; 0 16390 TABLE DATA public alembic_version admin
3411; 0 16391 TABLE DATA public services admin
3412; 0 16392 TABLE DATA public incidents admin
3413; 0 16393 TABLE DATA public deployments admin
3414; 0 16394 TABLE DATA public incident_checkpoints admin
3415; 0 16395 TABLE DATA public cache_entries admin
"""


@dataclass
class Run:
    returncode: int
    stdout: str
    stderr: str
    log: list[str]
    uploaded: list[str]


def _need(tool: str) -> None:
    if shutil.which(tool) is None:
        pytest.skip(f"{tool} not on PATH")


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f'#!/bin/bash\nprintf \'%s\\n\' "{name} $*" >> "$STUB_LOG"\n{body}\n')
    path.chmod(0o755)


def _run(script: Path, tmp_path: Path, args: list[str], env: dict[str, str] | None = None) -> Run:
    _need("bash")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, body in {
        "az": AZ_BODY,
        "terraform": TERRAFORM_BODY,
        "pg_dump": PG_DUMP_BODY,
        "pg_restore": PG_RESTORE_BODY,
        "psql": PSQL_BODY,
    }.items():
        _stub(bin_dir, name, body)
    stub_log = tmp_path / "stub.log"
    work = tmp_path / "tmp"
    work.mkdir(exist_ok=True)
    full_env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TMPDIR": str(work),
        "STUB_LOG": str(stub_log),
        "STUB_TOKEN": TOKEN,
        "GITHUB_ACTIONS": "true",
        **(env or {}),
    }
    proc = subprocess.run(
        ["bash", str(script), *args],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    log = stub_log.read_text().splitlines() if stub_log.exists() else []
    uploaded_file = Path(f"{stub_log}.uploaded")
    uploaded = uploaded_file.read_text().splitlines() if uploaded_file.exists() else []
    return Run(proc.returncode, proc.stdout, proc.stderr, log, uploaded)


def _calls(run: Run, prefix: str) -> list[str]:
    return [line for line in run.log if line.startswith(prefix)]


def _flag(line: str, flag: str) -> str:
    parts = line.split()
    return parts[parts.index(flag) + 1]


def _blobs(tmp_path: Path, names: list[str]) -> dict[str, str]:
    path = tmp_path / "blobs"
    path.write_text("".join(f"{name}\n" for name in names))
    return {"STUB_BLOBS": str(path)}


def _backup(tmp_path: Path, *extra: str, env: dict[str, str] | None = None) -> Run:
    args = ["--scope", "deployment", "--deployment", "sentinel", "--environment", "dev", *extra]
    return _run(BACKUP, tmp_path, args, env)


def _restore(tmp_path: Path, *extra: str, env: dict[str, str] | None = None) -> Run:
    toc = tmp_path / "toc"
    toc.write_text(TOC)
    args = ["--deployment", "sentinel", "--environment", "dev", *extra]
    return _run(RESTORE, tmp_path, args, {"STUB_TOC": str(toc), **(env or {})})


# --- memory-backup.sh ---


def test_backup_uploads_named_dump(tmp_path: Path) -> None:
    run = _backup(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    uploads = _calls(run, "az storage blob upload")
    assert len(uploads) == 1
    name = _flag(uploads[0], "-n")
    assert DUMP_NAME.match(name), name
    assert _flag(uploads[0], "-c") == "memory-backups"
    assert "--auth-mode login" in uploads[0]
    assert "--overwrite false" in uploads[0]
    assert _calls(run, "az storage blob show")
    dumps = _calls(run, "pg_dump ")
    assert len(dumps) == 1 and "-Fc" in dumps[0].split()
    assert "-h psql-x.postgres.database.azure.com" in dumps[0]
    assert "-d sentinel_dev" in dumps[0]
    assert f"BACKED_UP sentinel-dev {name}" in run.stdout


def test_backup_excludes_cache_data(tmp_path: Path) -> None:
    run = _backup(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    argv = _calls(run, "pg_dump ")[0].split()
    assert "--exclude-table-data=public.cache_entries" in argv
    assert "-Fc" in argv


def test_backup_pg_user_default(tmp_path: Path) -> None:
    ci = _backup(tmp_path / "ci")
    assert ci.returncode == 0, ci.stdout + ci.stderr
    assert _flag(_calls(ci, "pg_dump ")[0], "-U") == "gha-deploy"
    given = _backup(tmp_path / "given", "--pg-user", "x")
    assert given.returncode == 0, given.stdout + given.stderr
    assert _flag(_calls(given, "pg_dump ")[0], "-U") == "x"


def test_backup_prunes_to_ten(tmp_path: Path) -> None:
    old = [f"sentinel-dev/202610{day:02d}T000000Z.dump" for day in range(1, 13)]
    run = _backup(tmp_path, env=_blobs(tmp_path, old))
    assert run.returncode == 0, run.stdout + run.stderr
    deleted = [_flag(line, "-n") for line in _calls(run, "az storage blob delete")]
    assert sorted(deleted) == old[:3]
    assert run.uploaded[0] not in deleted


def test_backup_pg_dump_failure_blocks(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_PGDUMP_RC": "1"})
    assert run.returncode != 0
    assert not _calls(run, "az storage blob upload")
    assert "::error::memory-backup" in run.stdout


def test_backup_upload_failure_blocks(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_UPLOAD_RC": "1"})
    assert run.returncode != 0
    assert "::error::memory-backup" in run.stdout
    assert "BACKED_UP" not in run.stdout


def test_backup_missing_container_blocks(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_CONTAINER_RC": "3"})
    assert run.returncode != 0
    assert "bootstrap-state.sh" in run.stdout
    assert not _calls(run, "pg_dump ")


def test_backup_no_database_skips(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_HOST": "", "STUB_DB": ""})
    assert run.returncode == 0, run.stdout + run.stderr
    assert not _calls(run, "pg_dump ")
    assert not _calls(run, "az storage blob upload")
    assert "no database" in run.stdout


def test_backup_missing_workspace_skips(tmp_path: Path) -> None:
    # Absent from a list that SUCCEEDED: the only workspace skip.
    run = _backup(tmp_path, env={"STUB_WORKSPACES": r"  default\n* other-dev\n"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "sentinel-dev: no such workspace; skipped" in run.stdout
    assert not _calls(run, "terraform workspace select")
    assert not _calls(run, "pg_dump ")


def test_backup_workspace_list_failure_blocks(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_LIST_RC": "1"})
    assert run.returncode != 0
    assert "::error::memory-backup: terraform workspace list failed" in run.stdout
    assert "AuthorizationFailed" in run.stdout
    assert "skipped" not in run.stdout
    assert not _calls(run, "terraform workspace select")
    assert not _calls(run, "pg_dump ")


def test_backup_all_scope_list_failure_blocks(tmp_path: Path) -> None:
    run = _run(BACKUP, tmp_path, ["--scope", "all"], {"STUB_LIST_RC": "1"})
    assert run.returncode != 0
    assert "::error::memory-backup: terraform workspace list failed" in run.stdout
    assert not _calls(run, "pg_dump ")


def test_backup_select_failure_blocks(tmp_path: Path) -> None:
    # The list shows the workspace, so a failed select is an error (a lock), never a skip.
    run = _backup(tmp_path, env={"STUB_SELECT_FAIL": "sentinel-dev"})
    assert run.returncode != 0
    assert "::error::memory-backup: sentinel-dev: terraform workspace select failed" in run.stdout
    assert "state lock" in run.stdout
    assert not _calls(run, "pg_dump ")


def test_backup_output_not_found_skips(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_OUTPUT_MISSING": "database_host database_name"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "sentinel-dev: no database; skipped" in run.stdout
    assert not _calls(run, "pg_dump ")


def test_backup_output_error_blocks(tmp_path: Path) -> None:
    run = _backup(tmp_path, env={"STUB_OUTPUT_ERROR": "database_name"})
    assert run.returncode != 0
    assert (
        "::error::memory-backup: sentinel-dev: terraform output database_name failed" in run.stdout
    )
    assert "state lock" in run.stdout
    assert "skipped" not in run.stdout
    assert not _calls(run, "pg_dump ")
    assert not _calls(run, "az storage blob upload")


def test_backup_all_scope_select_failure_blocks(tmp_path: Path) -> None:
    workspaces = r"  default\n  platform\n* a-dev\n  b-prod\n"
    env = {"STUB_WORKSPACES": workspaces, "STUB_SELECT_FAIL": "b-prod"}
    run = _run(BACKUP, tmp_path, ["--scope", "all"], env)
    assert run.returncode != 0
    assert "::error::memory-backup: b-prod: terraform workspace select failed" in run.stdout


def test_backup_never_discards_terraform_errors() -> None:
    text = BACKUP.read_text(encoding="utf-8")
    tf_lines = [line for line in text.splitlines() if "terraform " in line and "#" not in line]
    assert tf_lines
    for line in tf_lines:
        assert "2>/dev/null" not in line and "|| true" not in line, line


def test_backup_all_scope_covers_every_workspace(tmp_path: Path) -> None:
    workspaces = r"  default\n  platform\n* a-dev\n  b-prod\n"
    run = _run(BACKUP, tmp_path, ["--scope", "all"], {"STUB_WORKSPACES": workspaces})
    assert run.returncode == 0, run.stdout + run.stderr
    assert len(_calls(run, "pg_dump ")) == 2
    prefixes = sorted(name.split("/")[0] for name in run.uploaded)
    assert prefixes == ["a-dev", "b-prod"]
    selected = [line.split()[-1] for line in _calls(run, "terraform workspace select")]
    assert selected == ["a-dev", "b-prod"]


def test_backup_prune_failure_warns_only(tmp_path: Path) -> None:
    old = [f"sentinel-dev/202610{day:02d}T000000Z.dump" for day in range(1, 13)]
    run = _backup(tmp_path, env={**_blobs(tmp_path, old), "STUB_DELETE_RC": "1"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "::warning::" in run.stdout
    assert "BACKED_UP sentinel-dev" in run.stdout


def test_backup_never_prints_token(tmp_path: Path) -> None:
    run = _backup(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert TOKEN not in run.stderr
    leaks = [line for line in run.stdout.splitlines() if TOKEN in line]
    assert leaks == [f"::add-mask::{TOKEN}"]


# --- memory-restore.sh ---


def test_restore_no_dump_skips(tmp_path: Path) -> None:
    run = _restore(tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "no dump" in run.stdout
    assert not _calls(run, "psql ")
    assert not _calls(run, "az storage blob download")


def test_restore_missing_schema_skips(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path, ["sentinel-dev/20261001T000000Z.dump"])
    run = _restore(tmp_path, env={**blobs, "STUB_MIGRATED": "f"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "alembic upgrade head" in run.stdout
    assert "grant-db-access.sh --deployment sentinel --environment dev" in run.stdout
    assert not _calls(run, "az storage blob download")
    assert not _calls(run, "pg_restore ")


def test_restore_nonempty_incidents_skips(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path, ["sentinel-dev/20261001T000000Z.dump"])
    run = _restore(tmp_path, env={**blobs, "STUB_ROWS": "5"})
    assert run.returncode == 0, run.stdout + run.stderr
    assert "incidents has 5 rows" in run.stdout
    assert not _calls(run, "az storage blob download")
    assert not _calls(run, "pg_restore ")


def test_restore_restores_latest(tmp_path: Path) -> None:
    names = [
        "sentinel-dev/20261001T000000Z.dump",
        "sentinel-dev/20261020T000000Z.dump",
        "sentinel-dev/20261010T000000Z.dump",
    ]
    run = _restore(tmp_path, env=_blobs(tmp_path, names))
    assert run.returncode == 0, run.stdout + run.stderr
    downloads = _calls(run, "az storage blob download")
    assert len(downloads) == 1
    assert _flag(downloads[0], "-n") == "sentinel-dev/20261020T000000Z.dump"
    restores = [line for line in _calls(run, "pg_restore ") if "--list" not in line]
    assert len(restores) == 1
    assert "--data-only -L" in restores[0] and " -f " in restores[0]
    kept = Path(f"{tmp_path / 'stub.log'}.list").read_text()
    assert "TABLE DATA public incidents" in kept
    assert "TABLE DATA public deployments" in kept
    for skipped in ("alembic_version", " services ", "incident_checkpoints", "cache_entries"):
        assert skipped not in kept, skipped
    # The data and the close of the restored placeholders: one psql, one transaction.
    (apply,) = [line for line in _calls(run, "psql ") if "UPDATE" in line]
    assert "--single-transaction" in apply and "ON_ERROR_STOP=1" in apply
    assert _flag(apply, "-U") == "gha-deploy"
    assert apply.index(" -f ") < apply.index(" -c ")  # the rows first, then the close
    assert (
        "UPDATE public.incidents SET status = 'escalated', resolution = 'restored from backup' "
        "WHERE status = 'in_progress'"
    ) in apply
    assert "COPY public.incidents" in Path(f"{tmp_path / 'stub.log'}.sql").read_text()
    assert "RESTORED sentinel-dev sentinel-dev/20261020T000000Z.dump" in run.stdout


def test_restore_failure_exits_nonzero(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path, ["sentinel-dev/20261001T000000Z.dump"])
    run = _restore(tmp_path, env={**blobs, "STUB_RESTORE_RC": "1"})
    assert run.returncode != 0
    assert "::error::memory-restore: sentinel-dev: pg_restore could not render" in run.stdout
    assert not [line for line in _calls(run, "psql ") if "UPDATE" in line]
    assert "RESTORED" not in run.stdout


def test_restore_apply_failure_exits_nonzero(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path, ["sentinel-dev/20261001T000000Z.dump"])
    run = _restore(tmp_path, env={**blobs, "STUB_PSQL_RC": "3"})
    assert run.returncode != 0
    assert (
        "::error::memory-restore: sentinel-dev: restore failed (nothing was written" in run.stdout
    )
    assert "RESTORED" not in run.stdout


def test_restore_never_prints_token(tmp_path: Path) -> None:
    blobs = _blobs(tmp_path, ["sentinel-dev/20261001T000000Z.dump"])
    run = _restore(tmp_path, env=blobs)
    assert run.returncode == 0, run.stdout + run.stderr
    assert TOKEN not in run.stderr
    assert [line for line in run.stdout.splitlines() if TOKEN in line] == [f"::add-mask::{TOKEN}"]


# --- grant-db-access.sh and lint ---


def test_grant_script_static() -> None:
    _need("bash")
    text = GRANT.read_text(encoding="utf-8")
    for needle in (
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES",
        "incident_checkpoints",
        "cache_entries",
        "-backend",
        "pgaadauth_create_principal_with_oid",
    ):
        assert needle in text, needle
    proc = subprocess.run(
        ["bash", "-n", str(GRANT)], capture_output=True, text=True, timeout=30, check=False
    )
    assert proc.returncode == 0, proc.stderr


def test_grant_deploy_role_scoped_to_one_database() -> None:
    """gha-deploy: this database only (decision 2026-10-25, amended 2026-10-28)."""
    text = GRANT.read_text(encoding="utf-8")
    deploy = text[text.index("# ── 4. gha-deploy") : text.index("# ── 5. The backend runtime role")]
    sql = "\n".join(
        line.strip() for line in deploy.splitlines() if not line.lstrip().startswith("#")
    )
    for grant in (
        'GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA public TO :"role";',
        'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"role";',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT ON TABLES TO :"role";',
        'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"role";',
        'GRANT UPDATE (status, resolution) ON public.incidents TO :"role";',
        'REVOKE pg_read_all_data, pg_write_all_data FROM :"role";',
    ):
        assert grant in sql, grant
    # The server-wide roles are only ever revoked, never granted.
    assert not re.search(r"^GRANT pg_(read|write)_all_data", sql, re.MULTILINE)
    assert 'grant_connect_usage "${DEPLOY_ROLE}"' in deploy  # CONNECT on PG_DATABASE only
    assert "GRANT CONNECT" not in sql
    # Read back: the memberships are gone, no CONNECT grant on another database, per-table
    # SELECT / INSERT, the default privileges, and UPDATE limited to the two columns.
    for check in (
        "pg_has_role('${DEPLOY_ROLE}', '${member}', 'MEMBER')\")\" = \"f\"",
        "d.datname <> current_database() AND a.privilege_type = 'CONNECT'",
        "has_table_privilege('${DEPLOY_ROLE}', '${t}', '${priv}')",
        "has_sequence_privilege('${DEPLOY_ROLE}', '${seq}', '${priv}')",
        "a.privilege_type IN ('SELECT','INSERT')",
        "has_column_privilege('${DEPLOY_ROLE}', 'public.incidents', '${col}', 'UPDATE')",
        "has_table_privilege('${DEPLOY_ROLE}', 'public.incidents', 'UPDATE')\")\" = \"f\"",
    ):
        assert check in deploy, check
    assert "6252a75" in text


def test_scripts_shellcheck_clean() -> None:
    _need("shellcheck")
    proc = subprocess.run(
        ["shellcheck", str(BACKUP), str(RESTORE), str(GRANT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
