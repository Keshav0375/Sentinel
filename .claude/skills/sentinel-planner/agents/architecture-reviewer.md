# Architecture Reviewer Agent

Use when `/sentinel-planner review <repo>` is invoked. Checks completeness of a repo's planning docs.

## Checklist

For the target repo folder, verify:

### README.md
- [ ] Exists and describes the repo
- [ ] Has "Key Decisions" table with at least one entry
- [ ] Has "Status" section reflecting current reality
- [ ] Links to ARCHITECTURE.md (if it exists)
- [ ] Under 80 lines

### ARCHITECTURE.md (if exists)
- [ ] Has system overview with diagram
- [ ] All components have their own numbered section
- [ ] External services have tier/cost tables
- [ ] Prerequisites section has actionable checkboxes
- [ ] Cost breakdown table exists
- [ ] No references to superseded decisions (e.g., DigitalOcean if we switched to Azure)
- [ ] Config schemas use real field names

### Consistency
- [ ] README decisions match ARCHITECTURE.md content
- [ ] STATE.md status matches README status
- [ ] No orphaned references to removed components
- [ ] Cost figures match Subscription-plans/ reference docs

## Output Format

```
## Review: <repo-name>

**Score:** X/Y checks passed

**Issues:**
1. <what's wrong and where>
2. <what's wrong and where>

**Missing:**
- <what doesn't exist yet but should>
```
