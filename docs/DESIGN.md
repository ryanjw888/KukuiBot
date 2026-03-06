# KukuiBot — Design Document

**Version:** 1.0  
**Last Updated:** 2026-02-16  
**Author:** KukuiBot Team

---

## 1. Overview

KukuiBot is a standalone, self-hosted, multi-provider AI agent interface. It provides a full-featured chat UI with tool execution (bash, file ops, sub-agents), a tiered security/elevation system, and context management. Supports OpenAI, Claude Code, Anthropic API, and OpenRouter.

### Design Goals
- **Zero build step** — vanilla HTML/JS/CSS frontend, no React, no Node, no bundler
- **Single-process backend** — FastAPI/uvicorn, one Python process
- **Self-contained** — all data in `~/.kukuibot/`, no external databases
- **Portable** — runs on any machine with Python 3.11+ and at least one AI provider account
- **Secure by default** — localhost-only on first run, opt-in LAN access, proper TLS

---

## 2. Architecture

```
┌──────────────────────────────────────────────────────┐
│  Browser (any device on LAN)                         │
│  ┌────────────────────────────────────────────────┐  │
│  │  Vanilla JS Frontend (app.js + style.css)      │  │
│  │  - SSE streaming, markdown, work log           │  │
│  │  - Elevation dialogs, reasoning controls       │  │
│  │  - Login / setup wizard (first-run)            │  │
│  └──────────────────────┬─────────────────────────┘  │
│                         │ HTTPS (TLS via mkcert)     │
├─────────────────────────┼────────────────────────────┤
│  FastAPI Backend         │ 0.0.0.0:7000 (HTTPS)      │
│  ┌──────────────────────┴─────────────────────────┐  │
│  │  server.py — main app, SSE streaming, routes   │  │
│  │  auth.py — OAuth tokens, user auth, sessions   │  │
│  │  security.py — elevation, path guards, egress  │  │
│  │  tools.py — bash, read/write/edit, spawn_agent │  │
│  │  compaction.py — self-compact via AI provider    │  │
│  │  memory.py — TF-IDF search over memory files   │  │
│  │  subagent.py — isolated sub-agent spawning     │  │
│  │  config.py — all paths, env vars, defaults     │  │
│  └────────────────────────────────────────────────┘  │
│                         │                            │
│  Storage: ~/.kukuibot/     │                            │
│  ├── kukuibot.db           │ SQLite (auth, history)     │
│  ├── config/            │ security-policy.json       │
│  ├── memory/            │ daily notes, search index  │
│  ├── logs/              │ tool-calls.log, server.log │
│  └── MEMORY.md, etc.   │ identity/context files     │
└─────────────────────────┴────────────────────────────┘
                          │
                          ▼
              AI Providers (OpenAI / Anthropic / OpenRouter)
```

### Key Components

| File | Responsibility |
|------|---------------|
| `server.py` | FastAPI app, SSE streaming, chat loop, tool execution, all API routes |
| `auth.py` | ChatGPT OAuth token storage, JWT parsing, user authentication, session management |
| `security.py` | Elevation system, path access control, bash command checks, egress guards |
| `tools.py` | Tool definitions and execution (bash, file ops, memory search, spawn_agent) |
| `compaction.py` | Context compaction via AI provider self-summarization |
| `generate_project_report.py` | Rule-based generator for `~/.kukuibot/PROJECT-REPORT.md` + daily archive |
| `memory.py` | TF-IDF memory search over markdown files |
| `subagent.py` | Isolated sub-agent spawning with fresh context windows |
| `config.py` | Centralized configuration — paths, env vars, model settings, defaults |

### Data Flow (Chat Request)

```
User types message
    → POST /api/chat (SSE response)
    → Build prompt (system prompt + memory context + conversation history)
    → Send to AI provider (streaming)
    → For each response chunk:
        - text → stream to client
        - tool_call → execute tool → stream result → continue
        - elevation_required → pause, notify client, wait for approval
    → On completion: save to history, update token count
    → If over compaction threshold: self-compact via AI provider
```

