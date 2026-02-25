# Network Audit Runbook

**Version:** 2.0
**Author:** KukuiBot IT Admin Worker
**Last Updated:** 2026-02-20
**Classification:** Internal — Authorized Scanning Only

---

## Overview

This runbook defines a repeatable, multi-phase network security audit for home, lab, or small office environments. It is designed to be executed by KukuiBot's IT Admin worker using bash tools, but every command is documented so a human can run it manually.

**Design principles:**
- Generic and portable — subnet, ports, and device baselines are parameterized
- IoT-aware — covers protocols that traditional enterprise scanners miss (MQTT, Modbus, UPnP, mDNS)
- IPv4 + IPv6 — scans both address families to catch dual-stack devices
- Non-destructive — read-only probes, no exploitation, no credential stuffing
- Produces a self-contained HTML report suitable for email delivery

---

## Prerequisites

### Required Tools

| Tool | Purpose | Install |
|------|---------|---------|
| `nmap` | Host discovery, port scanning, service enumeration, NSE scripts | `brew install nmap` |
| `curl` | HTTP auth checks, API probing | Pre-installed on macOS |
| `arp` | ARP cache for passive host discovery | Pre-installed on macOS |
| `openssl` | TLS certificate inspection | Pre-installed on macOS |
| `python3` | OUI lookup, report generation | Pre-installed on macOS |

### Optional Tools

| Tool | Purpose | Install |
|------|---------|---------|
| `mosquitto_pub` | MQTT anonymous-auth testing | `brew install mosquitto` |
| `dns-sd` | mDNS/DNS-SD service enumeration | Pre-installed on macOS |

### Verify Installation

```bash
# Check nmap
which nmap || brew install nmap
nmap --version

# Check all others
which curl arp openssl python3

# Optional tools (non-fatal if missing)
which mosquitto_pub 2>/dev/null && echo "mosquitto: OK" || echo "mosquitto: not installed (MQTT auth test will be limited)"
which dns-sd 2>/dev/null && echo "dns-sd: OK" || echo "dns-sd: not installed (mDNS enumeration will be limited)"
```

---

## Configuration

All parameters are defined here. Override per-environment before execution.

```bash
# === NETWORK SCOPE ===
SUBNET="192.168.1.0/24"             # Target CIDR (supports /16 through /32)
GATEWAY_IP="192.168.1.1"            # Primary gateway for focused checks

# === INTERFACE ===
IFACE="en0"                         # Primary network interface (en0=WiFi, en1=Ethernet on some Macs)

# === OUTPUT ===
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
REPORT_DIR="$HOME/.kukuibot/reports/audit_${TIMESTAMP}"
mkdir -p "$REPORT_DIR"

# === PORT LIST (32 ports — service + IoT coverage) ===
# Standard services
#   21    FTP
#   22    SSH
#   23    Telnet (should never be open)
#   53    DNS
#   80    HTTP
#   81    Alternate HTTP (common on IoT cams)
#   88    Kerberos
#   111   RPC / portmapper
#   123   NTP
#   139   NetBIOS
#   161   SNMP
#   389   LDAP
#   443   HTTPS
#   445   SMB
#   548   AFP (Apple Filing Protocol)
#   554   RTSP (IP cameras)
#   631   IPP / CUPS
#   3389  RDP
#   5353  mDNS (Multicast DNS)
#   8080  HTTP alt / admin UIs
#   8443  HTTPS alt / UniFi
#   7000  KukuiBot / custom services
#   49152 UPnP media servers (common alternate)
#
# IoT-specific
#   502   Modbus TCP (industrial IoT)
#   1883  MQTT (unencrypted)
#   1900  UPnP / SSDP
#   2000  iKettle / smart appliances
#   4028  CGMiner API (crypto miners)
#   4840  OPC-UA (industrial IoT)
#   8883  MQTT TLS (encrypted)
#   9522  SMA Speedwire (solar inverters)

PORTS="21,22,23,53,80,81,88,111,123,139,161,389,443,445,502,548,554,631,1883,1900,2000,3389,4028,4840,5353,8080,8443,8883,7000,9522,49152"

# === TIMING ===
NMAP_TIMING="-T4"                  # T3=normal, T4=aggressive, T5=insane
SCAN_TIMEOUT="--host-timeout 30s"  # Per-host timeout for large subnets

# === EMAIL DELIVERY (optional) ===
REPORT_EMAIL=""                    # Set to recipient address, or leave empty to skip email delivery
KUKUIBOT_URL="https://localhost:7000"  # KukuiBot API base URL
```

---

## Phase 1: Environment Setup & Reconnaissance

**Goal:** Establish the scanning environment, determine local network topology, and build the initial host list via ARP, nmap, and mDNS service discovery.

### Step 1.1 — Collect Local Network Info

```bash
echo "=== Local Network Info ===" | tee "$REPORT_DIR/netinfo.txt"

# Interface, IP, subnet
ifconfig "$IFACE" | grep -E 'inet |ether' | tee -a "$REPORT_DIR/netinfo.txt"

# Default gateway
netstat -rn | grep default | head -1 | tee -a "$REPORT_DIR/netinfo.txt"

# DNS servers
scutil --dns | grep 'nameserver\[' | head -5 | tee -a "$REPORT_DIR/netinfo.txt"

# Public IP (for NAT context)
curl -s --max-time 5 https://ifconfig.me | tee -a "$REPORT_DIR/netinfo.txt"
echo ""
```

