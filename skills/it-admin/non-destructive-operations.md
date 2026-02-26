# Non-Destructive Operations

## Rule (non-negotiable)

All infrastructure operations default to non-destructive. Before any change that modifies state, you MUST have a backup and a rollback plan. Scanning and auditing MUST use read-only probes exclusively.

## When This Fires

- Any network scanning or security auditing
- Modifying configuration files (firewall, DNS, DHCP, services)
- Restarting services or changing system state
- Docker operations (stop, rm, volume changes)
- Database schema changes or data modifications

## For Audits and Scans

These operations are ALWAYS non-destructive:
- Port scanning (SYN scan, connect scan) — read-only
- Service banner grabbing — read-only
- TLS certificate inspection — read-only
- SSH algorithm enumeration — read-only
- mDNS/DNS-SD discovery — read-only
- ARP table inspection — read-only
- SNMP read community checks — read-only
- HTTP header inspection — read-only

These operations are NEVER performed during audits:
- Credential brute forcing or stuffing
- Exploit execution or payload delivery
- Service disruption (DoS, resource exhaustion)
- Configuration modification on target devices
- Writing to target filesystems
- Account creation on target systems

## For Configuration Changes

Before any config change:

```
CHANGE_PLAN:
- Target: [service/file being modified]
- Current state: [what it looks like now]
- Proposed change: [exactly what will change]
- Backup: [path to backup copy — MUST exist before change]
- Rollback command: [exact command to restore previous state]
- Verification: [how to confirm the change worked]
- Blast radius: [what else might be affected]
```

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "It's just a small config change, no backup needed." | Small changes cause big outages. Backup first. |
| "I can recreate the config from memory." | Memory is unreliable. Create a backup file. |
| "The scan is passive, nothing can go wrong." | Use -sS (SYN) not -sT (connect) where possible. Minimize footprint. |
| "I need to test if the exploit works." | Audits are non-destructive. Document the vulnerability, don't test the exploit. |
| "I'll back up after I make the change." | A backup after the change is not a backup. Copy BEFORE modifying. |

## Red Flags (self-check)

- You are about to modify a config file and there is no `cp original.conf original.conf.bak` in your plan
- You are running nmap with -sV --script=exploit or any destructive NSE script
- Your CHANGE_PLAN has no rollback command
- You are about to `docker rm` a container without backing up its volumes
- You are modifying a production database without a backup

## Hard Gate

Any operation that modifies system state is BLOCKED until a CHANGE_PLAN block is present with a verified backup path. Audit operations that include exploitation or credential stuffing are ALWAYS BLOCKED — no exceptions.
