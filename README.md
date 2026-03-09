# KukuiBot

A self-hosted, multi-provider AI agent. Vanilla HTML/JS frontend, FastAPI backend, full tool execution, HTTPS out of the box.

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/ryanjw888/KukuiBot.git && cd kukuibot
pip install -r requirements.txt

# 2. Run
python3 server.py
```

Open **https://localhost:7000** — the setup wizard walks you through everything.

## What You Get

- **Multi-provider AI** — OpenAI (OAuth), Claude Code (CLI or API key), Anthropic API, OpenRouter
- **Chat UI** with SSE streaming, markdown, work log
- **Browser automation tools**: browser_open, browser_navigate, browser_click, browser_type, browser_extract, browser_snapshot, browser_close
- **Core tools**: bash, read_file, write_file, edit_file, spawn_agent (+ background, memory, web search/fetch)
- **Voice input** via Web Speech API (Safari's on-device Siri STT)
- **Wake listener resilience**: local wake-listener can fall back to the first available input device if macOS default mic routing breaks after reboot
- **Security**: elevation prompts, path guards, root mode with TTL
- **Context management**: auto-compaction, memory search, token accuracy tracking
- **Multi-tab**: run multiple workers side-by-side across providers
- **Cross-device tab sync**: worker names/session mapping persist server-side across devices
- **Mobile UX**: responsive layout with narrow-screen worker creation modal
- **Orphan tab cleanup cron**: hourly maintenance prunes stale tab metadata with no history
- **OAuth LAN support**: iframe overlay, remote detection, callback proxy, paste-URL fallback
- **Tab sync tombstones**: server-authoritative deletion — delete on one device, stays deleted everywhere
- **Resilient iOS streaming**: background/interruption recovery via resume endpoint
- **HTTPS by default** with mkcert certificates

## Installation

### Prerequisites

- Python 3.11+
- macOS, Windows 10/11, or Linux
- `ripgrep` (`rg`) installed and on PATH
- At least one AI provider account (OpenAI, Anthropic, or OpenRouter)

### One-Line Install (macOS)

```bash
curl -fsSL https://github.com/ryanjw888/KukuiBot/raw/main/install.sh | bash
```

Or with custom options:

```bash
curl -fsSL https://github.com/ryanjw888/KukuiBot/raw/main/install.sh | bash -s -- --port 8443 --dir ~/my-kukuibot
```

**Default port:** 7000. Use `--port` to change.

The installer handles all dependencies (Python 3.11+, mkcert, ripgrep, Node.js 18+, Claude Code CLI), HTTPS certs, launchd services, and cron jobs.

### One-Line Install (Windows)

Open **PowerShell as Administrator** and run:

```powershell
irm https://github.com/ryanjw888/KukuiBot/raw/main/install.ps1 | iex
```

Or with custom options:

```powershell
.\install.ps1 -Port 8443 -Dir C:\kukuibot
```

The installer uses `winget` for dependencies (Python 3.13, Node.js, Git, mkcert, ripgrep, Claude Code CLI), creates Windows Scheduled Tasks for the server (runs at logon), a watchdog (restarts server if it crashes), hourly backups, and orphan-tab cleanup. Requires Windows 10/11 with winget available.

**Note:** `uvloop` is automatically excluded from pip install on Windows (Unix-only). All platform-specific features (vm_stat, launchd, afplay, etc.) gracefully degrade with Windows-appropriate fallbacks.

### Manual Setup (macOS)

```bash
# Install native dependencies
brew install mkcert ripgrep node
mkcert -install
npm install -g @anthropic-ai/claude-code

# Clone and install Python deps
git clone https://github.com/ryanjw888/KukuiBot.git && cd kukuibot
pip install -r requirements.txt

# Generate HTTPS certs
mkdir -p certs
mkcert -cert-file certs/kukuibot.pem -key-file certs/kukuibot-key.pem \
  localhost 127.0.0.1 $(ipconfig getifaddr en0)

# Run
python3 server.py
```

### Manual Setup (Windows)

```powershell
# Install native dependencies
winget install Python.Python.3.13 OpenJS.NodeJS FiloSottile.mkcert BurntSushi.ripgrep.MSVC
mkcert -install
npm install -g @anthropic-ai/claude-code

# Clone and install Python deps
git clone https://github.com/ryanjw888/KukuiBot.git; cd kukuibot
pip install -r requirements.txt  # uvloop will fail — this is expected, server runs without it

# Generate HTTPS certs
mkdir certs
$lanIP = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -ne 'WellKnown' } | Select-Object -First 1).IPAddress
mkcert -cert-file certs\kukuibot.pem -key-file certs\kukuibot-key.pem localhost 127.0.0.1 $lanIP

# Run
python server.py
```

### First Run

1. Open **https://localhost** (or `https://localhost:PORT` if you used a custom port)
2. **Trust Certificate** (Step 0) — downloads the mkcert root CA for your device
3. **Create Account** (Step 1) — local admin user, stored locally
   - Or **skip** to run in localhost-only mode (no login required from the host machine)
