# Quality Gate & Tests

Every task ships **unit + integration tests** and must pass the category quality gate
before commit. One reusable entrypoint keeps local == CI:

```
python scripts/quality_gate.py --repo {infra|deployment|backend} --path <repo-dir>
```

Add `--fast` locally to skip slow/network checks (integration tests, pip-audit, tfsec,
gitleaks); CI runs the full set. Exit 0 = pass. Tools not installed → SKIPPED (warning),
never a silent pass in CI because CI images pin the toolchain.

## Matrix (mirrors README §7 — the script is source of truth)
| Repo | Lint / Format | Types / Validate | Secrets / Security | Tests |
|------|---------------|------------------|--------------------|-------|
| infra | `terraform fmt -check`, `tflint` | `terraform validate` | `tfsec`, `gitleaks` | plan-assert / terratest |
| deployment | `ruff`, `actionlint`, `yamllint` | — | `gitleaks` | `pytest` |
| backend | `ruff`, `ruff format --check` | `pyright src/` | `gitleaks`, `pip-audit` | `pytest` unit + integration |

## Test conventions
- **Backend:** unit = `tests/test_tools/`, `tests/test_models/`; integration =
  `tests/test_agents/`, `tests/test_api/`. Test file mirrors source
  (`src/sentinel/tools/log_fetcher.py` → `tests/test_tools/test_log_fetcher.py`). Local
  Postgres via `docker run pgvector/pgvector:pg16`; stub LLMs with `SENTINEL_FAKE_LLM=1`.
- **Deployment:** `tests/test_app.py` for the three endpoints; workflows linted by actionlint.
- **Infra:** `terraform validate` + `plan` assertions per module; a module isn't "tested"
  until it plans cleanly against the real (bootstrapped) backend — so infra test/verify is
  often gated on the Azure blockers (write the code, mark BLOCKED for verify).

## Phase-2 safety invariants (checked by review, enforced here)
The Phase-1 `safety-reviewer` predates the Phase-2 HITL redesign — apply these instead:
1. **No backend tool touches external state.** Per arch §3 Tools, all six tools are
   read-only or output-only. There is no "execute" tool; `comms_tools.py` and `hitl.py`
   are removed. GHA executes, backend reasons.
2. **HITL = the GitHub revert PR** (fire-and-forget). No `/approvals` endpoint, no PR
   lifecycle tracking, no `request_human_approval` tool.
3. **Tool-call budget = 20** per incident (raised from 15 for reflexion loops); enforced
   in the orchestrator, exhaustion → escalate.
4. **Judge is a separate model** from the agents it grades (no self-eval).
5. **All non-health endpoints require `X-Sentinel-Token`.**

If a backend task touches tools/agents/orchestrator/api, run `safety-reviewer` and check
these five explicitly. (Follow-up: refresh `.claude/agents/safety-reviewer.md` to Phase-2.)