### 2.1 Context Profiles & Accuracy

KukuiBot uses per-tab context profiles inferred from `session_id` prefixes:

- `codex` profile: **400k** context window, **300k** auto-compaction threshold
- `spark` profile: **175k** context window, **150k** auto-compaction threshold

Token accounting uses a hybrid strategy:

1. API-grounded input usage (`response.usage.input_tokens`) when available
2. Prompt+items estimate fallback (`len(json.dumps(...))//4` heuristic)
3. Effective blending (`api+delta`) to account for appended content after the last API request

Drift metrics are exposed via `/api/token-debug` and logged to `~/.kukuibot/logs/token-accuracy.log` for tuning and regression checks.

---

## 3. Current State (Completed)

### Backend ✅
- Full chat loop with SSE streaming and multi-turn tool execution
- Browser automation tools: `browser_open`, `browser_navigate`, `browser_click`, `browser_type`, `browser_extract`, `browser_snapshot`, `browser_close`
- Core tools: `bash`, `read_file`, `write_file`, `edit_file`, `spawn_agent` (plus background/memory/web utilities)
- Tiered security: default → auto-approve → root mode (10m TTL)
- Self-compact via connected AI provider
- Memory search via TF-IDF
- Disconnect resilience: event sequencing, resume endpoint, runtime restart detection
- Wake-listener mic resilience: falls back to the first available input device if default/default-selected macOS mic open fails after reboot or device churn
- Multi-provider auth: OpenAI OAuth PKCE, Claude Code CLI/API key, Anthropic API key, OpenRouter API key
- **HTTPS native** — uvicorn serves TLS directly (mkcert certs)
- Root CA download endpoint (`/api/cert`) for device onboarding
- Live OpenAI usage tracking (`/api/usage`)
- Context accounting telemetry (`/api/tokens`, `/api/token-debug`)
- Token drift logging (`~/.kukuibot/logs/token-accuracy.log`)
- Two-stage content guard (DeBERTa + Spark)
- **Max Sessions policy/status API** (`/api/max/config`, `/api/max/status`) — per-user persisted tab/session limits (see `docs/MAX_SESSIONS.md`)

### Frontend ✅
- Full vanilla JS port of StandaloneChatV2.jsx
- SSE streaming with real-time text display + TPS counter
- Unified work log (reasoning, tool calls, results, status)
- Elevation dialogs with approve/deny
- **Voice input** via Web Speech API (Safari Siri STT, Chrome Google STT)
  - Mic button toggles to send arrow when text is present
  - Auto-sends after 2s silence
  - Click textarea to cancel voice and edit transcript
  - Auto-restarts on Safari's 60s recognition limit
- **Reasoning picker** with tooltips: ⚡ Quick / L Low / M Medium / H High
- Multi-tab: multiple workers across providers, sidebar (desktop) + top bar (mobile)
- Cross-device tab metadata sync (`tab_meta`) with server-authoritative label hydration on boot
- Stable worker IDs for new tabs (legacy numeric IDs still supported)
- Narrow-screen worker creation modal (name input + OK/Cancel)
- Settings actions: delete current tab + restart server (both with confirmation modals)
- Tab delete API performs background cleanup of history, tab metadata, runtime stream state, and per-session elevation/approval state
- Hourly orphan-tab cleanup cron (`cleanup-orphan-tabs.sh`) prunes stale `tab_meta` rows with no corresponding history
- Weekly usage bar in sidebar and settings menu
- Status bar: context usage, token-source badge, TPS, reasoning picker, root mode countdown
- Auto-approve toggle, root mode toggle with countdown timer
- Slash commands: /compact, /clear, /reset, /status, /help
- Background/return stream recovery (resume after iOS suspension + network drops)
- Settings menu, visibility sync, audio beep on elevation
- Markdown rendering (marked.js) with XSS sanitization (DOMPurify)

