#!/opt/homebrew/bin/python3.12
"""
tts_service.py — Kokoro-82M TTS microservice.

Standalone FastAPI app that keeps the Kokoro model pre-loaded in memory.
Runs under /opt/homebrew/bin/python3.12 (has kokoro, torch, soco).
Binds to 127.0.0.1:5090 only (local traffic).

Endpoints:
  GET  /tts/health        — Health check + model status
  POST /tts/speak         — Stream WAV audio chunks
  POST /tts/speak/file    — Generate complete WAV, return file URL
  POST /tts/play/sonos    — Play a URL on Sonos speakers
"""

import asyncio
import io
import logging
import os
import struct
import sys
import time
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TTS_PORT = int(os.environ.get("TTS_SERVICE_PORT", "5090"))
TTS_HOST = "127.0.0.1"
TTS_VOICE = os.environ.get("TTS_DEFAULT_VOICE", "bm_daniel")
TTS_SPEED = float(os.environ.get("TTS_DEFAULT_SPEED", "1.0"))
SAMPLE_RATE = 24000
STATIC_TTS_DIR = Path(__file__).parent / "static" / "tts"
MAX_TTS_FILES = 20  # keep last N generated files

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [tts-service] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tts-service")

# ---------------------------------------------------------------------------
# App + global state
# ---------------------------------------------------------------------------

app = FastAPI(title="KukuiBot TTS Service")
_pipeline = None
_model_load_time = 0.0
_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Startup — load Kokoro model once
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def load_model():
    global _pipeline, _model_load_time
    logger.info("Loading Kokoro-82M model...")
    t0 = time.time()
    import kokoro
    _pipeline = kokoro.KPipeline(lang_code="b")
    _model_load_time = time.time() - t0
    logger.info(f"Kokoro model loaded in {_model_load_time:.2f}s")
    # Ensure static TTS output directory exists
    STATIC_TTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_numpy(audio) -> np.ndarray:
    """Convert audio (torch.Tensor or np.ndarray) to numpy float32."""
    if hasattr(audio, "cpu"):
        return audio.cpu().numpy().astype(np.float32)
    return np.asarray(audio, dtype=np.float32)


def _generate_audio(text: str, voice: str, speed: float) -> list:
    """Run Kokoro pipeline, return list of (graphemes, phonemes, audio_np) tuples."""
    if not _pipeline:
        raise RuntimeError("Model not loaded")
    chunks = []
    for result in _pipeline(text, voice=voice, speed=speed):
        chunks.append((result.graphemes, result.phonemes, _to_numpy(result.audio)))
    return chunks


def _audio_to_wav_bytes(audio_np: np.ndarray) -> bytes:
    """Convert float32 numpy audio at 24kHz to WAV bytes."""
    # Clip and convert to int16
    audio_int16 = np.clip(audio_np, -1.0, 1.0)
    audio_int16 = (audio_int16 * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    return buf.getvalue()


def _cleanup_old_files():
    """Remove oldest TTS files beyond MAX_TTS_FILES."""
    if not STATIC_TTS_DIR.exists():
        return
    files = sorted(STATIC_TTS_DIR.glob("*.wav"), key=lambda f: f.stat().st_mtime)
    while len(files) > MAX_TTS_FILES:
        oldest = files.pop(0)
        try:
            oldest.unlink()
            logger.debug(f"Cleaned up old TTS file: {oldest.name}")
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/tts/health")
async def health():
    return {
        "ok": _pipeline is not None,
        "model_load_time": round(_model_load_time, 3),
        "voice_default": TTS_VOICE,
        "sample_rate": SAMPLE_RATE,
        "tts_dir": str(STATIC_TTS_DIR),
    }


@app.post("/tts/speak")
async def speak(request: Request):
    """Stream WAV audio as it generates. Returns chunked audio/wav."""
    body = await request.json()
    text = body.get("text", "").strip()
    voice = body.get("voice", TTS_VOICE)
    speed = float(body.get("speed", TTS_SPEED))

    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)
    if not _pipeline:
        return JSONResponse({"error": "Model not loaded"}, status_code=503)

    logger.info(f"TTS stream: {len(text)} chars, voice={voice}, speed={speed}")

    async def _stream():
        loop = asyncio.get_event_loop()
        t0 = time.time()
        first_chunk = True

        # Generate in thread pool (Kokoro uses torch — not async safe)
        chunks = await loop.run_in_executor(_executor, _generate_audio, text, voice, speed)

        for graphemes, phonemes, audio_np in chunks:
            audio_int16 = np.clip(audio_np, -1.0, 1.0)
            audio_int16 = (audio_int16 * 32767).astype(np.int16)
            pcm_bytes = audio_int16.tobytes()

            if first_chunk:
                elapsed = time.time() - t0
                logger.info(f"First chunk ready in {elapsed:.3f}s")
                # Write WAV header for streaming (unknown length → 0xFFFFFFFF)
                header = struct.pack(
                    "<4sI4s4sIHHIIHH4sI",
                    b"RIFF", 0xFFFFFFFF - 8,  # file size placeholder
                    b"WAVE",
                    b"fmt ", 16,  # PCM format chunk
                    1,  # PCM format
                    1,  # mono
                    SAMPLE_RATE,
                    SAMPLE_RATE * 2,  # byte rate
                    2,  # block align
                    16,  # bits per sample
                    b"data", 0xFFFFFFFF - 44,  # data size placeholder
                )
                yield header
                first_chunk = False

            yield pcm_bytes

        elapsed = time.time() - t0
        logger.info(f"TTS stream complete in {elapsed:.3f}s")

    return StreamingResponse(_stream(), media_type="audio/wav")


