---
description: Scaffold a new Sentinel tool with Pydantic schemas, implementation, and test
allowed-tools: Read, Write, Edit, Bash
model: sonnet
---

Create a new Sentinel tool named: $ARGUMENTS

Steps:
1. Read ARCHITECTURE.md section 6 (Tool Layer Design) for the spec of this tool
2. If the tool needs new Pydantic models, add them to the appropriate file in `src/sentinel/models/`
3. Create the tool module at `src/sentinel/tools/<name>.py` with:
   - Pydantic input/output models (or import from models/)
   - The tool function decorated with `@function_tool` from the Agents SDK
   - Async implementation
   - Proper docstring (this becomes the tool description the LLM sees)
   - Error handling that returns structured responses, never raises raw exceptions
4. Create a test at `tests/test_tools/test_<name>.py` with:
   - Test with valid input → expected output
   - Test with edge case input (empty, missing fields)
   - Mock any external dependencies (memory, generators)
5. Register the tool in `src/sentinel/tools/registry.py` if it exists
6. Print what was created, the tool's Pydantic schema, and which agent(s) should use it
