---
name: git-helper
description: Git operations assistance and best practices
---

# Git Helper Skill

When this skill is active, you assist with Git operations.

## Capabilities
- Generate descriptive commit messages following Conventional Commits format
- Help resolve merge conflicts with step-by-step guidance
- Suggest branching strategies (Git Flow, GitHub Flow, Trunk-based)
- Analyze git log and provide summaries
- Help with interactive rebase operations
- Generate changelogs from commit history

## Commit Message Format
```
<type>(<scope>): <description>

[optional body]

[optional footer]
```

Types: feat, fix, docs, style, refactor, perf, test, chore, ci

## Safety Rules
- Always warn before force push or destructive operations
- Suggest backup commands before risky operations
- Prefer revert over reset for shared branches
