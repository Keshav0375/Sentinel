# task-2 — PostgreSQL module (+ pgvector, firewall)   ·   [category-1-sentinel-infra / phase-2-core-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-2-core-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §3.2 |
| **Depends on** | [[task-1-repo-skeleton-and-providers]] |
| **Referenced by** | [[task-3-keyvault-module]] (db-password), [[task-3-alembic-initial-schema]] (backend), [[task-2-ci-app-deployment]] (record-deployment) |

## Spec
Flexible Server B1MS + `sentinel` DB + pgvector extension + dev allow-all firewall.

**Files created:** `modules/postgresql/{main.tf,variables.tf,outputs.tf}`
- `azurerm_postgresql_flexible_server "sentinel"` — name `sentinel-pg`, version `16`,
  sku `B_Standard_B1ms`, storage `32768`, admin `sentinel_admin`, password `var.db_password`, zone `1`.
- `azurerm_postgresql_flexible_server_database "sentinel"` — charset UTF8, collation en_US.utf8.
- `azurerm_postgresql_flexible_server_configuration "pgvector"` — `azure.extensions = VECTOR`.
- `azurerm_postgresql_flexible_server_firewall_rule "allow_all_dev"` — 0.0.0.0–255.255.255.255 (dev; document rationale from §3.2).
- `variables.tf` — `resource_group_name`, `location`, `db_password` (sensitive).
- `outputs.tf` — `db_host`, `db_name`, `db_port`.

## Prerequisites
- [ ] terraform CLI. [ ] `db_password` chosen (⛔ B1 to apply).

## Acceptance Criteria
- [ ] Validates + fmt clean; `azure.extensions=VECTOR` present so backend can `CREATE EXTENSION vector`.
- [ ] Firewall rule documented as dev-only (comment cites §3.2 comparison table).
- [ ] Outputs expose host/name/port.

## Tests
- **Validate:** validate, tflint, tfsec (allow-all firewall = accepted dev risk, annotate).
- **Integration (⛔ B1):** apply, then `psql "host=sentinel-pg... sslmode=require"` connects; `CREATE EXTENSION vector` works.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform plan -target=module.postgresql` → server + db + config + firewall.
2. (post-apply) `psql` connect + `SELECT * FROM pg_available_extensions WHERE name='vector';` returns a row.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Apply/verify ⛔ B1. Code writable now._
