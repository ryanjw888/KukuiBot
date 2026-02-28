# Context Window Tracking — Claude Code Tabs

How KukuiBot estimates and displays context window usage for Claude Code (CLI) tabs.

## The Problem

The Claude CLI (`claude --print`) aggregates token usage across **all API calls within a single turn**. When Claude uses tools (Read, Bash, Grep, etc.), each tool-use cycle is a separate API call to Anthropic. The `result` event's `usage` object sums all calls together.

For example, a turn with 10 tool calls where each call sends ~200K cached context reports `cache_read_input_tokens: 2,000,000` — 10x the actual context window size. Naively using this number makes the context appear to exceed the 1M window.

## The Formula

```
context_per_call = (input_tokens + cache_read_input_tokens + cache_creation_input_tokens) / iterations
```

Where:
- `input_tokens` — uncached tokens (new content not in any cache)
- `cache_read_input_tokens` — tokens read from prompt cache (aggregated across all API calls)
- `cache_creation_input_tokens` — tokens written to cache for the first time (aggregated)
- `iterations` — number of `assistant` events observed during this turn (each = one API call)
### Why divide by iterations?

Each API call in a multi-tool turn sends roughly the same context (system prompt + conversation history + tool results). The CLI aggregates cache reads across all calls. Dividing by the number of calls recovers the per-call context size — what the model actually sees in one request.

### Why include cache_creation?

`cache_creation` tokens are tokens sent to the model that were cached for the first time. They ARE part of the context — they just weren't in the cache yet. Excluding them (as a previous version did) would undercount context on first messages or after cache expiry.

## Data Flow

```
Claude CLI (subprocess)
  │
  ├─ assistant event ──► _turn_iterations++ (claude_bridge.py:624)
  │                       Count API calls within current turn
  │
  ├─ result event ──────► Calculate ctx_input = total / iters (claude_bridge.py:660)
  │                       Store in proc.last_input_tokens
  │                       Persist to .claude_session_{slot}.json
  │                       Broadcast to subscribers
  │
  └─ (turn complete) ──► Reset _turn_iterations = 0
```

### SSE Delivery to Frontend

Two pathways deliver context info to the browser:

1. **Real-time SSE** (`/api/claude/events`):
   - On `result` event, emits a `context` event with `{tokens, max, pct}` before the `done` event
   - Frontend `handleEvent()` sets `tab.contextInfo` from this event
   - File: `server.py:3493-3497`

2. **Poll** (`refreshMeta()` → `/api/claude/status`):
   - Called after every `done` event
   - Fetches `last_input_tokens` from the process status endpoint
   - Sets `tab.contextInfo` as a fallback/confirmation
   - File: `app.js:3364-3367`

### Display Formatting

`fmtCtx()` in `app.js:1255` formats the context display:
- Under 100K tokens: shows as `K` (e.g. `20K / 1M`)
- 100K and above: shows as `M` with one decimal (e.g. `0.2M / 1M`)
- Max is shown as `M` when >= 500K, otherwise `K`

This avoids the previous issue where small values displayed as `0.0M / 1M`.

## Token Counters

| Counter | Meaning | Scope |
|---------|---------|-------|
| `last_input_tokens` | Estimated context size from most recent turn | Per-call (divided by iterations) |
| `peak_input_tokens` | Highest `last_input_tokens` seen this session | Session lifetime |
| `total_input_tokens` | Cumulative billing total (all input tokens) | Session lifetime (grows forever) |
| `total_output_tokens` | Cumulative output tokens | Session lifetime |

## Files Changed

| File | What |
|------|------|
| `claude_bridge.py:365` | `_turn_iterations` counter added to `PersistentClaudeProcess` |
| `claude_bridge.py:624` | Increment counter on each `assistant` event |
| `claude_bridge.py:649-670` | Divide aggregated usage by iterations (no cap — diagnostic visibility) |
| `claude_bridge.py:815` | Reset iteration counter when sending a new message |
| `server.py:3493-3497` | Emit `context` SSE event from `/api/claude/events` on result |
| `app.js:1255-1261` | `fmtCtx()` — K format under 100K, M format above |
| `app.js:139-150` | Handle `context` and `chunk` events when tab not loading |
| `app.js:2014` | `handleEvent` accepts `chunk` type as text |

## Cross-Platform Comparison (KukuiBot Comparison)

Both apps use the same Claude Code CLI but had different token counting strategies:

| | KukuiBot (Strategy B) | Legacy (Strategy A → C) |
|---|---|---|
| **Division** | Divide by `_turn_iterations` | Was raw sum (5-6x inflation), now divides by iterations |
| **Cap** | ~~`min(ctx_input, CONTEXT_WINDOW)`~~ Removed | No cap (never had one) |
| **`context_window` source** | Hardcoded `CONTEXT_WINDOW = 1_000_000` | Dynamic from CLI's `modelUsage.contextWindow` |
| **Process model** | Pool (up to 3 processes, counters reset on respawn) | Single persistent process |
| **Observed peak** | 1,980,682 (2x window, before cap) | 5,722,803 (5.7x window, raw sum era) |

### Why the cap was removed

The hard cap (`min(ctx_input, CONTEXT_WINDOW)`) hid how far off the estimate was. If the divided number exceeds the context window, that's a signal the iteration count doesn't match reality — a key diagnostic. With the cap removed, inflated values are visible in logs and session state for debugging.

### Convergence

As of 2026-02-20, both platforms now use the same strategy:
- Divide aggregate token count by `assistant` event count (iterations)
- No hard cap
- Legacy additionally extracts `context_window` from `modelUsage` (KukuiBot should adopt this)

## Known Limitations

- The iteration-division is a heuristic. If different API calls within a turn have significantly different context sizes (e.g. early calls are smaller), the average may not perfectly represent the final context size.
- The `iterations` field in the CLI's usage object is currently empty (`[]`), so we can't use per-iteration breakdowns even if they existed.
- Context ack (first message context injection) updates `last_input_tokens` but the server-side handler consumes and discards the ack — so the first real update comes from the user's actual message response.
- Dividing gives the **average** per-call input, not the **last-call** input (which is the actual current context size and the largest).
