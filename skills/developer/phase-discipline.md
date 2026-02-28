# Phase Discipline

## Rule (non-negotiable)

For any project with 3+ phases or significant scope, you MUST follow the plan-approve-execute-verify-compact cycle. Each phase is a coherent unit committed and verified independently. No combining phases. No skipping approval.

## When This Fires

- Any multi-file feature implementation
- Any refactoring that touches 3+ files
- Any project the user or planner has broken into phases
- Any task where you're tempted to "just do it all at once"

## The Cycle

For each phase:

1. **Plan** — State what this phase will change, which files, and what the expected outcome is.
2. **Get approval** — Present the phase plan. Wait for approval before starting. If delegated with an approved plan, proceed.
3. **Execute** — Make the changes for THIS phase only. Do not sneak in work from future phases.
4. **Verify** — Test the changes. Include evidence (invoke test-after-change skill).
5. **Commit** — Commit the verified changes with a descriptive message.
6. **Offer compact** — If accumulated context is significant, offer compaction before the next phase.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "These two phases are so related I should do them together." | Related phases still need independent verification. Do them separately. |
| "I can see the whole solution, let me just implement it." | Seeing the solution is not validating it. Phase by phase. |
| "Approval would slow me down." | Approval catches wrong directions before you waste time. It speeds you up. |
| "I'll commit everything at the end." | Large commits are unreviewable and hard to revert. Commit per phase. |
| "Compaction wastes time between phases." | Stale context causes bugs. Compact when context is heavy. |

## Red Flags (self-check)

- You are editing files that belong to a future phase
- You haven't stated which phase you're on
- You are making changes without approval on a multi-phase project
- Your commit includes changes from multiple phases
- You completed 3+ phases without offering compaction

## Hard Gate

Multi-phase projects CANNOT proceed to Phase N+1 until Phase N is committed and verified. Combining phases into a single commit is invalid unless explicitly approved by the user.
