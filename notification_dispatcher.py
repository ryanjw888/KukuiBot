"""
notification_dispatcher.py — Event-driven per-session notification delivery.

Replaces the inline delivery logic in _deliver_or_queue_parent_notification with
a clean event-driven dispatcher. Notifications are enqueued to DB (via
notification_store), then an asyncio.Event triggers immediate dispatch.

State machine (from notification_store):
  pending → claimed → injected → consumed | failed

This module has minimal imports from server.py — only what's needed to check
subprocess idle state and fire proactive wakes. All DB operations go through
notification_store.
"""

import asyncio
import logging
from typing import Any, Callable, Coroutine

import notification_store

logger = logging.getLogger("kukuibot.notification_dispatcher")

# Type aliases for callbacks injected from server.py
GetClaudePoolFn = Callable[[], Any]
EnsureSubprocessFn = Callable[[Any, str], Coroutine[Any, Any, Any]]
TryProactiveWakeFn = Callable[..., Coroutine[Any, Any, bool]]
GetActiveTasksFn = Callable[[], dict[str, dict]]


class NotificationDispatcher:
    """Event-driven per-session notification dispatcher.

    Instead of polling, uses asyncio.Event per session to trigger immediate
    dispatch attempts when notifications are enqueued or subprocesses become idle.
    """

    def __init__(
        self,
        get_claude_pool: GetClaudePoolFn,
        ensure_subprocess: EnsureSubprocessFn,
        try_proactive_wake: TryProactiveWakeFn,
        get_active_tasks: GetActiveTasksFn,
    ):
        self._get_claude_pool = get_claude_pool
        self._ensure_subprocess = ensure_subprocess
        self._try_proactive_wake = try_proactive_wake
        self._get_active_tasks = get_active_tasks

        # Per-session events: signal when a session has pending work
        self._events: dict[str, asyncio.Event] = {}
        # Per-session dispatch tasks
        self._tasks: dict[str, asyncio.Task] = {}
        # Reconciler task
        self._reconciler_task: asyncio.Task | None = None
        # Shutdown flag
        self._stopped = False
        # Max concurrent per-session dispatchers to prevent unbounded growth
        self._max_sessions = 100

    async def start(self) -> None:
        """Start the reconciler and fan-out pending sessions from DB."""
        self._stopped = False
        self._reconciler_task = asyncio.create_task(self._reconcile_loop())
        logger.info("NotificationDispatcher started")

        # Fan-out: trigger dispatch for any sessions with pending notifications in DB
        try:
            pending_sessions = notification_store.list_sessions_with_pending()
            for sid in pending_sessions:
                self._trigger(sid)
            if pending_sessions:
                logger.info(f"NotificationDispatcher: startup fan-out triggered for {len(pending_sessions)} session(s)")
        except Exception as e:
            logger.warning(f"NotificationDispatcher: startup fan-out failed: {e}")

    async def stop(self) -> None:
        """Stop the dispatcher and all per-session tasks."""
        self._stopped = True
        # Cancel reconciler
        if self._reconciler_task and not self._reconciler_task.done():
            self._reconciler_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._reconciler_task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        # Cancel all per-session tasks
        for sid, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        # Wait briefly for cancellation
        if self._tasks:
            await asyncio.sleep(0.1)
        self._tasks.clear()
        self._events.clear()
        logger.info("NotificationDispatcher stopped")

    def trigger_enqueue(self, session_id: str) -> None:
        """Called after notification_store.enqueue() — signal dispatch attempt."""
        self._trigger(session_id)

    def trigger_subprocess_idle(self, session_id: str) -> None:
        """Called when a subprocess finishes a run — try delivering pending notifications."""
        self._trigger(session_id)

    def _trigger(self, session_id: str) -> None:
        """Set the per-session event, creating the dispatch loop task if needed."""
        if self._stopped:
            return

        # Get or create the event
        if session_id not in self._events:
            if len(self._events) >= self._max_sessions:
                # Evict the oldest session that has no pending task
                self._evict_stale_session()
            self._events[session_id] = asyncio.Event()

        self._events[session_id].set()

        # Ensure a dispatch loop task exists for this session
        task = self._tasks.get(session_id)
        if task is None or task.done():
            self._tasks[session_id] = asyncio.create_task(
                self._session_dispatch_loop(session_id)
            )

    def _evict_stale_session(self) -> None:
        """Remove one stale session event/task to stay under _max_sessions."""
        for sid in list(self._events):
            task = self._tasks.get(sid)
            if task is None or task.done():
                self._events.pop(sid, None)
                self._tasks.pop(sid, None)
                return
        # All sessions have active tasks — force-evict the first one
        if self._events:
            sid = next(iter(self._events))
            task = self._tasks.pop(sid, None)
            if task and not task.done():
                task.cancel()
            self._events.pop(sid, None)

    async def _session_dispatch_loop(self, session_id: str) -> None:
        """Per-session event loop: wait for trigger, attempt dispatch, repeat.

        Exits after 60s of no activity to avoid leaking tasks for idle sessions.
        """
        idle_timeout = 60.0
        try:
            while not self._stopped:
                event = self._events.get(session_id)
                if event is None:
                    return

                # Wait for trigger or timeout
                try:
                    await asyncio.wait_for(event.wait(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    # No activity for this session — clean up and exit
                    logger.debug(f"NotificationDispatcher: session loop idle timeout for {session_id}")
                    return

                event.clear()

                # Small delay to batch rapid-fire triggers (e.g. multiple tasks completing)
                await asyncio.sleep(0.15)

                # Attempt dispatch
                try:
                    count = await self.dispatch_pending(session_id)
                    if count > 0:
                        logger.info(f"NotificationDispatcher: delivered {count} notification(s) to {session_id}")
                except Exception as e:
                    logger.warning(f"NotificationDispatcher: dispatch error for {session_id}: {e}")

        except asyncio.CancelledError:
            pass
        finally:
            # Cleanup
            self._events.pop(session_id, None)
            self._tasks.pop(session_id, None)

    async def dispatch_pending(self, session_id: str) -> int:
        """Deliver pending notifications to a session if subprocess is idle.

        Returns count of notifications delivered.
        """
        # Claim from DB
        ids, payloads = notification_store.claim(session_id, limit=20)
        if not ids:
            return 0

        # Check if subprocess is idle
        pool = self._get_claude_pool()
        if not pool:
            # No pool — release claims back to pending
            self._release_claims(ids)
            return 0

        proc = await self._ensure_subprocess(pool, session_id)
        if not proc:
            # Can't get subprocess — release claims
            self._release_claims(ids)
            return 0

        # Check idle state
        if not self._is_subprocess_idle(session_id, proc):
            # Busy — release claims back to pending so they can be
            # drained by the in-flight run or a later dispatch
            self._release_claims(ids)
            return 0

        # Format notification text
        rendered = []
        for payload in payloads:
            rendered.append(notification_store.render_notification(payload))
        notify_text = "\n\n".join(rendered)

        # Queue in-memory on subprocess (for drain_notifications path)
        proc.queue_notification(notify_text)

        # Broadcast SSE event to browser
        for nid, payload in zip(ids, payloads):
            proc._broadcast({
                "type": "delegation_notification",
                "notification_id": nid,
                "task_id": payload.get("task_id", ""),
                "status": payload.get("to_status", ""),
                "message": notification_store.render_notification(payload),
                "created_at": payload.get("created_at", 0),
            })

        # Mark as injected in DB
        notification_store.mark_injected(ids)

        # Fire proactive wake if idle
        task_id = payloads[0].get("task_id", "dispatch") if payloads else "dispatch"
        to_status = payloads[0].get("to_status", "notification") if payloads else "notification"
        woke = await self._try_proactive_wake(
            session_id, proc, notify_text, task_id, to_status,
            label="dispatcher: ",
        )
        if woke:
            # Proactive wake succeeded — mark consumed via dedupe
            for payload in payloads:
                t_id = payload.get("task_id", "")
                t_status = payload.get("to_status", "")
                if t_id and t_status:
                    notification_store.mark_consumed_by_dedupe(
                        session_id, f"{t_id}:{t_status}"
                    )

        return len(ids)

    def _is_subprocess_idle(self, session_id: str, proc: Any) -> bool:
        """Check if a subprocess can accept injected content.

        All conditions must be true:
        - proc exists and alive (returncode is None)
        - proc.stdin exists and writable
        - chat_lock not held (proc.is_busy == False)
        - No active run for this session
        """
        if proc.proc is None or proc.proc.returncode is not None:
            return False
        if proc.is_busy:
            return False
        # Check _active_tasks for running task
        active_tasks = self._get_active_tasks()
        current = active_tasks.get(session_id, {})
        cur_status = str(current.get("status") or "")
        cur_task = current.get("task")
        if (
            cur_status == "running"
            and cur_task is not None
            and not cur_task.done()
        ):
            return False
        return True

    def _release_claims(self, ids: list[str]) -> None:
        """Release claimed notifications back to pending state."""
        if not ids:
            return
        try:
            from auth import db_connection
            with db_connection() as db:
                db.execute(
                    f"UPDATE delegation_notifications SET state='pending', claimed_at=NULL "
                    f"WHERE id IN ({','.join('?' * len(ids))}) AND state='claimed'",
                    ids,
                )
                db.commit()
            logger.debug(f"NotificationDispatcher: released {len(ids)} claim(s) back to pending")
        except Exception as e:
            logger.warning(f"NotificationDispatcher: failed to release claims: {e}")

    async def _reconcile_loop(self) -> None:
        """Periodic reconciler — safety net for missed events.

        Runs every 45s. Finds sessions with pending notifications
        and triggers dispatch for them.
        """
        # Initial delay to let startup complete
        await asyncio.sleep(10)
        logger.info("NotificationDispatcher: reconciler started (45s interval)")

        while not self._stopped:
            try:
                await asyncio.sleep(45)
                stats = await self.reconcile_once()
                if stats.get("triggered", 0) > 0:
                    logger.info(f"NotificationDispatcher reconciler: {stats}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"NotificationDispatcher reconciler error: {e}")
                await asyncio.sleep(15)

    async def reconcile_once(self) -> dict[str, int]:
        """Single reconciliation pass. Returns stats dict."""
        stats = {"triggered": 0, "recovered": 0}
        try:
            # Find sessions with pending notifications
            pending_sessions = notification_store.list_sessions_with_pending()
            for sid in pending_sessions:
                self._trigger(sid)
                stats["triggered"] += 1

            # Run recovery for stale claimed/injected states
            recovery = notification_store.recover(max_attempts=3, retention_seconds=86400)
            stats["recovered"] = sum(recovery.values())
        except Exception as e:
            logger.warning(f"NotificationDispatcher reconcile error: {e}")
        return stats

    def cleanup_session(self, session_id: str) -> None:
        """Clean up per-session state when a session is reaped."""
        event = self._events.pop(session_id, None)
        task = self._tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
