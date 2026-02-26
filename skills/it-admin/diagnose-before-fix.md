# Diagnose Before Fix

## Rule (non-negotiable)

Before making ANY change to a system, service, or configuration, you MUST diagnose the current state and identify the root cause. Guessing and fixing are separate steps — never combine them.

## When This Fires

- Any service is down or misbehaving
- A user reports a problem with infrastructure
- You are about to modify a config file, firewall rule, service, or network setting
- Any troubleshooting request

## The Diagnostic Sequence

1. **Gather state** — Collect logs, check service status, inspect configuration. Use read-only commands first.
2. **Identify symptoms** — List what is actually observed (not assumed). Include timestamps.
3. **Form hypothesis** — Based on evidence, propose the most likely root cause.
4. **Verify hypothesis** — Run a targeted check that confirms or refutes the hypothesis BEFORE applying any fix.
5. **Only THEN fix** — Apply the minimum change that addresses the confirmed root cause.

Emit a `DIAGNOSIS` block before any fix:

```
DIAGNOSIS:
- Symptoms: [what was observed]
- Evidence: [logs, status output, error messages]
- Root cause: [confirmed or probable cause]
- Confidence: HIGH / MEDIUM / LOW
- Fix plan: [specific change to apply]
- Rollback: [how to undo if the fix fails]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "I already know what's wrong." | Knowing is not diagnosing. Show the evidence. |
| "Restarting the service will fix it." | Restart masks the root cause. Diagnose first. |
| "It's a simple config change." | Simple changes break things. Check current state first. |
| "The user told me what to fix." | Users report symptoms, not root causes. Verify. |
| "I'll diagnose after I try a quick fix." | Quick fixes without diagnosis compound failures. |

## Red Flags (self-check)

- You are about to run `systemctl restart` or `pkill` without checking logs first
- You are modifying a config file without reading it first
- You have no `DIAGNOSIS` block in your response before a fix
- You are applying a fix to a different symptom than what was reported
- You cannot explain WHY the fix will work

## Hard Gate

Any config change, service restart, or infrastructure modification is BLOCKED until a DIAGNOSIS block is present with evidence. Fixes without diagnosis are invalid.
