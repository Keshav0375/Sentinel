---
description: Pick a task from TODO.md and implement it end-to-end
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
model: sonnet
---

1. Read TODO.md and find the task matching: $ARGUMENTS
2. Read ARCHITECTURE.md for any relevant design details for this task
3. Implement the task following all coding standards from CLAUDE.md
4. After implementation, run `ruff check` on changed files and fix any issues
5. Run `pyright` on changed files and fix type errors
6. If the task involves a tool or agent, write a test in the appropriate test directory
7. Run `pytest -xvs` on the new/changed test files to verify
8. Update TODO.md: change `[ ]` to `[x]` for this task and add today's date + a one-line note under `Notes:`
9. Show me a summary of what was implemented and any decisions made
