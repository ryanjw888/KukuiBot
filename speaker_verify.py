"""Speaker verification using SpeechBrain ECAPA-TDNN embeddings.

Replaces Picovoice Eagle with a fully open-source, no-API-key solution.
Provides enrollment (WAV → embedding) and verification (cosine similarity).

Model: ECAPA-TDNN trained on VoxCeleb (192-dim embeddings, 0.86% EER)
Profiles: saved as .npy files in ~/.jarvis/data/speaker_profile_{name}.npy
"""
import io
import logging
import os
import wave

import numpy as np

logger = logging.getLogger("kukuibot.speaker-verify")

MODEL_DIR = os.path.join(os.path.dirname(__file__), "..", "models", "ecapa-tdnn")
PROFILE_DIR = os.path.expanduser("~/.jarvis/data")
SAMPLE_RATE = 16000

# Lazy-loaded globals
_model_loaded = False
_compute_features = None
_mean_var_norm = None
_embedding_model = None
_mean_var_norm_emb = None
_torch = None

# Dependency availability check (runs at import time)
_missing_deps = []
for _mod_name in ("torch", "torchaudio"):
    try:
        __import__(_mod_name)
    except ImportError:
        _missing_deps.append(_mod_name)

# speechbrain requires torchaudio compat patch before import
if "torchaudio" not in _missing_deps:
    try:
        import torchaudio as _ta_check
        if not hasattr(_ta_check, "list_audio_backends"):
            _ta_check.list_audio_backends = lambda: ["default"]
        __import__("speechbrain")
    except ImportError:
        _missing_deps.append("speechbrain")
    except Exception:
        # speechbrain may fail for non-import reasons; treat as unavailable
        _missing_deps.append("speechbrain")
else:
    # Can't check speechbrain without torchaudio
    _missing_deps.append("speechbrain")

_model_files_exist = all(
    os.path.isfile(os.path.join(MODEL_DIR, f))
    for f in ("embedding_model.ckpt", "mean_var_norm_emb.ckpt", "hyperparams.yaml")
)

AVAILABLE = (not _missing_deps) and _model_files_exist

if _missing_deps:
    STATUS_MESSAGE = f"Missing Python packages: {', '.join(_missing_deps)}. Install with: pip install speechbrain torch torchaudio"
elif not _model_files_exist:
    STATUS_MESSAGE = f"ECAPA-TDNN model files not found in {MODEL_DIR}. Download the model first."
else:
    STATUS_MESSAGE = "ECAPA-TDNN speaker verification ready"


def _ensure_model():
    """Lazy-load the ECAPA-TDNN model on first use."""
    global _model_loaded, _compute_features, _mean_var_norm
    global _embedding_model, _mean_var_norm_emb, _torch

    if not AVAILABLE:
        raise RuntimeError(f"Speaker verification unavailable: {STATUS_MESSAGE}")

    if _model_loaded:
        return

    # Patch torchaudio compatibility (torchaudio 2.10+ dropped list_audio_backends)
    import torchaudio
    if not hasattr(torchaudio, "list_audio_backends"):
        torchaudio.list_audio_backends = lambda: ["default"]

    import torch
    _torch = torch

    from speechbrain.lobes.models.ECAPA_TDNN import ECAPA_TDNN
    from speechbrain.lobes.features import Fbank
    from speechbrain.processing.features import InputNormalization

    _compute_features = Fbank(n_mels=80)
    _mean_var_norm = InputNormalization(norm_type="sentence", std_norm=False)

    _embedding_model = ECAPA_TDNN(
        input_size=80,
        channels=[1024, 1024, 1024, 1024, 3072],
        kernel_sizes=[5, 3, 3, 3, 1],
        dilations=[1, 2, 3, 4, 1],
        attention_channels=128,
        lin_neurons=192,
    )
    _embedding_model.load_state_dict(
        torch.load(os.path.join(MODEL_DIR, "embedding_model.ckpt"),
                    map_location="cpu", weights_only=True)
    )
    _embedding_model.eval()

    # Load global mean/var normalization statistics
    _mean_var_norm_emb = InputNormalization(norm_type="global", std_norm=False)
    mvn_state = torch.load(
        os.path.join(MODEL_DIR, "mean_var_norm_emb.ckpt"),
        map_location="cpu", weights_only=True,
    )
    _mean_var_norm_emb.count = mvn_state["count"]
    _mean_var_norm_emb.glob_mean = mvn_state["glob_mean"]
    _mean_var_norm_emb.glob_std = mvn_state["glob_std"]
    _mean_var_norm_emb.spk_dict_mean = mvn_state["spk_dict_mean"]
    _mean_var_norm_emb.spk_dict_std = mvn_state["spk_dict_std"]
    _mean_var_norm_emb.spk_dict_count = mvn_state["spk_dict_count"]
    _mean_var_norm_emb.eval()

    _model_loaded = True
    logger.info("ECAPA-TDNN speaker verification model loaded (192-dim embeddings)")


