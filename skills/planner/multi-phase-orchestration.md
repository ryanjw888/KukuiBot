# Multi-Phase Orchestration

## Rule (non-negotiable)

For any pipeline with 3+ phases, maintain explicit state tracking and offer compaction between phases. Context quality degrades with accumulated agent outputs — manage it proactively.

## When This Fires

- Any documentation pipeline run (5-phase process)
- Any multi-agent project with sequential phases
- After any compaction event (must re-orient)

## Pipeline State Tracker

Maintain a running state block. Update it after every phase transition:

```
PIPELINE_STATE:
- Phase: [current phase number and name]
- Status: [in-progress / gating / waiting-for-agents / complete]
- Agents in-flight: [task IDs and workers]
- Agents completed: [task IDs, statuses]
- Outputs collected: [list of phase outputs received]
- Quality gates passed: [list of phase transitions approved]
- Context health: [estimated % of context used]
- Next action: [what happens next]
```

## Compaction Strategy

- **After Phase 2 (Deep Dive)**: ALWAYS offer compaction. Phase 2 produces 3-7 agent outputs that dominate context. Summarize findings before Phase 3.
- **After Phase 4 (Review)**: Offer compaction if context >50%. Phase 4 adds 3 more agent outputs on top of everything.
- **Mandatory compact**: If context usage exceeds 60% at any phase boundary, compaction is required (not optional).
- **After ANY compaction**: Re-read PIPELINE_STATE, re-read last phase output, re-read quality gate status. Do NOT rely on memory.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I still have context budget." | Budget != quality. Old agent diffs actively confuse later phases. Compact. |
| "I remember where I was." | You don't after compaction. Read the state file. |
| "Compaction wastes time." | Compaction takes 5 seconds. Confused reasoning wastes hours. |
| "I'll compact later." | The best time to compact is at phase boundaries. Later is too late. |

## Red Flags (self-check)

- You are in Phase 4+ and haven't offered compaction once
- You are running phases out of order (e.g., Review before Consolidation)
- You cannot state the current phase and what agents are pending without checking
- You just compacted and are proceeding without re-reading pipeline state
- Phase 2 produced 5+ agent outputs and you are carrying all of them into Phase 3

## Phase Ordering (Enforced)

1 → 2 → 3 → 4 → 5. No skipping. No reordering. Each phase depends on the previous.

Exception: If the quality gate identifies a critical gap, you may re-dispatch within the current phase (not skip ahead).

## Timeout Handling

- If an agent hasn't completed in 10 minutes: `check_task` for status
- If 15 minutes: flag for user intervention
- If 20 minutes: mark as timed out, proceed with available outputs if quality gate allows

## Hard Gate

Phase N+1 dispatch is BLOCKED until:
1. PIPELINE_STATE is current
2. Phase N quality gate shows PASS
3. Compaction has been offered (if applicable)

## Terminal State

→ Pipeline reaches Phase 5 delivery, or explicit abort with partial results saved
