# task-1 — `ci_demo_prs.yml` + 14-PR scenario templates (A/B/C)   ·   [category-2-sentinel-deployment / phase-3-demo-scenarios]

| Field | Value |
|-------|-------|
| **Status** | `not-started` |
| **Repo** | `Sentinel-deployment` |
| **Phase branch** | `impl/deploy-phase-3-demo-scenarios` |
| **Commit prefix** | `feat:` |
| **Arch refs** | sentinel-deployment/ARCHITECTURE.md §4 (taxonomy + 14-PR table) |
| **Depends on** | [[task-1-fastapi-app]], [[task-2-ci-app-deployment]] |
| **Referenced by** | demo runs feeding backend incident pipeline |

## Spec
Self-contained demo-PR generator — **no backend involvement** (§4). Each scenario carries
scripted file changes + a pre-written realistic title/description; the workflow applies them
on a branch and opens the PR.

**Files created:**
- `.github/workflows/ci_demo_prs.yml` — `workflow_dispatch` with a scenario input/matrix; for the chosen scenario: create branch, apply the scripted diff, `gh pr create` with the template title/body.
- `demo-scenarios/` — one entry per PR #1–14 (§4.1 table): the file change(s) + title + description + expected Datadog signal + condition (A/B/C). Notable: #3 bad requirements (C), #5 health 503 (C), #7 slow startup (C), #9 wrong version (C), #11 `GET /` 500 with verify passing (**B**), #13 delayed `/health` degradation (**B**), even-numbered fixes (A).

## Prerequisites
- [ ] actionlint. [ ] task 1.1 app to mutate. [ ] `gh` + repo write for live PR creation.

## Acceptance Criteria
- [ ] Workflow validates; opens a real PR from a chosen scenario with the scripted change + pre-written text.
- [ ] All 14 scenarios encoded with correct A/B/C classification; B scenarios (#11/#13) pass verify but break at runtime.
- [ ] No backend/`SENTINEL_API_URL` reference; not in any concurrency group.

## Tests
- **Lint:** actionlint, yamllint; validate each scenario's diff applies cleanly.
- **Integration:** dispatch scenario #3 → a PR opens that, when merged, yields `deploy_status:failed failed_stage:deploy`; dispatch #11 → merges green but breaks `GET /` at runtime.
- **Quality gate:** `--repo deployment`.

## How to Verify (phase gate)
1. actionlint clean; a dry scenario apply produces the expected diff.
2. Dispatch one A, one C (#3/#5), one B (#11) → PRs open with realistic text; merging reproduces the documented Datadog signal.

## Report   ·   _filled on completion_
_not yet implemented_

## BLOCKED
_Live PR creation needs repo write; end-to-end signal needs Category-2 phase-2 wired. Workflow + scenarios writable now._