### Setup Wizard ✅
- **Step 0: Trust Certificate** — detects LAN access, offers root CA download with per-OS instructions
- **Step 1: Create Account** — local admin user (bcrypt hashed), or skip for localhost-only mode
- **Step 2: Connect a Provider** — OpenAI OAuth, Claude Code, Anthropic API, OpenRouter (or skip to configure later)
- Auto-detects reconfiguration vs first-run

---

## 4. Security & First-Run Design

### 4.1 Problem Statement

Currently the app:
- Binds to `0.0.0.0:7000` (exposed to entire LAN) immediately
- Has no user authentication (anyone on the network can use it)
- Serves plain HTTP (no TLS)
- Requires manually configuring an AI provider with no guided onboarding

This is fine for development but unacceptable for any real deployment.

### 4.2 Design: First-Run Onboarding

On first launch (no users exist in the database), the app enters **setup mode**:

```
┌─────────────────────────────────────────────────────────┐
│  SETUP MODE (first launch only)                         │
│                                                         │
│  1. Bind to 127.0.0.1 ONLY                             │
│     - Cannot be reached from LAN during setup           │
│     - Prevents MITM or drive-by during credential entry │
│                                                         │
│  2. Serve setup wizard at /                             │
│     ┌─────────────────────────────────────────────┐     │
│     │  Step 1: Create Admin Account               │     │
│     │  - Username                                 │     │
│     │  - Password (shown strength meter)          │     │
│     │  - Confirm password                         │     │
│     │                                             │     │
│     │  Step 2: Connect AI Provider                 │     │
│     │  - OpenAI OAuth / Claude / Anthropic / OR   │     │
│     │  - Or skip to configure later               │     │
│     │                                             │     │
│     │  Step 3: Network Access                     │     │
│     │  - [ ] Enable LAN access (recommended)      │     │
│     │  - Hostname: kukuibot.local (auto-detected)    │     │
│     │  - Note: requires Caddy + mkcert for HTTPS  │     │
│     │                                             │     │
│     │  [Complete Setup →]                         │     │
│     └─────────────────────────────────────────────┘     │
│                                                         │
│  3. On submit:                                          │
│     - Hash password with bcrypt, store in SQLite        │
│     - Save ChatGPT token (encrypted at rest)            │
│     - Generate session cookie, log user in              │
│     - If LAN enabled: restart listener on 0.0.0.0      │
│     - Redirect to main chat UI                          │
└─────────────────────────────────────────────────────────┘
```

### 4.3 Design: Authentication System

#### Password Storage
- **bcrypt** with work factor 12 (not SHA-256 — resistant to GPU cracking)
- Stored in SQLite `users` table: `(username, password_hash, role, created_at)`
- Support for multiple users (admin + household roles)

#### Session Management
- On successful login: generate `secrets.token_hex(32)` session token
- Store in `sessions` table: `(token, username, role, created_at, expires_at)`
- Set as `httpOnly`, `secure`, `SameSite=Strict` cookie — 30-day expiry
- Session validation on every request via middleware

#### Localhost Auto-Trust
- Requests from `127.0.0.1` / `::1` are automatically authenticated as admin
- No login required when accessing from the machine itself
- This matches the pattern used by Home Assistant, Portainer, and similar self-hosted apps

#### Auth Middleware Flow
```
Every request:
  1. Is path in AUTH_EXEMPT set? → allow (login page, health, cert endpoint)
  2. Is client IP localhost? → auto-admin, skip auth
  3. Check session cookie → valid? → allow with role
  4. Otherwise → redirect to /login (HTML) or 401 (API)
```

#### Multi-User Support
| Role | Permissions |
|------|------------|
| `admin` | Full access — chat, tools, elevation, settings, user management |
| `household` | Chat access, no elevation approval, no settings changes |

### 4.4 Design: TLS Strategy

#### The Problem with Self-Signed Certs
Self-signed certificates always show browser warnings ("Your connection is not private"). Users must click through scary dialogs. This erodes trust and trains bad security habits.

#### Solution: mkcert (Already Deployed)

