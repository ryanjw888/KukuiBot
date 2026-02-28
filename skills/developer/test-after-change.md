# Test After Change

## Rule (non-negotiable)

After EVERY code change, you MUST verify that it works before declaring it done. "It should work" is not verification. Run the code, check the output, confirm the behavior.

## When This Fires

- After any code edit (bug fix, feature, refactor)
- After any configuration change
- After any dependency change
- Before committing code
- Before reporting a task as complete

## Verification Methods (in order of preference)

1. **Run the test suite** — If tests exist for the changed code, run them.
2. **Curl the endpoint** — For API/server changes, make a request and inspect the response.
3. **Run the script** — For standalone scripts, execute and check output.
4. **Check the logs** — After restarting a service, verify it started without errors.
5. **Visual inspection** — For UI changes, load the page and verify rendering.

## What to Verify

- The fix/feature works as intended (happy path)
- The change doesn't break existing functionality (regression check)
- Edge cases identified during development are handled
- Error cases produce sensible behavior (not crashes or blank responses)

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "The code is obviously correct." | Obvious code has non-obvious bugs. Test it. |
| "I'll test it all together at the end." | End-to-end testing after many changes makes failures impossible to isolate. Test after each change. |
| "The change is too small to break anything." | Small changes cause the most surprising failures. Test it. |
| "Testing this requires restarting the server." | Then restart the server. Untested code is worse than slow testing. |
| "I already tested something similar." | Similar is not identical. Test THIS change. |

## Red Flags (self-check)

- You are about to commit without running any verification
- You said "done" but your response contains no test output or evidence
- You are stacking multiple edits before testing any of them
- You are using hedging language: "should work," "probably fine," "looks correct"
- You assume a server restart is unnecessary after a code change

## Hard Gate

No code change is COMPLETE until verification evidence is included in the response. "Done" without test output is invalid. Commits without verification are blocked.
