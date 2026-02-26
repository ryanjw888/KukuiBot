# Evidence Anchoring

## Rule (non-negotiable)

Every claim, assessment, or finding in your analysis MUST be anchored to specific evidence from the codebase. Claims without code references are assertions, not analysis. Assertions are invalid output.

## When This Fires

- Any code review or quality assessment
- Identifying technical debt or anti-patterns
- Security analysis or vulnerability assessment
- Performance analysis or scalability concerns
- Any statement about code quality, structure, or correctness

## Evidence Requirements

For every analytical claim:

1. **Cite the file and line** — `server.py:142` not "in the server code"
2. **Quote the relevant code** — Show the specific lines that support your claim
3. **Explain the connection** — State WHY this evidence supports your conclusion
4. **Quantify where possible** — "Called from 7 places" not "called from many places"

## Claim Categories and Evidence Standards

| Claim type | Required evidence |
|---|---|
| "This function has a bug" | Exact code path that triggers the bug, with inputs that cause it |
| "This is technical debt" | The specific code, what pattern it should use, and what breaks if left unfixed |
| "This is a security risk" | The vulnerable code path, what an attacker could do, and how to trigger it |
| "This doesn't scale" | The specific O(n) or resource concern, with current and projected numbers |
| "This pattern is wrong" | The pattern in use, the correct pattern, and what specifically breaks |
| "This dependency is risky" | Version, known CVEs or maintenance status, and what it's used for |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "It's obvious from looking at the code." | Obvious to you is opaque to the reader. Cite the evidence. |
| "I already explained this." | Explanations without citations are opinions. Anchor to code. |
| "The evidence would make the analysis too long." | Long analysis with evidence beats short analysis without it. Include it. |
| "Everyone knows this pattern is bad." | Not everyone has the same context. Show WHY in THIS code. |
| "I'll add references in the final version." | Adding references later means adding them from memory. Cite now. |

## Red Flags (self-check)

- Your analysis contains "seems to," "appears to," or "probably" without a follow-up check
- You are making claims about code you haven't read or quoted
- Your findings have no file:line references
- You describe a pattern as "common" or "standard" without showing it in the actual code
- Your security assessment lists generic vulnerabilities without mapping them to specific code paths

## Hard Gate

Analysis output is INVALID if claims lack specific file:line references and code evidence. Every finding, recommendation, and assessment must be traceable to the source code. Unanchored claims are rejected.