@app.post("/tts/speak/file")
async def speak_to_file(request: Request):
    """Generate complete WAV, save to static/tts/, return URL path."""
    body = await request.json()
    text = body.get("text", "").strip()
    voice = body.get("voice", TTS_VOICE)
    speed = float(body.get("speed", TTS_SPEED))

    if not text:
        return JSONResponse({"error": "No text provided"}, status_code=400)
    if not _pipeline:
        return JSONResponse({"error": "Model not loaded"}, status_code=503)

    logger.info(f"TTS file: {len(text)} chars, voice={voice}, speed={speed}")
    t0 = time.time()

    loop = asyncio.get_event_loop()
    chunks = await loop.run_in_executor(_executor, _generate_audio, text, voice, speed)

    # Concatenate all audio chunks
    all_audio = np.concatenate([c[2] for c in chunks])
    wav_bytes = _audio_to_wav_bytes(all_audio)
    duration = len(all_audio) / SAMPLE_RATE

    # Write to file
    filename = f"tts-{uuid.uuid4().hex[:12]}.wav"
    filepath = STATIC_TTS_DIR / filename
    filepath.write_bytes(wav_bytes)

    elapsed = time.time() - t0
    logger.info(f"TTS file saved: {filename} ({duration:.2f}s audio, {elapsed:.3f}s gen)")

    # Cleanup old files
    _cleanup_old_files()

    return {
        "ok": True,
        "filename": filename,
        "path": str(filepath),
        "url": f"/tts/{filename}",
        "duration": round(duration, 3),
        "generation_time": round(elapsed, 3),
        "size_bytes": len(wav_bytes),
    }


@app.post("/tts/play/sonos")
async def play_sonos(request: Request):
    """Play an audio URL on discovered Sonos speakers."""
    body = await request.json()
    audio_url = body.get("url", "").strip()

    if not audio_url:
        return JSONResponse({"error": "No URL provided"}, status_code=400)

    loop = asyncio.get_event_loop()

    def _do_sonos():
        try:
            import soco
            speakers = soco.discover(timeout=3)
            if not speakers:
                return {"ok": False, "error": "No Sonos speakers found"}

            # Pick first available speaker
            speaker = list(speakers)[0]
            speaker.play_uri(audio_url)
            return {
                "ok": True,
                "speaker": speaker.player_name,
                "url": audio_url,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    result = await loop.run_in_executor(None, _do_sonos)
    return result


@app.post("/tts/play/local")
async def play_local(request: Request):
    """Play a local WAV file via macOS afplay."""
    body = await request.json()
    filepath = body.get("path", "").strip()

    if not filepath or not os.path.isfile(filepath):
        return JSONResponse({"error": f"File not found: {filepath}"}, status_code=400)

    loop = asyncio.get_event_loop()

    def _do_afplay():
        try:
            import subprocess
            result = subprocess.run(
                ["afplay", filepath],
                timeout=60, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return {"ok": result.returncode == 0, "path": filepath}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "Playback timed out"}
        except FileNotFoundError:
            return {"ok": False, "error": "afplay not available"}

    result = await loop.run_in_executor(None, _do_afplay)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(f"Starting TTS service on {TTS_HOST}:{TTS_PORT}")
    uvicorn.run(
        app,
        host=TTS_HOST,
        port=TTS_PORT,
        log_level="info",
    )
