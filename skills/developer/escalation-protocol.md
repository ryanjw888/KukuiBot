# Escalation Protocol

## Rule (non-negotiable)

When you encounter complexity that exceeds the scope of your implementation plan, you MUST pause and escalate rather than improvise architectural decisions. Developers execute plans — they don't redesign systems mid-flight.

## When This Fires

- The blast radius of a change exceeds 5 files not mentioned in the plan
- You discover a dependency chain deeper than 3 levels that the plan didn't account for
- You need to make an architectural decision (new pattern, new abstraction, restructure) not covered by the plan
- The plan's approach is blocked by something the analyst didn't anticipate
- You're unsure whether a change is safe and can't verify with existing tests

## The Escalation Protocol

1. **Stop coding.** Do not push through uncertainty. Save your current state.
2. **Document what you found:**

```
ESCALATION:
- Phase: [which phase you're on]
- Blocked by: [specific technical obstacle]
- Discovery: [what you found that the plan didn't account for]
- Files affected: [list of files beyond the plan's scope]
- Question: [specific question that needs an analyst/architect answer]
- Impact if I proceed without guidance: [what could go wrong]
- Suggested options: [if you have ideas, list 2-3 — but don't pick one]
```

3. **Route the escalation:**
   - **Architectural question** -> delegate to code-analyst with the ESCALATION block + relevant file contents
   - **Infrastructure/environment issue** -> delegate to it-admin
   - **Scope expansion** -> report to the user directly (they need to approve expanded scope)

4. **Wait for resolution** before continuing the blocked phase. You may proceed with other independent phases if they exist.

## What Counts as Escalation-Worthy

| Escalate | Don't escalate |
|---|---|
| "This function has 12 callers I need to update" | "This function has a typo in its name" |
| "The plan says modify X but X was refactored since the analysis" | "The plan says line 42 but it's now line 45" |
| "I need to add a new module/pattern not in the plan" | "I need to add a helper function within the planned file" |
| "Changing this breaks the API contract" | "Changing this requires updating a test" |
| "I don't understand why the existing code works this way" | "I found a minor style inconsistency" |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I can figure this out, I'm a developer." | Figuring out architecture mid-implementation produces spaghetti. Escalate. |
| "Escalating will slow things down." | Pushing through produces rework. Escalation is faster overall. |
| "The analyst will just tell me what I already know." | Then the escalation is quick. But if they catch something you missed, it saves hours. |
| "It's just one extra file beyond the plan." | One file becomes three. Scope creep starts small. Flag it now. |
| "I'll document the deviation after I'm done." | After-the-fact documentation is rationalization. Escalate BEFORE acting. |

## Red Flags (self-check)

- You are modifying files not listed in the plan without flagging it
- You are making design decisions ("I'll create a new utility module for this")
- You are rewriting a function's interface to make your change work
- You spent more than 10 minutes understanding a dependency chain the plan didn't mention
- You are adding error handling for scenarios the plan didn't anticipate

## Hard Gate

Changes that expand beyond the plan's stated file list and function scope are BLOCKED until an ESCALATION block is produced and routed. Unauthorized scope expansion is invalid.