`mkcert` creates certificates signed by a locally-installed root CA. Once the root CA is trusted by the OS/browser, all mkcert certificates are trusted automatically — **zero warnings**.

**Current state on this machine:**
- mkcert installed (`/opt/homebrew/bin/mkcert`)
- Root CA generated and installed in macOS system keychain ✅
- Certs can be generated for any hostname/IP combination

#### TLS Architecture — Native HTTPS

KukuiBot serves HTTPS directly via uvicorn — **no reverse proxy required**.

```
Browser (any device)
    ↓ HTTPS (port 7000)
uvicorn + TLS (mkcert certs in kukuibot/certs/)
    ↓
FastAPI backend
```

**Cert generation (one-time):**
```bash
mkcert -cert-file certs/kukuibot.pem -key-file certs/kukuibot-key.pem \
  localhost 127.0.0.1 $(ipconfig getifaddr en0)
```

**Startup behavior:**
- If `certs/kukuibot.pem` + `certs/kukuibot-key.pem` exist → HTTPS on port 7000
- If certs missing → HTTP fallback on port 7000 (with console warning)

**Optional reverse proxy** (e.g. Caddy or nginx for path-based routing):
```
handle_path /kukuibot/* {
    reverse_proxy https://localhost:7000 {
        transport http { tls_insecure_skip_verify }
    }
}
```

**Optional Cloudflare tunnel** (for remote access with globally-trusted cert):
```yaml
ingress:
  - hostname: kukuibot.yourdomain.com
    service: https://localhost:7000
    originRequest:
      noTLSVerify: true
```

#### Device Onboarding (LAN clients)

The setup wizard (Step 0) auto-detects LAN access and guides users through cert installation:

1. **`GET /api/cert`** — downloads the mkcert root CA (`.pem` file)
2. Per-OS install instructions shown automatically:
   - **macOS**: Keychain Access → Always Trust
   - **iPhone/iPad**: Install Profile → Certificate Trust Settings → enable
   - **Windows**: Install Certificate → Trusted Root CAs
   - **Android**: Open file → enter PIN → user cert
3. Reload — all mkcert certs are trusted permanently, zero warnings

This endpoint is auth-exempt — it only serves the public root CA (not a secret).

### 4.5 Design: Binding Strategy

| State | Bind Address | Why |
|-------|-------------|-----|
| First run (setup mode) | `127.0.0.1:7000` | Credentials entered over localhost only — no LAN exposure |
| Normal (LAN disabled) | `127.0.0.1:7000` | User chose localhost-only access |
| Normal (LAN enabled) | `0.0.0.0:7000` | LAN access enabled |

The bind address is stored in config and persists across restarts.

### 4.6 Implementation Plan

#### Phase 1: Database Schema
```sql
-- New tables in kukuibot.db
CREATE TABLE users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,  -- bcrypt
    role TEXT NOT NULL DEFAULT 'admin',
    created_at INTEGER NOT NULL
);

CREATE TABLE sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (username) REFERENCES users(username)
);
```

#### Phase 2: Auth Module Updates
- Add bcrypt password hashing/verification to `auth.py`
- Add session create/verify/expire functions
- Add `is_first_run()` check (no users in DB)
- Add user CRUD (create, list, delete)

#### Phase 3: Middleware
- Add FastAPI middleware that checks every request
- Auth-exempt paths: `/login`, `/setup`, `/health`, `/api/cert`, static assets
- Localhost auto-trust
- Cookie-based session validation
- Redirect to `/login` or `/setup` as appropriate

#### Phase 4: Frontend Pages
- **Setup wizard** (`/setup`) — shown only on first run, multi-step form
- **Login page** (`/login`) — username/password form, shown when not authenticated
- **Main app** (`/`) — existing chat UI, shown when authenticated

#### Phase 5: Caddy Integration
- Add `/kukuibot/*` route to existing Caddyfile
- Test HTTPS access from Mac, iPhone, other LAN devices
- Add `/api/cert` endpoint for device onboarding

