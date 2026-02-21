---
name: daily-report
description: Generate structured daily/weekly reports from activity data
---

# Daily Report Skill

When this skill is active, you generate structured reports.

## Report Types
1. **Daily Report**: Summary of today's activities
2. **Weekly Report**: Week overview with metrics
3. **Sprint Report**: Sprint progress and blockers

## Data Sources
- Git commit history (use shell_execute with git log)
- File changes (use file_search)
- Calendar events (use calendar_read)
- Memory entries (use memory_search)

## Output Template
```markdown
# [Date] Daily Report

## Summary
[2-3 sentence overview]

## Completed
- [task 1]
- [task 2]

## In Progress
- [task with status]

## Blockers
- [any blockers]

## Tomorrow's Plan
- [planned tasks]
```
