# Read Before You Opine

## Rule (non-negotiable)

Before making ANY recommendation, assessment, or architectural suggestion, you MUST have read the relevant source code and traced the full call chain. Opinions without code evidence are invalid.

## When This Fires

- Any code review or analysis request
- Any architecture assessment
- Any implementation plan that modifies existing code
- Any recommendation about patterns, structure, or design
- Any time you are about to say "you should" or "I recommend"

## The Analysis Protocol

Before any recommendation:

1. **Read the target code** — Not just the file in question, but the relevant functions and their callers.
2. **Trace the data flow** — Follow inputs from entry point through processing to output. Grep for all references.
3. **Map the dependencies** — What imports this? What does this import? What breaks if this changes?
4. **Identify the conventions** — What patterns does this codebase use? Your recommendation must work WITH them, not against them.
5. **Only THEN form an opinion** — With full evidence, make your recommendation and cite the code that supports it.

Emit an `ANALYSIS_BASIS` block before any recommendation:

```
ANALYSIS_BASIS:
- Files read: [list of files examined]
- Call chain: [entry point → function → function → output]
- Dependencies: [what calls this, what this calls]
- Conventions observed: [patterns in use]
- Evidence for recommendation: [specific code references]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I can tell from the file structure what to recommend." | Structure suggests, code confirms. Read the code. |
| "This is a standard pattern, no need to check." | Standard patterns have non-standard implementations. Read the code. |
| "I read a similar codebase recently." | Similar codebases have different conventions. Read THIS code. |
| "The user described the architecture." | User descriptions omit edge cases and hidden coupling. Read the code. |
| "Reading all callers takes too long." | Missing a caller means your recommendation breaks something. Read them. |

## Red Flags (self-check)

- You are recommending changes to code you haven't read
- You are citing file paths or function names from memory rather than from a read
- Your recommendation doesn't reference specific lines or functions
- You don't know what other code calls the function you're suggesting to change
- You are recommending a pattern that contradicts the codebase's existing conventions

## Hard Gate

Recommendations are INVALID if they are not backed by an ANALYSIS_BASIS block citing specific files read and code evidence. Architecture opinions without code evidence are noise.
