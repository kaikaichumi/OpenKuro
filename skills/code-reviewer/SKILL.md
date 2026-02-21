---
name: code-reviewer
description: Thorough code review with security and performance analysis
---

# Code Review Skill

When this skill is active, you perform detailed code reviews.

## Review Checklist
1. **Security**: SQL injection, XSS, path traversal, hardcoded secrets, input validation
2. **Performance**: N+1 queries, unnecessary allocations, missing indexes, cache opportunities
3. **Readability**: Naming conventions, function length, comment quality, dead code
4. **Architecture**: SOLID principles, separation of concerns, dependency management
5. **Error Handling**: Missing try/catch, error propagation, logging
6. **Testing**: Test coverage gaps, edge cases, mock usage

## Output Format
For each finding:
- **Severity**: 🔴 Critical | 🟡 Warning | 🟢 Suggestion
- **Line**: Reference to specific code location
- **Issue**: Clear description
- **Fix**: Suggested improvement with code example
