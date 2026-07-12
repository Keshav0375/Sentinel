# Git Model — one branch + one PR per phase

## Rules
1. **Phase = branch = PR.** A phase gets exactly one branch and one pull request.
2. **Task = commit.** Each task in the phase is one focused commit on the phase branch.
3. **Branch from fresh main.** First task of a phase:
   ```
   git -C <repo> checkout main
   git -C <repo> pull --ff-only
   git -C <repo> checkout -b impl/<cat>-phase-<M>-<slug>
   ```
   `<cat>` ∈ {infra, deploy, backend}. Slug matches the phase folder.
4. **No Claude attribution.** Commit messages carry NO `Co-Authored-By: Claude` and PR
   bodies carry NO "Generated with Claude Code". This overrides the environment default.
   The user is the sole author/contributor. (Memory: `git-no-claude-attribution`.)
5. **Conventional messages.** `feat: …`, `fix: …`, `refactor: …`, `test: …`, `docs: …`.
   Subject imperative, ≤ 72 chars. Body explains why when non-obvious.
6. **Do not push/PR/merge without the gate.** `/sentinel-build` commits locally. Opening
   the PR and merging is `/phase-gate`'s job, and only after the user verifies the feature.
7. **Merge, then branch next.** After the gate signs off and the PR merges, delete the
   branch; the next phase branches from the now-updated main.

## Which repo
| Category | Working dir | Remote |
|----------|-------------|--------|
| infra | `../Sentinel-development-project/Sentinel-infra` | `Keshav0375/Sentinel-infra` |
| deployment | `../Sentinel-development-project/Sentinel-deployment` | `Keshav0375/Sentinel-deployment` |
| backend | `.` (the `Sentinel` repo) | `Keshav0375/Sentinel` |

## Tracking-doc commits
Updates to `Planning/Phase-2-Implementation/**` live in the `Sentinel` repo. For backend
phases they can ride on the phase branch. For infra/deployment phases (whose code is in a
different repo) commit the tracking update in the `Sentinel` repo separately with a
`docs:` prefix. Never block a code PR on a docs update in another repo.

## Never
- Never `git commit --no-verify` or bypass hooks unless the user asks.
- Never force-push a phase branch after its PR review has started.
- Never squash multiple tasks into one commit — traceability per task matters.