def extract_embedding(pcm_int16: np.ndarray) -> np.ndarray:
    """Extract a 192-dim speaker embedding from 16kHz mono int16 PCM.

    Args:
        pcm_int16: numpy array of int16 samples at 16kHz

    Returns:
        numpy array of shape (192,) — the speaker embedding
    """
    _ensure_model()

    # Convert int16 to float32 [-1, 1]
    audio = pcm_int16.astype(np.float32) / 32768.0
    wav_tensor = _torch.from_numpy(audio).unsqueeze(0)  # [1, samples]

    with _torch.no_grad():
        feats = _compute_features(wav_tensor)
        feats = _mean_var_norm(feats, _torch.tensor([1.0]))
        emb = _embedding_model(feats)
        emb = _mean_var_norm_emb(emb, _torch.tensor([1.0]))

    return emb.squeeze(0).squeeze(0).numpy()  # [192]


def extract_embedding_from_wav(wav_bytes: bytes) -> np.ndarray:
    """Extract speaker embedding from WAV file bytes."""
    wf = wave.open(io.BytesIO(wav_bytes), "rb")
    channels = wf.getnchannels()
    sample_rate = wf.getframerate()
    n_frames = wf.getnframes()
    raw_pcm = np.frombuffer(wf.readframes(n_frames), dtype=np.int16)
    wf.close()

    # Convert stereo to mono
    if channels > 1:
        raw_pcm = raw_pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)

    # Resample if needed
    if sample_rate != SAMPLE_RATE:
        new_len = int(len(raw_pcm) * SAMPLE_RATE / sample_rate)
        indices = np.linspace(0, len(raw_pcm) - 1, new_len)
        raw_pcm = np.interp(indices, np.arange(len(raw_pcm)),
                            raw_pcm.astype(np.float64)).astype(np.int16)

    return extract_embedding(raw_pcm)


def enroll_speaker(name: str, wav_bytes: bytes) -> dict:
    """Enroll a speaker from WAV audio bytes.

    Extracts embedding and saves as speaker_profile_{name}.npy

    Returns dict with ok, name, profile_path, duration keys.
    """
    if not AVAILABLE:
        return {"ok": False, "error": STATUS_MESSAGE}

    safe_name = name.strip().lower().replace(" ", "_")
    if not safe_name or "/" in safe_name or ".." in safe_name:
        return {"ok": False, "error": "Invalid speaker name"}

    os.makedirs(PROFILE_DIR, exist_ok=True)
    profile_path = os.path.join(PROFILE_DIR, f"speaker_profile_{safe_name}.npy")

    try:
        wf = wave.open(io.BytesIO(wav_bytes), "rb")
    except Exception as e:
        return {"ok": False, "error": f"Invalid WAV file: {e}"}

    channels = wf.getnchannels()
    sample_rate = wf.getframerate()
    n_frames = wf.getnframes()
    raw_pcm = np.frombuffer(wf.readframes(n_frames), dtype=np.int16)
    wf.close()

    if channels > 1:
        raw_pcm = raw_pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)

    if sample_rate != SAMPLE_RATE:
        new_len = int(len(raw_pcm) * SAMPLE_RATE / sample_rate)
        indices = np.linspace(0, len(raw_pcm) - 1, new_len)
        raw_pcm = np.interp(indices, np.arange(len(raw_pcm)),
                            raw_pcm.astype(np.float64)).astype(np.int16)

    duration = len(raw_pcm) / SAMPLE_RATE
    if duration < 5:
        return {"ok": False, "error": f"Audio too short ({duration:.1f}s). Need at least 5 seconds."}

    # Extract embedding from the full recording
    embedding = extract_embedding(raw_pcm)

    # Save embedding
    np.save(profile_path, embedding)

    logger.info(f"Speaker enrolled: {name} → {profile_path} ({duration:.1f}s audio, 192-dim embedding)")
    return {
        "ok": True,
        "name": name,
        "profile_path": profile_path,
        "duration": f"{duration:.1f}s",
    }


