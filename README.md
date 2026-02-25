# KukuiBot

A self-hosted AI agent powered by GPT-5.3 Codex. Vanilla HTML/JS frontend, FastAPI backend, full tool execution, HTTPS out of the box.

## Quick Start

```bash
# 1. Clone and install
git clone <repo> && cd kukuibot
pip install -r requirements.txt

# 2. Run
python3 server.py
```

Open **https://localhost:7000** — the setup wizard walks you through everything.

## What You Get

- **Chat UI** with SSE streaming, markdown, work log
- **Browser automation tools**: browser_open, browser_navigate, browser_click, browser_type, browser_extract, browser_snapshot, browser_close
- **Core tools**: bash, read_file, write_file, edit_file, spawn_agent (+ background, memory, web search/fetch)
- **Voice input** via Web Speech API (Safari's on-device Siri STT)
- **Security**: elevation prompts, path guards, root mode with TTL
- **Context management**: auto-compaction, memory search, token accuracy tracking
- **Project report context**: auto-generated `PROJECT-REPORT.md` is injected into worker prompts and compaction context
- **Multi-tab**: run multiple Codex and Spark workers side-by-side
- **Cross-device tab sync**: worker names/session mapping persist server-side across devices
- **Mobile UX**: narrow-screen “+” opens worker-name modal (OK/Cancel), settings include delete-current-tab confirmation
- **Server controls**: settings menu includes restart-server action with confirmation dialog
- **Tab deletion cleanup**: deleting a tab queues background cleanup of history + tab metadata + runtime/elevation state
- **Orphan tab cleanup cron**: hourly maintenance prunes stale tab metadata with no history
- **Global nav**: dropdown on all pages (Chat, Settings) with navigation + logout
- **OAuth LAN support**: iframe overlay, remote detection, callback proxy, paste-URL fallback
- **Tab sync tombstones**: server-authoritative deletion — delete on one device, stays deleted everywhere
- **Resilient iOS streaming**: background/interruption recovery via resume endpoint
- **Max Sessions management**: `/max/` UI for viewing/editing per-user tab/session limits (Codex/Spark/total)
- **HTTPS by default** with mkcert certificates

## Installation

### Prerequisites

- Python 3.11+
- macOS, Linux, or Windows
- `ripgrep` (`rg`) installed and on PATH (required by KukuiBot shell workflows)
- A ChatGPT Plus or Pro subscription (or OpenAI API key)

### One-Line Install (macOS)

```bash
curl -fsSL https://raw.githubusercontent.com/<repo>/install.sh | bash
```

Or manually:

```bash
# Install native dependencies
brew install mkcert ripgrep
mkcert -install

# Clone and setup
git clone <repo> kukuibot && cd kukuibot
pip install -r requirements.txt

# Generate HTTPS certs
mkdir -p certs
mkcert -cert-file certs/kukuibot.pem -key-file certs/kukuibot-key.pem \
  localhost 127.0.0.1 $(ipconfig getifaddr en0)

# Run
python3 server.py
```

### First Run

1. Open **https://localhost:7000**
2. **Trust Certificate** (Step 0) — downloads the mkcert root CA for your device
3. **Create Account** (Step 1) — local admin user, stored on your machine
4. **Connect OpenAI** (Step 2) — sign in with OAuth or paste an API key
5. You're in. Start chatting.

### Accessing from Other Devices (LAN)

KukuiBot runs HTTPS on port 7000. Other devices on your network can access it at:

```
https://<your-mac-ip>:7000
```

On first visit, they'll see a certificate warning. To fix it permanently:

1. Go to **https://<your-mac-ip>:7000/api/cert**
2. Download and install the root CA:
   - **macOS**: Open file → Keychain Access → double-click cert → Always Trust
   - **iPhone/iPad**: Open file → Install Profile → Settings → General → About → Certificate Trust Settings → enable
   - **Windows**: Double-click → Install Certificate → Trusted Root CAs
   - **Android**: Open file → enter PIN → installs as user cert
3. Reload — no more warnings, ever.

### Accessing Remotely (Cloudflare Tunnel)

If you have a Cloudflare tunnel set up, add KukuiBot as a route:

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
FastAPI + Uvicorn (port 7000, TLS via mkcert)
    ↓
GPT-5.3 Codex API (chatgpt.com)
    ↓
Tool execution (bash, files, sub-agents)
    ↓
SQLite (~/.kukuibot/kukuibot.db)
```

- **Zero build step** — vanilla HTML/JS/CSS, no React, no bundler
- **Single process** — one Python process, one port
- **Self-contained** — all data in `~/.kukuibot/`
- **Self-compacting** — uses Codex itself for context compaction (no external deps)

## Configuration

Environment variables (all optional):

| Variable | Default | Description |
|----------|---------|-------------|
| `KUKUIBOT_PORT` | `7000` | Server port (HTTPS) |
| `KUKUIBOT_HOST` | `0.0.0.0` | Bind address |
| `KUKUIBOT_HOME` | `~/.kukuibot` | Data directory |
| `KUKUIBOT_MAX_TOOL_ROUNDS` | `100` | Tool-call safety cap per turn |
| `KUKUIBOT_BROWSER_ALLOW_LOCALHOST` | unset (`false`) | Allow browser_* tools to open/navigate `http(s)://localhost` / loopback URLs for local UI automation |

## Context Accounting & Compaction

KukuiBot tracks context usage using a hybrid method for better accuracy:

- **API-grounded usage** from `response.usage.input_tokens`
- **Estimate fallback** from serialized prompt + conversation items
- **Effective context** blending (`api+delta`) to account for content appended after the last request (tool outputs, assistant text)

### Per-Profile Limits

- **Codex tabs**: 400k context window, 300k auto-compaction threshold
- **Spark tabs**: 175k context window, 150k auto-compaction threshold

> Note: Spark tabs currently route through the Codex Responses API model for compatibility, while still enforcing Spark-specific context limits.

### Debug & Drift Telemetry

- `GET /api/tokens?session_id=...` — current effective context usage used by status bar
- `GET /api/token-debug?session_id=...` — estimator internals + drift metrics
- Drift logs: `~/.kukuibot/logs/token-accuracy.log` (JSONL)

## Security

- **HTTPS by default** — mkcert certs auto-detected
- **Local auth** — bcrypt passwords, httpOnly session cookies
- **Localhost auto-trust** — no login needed from the host machine
- **Workspace sandbox** — file tools restricted to `~/.kukuibot/` by default
- **Elevation system** — dangerous commands require explicit approval
- **Root mode** — 10-minute TTL bypass for admin tasks
- **Content guard** — two-stage injection detection (DeBERTa + Spark)

## Voice Input

Click the mic button to dictate. Uses the Web Speech API:
- **Safari**: On-device Siri STT (private, fast)
- **Chrome**: Google Cloud STT
- **Auto-sends** after 2 seconds of silence
- Click into the text field to cancel voice and edit manually

Requires HTTPS (which KukuiBot provides by default).

## Docs

- [Design Document](docs/DESIGN.md) — full architecture, security model, data flow
- [Security Policy](SECURITY.md) — runtime guardrails, elevation rules

## License

MIT


