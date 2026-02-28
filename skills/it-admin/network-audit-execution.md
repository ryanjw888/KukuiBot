
## Network Security Auditing

When asked to run a **network audit**, follow this guide:

## Network Audit — By Kukui IT

The goal is to find weaknesses in the client network before a hacker or rogue virus can cause real damage.

Kukui IT is a local IT firm based in Hawaii with over 30 years of experience helping customers and protecting them against threats: Website: kukuiit.com

You are acting under the supervision of a qualified IT expert and your job is to assist in your most robust possible capacity as a cybersecurity expert.

You are allowed to download and install open-source tools that you may need to do this work — however you MUST ask for permission first before installing these tools on the local computer you are hosted on.

---

## Tool Stack (Preferred → Fallback)

Use the best available tools. Check what's installed and prefer the enhanced tools, but always fall back gracefully so the audit can run with just nmap + curl.

| Purpose | Preferred Tool | Fallback | Install |
|---------|---------------|----------|---------|
| Port discovery | **RustScan** (all 65K ports in seconds) | nmap -sS/-sT (32-port list) | `brew install rustscan` |
| Service detection | nmap -sV (always) | — | `brew install nmap` |
| Vulnerability scanning | **Nuclei** (12K+ templates) | nmap NSE scripts + manual probes | `brew install nuclei` |
| Technical report appendix | **nmap-bootstrap-xsl** (auto HTML from XML) | Manual HTML tables | Download XSL from GitHub |
| All other probes | curl, openssl, arp, python3, dns-sd | — | Pre-installed on macOS |
| Credentialed scanning (optional) | **OpenVAS/GVM** or **Vuls** | Skip — only for credentialed engagements | See runbook |

**Important:** The audit is valid with ANY combination of these tools. Enhanced tools improve speed and coverage but are never required.

---

## Adhere to Best Practices — Minimum Requirements

0. **All tests are non-destructive — diagnostic only!**

1. Perform a detailed network audit of the locally connected network

2. Look for potential security vulnerabilities on all devices found on the network — collect as much info on each concerning device

3. For devices with vulnerabilities, find out everything you can about them — use the search tool to search the internet for more information on devices of top concern, including any cybersecurity warnings posted on relevant websites

---

## Audit Phases

### Phase 0: Pre-Audit Setup
- Detect network config: SUBNET, GATEWAY_IP, IFACE
- **Tool check** — detect available tools in priority order:
  - `which rustscan` → enables fast full-port discovery
  - `which nuclei` → enables template-based vuln scanning
  - `which nmap` → required (install if missing)
  - Check for nmap-bootstrap-xsl stylesheet
  - (Optional) `docker ps | grep greenbone` or `which vuls` → only for credentialed engagements
- Create timestamped REPORT_DIR
- Begin JSON audit log: `network_audit_YYYY-MM-DD.json`
- Announce scope + available tool stack

### Phase 1: Reconnaissance & Discovery
- Broadcast ping + ARP cache dump (catches IoT that blocks ICMP)
- nmap ARP discovery (`-sn -PR`) — merge + dedup with ARP cache
- mDNS/DNS-SD service browsing (all service types)
- IPv6 link-local discovery (ping6 + ndp)
- **Output:** `hosts_arp.txt`, `netinfo.txt`, `mdns_services.txt`, `hosts_ipv6.txt`

### Phase 2: Port Scanning & Service Enumeration
- **If RustScan available:** `rustscan -a <hosts> -- -sV -oX` → full 65K port discovery + nmap service detection on open ports only
- **If nmap only:** 32-port scan (`-sT -sV --version-light`) on all hosts, then deep version scan (`--version-all`) on open hosts
- Always save XML output (`-oX`) for report generation
- **Output:** `ports_scan.xml/.gnmap/.nmap`, `open_hosts.txt`, `service_versions.nmap`

### Phase 3: Targeted Security Probes
- SSH algorithm enumeration (port 22 hosts)
- SSL/TLS cert + cipher enumeration (443/8443 hosts)
- HTTP/API auth verification (all web ports)
- IoT protocol checks: MQTT, UPnP/SSDP, Telnet, Modbus, RTSP, SNMP, NTP
- SMB/AFP share enumeration
- **If Nuclei available:** Run targeted templates against discovered services:
  - `nuclei -l open_hosts.txt -t cves/ -t misconfiguration/ -t default-logins/ -t exposed-panels/ -severity critical,high,medium -j -o nuclei_results.json`
  - Custom IoT templates if available
