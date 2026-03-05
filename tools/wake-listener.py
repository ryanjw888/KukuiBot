#!/usr/bin/env python3
"""
KukuiBot Wake Word Listener — always-on Vosk STT with Eagle speaker verification.

Detection: Vosk streaming STT watches for "jarvis" in transcripts (no pause needed)
Speaker verification: Picovoice Eagle (optional — requires enrolled voice profile)

Modes (configured via KukuiBot Settings → listener_mode):
  local:  POST /api/listener/wake → SSE → browser beeps + Web Speech API STT
  remote: verify speaker → send Vosk transcript to Jarvis chat → TTS response

The mode is fetched from /api/config and cached (refreshed every 30s).

Requires: vosk, pveagle, numpy, pyaudio
"""
import argparse, io, json, logging, os, signal, ssl, subprocess, sys, threading, time, wave
from collections import deque
from pathlib import Path
from urllib import request as urlrequest
import numpy as np

SAMPLE_RATE = 16000
VOSK_CHUNK_SAMPLES = 4000       # 250ms chunks for Vosk (good balance of latency vs efficiency)

EAGLE_PROFILE_DIR = os.path.expanduser("~/.jarvis/data")
EAGLE_PROFILE_PATH = os.path.join(EAGLE_PROFILE_DIR, "eagle_profile.bin")  # legacy single-profile path
EAGLE_SCORE_THRESHOLD = 0.7     # minimum speaker similarity score (0.0-1.0)

VOSK_MODEL_PATH = os.path.expanduser("~/jarvis-voice/models/vosk-model-small-en-us-0.15")

# Defaults — overridden at startup by /api/config values
COOLDOWN_SECS = 2.5           # ignore triggers for this long after a command
SILENCE_THRESHOLD = 150       # RMS amplitude threshold for silence (int16 scale)
SILENCE_DURATION = 1.5        # seconds of sustained silence to stop recording
MAX_RECORD_SECS = 15          # max recording length after wake word
MIN_RECORD_SECS = 2.0         # minimum recording time before silence detection kicks in
PRE_BUFFER_SECS = 3.0         # seconds of audio to keep before wake detection

# Legacy env var — prefer kukuibot_url routing, fall back to direct Jarvis connection
_JARVIS_BACKEND_URL_OVERRIDE = os.getenv("JARVIS_BACKEND_URL", "")

logger = logging.getLogger("wake-listener")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

# Local ASR state (auto-detected at startup)
_local_asr_session = None
_local_asr_available = None  # None=unchecked, True/False after init

# SSL context that skips cert verification (self-signed *.wilmot.org cert)
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Wake engine: Vosk streaming STT
# ---------------------------------------------------------------------------

def build_vosk_recognizer(model_path: str):
    """Create a Vosk recognizer for streaming keyword detection."""
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)  # suppress Vosk internal logs
    model = Model(model_path)
    rec = KaldiRecognizer(model, SAMPLE_RATE)
    rec.SetWords(True)
    return rec


# ---------------------------------------------------------------------------
# Wake engine: Porcupine (fallback)
# ---------------------------------------------------------------------------

def build_porcupine(access_key: str, keywords: list = None, sensitivity: float = 0.8):
    """Create a Porcupine wake word engine for the 'jarvis' keyword."""
    import pvporcupine
    keywords = keywords or ["jarvis"]
    porcupine = pvporcupine.create(
        access_key=access_key,
        keywords=keywords,
        sensitivities=[sensitivity] * len(keywords),
    )
    return porcupine


# ---------------------------------------------------------------------------
# Speaker verification: Eagle
# ---------------------------------------------------------------------------

def load_eagle_recognizer(access_key: str, profile_path: str = EAGLE_PROFILE_PATH):
    """Load Eagle speaker recognizer from all enrolled profiles in EAGLE_PROFILE_DIR.

    Returns (recognizer, speaker_names) or (None, []) if no profiles found.
    Profiles are named eagle_profile_{name}.bin — the name is extracted from the filename.
    Falls back to legacy single eagle_profile.bin (mapped to 'unknown').
    """
    import glob
    import pveagle

    profile_dir = os.path.dirname(profile_path)
    pattern = os.path.join(profile_dir, "eagle_profile_*.bin")
    profile_files = sorted(glob.glob(pattern))

    # Fall back to legacy single profile if no named profiles exist
    if not profile_files and os.path.isfile(profile_path):
        profile_files = [profile_path]

    if not profile_files:
        logger.warning(f"No Eagle voice profiles in {profile_dir} -- speaker verification disabled")
        return None, []

    profiles = []
    speaker_names = []
    for pf in profile_files:
        basename = os.path.basename(pf)
        # Extract name: eagle_profile_ryan.bin → "ryan"
        if basename.startswith("eagle_profile_") and basename.endswith(".bin"):
            name = basename[len("eagle_profile_"):-len(".bin")]
        else:
            name = "unknown"
        with open(pf, "rb") as f:
            profile_bytes = f.read()
        profiles.append(pveagle.EagleProfile.from_bytes(profile_bytes))
        speaker_names.append(name)

    recognizer = pveagle.create_recognizer(access_key=access_key, speaker_profiles=profiles)
    logger.info(f"Eagle loaded {len(profiles)} speaker(s): {speaker_names} (frame_length: {recognizer.frame_length})")
    return recognizer, speaker_names


