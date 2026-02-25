# Max Sessions (KukuiBot)

KukuiBot supports **per-user** limits on the number of persisted worker sessions (tabs).

A “session” here corresponds to a worker tab `session_id` that is persisted via `POST /api/tabs/sync` into the `tab_meta` table.

## UI

- Open **Max Sessions** at: `https://<host>:7000/max/`
- The page loads and edits limits via the API endpoints documented below.

## API

### Get policy

`GET /api/max/config`

Returns the current merged policy (defaults + persisted values):

```json
{
  "ok": true,
  "owner": "user",
  "limits": {
    "max_total_sessions": 20,
    "max_codex_sessions": 12,
    "max_spark_sessions": 8
  }
}
```

### Set policy (admin only)

`POST /api/max/config`

Payload:

```json
{
  "max_total_sessions": 20,
  "max_codex_sessions": 12,
  "max_spark_sessions": 8
}
```

Notes:
- Values must be integers.
- Values must be between **0** and **500**.
- If `max_total_sessions > 0`, then `max_codex_sessions` and `max_spark_sessions` may not exceed it.
- Setting a limit to `0` disables that bucket.

### Get status

`GET /api/max/status`

Returns persisted counts for the current owner (excluding tombstoned sessions) plus convenience flags:

```json
{
  "ok": true,
  "owner": "user",
  "active_total": 12,
  "active_codex": 7,
  "active_spark": 5,
  "limits": {
    "max_total_sessions": 20,
    "max_codex_sessions": 12,
    "max_spark_sessions": 8
  },
  "at_total_limit": false,
  "at_codex_limit": false,
  "at_spark_limit": false,
  "at_any_limit": false
}
```

## Enforcement (Phase C)

Enforcement happens in two places:

1) **Client-side pre-check** (fast UX)
- When creating a new worker from the UI modal, the client checks `/api/max/status`.
- If at limit, creation is blocked and the user is shown an error.

2) **Server-side authoritative enforcement**
- `POST /api/tabs/sync` rejects *new* session_ids beyond policy.
- The response includes `rejected_session_ids` so the client can remove locally-created tabs and show a banner.

## Persistence

Policy is stored in SQLite `config` under:
- `max_sessions.policy_json`

Counts are derived from:
- `tab_meta` (per-owner persisted sessions)
- `tab_tombstones` (exclude deletions)

## FAQ

### What does “active session” mean?
KukuiBot does not use a browser heartbeat. “Active” means “persisted in `tab_meta` for this owner and not tombstoned.”

### What happens if limits are lowered below current usage?
No sessions are deleted automatically. New session creation is blocked until usage is under the limit.
