# Sentinel Phase 2 — Implementation Tracker

> **This folder is the execution source of truth.** Planning/design lives in
> [`../Phase-2/`](../Phase-2/) (architecture, STATE, decision log). Here we track *building* it:
> what to build, in what order, whether it's done, and a per-task report of what shipped.
>
> Design-time agent: [`/sentinel-planner`](../../.claude/skills/sentinel-planner/SKILL.md).
> Build-time agents: [`/sentinel-build`](../../.claude/skills/sentinel-build/SKILL.md) + [`/phase-gate`](../../.claude/skills/phase-gate/SKILL.md).

---

## 1. How work is organized

```
Category  →  Phase (= 1 branch + 1 PR)  →  Task (= 1 commit, PR-sized)
```

- **3 categories**, one per repo, implemented **in dependency order**:
  `sentinel-infra` → `sentinel-deployment` → `sentinel-backend`.
  Infra first so deployment and backend build on real, provisioned ground truth.
- **16 phases** total. **Each phase is one git branch and one pull request.**
- **~55 tasks** total. Each task is one PR-sized commit on its phase branch, ships
  **unit + integration tests**, and must pass the category **quality gate** (§4).

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

| Doc | Scope |
|-----|-------|
| [../Phase-2/ARCHITECTURE.md](../Phase-2/ARCHITECTURE.md) | Master — three-repo system, end-to-end flow, cost. |
| [../Phase-2/sentinel-infra/ARCHITECTURE.md](../Phase-2/sentinel-infra/ARCHITECTURE.md) | 7 Terraform modules, OIDC, cross-repo secrets, CI. |
| [../Phase-2/sentinel-deployment/ARCHITECTURE.md](../Phase-2/sentinel-deployment/ARCHITECTURE.md) | Deploy pipeline stages, Datadog schema, 14-PR demo taxonomy. |
| [../Phase-2/sentinel/ARCHITECTURE.md](../Phase-2/sentinel/ARCHITECTURE.md) | Agents, tools, memory, API, DB schema, K8s, workflows, Phase-1 cleanup. |
| [../Phase-2/STATE.md](../Phase-2/STATE.md) | Planning decision log + open blockers. |

## 5. The build loop (per task)

Driven by `/sentinel-build` (see its SKILL for the authoritative steps):

1. **Pick** the next unblocked task honoring category order + phase-gate locks.
2. **Header** — print a compact summary: category/phase/task, one-line goal, files, arch refs, deps.
3. **Prereqs** — verify tools/accounts/keys/upstream tasks. Missing → write a **BLOCKED**
   report into the task file + [STATE-IMPL.md](STATE-IMPL.md), and **halt**.
4. **Branch** — if starting a phase, `git pull` main then create the phase branch (§6).
5. **Implement** in the correct repo, per the task's Spec + the architecture section.
6. **Test + gate** — add unit + integration tests, run `scripts/quality_gate.py --repo <name>`.
7. **Review** — `architecture-conformance` + `safety-reviewer` subagents on the diff.
8. **Commit** one task = one commit (conventional prefix, **no Claude attribution**).
9. **Report** — fill the task file's Report/Tests/How-to-Verify; flip status; update TODO + STATE-IMPL.
10. **Phase end** → hand to `/phase-gate` (§6).

## 6. Git model — one branch + one PR per phase

- A phase branches **fresh from updated `main`**: `git checkout main && git pull && git checkout -b impl/<cat>-phase-<M>-<slug>`.
- Each task in the phase is a **separate commit** on that branch (prefixes: `feat/fix/refactor/test/docs`).
- **No Claude as contributor** — commits and the PR carry no `Co-Authored-By` / "generated with" attribution.
- When every task in the phase is `done-pending-review` and green, `/phase-gate`:
  opens the **PR**, produces a **verification checklist** ("here's how to see it working"),
  and asks the user to confirm.
- On **human sign-off**, the PR **merges to main**; the branch is deleted; the phase is
  marked `verified` in TODO/STATE-IMPL. The next phase branches from the updated main.
- Until sign-off, the next phase stays **locked**. This is the human-review gate.

> Code branches/PRs open in each **target repo** (infra/deployment/backend). The tracking
> docs in this folder are updated in the `Sentinel` repo alongside the work.

## 7. Quality gate (reusable in CI)

`scripts/quality_gate.py --repo {infra|deployment|backend}` runs the right toolchain per
repo type and is the exact body CI jobs call:

| Repo | Lint / Format | Types / Validate | Secrets / Security | Tests |
|------|---------------|------------------|--------------------|-------|
| infra | `terraform fmt -check`, `tflint` | `terraform validate` | `tfsec`/`checkov`, `gitleaks` | plan-assert / `terratest` |
| deployment | `ruff`, `actionlint`, `yamllint` | — | `gitleaks` | `pytest` |
| backend | `ruff` | `pyright` | `gitleaks`, `pip-audit` | `pytest` unit + integration |

## 8. Blockers & standards halt

If a prerequisite is missing (unprovisioned Azure resource, absent account/key, a tool not
installed, an upstream task not `verified`), the build **stops**: a `BLOCKED` section is
written to the task file and mirrored to [STATE-IMPL.md](STATE-IMPL.md#blockers). Nothing
downstream proceeds until the user clears it. This is deliberate — we never build on a
missing foundation.

## 9. Status legend

`not-started` · `in-progress` · `blocked` · `done-pending-review` (built + green, awaiting
phase gate) · `verified` (phase-gate signed off, merged).