def verify_speaker(eagle_recognizer, speaker_names: list, frames: list,
                   threshold: float = EAGLE_SCORE_THRESHOLD) -> tuple:
    """Run Eagle speaker verification on recorded audio frames.

    Returns (passed: bool, best_score: float, speaker_name: str).
    Eagle returns per-speaker scores for each frame; we average per speaker
    and pick the best match.
    """
    if eagle_recognizer is None:
        return True, 1.0, ""

    eagle_frame_len = eagle_recognizer.frame_length
    all_pcm = np.concatenate(frames)
    num_speakers = len(speaker_names) if speaker_names else 1

    # Collect per-speaker scores across all frames
    all_scores = [[] for _ in range(num_speakers)]

    eagle_recognizer.reset()
    offset = 0
    while offset + eagle_frame_len <= len(all_pcm):
        chunk = all_pcm[offset:offset + eagle_frame_len]
        frame_scores = eagle_recognizer.process(chunk)  # list of floats, one per speaker
        for i, s in enumerate(frame_scores):
            all_scores[i].append(s)
        offset += eagle_frame_len

    if not any(all_scores):
        logger.warning("Eagle: no scores produced (audio too short for verification)")
        return True, 0.0, ""

    # Average score per speaker, find best match
    avg_scores = []
    for i, scores in enumerate(all_scores):
        avg = sum(scores) / len(scores) if scores else 0.0
        avg_scores.append(avg)

    best_idx = max(range(len(avg_scores)), key=lambda i: avg_scores[i])
    best_score = avg_scores[best_idx]
    best_name = speaker_names[best_idx] if best_idx < len(speaker_names) else "unknown"

    passed = best_score >= threshold
    logger.debug(f"Eagle scores: {dict(zip(speaker_names, [f'{s:.3f}' for s in avg_scores]))}")
    return passed, best_score, best_name


# ---------------------------------------------------------------------------
# Eagle enrollment (triggered via config flag from KukuiBot server)
# ---------------------------------------------------------------------------

def _post_enroll_progress(kukuibot_url, name, percentage, status, feedback="", error=""):
    """POST enrollment progress to KukuiBot server for UI display."""
    try:
        data = json.dumps({
            "name": name, "percentage": percentage,
            "status": status, "feedback": feedback, "error": error,
        }).encode()
        req = urlrequest.Request(
            f"{kukuibot_url}/api/listener/eagle/enroll/progress",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlrequest.urlopen(req, timeout=5, context=_ssl_ctx)
    except Exception as e:
        logger.warning(f"Failed to POST enrollment progress: {e}")


def run_enrollment(stream, pa, access_key, speaker_name, duration, kukuibot_url):
    """Run Eagle voice enrollment using the local mic.

    Pauses the existing Vosk stream, opens a new stream for enrollment,
    and restarts the Vosk stream when done.

    Returns (success: bool, profile_path: str).
    """
    import pyaudio
    import pveagle

    # Sanitize speaker name
    safe_name = speaker_name.strip().lower().replace(" ", "_")
    if not safe_name or "/" in safe_name or "\\" in safe_name or ".." in safe_name or "\x00" in safe_name:
        logger.error(f"Invalid speaker name: {speaker_name!r}")
        _post_enroll_progress(kukuibot_url, speaker_name, 0, "failed", error="Invalid speaker name")
        return False, ""

    profile_path = os.path.join(EAGLE_PROFILE_DIR, f"eagle_profile_{safe_name}.bin")
    os.makedirs(EAGLE_PROFILE_DIR, exist_ok=True)

    # Pause the Vosk input stream to release the mic
    try:
        stream.stop_stream()
        logger.info("Vosk stream paused for enrollment")
    except Exception as e:
        logger.warning(f"Failed to pause Vosk stream: {e}")

    enroll_stream = None
    profiler = None
    success = False

    try:
        profiler = pveagle.create_profiler(access_key=access_key)
        sample_rate = profiler.sample_rate
        frame_length = profiler.min_enroll_samples

        enroll_stream = pa.open(
            format=pyaudio.paInt16, channels=1, rate=sample_rate,
            input=True, frames_per_buffer=frame_length,
        )

        _post_enroll_progress(kukuibot_url, speaker_name, 0, "enrolling")
        logger.info(f"Enrollment started for '{speaker_name}' (duration={duration}s, rate={sample_rate}, frame={frame_length})")

        total_frames = int(duration * sample_rate / frame_length)
        enroll_percentage = 0.0
        last_progress_pct = -5  # force first report
        last_progress_time = time.time()

        for i in range(total_frames):
            pcm = np.frombuffer(
                enroll_stream.read(frame_length, exception_on_overflow=False),
                dtype=np.int16,
            )
            enroll_percentage, feedback = profiler.enroll(pcm)

            # Report progress every 5% or every 5 seconds
            now = time.time()
            if enroll_percentage - last_progress_pct >= 5 or now - last_progress_time >= 5:
                feedback_str = feedback.name if feedback.name != "AUDIO_OK" else ""
                _post_enroll_progress(
                    kukuibot_url, speaker_name, enroll_percentage,
                    "enrolling", feedback=feedback_str,
                )
                last_progress_pct = enroll_percentage
                last_progress_time = now
                logger.info(f"Enrollment: {enroll_percentage:.0f}% ({(i+1)*frame_length/sample_rate:.1f}s)")

            if enroll_percentage >= 100.0:
                break

        if enroll_percentage < 100.0:
            msg = f"Enrollment only {enroll_percentage:.0f}% complete (need 100%). Try speaking louder/closer."
            logger.warning(msg)
            _post_enroll_progress(kukuibot_url, speaker_name, enroll_percentage, "failed", error=msg)
            profiler.delete()
            profiler = None
            return False, ""

        # Export and save profile
        profile = profiler.export()
        profiler.delete()
        profiler = None

        profile_bytes = profile.to_bytes()
        with open(profile_path, "wb") as f:
            f.write(profile_bytes)

        logger.info(f"Voice profile saved: {profile_path} ({len(profile_bytes)} bytes)")
        _post_enroll_progress(kukuibot_url, speaker_name, 100, "complete")
        success = True

    except Exception as e:
        logger.error(f"Enrollment failed: {e}")
        _post_enroll_progress(kukuibot_url, speaker_name, 0, "failed", error=str(e))
        if profiler:
            try:
                profiler.delete()
            except Exception:
                pass
    finally:
        # Close the enrollment stream
        if enroll_stream:
            try:
                enroll_stream.stop_stream()
                enroll_stream.close()
            except Exception:
                pass

        # Resume the Vosk stream
        try:
            stream.start_stream()
            logger.info("Vosk stream resumed after enrollment")
        except Exception as e:
            logger.warning(f"Failed to resume Vosk stream: {e}, reopening mic...")
            try:
                new_pa, new_stream = open_mic()
                # Caller will need the new stream — but since we modify in-place,
                # we rely on the existing stream object. Log the issue.
                logger.warning("Mic reopened — stream object may need refresh on next loop")
            except Exception as e2:
                logger.error(f"Failed to reopen mic: {e2}")

    return success, profile_path if success else ""


# ---------------------------------------------------------------------------
# Mic / audio
# ---------------------------------------------------------------------------

def open_mic(device=None, chunk_samples=VOSK_CHUNK_SAMPLES):
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
        frames_per_buffer=chunk_samples,
    )
    return pa, stream