### Step 1.2 — ARP Cache Host Discovery

ARP discovery catches IoT devices that block ICMP ping (Tuya, Espressif, Meross, etc). This is the primary discovery method.

```bash
# Stimulate ARP cache — ping broadcast to wake up sleeping devices
BROADCAST=$(ifconfig "$IFACE" | grep 'broadcast' | awk '{print $NF}')
if [ -n "$BROADCAST" ]; then
  ping -c 2 -t 1 "$BROADCAST" > /dev/null 2>&1
fi
sleep 2

# Dump ARP cache
arp -a | grep -v incomplete | tee "$REPORT_DIR/arp_cache_raw.txt"

# Extract IPs
arp -a | grep -v incomplete | \
  awk -F'[() ]' '{print $2}' | \
  grep -E '^[0-9]+\.' | \
  sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | \
  tee "$REPORT_DIR/hosts_arp.txt"

echo "ARP hosts found: $(wc -l < "$REPORT_DIR/hosts_arp.txt")"
```

### Step 1.3 — Nmap ARP Discovery (Supplemental)

Catches any hosts the OS ARP cache missed. Only works on directly-connected subnets.

```bash
nmap -sn -PR \
  -oA "$REPORT_DIR/discovery_arp" \
  $SUBNET

# Merge with ARP cache list (deduplicate)
grep 'Host is up' "$REPORT_DIR/discovery_arp.nmap" | \
  grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' >> "$REPORT_DIR/hosts_arp.txt"

sort -t. -k1,1n -k2,2n -k3,3n -k4,4n -u "$REPORT_DIR/hosts_arp.txt" \
  -o "$REPORT_DIR/hosts_arp.txt"

TOTAL_HOSTS=$(wc -l < "$REPORT_DIR/hosts_arp.txt")
echo "Total unique hosts after merge: $TOTAL_HOSTS"
```

### Step 1.4 — mDNS / DNS-SD Service Discovery

Catches devices advertising services via Bonjour/Avahi (HomeKit, AirPlay, printers, Home Assistant, etc).

```bash
MDNS_FILE="$REPORT_DIR/mdns_services.txt"
echo "=== mDNS/DNS-SD Service Discovery ===" > "$MDNS_FILE"

# Common service types to browse
MDNS_SERVICES=(
  "_http._tcp"
  "_https._tcp"
  "_ssh._tcp"
  "_smb._tcp"
  "_afpovertcp._tcp"
  "_airplay._tcp"
  "_raop._tcp"
  "_homekit._tcp"
  "_hap._tcp"
  "_ipp._tcp"
  "_printer._tcp"
  "_companion-link._tcp"
  "_mqtt._tcp"
  "_home-assistant._tcp"
  "_sonos._tcp"
  "_spotify-connect._tcp"
  "_googlecast._tcp"
)

for SVC in "${MDNS_SERVICES[@]}"; do
  echo "--- $SVC ---" >> "$MDNS_FILE"
  # dns-sd -B runs indefinitely, so timeout after 3 seconds per service type
  timeout 3 dns-sd -B "$SVC" local. 2>/dev/null >> "$MDNS_FILE" || true
done

# Also try nmap mDNS enumeration
nmap -sU -p5353 --script dns-service-discovery \
  -oN "$REPORT_DIR/mdns_nmap.txt" \
  "$GATEWAY_IP" 2>/dev/null || true

echo "mDNS discovery complete. See: $MDNS_FILE"
```

### Step 1.5 — IPv6 Link-Local Discovery

Discovers dual-stack and IPv6-only devices on the local segment.

```bash
IPV6_FILE="$REPORT_DIR/hosts_ipv6.txt"
echo "=== IPv6 Link-Local Discovery ===" > "$IPV6_FILE"

# Ping all-nodes multicast to populate neighbor cache
ping6 -c 3 -I "$IFACE" ff02::1 > /dev/null 2>&1
sleep 1

# Dump IPv6 neighbor cache
ndp -an 2>/dev/null | grep -v permanent | tee -a "$IPV6_FILE"

# Also use nmap for IPv6 link-local discovery
nmap -6 -sn --script ipv6-multicast-mld-list \
  -e "$IFACE" \
  -oN "$REPORT_DIR/ipv6_discovery.txt" \
  ff02::1%"$IFACE" 2>/dev/null || true

# Count unique IPv6 hosts
IPV6_COUNT=$(grep -cE 'fe80::' "$IPV6_FILE" 2>/dev/null || echo 0)
echo "IPv6 link-local hosts found: $IPV6_COUNT"
```

---

## Phase 2: Port Scanning & Service Enumeration

**Goal:** Identify open ports and running services on all discovered hosts.

### Step 2.1 — Full Port Scan (32 Ports)

```bash
nmap -Pn -n $NMAP_TIMING -sT -sV --version-light --open \
  $SCAN_TIMEOUT \
  -p $PORTS \
  -iL "$REPORT_DIR/hosts_arp.txt" \
  -oA "$REPORT_DIR/ports_scan"

# Extract hosts with at least one open port
grep 'open' "$REPORT_DIR/ports_scan.gnmap" | \
  awk '{print $2}' | sort -u | \
  tee "$REPORT_DIR/open_hosts.txt"

OPEN_COUNT=$(wc -l < "$REPORT_DIR/open_hosts.txt")
echo "Hosts with open ports: $OPEN_COUNT"
```

