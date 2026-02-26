# Finding Documentation

## Rule (non-negotiable)

Every security finding, vulnerability, or misconfiguration discovered during an audit MUST be documented as a structured FINDING_CARD. Narrative descriptions without structured documentation are invalid findings.

## When This Fires

- Any vulnerability is discovered during scanning
- Any misconfiguration is identified
- Any security concern is noted (open ports, weak ciphers, missing auth, default credentials)
- Generating the audit report

## FINDING_CARD Format

For every finding, emit:

```
FINDING_CARD:
- ID: F-[sequential number]
- Severity: CRITICAL / HIGH / MEDIUM / LOW / INFO
- Category: [auth | encryption | exposure | protocol | config | firmware | network]
- Host: [IP address and/or hostname]
- Port/Service: [port number and service name]
- Finding: [one-line summary]
- Evidence: [exact command output or probe result that proves this finding]
- Risk: [what could happen if this is exploited]
- Remediation: [specific action to fix this]
- CVE: [CVE ID if applicable, or "N/A"]
- Verified: [YES — finding confirmed by probe / NO — inferred from service banner]
```

## Severity Classification

| Severity | Criteria |
|---|---|
| CRITICAL | Remote code execution, default credentials on externally reachable service, unpatched CVE with known exploit |
| HIGH | Unauthenticated access to sensitive service, weak/no encryption on credential-bearing protocol, UPnP/SSDP exposed to WAN |
| MEDIUM | Outdated TLS versions, weak cipher suites, unnecessary open ports, missing security headers |
| LOW | Information disclosure (service banners, version strings), mDNS leaking hostnames |
| INFO | Informational observation (e.g., device correctly configured, service responding as expected) |

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "It's just an open port, not a real finding." | Open ports expand attack surface. Document it. |
| "This is a home network, severity doesn't matter." | Home networks contain personal data, financial accounts, and IoT devices. Severity always matters. |
| "I'll summarize the findings later in the report." | Findings must be structured NOW, when the evidence is fresh. |
| "Too many findings, I'll just list the important ones." | Every finding gets a FINDING_CARD. Filter by severity in the report, not during documentation. |
| "The evidence is obvious from the scan." | Obvious to you now is opaque in the report. Include the exact evidence. |

## Red Flags (self-check)

- You discovered a vulnerability but described it in narrative text without a FINDING_CARD
- Your FINDING_CARD has empty Evidence or Remediation fields
- You are assigning severity based on feeling rather than the classification table
- You found >5 issues on a host but only documented 2-3
- Your finding says "appears to be" or "might be" instead of citing evidence

## Hard Gate

The audit report is INVALID if any finding is described in narrative text without a corresponding FINDING_CARD. All findings must be structured. The report generator consumes FINDING_CARDs — unstructured findings will be lost.
