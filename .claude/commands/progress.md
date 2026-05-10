---
description: Show current project progress from TODO.md
allowed-tools: Read, Bash
model: haiku
---

Read TODO.md and give me a progress report:

1. Count total tasks and completed tasks (lines with `[x]` vs `[ ]`)
2. Calculate overall percentage complete
3. Identify the current phase (the phase with incomplete tasks that comes first)
4. List the next 3 uncompleted tasks with their descriptions
5. Check if any tasks have Notes that mention blockers
6. Format as a clean summary:
   - Progress bar (visual: ████░░░░░░ 40%)
   - Current phase name
   - Next 3 tasks
   - Any blockers
