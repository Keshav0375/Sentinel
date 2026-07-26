# Sentinel Phase 2 — Implementation Tracker

> **This folder is the execution source of truth.** Planning/design lives in
> [`../Phase-2/`](../Phase-2/) (architecture, STATE, decision log). Here we track *building* it:
> what to build, in what order, whether it's done, and a per-task report of what shipped.
>
> Build driver: [`/implement-phase`](../../.claude/skills/implement-phase/SKILL.md) — one orchestrator
> that builds a whole phase and closes it with a human sign-off (replaces the former
> `/sentinel-planner`, `/sentinel-build`, and `/phase-gate`). Planning docs in [`../Phase-2/`](../Phase-2/) are edited directly.

---

## 1. How work is organized

```
Category  →  Phase (= 1 branch + 1 PR)  →  Task (= 1 commit, PR-sized)
```

- **3 categories**, one per repo, implemented **in dependency order**:
  `sentinel-infra` → `sentinel-deployment` → `sentinel-backend`.
  Infra first so deployment and backend build on real, provisioned ground truth.
- **16 phases** total. **Each phase is one git branch and one pull request.**
- **~58 tasks** total (rev-5 added infra 3.5/3.6 + backend 5.6). Each task is one PR-sized
  commit on its phase branch, ships **unit + integration tests**, and must pass the category
  **quality gate** (§4).

The master checklist is [TODO.md](TODO.md) — headings + status only. The full technical
detail for each task lives in its own file (§2).

## 2. Folder map

| Path | What it is |
|------|-----------|
| [TODO.md](TODO.md) | Master tracker — every phase & task as `heading · file · status`. Lean by design. |
| [STATE-IMPL.md](STATE-IMPL.md) | Live execution state: current branch/PR, phase-gate ledger, active blockers. |
| [_templates/task-template.md](_templates/task-template.md) | Schema every task file follows (spec + report + blocked). |
| `category-1-sentinel-infra/` | Infra tasks. [Index](category-1-sentinel-infra/README.md) · 4 phases. |
| `category-2-sentinel-deployment/` | Deployment tasks. [Index](category-2-sentinel-deployment/README.md) · 3 phases. |
| `category-3-sentinel-backend/` | Backend tasks. [Index](category-3-sentinel-backend/README.md) · 9 phases. |

Each `category-N-*/` has its own `README.md` indexing its phases and tasks, and one
`phase-M-*/` folder per phase holding `task-K-*.md` files.

## 3. Where the code lives (local + remote)

| Category | Local working dir | GitHub | Notes |
|----------|-------------------|--------|-------|
| **sentinel-infra** | `Agentic-Engineering/Sentinel-development-project/Sentinel-infra` | `Keshav0375/Sentinel-infra` | Terraform IaC. Currently bare (`.gitignore`, `LICENSE`, `README.md`). |
| **sentinel-deployment** | `Agentic-Engineering/Sentinel-development-project/Sentinel-deployment` | `Keshav0375/Sentinel-deployment` | Dummy FastAPI app + deploy pipeline. Bare. |
| **sentinel-backend** | `Agentic-Engineering/Sentinel` (this repo) | `Keshav0375/Sentinel` | The backend IS the sentinel repo. Has Phase-1 code to migrate. |

> ✅ **Owner/repo identity (reconciled 2026-07-11):** owner is **`Keshav0375`**, repos are
> `Sentinel-infra` / `Sentinel-deployment` / `Sentinel` (capitalized). The architecture docs
> were updated from the old `keshxvDev` placeholder — OIDC `sub` claims are exact
> case-sensitive matches, so use these values verbatim. History in [STATE-IMPL.md](STATE-IMPL.md).

## 4. Architecture reference (read before building a task)

Every task cites the section it implements. Never deviate from architecture without asking.
**Start at the index** for the whole picture, then follow its §4 map to the per-repo file your
task's **Arch refs** name — the per-repo files are authoritative for detail.

| Doc | Scope |
|-----|-------|
| [../Phase-2/ARCHITECTURE.md](../Phase-2/ARCHITECTURE.md) | **Architecture Index — start here.** Whole picture in diagrams + a concern→file §4 map. Routes to the deep-dive files below. |
| [../Phase-2/sentinel-infra/ARCHITECTURE.md](../Phase-2/sentinel-infra/ARCHITECTURE.md) | 7 Terraform modules + identity plane, Entra DB auth, KV rotation, workload identity, `ci_destroy_infra`, cross-repo secrets, CI. |
| [../Phase-2/sentinel-deployment/ARCHITECTURE.md](../Phase-2/sentinel-deployment/ARCHITECTURE.md) | Deploy pipeline stages, Datadog schema, **30 scenario branches (3 cases)**. |
| [../Phase-2/sentinel/ARCHITECTURE.md](../Phase-2/sentinel/ARCHITECTURE.md) | Agents, tools, memory, API (+ Entra bearer §3.6, signal_type two-case), DB schema, K8s (workload identity), workflows, Phase-1 cleanup. |
| [../Phase-2/STATE.md](../Phase-2/STATE.md) | Planning decision log + open blockers. |

