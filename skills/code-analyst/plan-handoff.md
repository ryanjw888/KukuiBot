# Plan Handoff

## Rule (non-negotiable)

After producing a complete implementation plan, you MUST offer to delegate it to a developer worker for execution. Plans that sit unexecuted are waste. Completed analysis should flow to implementation.

## When This Fires

- You have produced an implementation plan with specific file paths, line numbers, and phases
- The plan passes `plan-specificity` requirements (file paths, function names, verification steps)
- The user has confirmed the plan (or the plan was requested with clear intent to implement)

## The Handoff Protocol

1. **Verify plan completeness** — Does the plan have all required fields per `plan-specificity`? If not, complete it first.
2. **Check developer pool** — Read the Available Workers table. Identify available developer workers and their models.
3. **Offer the handoff** — Ask the user: "Want me to hand this off to [Developer N] for implementation?"
4. **Build the delegation prompt** — If approved, construct the prompt using the Pre-Dispatch Checklist:

```
HANDOFF_PROMPT:
- Objective: [from plan's Objective section]
- Input files: [every file path from the plan's Phases]
- Constraints: [from plan's Security Considerations + scope boundaries]
- Deliverables: [numbered list — one per phase]
- Output format: [phase-by-phase completion with VERIFICATION blocks]
- Evidence rules: "Every change must include test output proving it works"
- Character limit: "Keep status updates under 8K chars"
- Stop condition: "All phases complete and verified, or blocked with clear blocker description"
- TASK_DONE: "End with TASK_DONE {task_id}"
```

5. **Include the full plan** — Paste the complete plan into the delegation prompt. Do not summarize or abbreviate.
6. **Dispatch** — Use `delegate_task` with the constructed prompt.
7. **Track** — Note the task_id. Offer to check on progress when the user asks.

## Model Selection

| Plan complexity | Recommended developer model |
|---|---|
| Single-phase, straightforward edits | claude_sonnet (fast, cost-effective) |
| Multi-phase, 3+ files, architectural changes | claude_opus (best reasoning) |
| Parallel independent phases | Multiple workers (one per independent phase) |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The user can delegate it themselves." | Offering handoff is part of the analyst's job. The plan is most accurate when the author builds the prompt. |
| "The plan is clear enough for anyone." | Clear to the analyst is not clear to a fresh developer context. Build a complete prompt. |
| "I should wait for the user to ask." | Proactive handoff saves a round-trip. Offer it. |
| "I'll just do the implementation myself." | Analysts don't write production code. Delegate to a developer. |

## Red Flags (self-check)

- You produced a plan but didn't offer delegation
- Your handoff prompt is under 500 characters (too vague for a developer)
- You summarized the plan instead of including it in full
- You didn't check the Available Workers table before suggesting a target
- You delegated without user approval

## Hard Gate

Handoff prompts are BLOCKED unless:
1. The plan passes `plan-specificity` requirements
2. The Available Workers table has been checked
3. The user approved the delegation (or explicitly pre-authorized it)
4. The prompt includes the full plan text and all 9 Pre-Dispatch Checklist items

## Terminal State

-> `check_task` polling or user-initiated status check
