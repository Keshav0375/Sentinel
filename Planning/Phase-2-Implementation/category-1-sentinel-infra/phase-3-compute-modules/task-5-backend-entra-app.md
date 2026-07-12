# task-5 — Identity plane: backend Entra app + app role + role grant   ·   [category-1-sentinel-infra / phase-3-compute-modules]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-infra` |
| **Phase branch** | `impl/infra-phase-3-compute-modules` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-infra/ARCHITECTURE.md §4.4 (backend app reg) + §4.5 (token model) |
| **Depends on** | [[task-3-oidc-federation]] (needs the `sentinel-gha` SP) |
| **Referenced by** | backend inbound auth ([[task-6-entra-bearer-auth]]), incident workflow token |

> **rev-5 (2026-07-12):** new task. Registers the backend API as an Entra app so callers
> obtain tokens **scoped to it** and the backend validates against JWKS — deletes the shared
> `sentinel-api-token`.

## Spec
Terraform-manage the backend API app registration and grant the CI identity its role.

**Files created / changed:**
- `identity.tf` (or extend `oidc.tf`):
  - `azuread_application "sentinel_backend"` — `identifier_uris = ["api://sentinel-backend"]`,
    one `app_role` `Incident.Write` (`allowed_member_types = ["Application"]`, stable GUID `id`).
  - `azuread_service_principal "sentinel_backend"`.
  - `azuread_app_role_assignment "gha_incident_write"` — grants `Incident.Write` to the
    `sentinel-gha` SP (`resource_object_id` = backend SP, `principal_object_id` = gha SP).
- `outputs.tf`: `backend_api_audience = "api://sentinel-backend"` and `tenant_id` (pushed to the
  sentinel repo as **variables** by [[task-1-cross-repo-secrets]], not secrets).

**Contract:**
```
audience  = api://sentinel-backend
app role  = Incident.Write   (application permission)
grantee   = sentinel-gha service principal
validate  = https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys (backend side)
```

## Prerequisites
- [ ] `az`/AD rights to create app registrations + app-role assignments (⛔ B1/B3/B11).
- [ ] `sentinel-gha` SP exists (task 1.3).

## Acceptance Criteria
- [ ] `terraform validate`/`tflint` pass; app + SP + role assignment plan cleanly.
- [ ] `az account get-access-token --resource api://sentinel-backend` returns a JWT whose
      `aud=api://sentinel-backend` and `roles` includes `Incident.Write` for the gha SP.
- [ ] Audience + tenant exported for cross-repo variable push.

## Tests
- **Validate:** `terraform validate`, `tflint`.
- **Integration (blocked on B1/B3/B11):** mint a token from the gha SP → decode → assert aud + role.
- **Quality gate:** `--repo infra`.

## How to Verify (phase gate)
1. `terraform apply` creates the app reg + role + grant.
2. From a gha-authenticated context, mint an `api://sentinel-backend` token and decode it (jwt.io / `jwt` CLI) → `aud` + `roles` correct.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Verification BLOCKED on B1 + B3 + B11 (Azure + AD app-registration rights). Terraform writable now._