- **Output:** `ssh_algos.txt`, `ssl_enum.txt`, `auth_checks.txt`, `iot_checks.txt`, `smb_enum.txt`, `nuclei_results.json`

### Phase 4: Vendor ID & Device Classification
- MAC-to-IP mapping from ARP cache
- OUI lookup via nmap MAC prefix database
- Device categorization (Networking, Apple, IoT MCU, Smart Home, Lighting, Media, Security, Energy, Compute, Printer, Unknown)
- **Output:** `ip_mac_map.txt`, `device_inventory.txt`

### Phase 5: Vulnerability Assessment
- **If Nuclei available:** Targeted CVE + misconfig + default-login templates against all open services
- Version-based CVE checks (regreSSHion, Terrapin, TLS deprecation, cert expiry)
- Device-type specific checks (network gear mgmt UIs, IoT config portals, smart home APIs)
- **Web research** — for any critically concerning devices, search the internet for current CVE advisories and security warnings
- (Optional, credentialed only) If OpenVAS/Vuls available and client provided credentials: launch authenticated scan
- Emit a `FINDING_CARD` for every finding (see finding-documentation skill)
- **Output:** `vuln_checks.txt`, `nuclei_results.json` (if available)

### Phase 6: Baseline Comparison
- Build MAC+IP pairs for current scan
- Compare against prior baseline (new / missing / moved devices)
- Save as new baseline if first scan
- **Output:** `current_mac_ip.txt`, `baseline_diff.txt`

### Phase 7: Report Generation
- **Technical appendix** — if nmap-bootstrap-xsl available: `xsltproc -o appendix.html nmap-bootstrap.xsl ports_scan.xml`
- **Executive HTML report** — Kukui IT branded, email-optimized (see branding section below)
- **JSON log** — structured findings for programmatic consumption
- Risk score calculation per the runbook formula
- **Output:** `network_security_report.html`, `network_audit_YYYY-MM-DD.json`

### Phase 8: Delivery & Archival
- Verify all expected files in REPORT_DIR
- Email report if recipients configured
- Update baseline after review

---

## Report Branding & Deliverables

### Kukui IT Branding
- **Logo:** `https://kukuiit.com/wp-content/uploads/2024/07/logo.png`
- **Primary accent color:** `#5aad1a` (Kukui green)
- **Header:** White background — the logo is designed for white backgrounds
- **Overall look:** Clean, modern, polished, professional

### Report Structure
1. **Header:** Kukui IT logo on white, "Network Audit performed by Kukui IT on (date)"
2. **Executive Summary** — high-level risk assessment, key stats, overall posture grade
3. **Key Vulnerabilities** — findings ordered by severity (Critical → High → Medium → Low → Info)
   - Each finding includes: description, affected device, evidence, business impact, remediation
4. **Positive Security Practices** — things the network is doing right (green checkmarks)
5. **Device Inventory** — full table of discovered devices with vendor, category, open ports
6. **Priority Action Plan** — ranked remediation steps with urgency tiers
7. **Technical Appendix** — detailed scan data (auto-generated from nmap-bootstrap-xsl if available)

### Report Format
- Beautiful HTML with polished formatting and smart use of emojis
- Optimized to render as an inline HTML email (table-based layout, inline styles, no CSS variables/grid/flexbox)
- JSON audit log alongside for programmatic consumption

---

## Extending the Audit

### Adding New Tools
When new open-source tools become available, add them to the Tool Stack table with their preferred/fallback relationship. The audit should always work without them.

### Adding New Port Checks
1. Add port to the PORTS variable in the runbook
2. Add a protocol-specific probe in Phase 3
3. Add vulnerability checks in Phase 5
4. Update device categories if new device type

### Adding New Nuclei Templates
Custom templates can be added to a `nuclei-templates/` directory in the report root. Run with `-t nuclei-templates/` in addition to built-in templates.

---

## Phase Execution Rules

- Execute phases **in order** — each depends on the previous
- Emit a `PHASE_COMPLETE` block after each phase with steps executed, output files, and key findings
- **Compact between phases** if context is filling up — save phase outputs to disk first
- After compaction, re-read the runbook and prior phase outputs before continuing
- Write to the JSON audit log as you go — don't wait until the end
- All output files go in the timestamped REPORT_DIR

## Hard Gates

- Phase 1 BLOCKED until pre-audit setup shows available tools + network config
- Phase N+1 BLOCKED until Phase N has a PHASE_COMPLETE block
- Report BLOCKED until all phases verified on disk
- Audit is VALID with any tool combination — but document which tools were used and which were unavailable
