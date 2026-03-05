"""
tts_bridge.py — Client for the Kokoro TTS microservice.

Provides async and sync interfaces for the main server and wake-listener.
Communicates with tts_service.py over HTTP on 127.0.0.1:5090.
"""

import json
import logging
import os
import subprocess
import time
from urllib import request as urlrequest

logger = logging.getLogger("tts-bridge")

TTS_SERVICE_URL = os.environ.get(
    "TTS_SERVICE_URL", "http://127.0.0.1:5090"
).rstrip("/")

# ---------------------------------------------------------------------------
# Async interface (for main server — uses httpx)
# ---------------------------------------------------------------------------

async def speak_text(
    text: str,
    voice: str = "bm_daniel",
    speed: float = 1.0,
    play: bool = True,
) -> dict:
    """Generate TTS audio and optionally play it. Returns timing info.

    Args:
        text: Text to speak.
        voice: Kokoro voice ID (default: bm_daniel).
        speed: Playback speed multiplier.
        play: If True, play audio via afplay after generation.
    """
    import httpx

    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TTS_SERVICE_URL}/tts/speak/file",
                json={"text": text, "voice": voice, "speed": speed},
            )
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        logger.error(f"TTS speak/file failed: {e}")
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        return result

    elapsed_gen = time.time() - t0
    result["total_time"] = round(elapsed_gen, 3)

    if play:
        played = await _play_audio(result.get("path", ""))
        result["played"] = played

    return result


async def _play_audio(filepath: str) -> bool:
    """Try Sonos first, fall back to afplay."""
    # Try Sonos
    sonos_ok = await speak_to_sonos_file(filepath)
    if sonos_ok:
        return True
    # Fallback: afplay
    return await _afplay_async(filepath)


async def speak_to_sonos_file(filepath: str) -> bool:
    """Play a local WAV file on Sonos via the TTS service's Sonos endpoint."""
    import httpx

    # Sonos needs a URL it can fetch — use the KukuiBot static server URL
    # The file is at src/static/tts/<filename>, served at /tts/<filename>
    filename = os.path.basename(filepath)
    # Sonos needs an HTTP URL it can reach on the LAN
    local_ip = _get_local_ip()
    audio_url = f"https://{local_ip}:7000/tts/{filename}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{TTS_SERVICE_URL}/tts/play/sonos",
                json={"url": audio_url},
            )
            result = resp.json()
            if result.get("ok"):
                logger.info(f"Playing on Sonos: {result.get('speaker')}")
                return True
            logger.debug(f"Sonos unavailable: {result.get('error')}")
    except Exception as e:
        logger.debug(f"Sonos play failed: {e}")
    return False


async def _afplay_async(filepath: str) -> bool:
    """Play audio via macOS afplay (non-blocking)."""
    import asyncio

    if not os.path.isfile(filepath):
        logger.warning(f"Audio file not found: {filepath}")
        return False

    loop = asyncio.get_event_loop()

    def _do():
        try:
            subprocess.run(
                ["afplay", filepath],
                timeout=60, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    return await loop.run_in_executor(None, _do)


async def tts_health() -> dict:
    """Check TTS service health."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{TTS_SERVICE_URL}/tts/health")
            return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Sync interface (for wake-listener — no async, no httpx dependency)
# ---------------------------------------------------------------------------

def speak_text_sync(
    text: str,
    voice: str = "bm_daniel",
    speed: float = 1.0,
    play: bool = True,
) -> dict:
    """Synchronous TTS: generate audio and play via afplay.

    Uses stdlib urllib only (wake-listener runs under python3.12 without
    necessarily having httpx).
    """
    t0 = time.time()
    payload = json.dumps({"text": text, "voice": voice, "speed": speed}).encode()

    try:
        req = urlrequest.Request(
            f"{TTS_SERVICE_URL}/tts/speak/file",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"TTS speak/file failed: {e}")
        return {"ok": False, "error": str(e)}

    if not result.get("ok"):
        return result

    result["total_time"] = round(time.time() - t0, 3)

    if play:
        filepath = result.get("path", "")
        if filepath and os.path.isfile(filepath):
            try:
                # Non-blocking afplay — don't wait for it to finish
                subprocess.Popen(
                    ["afplay", filepath],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                result["played"] = True
                logger.info(
                    f"TTS playing: {result.get('duration', 0):.1f}s audio "
                    f"(gen {result.get('generation_time', 0):.3f}s)"
                )
            except FileNotFoundError:
                result["played"] = False
                logger.warning("afplay not available")
        else:
            result["played"] = False

    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_local_ip() -> str:
    """Get local LAN IP for Sonos URL construction."""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