### Step 2.2 — Service Version Deep Scan (Open Hosts Only)

```bash
if [ -s "$REPORT_DIR/open_hosts.txt" ]; then
  nmap -Pn -n -sV --version-all \
    -p $PORTS \
    -iL "$REPORT_DIR/open_hosts.txt" \
    -oA "$REPORT_DIR/service_versions"
fi
```

---

## Phase 3: Targeted Security Probes

**Goal:** Deep-dive into specific protocols and services found in Phase 2.

### Step 3.1 — SSH Algorithm Enumeration

Run against every host with port 22 open.

```bash
SSH_HOSTS=$(grep '22/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')

if [ -n "$SSH_HOSTS" ]; then
  nmap -Pn -n -p22 \
    --script ssh2-enum-algos \
    -oN "$REPORT_DIR/ssh_algos.txt" \
    $SSH_HOSTS
fi
```

**What to flag:**
- `ssh-rsa` in host key algorithms (SHA-1 based, deprecated)
- `diffie-hellman-group1-sha1` or `diffie-hellman-group14-sha1` in kex (weak)
- `hmac-sha1`, `hmac-md5`, `arcfour` in ciphers/MACs (insecure)
- Missing `kex-strict-s-v00@openssh.com` (Terrapin vulnerability mitigation)

### Step 3.2 — SSL/TLS Certificate & Cipher Enumeration

Run against every host with port 443 or 8443 open.

```bash
TLS_HOSTS=$(grep -E '(443|8443)/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | sort -u | tr '\n' ' ')

if [ -n "$TLS_HOSTS" ]; then
  nmap -Pn -n -p443,8443 \
    --script ssl-cert,ssl-enum-ciphers \
    -oN "$REPORT_DIR/ssl_enum.txt" \
    $TLS_HOSTS
fi
```

**What to flag:**
- Certificates expiring within 30 days
- Self-signed certificates on production services
- TLS 1.0 or 1.1 support (deprecated)
- Cipher suites rated below grade A
- Missing Subject Alternative Names

### Step 3.3 — HTTP/API Auth Verification

Test whether web services enforce authentication or are wide open.

```bash
AUTH_FILE="$REPORT_DIR/auth_checks.txt"

# For each host with HTTP/HTTPS ports open, probe common endpoints
while read -r HOST; do
  for PORT in 80 81 443 8080 8443 7000; do
    # Check if this host+port was actually open
    if grep -q "${HOST}.*${PORT}/open" "$REPORT_DIR/ports_scan.gnmap" 2>/dev/null; then
      SCHEME="http"
      [ "$PORT" -eq 443 ] || [ "$PORT" -eq 8443 ] || [ "$PORT" -eq 7000 ] && SCHEME="https"

      echo "## ${HOST}:${PORT}" >> "$AUTH_FILE"

      for URLPATH in "/" "/api/health" "/openapi.json" "/docs" "/api/v1/status" "/login"; do
        RESP=$(curl -sk -o /dev/null -w "%{http_code}" \
          --connect-timeout 5 --max-time 10 \
          "${SCHEME}://${HOST}:${PORT}${URLPATH}" 2>/dev/null)
        echo "  ${URLPATH} -> HTTP ${RESP}" >> "$AUTH_FILE"
      done
    fi
  done
done < "$REPORT_DIR/open_hosts.txt"
```

**What to flag:**
- HTTP 200 on sensitive endpoints without auth (should be 401/403)
- OpenAPI/Swagger docs publicly accessible
- Admin panels returning 200 without credentials
- HTTP 301/302 redirects (note: redirect to login is OK)

### Step 3.4 — IoT Protocol Checks

