# Multi-Phase Orchestration

## Rule (non-negotiable)

For any development project with 3+ phases, maintain explicit state tracking and offer compaction between phases. Context quality degrades with accumulated agent outputs — manage it proactively.

## When This Fires

- Any multi-phase development project you are coordinating
- Multiple developer agents working on sequential or parallel phases
- After any compaction event (must re-orient)

## Pipeline State Tracker

Maintain a running state block. Update it after every phase transition:

```
PIPELINE_STATE:
- Project: [what is being built/fixed]
- Phase: [current phase number and name]
- Status: [in-progress / gating / waiting-for-agents / complete]
- Agents in-flight: [task IDs and workers]
- Agents completed: [task IDs, statuses, brief outcome]
- Quality gates passed: [list of phase transitions approved]
- Context health: [estimated % of context used]
- Next action: [what happens next]
```

## Compaction Strategy

- **After 3+ agent outputs received**: ALWAYS offer compaction. Summarize findings before dispatching more work.
- **Mandatory compact**: If context usage exceeds 60% at any phase boundary, compaction is required.
- **After ANY compaction**: Re-read PIPELINE_STATE, re-read last phase output, re-read quality gate status. Do NOT rely on memory.

## Parallel Dispatch for Independent Phases

When a plan contains independent phases (no data dependency between them):
1. Identify which phases can run in parallel
2. Dispatch each to a separate developer worker
3. Track all task IDs in PIPELINE_STATE
4. Quality gate each independently before proceeding to dependent phases

## Timeout Handling

- If an agent hasn't completed in 10 minutes: `check_task` for status
- If 15 minutes: flag for user intervention
- If 20 minutes: mark as timed out, proceed with available outputs if quality gate allows

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I still have context budget." | Budget != quality. Old agent outputs confuse later phases. Compact. |
| "I remember where I was." | You don't after compaction. Read the state. |
| "All phases are sequential, no parallelism possible." | Check again. Many plans have independent setup phases. |
| "I'll track state in my head." | Write it down. PIPELINE_STATE exists for a reason. |

## Hard Gate

Phase N+1 dispatch is BLOCKED until:
1. PIPELINE_STATE is current
2. Phase N quality gate shows PASS
3. Compaction has been offered (if applicable)
