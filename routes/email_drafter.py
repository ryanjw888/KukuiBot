"""Email Drafter API routes."""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger("kukuibot.drafter.routes")


@router.post("/api/drafter/chat/register")
async def api_drafter_chat_register(req: Request):
    """Register the email chat session in tab_meta so worker_identity resolves correctly."""
    from auth import db_connection, get_request_user
    import time
    try:
        body = await req.json()
        session_id = str(body.get("session_id", "")).strip()
        worker_identity = str(body.get("worker_identity", "assistant")).strip()
        model_key = str(body.get("model_key", "")).strip()
        if not session_id:
            return JSONResponse({"error": "session_id required"}, status_code=400)
        user_info = get_request_user(req)
        owner = user_info.get("user", "unknown") if isinstance(user_info, dict) else str(user_info or "unknown")
        with db_connection() as db:
            db.execute(
                """INSERT INTO tab_meta (owner, session_id, tab_id, model_key, label, worker_identity, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(owner, session_id) DO UPDATE SET
                     worker_identity = excluded.worker_identity,
                     model_key = excluded.model_key,
                     updated_at = excluded.updated_at""",
                (owner, session_id, "email-chat", model_key, "Email Chat", worker_identity, int(time.time())),
            )
            db.commit()
        return {"ok": True}
    except Exception as e:
        logger.warning(f"chat register error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/drafter/status")
async def api_drafter_status():
    """Return drafter status — Gmail connection, profile state, config."""
    from email_drafter import get_status
    try:
        return get_status()
    except Exception as e:
        logger.warning(f"drafter status error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/drafter/config")
async def api_drafter_config_get():
    """Return drafter configuration."""
    from email_drafter import get_config_dict
    return get_config_dict()


@router.post("/api/drafter/config")
async def api_drafter_config_set(req: Request):
    """Update drafter configuration."""
    from email_drafter import save_config_dict, get_config_dict
    try:
        body = await req.json()
        save_config_dict(body)
        return {"ok": True, "config": get_config_dict()}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@router.post("/api/drafter/run")
async def api_drafter_run():
    """Run the drafter — check inbox and create drafts. Long-running; uses asyncio.to_thread for IMAP."""
    from email_drafter import check_and_draft
    try:
        result = await asyncio.to_thread(_run_drafter_sync, False)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("drafter run error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/dry-run")
async def api_drafter_dry_run():
    """Dry run — shows what would be drafted without creating anything."""
    try:
        result = await asyncio.to_thread(_run_drafter_sync, True)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("drafter dry-run error")
        return JSONResponse({"error": str(e)}, status_code=500)


def _run_drafter_sync(dry_run: bool):
    """Wrapper to run the async check_and_draft inside to_thread's own event loop."""
    import asyncio
    from email_drafter import check_and_draft
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(check_and_draft(dry_run=dry_run))
    finally:
        loop.close()


@router.get("/api/drafter/drafts")
async def api_drafter_drafts():
    """List auto-drafted emails from Gmail Drafts folder."""
    from email_drafter import list_drafts
    try:
        drafts = await asyncio.to_thread(list_drafts)
        return {"drafts": drafts}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"list drafts error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/drafter/drafts/{uid}/original")
async def api_drafter_original(uid: str):
    """Fetch the original email that a draft is replying to."""
    from email_drafter import get_original_for_draft
    try:
        result = await asyncio.to_thread(get_original_for_draft, uid)
        if result.get("not_found"):
            return JSONResponse(result, status_code=404)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.warning(f"get original error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/drafts/{uid}/send")
async def api_drafter_send(uid: str):
    """Send a specific auto-drafted email."""
    from email_drafter import send_draft
    try:
        result = await asyncio.to_thread(send_draft, uid)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception(f"send draft error: uid={uid}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/drafts/{uid}/discard")
async def api_drafter_discard(uid: str):
    """Discard (delete) a specific auto-drafted email."""
    from email_drafter import discard_draft
    try:
        result = await asyncio.to_thread(discard_draft, uid)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception(f"discard draft error: uid={uid}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/ai-reply")
async def api_drafter_ai_reply(req: Request):
    """Generate an AI reply for a single message on demand."""
    def _generate(from_addr, subject, body, message_id):
        import asyncio
        from email_drafter import generate_ai_reply
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                generate_ai_reply(from_addr, subject, body, message_id)
            )
        finally:
            loop.close()

    try:
        body = await req.json()
        from_addr = body.get("from", "")
        subject = body.get("subject", "")
        msg_body = body.get("body", "")
        message_id = body.get("message_id", "")
        if not from_addr or not subject:
            return JSONResponse({"error": "from and subject are required"}, status_code=400)
        result = await asyncio.to_thread(_generate, from_addr, subject, msg_body, message_id)
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except Exception as e:
        logger.exception("ai-reply error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/drafter/history")
async def api_drafter_history(limit: int = 50, offset: int = 0, action: str = ""):
    """Return paginated drafter history."""
    from email_drafter import get_history
    return get_history(limit=min(limit, 200), offset=offset, action_filter=action)


@router.get("/api/drafter/profile")
async def api_drafter_profile():
    """Return the current style profile text."""
    from email_drafter import STYLE_PROFILE_PATH
    try:
        if STYLE_PROFILE_PATH.exists():
            text = STYLE_PROFILE_PATH.read_text(encoding="utf-8")
            return {"exists": True, "text": text, "size": len(text)}
        return {"exists": False, "text": "", "size": 0}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/profile/rebuild")
async def api_drafter_profile_rebuild():
    """Rebuild the style profile from sent emails."""
    def _rebuild():
        import asyncio
        from email_drafter import build_style_profile
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(build_style_profile(force=True))
        finally:
            loop.close()

    try:
        text = await asyncio.to_thread(_rebuild)
        return {"ok": True, "size": len(text)}
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    except Exception as e:
        logger.exception("profile rebuild error")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/drafter/profile/save")
async def api_drafter_profile_save(req: Request):
    """Save manually edited style profile text."""
    from email_drafter import STYLE_PROFILE_PATH, _load_state, _save_state
    import time
    try:
        body = await req.json()
        text = body.get("text", "")
        if not text or len(text.strip()) < 10:
            return JSONResponse({"error": "Profile text is too short"}, status_code=400)
        STYLE_PROFILE_PATH.write_text(text, encoding="utf-8")
        # Update profile_built_at so freshness indicator stays accurate
        state = _load_state()
        state["profile_built_at"] = int(time.time())
        _save_state(state)
        logger.info(f"Style profile saved manually ({len(text)} chars)")
        return {"ok": True, "size": len(text)}
    except Exception as e:
        logger.exception("profile save error")
        return JSONResponse({"error": str(e)}, status_code=500)
