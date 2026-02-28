# KukuiBot App — Security Policy

## 1) Runtime Security

### Tool & Filesystem Controls
- **Workspace-first boundary**: All file tools (read, write, edit) are restricted to the workspace directory (`~/.kukuibot/`) by default.
- **Paths outside workspace** require explicit elevation (user approval).
- **Elevation patterns**: Certain shell commands trigger an approval prompt before execution.
- **Egress controls**: Network transfer commands (curl, wget, scp, etc.) require approval.
- **Self-modification protection**: Core app files (server.py, tools.py, etc.) require approval to modify.

### Elevation System
The app uses a tiered security model:

1. **Default mode**: Most operations run freely within the workspace. Dangerous commands or out-of-workspace access trigger an elevation prompt.
2. **Auto Approve**: Toggle in the status bar. When enabled, all elevation prompts are auto-approved for the current session. Use when doing trusted bulk work.
3. **Root mode (10m)**: Time-limited elevated session. All restrictions bypassed for 10 minutes. Use for system administration tasks.

### Blocked Patterns (require elevation)
```
sudo, launchctl, systemctl, rm -rf,
curl, wget, scp, sftp, rsync, nc, ncat, netcat, ftp,
python -c, python3 -c
```

### Protected Files (require elevation to write)
- Identity files: SOUL.md, USER.md, IDENTITY.md, AGENTS.md
- Memory files: TOOLS.md, MEMORY.md  
- Core app files: server.py, tools.py, security.py, auth.py, config.py

## 2) Authentication

- OAuth tokens stored in SQLite (`~/.kukuibot/kukuibot.db`)
- JWT-based account ID extraction from ChatGPT tokens
- Token expiry tracking with auto-refresh support
- Manual token paste as fallback authentication method

## 3) Data Handling

### Conversation History
- Stored locally in SQLite — never sent to external services
- Compaction summaries saved to daily memory files
- Session reset clears all history for that session

### Memory Files
- MEMORY.md and memory/*.md are searchable via TF-IDF
- Memory search is restricted to workspace paths
- No external indexing or telemetry

## 4) Configuration

Security policy is defined in `config/security-policy.json` and loaded at startup.

### Policy Fields
| Field | Description |
|-------|-------------|
| `workspace_root` | Root directory for workspace-first access control |
| `allow_read_paths` | Additional paths allowed for reading (beyond workspace) |
| `allow_write_paths` | Additional paths allowed for writing (beyond workspace) |
| `elevate_write_paths` | Specific files that always require elevation to write |
| `elevate_self_modify` | Filename patterns that trigger self-modification protection |
| `elevate_bash_patterns` | Shell command substrings that trigger elevation |
| `egress_command_regex` | Regex for network transfer commands requiring elevation |
| `blocked_write_files` | Paths that are hard-blocked from writes by tools |

## 5) Health Update Security Snapshot

Settings → **🩺 Health Update** runs a fast local security snapshot (`GET /api/security-quick-check`) and renders results in chat.

Current quick checks include:
- sudo posture (`NOPASSWD: ALL` = critical)
- macOS firewall status (**warn-only** when disabled)
- Remote Login (SSH) state
- `/etc/sudoers.d` mode hygiene (expects `0440`)
- FileVault status
- SIP (System Integrity Protection) status

The response includes per-check `state` + `detail`, plus severity-tagged findings and an overall roll-up.

## 6) Best Practices

- Start in default mode. Only enable Auto Approve or Root when needed.
- Review elevation prompts — they show the exact command and reason.
- Root mode auto-expires after 10 minutes for safety.
- Keep `security-policy.json` updated if you add new protected paths.
- Regularly check `logs/tool-calls.log` for audit trail.
