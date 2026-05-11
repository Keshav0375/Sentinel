"""draft_rollback_pr and draft_hotfix tools — remediation artifact generators."""

from __future__ import annotations

from agents import FunctionTool, function_tool


def make_remediation_tools() -> list[FunctionTool]:
    """Create both remediation draft tools.

    These tools produce artifacts (PR descriptions, hotfix diffs) but never
    execute any action directly. The artifacts are used to populate the HITL
    approval request, which is the only path to actual execution.

    Returns:
        List of [draft_rollback_pr, draft_hotfix] FunctionTools.
    """

    @function_tool
    async def draft_rollback_pr(deploy_id: str, justification: str) -> str:
        """Draft a pull request to roll back a specific deploy.

        Produces a PR title and body that a human can review and merge.
        Does NOT open the PR — only drafts it. Always follow this with
        request_human_approval before any further action.

        Args:
            deploy_id: ID of the deploy to roll back (e.g. "deploy-abc123").
            justification: Why this deploy is suspected as the root cause.
        """
        return _draft_rollback_pr(deploy_id, justification)

    @function_tool
    async def draft_hotfix(file_path: str, fix_description: str) -> str:
        """Draft a minimal hotfix patch for a specific file.

        Produces a unified diff template and test suggestions. Does NOT commit
        or push anything — only drafts the fix. Always follow this with
        request_human_approval before any further action.

        Args:
            file_path: Path to the file that needs fixing (e.g. "src/auth/middleware.py").
            fix_description: Description of what needs to change and why.
        """
        return _draft_hotfix(file_path, fix_description)

    return [draft_rollback_pr, draft_hotfix]


def _draft_rollback_pr(deploy_id: str, justification: str) -> str:
    """Generate a rollback PR draft from deploy_id and justification.

    Extracted from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.
    """
    if not deploy_id.strip():
        return "ERROR: 'deploy_id' must be a non-empty string."

    if not justification.strip():
        return "ERROR: 'justification' must be a non-empty string."

    branch = f"revert/{deploy_id}"
    title = f"revert: rollback {deploy_id} (Sentinel auto-draft)"

    body = f"""\
## Rollback PR — {deploy_id}

**Incident trigger:** Sentinel identified this deploy as the likely root cause
of an active incident.

### Justification

{justification}

### What this PR does

Reverts `{deploy_id}` to restore the previous stable state of the service.
The revert commit should be reviewed to confirm it cleanly undoes the suspect changes.

### Pre-merge checklist

- [ ] CI passes on the revert branch
- [ ] On-call engineer has reviewed the justification and diff
- [ ] Monitoring confirms error rate is recovering after merge
- [ ] Rollback does not re-introduce a previously fixed bug

### Post-merge actions

1. Monitor error rate for 10 minutes post-merge
2. Update the incident timeline with resolution timestamp
3. File a post-mortem within 48 hours

---
⚠️ This PR was drafted by Sentinel and requires human approval before opening."""

    return (
        f"PR DRAFT — {deploy_id}\n\n"
        f"title:         {title}\n"
        f"branch:        {branch}\n"
        f"target:        main\n"
        f"deploy_id:     {deploy_id}\n\n"
        f"{body}"
    )


def _draft_hotfix(file_path: str, fix_description: str) -> str:
    """Generate a hotfix diff template from file path and fix description.

    Extracted from the tool decorator so unit tests can call this directly
    without constructing a ToolContext.
    """
    if not file_path.strip():
        return "ERROR: 'file_path' must be a non-empty string."

    if not fix_description.strip():
        return "ERROR: 'fix_description' must be a non-empty string."

    diff_template = f"""\
--- a/{file_path}
+++ b/{file_path}
@@ -0,0 +1,4 @@
+# HOTFIX — apply the change described below
+# File: {file_path}
+# Fix:  {fix_description}
+# TODO: Replace this placeholder with the actual code change"""

    test_suggestions = f"""\
## Test Suggestions

1. Add a regression test reproducing the failure scenario:
   - Set up the conditions described in the fix: "{fix_description}"
   - Assert the fix eliminates the error

2. Run the existing test suite for `{file_path}`:
   - Confirm no existing tests regress
   - Pay attention to edge cases around the changed logic

3. Add a smoke test for the affected endpoint or function:
   - Verify the happy path still works after the change
   - Verify the error case is now handled correctly"""

    return (
        f"HOTFIX DRAFT — {file_path}\n\n"
        f"file:          {file_path}\n"
        f"fix:           {fix_description}\n\n"
        f"## Diff Template\n\n"
        f"{diff_template}\n\n"
        f"{test_suggestions}\n\n"
        f"---\n"
        f"⚠️ This hotfix was drafted by Sentinel and requires human approval before committing."
    )
