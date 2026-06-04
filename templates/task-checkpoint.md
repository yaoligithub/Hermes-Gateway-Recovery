# <Task title>

- task_id: <YYYYMMDD-HHMM-task-slug>
- owner_profile: <profile-or-default>
- updated_at: <ISO timestamp>
- status: active
- resume_summary: <2-5 sentences explaining the task and current state>

## Original Request

<Copy the user's original request or a compact faithful summary.>

## Artifacts

- <paths / URLs / IDs / process IDs / cron job IDs>

## Completed

- [x] <completed step>

## Pending

- [ ] <next exact step>

## Risks / Constraints

- <Anything that should not be repeated, restarted, deleted, or exposed.>

## Resume Instructions

1. Read this checkpoint before asking the user to restate context.
2. Verify artifact paths still exist.
3. Continue from the first unchecked pending item.
4. Update this checkpoint before any gateway restart, context compaction, or handoff.
5. Mark `status: completed` when done.
