#!/usr/bin/env python3
"""
KukuiBot Wake Word Listener — dual-mode "Hey Jarvis" detection.

Modes (configured via KukuiBot Settings → listener_mode):
  local:  POST /api/listener/wake → SSE → browser beeps + Web Speech API STT
  remote: Sonos chime → record speech → Whisper STT → Jarvis chat → Sonos TTS

The mode is fetched from /api/config and cached (refreshed every 30s).

Requires: openwakeword, numpy, pyaudio, onnxruntime
"""
import argparse, io, json, logging, os, signal, ssl, subprocess, sys, threading, time, wave
from pathlib import Path
from urllib import request as urlrequest
import numpy as np
from openwakeword.model import Model

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280          # 80ms — openWakeWord expects this
COOLDOWN_SECS = 5.0           # ignore wake scores for this long after detection
SILENCE_THRESHOLD = 150       # RMS amplitude threshold for silence (int16 scale)
SILENCE_DURATION = 1.2        # seconds of sustained silence to stop recording
MAX_RECORD_SECS = 15          # max recording length after wake word
MIN_RECORD_SECS = 1.0         # minimum recording time before silence detection kicks in

JARVIS_BACKEND_URL = os.getenv("JARVIS_BACKEND_URL", "http://127.0.0.1:5080")

logger = logging.getLogger("wake-listener")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# SSL context that skips cert verification (self-signed *.wilmot.org cert)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def build_model(model_path: str):
    mp = Path(model_path)
    if not mp.exists():
        raise FileNotFoundError(f"Model not found: {model_path}")
    framework = "onnx" if mp.suffix.lower() == ".onnx" else "tflite"
    model_dir = mp.parent
    m = Model(
        wakeword_models=[str(mp)],
        inference_framework=framework,
        melspec_model_path=str(model_dir / f"melspectrogram.{framework}"),
        embedding_model_path=str(model_dir / f"embedding_model.{framework}"),
    )
    model_name = mp.stem
    if model_name not in m.models:
        model_name = list(m.models.keys())[0]
    return m, model_name


def open_mic(device=None):
    import pyaudio
    pa = pyaudio.PyAudio()
    device_index = None
    if device is not None:
        try:
            device_index = int(device)
        except ValueError:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if str(device).lower() in info.get("name", "").lower() and int(info.get("maxInputChannels", 0)) > 0:
                    device_index = i
                    break
            if device_index is None:
                raise RuntimeError(f"No input device matched '{device}'")
    stream = pa.open(
        format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
        input=True, input_device_index=device_index,
        frames_per_buffer=CHUNK_SAMPLES,
    )
    return pa, stream


def read_chunk(stream) -> np.ndarray:
    return np.frombuffer(stream.read(CHUNK_SAMPLES, exception_on_overflow=False), dtype=np.int16)


def post_wake_event(base_url: str, score: float, username: str, room: str):
    """POST wake event to KukuiBot /api/listener/wake."""
    url = f"{base_url}/api/listener/wake"
    payload = json.dumps({
        "score": round(score, 4), "source": "wake-listener",
        "username": username, "room": room,
    }).encode()
    req = urlrequest.Request(url, data=payload,
                             headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
            logger.info(f"POST /api/listener/wake → {resp.status}  {resp.read().decode()}")
    except Exception as e:
        logger.error(f"POST /api/listener/wake failed: {e}")


def play_chime():
    """Play a short listening chime via macOS afplay."""
    try:
        subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"],
                        timeout=2, check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Audio helpers (remote mode)
# ---------------------------------------------------------------------------

def rms(pcm: np.ndarray) -> float:
    """Root-mean-square amplitude of int16 PCM."""
    return float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2)))


