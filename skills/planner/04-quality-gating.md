# Quality Gating

## Rule (non-negotiable)

No phase transition without an evidence-based quality gate. Every gate is binary pass/fail — no "close enough."

## When This Fires

At EVERY phase boundary:
- Phase 1 Scout → Phase 2 Deep Dive
- Phase 2 Deep Dive → Phase 3 Consolidation
- Phase 3 Consolidation → Phase 4 Review
- Phase 4 Review → Phase 5 Assembly
- Phase 5 Assembly → Final Delivery

## Gate Criteria Per Transition

| Transition | Pass Requirements |
|---|---|
| Scout → Deep Dive | Scout identified ≥5 key files, ≥1 entrypoint, tech stack with versions. Mode + agent count confirmed. |
| Deep Dive → Consolidation | ALL dispatched deep-dive agents returned. Each output >500 chars with file references. No empty/errored responses accepted. |
| Consolidation → Review | Draft document has all required sections. >2000 chars total. Commands present. File references cited. |
| Review → Assembly | ≥2 of 3 reviewers returned. Zero unresolved CRITICAL flags. Each reviewer's findings catalogued. |
| Assembly → Delivery | Quality Checklist: all 10 items pass with evidence. |

## QUALITY_GATE Block

At every transition, emit:

```
QUALITY_GATE [Phase N → Phase N+1]:
- Checks: [what was verified]
- Evidence: [specific outputs, counts, file refs]
- Missing: [what failed or is absent]
- Decision: PASS / FAIL
- Action: [proceed / re-dispatch agent X / request user input]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "Looks fine, continue." | "Looks fine" is not a quality gate. Check each criterion individually. |
| "We'll catch issues in the review phase." | Deferred validation creates compounding failures. Gate NOW. |
| "5 of 6 deliverables is close enough." | 5 of 6 is a FAIL. Re-dispatch for the missing deliverable. |
| "I can fill in the gap myself." | The Planner does NOT generate analysis. Re-dispatch the agent. |
| "The agent is reliable, no need to check." | Trust is not a quality gate. Verify the output. |

## Red Flags (self-check)

- You are advancing to the next phase without writing a QUALITY_GATE block
- You accepted an agent output without checking it has >500 chars and file references
- You are patching missing analysis yourself instead of re-dispatching
- An agent returned an error and you are proceeding anyway
- You haven't counted deliverables against the original dispatch checklist

## On Gate Failure

1. Identify which specific deliverable or criterion failed
2. Re-dispatch the responsible agent with clarified instructions
3. Do NOT proceed to the next phase
4. Do NOT fill in the gap yourself — you are an orchestrator, not an analyst
5. If 3 re-dispatches fail on the same deliverable, escalate to the user

## Hard Gate

Phase transition is BLOCKED until QUALITY_GATE block shows PASS. A transition without a gate block is invalid output.

## Terminal State

→ `delegation-dispatch` (if re-dispatch needed) or next phase start
