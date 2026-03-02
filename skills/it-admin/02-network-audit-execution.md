
## Network Security Auditing

When asked to run a **network audit**, follow these steps in order. Do NOT skip steps.

---

## Step 1: Discover Networks and Ask User Which to Scan

Before scanning anything, show the user what networks are available and let them choose.

```bash
python3 src/tools/network-audit/list_networks.py
```

This outputs a numbered list of connected networks with interface, IP, subnet, and gateway info. Example:

```
Connected Networks:
----------------------------------------------------------------------
  1. Ethernet [DEFAULT]
     Type: Ethernet
     Interface: en0  IP: 192.168.20.110
     Subnet: 192.168.20.0/23  GW: 192.168.20.1

  2. Wi-Fi (OfficeNet)
     Type: Wi-Fi  (SSID: OfficeNet)
     Interface: en1  IP: 10.0.1.45
     Subnet: 10.0.1.0/24  GW: 10.0.1.1
```

**Present the networks and offer smart defaults with a one-word confirmation option.**

Pick defaults automatically:
- **Network:** If only one network is found, default to it. If multiple, default to the one marked `[DEFAULT]`.
- **Client name:** Default to `"Network Security Audit"` (generic). If you know the client/site name from context, use that instead.
- **Scan type:** Default to `"Full audit"`.

**Format your prompt like this:**

> Here's what I'll scan:
>
> - **Network:** Ethernet — 192.168.20.0/23 (the only connected network)
> - **Client name:** "Wilmot Residence"
> - **Scan type:** Full comprehensive audit
>
> **Continue with these defaults?** Or let me know if you'd like to change anything.

This lets the user just say "yes" / "go" / "looks good" to proceed immediately, while still giving them the chance to override any of the three settings.

**Wait for their response before proceeding.** Do NOT auto-start the scan without confirmation.

For the JSON version (if you need to parse it programmatically):
```bash
python3 src/tools/network-audit/list_networks.py --json
```

---

## Step 2: Run the Automated Scanner (Background + Polling)

**CRITICAL: Network audits take 5-30 minutes. You MUST run the scanner in the background
and poll for progress. NEVER run it as a blocking `bash` call or you WILL time out.**

### 2a. Start the scan in the background

Use `bash_background` to launch the audit:

```
Tool: bash_background
Command: python3 src/tools/network-audit/audit.py --subnet <SELECTED_SUBNET> --client "<CLIENT_NAME>"
```

This returns a **process ID** (e.g., `a1b2c3d4`). Save this ID — you need it for polling.

Options:
- `--subnet` — The CIDR from the user's chosen network (e.g., `192.168.1.0/24`)
- `--client` — Client name for the report header
- `--no-sudo` — If the user wants an unprivileged scan (limited quality)
- `--output` — Custom output directory (default: `~/.kukuibot/reports/network-audits/<timestamp>/`)

### 2b. Poll for progress in a loop

Use `bash_check` to poll for new output. **Repeat until status is `done`:**

```
Tool: bash_check
id: <PROCESS_ID>
action: poll
wait_seconds: 15
```

This waits 15 seconds, then returns:
- `[running] elapsed=45.2s` + any new stdout lines since last poll
- `[done] elapsed=312.5s exit_code=0` when the scan finishes

### 2c. Keep the user informed during polling

**Every time you poll**, read the new output and tell the user what's happening. Examples:

- "Setting up... detected 3 tools (nmap, rustscan, nuclei)"
- "Discovery complete - found 24 live hosts on the network"
- "Port scanning 24 hosts in parallel..."
- "Phases 3, 4, and 5 running in parallel (probes + classification + vuln assessment)..."
- "Scan complete! Analyzing results..."

### 2d. Polling loop pseudocode

```
1. Call bash_background → get PROC_ID
2. Tell user: "Scan started, I'll keep you posted..."
3. LOOP:
   a. Call bash_check(id=PROC_ID, action=poll, wait_seconds=15)
   b. Read new output lines
   c. Relay phase completions and stats to user
   d. If status is "running" → go to 3a
   e. If status is "done" → break
4. Note the output directory from Phase 0 output
5. Proceed to Step 3
```

### 2e. Finding the output directory

