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
- macOS (primary), Linux, or Windows
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

The installer handles all dependencies (Python 3.11+, mkcert, ripgrep, Node.js, Claude Code CLI), HTTPS certs, launchd services, and cron jobs.

### Manual Setup

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

### First Run

1. Open **https://localhost:7000** (or your configured port)
2. **Trust Certificate** (Step 0) — downloads the mkcert root CA for your device
3. **Create Account** (Step 1) — local admin user, stored locally
   - Or **skip** to run in localhost-only mode (no login required from the host machine)
4. **Connect a Provider** (Step 2) — OpenAI OAuth, Claude Code, Anthropic API, or OpenRouter
   - Or **skip** to configure providers later in Settings
5. You're in. Start chatting.

### Accessing from Other Devices (LAN)

KukuiBot runs HTTPS on port 7000 by default. Other devices on your network can access it at:

```
https://<your-ip>:7000
```

On first visit, they'll see a certificate warning. To fix it permanently:

1. Go to **https://\<your-ip\>:7000/api/cert**
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

## Voice Input

Click the mic button to dictate. Uses the Web Speech API:
- **Safari**: On-device Siri STT (private, fast)
- **Chrome**: Google Cloud STT
- **Auto-sends** after 2 seconds of silence
- Click into the text field to cancel voice and edit manually

Requires HTTPS (which KukuiBot provides by default).

## Uninstalling

```bash
bash ~/.kukuibot/src/uninstall.sh
# Or if installed to a custom directory:
bash ~/my-kukuibot/src/uninstall.sh --dir ~/my-kukuibot
```

Removes services, cron jobs, sudoers rules, data, and logs. Does not remove Homebrew packages, Node.js, Claude Code CLI, or the mkcert root CA.

## Docs

- [Design Document](docs/DESIGN.md) — full architecture, security model, data flow
- [Security Policy](SECURITY.md) — runtime guardrails, elevation rules

## License

MIT — see [LICENSE](LICENSE).
