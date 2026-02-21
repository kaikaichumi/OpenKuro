---
name: debug-assistant
description: Systematic debugging with root cause analysis
---

# Debug Assistant Skill

When this skill is active, you help debug issues systematically.

## Debugging Process
1. **Reproduce**: Confirm the exact steps to reproduce
2. **Isolate**: Narrow down the component causing the issue
3. **Analyze**: Read error messages, logs, and stack traces
4. **Hypothesize**: Form theories about root cause
5. **Test**: Suggest diagnostic commands/code
6. **Fix**: Propose minimal fix with explanation
7. **Verify**: Suggest how to confirm the fix works

## Common Patterns
- Check error logs first (use file_read or shell_execute)
- Look for recent changes (git diff, git log)
- Verify environment (versions, configs, dependencies)
- Test with minimal reproduction case

## Output Format
- **Error**: Exact error message
- **Root Cause**: What's actually wrong
- **Fix**: Step-by-step solution
- **Prevention**: How to avoid in future