def pcm_to_wav_bytes(frames: list) -> bytes:
    """Convert list of int16 PCM chunks to a WAV byte buffer."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        for f in frames:
            wf.writeframes(f.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Backend communication (remote mode)
# ---------------------------------------------------------------------------

def transcribe_audio(wav_bytes: bytes) -> str:
    """POST WAV audio to Jarvis backend Whisper STT, return transcript."""
    import http.client
    import urllib.parse

    url = f"{JARVIS_BACKEND_URL}/api/transcribe"
    parsed = urllib.parse.urlparse(url)

    boundary = f"----Jarvis{int(time.time()*1000)}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="voice.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

    conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=30)
    try:
        conn.request("POST", parsed.path, body=body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        resp = conn.getresponse()
        data = json.loads(resp.read().decode())
        return data.get("text", "").strip()
    except Exception as e:
        logger.error(f"Transcribe failed: {e}")
        return ""
    finally:
        conn.close()


def send_to_jarvis(text: str, room: str) -> str:
    """POST transcript to /jarvis endpoint. Returns assistant response text."""
    url = f"{JARVIS_BACKEND_URL}/jarvis"
    payload = json.dumps({
        "messages": [{"role": "user", "content": text}],
        "room": room,
        "source": "voice",
    }).encode()

    req = urlrequest.Request(url, data=payload,
                             headers={"Content-Type": "application/json"},
                             method="POST")
    try:
        with urlrequest.urlopen(req, timeout=60) as resp:
            # Response is SSE stream — collect the final text
            response_text = ""
            for line in resp:
                line = line.decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    try:
                        evt = json.loads(line[6:])
                        if evt.get("type") == "done":
                            response_text = evt.get("text", "")
                    except (json.JSONDecodeError, ValueError):
                        pass
            return response_text
    except Exception as e:
        logger.error(f"Jarvis chat failed: {e}")
        return ""


def fire_chime(room: str):
    """Fire-and-forget: play wake chime on Sonos via Jarvis backend."""
    def _do():
        try:
            url = f"{JARVIS_BACKEND_URL}/jarvis/chime?room={room}"
            urlrequest.urlopen(url, timeout=3)
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Config polling
# ---------------------------------------------------------------------------

def fetch_listener_mode(kukuibot_url: str) -> str:
    """Fetch listener_mode from KukuiBot /api/config. Returns 'local' or 'remote'."""
    try:
        url = f"{kukuibot_url}/api/config"
        req = urlrequest.Request(url, method="GET")
        with urlrequest.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
            data = json.loads(resp.read().decode())
            return data.get("listener_mode", "local")
    except Exception as e:
        logger.warning(f"Config fetch failed: {e}")
        return "local"


def main():
    parser = argparse.ArgumentParser(description="KukuiBot wake word listener")
    parser.add_argument("--threshold", type=float, default=0.5, help="Wake word detection threshold")
    parser.add_argument("--device", default=None, help="Audio input device index or name")
    parser.add_argument("--room", default="Office", help="Room name sent in wake event")
    parser.add_argument("--username", default="", help="Username sent in wake event")
    parser.add_argument("--model", default="/Users/jarvis/.jarvis/data/wakeword-models/hey_jarvis_v0.1.onnx",
                        help="Path to wake word model")
    parser.add_argument("--kukuibot-url", default=None,
                        help="KukuiBot base URL (default: KUKUIBOT_URL env or https://localhost:7000)")
    args = parser.parse_args()

    kukuibot_url = (args.kukuibot_url or os.getenv("KUKUIBOT_URL", "https://localhost:7000")).rstrip("/")

    model, label = build_model(args.model)
    logger.info(f"Wake word model loaded: {label} (threshold={args.threshold})")

    pa, stream = open_mic(args.device)
    logger.info(f"Microphone open (rate={SAMPLE_RATE}, chunk={CHUNK_SAMPLES})")

    stop = False
    def _stop(sig, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    last_wake_time = 0.0
    cached_mode = "local"
    mode_fetched_at = 0.0
    MODE_CACHE_SECS = 30.0

    # Fetch initial mode
    cached_mode = fetch_listener_mode(kukuibot_url)
    mode_fetched_at = time.time()
    logger.info(f"Listening for 'Hey Jarvis' — room={args.room}, mode={cached_mode}, url={kukuibot_url}")

    while not stop:
        try:
            pcm = read_chunk(stream)
        except Exception as e:
            logger.error(f"Audio read error: {e}")
            time.sleep(0.5)
            continue

        pred = model.predict(pcm)
        score = float(pred.get(label, 0.0))

        now = time.time()
        if now - last_wake_time < COOLDOWN_SECS:
            continue
        if score < args.threshold:
            continue

        # --- Wake word detected! ---
        last_wake_time = time.time()
        logger.info(f"WAKE DETECTED (score={score:.3f})")

        # Refresh mode cache if stale
        if now - mode_fetched_at > MODE_CACHE_SECS:
            cached_mode = fetch_listener_mode(kukuibot_url)
            mode_fetched_at = time.time()
            logger.info(f"Mode refreshed: {cached_mode}")

        if cached_mode == "remote":
            # --- Remote mode: record → transcribe → Jarvis chat → Sonos TTS ---
            fire_chime(args.room)

            # Record speech until silence or max duration
            frames = []
            silence_chunks = 0
            silence_limit = int(SILENCE_DURATION * SAMPLE_RATE / CHUNK_SAMPLES)
            max_chunks = int(MAX_RECORD_SECS * SAMPLE_RATE / CHUNK_SAMPLES)
            min_chunks = int(MIN_RECORD_SECS * SAMPLE_RATE / CHUNK_SAMPLES)
            recorded = 0

            logger.info("Recording speech...")
            while not stop and recorded < max_chunks:
                try:
                    pcm = read_chunk(stream)
                except Exception:
                    break
                frames.append(pcm)
                recorded += 1

                amp = rms(pcm)
                if amp < SILENCE_THRESHOLD:
                    silence_chunks += 1
                else:
                    silence_chunks = 0

                if silence_chunks >= silence_limit and recorded >= min_chunks:
                    logger.info(f"Silence detected after {recorded} chunks (rms={amp:.0f})")
                    break

            duration = recorded * CHUNK_SAMPLES / SAMPLE_RATE
            logger.info(f"Recorded {duration:.1f}s of speech ({recorded} chunks)")

            if recorded < min_chunks:
                logger.info("Too short — ignoring")
                last_wake_time = time.time()
                continue

            # Convert to WAV
            wav_bytes = pcm_to_wav_bytes(frames)
            logger.info(f"WAV: {len(wav_bytes)} bytes")

            # Transcribe via Jarvis backend Whisper
            logger.info("Transcribing...")
            transcript = transcribe_audio(wav_bytes)

            if not transcript:
                logger.info("No speech detected — resuming listening")
                last_wake_time = time.time()
                continue

            logger.info(f"Transcript: '{transcript}'")

            # Send to Jarvis chat (TTS response auto-plays on Sonos)
            logger.info(f"Sending to Jarvis (room={args.room})...")
            response = send_to_jarvis(transcript, args.room)
            if response:
                logger.info(f"Jarvis: {response[:100]}")
            else:
                logger.info("No response from Jarvis")

            # Extend cooldown after full interaction (TTS may be playing)
            last_wake_time = time.time()
        else:
            # --- Local mode: POST wake event → SSE → browser STT ---
            post_wake_event(kukuibot_url, score, args.username, args.room)

    logger.info("Shutting down...")
    stream.stop_stream()
    stream.close()
    pa.terminate()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
