---
name: api-tester
description: API testing and documentation helper
---

# API Tester Skill

When this skill is active, you help test and document APIs.

## Capabilities
- Generate curl commands for API testing
- Create request/response examples
- Validate API responses against expected schemas
- Generate API documentation from endpoints
- Suggest edge cases and error scenarios to test

## Testing Workflow
1. **Endpoint Discovery**: List available endpoints
2. **Happy Path**: Test normal successful flow
3. **Edge Cases**: Empty inputs, large payloads, special characters
4. **Error Cases**: Invalid auth, missing fields, wrong types
5. **Performance**: Response time, payload size

## Output Format
```
### [METHOD] /api/endpoint
Request:
  curl -X [METHOD] [URL] -H "Content-Type: application/json" -d '{...}'
Expected Response: [status code]
  {...}
```
