# task-2 — App tests (health / version / root)   ·   [category-2-sentinel-deployment / phase-1-app]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `dev/deploy-phase-1-app` |
| **Commit prefix** | `test:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §2.1, §5 |
| **Depends on** | [[task-1-fastapi-app]] |
| **Referenced by** | [[task-2-ci-app-deployment]] (CI runs these) |

## Spec
Endpoint tests using FastAPI `TestClient`.

**Files created:** `tests/__init__.py`, `tests/test_app.py`
- `test_root` — 200, body `{"message":"ok","service":"dummy-api"}`.
- `test_health` — 200, `status == "ok"`, `uptime_seconds` is an int ≥ 0.
- `test_version` — 200, `version` matches configured `app_version`, `service == "dummy-api"`.
- `test_startup_log` (optional) — capture the startup log line shape.
- Add `pytest` (+`httpx`) to a dev-requirements or `requirements-dev.txt`.

## Prerequisites
- [ ] task 1.1 app exists. [ ] pytest installed.

## Acceptance Criteria
- [ ] `pytest tests/ -x` green; covers all three endpoints + response shapes.
- [ ] Tests import the app without hitting the network (TestClient).

## Tests
- **Unit:** the file itself. **Quality gate:** `--repo deployment` (ruff + pytest + actionlint on any workflows present).

## How to Verify (phase gate)
1. `pytest tests/ -q` → all pass.
2. `python scripts/quality_gate.py --repo deployment --path <repo>` → PASS.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_none — fully local._
