# Brainstorming

## Rule (non-negotiable)

Before dispatching ANY implementation or analysis agents, refine the request through structured dialogue. Do NOT jump to agent dispatch on the first message.

## When This Fires

- User requests documentation generation, code analysis, or any multi-agent task
- User describes a goal that requires scoping decisions (agent count, approach, format)
- Any new pipeline that hasn't been through brainstorming yet

## HARD GATE

Do NOT call `delegate_task` until you have:
1. Confirmed the target (what codebase, what repo, what scope)
2. Clarified the output format (CLAUDE.md, AGENTS.md, custom format, report)
3. Determined the approach (full pipeline, update mode, quick analysis)
4. Presented a scope summary and received user confirmation

## The Brainstorming Process

1. **Explore**: Ask 1-2 clarifying questions (not a wall of questions). Use numbered options when presenting choices.
2. **Propose**: Suggest 2-3 approaches with tradeoffs. Recommend one.
3. **Confirm**: Present a short scope summary. Get explicit approval before proceeding.
4. **Transition**: Move to scope-assessment, then delegation-dispatch.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The request is clear enough." | Clear to you is not clear to the agents you'll dispatch. Confirm with the user. |
| "Brainstorming wastes time on obvious tasks." | Obvious tasks still need scope confirmation. 30 seconds of alignment saves 30 minutes of rework. |
| "The user already told me what to do." | Did they specify format, approach, and scope? If not, ask. |
| "I'll figure it out as I go." | Agents can't course-correct mid-task. Get it right before dispatch. |

## Red Flags (self-check)

- You are calling `delegate_task` on your first response to the user
- You haven't asked the user any clarifying questions
- You don't know the output format the user expects
- You're assuming full pipeline when update mode might be appropriate

## Exception

If the user explicitly says "just do it" or provides a complete specification with target, format, and scope — you may skip brainstorming and go directly to scope-assessment. But log that brainstorming was skipped with justification.

## Terminal State

→ `scope-assessment` → `delegation-dispatch`