4. **Connect a Provider** (Step 2) — OpenAI OAuth, Claude Code, Anthropic API, or OpenRouter
   - Or **skip** to configure providers later in Settings
5. You're in. Start chatting.

### Accessing from Other Devices (LAN)

KukuiBot runs HTTPS on port 7000 by default. Other devices on your network can access it at:

```
https://<your-ip>:PORT
```

On first visit, they'll see a certificate warning. To fix it permanently:

1. Go to **https://\<your-ip\>/api/cert** (or `https://<your-ip>:PORT/api/cert` for custom ports)
2. Download and install the root CA:
   - **macOS**: Open file → Keychain Access → double-click cert → Always Trust
   - **iPhone/iPad**: Open file → Install Profile → Settings → General → About → Certificate Trust Settings → enable
   - **Windows**: Double-click → Install Certificate → Trusted Root CAs
   - **Android**: Open file → enter PIN → installs as user cert
3. Reload — no more warnings, ever.

### Accessing Remotely (Cloudflare Tunnel)

```yaml
# In cloudflared config
ingress:
  - hostname: kukuibot.yourdomain.com
    service: https://localhost:7000
    originRequest:
      noTLSVerify: true
```

Cloudflare provides a globally-trusted TLS cert automatically — zero setup on client devices.

## Architecture

```
Browser (any device)
    ↓ HTTPS
FastAPI + Uvicorn (TLS via mkcert)
    ↓
AI Provider (OpenAI / Anthropic / OpenRouter)
    ↓
Tool execution (bash, files, sub-agents)
    ↓
SQLite (~/.kukuibot/kukuibot.db)
```

- **Zero build step** — vanilla HTML/JS/CSS, no React, no bundler
- **Single process** — one Python process, one port
- **Self-contained** — all data in `~/.kukuibot/`
- **Self-compacting** — uses the connected AI provider for context compaction

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `KUKUIBOT_PORT` | `7000` | Server port (HTTPS) |
| `KUKUIBOT_HOST` | `0.0.0.0` | Bind address |
| `KUKUIBOT_HOME` | `~/.kukuibot` | Data directory |
| `KUKUIBOT_MAX_TOOL_ROUNDS` | `100` | Tool-call safety cap per turn |
| `KUKUIBOT_BROWSER_ALLOW_LOCALHOST` | unset (`false`) | Allow browser tools to access localhost URLs |

## Security

- **HTTPS by default** — mkcert certs auto-detected
- **Local auth** — bcrypt passwords, httpOnly session cookies
- **Localhost auto-trust** — no login needed from the host machine
- **Workspace sandbox** — file tools restricted to `~/.kukuibot/` by default
- **Elevation system** — dangerous commands require explicit approval
- **Root mode** — 10-minute TTL bypass for admin tasks
- **Content guard** — two-stage injection detection (DeBERTa + sandboxed model)

### Gmail Security Controls

When Gmail is connected, three granular send permission levels are available in Settings:

| Permission | Owner | Same Domain | External |
|-----------|-------|-------------|----------|
| **Send to Owner Only** | ✅ | ❌ | ❌ |
| **Send within Org** | ✅ | ✅ | ❌ |
| **Send to Anyone** | ✅ | ✅ | ✅ |

- **Owner-only** mode restricts sending to the authenticated Gmail account and any configured admin emails
- **Organization** mode allows sending only to addresses at the same domain (e.g., @wilmot.org)
- **Anyone** mode permits external sends (validates via content sanitizer)
- All outbound email is scanned for sensitive content (IPs, ports, credentials, tokens) before sending
- Permissions are enforced server-side — attempts to bypass are blocked with `PermissionError`

## Voice Input

Click the mic button to dictate. Uses the Web Speech API:
- **Safari**: On-device Siri STT (private, fast)
- **Chrome**: Google Cloud STT
- **Auto-sends** after 2 seconds of silence
- Click into the text field to cancel voice and edit manually

Requires HTTPS (which KukuiBot provides by default).

## Uninstalling

### macOS

```bash
bash ~/.kukuibot/src/uninstall.sh
# Or if installed to a custom directory:
bash ~/my-kukuibot/src/uninstall.sh --dir ~/my-kukuibot
```

Removes services, cron jobs, sudoers rules, data, and logs. Does not remove Homebrew packages, Node.js, Claude Code CLI, or the mkcert root CA.

### Windows

```powershell
# Remove scheduled tasks
schtasks /Delete /TN KukuiBot-Server /F
schtasks /Delete /TN KukuiBot-Watchdog /F
schtasks /Delete /TN KukuiBot-Backup /F
schtasks /Delete /TN KukuiBot-OrphanCleanup /F

# Remove data directory (adjust path if custom)
Remove-Item -Recurse -Force "$env:USERPROFILE\.kukuibot"
```

Does not remove Python, Node.js, Git, Claude Code CLI, or the mkcert root CA.

## Docs

- [Design Document](docs/DESIGN.md) — full architecture, security model, data flow
- [Security Policy](SECURITY.md) — runtime guardrails, elevation rules

## License

All rights reserved. Source code is viewable but may not be copied, modified, or distributed without explicit permission.
