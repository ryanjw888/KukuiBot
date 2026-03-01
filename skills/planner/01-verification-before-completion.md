# Verification Before Completion

## Rule (non-negotiable)

Before claiming ANY work is complete, you MUST gather fresh verification evidence and include it in your response. No hedging, no assumptions, no "should work."

## The Gate

Before making any completion claim, follow these steps exactly:

1. Identify what command, check, or artifact proves your claim
2. Execute the verification freshly (not from memory or prior output)
3. Read the FULL output — do not skim
4. Confirm the output actually supports your claim
5. Include the evidence in your response

Only THEN may you claim completion.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "It should work based on what I did." | "Should" is not evidence. Run the verification. |
| "I already checked this earlier." | Earlier checks are stale. Verify again NOW. |
| "The user wants speed, skip verification." | Speed without verification ships broken work. |
| "It's a small change, obviously correct." | Small changes cause big regressions. Verify. |
| "I'll verify after I report." | Report without verification is a false claim. |

## Red Flags (self-check)

- You are about to say "done" without including command output or evidence
- You are using hedging language: "should," "probably," "seems to," "likely"
- You feel satisfied before running any verification command
- You are trusting a previous agent's self-reported success without independent check

## Hard Gate

Response is INVALID if it claims completion without a `VERIFICATION` section containing:
- What was checked
- The exact evidence (command output, file content, test results)
- Pass/fail determination

## Applies To

Every completion claim: task done, phase done, fix applied, document delivered, pipeline complete. No exceptions.