def read_chunk(stream, chunk_samples=VOSK_CHUNK_SAMPLES) -> np.ndarray:
    return np.frombuffer(stream.read(chunk_samples, exception_on_overflow=False), dtype=np.int16)


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
            logger.info(f"POST /api/listener/wake -> {resp.status}  {resp.read().decode()}")
    except Exception as e:
        logger.error(f"POST /api/listener/wake failed: {e}")


def post_transcript_event(base_url: str, text: str, room: str, is_final: bool = True):
    """POST transcript to KukuiBot for browser injection via SSE."""
    url = f"{base_url}/api/listener/transcript"
    payload = json.dumps({
        "text": text, "room": room, "is_final": is_final,
        "source": "wake-listener",
    }).encode()
    req = urlrequest.Request(url, data=payload,
                             headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
            logger.info(f"POST /api/listener/transcript -> {resp.status}")
    except Exception as e:
        logger.error(f"POST /api/listener/transcript failed: {e}")


def play_chime():
    """Play a short listening chime via macOS afplay."""
    try:
        subprocess.run(["afplay", "/System/Library/Sounds/Tink.aiff"],
                        timeout=2, check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Local ASR (Qwen3-ASR via mlx_qwen3_asr — auto-detected)
# ---------------------------------------------------------------------------

def _init_local_asr() -> bool:
    """Try to initialize local Qwen3-ASR. Returns True if available."""
    global _local_asr_session, _local_asr_available
    if _local_asr_available is not None:
        return _local_asr_available
    try:
        from mlx_qwen3_asr import Session
        import tempfile, struct
        logger.info("[asr] Initializing local Qwen3-ASR...")
        _local_asr_session = Session(model="Qwen/Qwen3-ASR-0.6B")
        # Warm up with 1s silence
        dummy = tempfile.mktemp(suffix=".wav")
        try:
            with wave.open(dummy, "w") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(16000)
                w.writeframes(struct.pack("<" + "h" * 16000, *([0] * 16000)))
            _local_asr_session.transcribe(dummy, language="English")
        finally:
            try: os.unlink(dummy)
            except OSError: pass
        _local_asr_available = True
        logger.info("[asr] Local Qwen3-ASR ready")
        return True
    except ImportError:
        logger.info("[asr] mlx_qwen3_asr not installed — using remote transcription")
        _local_asr_available = False
        return False
    except Exception as e:
        logger.warning(f"[asr] Local ASR init failed: {e} — using remote transcription")
        _local_asr_available = False
        return False


def _transcribe_local(wav_bytes: bytes) -> str:
    """Transcribe WAV audio bytes using local Qwen3-ASR. Returns transcript text."""
    global _local_asr_session
    if _local_asr_session is None:
        return ""
    import tempfile
    t0 = time.time()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name
    try:
        result = _local_asr_session.transcribe(tmp_path, language="English")
        text = result.text.strip() if hasattr(result, 'text') else str(result).strip()
        elapsed = time.time() - t0
        logger.info(f"[asr] Local transcription in {elapsed:.2f}s: {text[:60]}")
        return text
    except Exception as e:
        logger.error(f"[asr] Local transcription failed: {e}")
        return ""
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


# ---------------------------------------------------------------------------
# Audio helpers
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

def _probe_jarvis_url(url: str) -> bool:
    """Probe a Jarvis backend URL to see if it's reachable. Returns True if responsive."""
    import http.client
    import urllib.parse
    try:
        parsed = urllib.parse.urlparse(url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=2)
        conn.request("GET", "/")
        resp = conn.getresponse()
        conn.close()
        return resp.status < 500
    except Exception:
        return False


def transcribe_audio(wav_bytes: bytes, kukuibot_url: str = "https://localhost:7000",
                     jarvis_direct_url: str = "") -> str:
    """POST WAV audio for transcription. Uses local ASR if available, else remote."""
    import http.client
    import urllib.parse

    # Priority 0: Local ASR (if available on this machine)
    if _local_asr_available:
        local_result = _transcribe_local(wav_bytes)
        if local_result:
            return local_result
        logger.warning("[asr] Local transcription returned empty, falling back to remote")

    if _JARVIS_BACKEND_URL_OVERRIDE:
        url = f"{_JARVIS_BACKEND_URL_OVERRIDE}/api/transcribe"
    elif jarvis_direct_url:
        url = f"{jarvis_direct_url}/api/transcribe"
    else:
        url = f"{kukuibot_url}/api/listener/transcribe"
    parsed = urllib.parse.urlparse(url)

    boundary = f"----Jarvis{int(time.time()*1000)}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="audio"; filename="voice.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode() + wav_bytes + f"\r\n--{boundary}--\r\n".encode()

    # Use HTTPS with cert verification disabled (self-signed)
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=30, context=_ssl_ctx)
    else:
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


def send_to_jarvis(text: str, room: str, kukuibot_url: str = "https://localhost:7000",
                   jarvis_direct_url: str = "") -> str:
    """POST transcript to Jarvis. Uses direct backend if available, else KukuiBot proxy."""
    import http.client
    import urllib.parse

    if _JARVIS_BACKEND_URL_OVERRIDE:
        url = f"{_JARVIS_BACKEND_URL_OVERRIDE}/jarvis"
    elif jarvis_direct_url:
        url = f"{jarvis_direct_url}/jarvis"
    else:
        url = f"{kukuibot_url}/api/listener/chat"

    payload = json.dumps({
        "messages": [{"role": "user", "content": text}],
        "room": room,
        "source": "voice",
    }).encode()

    parsed = urllib.parse.urlparse(url)
    use_direct_http = parsed.scheme == "http"

    if use_direct_http:
        # Direct HTTP to Jarvis backend — use http.client for reliable SSE streaming
        try:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=60)
            conn.request("POST", parsed.path, body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            response_text = ""
            while True:
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", errors="replace").strip()
                if line.startswith("data: "):
                    try:
                        evt = json.loads(line[6:])
                        if evt.get("type") == "done":
                            response_text = evt.get("text", "")
                    except (json.JSONDecodeError, ValueError):
                        pass
            conn.close()
            return response_text
        except Exception as e:
            logger.error(f"Jarvis chat (direct) failed: {e}")
            return ""
    else:
        # HTTPS proxy via urllib (self-signed cert)
        req = urlrequest.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
        try:
            with urlrequest.urlopen(req, timeout=60, context=_ssl_ctx) as resp:
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
            logger.error(f"Jarvis chat (proxy) failed: {e}")
            return ""


def fire_chime(kukuibot_url: str):
    """Fire-and-forget: play wake chime via KukuiBot server, local afplay fallback."""
    def _do():
        try:
            url = f"{kukuibot_url}/api/listener/chime"
            req = urlrequest.Request(url, data=b'{}',
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
            with urlrequest.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
                logger.debug(f"Chime response: {resp.read().decode()}")
                return
        except Exception as e:
            logger.debug(f"Remote chime failed ({e}), falling back to local afplay")
        # Fallback: play locally via afplay
        play_chime()
    threading.Thread(target=_do, daemon=True).start()


# ---------------------------------------------------------------------------
# Config polling
# ---------------------------------------------------------------------------

def _parse_triggers(triggers_str: str) -> dict:
    """Parse comma-separated trigger words/phrases into a trigger dict.

    'jarvis' is handled specially as the wake word.
    Single words map to control types: lights/light -> light_control, shades/shade/blinds -> shade_control.
    Multi-word phrases (e.g., "what's the weather") map to 'jarvis_query' — sent to Jarvis as-is.
    """
    triggers = {}
    _type_map = {
        "lights": "light_control", "light": "light_control",
        "shades": "shade_control", "shade": "shade_control", "blinds": "shade_control",
    }
    for phrase in triggers_str.lower().split(","):
        phrase = phrase.strip()
        if not phrase or phrase == "jarvis":
            continue
        if " " in phrase:
            # Multi-word phrase — treat as a jarvis query trigger
            triggers[phrase] = "jarvis_query"
        else:
            triggers[phrase] = _type_map.get(phrase, "direct_action")
    return triggers


def fetch_listener_config(kukuibot_url: str) -> dict:
    """Fetch listener config from KukuiBot /api/listener/config (auth-exempt)."""
    try:
        url = f"{kukuibot_url}/api/listener/config"
        req = urlrequest.Request(url, method="GET")
        with urlrequest.urlopen(req, timeout=5, context=_ssl_ctx) as resp:
            data = json.loads(resp.read().decode())
            triggers_str = data.get("listener_wake_triggers", "jarvis,lights,light,shades,shade,blinds")
            eagle_enabled_raw = data.get("listener_eagle_enabled", True)
            eagle_enabled = eagle_enabled_raw not in (False, "0", "false", 0)
            return {
                "mode": data.get("listener_mode", "local"),
                "device": data.get("listener_device", ""),
                "cooldown": _safe_float(data.get("listener_cooldown"), COOLDOWN_SECS),
                "silence_threshold": _safe_float(data.get("listener_silence_threshold"), SILENCE_THRESHOLD),
                "silence_duration": _safe_float(data.get("listener_silence_duration"), SILENCE_DURATION),
                "max_record": _safe_float(data.get("listener_max_record"), MAX_RECORD_SECS),
                "min_record": _safe_float(data.get("listener_min_record"), MIN_RECORD_SECS),
                "eagle_enabled": eagle_enabled,
                "eagle_threshold": _safe_float(data.get("listener_eagle_threshold"), EAGLE_SCORE_THRESHOLD),
                "triggers": _parse_triggers(triggers_str),
                "triggers_str": triggers_str,
                "jarvis_url": data.get("listener_jarvis_url", ""),
                "room": data.get("listener_room", ""),
                "username": data.get("listener_username", ""),
                "enroll_speaker": data.get("listener_enroll_speaker", ""),
                "enroll_duration": _safe_float(data.get("listener_enroll_duration"), 40),
            }
    except Exception as e:
        logger.warning(f"Config fetch failed: {e}")
        return {"mode": "local", "device": ""}


def _safe_float(val, default):
    """Parse a float from config, returning default on failure."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(default)


def start_config_refresh_thread(kukuibot_url, config_state, interval=30.0):
    """Background thread that refreshes listener config every `interval` seconds."""
    def _refresh():
        while True:
            time.sleep(interval)
            try:
                cfg = fetch_listener_config(kukuibot_url)
                # Re-probe direct Jarvis URL if config changed (skip if env override active)
                if not _JARVIS_BACKEND_URL_OVERRIDE:
                    cfg_jarvis = (cfg.get("jarvis_url") or "").rstrip("/")
                    if cfg_jarvis and _probe_jarvis_url(cfg_jarvis):
                        if cfg_jarvis != config_state.get("jarvis_direct_url"):
                            logger.info(f"Direct Jarvis backend now reachable: {cfg_jarvis}")
                        cfg["jarvis_direct_url"] = cfg_jarvis
                    else:
                        if config_state.get("jarvis_direct_url"):
                            logger.info("Direct Jarvis backend unreachable, falling back to proxy")
                        cfg["jarvis_direct_url"] = ""
                config_state.update(cfg)
            except Exception:
                pass
    t = threading.Thread(target=_refresh, daemon=True)
    t.start()
    return t


def extract_command(text: str) -> str:
    """Extract the command portion after 'jarvis' from a transcript.

    Examples:
        'jarvis turn on the lights' -> 'turn on the lights'
        'hey jarvis what time is it' -> 'what time is it'
        'jarvis' -> ''
    """
    lower = text.lower()
    # Find the last occurrence of 'jarvis' and take everything after it
    idx = lower.rfind("jarvis")
    if idx < 0:
        return text.strip()
    after = text[idx + len("jarvis"):].strip()
    # Strip leading punctuation/comma
    after = after.lstrip(",.!? ")
    return after


# Default direct-action triggers (overridden by server config)
DIRECT_TRIGGERS = {
    "lights": "light_control",
    "light": "light_control",
    "shades": "shade_control",
    "shade": "shade_control",
    "blinds": "shade_control",
}


def detect_trigger(text: str, triggers: dict = None) -> tuple:
    """Detect wake word or direct-action trigger in transcript.

    Args:
        text: Vosk transcript text
        triggers: Dict of trigger_word/phrase -> control_type (from server config).
                  Falls back to DIRECT_TRIGGERS if None.

    Returns (trigger_type, command):
        ('jarvis', 'turn on the lights')     — full assistant command
        ('jarvis', '')                        — wake word only, needs follow-up
        ('direct', 'lights fifty percent')    — direct action, full utterance is the command
        (None, '')                            — no trigger found
    """
    lower = text.lower()
    active_triggers = triggers if triggers is not None else DIRECT_TRIGGERS

    # Check for "jarvis" first (takes priority)
    if "jarvis" in lower:
        return "jarvis", extract_command(text)

    # Check for multi-word phrase triggers first (longer matches win)
    for phrase, ttype in sorted(active_triggers.items(), key=lambda x: -len(x[0])):
        if " " in phrase and phrase in lower:
            # Multi-word phrase matched — send the full utterance as a jarvis query
            return "jarvis", text.strip()

    # Check for single-word direct-action triggers
    words = lower.split()
    for word in words:
        if word in active_triggers and " " not in word:
            # The entire utterance IS the command (e.g., "lights fifty percent")
            return "direct", text.strip()

    return None, ""


def main():
    parser = argparse.ArgumentParser(description="KukuiBot wake word listener")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="Wake word detection threshold (kept for config compat)")
    parser.add_argument("--device", default=None, help="Audio input device index or name")
    parser.add_argument("--room", default="Office", help="Room name sent in wake event")
    parser.add_argument("--username", default="", help="Username sent in wake event")
    parser.add_argument("--model", default=None, help="[DEPRECATED] ignored")
    parser.add_argument("--access-key",
                        default=os.getenv("PICOVOICE_ACCESS_KEY", "c7cXxfOZp42ls99y7tlBYNfwnIHS9yt9J/nyZAq5xaRz9PSzLm/JtQ=="),
                        help="Picovoice access key for Eagle speaker verification")
    parser.add_argument("--eagle-profile", default=EAGLE_PROFILE_PATH,
                        help="Path to Eagle speaker profile for voice verification")
    parser.add_argument("--eagle-threshold", type=float, default=EAGLE_SCORE_THRESHOLD,
                        help="Eagle speaker verification threshold (0.0-1.0)")
    parser.add_argument("--vosk-model", default=VOSK_MODEL_PATH,
                        help="Path to Vosk model directory")
    parser.add_argument("--kukuibot-url", default=None,
                        help="KukuiBot base URL (default: KUKUIBOT_URL env or https://localhost:7000)")
    # Legacy args kept for plist compatibility
    parser.add_argument("--porcupine-sensitivity", type=float, default=0.8, help=argparse.SUPPRESS)
    args = parser.parse_args()

    kukuibot_url = (args.kukuibot_url or os.getenv("KUKUIBOT_URL", "https://localhost:7000")).rstrip("/")

    # Fetch device from config (CLI --device overrides)
    initial_cfg = fetch_listener_config(kukuibot_url)
    device = args.device
    if not device and initial_cfg.get("device"):
        device = initial_cfg["device"]
        logger.info(f"Using mic from config: device={device}")

    # Override room/username from server config when CLI args are at defaults
    if args.room == "Office" and initial_cfg.get("room"):
        args.room = initial_cfg["room"]
        logger.info(f"Using room from config: {args.room}")
    if not args.username and initial_cfg.get("username"):
        args.username = initial_cfg["username"]
        logger.info(f"Using username from config: {args.username}")

    # Resolve direct Jarvis backend URL (bypass proxy for local connections)
    jarvis_direct_url = ""
    if _JARVIS_BACKEND_URL_OVERRIDE:
        jarvis_direct_url = _JARVIS_BACKEND_URL_OVERRIDE.rstrip("/")
        logger.info(f"Using direct Jarvis backend (env override): {jarvis_direct_url}")
    else:
        cfg_jarvis = (initial_cfg.get("jarvis_url") or "").rstrip("/")
        if cfg_jarvis and _probe_jarvis_url(cfg_jarvis):
            jarvis_direct_url = cfg_jarvis
            logger.info(f"Using direct Jarvis backend: {jarvis_direct_url}")
        else:
            logger.info("Using KukuiBot proxy for Jarvis API")

    # Apply tuning from server config
    cooldown = initial_cfg.get("cooldown", COOLDOWN_SECS)
    silence_thresh = initial_cfg.get("silence_threshold", SILENCE_THRESHOLD)
    silence_dur = initial_cfg.get("silence_duration", SILENCE_DURATION)
    max_record = initial_cfg.get("max_record", MAX_RECORD_SECS)
    min_record = initial_cfg.get("min_record", MIN_RECORD_SECS)

    # Initialize Vosk streaming recognizer
    vosk_rec = build_vosk_recognizer(args.vosk_model)
    logger.info(f"Vosk STT loaded: model={args.vosk_model}")

    # Initialize Eagle speaker verification (optional, multi-speaker)
    eagle_recognizer, eagle_speaker_names = load_eagle_recognizer(args.access_key, args.eagle_profile)
    # Eagle threshold: prefer config, fall back to CLI arg
    eagle_threshold = initial_cfg.get("eagle_threshold", args.eagle_threshold)
    eagle_enabled = initial_cfg.get("eagle_enabled", True) if eagle_recognizer else False

    pa, stream = open_mic(device, VOSK_CHUNK_SAMPLES)
    logger.info(f"Microphone open (rate={SAMPLE_RATE}, chunk={VOSK_CHUNK_SAMPLES}, device={device or 'system default'})")

    # Initialize local ASR if available (auto-detect mlx_qwen3_asr)
    _init_local_asr()

    stop = False
    def _stop(sig, frame):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    last_wake_time = 0.0
    config_state = {
        "mode": initial_cfg.get("mode", "local"),
        "cooldown": cooldown,
        "silence_threshold": silence_thresh,
        "silence_duration": silence_dur,
        "max_record": max_record,
        "min_record": min_record,
        "eagle_enabled": eagle_enabled,
        "eagle_threshold": eagle_threshold,
        "triggers": initial_cfg.get("triggers", DIRECT_TRIGGERS),
        "jarvis_direct_url": jarvis_direct_url,
    }
    start_config_refresh_thread(kukuibot_url, config_state)
    triggers_list = list(config_state.get("triggers", {}).keys())
    eagle_status = f"eagle={'ON' if eagle_recognizer and eagle_enabled else 'OFF'} (threshold={eagle_threshold})"
    logger.info(f"Listening for 'Jarvis' via Vosk STT -- room={args.room}, mode={config_state['mode']}, {eagle_status}, device={device or 'system default'}, url={kukuibot_url}")
    logger.info(f"Triggers: jarvis + {triggers_list}")
    logger.info(f"Tuning: cooldown={cooldown}s, silence={silence_thresh}RMS/{silence_dur}s, record={min_record}-{max_record}s")

    # Audio ring buffer for Eagle verification (keeps last N seconds)
    pre_buffer_chunks = int(PRE_BUFFER_SECS * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
    audio_ring = deque(maxlen=pre_buffer_chunks)

    while not stop:
        # Check for enrollment request from server config
        enroll_name = config_state.get("enroll_speaker", "")
        if enroll_name:
            enroll_duration = config_state.get("enroll_duration", 40)
            logger.info(f"Enrollment requested for '{enroll_name}', pausing listener...")
            # Clear immediately to prevent re-trigger from config refresh
            config_state["enroll_speaker"] = ""

            success, profile_path = run_enrollment(
                stream, pa, args.access_key, enroll_name, enroll_duration, kukuibot_url
            )

            if success:
                # Reload Eagle with new profile
                eagle_recognizer, eagle_speaker_names = load_eagle_recognizer(args.access_key, args.eagle_profile)
                logger.info(f"Eagle reloaded with {len(eagle_speaker_names)} speaker(s): {eagle_speaker_names}")

            # Notify server to clear the config flag
            try:
                clear_data = json.dumps({"listener_enroll_speaker": ""}).encode()
                clear_req = urlrequest.Request(
                    f"{kukuibot_url}/api/config",
                    data=clear_data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urlrequest.urlopen(clear_req, timeout=5, context=_ssl_ctx)
            except Exception as e:
                logger.warning(f"Failed to clear enrollment flag: {e}")

            logger.info("Resuming wake-word listening...")
            last_wake_time = time.time()  # cooldown after enrollment
            continue

        try:
            pcm = read_chunk(stream, VOSK_CHUNK_SAMPLES)
        except Exception as e:
            logger.error(f"Audio read error: {e}")
            time.sleep(0.5)
            continue

        audio_ring.append(pcm)

        # Feed audio to Vosk
        raw_bytes = pcm.tobytes()
        if vosk_rec.AcceptWaveform(raw_bytes):
            # Final result for this utterance
            result = json.loads(vosk_rec.Result())
            text = result.get("text", "").strip()

            if not text:
                continue

            now = time.time()
            if now - last_wake_time < config_state.get("cooldown", cooldown):
                continue

            # Check for wake word or direct-action trigger
            trigger_type, command = detect_trigger(text, config_state.get("triggers"))
            if trigger_type is None:
                continue

            # --- Trigger detected! ---
            last_wake_time = time.time()
            logger.info(f"WAKE DETECTED [{trigger_type}] via Vosk: '{text}' -> command: '{command}'")

            cached_mode = config_state.get("mode", "local")
            cooldown = config_state.get("cooldown", cooldown)

            if cached_mode == "remote":
                # --- Remote mode: verify speaker -> re-transcribe via Qwen3-ASR -> send to Jarvis ---
                fire_chime(kukuibot_url)

                # Speaker verification on the audio ring buffer
                pre_frames = list(audio_ring)
                cur_eagle_enabled = config_state.get("eagle_enabled", eagle_enabled)
                cur_eagle_threshold = config_state.get("eagle_threshold", eagle_threshold)
                if eagle_recognizer and cur_eagle_enabled:
                    passed, avg_score, speaker_name = verify_speaker(eagle_recognizer, eagle_speaker_names, pre_frames, cur_eagle_threshold)
                    if not passed:
                        logger.info(f"Speaker verification FAILED (score {avg_score:.3f} < {cur_eagle_threshold}) -- ignoring")
                        last_wake_time = time.time()
                        continue
                    logger.info(f"Speaker verified: {speaker_name} (score {avg_score:.3f})")

                if command:
                    # Vosk heard a command — re-transcribe the audio buffer via Qwen3-ASR for accuracy
                    wav_bytes = pcm_to_wav_bytes(pre_frames)
                    logger.info(f"Re-transcribing {len(pre_frames)} frames via Qwen3-ASR...")
                    accurate_text = transcribe_audio(wav_bytes, kukuibot_url, config_state.get("jarvis_direct_url", ""))
                    if accurate_text:
                        if trigger_type == "direct":
                            # Direct trigger — use the full Qwen3-ASR transcript as the command
                            logger.info(f"Qwen3-ASR: '{accurate_text}'")
                            command = accurate_text.strip()
                        else:
                            # Jarvis trigger — extract command portion after "jarvis"
                            accurate_cmd = extract_command(accurate_text)
                            logger.info(f"Qwen3-ASR: '{accurate_text}' -> command: '{accurate_cmd}'")
                            if accurate_cmd:
                                command = accurate_cmd
                            elif "jarvis" in accurate_text.lower():
                                command = ""
                    else:
                        logger.info("Qwen3-ASR returned empty — using Vosk transcript")

                if command:
                    logger.info(f"Sending to Jarvis (room={args.room}): '{command}'")
                    response = send_to_jarvis(command, args.room, kukuibot_url, config_state.get("jarvis_direct_url", ""))
                    if response:
                        logger.info(f"Jarvis: {response[:100]}")
                    else:
                        logger.info("No response from Jarvis")
                else:
                    # Just "Jarvis" with no command — record follow-up speech
                    logger.info("Wake word only, recording follow-up...")
                    silence_thresh_val = config_state.get("silence_threshold", silence_thresh)
                    silence_dur_val = config_state.get("silence_duration", silence_dur)
                    max_record_val = config_state.get("max_record", max_record)
                    min_record_val = config_state.get("min_record", min_record)

                    frames = list(audio_ring)
                    silence_chunks = 0
                    silence_limit = int(silence_dur_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    max_chunks = int(max_record_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    min_chunks = int(min_record_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    recorded = 0

                    logger.info("Recording speech...")
                    while not stop and recorded < max_chunks:
                        try:
                            rec_pcm = read_chunk(stream, VOSK_CHUNK_SAMPLES)
                        except Exception:
                            break
                        frames.append(rec_pcm)
                        recorded += 1

                        amp = rms(rec_pcm)
                        if amp < silence_thresh_val:
                            silence_chunks += 1
                        else:
                            silence_chunks = 0

                        if silence_chunks >= silence_limit and recorded >= min_chunks:
                            logger.info(f"Silence detected after {recorded} chunks (rms={amp:.0f})")
                            break

                    duration = recorded * VOSK_CHUNK_SAMPLES / SAMPLE_RATE
                    logger.info(f"Recorded {duration:.1f}s of speech ({recorded} chunks)")

                    if recorded < min_chunks:
                        logger.info("Too short -- ignoring")
                        last_wake_time = time.time()
                        continue

                    # Transcribe follow-up via Whisper
                    wav_bytes = pcm_to_wav_bytes(frames)
                    logger.info("Transcribing follow-up...")
                    transcript = transcribe_audio(wav_bytes, kukuibot_url, config_state.get("jarvis_direct_url", ""))

                    if not transcript:
                        logger.info("No speech detected -- resuming listening")
                        last_wake_time = time.time()
                        continue

                    logger.info(f"Transcript: '{transcript}'")
                    command = extract_command(transcript) or transcript
                    logger.info(f"Sending to Jarvis (room={args.room}): '{command}'")
                    response = send_to_jarvis(command, args.room, kukuibot_url, config_state.get("jarvis_direct_url", ""))
                    if response:
                        logger.info(f"Jarvis: {response[:100]}")
                    else:
                        logger.info("No response from Jarvis")

                last_wake_time = time.time()
            else:
                # --- Local mode ---
                if eagle_recognizer:
                    pre_frames = list(audio_ring)
                    passed, avg_score, speaker_name = verify_speaker(eagle_recognizer, eagle_speaker_names, pre_frames, eagle_threshold)
                    if not passed:
                        logger.info(f"Speaker verification FAILED (score {avg_score:.3f} < {eagle_threshold}) -- ignoring")
                        last_wake_time = time.time()
                        continue
                    logger.info(f"Speaker verified: {speaker_name} (score {avg_score:.3f})")

                threading.Thread(target=play_chime, daemon=True).start()

                if _local_asr_available and command:
                    # Local ASR + command detected in wake phrase — transcribe for accuracy
                    wav_bytes = pcm_to_wav_bytes(list(audio_ring))
                    accurate_text = _transcribe_local(wav_bytes)
                    if accurate_text:
                        cmd = extract_command(accurate_text) or accurate_text
                        threading.Thread(target=post_transcript_event,
                                         args=(kukuibot_url, cmd, args.room, True), daemon=True).start()
                    else:
                        # Transcription failed — fall back to wake event for browser STT
                        threading.Thread(target=post_wake_event, args=(kukuibot_url, 1.0, args.username, args.room), daemon=True).start()
                elif _local_asr_available and not command:
                    # Local ASR + wake word only — record follow-up speech
                    silence_thresh_val = config_state.get("silence_threshold", silence_thresh)
                    silence_dur_val = config_state.get("silence_duration", silence_dur)
                    max_record_val = config_state.get("max_record", max_record)
                    min_record_val = config_state.get("min_record", min_record)

                    frames = list(audio_ring)
                    silence_chunks = 0
                    silence_limit = int(silence_dur_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    max_chunks = int(max_record_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    min_chunks = int(min_record_val * SAMPLE_RATE / VOSK_CHUNK_SAMPLES)
                    recorded = 0

                    logger.info("Recording follow-up speech (local STT)...")
                    while not stop and recorded < max_chunks:
                        try:
                            rec_pcm = read_chunk(stream, VOSK_CHUNK_SAMPLES)
                        except Exception:
                            break
                        frames.append(rec_pcm)
                        recorded += 1
                        amp = rms(rec_pcm)
                        if amp < silence_thresh_val:
                            silence_chunks += 1
                        else:
                            silence_chunks = 0
                        if silence_chunks >= silence_limit and recorded >= min_chunks:
                            logger.info(f"Silence detected after {recorded} chunks")
                            break

                    duration = recorded * VOSK_CHUNK_SAMPLES / SAMPLE_RATE
                    logger.info(f"Recorded {duration:.1f}s of speech")
                    if recorded >= min_chunks:
                        wav_bytes = pcm_to_wav_bytes(frames)
                        transcript = _transcribe_local(wav_bytes)
                        if transcript:
                            cmd = extract_command(transcript) or transcript
                            threading.Thread(target=post_transcript_event,
                                             args=(kukuibot_url, cmd, args.room, True), daemon=True).start()
                        else:
                            logger.info("No speech detected from local STT")
                else:
                    # No local ASR — fall back to existing behavior: wake event → browser STT
                    threading.Thread(target=post_wake_event, args=(kukuibot_url, 1.0, args.username, args.room), daemon=True).start()
        else:
            # Partial result — we can optionally log it for debugging
            pass

    logger.info("Shutting down...")
    stream.stop_stream()
    stream.close()
    pa.terminate()
    if eagle_recognizer:
        eagle_recognizer.delete()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