```bash
IOT_FILE="$REPORT_DIR/iot_checks.txt"

# --- MQTT (1883/8883) — check for unauthenticated broker access ---
MQTT_HOSTS=$(grep '1883/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$MQTT_HOSTS" ]; then
  echo "=== MQTT Broker Check ===" >> "$IOT_FILE"
  for H in $MQTT_HOSTS; do
    if which mosquitto_pub > /dev/null 2>&1; then
      # Attempt anonymous CONNECT with a null-op publish
      if mosquitto_pub -h "$H" -p 1883 -t '__kukuibot_audit/probe' -m 'test' \
        --will-topic '__kukuibot_audit/gone' --will-payload '' \
        -q 0 --quiet 2>/dev/null; then
        echo "  CRITICAL: $H:1883 — MQTT broker accepts anonymous connections" >> "$IOT_FILE"
      else
        echo "  OK: $H:1883 — MQTT broker requires authentication" >> "$IOT_FILE"
      fi
    else
      # Fallback: use nmap MQTT script
      nmap -Pn -n -p1883 --script mqtt-subscribe \
        -oN - "$H" 2>/dev/null | grep -E 'mqtt|ERROR|CONNACK' >> "$IOT_FILE"
      echo "  $H:1883 — MQTT port open (install mosquitto for deeper auth test)" >> "$IOT_FILE"
    fi
  done
fi

# --- UPnP/SSDP (1900) — M-SEARCH deep probe ---
UPNP_HOSTS=$(grep '1900/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$UPNP_HOSTS" ]; then
  echo "=== UPnP/SSDP Deep Probe ===" >> "$IOT_FILE"
  for H in $UPNP_HOSTS; do
    # Use nmap UPnP info script for device details
    nmap -Pn -n -p1900 -sU --script upnp-info \
      -oN - "$H" 2>/dev/null | grep -E 'Server:|Location:|friendlyName' >> "$IOT_FILE"
    echo "  $H:1900 — UPnP service exposed" >> "$IOT_FILE"
  done

  # Broadcast M-SEARCH to find all SSDP responders
  echo "--- SSDP Broadcast M-SEARCH ---" >> "$IOT_FILE"
  python3 -c "
import socket, time
msg = (
    'M-SEARCH * HTTP/1.1\r\n'
    'HOST: 239.255.255.250:1900\r\n'
    'MAN: \"ssdp:discover\"\r\n'
    'MX: 2\r\n'
    'ST: ssdp:all\r\n\r\n'
)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sock.settimeout(3)
sock.sendto(msg.encode(), ('239.255.255.250', 1900))
try:
    while True:
        data, addr = sock.recvfrom(4096)
        print(f'  {addr[0]}:{addr[1]} — {data.decode(errors=\"replace\").splitlines()[0]}')
except socket.timeout:
    pass
sock.close()
" >> "$IOT_FILE" 2>/dev/null
fi

# --- Telnet (23) — should NEVER be open ---
TELNET_HOSTS=$(grep '23/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$TELNET_HOSTS" ]; then
  echo "=== CRITICAL: Telnet Open ===" >> "$IOT_FILE"
  for H in $TELNET_HOSTS; do
    echo "  $H:23 — TELNET OPEN (plaintext credentials!)" >> "$IOT_FILE"
  done
fi

# --- Modbus (502) — industrial IoT, should not be on home/office network ---
MODBUS_HOSTS=$(grep '502/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$MODBUS_HOSTS" ]; then
  echo "=== Modbus TCP Check ===" >> "$IOT_FILE"
  for H in $MODBUS_HOSTS; do
    echo "  $H:502 — Modbus TCP exposed (no built-in auth!)" >> "$IOT_FILE"
  done
fi

# --- RTSP (554) — IP cameras (use nmap, not curl — curl doesn't speak RTSP) ---
RTSP_HOSTS=$(grep '554/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$RTSP_HOSTS" ]; then
  echo "=== RTSP Camera Check ===" >> "$IOT_FILE"
  for H in $RTSP_HOSTS; do
    nmap -Pn -n -p554 --script rtsp-methods,rtsp-url-brute \
      -oN - "$H" 2>/dev/null | grep -E 'rtsp|Methods|URL' >> "$IOT_FILE"
    echo "  $H:554 — RTSP stream endpoint detected" >> "$IOT_FILE"
  done
fi

# --- SNMP (161) — community string check ---
SNMP_HOSTS=$(grep '161/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$SNMP_HOSTS" ]; then
  echo "=== SNMP Check ===" >> "$IOT_FILE"
  nmap -Pn -n -sU -p161 --script snmp-info,snmp-brute \
    -oN "$REPORT_DIR/snmp_info.txt" $SNMP_HOSTS 2>/dev/null
fi

# --- NTP (123) — amplification / monlist check ---
NTP_HOSTS=$(grep '123/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')
if [ -n "$NTP_HOSTS" ]; then
  echo "=== NTP Amplification Check ===" >> "$IOT_FILE"
  nmap -Pn -n -sU -p123 --script ntp-monlist,ntp-info \
    -oN "$REPORT_DIR/ntp_check.txt" $NTP_HOSTS 2>/dev/null
  # Flag hosts that respond to monlist (amplification vector)
  grep -B3 'monlist' "$REPORT_DIR/ntp_check.txt" 2>/dev/null | \
    grep 'scan report' | while read -r line; do
      HOST=$(echo "$line" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}')
      echo "  HIGH: $HOST:123 — NTP monlist enabled (DDoS amplification vector)" >> "$IOT_FILE"
    done
fi
```

### Step 3.5 — SMB/AFP Share Enumeration

```bash
SMB_HOSTS=$(grep -E '(445|139)/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | sort -u | tr '\n' ' ')

if [ -n "$SMB_HOSTS" ]; then
  nmap -Pn -n -p445 \
    --script smb-enum-shares,smb-os-discovery \
    -oN "$REPORT_DIR/smb_enum.txt" \
    $SMB_HOSTS
fi

AFP_HOSTS=$(grep '548/open' "$REPORT_DIR/ports_scan.gnmap" | awk '{print $2}' | tr '\n' ' ')

if [ -n "$AFP_HOSTS" ]; then
  nmap -Pn -n -p548 \
    --script afp-serverinfo \
    -oN "$REPORT_DIR/afp_enum.txt" \
    $AFP_HOSTS
fi
```

---

## Phase 4: MAC OUI Vendor Identification & Device Classification

**Goal:** Map every discovered device to a manufacturer and category using MAC+IP pairing.

### Step 4.1 — OUI Lookup (MAC+IP Pairs)