> ⚠ **rev-5 (2026-07-12) supersedes several existing task bodies — read the arch first.**
> The TODO row descriptions are current, but some task-file *bodies* predate the security +
> ground-truth overhaul. When building these, follow the cited arch section, not the stale body:
>
> | Task file | Superseded by |
> |-----------|---------------|
> | infra 1.1 repo-skeleton | no `db_password` variable — Postgres is Entra-only (infra §3.2) |
> | infra 2.2 postgresql-module | Entra-only auth + admin group (infra §3.2) — no `db-password`/password auth |
> | infra 2.3 keyvault-module | secret inventory minus db-password/api-token; 4 RBAC roles (infra §3.3) |
> | infra 3.1 aks-module | + workload identity (OIDC issuer, backend UAMI, federated cred) (infra §3.7) |
> | infra 3.2 event-grid / 3.3 functions-bridge | two-signal routing + bridge stamps `signal_type` (infra §3.4/§3.5) |
> | infra 4.1 cross-repo-secrets | variables + `SENTINEL_API_AUDIENCE`, no `DB_PASSWORD` (infra §5) |
> | infra 4.3 infra-workflows | add `ci_destroy_infra.yml` (infra §7.3); no `db_password` workflow var |
> | deploy 2.2 ci-app-deployment | record stage uses Entra DB token, not `db-password` (deploy §3 Stage 5) |
> | backend 5.1 webhook-receiver | `signal_type` two-case handling (sentinel §3.1); auth = Entra bearer via task 5.6, **not** `X-Sentinel-Token` (sentinel §3.6) |
> | backend 5.5 app-lifespan | workload-identity KV/DB wiring; auth moved to 5.6 |
> | backend 7.2 k8s-manifests | ServiceAccount + workload-identity label; ConfigMap not Secret (sentinel §8.2) |
> | backend 7.3 composite-actions | + `get-db-token`, `get-backend-token`; `psql-exec` takes a token (sentinel §9) |
> | backend 6.2 eval-runner | scores against 30 branches / `branches.yaml`, not synthetic JSON (sentinel §13.2) |
>
> New rev-5 tasks: infra [3.5 backend-entra-app](category-1-sentinel-infra/phase-3-compute-modules/task-5-backend-entra-app.md),
> infra [3.6 keyvault-rotation](category-1-sentinel-infra/phase-3-compute-modules/task-6-keyvault-rotation.md),
> backend [5.6 entra-bearer-auth](category-3-sentinel-backend/phase-5-api/task-6-entra-bearer-auth.md).

## 5. The build loop (per phase)

Driven by `/implement-phase` (see its SKILL for the authoritative steps). One run builds a
whole phase; within it, each task moves through:

1. **Locate + context** — resolve the active phase (category order + phase-gate locks), rebuild
   full context via the `phase-context-builder` subagent, and distill the architecture contract
   via `architecture-warden` (distill mode).
2. **Goal + branch** — one todo per task; branch `dev/<cat>-phase-<M>-<slug>` fresh from
   `release-phase-2` (§6).