def load_speaker_profiles() -> tuple:
    """Load all enrolled speaker profiles.

    Returns (embeddings_dict, speaker_names) where embeddings_dict maps
    name → numpy array of shape (192,).
    """
    import glob as globmod

    os.makedirs(PROFILE_DIR, exist_ok=True)
    pattern = os.path.join(PROFILE_DIR, "speaker_profile_*.npy")
    profile_files = sorted(globmod.glob(pattern))

    if not profile_files:
        return {}, []

    embeddings = {}
    names = []
    for pf in profile_files:
        basename = os.path.basename(pf)
        name = basename[len("speaker_profile_"):-len(".npy")]
        emb = np.load(pf)
        embeddings[name] = emb
        names.append(name)

    logger.info(f"Loaded {len(names)} speaker profile(s): {names}")
    return embeddings, names


def verify_speaker(pcm_frames: list, enrolled_embeddings: dict,
                   speaker_names: list, threshold: float = 0.35) -> tuple:
    """Verify speaker identity from PCM audio frames.

    Args:
        pcm_frames: list of numpy int16 arrays (audio chunks)
        enrolled_embeddings: dict mapping name → embedding array
        speaker_names: list of enrolled speaker names
        threshold: minimum cosine similarity to accept (0.0-1.0)

    Returns:
        (passed: bool, best_score: float, speaker_name: str)
    """
    if not enrolled_embeddings or not speaker_names:
        return True, 1.0, ""

    all_pcm = np.concatenate(pcm_frames)

    if len(all_pcm) < SAMPLE_RATE:  # less than 1 second
        logger.warning("Speaker verification: audio too short (<1s)")
        return True, 0.0, ""

    # Extract embedding from the audio
    test_embedding = extract_embedding(all_pcm)

    # Compare against all enrolled speakers via cosine similarity
    best_score = -1.0
    best_name = "unknown"

    for name in speaker_names:
        enrolled = enrolled_embeddings[name]
        # Cosine similarity
        dot = np.dot(test_embedding, enrolled)
        norm_a = np.linalg.norm(test_embedding)
        norm_b = np.linalg.norm(enrolled)
        if norm_a > 0 and norm_b > 0:
            score = dot / (norm_a * norm_b)
        else:
            score = 0.0

        if score > best_score:
            best_score = score
            best_name = name

    passed = best_score >= threshold
    logger.debug(f"Speaker scores: {', '.join(f'{n}={np.dot(test_embedding, enrolled_embeddings[n]) / (np.linalg.norm(test_embedding) * np.linalg.norm(enrolled_embeddings[n])):.3f}' for n in speaker_names)}")
    return passed, float(best_score), best_name


def get_enrolled_speakers() -> list:
    """List all enrolled speakers with metadata."""
    import glob as globmod

    os.makedirs(PROFILE_DIR, exist_ok=True)
    pattern = os.path.join(PROFILE_DIR, "speaker_profile_*.npy")
    profile_files = sorted(globmod.glob(pattern))

    speakers = []
    for pf in profile_files:
        basename = os.path.basename(pf)
        name = basename[len("speaker_profile_"):-len(".npy")]
        size = os.path.getsize(pf)
        mtime = os.path.getmtime(pf)
        import time
        speakers.append({
            "name": name,
            "profile_path": pf,
            "profile_size": size,
            "enrolled_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)),
        })

    return speakers
