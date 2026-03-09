# MEMORY.md — Agent Long-Term Memory

_This file is the agent's persistent memory across sessions. It reads this on startup and updates it as it learns._

## Chat History Recall (IMPORTANT)

**When asked to recall or find prior conversation content, ALWAYS query the database first — never rely on compaction summaries or in-context memory.**

Compaction is lossy — it routinely drops entire topics. The DB has the complete, searchable record of every message.

### Quick Queries
```sql
-- Find assistant messages by keyword (replace SESSION_ID with your tab's session ID from Available Workers)
SELECT run_id, datetime(started_at, 'unixepoch', 'localtime') as ts,
       substr(final_text, 1, 300) as preview
FROM chat_runs
WHERE session_id='SESSION_ID' AND final_text LIKE '%keyword%'
ORDER BY started_at ASC;

-- Find user messages by keyword
SELECT json_extract(event_json, '$.text') as msg,
       datetime(created_at, 'unixepoch', 'localtime') as ts
FROM chat_events
WHERE session_id='SESSION_ID'
  AND json_extract(event_json, '$.type')='user_message'
  AND json_extract(event_json, '$.text') LIKE '%keyword%'
ORDER BY created_at ASC;

-- Recent conversation (last N exchanges)
SELECT datetime(started_at, 'unixepoch', 'localtime') as ts,
       substr(final_text, 1, 300) as preview
FROM chat_runs
WHERE session_id='SESSION_ID'
ORDER BY started_at DESC LIMIT 20;
```

**DB path:** `/Users/jarvis/.kukuibot/kukuibot.db`
**Tables:** `chat_runs` (assistant responses + metadata), `chat_events` (all events including user messages)
**Session ID:** Check the "Available Workers" table in your system prompt — your row is marked `(you)`

## Identity
- **App:** KukuiBot
- **First run:** 2026-02-27

## Key Decisions
_Record important architectural choices, user preferences, and lessons learned here._

## Projects
_Track ongoing work, status, and next steps._

## System Notes
_Environment-specific details the agent discovers and should remember._

---