```bash
# Build MAC-to-IP mapping from ARP cache
arp -a | grep -v incomplete | \
  awk '{gsub(/[()]/, "", $2); print $2, $4}' | \
  tee "$REPORT_DIR/ip_mac_map.txt"
```

### Step 4.2 — Vendor Classification Rules

The following OUI-to-category mapping covers common home/lab/office device families. Extend as needed.

```
# OUI Prefix or Vendor String -> Category

# Networking Infrastructure
Ubiquiti             -> Networking
Cisco                -> Networking
Netgear              -> Networking
TP-Link              -> Networking
Aruba                -> Networking
MikroTik             -> Networking
Meraki               -> Networking

# Compute / Personal Devices
Apple                -> Apple
Dell                 -> Compute
Lenovo               -> Compute
Intel                -> Compute
Microsoft            -> Compute
HP / Hewlett Packard -> Compute
Samsung Electronics  -> Mobile/Consumer

# IoT Microcontrollers
Espressif            -> IoT MCU
Microchip Technology -> IoT MCU
Ampak Technology     -> IoT MCU
Texas Instruments    -> IoT MCU

# Smart Home
Amazon Technologies  -> Smart Home
Google               -> Smart Home
Nabu Casa            -> Smart Home    (Home Assistant)
Tuya Smart           -> Smart Home
Brilliant Home       -> Smart Home
Chengdu Meross       -> Smart Home
Orbit Irrigation     -> Smart Home

# Lighting
Philips Lighting     -> Lighting
Signify              -> Lighting
WiZ Connected        -> Lighting
LIFX                 -> Lighting

# Media / Entertainment
Sonos                -> Media
Sony                 -> Media
Nintendo             -> Media
Roku                 -> Media

# Security / Cameras
Ring                 -> Security
Arlo                 -> Security
Hikvision            -> Security
Dahua                -> Security
Wyze                 -> Security

# Industrial / Energy
SMA Solar            -> Energy
Enphase              -> Energy

# Printers
Brother              -> Printer
Canon                -> Printer
Epson                -> Printer

# Unknown / Private MAC
(locally administered bit set, or no OUI match) -> Unknown
```

### Step 4.3 — Generate Device Inventory

```bash
# Python script to resolve OUI (uses nmap's OUI database if available)
python3 -c "
import subprocess, re, sys

# Read IP-MAC map
devices = []
with open('$REPORT_DIR/ip_mac_map.txt') as f:
    for line in f:
        parts = line.strip().split()
        if len(parts) >= 2:
            devices.append({'ip': parts[0], 'mac': parts[1]})

# Try nmap OUI database (check multiple paths)
oui = {}
for db_path in [
    '/usr/local/share/nmap/nmap-mac-prefixes',
    '/opt/homebrew/share/nmap/nmap-mac-prefixes',
    '/usr/share/nmap/nmap-mac-prefixes'
]:
    try:
        with open(db_path) as f:
            for line in f:
                if line and not line.startswith('#'):
                    parts = line.strip().split(' ', 1)
                    if len(parts) == 2:
                        oui[parts[0].upper()] = parts[1]
        break  # Found a valid DB
    except FileNotFoundError:
        continue

for d in devices:
    mac_clean = d['mac'].replace(':', '').upper()
    prefix = mac_clean[:6]
    # Check if locally administered (bit 1 of first octet)
    try:
        first_byte = int(mac_clean[:2], 16)
        is_local = bool(first_byte & 0x02)
    except ValueError:
        is_local = False
    if is_local:
        d['vendor'] = 'Private/Randomized MAC'
    else:
        d['vendor'] = oui.get(prefix, 'Unknown')
    print(f\"{d['ip']}\t{d['mac']}\t{d['vendor']}\")
" | tee "$REPORT_DIR/device_inventory.txt"
```

---

## Phase 5: Vulnerability Assessment

**Goal:** Check for known vulnerabilities specific to each device type and service version.

### Step 5.1 — Version-Based CVE Checks

For each service version discovered in Phase 2, check against known vulnerabilities:

```bash
VULN_FILE="$REPORT_DIR/vuln_checks.txt"

# SSH version checks
if [ -s "$REPORT_DIR/ssh_algos.txt" ]; then
  echo "=== SSH Vulnerability Checks ===" >> "$VULN_FILE"

  # OpenSSH < 9.8 — CVE-2024-6387 (regreSSHion)
  grep -E 'OpenSSH [0-8]\.' "$REPORT_DIR/service_versions.nmap" 2>/dev/null | while read -r line; do
    echo "  CRITICAL: $line — potential CVE-2024-6387 (regreSSHion)" >> "$VULN_FILE"
  done

  # Check for ssh-rsa (SHA-1) host key algorithm
  grep -B5 'ssh-rsa' "$REPORT_DIR/ssh_algos.txt" | grep 'scan report' | while read -r line; do
    HOST=$(echo "$line" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}')
    echo "  MEDIUM: $HOST — ssh-rsa host key (SHA-1 deprecated)" >> "$VULN_FILE"
  done

  # Check for Terrapin mitigation (per-host)
  python3 -c "
import re
with open('$REPORT_DIR/ssh_algos.txt') as f:
    content = f.read()
# Split into per-host blocks
blocks = re.split(r'Nmap scan report for ', content)
for block in blocks[1:]:
    host_match = re.search(r'([\d.]+)', block)
    if not host_match:
        continue
    host = host_match.group(1)
    if 'kex-strict-s-v00@openssh.com' not in block:
        print(f'  MEDIUM: {host} — Terrapin (CVE-2023-48795) mitigation missing')
    else:
        print(f'  OK: {host} — Terrapin mitigation present')
" >> "$VULN_FILE"
fi

# TLS certificate expiration checks
if [ -s "$REPORT_DIR/ssl_enum.txt" ]; then
  echo "=== TLS Certificate Checks ===" >> "$VULN_FILE"

  # Extract cert expiry dates and check against 30-day window
  python3 -c "
import re, datetime
with open('$REPORT_DIR/ssl_enum.txt') as f:
    content = f.read()
blocks = content.split('Nmap scan report for ')
now = datetime.datetime.now()
for block in blocks[1:]:
    host = block.split('\n')[0].strip()
    expires = re.findall(r'Not valid after:\s+([\d-]+T[\d:]+)', block)
    for exp in expires:
        exp_date = datetime.datetime.strptime(exp, '%Y-%m-%dT%H:%M:%S')
        days_left = (exp_date - now).days
        if days_left < 0:
            print(f'  CRITICAL: {host} — certificate EXPIRED ({exp})')
        elif days_left < 30:
            print(f'  HIGH: {host} — certificate expires in {days_left} days ({exp})')
        elif days_left < 90:
            print(f'  MEDIUM: {host} — certificate expires in {days_left} days ({exp})')
        else:
            print(f'  OK: {host} — certificate valid for {days_left} days')
" >> "$VULN_FILE"
fi
```

### Step 5.2 — Device-Type Specific Checks

```bash
# Networking gear — check for default/open management UIs, firmware versions
NET_HOSTS=$(grep -iE 'ubiquiti|cisco|netgear|tp-link|aruba|mikrotik' "$REPORT_DIR/device_inventory.txt" | awk '{print $1}')
if [ -n "$NET_HOSTS" ]; then
  echo "=== Network Infrastructure Checks ===" >> "$VULN_FILE"
  for H in $NET_HOSTS; do
    # Check for open management UI
    for PORT in 80 443 8080 8443; do
      if grep -q "${H}.*${PORT}/open" "$REPORT_DIR/ports_scan.gnmap" 2>/dev/null; then
        RESP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 5 "http://${H}:${PORT}/" 2>/dev/null)
        echo "  $H:$PORT — management UI: HTTP $RESP" >> "$VULN_FILE"
      fi
    done
  done
fi

# Espressif/Tuya/IoT MCU — check for open HTTP config portals
IOT_MCU_HOSTS=$(grep -iE 'espressif|tuya|meross|ampak' "$REPORT_DIR/device_inventory.txt" | awk '{print $1}')
if [ -n "$IOT_MCU_HOSTS" ]; then
  echo "=== IoT MCU HTTP Checks ===" >> "$VULN_FILE"
  for H in $IOT_MCU_HOSTS; do
    RESP=$(curl -sk -o /dev/null -w "%{http_code}" --max-time 3 "http://${H}:80/" 2>/dev/null)
    [ "$RESP" != "000" ] && echo "  $H — HTTP config portal accessible (HTTP $RESP)" >> "$VULN_FILE"
  done
fi

# Sonos — check for unauthenticated API
SONOS_HOSTS=$(grep -i sonos "$REPORT_DIR/device_inventory.txt" | awk '{print $1}')
if [ -n "$SONOS_HOSTS" ]; then
  echo "=== Sonos Checks ===" >> "$VULN_FILE"
  for H in $SONOS_HOSTS; do
    RESP=$(curl -sk --max-time 3 "http://${H}:1400/status" 2>/dev/null | head -c 200)
    [ -n "$RESP" ] && echo "  $H:1400 — status endpoint accessible" >> "$VULN_FILE"
  done
fi

# Smart lighting (Hue, LIFX, etc) — check bridge/bulb API
LIGHT_HOSTS=$(grep -iE 'philips|signify|lifx' "$REPORT_DIR/device_inventory.txt" | awk '{print $1}')
if [ -n "$LIGHT_HOSTS" ]; then
  echo "=== Smart Lighting Checks ===" >> "$VULN_FILE"
  for H in $LIGHT_HOSTS; do
    RESP=$(curl -sk --max-time 3 "http://${H}/api/config" 2>/dev/null | head -c 500)
    [ -n "$RESP" ] && echo "  $H — lighting bridge API accessible (check for linked user tokens)" >> "$VULN_FILE"
  done
fi

# Smart home hubs (Amazon, Google) — check for unexpected open ports
SMARTHOME_HOSTS=$(grep -iE 'amazon|google' "$REPORT_DIR/device_inventory.txt" | awk '{print $1}')
if [ -n "$SMARTHOME_HOSTS" ]; then
  echo "=== Smart Home Hub Checks ===" >> "$VULN_FILE"
  for H in $SMARTHOME_HOSTS; do
    nmap -Pn -n -p8443,55443 --open -oN - "$H" 2>/dev/null | \
      grep open && echo "  $H — smart home device with unexpected open ports" >> "$VULN_FILE"
  done
fi
```