The audit tool prints the output path in Phase 0:
```
[Phase 0] Setup complete in 0.2s
  Output:    /Users/jarvis/.kukuibot/reports/network-audits/20260301-143022/
```

Save this path for Steps 3-5. The scan results will be at `<OUTPUT_DIR>/scan_results.json`.

You can also read the progress file for structured status:
```
Tool: read_file
path: <OUTPUT_DIR>/progress.json
```

The progress file contains:
```json
{
  "status": "running|completed|failed",
  "current_phase": 2,
  "current_phase_name": "Port Scanning",
  "total_hosts": 24,
  "total_ports": 156,
  "total_vulns": 3,
  "message": "Phase 2: Port Scanning...",
  "elapsed_seconds": 142.5,
  "scan_results": "/path/to/scan_results.json"
}
```

### Phase overview

The tool runs 6 phases automatically:
- Phase 0: Setup (detect network config, check tools)
- Phase 1: Discovery (ARP, ping sweep, mDNS, IPv6 — in parallel)
- Phase 2: Port scanning (RustScan + nmap service enum — per-host parallel)
- Phases 3+4+5 run IN PARALLEL:
  - Phase 3: Security probes (SSH, TLS, HTTP, SMB, AFP + Nuclei)
  - Phase 4: Device classification (OUI vendor lookup + heuristics)
  - Phase 5: Vulnerability assessment (Nuclei CVEs + version checks)

### Timing expectations

- Small network (< 20 hosts): 5-10 minutes
- Medium network (20-100 hosts): 10-20 minutes
- Large network (100+ hosts): 20-30 minutes

---

## Step 3: Automated Analysis + Report Generation

The audit tool now **automatically** generates both `analysis.json` and the branded HTML report after the scan completes. The pipeline is:

```
scan → analyzer.py (auto-findings) → generator.py (HTML report)
```

When the scan finishes, the output directory will contain:
- `scan_results.json` — raw scan data
- `analysis.json` — auto-generated findings, positives, actions, and grade
- `network_security_report.html` — branded Kukui IT HTML report

### What the auto-analyzer detects

The `analyzer.py` module programmatically detects:
- **Telnet** (unencrypted, any device)
- **Legacy TLS** (1.0/1.1 on any port)
- **rpcbind** exposed (port 111)
- **Unauthenticated SOCKS proxies**
- **Unauthenticated IoT HTTP** (excludes Hue bridges which use physical-button auth)
- **CBC cipher suites** on non-standard ports
- **Weak SSH key exchange** (SHA-1 based KEX algorithms)
- **Positive practices**: post-quantum SSH, Grade-A TLS, TLS 1.3-only, Let's Encrypt certs, HTTP→HTTPS redirects

Similar findings on multiple devices are **automatically merged** (e.g., "rpcbind Exposed on 2 Devices" instead of 2 separate findings).

### AI review (optional)

After the scan completes, you MAY review and enhance the auto-generated `analysis.json`:
- Add business context the analyzer can't know (e.g., "this is a pool controller" vs "this is a server")
- Adjust severity based on network context
- Add findings the analyzer missed (e.g., outdated software versions, CVEs)
- Adjust the grade if the auto-grade doesn't match your expert assessment

If you edit `analysis.json`, re-render the report:
```bash
python3 src/tools/network-audit/audit.py --report \
  --scan-data <OUTPUT_DIR>/scan_results.json \
  --analysis <OUTPUT_DIR>/analysis.json \
  --output <OUTPUT_DIR>
```

### Grading scale

| Grade | Meaning |
|---|---|
| A+ / A | Excellent — no critical/high findings, strong practices |
| A- / B+ | Good — minor issues, generally well-secured |
| B / B- | Adequate — some medium findings, room for improvement |
| C+ / C | Concerning — high findings present, needs attention |
| C- / D+ | Poor — critical findings, significant risk |
| D / D- | Very poor — multiple critical issues, urgent action needed |
| F | Failing — severe, widespread vulnerabilities |

---

## Step 4: Report Branding & Design Guidelines

The report is auto-generated by `generator.py` using the analysis data. The following guidelines document the design system for reference and for any manual overrides.

### Branding

