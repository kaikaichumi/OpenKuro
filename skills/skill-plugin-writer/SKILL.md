---
name: skill-plugin-writer
description: Expert in writing Kuro Skills (SKILL.md) and Plugins (Python BaseTool)
---

# Skill & Plugin Writer

When this skill is active, you are an expert in creating Kuro extensions.

## Creating a Skill (SKILL.md)

Skills are markdown instruction files in `./skills/<name>/SKILL.md`:

```markdown
---
name: my-skill
description: Brief description
---

# Skill Instructions

When this skill is active, you should...
- Follow these guidelines
- Use this approach
```

### Skill Best Practices
- Keep instructions clear and actionable
- Define specific output formats
- Reference available tools the skill should use
- Include examples of expected behavior

## Creating a Plugin (Python Tool)

Plugins are Python files in `./plugins/<name>.py`:

```python
from src.tools.base import BaseTool, RiskLevel, ToolContext, ToolResult

class MyTool(BaseTool):
    name = "my_tool"
    description = "What this tool does"
    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "Input parameter"
            }
        },
        "required": ["input"]
    }
    risk_level = RiskLevel.LOW  # LOW, MEDIUM, HIGH, CRITICAL

    async def execute(self, params: dict, context: ToolContext) -> ToolResult:
        try:
            result = params["input"].upper()
            return ToolResult.ok(result)
        except Exception as e:
            return ToolResult.error(str(e))
```

### Plugin Best Practices
- Use appropriate risk_level (LOW for read-only, MEDIUM for writes, HIGH for system)
- Always handle errors with try/except
- Return ToolResult.ok() or ToolResult.error()
- Keep parameters schema complete with descriptions
- Available context: context.session_id, context.agent_manager

## Available Risk Levels
- **LOW**: Auto-approved (read operations)
- **MEDIUM**: Approved with trust (write operations)
- **HIGH**: Always requires approval (system operations)
- **CRITICAL**: Never auto-approved (messaging, external calls)
