# Scope Assessment

## Rule (non-negotiable)

Before dispatching ANY Phase 1 Scout agent, produce a SCOPE_CARD that determines pipeline configuration. No blind dispatch.

## When This Fires

- User requests documentation generation for a codebase
- User requests a code analysis pipeline
- Any task requiring multi-agent orchestration

## The SCOPE_CARD

Before dispatching Phase 1, you MUST produce and present:

```
SCOPE_CARD:
- Target: [repo path or URL]
- Accessible: [yes/no — verified by reading a file]
- Mode: [full-gen / update / multi-repo]
- Estimated LOC: [small <10K / medium 10K-100K / large 100K+]
- Existing docs: [list any CLAUDE.md, AGENTS.md, README.md found]
- Agent count: [N deep-dive + N review = N total]
- Conditional agents: [which of 2d-2g are pre-planned and why]
- Estimated cost: [$X-Y based on agent count]
- User confirmation: [required before proceeding]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "This is small, I can skip scoping." | That is a red flag. Create SCOPE_CARD anyway. Small repos need fewer agents — scoping determines that. |
| "I already know this codebase." | Familiarity is not a substitute for systematic assessment. Scope it. |
| "I'll figure out agent count as I go." | Agent count must be justified BEFORE dispatch. Adaptive is not the same as unplanned. |
| "Scoping slows me down." | Bad delegation causes 3x rework. Scoping is speed insurance. |

## Red Flags (self-check)

- You are about to call `delegate_task` without a written SCOPE_CARD
- You cannot explain why N agents instead of N-1 or N+1
- You haven't verified the target path is accessible
- You haven't checked for existing documentation

## Hard Gate

`delegate_task` for Phase 1 is BLOCKED until:
1. SCOPE_CARD is written and presented to user
2. Target path accessibility is verified (read a file from it)
3. Mode decision is explicit (full-gen vs update)
4. User has confirmed scope (or scope is unambiguous)

## Terminal State

→ `delegation-dispatch` (Phase 1 Scout)
