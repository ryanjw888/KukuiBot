# Audit Phase Discipline

## Rule (non-negotiable)

Network security audits are sequential, multi-phase operations. Each phase depends on the output of the previous phase. You MUST execute phases in order, verify completion, and document results before advancing. No skipping. No reordering.

## When This Fires

- Any request to run a network audit, security scan, or vulnerability assessment
- Resuming an audit after compaction
- Any multi-phase infrastructure operation (migration, hardening, deployment)

## Phase Execution Protocol

For each phase of an audit:

1. **Re-read the runbook** — Before starting any phase, re-read the relevant section of the audit runbook. Do NOT work from memory.
2. **Announce the phase** — State which phase you are starting and what it will accomplish.
3. **Execute all steps** — Run every command in the phase. Do not skip steps because "they're probably fine."
4. **Capture all output** — Save scan results to the report directory. Do not discard intermediate data.
5. **Verify completion** — Confirm the phase produced the expected output files/data before moving on.
6. **Emit a PHASE_COMPLETE block** — Document what was done and what was found.

```
PHASE_COMPLETE [Phase N: Name]:
- Steps executed: [list of steps run]
- Output files: [files saved to report dir]
- Key findings: [summary of what was discovered]
- Hosts/services discovered: [counts]
- Ready for Phase N+1: YES / NO
- Blockers: [any issues that prevent advancing]
```

## Compaction at Phase Boundaries

After completing any phase that produces significant scan output (Phases 1-5 especially):
- Offer compaction to the user
- If compacting: save PHASE_COMPLETE block and phase outputs to disk BEFORE compacting
- After compaction: re-read the runbook, re-read saved outputs, re-read PHASE_COMPLETE from prior phases

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I can skip Phase 1 discovery, I know the hosts." | Discovery catches devices you don't know about. That's the point. Run it. |
| "Port scanning takes too long, I'll just check common ports." | The runbook defines the port list for a reason. Run the full scan. |
| "Phase 3 probes are overkill for this network." | Targeted probes find vulns that port scans miss. Run every probe. |
| "I'll combine Phases 2 and 3 to save time." | Combined phases produce muddled output. Separate phases, separate analysis. |
| "The baseline hasn't changed, skip Phase 6." | Skipping baseline comparison defeats the purpose of repeat audits. |

## Red Flags (self-check)

- You are starting Phase N+1 without a PHASE_COMPLETE block for Phase N
- You are skipping commands in the runbook
- You are running commands from memory instead of re-reading the runbook
- You just compacted and are proceeding without re-reading prior phase outputs
- You are combining multiple phases into a single execution block

## Hard Gate

Phase N+1 is BLOCKED until Phase N has a PHASE_COMPLETE block with "Ready for Phase N+1: YES." Advancing without this block is invalid output.
