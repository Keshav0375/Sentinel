---
description: Review uncommitted or recent changes for quality, patterns, and HITL safety
allowed-tools: Read, Bash, Grep, Glob
model: sonnet
---

Review the current changes in this repo:

1. Run `git diff --name-only` to see changed files. If no uncommitted changes, use `git diff HEAD~1 --name-only`.
2. For each changed file, check against CLAUDE.md coding standards:
   - Type hints on all functions?
   - Async where needed?
   - Pydantic models for cross-boundary data?
   - Proper error handling (no raw exceptions from tools)?
   - structlog usage (not print statements)?
   - Dependency injection (no hardcoded singletons)?
3. **HITL safety audit**: Grep for any function that could modify external state (write, send, post, push, merge, deploy, restart, rollback). Verify it goes through `request_human_approval`. Flag violations as CRITICAL.
4. Check test coverage: does every new tool/agent have a corresponding test file?
5. Check imports: no circular imports, no wildcard imports, proper grouping
6. Provide a summary with:
   - ✅ What looks good
   - ⚠️ Suggestions for improvement
   - 🚨 Critical issues (especially HITL violations)