---

## Phase 6: Baseline Comparison

**Goal:** Compare current scan results against the known-good device baseline to detect new, missing, or changed devices. Uses MAC+IP pairs for reliable identity tracking.

### Network Baseline

> **Maintenance:** Update this table after every scan. New devices should be identified, named, and categorized. Remove decommissioned devices.
>
> **Note:** The table below is a template. Populate it with your actual network inventory after your first scan.

| IP | MAC | Vendor | Category | Expected Ports | Notes |
|----|-----|--------|----------|---------------|-------|
| *(gateway IP)* | *(gateway MAC)* | *(vendor)* | Networking | 22,53,80,443 | Gateway/router |
| *(example)* | `aa:bb:cc:dd:ee:ff` | *(vendor)* | Compute | 22,443 | Example workstation |

### Baseline Comparison Script

```bash
BASELINE_FILE="$REPORT_DIR/../network_baseline.txt"  # Persistent across scans
CURRENT_BASELINE="$REPORT_DIR/current_mac_ip.txt"
DIFF_FILE="$REPORT_DIR/baseline_diff.txt"

# Build MAC+IP pairs for current scan (more reliable than IP-only)
awk '{print $2, $1}' "$REPORT_DIR/ip_mac_map.txt" | sort > "$CURRENT_BASELINE"

if [ -f "$BASELINE_FILE" ]; then
  echo "=== New Devices (not in baseline) ===" > "$DIFF_FILE"
  comm -13 <(sort "$BASELINE_FILE") <(sort "$CURRENT_BASELINE") >> "$DIFF_FILE"

  echo "" >> "$DIFF_FILE"
  echo "=== Missing Devices (in baseline but not found) ===" >> "$DIFF_FILE"
  comm -23 <(sort "$BASELINE_FILE") <(sort "$CURRENT_BASELINE") >> "$DIFF_FILE"

  echo "" >> "$DIFF_FILE"
  echo "=== IP Changes (same MAC, different IP) ===" >> "$DIFF_FILE"
  # Detect MAC addresses that appear in both but with different IPs
  python3 -c "
baseline = {}
current = {}
with open('$BASELINE_FILE') as f:
    for line in f:
        parts = line.strip().split()
        if len(parts) >= 2:
            baseline[parts[0]] = parts[1]
with open('$CURRENT_BASELINE') as f:
    for line in f:
        parts = line.strip().split()
        if len(parts) >= 2:
            current[parts[0]] = parts[1]
for mac in set(baseline.keys()) & set(current.keys()):
    if baseline[mac] != current[mac]:
        print(f'  {mac}: {baseline[mac]} -> {current[mac]}')
" >> "$DIFF_FILE"

  NEW_COUNT=$(comm -13 <(sort "$BASELINE_FILE") <(sort "$CURRENT_BASELINE") | wc -l)
  MISSING_COUNT=$(comm -23 <(sort "$BASELINE_FILE") <(sort "$CURRENT_BASELINE") | wc -l)
  echo "New: $NEW_COUNT | Missing: $MISSING_COUNT"
else
  echo "No baseline file found. Saving current scan as baseline."
  cp "$CURRENT_BASELINE" "$BASELINE_FILE"
fi
```

---

## Phase 7: Report Generation

**Goal:** Produce a polished, self-contained HTML report suitable for email delivery and archival.

### Report Data Model

The report generator consumes all Phase 1–6 output files and produces:

1. **Markdown report** — `network_security_report.md`
2. **HTML report** — `network_security_report.html` (email-ready)

### Risk Score Calculation

```
Base score: 0

Per finding:
  +25  Critical (telnet open, expired cert, default creds, RCE CVE, NTP monlist)
  +15  High (management UI exposed, unauthenticated API/MQTT, weak crypto)
  +8   Medium (SSH with deprecated algos, cert expiring <90d, unnecessary services)
  +3   Low (expected services with auth, informational)

Deductions:
  -5   Per positive control (auth enforced, TLS 1.3, strong ciphers, Terrapin mitigated)
  -3   Per device in baseline (known/managed)

Final score: clamped to 0–100
  0–25:   Low risk    (green)
  26–50:  Moderate    (yellow)
  51–75:  Medium      (orange)
  76–100: High risk   (red)
```

### HTML Report Template Spec

The HTML report follows KukuiBot's standard dark-themed email format:

