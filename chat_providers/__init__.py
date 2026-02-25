"""
chat_providers — Chat provider modules extracted from server.py (Phase 10).

Each provider implements the async processing loop for a specific model backend.
Common utilities shared across providers live in this __init__.
"""

import asyncio
import logging

logger = logging.getLogger("kukuibot.chat_providers")


async def run_with_keepalive(coro, session_id: str, queue: asyncio.Queue, run_id: str, interval: float = 15.0, *, emit_event=None):
    """Run an awaitable while sending SSE keepalive pings to prevent connection drops."""
    done_event = asyncio.Event()

    async def _pinger():
        idx = 0
        while not done_event.is_set():
            try:
                await asyncio.wait_for(asyncio.shield(done_event.wait()), timeout=interval)
                return  # event was set
            except asyncio.TimeoutError:
                pass
            idx += 1
            if emit_event:
                await emit_event(session_id, queue, {"type": "ping", "keepalive": idx}, run_id=run_id)

    pinger_task = asyncio.create_task(_pinger())
    try:
        return await coro
    finally:
        done_event.set()
        pinger_task.cancel()
        try:
            await pinger_task
        except (asyncio.CancelledError, Exception):
            pass
