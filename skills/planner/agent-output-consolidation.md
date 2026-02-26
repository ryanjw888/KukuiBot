# Agent Output Consolidation

## Rule (non-negotiable)

When two or more parallel agents return outputs for the same phase, you MUST reconcile them through a structured conflict resolution process before using them. No cherry-picking. No first-wins bias.

## When This Fires

- Two or more delegated tasks for the same phase have completed
- You are about to synthesize agent outputs into a single artifact
- Phase 3 (Consolidation) is starting with multiple Phase 2 inputs

## The CONFLICT_MATRIX

When parallel outputs contain overlapping or contradictory claims, produce:

```
CONFLICT_MATRIX:
| # | Topic | Agent A Position | Agent B Position | Evidence A | Evidence B | Resolution | Rationale |
```

Rules for resolution:
- Agent with `file:line` evidence wins over agent with general claims
- When both have evidence: flag as `[NEEDS VERIFICATION]` with both sources
- When neither has evidence: drop the claim entirely

## Consolidation Process

1. **Read ALL outputs completely** — do not skim, do not stop at the first one
2. **Build fact table**: claim, evidence, source_agent, confidence
3. **Identify overlaps**: same topic covered by multiple agents
4. **Resolve conflicts**: apply CONFLICT_MATRIX for contradictions
5. **Deduplicate**: keep the version with better evidence
6. **Preserve unique insights**: union of non-overlapping findings (not intersection)
7. **Cap output**: consolidated artifact must be ≤15K chars (fits in one delegation prompt)

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "Both agents mostly agree, no need to cross-reference." | "Mostly agree" hides the 10% that contradicts. Cross-reference everything. |
| "I'll just take the better output." | Better by what criteria? Without a CONFLICT_MATRIX, you're guessing. |
| "The first agent's output is comprehensive enough." | First-wins bias produces incomplete analysis. Read ALL outputs. |
| "Merging is just concatenation." | Concatenation produces duplicates and contradictions. Structure the merge. |

## Red Flags (self-check)

- You haven't read all agent outputs before starting to write
- You are using one agent's output as the base and ignoring the other
- No CONFLICT_MATRIX exists despite parallel agent outputs
- Your consolidated output is just copy-pasted sections from each agent
- Contradictions exist in your output with no written resolution

## Hard Gate

Consolidated output is BLOCKED until:
1. ALL parallel agent outputs have been read (not just the first)
2. CONFLICT_MATRIX is produced for any overlapping claims
3. Source attribution is preserved (which agent said what)
4. Total output is ≤15K chars

## Terminal State

→ `quality-gating` → next phase's `delegation-dispatch`
