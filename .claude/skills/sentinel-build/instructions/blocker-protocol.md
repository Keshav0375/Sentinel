# Blocker Protocol — halt on a missing prerequisite

The system never builds on a missing foundation. When a task's prerequisite is not met,
STOP and record it — do not partially implement, do not fake data, do not skip ahead.

## What counts as a blocker
- A required **tool** is not installed (terraform, az, kubectl, docker, gitleaks, …).
- A required **account/credential/resource** does not exist yet (Azure RG, Key Vault
  secret, Datadog key, LangFuse keys, Teams webhook, GitHub PAT) — cross-check
  `STATE-IMPL.md` Blockers (B1–B9).
- An **upstream task** the current one depends on is not `verified` (or not
  `done-pending-review` within the same phase).
- An **open reconciliation** (STATE-IMPL R1–R3) that changes the code to write is unresolved.

Note the distinction: many infra tasks can have their **code written** without Azure, but
their **verification** (plan/apply) is blocked. In that case, write the code, add tests
that can run offline (fmt/validate/tflint), and mark the *verification* BLOCKED — say
exactly which acceptance criteria are deferred.

## Steps
1. Fill the task file's **BLOCKED** section:
   ```
   ## BLOCKED
   - Missing: <what> (<exact tool/resource/secret/task>)
   - Symptom: <command + error, or "resource not provisioned">
   - Blocks: <which acceptance criteria / downstream tasks>
   - Resolver: <who> (usually Keshav) — <what they must do>
   - Detected: <date>
   ```
2. Set the task Status to `blocked` (⛔ in TODO.md).
3. Mirror a row into `STATE-IMPL.md` → Blockers (link the task; reuse an existing B# if it
   is the same external dependency).
4. In chat, tell the user plainly: what's blocked, why, the one action that unblocks it,
   and what (if anything) can proceed in the meantime.
5. STOP. Do not move to the next task if it shares the same blocker; if the next task is
   independent and unblocked, offer to proceed with it instead.

## Unblocking
When the user confirms the dependency is resolved: update the STATE-IMPL blocker to
`resolved` (move to Change Log), clear the task's BLOCKED section (leave a one-line note in
Report), set Status back to `in-progress`, and resume the loop from the prerequisite check.
