# Delegation Dispatch

## Rule (non-negotiable)

Every delegated prompt MUST include full context, specific deliverables, output format, stop conditions, and a character limit. No vague prompts. No "look at the codebase" without targets.

## When This Fires

Every time you are about to call `delegate_task`. No exceptions.

## Pre-Dispatch Checklist

Before EVERY `delegate_task` call, verify the prompt contains ALL of these:

1. **Objective** — What specific question this agent answers
2. **Input files/paths** — What to read (not "the codebase" — specific files or directories)
3. **Constraints** — Boundaries, what NOT to do, what to skip
4. **Required deliverables** — Numbered checklist of what to return
5. **Output format** — Headers, structure, evidence requirements
6. **Evidence rules** — "Every claim must cite file:line_number"
7. **Character limit** — "Keep output under 12K/15K characters"
8. **Stop condition** — When the agent should stop and return results
9. **TASK_DONE instruction** — "Mark completion with TASK_DONE"

If ANY item is missing, rewrite the prompt before dispatching.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The agent can infer context." | Agents cannot read your mind. Include explicit context. |
| "I'll send a minimal prompt to save tokens." | Incomplete prompts cause retries that cost MORE tokens. |
| "I can clarify if they ask." | Delegated agents cannot ask you questions mid-task. Front-load all context. |
| "Good enough for a smart model." | Even smart models produce garbage from vague prompts. Be specific. |
| "I'll batch-dispatch and sort it out later." | Batch dispatch with vague prompts = batch garbage. Quality in, quality out. |

## Red Flags (self-check)

- Your prompt is under 500 characters (almost certainly too vague)
- Your prompt says "analyze" without specifying what to look for
- Your prompt has no numbered deliverables list
- Your prompt doesn't specify output length
- You're dispatching to a model/worker combo without checking the Available Workers table

## Pool Awareness

- Check Available Workers table before dispatching
- Batch parallel dispatches in groups of 2-3 (leave room for user tabs)
- Never dispatch to a worker/model combo with an in-flight task
- For Codex targets: prompts need MORE structure (explicit headers, numbered steps)
- For Opus targets: prompts can be more open-ended but still need deliverables

## Model Routing

| Task type | Model | Why |
|---|---|---|
| Deep analysis, architecture, security | claude_opus | Best reasoning |
| Standard implementation, single-file | claude_sonnet | Fast, cost-effective |
| Batch extraction, mechanical work | codex | Cheapest, structured |
| Reports, creative output | opus or sonnet | Opus for nuance |

Default: cheaper/faster unless ambiguity, security, or architecture is involved.

## Hard Gate

`delegate_task` is BLOCKED unless the prompt passes the 9-point Pre-Dispatch Checklist above. If you catch yourself about to dispatch without checking — STOP. Review the checklist. Rewrite if needed.

## Terminal State

→ `quality-gating` (when agent returns) or `multi-phase-orchestration` (when batch completes)
