# Network Audit Execution

## Rule (non-negotiable)

When running a network security audit, follow the 8-phase protocol exactly. Each phase has mandatory steps, required outputs, and completeness criteria. The runbook at `docs/NetworkAudit.md` is the authoritative reference — re-read it before every phase. Do NOT work from memory.

## When This Fires

- Any request to run a network audit, scan the network, security audit, or pentest
- Resuming an audit after compaction
- Any request involving device discovery, port scanning, or vulnerability assessment

## Pre-Audit Setup (MANDATORY)

Before Phase 1:
1. **Verify tools** — `which nmap curl arp openssl python3`. Install nmap if missing. Do NOT proceed without it.
2. **Configure** — Detect or ask: SUBNET, GATEWAY_IP, IFACE. Create timestamped REPORT_DIR. Use full 32-port list.
3. **Announce scope** — State subnet, gateway, interface, port count.

Emit `PRE_AUDIT:` block with tools, subnet, gateway, interface, report dir, port count, ready status.

**Hard gate:** Phase 1 BLOCKED until PRE_AUDIT shows "Ready: YES."

## Phase Completeness Requirements

### Phase 1: Reconnaissance
**Outputs:** `netinfo.txt`, `hosts_arp.txt`, `mdns_services.txt`, `hosts_ipv6.txt`
**Steps (ALL required):** local net info, ARP cache + broadcast ping, nmap ARP discovery (merge+dedup), mDNS/DNS-SD browsing (all service types), IPv6 link-local (ping6+ndp)
**Gate:** `hosts_arp.txt` ≥1 host or diagnose config error.

### Phase 2: Port Scanning
**Outputs:** `ports_scan.gnmap/.nmap/.xml`, `open_hosts.txt`, `service_versions.nmap`
**Steps:** Full 32-port scan (`-sT -sV --version-light`) on all hosts, extract open hosts, version deep scan (`--version-all`) on open hosts.
**Gate:** Must use ALL 32 ports. No reduced port lists.

### Phase 3: Targeted Probes
**Outputs (conditional):** `ssh_algos.txt` (port 22), `ssl_enum.txt` (443/8443), `auth_checks.txt` (HTTP hosts), `iot_checks.txt` (MQTT/UPnP/Telnet/Modbus/RTSP/SNMP/NTP), `smb_enum.txt`/`afp_enum.txt` (445/548)
**Required probes:** SSH algos, TLS certs+ciphers, HTTP auth check, MQTT anon auth, UPnP/SSDP+M-SEARCH, Telnet flag (CRITICAL), Modbus flag, RTSP methods, SNMP community, NTP monlist, SMB shares, AFP info.
**Gate:** Every probe with applicable hosts MUST run. Missing output for open ports = INCOMPLETE.

### Phase 4: Vendor ID
**Outputs:** `ip_mac_map.txt`, `device_inventory.txt`
**Gate:** Inventory count ≈ host count (±2).

### Phase 5: Vulnerability Assessment
**Outputs:** `vuln_checks.txt`
**Checks:** SSH CVEs (regreSSHion, Terrapin), SSH algo deprecation, TLS cert expiry, TLS version deprecation, device-type checks (networking gear, IoT MCU, Sonos, lighting, smart home).
**Gate:** Every finding severity-classified. Emit FINDING_CARD per finding.

### Phase 6: Baseline Comparison
**Outputs:** `current_mac_ip.txt`, `baseline_diff.txt` (or new baseline)
**Gate:** Report new/missing/moved device counts.

### Phase 7: Report Generation
**Outputs:** `network_security_report.md`, `network_security_report.html`
**Must include:** risk score (per runbook formula), stat cards, exec summary, findings by severity, positive controls, baseline diff, action plan, vendor overview, device inventory.

### Phase 8: Delivery
**Steps:** Verify all files in REPORT_DIR, list with sizes, email if configured, update baseline after review.

## Rationalization Resistance

| Avoidance thought | Required response |
|---|---|
| "Small network, only need a few phases." | Same vulnerability classes. All 8 phases. |
| "Already scanned recently." | Networks change daily. Full scan. |
| "Some probes won't find anything." | Probes CONFIRM nothing is there. Run them. |
| "I'll generate the report from memory." | Reports consume Phase 1-6 output files. |
| "IPv6/mDNS is unnecessary." | Dual-stack devices expose IPv6-only services. mDNS finds services, not just hosts. |
| "I'll scan fewer ports." | Full 32-port list. IoT uses non-standard ports. |

## Red Flags (self-check)

- nmap with <30 ports; skipped mDNS/IPv6; no `ssh_algos.txt` but port 22 hosts exist
- Report without Phase 5 vulns; not saving to REPORT_DIR; working from memory
- FINDING_CARDs missing evidence; skipped baseline ("first scan" still creates baseline)

## Hard Gate

Audit INVALID if any phase outputs missing. Phase N+1 BLOCKED until Phase N verified on disk. Report BLOCKED until all 8 PHASE_COMPLETE blocks exist.