#### Phase 6: Config Persistence
- Store `lan_enabled`, `bind_address` in `config/app.json`
- On startup: read config, bind accordingly
- Settings page (admin only) to toggle LAN access post-setup

---

## 5. File Structure

```
kukuibot/
├── server.py              # FastAPI app, routes, SSE streaming
├── auth.py                # Token management, user auth, sessions
├── security.py            # Elevation, path guards, egress checks
├── tools.py               # Tool definitions and execution
├── compaction.py          # Self-compact via KukuiBot
├── memory.py              # TF-IDF search
├── subagent.py            # Sub-agent spawning
├── config.py              # Configuration, paths, env vars
├── requirements.txt       # Python dependencies
├── SECURITY.md            # Security policy documentation
├── static/
│   ├── index.html         # Main app shell
│   ├── app.js             # Chat UI (vanilla JS)
│   ├── style.css          # Dark theme styles
│   ├── setup.html         # First-run wizard (Phase 4)
│   └── login.html         # Login page (Phase 4)
├── config/
│   ├── security-policy.json
│   └── app.json           # Runtime config (Phase 6)
├── docs/
│   └── DESIGN.md          # This document
└── memory/                # Daily memory files
```

---

## 6. Dependencies

| Package | Purpose | Required |
|---------|---------|----------|
| fastapi | Web framework | ✅ |
| uvicorn | ASGI server | ✅ |
| httpx | Async HTTP client | ✅ |
| requests | Sync HTTP (compaction) | ✅ |
| scikit-learn | TF-IDF memory search | ✅ |
| python-multipart | Form data parsing | ✅ |
| bcrypt | Password hashing | ✅ (new) |

**Explicitly NOT dependencies:**
- ~~React / Node / npm~~ — frontend is vanilla JS
- ~~external databases~~ — fully standalone, SQLite only

---

## 7. Security Summary

| Layer | Mechanism |
|-------|-----------|
| Network | Localhost-only by default, opt-in LAN via config |
| Transport | TLS via mkcert (zero browser warnings) |
| Authentication | bcrypt passwords, httpOnly secure cookies, localhost auto-trust |
| Authorization | Role-based (admin/household), middleware on every request |
| Tool execution | Workspace-first boundary, elevation for dangerous ops |
| Egress control | Regex-based detection of network transfer commands |
| Self-modification | Protected file list requires elevation |
| Audit | Tool call logging, session tracking |

---

## 8. OAuth (LAN-Aware)

OAuth flow handles remote/LAN access gracefully:
- Callback server binds `0.0.0.0:1455` (accessible from LAN)
- **Iframe overlay** — OAuth login happens inline, no new tab
- **Remote detection** — yellow warning banner when not on localhost
- **Paste fallback** — copy/paste redirect URL if URL swap is inconvenient

## 9. Tab Sync (Cross-Device)

Tabs are **server-authoritative** with tombstone-based deletion:

- **`tab_meta`** table: stores tab ID, session ID, model, label per user
- **`tab_tombstones`** table: records deleted sessions per user
- **Sync flow (`POST /api/tabs/sync`):**
  - Client pushes local tabs to server
  - Server skips any session with an active tombstone (won't re-create deleted tabs)
  - Tombstones auto-expire after 7 days
- **Session list (`GET /api/history/sessions`):**
  - Returns `deleted[]` array of tombstoned session IDs
  - Client removes matching tabs from localStorage on load
- **Delete (`POST /api/tab-delete`):**
  - Clears history, tab_meta, runtime state, security approvals
  - Writes tombstone so other devices learn about the deletion

This ensures: delete a tab on Device A → Device B won't re-create it on next sync.

## 10. Future Considerations

- **TOTP/2FA** — for remote access scenarios (Cloudflare tunnel)
- **API keys** — for programmatic access (webhooks, automation)
- **Rate limiting** — prevent brute-force login attempts
- **Encrypted SQLite** — encrypt tokens at rest with a master key
- **Audit log UI** — surface tool-calls.log in the frontend for admin review
