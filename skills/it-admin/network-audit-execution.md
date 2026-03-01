
## Network Security Auditing

When asked to run a **network audit**, follow this guide:

## Network Audit — By Kukui IT

The goal is to find weaknesses in the client network before a hacker or rogue virus can cause real damage.

Kukui IT is a local IT firm based in Hawaii with over 30 years of experience helping customers and protecting them against threats: Website: kukuiit.com

You are acting under the supervision of a qualified IT expert and your job is to assist in your most robust possible capacity as a cybersecurity expert.

You are allowed to download and install open-source tools that you may need to do this work — however you MUST ask for permission first before installing these tools on the local computer you are hosted on.

---

Network Audit — Kukui IT
Find weaknesses in a client network before an attacker or rogue malware can exploit them. You are assisting a qualified IT professional as a cybersecurity expert.

Required Tools
nmap, RustScan, Nuclei, nmap-bootstrap-xsl. Install missing tools only with permission.

Phases
0 — Pre-Audit Setup
Detect network config (subnet, gateway, interface)
Create timestamped report directory + JSON log

1 — Reconnaissance & Discovery
ARP cache + broadcast ping sweep (best for IoT discovery)
RustScan top ~1000 ports (--top flag, 500ms timeout, batch 512) on live hosts only
Fallback: nmap -sS --top-ports 1000 -T4 if RustScan unavailable
mDNS/DNS-SD service browsing
IPv6 link-local discovery
Important: Only scan hosts confirmed alive by ARP/ping sweep. Skip dark hosts.

2 — Port Scanning & Service Enumeration
If RustScan ran: nmap -sV -sC on discovered open ports only (no full range re-scan)
If nmap-only: results from Phase 1 already include service info
Save XML output for nmap-bootstrap-xsl report generation
Timeout: per-host 90s max. Skip unresponsive hosts after 2 retries.

3 — Targeted Security Probes
Protocol-specific probes: SSH algos, TLS certs, HTTP auth, IoT protocols, SMB/AFP
Nuclei templates: known CVEs, default creds, exposed panels, misconfigs
Fallback: nmap NSE scripts if Nuclei unavailable

4 — Vendor ID & Device Classification
OUI lookup + device categorization

5 — Vulnerability Assessment
Nuclei targeted templates for IoT-specific CVEs
Manual version-based checks as supplement
Web search for current advisories on top-concern devices

---

## Report Branding & Deliverables

### Kukui IT Branding
- **Logo:** `https://kukuiit.com/wp-content/uploads/2024/07/logo.png` — **IMPORTANT:** the img tag MUST include `referrerpolicy="no-referrer"` to bypass hotlink protection (e.g. `<img src="https://kukuiit.com/wp-content/uploads/2024/07/logo.png" alt="Kukui IT" referrerpolicy="no-referrer" />`)
- **Primary accent color:** `#5aad1a` (Kukui green)
- **Header:** White background — the logo is designed for white backgrounds
- **Overall look:** Clean, modern, polished, professional

### Report Structure
1. **Header:** Kukui IT logo on white, "Network Audit performed by Kukui IT on (date)"
2. **Executive Summary** — high-level risk assessment, key stats, overall posture grade
3. **Key Vulnerabilities** — findings ordered by severity (Critical -> High -> Medium -> Low -> Info)
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

## Efficiency Rules

- **One nmap pass, not four.** Merge service detection + SSH + TLS + banner scripts into a single Phase 2 scan.
- **Sudo everything.** Never waste time discovering client isolation mid-audit. Start with `sudo nmap` from Phase 2 onward.
- **Test reachability once.** The Phase 1 reachability pre-check tells you which tools work on which hosts. Don't rediscover this in Phase 3.
- **Skip unreachable Nuclei targets.** If reachability check shows client isolation, only feed the gateway + local machine to Nuclei.
- **Version-light always.** Only escalate to `--version-all` for specific mystery services, never as a default.
- **Update JSON log per-phase.** Don't leave it empty until the end. Write findings and phase metadata after each phase completes.

