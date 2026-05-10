---
description: Scaffold a new Sentinel agent with prompt file, agent definition, and test stub
allowed-tools: Read, Write, Edit, Bash
model: sonnet
---

Create a new Sentinel agent named: $ARGUMENTS

Steps:
1. Read ARCHITECTURE.md section 5 (Agent Definitions) for the spec of this agent
2. Create the system prompt file at `src/sentinel/agents/prompts/<name>.txt` following the prompt rules in CLAUDE.md
3. Create the agent module at `src/sentinel/agents/<name>.py` with:
   - Import the relevant tools from `src/sentinel/tools/`
   - Load the system prompt from the txt file
   - Define the Agent using the OpenAI Agents SDK pattern from CLAUDE.md
   - Add proper type hints and docstring
4. Create a test stub at `tests/test_agents/test_<name>.py` with:
   - A fixture that creates the agent with mock tools
   - A test that verifies the agent is properly configured (name, model, tools, handoffs)
   - A TODO comment for the integration test with a real scenario
5. Add the agent import to `src/sentinel/agents/__init__.py`
6. Print what was created and what tools/handoffs are wired up