- **Logo:** `https://kukuiit.com/wp-content/uploads/2024/07/logo.png` (add `referrerpolicy="no-referrer"`)
- **Primary green:** `#5aad1a` (accent color, links, positive indicators)
- **Footer:** Kukui IT logo (same URL, smaller ~36px) · `Protecting Hawaii's Networks Since 1996` · [kukuiit.com](https://kukuiit.com)
- **Confidentiality notice** in footer with client name, generation date, and report ID

### Design System

Use a clean, modern card-based layout with these conventions:

**Colors & variables:**
```css
--green: #5aad1a;  --green-light: #e8f5d9;  --green-dark: #3d7a0e;
--red: #ef4444;    --orange: #f59e0b;        --blue: #3b82f6;
--bg: #f0f4f8;     --card: #ffffff;           --text: #1e293b;
```

**Typography:** Inter / system sans-serif stack. Monospace: SF Mono / Fira Code / Consolas.

**Cards:** White background, `border-radius: 16px`, subtle shadow (`0 4px 24px rgba(0,0,0,.06)`), 1px border `#e8ecf1`. Section headers use green bottom border with emoji icons.

**Hero header (animated, dark):**
- Dark gradient background with subtle radial glows
- Animated logo with glow effect (use `@keyframes logoGlow` + `drop-shadow`)
- Staggered fade-up entrance on all hero elements
- Green scanline accent bar at bottom
- Severity summary badges with frosted glass effect (use `backdrop-filter: blur`)
- Title format: `🔒 Network Security Audit Report`
- Subtitle: `<Client Name> — Comprehensive Vulnerability Assessment`
- Date pill: `🌴 Performed by Kukui IT — <Date> • <Subnet>`

**Responsive design — CRITICAL:**
All layouts must work on mobile (480px) through desktop. Include at minimum:
- `@media(max-width:768px)` — tablet breakpoint: tables get `overflow-x:auto`, grids collapse
- `@media(max-width:480px)` — mobile breakpoint: hero compacts, font sizes reduce, finding heads stack vertically, chip wrapping tightens
- Tables (inventory, action plan) must use `display:block;overflow-x:auto` on narrow screens
- Category chip containers: `flex-wrap:wrap` with smaller padding/font on mobile
- Test mentally: would a 375px wide iPhone render this without horizontal scroll?

### Report Sections (in order, but you may rearrange or merge as appropriate)

1. **Hero header** — Logo, title, subtitle, date pill, severity summary badges
2. **Security score** — SVG ring chart (score out of A+, stroke-dashoffset calculated), caption
3. **Stats row** — Pill cards: Hosts Scanned (only those with open ports), Open Ports, Findings, Positive. Do NOT show total ARP/ping-responsive IPs — only hosts with open ports matter.
4. **Executive summary** — 3-4 paragraphs, business language, mention key numbers and risk posture
5. **Security findings** — Each finding in a card with:
   - Severity badge row at top: `🔴 Critical` / `⚠ High` / `⚠ Medium` / `ℹ Low` / `ℹ Info`
   - Colored left border (5px, gradient matching severity)
   - Background tint matching severity
   - Detail grid (Host, Port, Category, Vendor, CVE) in a subtle inner panel
   - Evidence block (dark terminal style, green monospace text)
   - Risk note (subtle gray background with left border)
   - Remediation box (green background, `border-left: 4px solid green`)
   - Summary count badges at the top of the section
6. **Positive findings** — 2-column grid of green cards with `✅` emoji titles
7. **Device inventory** — Show only devices referenced in findings (notable devices), NOT every host on the network. Include category chips for just the shown devices, a note like "68 hosts scanned — request full inventory for details", and a table with IP, hostname, vendor, category tag, open ports, and a Finding column linking to the relevant finding ID. The full inventory can be provided on request.
8. **Prioritized action plan** — Table with urgency badges (Immediate/This Week/This Month/Quarterly), action description, affected hosts, effort estimate
9. **Scan methodology** — Definition list grid with scan tools, phases, timing, and target info
10. **Footer** — Kukui IT logo (small, centered), tagline, kukuiit.com link, and confidentiality notice

### Emoji Conventions

Use these consistently throughout the report:
- 🔒 Security / encryption related
- 🔴 Critical severity
- ⚠ High and Medium severity (⚠️ with variation selector)
- ℹ Low and Info severity
- ✅ Positive findings and good practices
- 🔧 Remediation steps
- 🌴 Kukui IT branding
- 🎯 Action plan
- 🔍 / 🔬 Methodology / investigation
- 📋 Executive summary
- 🚨 Findings header
- 🛡 Positive findings header
- 📡 Device inventory
- 🔑 / 🚀 / 📜 / 📷 / 🎵 / 🖥 / 🏠 — Specific positive finding categories
- Section headers: combine the category icon emoji with a descriptive emoji

### Print Styles

Include `@media print` with:
- White background, no hero animations
- Cards: no shadow, 1px solid border, `break-inside: avoid`
- Findings: `break-inside: avoid`

### Technical Notes

- All styles must be inline in `<style>` (no external CSS) — the report is email-safe and Gmail app compatible
- Use HTML entities for emojis where possible (e.g., `&#x1F512;` for 🔒)
- MAC addresses in evidence blocks should be partially masked (e.g., `XX:XX:XX:XX:B5:01`)
- No local file paths in the report body
- Save as: `<OUTPUT_DIR>/network_security_report.html`

---

## Step 5: Offer to Email the Report

After the report is generated, **ask the user if they'd like it emailed.**

Say something like:
> "The audit report is ready at `<path>/report.html`. Would you like me to email it to you?"

If they say yes, send it using the Gmail send-report API:

```bash
curl -s -X POST https://localhost:3456/api/gmail/send-report \
  -H "Content-Type: application/json" \
  --cacert /Users/jarvis/.kukuibot/src/certs/rootCA.pem \
  -d '{
    "to": "<USER_EMAIL>",
    "subject": "Network Security Audit Report — <CLIENT_NAME> — <DATE>",
    "html_path": "<ABSOLUTE_PATH_TO_REPORT_HTML>"
  }'
```

**Important:**
- The `html_path` must be an absolute path under `~/.kukuibot/`
- The report HTML is already email-safe (inline styles, table layout)
- Gmail permissions must allow sending to the recipient
- Use the subject format: `"Network Security Audit Report — <Client> — <Date>"`

If the user provides a different email address, use that. If they don't specify, use the authenticated Gmail address.

---

## Quick Reference: Full Tool Sequence

```
# 1. List networks (bash)
python3 src/tools/network-audit/list_networks.py

# 2. Start audit in background (bash_background)
python3 src/tools/network-audit/audit.py --subnet 192.168.1.0/24 --client "Client Name"
# → Runs scan → auto-analyze → auto-render report
# → Output: <OUTPUT_DIR>/scan_results.json, analysis.json, network_security_report.html

# 2b. Poll until done (bash_check — repeat in loop)
bash_check(id=PROC_ID, action=poll, wait_seconds=15)
# → Relay new output to user each cycle
# → Stop when status is "done"

# 3. (Optional) Review and enhance analysis.json, then re-render
python3 src/tools/network-audit/audit.py --report \
  --scan-data <OUTPUT_DIR>/scan_results.json \
  --analysis <OUTPUT_DIR>/analysis.json \
  --output <OUTPUT_DIR>

# 4. Email report if user wants (bash)
curl -sk -X POST https://localhost:7000/api/gmail/send-report \
  -H "Content-Type: application/json" \
  -d '{"to": "user@example.com", "subject": "Network Audit — Client — 2026-03-01", "html_path": "<OUTPUT_DIR>/network_security_report.html"}'
```

---

## Fallback: Manual Audit

If the automated tool is unavailable or fails, you can run phases manually:

0. Detect network: `route -n get default`, `ifconfig`
1. Discovery: `arp -a`, `nmap -sn <subnet>`, `dns-sd -B _services._dns-sd._udp local.`
2. Port scan: `sudo nmap -sS -sV -sC --top-ports 1000 -T4 -oX scan.xml <subnet>`
3. Probes: `nmap --script ssh2-enum-algos,ssl-cert,ssl-enum-ciphers,http-title ...`
4. Classify: OUI lookup from MAC addresses
5. Vulns: `nuclei -l targets.txt -t cves/ -severity critical,high -jsonl`

### Efficiency rules for manual mode:
- **One nmap pass, not four.** Merge service detection + scripts.
- **Sudo from Phase 2 onward.**
- **Only scan live hosts** from Phase 1.
- **90s per-host timeout.** Skip after 2 retries.
