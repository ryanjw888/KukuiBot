# Tradeoff Analysis

## Rule (non-negotiable)

Every architectural recommendation or design decision MUST include tradeoff analysis. No solution is presented without alternatives considered and pros/cons evaluated. Single-option recommendations are invalid.

## When This Fires

- Proposing an architecture or design approach
- Recommending a technology, pattern, or library
- Suggesting how to solve a problem that has multiple valid approaches
- Any recommendation that will significantly influence the codebase direction

## The Tradeoff Protocol

For every significant recommendation:

1. **State the problem clearly** — What constraint or goal is driving this decision?
2. **Present 2-3 viable options** — Not strawmen. Genuine alternatives that could work.
3. **Evaluate each on consistent criteria** — Complexity, performance, maintainability, security, reversibility.
4. **Make a clear recommendation** — State which option you recommend and the primary reason why.
5. **Acknowledge the cost** — Every choice has a downside. State what you're giving up.

Emit a `TRADEOFF_ANALYSIS` block:

```
TRADEOFF_ANALYSIS:
- Decision: [what is being decided]
- Options:
  1. [Option A]: [brief description]
     - Pros: [specific advantages]
     - Cons: [specific disadvantages]
  2. [Option B]: [brief description]
     - Pros: [specific advantages]
     - Cons: [specific disadvantages]
  3. [Option C]: [brief description] (if applicable)
     - Pros: [specific advantages]
     - Cons: [specific disadvantages]
- Recommendation: [Option X] because [primary reason]
- Cost of choice: [what we give up with this option]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "There's obviously only one right answer." | Obvious answers haven't been stress-tested. Find alternatives. |
| "The alternatives are clearly worse." | If they're clearly worse, it should be easy to explain why. Document it. |
| "Tradeoff analysis slows down the recommendation." | Wrong recommendations slow down the project. Analyze tradeoffs. |
| "The user asked for a recommendation, not options." | Recommendations without context are opinions. Provide the analysis. |
| "I already know what the team will pick." | Your job is to inform the decision, not predict it. Present the options. |

## Red Flags (self-check)

- You are presenting a single approach with no alternatives mentioned
- Your "alternatives" are obviously bad choices designed to make your recommendation look good
- You cannot articulate what you're giving up with your recommended approach
- All your pros/cons are vague ("cleaner," "better," "simpler") instead of specific
- You are recommending a complex solution without considering the simple one

## Hard Gate

Architectural recommendations are INVALID without a TRADEOFF_ANALYSIS block showing at least 2 genuine options with specific pros/cons. Single-option recommendations must be justified by explaining why no alternatives exist.
