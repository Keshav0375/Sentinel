---
description: Run lint, type check, and tests — fix issues automatically
allowed-tools: Read, Write, Edit, Bash, Grep
model: haiku
---

Run the full quality pipeline and fix any issues:

1. Run `ruff check src/ tests/ --fix` — auto-fix what it can, report the rest
2. Run `ruff format src/ tests/` — format all code
3. Run `pyright src/` — report type errors. Fix any that are straightforward (missing annotations, wrong types). Flag complex ones for me to review.
4. Run `pytest -x --tb=short` — if tests fail, show the failure and attempt a fix. Re-run after fixing.
5. Print a summary: ✅ passed / ❌ failed for each step
