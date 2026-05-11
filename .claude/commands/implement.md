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
9. Show a final implementation report:
   - **Files changed:** list every file that was created or modified
   - **Plain English summary:** explain what was built in simple story form —
     what problem it solves, what the code does step by step, and any key
     decisions or trade-offs made — as if explaining to someone non-technical
10. Save the implementation report from step 9 to:
    Story-Reports/{phase}-{name}.md
    where {phase} = the project phase or epic number (e.g. 1.1.1, 1.6, 2.5)
    and   {name}  = short snake_case descriptor of the task (e.g. login_flow, data_ingestion)
