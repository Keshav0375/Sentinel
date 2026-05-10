---
description: Fire a synthetic scenario and watch the full agent pipeline
allowed-tools: Read, Bash
---

Run a Sentinel scenario end-to-end: $ARGUMENTS

1. If no scenario specified, list available scenarios from `data/scenarios/` and ask which one
2. Run: `python scripts/run_scenario.py $ARGUMENTS`
3. Watch the output — show me:
   - Which agents were invoked and in what order
   - What tools were called
   - Whether HITL gate was triggered
   - The final incident summary
   - Any errors or unexpected behavior
4. If the scenario fails, diagnose the issue from the traceback and suggest a fix
