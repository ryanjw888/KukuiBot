# Plan Specificity

## Rule (non-negotiable)

Every implementation plan MUST reference specific file paths, function names, and line numbers. Vague plans that say "modify the handler" or "update the config" without specifics are invalid — they are useless to the developer who must execute them.

## When This Fires

- Producing any implementation plan
- Describing any proposed code change
- Any phase breakdown for a multi-phase project
- Any analysis output that will be handed to a developer

## Specificity Requirements

Every plan phase MUST include:

1. **Exact file paths** — `/Users/jarvis/KukuiBot/server.py`, not "the server file"
2. **Function/class names** — `_get_system_prompt()`, not "the prompt function"
3. **Line numbers** — `server.py:557-650`, not "somewhere in server.py"
4. **The change described in code terms** — "Add a `load_skills_for_worker()` call after line 580 where worker_identity is set", not "integrate the skills"
5. **Dependencies between phases** — "Phase 2 requires the `skill_loader.py` module created in Phase 1"

## Plan Phase Template

```
### Phase N: [Name]
- Files: [exact paths]
- Functions: [function names and current line numbers]
- Changes:
  - In [file:line]: [specific change]
  - In [file:line]: [specific change]
- New files: [if any, with proposed structure]
- Dependencies: [what must exist before this phase]
- Risks: [specific edge cases with file references]
- Verification: [exact command to test this phase]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The developer will figure out where to put it." | Developers execute plans, not interpret vague guidance. Be specific. |
| "Line numbers change, so they're not useful." | Line numbers orient the developer. They're useful NOW. Include them. |
| "I haven't read the code deeply enough for specifics." | Then read deeper. Vague plans waste more time than thorough analysis. |
| "Too much detail makes the plan hard to read." | Insufficient detail makes the plan impossible to execute. Be specific. |
| "I'll reference the general area and the dev can grep." | You are the analyst. Grepping is YOUR job. Do it and cite the results. |

## Red Flags (self-check)

- Your plan says "modify" without specifying which function or lines
- You reference a file without a full path
- Your phase has no verification step
- You describe a change in abstract terms ("add error handling") instead of specific terms ("wrap the `fetch_data()` call at line 42 in a try/except catching `ConnectionError`")
- Your plan could apply to any codebase — it's not grounded in THIS code

## Hard Gate

Plans are INVALID if any phase lacks specific file paths, function names, and testable verification steps. Abstract plans are rejected — re-analyze with code-level specificity.