3. **Prereqs** — per task, verify tools/accounts/keys/upstream tasks **and that no open
   Reconciliation (R-item) in [STATE-IMPL.md](STATE-IMPL.md) applies to this task**. Missing →
   write a **BLOCKED** report into the task file + STATE-IMPL, and **halt**. Never default an
   open R-item (e.g. don't silently pick a region).
4. **Implement** in the correct repo, per the task's Spec + the architecture contract.
5. **Test + gate** — add unit + integration tests, loop
   `python scripts/quality_gate.py --repo <name> --path <repo>` to green (§7).
6. **Commit** one task = one commit (conventional prefix, **no Claude attribution**).
7. **Report** — fill the task file's Report/Tests/How-to-Verify; flip status; update TODO + STATE-IMPL.
8. **Phase review** — when all tasks are green, `architecture-warden` (review) + `code-reviewer` +
   `safety-reviewer` (backend) run over the phase diff; resolve blockers.
9. **Close** — open the PR **into `release-phase-2`**, present the "see it working" checklist,
   and ask the user to sign off (§6).

## 6. Git model — one branch + one PR per phase

```
release-phase-2  ──┬──►  dev/<cat>-phase-<M>-<slug>  ──PR──►  release-phase-2
                   │            (one branch + one PR per phase)
                   └──────────────────────────────────────────►  main
                            (final, once ALL 16 phases are merged)
```

- **`release-phase-2` is the integration branch in all three repos.** A phase branches
  fresh from it, never from `main`:
  `git checkout release-phase-2 && git pull && git checkout -b dev/<cat>-phase-<M>-<slug>`.
- The prefix is **`dev/`**. `Sentinel`'s [`ci.yml`](../../.github/workflows/ci.yml)
  branch-convention check only accepts `dev|feat|fix|refactor|ci|docs|test|chore|planning|ai|hotfix`.
- Each task in the phase is a **separate commit** on that branch (prefixes: `feat/fix/refactor/test/docs`).
- **No Claude as contributor** — commits and the PR carry no `Co-Authored-By` / "generated with" attribution.
- When every task in the phase is `done-pending-review` and green, `/implement-phase` closes it:
  opens the **PR into `release-phase-2`**, produces a **verification checklist** ("here's how to
  see it working"), and asks the user to confirm.
- On **human sign-off**, the PR **merges to `release-phase-2`**; the branch is deleted; the phase
  is marked `verified` in TODO/STATE-IMPL. The next phase branches from the updated `release-phase-2`.
- Until sign-off, the next phase stays **locked**. This is the human-review gate.
- **`release-phase-2` → `main` happens once**, at the end of Phase 2, after the planning docs
  are removed. The user drives that merge; `/implement-phase` never does.

> Enforced by [`guard-main-source.yml`](../../.github/workflows/guard-main-source.yml) in the
> `Sentinel` repo: `release-phase-2 <- dev/* | planning/phase-2-e2e` and `main <- release-phase-2`.
> **A PR from `dev/*` straight to `main` is rejected.**

### Where the tracker commits live

Code branches/PRs open in each **target repo**. TODO.md, STATE-IMPL.md and the task files
live in `Sentinel`, so an **infra or deployment** phase produces **two branches and two PRs** —
both based on `release-phase-2`, both closed at the same phase gate:

| Phase | Code branch | Tracker branch |
|-------|-------------|----------------|
| infra / deployment | `dev/<cat>-phase-<M>-<slug>` in the sibling repo | `dev/<cat>-phase-<M>-<slug>` in `Sentinel` (docs only) |
| backend | `dev/backend-phase-<M>-<slug>` in `Sentinel` — code + tracker in one branch | — |

`planning/phase-2-e2e` is for **plans, architecture, and tracker restructuring only** — never
for phase implementation work.

## 7. Quality gate (reusable in CI)

`scripts/quality_gate.py --repo {infra|deployment|backend}` runs the right toolchain per
repo type and is the exact body CI jobs call:

| Repo | Lint / Format | Types / Validate | Secrets / Security | Tests |
|------|---------------|------------------|--------------------|-------|
| infra | `terraform fmt -check`, `tflint` | `terraform init -backend=false` → `terraform validate` | `tfsec`/`checkov`, `gitleaks` | plan-assert / `terratest` |
| deployment | `ruff`, `actionlint`, `yamllint` | — | `gitleaks` | `pytest tests/` |
| backend | `ruff` check + format | `pyright` | `gitleaks`, `pip-audit` | `pytest` unit + integration (see below) |

**Backend test coverage — keep this in sync.** The gate's two pytest checks must name every
`tests/` package a task file uses, or a phase reports green with its own tests never run:

| Check | Packages | Notes |
|-------|----------|-------|
| `pytest-unit` | `test_models/` · `test_tools/` · `test_providers/` · `test_config.py` | no external service |
| `pytest-integration` | `test_infra/` · `test_memory/` · `test_agents/` · `test_api/` · `test_eval/` | needs pgvector / fake-LLM pipeline; skipped by `--fast` |

Adding a new `tests/` package in a task ⇒ add it to
[`scripts/quality_gate.py`](../../scripts/quality_gate.py) `MATRIX` in the same commit.
Paths that don't exist yet are pruned automatically, so the gate is safe to run mid-build.

## 8. Blockers & standards halt

If a prerequisite is missing (unprovisioned Azure resource, absent account/key, a tool not
installed, an upstream task not `verified`), the build **stops**: a `BLOCKED` section is
written to the task file and mirrored to [STATE-IMPL.md](STATE-IMPL.md#blockers). Nothing
downstream proceeds until the user clears it. This is deliberate — we never build on a
missing foundation.

## 9. Status legend

`not-started` · `in-progress` · `blocked` · `done-pending-review` (built + green, awaiting
the end-of-phase gate) · `verified` (user signed off, merged).