```
STRUCTURE:
+-- Header: "Network Security Audit Report"
|   +-- Subtitle: date, scope, method
+-- Stat Cards (4-column grid):
|   +-- Risk Gauge (score/100, color-coded)
|   +-- Devices Observed (count)
|   +-- Exposed Hosts (count)
|   +-- Total Findings (count)
+-- Executive Summary (paragraph)
+-- Findings (ordered by severity):
|   +-- Each finding card:
|       +-- Title with severity tag
|       +-- Asset IP
|       +-- Evidence block (monospace pre)
|       +-- CVE/CWE reference (if applicable)
|       +-- Remediation steps
+-- Positive Controls (green checkmarks)
+-- Baseline Comparison:
|   +-- New devices (if any)
|   +-- Missing devices (if any)
|   +-- IP changes (if any)
+-- Priority Action Plan (table: priority, action, owner, ETA)
+-- Vendor Overview (tag cloud)
+-- Full Device Inventory (table):
|   +-- IP, MAC, Vendor, Category (color tag), Open Ports
+-- Footer: "Generated by KukuiBot · {date}"

STYLING:
  background:    #0b1220
  cards:         #111827, border #1f2937, radius 12px
  text:          #e5e7eb (body), #94a3b8 (muted)
  severity colors:
    Critical:    #ef4444 (bg: rgba(239,68,68,0.1))
    High:        #dc2626 (border-left on finding cards)
    Medium:      #f59e0b
    Low:         #3b82f6
    Info:        #64748b
  positive:      #22c55e
  gauge:         #f59e0b (medium), #ef4444 (high), #22c55e (low)
  category tags: pill-shaped, #1f2937 bg, 12px font
  tables:        #111827 bg, sticky headers on #0f172a
  code/pre:      #0f172a bg, #cbd5e1 text, 1px border #1e293b
  max-width:     1200px (report), 680px (email variant)
  font:          -apple-system, BlinkMacSystemFont, Segoe UI, Roboto
```

### Category Color Tags

| Category | Tag Color |
|----------|-----------|
| Networking | `#3b82f6` (blue) |
| Apple | `#a78bfa` (purple) |
| IoT MCU | `#f59e0b` (amber) |
| Smart Home | `#22c55e` (green) |
| Lighting | `#fbbf24` (yellow) |
| Media | `#ec4899` (pink) |
| Security | `#ef4444` (red) |
| Energy | `#06b6d4` (cyan) |
| Compute | `#8b5cf6` (violet) |
| Printer | `#78716c` (stone) |
| Unknown | `#64748b` (slate) |

---

## Phase 8: Delivery & Archival

### Save Reports

```bash
# Reports are already saved in $REPORT_DIR by each phase
echo "All reports saved to: $REPORT_DIR"
ls -la "$REPORT_DIR"
```

### Email Report (via KukuiBot)

```bash
if [ -n "$REPORT_EMAIL" ]; then
  HTML_BODY=$(cat "$REPORT_DIR/network_security_report.html")

  curl -sk -X POST "${KUKUIBOT_URL}/api/gmail/send" \
    -H 'Content-Type: application/json' \
    -d "{
      \"to\": \"${REPORT_EMAIL}\",
      \"subject\": \"Network Audit Report — $(date +%Y-%m-%d)\",
      \"body\": $(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" <<< "$HTML_BODY")
    }"
else
  echo "No REPORT_EMAIL set — skipping email delivery. Report saved to: $REPORT_DIR"
fi
```

### Update Baseline

```bash
# After review, promote current MAC+IP pairs to baseline
cp "$REPORT_DIR/current_mac_ip.txt" "$REPORT_DIR/../network_baseline.txt"
echo "Baseline updated with $(wc -l < "$REPORT_DIR/../network_baseline.txt") MAC+IP pairs"
```

---

## Execution Checklist

Run phases in order. Each phase depends on the previous.

| Phase | Name | ~Duration | Depends On |
|-------|------|-----------|------------|
| 1 | Environment & Reconnaissance | 30–45 sec | — |
| 2 | Port Scanning & Service Enum | 2–10 min | Phase 1 hosts |
| 3 | Targeted Security Probes | 1–5 min | Phase 2 open hosts |
| 4 | Vendor ID & Classification | 10 sec | Phase 1 ARP data |
| 5 | Vulnerability Assessment | 1–3 min | Phases 2, 3, 4 |
| 6 | Baseline Comparison | 5 sec | Phase 1 hosts + prior baseline |
| 7 | Report Generation | 10 sec | All phases |
| 8 | Delivery & Archival | 10 sec | Phase 7 report |

**Total estimated time: 5–20 minutes** (depends on network size and host responsiveness)

---

## Scheduling

### One-Time Run

Tell KukuiBot: *"Run a network audit"*

### Recurring (Weekly)

Add to crontab:
```bash
# Weekly network audit — Sunday 3 AM
0 3 * * 0 /path/to/network_audit.sh >> ~/.kukuibot/logs/audit.log 2>&1
```

Or tell KukuiBot: *"Schedule a weekly network audit on Sundays at 3 AM"*

---

## Extending This Runbook

### Adding New Port Checks

1. Add the port number to the `PORTS` variable
2. Add a protocol-specific check in Phase 3 (Step 3.4)
3. Add the service to the vulnerability checks in Phase 5
4. Update the category mapping if it's a new device type

### Adding New Device Types

1. Add the OUI prefix to the vendor classification table (Phase 4, Step 4.2)
2. Add device-specific checks in Phase 5, Step 5.2
3. Add a category color tag entry for the report

### Adding New Vulnerability Checks

1. Add the CVE check to Phase 5, Step 5.1 (version-based) or Step 5.2 (device-specific)
2. Include the CVE ID, severity, and remediation in the finding output
3. Update the risk score weights if introducing a new severity tier

---

## References

- [Nmap Reference Guide](https://nmap.org/book/man.html)
- [OWASP Testing Guide](https://owasp.org/www-project-web-security-testing-guide/)
- [CIS Controls v8](https://www.cisecurity.org/controls)
- [IEEE OUI Database](https://standards-oui.ieee.org/)
- [CVE Database](https://cve.mitre.org/)
