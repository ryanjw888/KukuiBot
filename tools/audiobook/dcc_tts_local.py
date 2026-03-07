#!/usr/bin/env python3
"""
DCC Book 9 Chapter 1 — Local TTS with Qwen3-TTS voice cloning.

Generates audiobook from tagged chapter JSON using Jeff Hays' character voices
cloned via Qwen3-TTS Base model on Apple Silicon (M2 Ultra).

Usage:
    /Users/jarvis/.kukuibot/audiobook/qwen3-tts-apple-silicon/.venv/bin/python3 \
        /Users/jarvis/.kukuibot/src/tools/audiobook/dcc_tts_local.py [--test]

    --test    Generate only the first 5 segments (dry run)
"""

import os
import sys
import json
import time
import shutil
import wave
import subprocess
import tempfile
import warnings
from pathlib import Path

# Suppress noisy warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false"
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# Add qwen3-tts repo to path
REPO_DIR = "/Users/jarvis/.kukuibot/audiobook/qwen3-tts-apple-silicon"
sys.path.insert(0, REPO_DIR)

# ── Paths ──────────────────────────────────────────────────────────────
TAGGED_JSON = "/Users/jarvis/.kukuibot/audiobook/chapter_001_tagged.json"
VOICES_DIR = "/Users/jarvis/.kukuibot/audiobook/voices"
BASE_MODEL_PATH = os.path.join(REPO_DIR, "models", "Qwen3-TTS-12Hz-1.7B-Base-8bit")
OUTPUT_DIR = "/Users/jarvis/.kukuibot/audiobook/cloned_segments"
FINAL_MP3 = "/Users/jarvis/.kukuibot/audiobook/chapter_001_cloned_final.mp3"
FFMPEG = "/opt/homebrew/bin/ffmpeg"

# ── Voice mapping ──────────────────────────────────────────────────────
# Each key is (speaker, segment_type). Value is a list of ref audio filenames to cycle through.
VOICE_MAP = {
    ("carl", "carl_narration"):   ["carl_narration_01.wav", "carl_narration_02.wav", "carl_narration_03.wav"],
    ("carl", "carl_spoken"):      ["carl_dialogue_01.wav", "carl_dialogue_02.wav"],
    ("donut", "donut_spoken"):    ["donut_01.wav", "donut_02.wav", "donut_03.wav"],
    ("donut", "donut_chat"):      ["donut_01.wav", "donut_02.wav", "donut_03.wav"],
    ("mordecai", "mordecai_chat"): ["mordecai_01.wav", "mordecai_02.wav"],
    ("system", "system_message"): ["system_01.wav", "system_02.wav", "system_03.wav"],
    ("system", "description_box"): ["system_01.wav", "system_02.wav", "system_03.wav"],
}

# Fallback voices for speakers not in the main map
SPEAKER_FALLBACKS = {
    "hedy the gremlin": ["carl_dialogue_01.wav", "carl_dialogue_02.wav"],
    "waldrip chris":    ["carl_dialogue_01.wav", "carl_dialogue_02.wav"],
    "justice light":    ["carl_narration_01.wav", "carl_narration_02.wav", "carl_narration_03.wav"],
    "rosetta":          ["samantha_01.wav", "samantha_02.wav", "samantha_03.wav"],
}

# ── Emotion prefixes ──────────────────────────────────────────────────
EMOTION_PREFIX = {
    "excited":   "(with excitement) ",
    "dramatic":  "(dramatically) ",
    "sarcastic": "(sarcastically) ",
    "somber":    "(sadly) ",
    "urgent":    "(urgently) ",
    "angry":     "(angrily) ",
    "shocked":   "(in shock) ",
    "amused":    "(with amusement) ",
}

# Types that should never get emotion prefixes
NO_EMOTION_TYPES = {"system_message", "description_box"}

# Counters for cycling through reference clips
_voice_counters: dict[str, int] = {}


def get_ref_audio(speaker: str, seg_type: str) -> str:
    """Pick the next reference audio file, cycling through available clips."""
    key = (speaker, seg_type)
    clips = VOICE_MAP.get(key)
    if not clips:
        clips = SPEAKER_FALLBACKS.get(speaker)
    if not clips:
        # Ultimate fallback: carl narration
        clips = ["carl_narration_01.wav"]

    counter_key = f"{speaker}_{seg_type}"
    idx = _voice_counters.get(counter_key, 0)
    chosen = clips[idx % len(clips)]
    _voice_counters[counter_key] = idx + 1
    return os.path.join(VOICES_DIR, chosen)


def prepare_text(text: str, seg_type: str, emotion: str) -> str:
    """Prepare segment text: apply emotion prefix, handle ALL CAPS for donut_chat."""
    # Convert ALL CAPS to title case for donut_chat
    if seg_type == "donut_chat":
        text = caps_to_title(text)

    # Add emotion prefix (except for system/description types)
    if seg_type not in NO_EMOTION_TYPES and emotion in EMOTION_PREFIX:
        text = EMOTION_PREFIX[emotion] + text

    return text


def caps_to_title(text: str) -> str:
    """Convert ALL CAPS words to title case, leaving mixed-case words alone."""
    words = text.split()
    result = []
    for w in words:
        # Check if the word (minus punctuation) is all uppercase and >1 char
        stripped = w.strip(".,!?;:\"'—-()[]")
        if stripped.isupper() and len(stripped) > 1:
            result.append(w.capitalize() if w == stripped else _title_with_punct(w))
        else:
            result.append(w)
    return " ".join(result)


def _title_with_punct(word: str) -> str:
    """Title-case a word while preserving leading/trailing punctuation."""
    leading = ""
    trailing = ""
    i = 0
    while i < len(word) and not word[i].isalpha():
        leading += word[i]
        i += 1
    j = len(word) - 1
    while j >= i and not word[j].isalpha():
        trailing = word[j] + trailing
        j -= 1
    core = word[i:j + 1] if i <= j else ""
    return leading + core.capitalize() + trailing


def get_wav_duration(path: str) -> float:
    """Get WAV duration in seconds."""
    try:
        with wave.open(path, 'rb') as wf:
            return wf.getnframes() / float(wf.getframerate())
    except Exception:
        return 0.0


def extract_output_wav(temp_dir: str, output_path: str) -> bool:
    """Move generated audio_000.wav from temp dir to output path."""
    source = os.path.join(temp_dir, "audio_000.wav")
    if os.path.exists(source):
        shutil.move(source, output_path)
        return True
    # Fallback: any WAV in temp dir
    for f in os.listdir(temp_dir):
        if f.endswith(".wav"):
            shutil.move(os.path.join(temp_dir, f), output_path)
            return True
    return False


def generate_segment(model, seg: dict, output_path: str) -> dict:
    """Generate one segment's audio. Returns stats dict."""
    seg_id = seg["id"]
    speaker = seg["speaker"]
    seg_type = seg["type"]
    emotion = seg.get("emotion", "neutral")
    raw_text = seg["text"]

    ref_audio = get_ref_audio(speaker, seg_type)
    ref_name = os.path.basename(ref_audio)
    text = prepare_text(raw_text, seg_type, emotion)

    # Truncate display text
    display_text = raw_text[:50] + "..." if len(raw_text) > 50 else raw_text

    temp_dir = tempfile.mkdtemp(prefix=f"tts_seg{seg_id:03d}_")

    try:
        from mlx_audio.tts.generate import generate_audio

        t0 = time.time()
        generate_audio(
            model=model,
            text=text,
            ref_audio=ref_audio,
            ref_text=".",
            output_path=temp_dir,
        )
        gen_time = time.time() - t0

        if extract_output_wav(temp_dir, output_path):
            duration = get_wav_duration(output_path)
            size_kb = os.path.getsize(output_path) / 1024
            return {
                "ok": True,
                "gen_time": gen_time,
                "duration": duration,
                "size_kb": size_kb,
                "ref": ref_name,
                "display": display_text,
                "emotion": emotion,
                "seg_type": seg_type,
            }
        else:
            return {"ok": False, "error": "No WAV output", "display": display_text}
    except Exception as e:
        return {"ok": False, "error": str(e), "display": display_text}
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)


def concatenate_segments(segments: list[dict], wav_dir: str, output_mp3: str):
    """Concatenate WAV segments with silence gaps using ffmpeg."""
    # Generate silence WAVs at 24000 Hz (Qwen3-TTS default sample rate)
    sr = 24000
    silence_dir = tempfile.mkdtemp(prefix="tts_silence_")

    def make_silence(duration: float, name: str) -> str:
        path = os.path.join(silence_dir, name)
        subprocess.run([
            FFMPEG, "-y", "-f", "lavfi", "-i",
            f"anullsrc=channel_layout=mono:sample_rate={sr}",
            "-t", str(duration), path,
        ], capture_output=True)
        return path

    silence_03 = make_silence(0.3, "silence_03.wav")
    silence_08 = make_silence(0.8, "silence_08.wav")
    silence_12 = make_silence(1.2, "silence_12.wav")

    # Build concat list
    concat_file = os.path.join(silence_dir, "concat.txt")
    prev_speaker = None
    prev_type = None
    entries = []

    for seg in segments:
        seg_id = seg["id"]
        seg_type = seg["type"]
        speaker = seg["speaker"]
        wav_path = os.path.join(wav_dir, f"chapter_001_seg_{seg_id:03d}.wav")

        if not os.path.exists(wav_path) or os.path.getsize(wav_path) == 0:
            continue

        # Determine silence before this segment
        if prev_speaker is not None:
            if seg_type in ("system_message", "description_box") or \
               prev_type in ("system_message", "description_box"):
                entries.append(silence_12)
            elif speaker != prev_speaker:
                entries.append(silence_08)
            else:
                entries.append(silence_03)

        entries.append(wav_path)
        prev_speaker = speaker
        prev_type = seg_type

    with open(concat_file, 'w') as f:
        for entry in entries:
            f.write(f"file '{entry}'\n")

    # Concatenate to MP3
    subprocess.run([
        FFMPEG, "-y", "-f", "concat", "-safe", "0",
        "-i", concat_file,
        "-codec:a", "libmp3lame", "-b:a", "192k",
        "-ac", "1", "-ar", "24000",
        output_mp3,
    ], capture_output=True)

    # Cleanup
    shutil.rmtree(silence_dir, ignore_errors=True)


def main():
    test_mode = "--test" in sys.argv
    limit = 5 if test_mode else None

    print("=" * 60)
    print("DCC Book 9 Chapter 1 — Qwen3-TTS Voice Clone Generator")
    print(f"Mode: {'TEST (first 5 segments)' if test_mode else 'FULL (all segments)'}")
    print("=" * 60)

    # Load tagged JSON
    with open(TAGGED_JSON) as f:
        data = json.load(f)
    segments = data["segments"]
    if limit:
        segments = segments[:limit]
    total = len(segments)

    print(f"Segments to generate: {total}")
    print(f"Output dir: {OUTPUT_DIR}")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Load model once
    print(f"\nLoading Base model...")
    from mlx_audio.tts.utils import load_model
    t0 = time.time()
    model = load_model(BASE_MODEL_PATH)
    load_time = time.time() - t0
    print(f"Model loaded in {load_time:.1f}s")

    # Generate segments
    total_gen_time = 0.0
    total_audio_dur = 0.0
    success = 0
    failed = 0
    skipped = 0
    overall_start = time.time()

    for i, seg in enumerate(segments):
        seg_id = seg["id"]
        output_path = os.path.join(OUTPUT_DIR, f"chapter_001_seg_{seg_id:03d}.wav")

        # Resume support: skip if already exists
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            dur = get_wav_duration(output_path)
            total_audio_dur += dur
            skipped += 1
            print(f"[{i+1}/{total}] SKIP (exists) seg_{seg_id:03d}.wav ({dur:.1f}s)")
            continue

        result = generate_segment(model, seg, output_path)

        if result["ok"]:
            success += 1
            total_gen_time += result["gen_time"]
            total_audio_dur += result["duration"]
            print(
                f"[{i+1}/{total}] {result['seg_type']} ({result['emotion']}) "
                f"-> {result['ref']}: \"{result['display']}\" "
                f"({result['gen_time']:.1f}s gen, {result['duration']:.1f}s audio)"
            )
        else:
            failed += 1
            print(f"[{i+1}/{total}] FAIL seg_{seg_id:03d}: {result.get('error', 'unknown')}")

    wall_time = time.time() - overall_start

    print(f"\n{'=' * 60}")
    print("GENERATION SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Total segments:  {total}")
    print(f"  Generated:       {success}")
    print(f"  Skipped (exist): {skipped}")
    print(f"  Failed:          {failed}")
    print(f"  Total audio:     {total_audio_dur:.1f}s ({total_audio_dur/60:.1f}m)")
    print(f"  Gen time:        {total_gen_time:.1f}s ({total_gen_time/60:.1f}m)")
    print(f"  Wall time:       {wall_time:.1f}s ({wall_time/60:.1f}m)")

    if test_mode:
        print(f"\nTest mode — skipping concatenation.")
        print("Run without --test to generate all segments and concatenate.")
        return

    # Concatenate into final MP3
    print(f"\nConcatenating {success + skipped} segments into final MP3...")
    # Use ALL segments from original data for concatenation (not just the subset)
    with open(TAGGED_JSON) as f:
        all_data = json.load(f)
    concatenate_segments(all_data["segments"], OUTPUT_DIR, FINAL_MP3)

    if os.path.exists(FINAL_MP3):
        size_mb = os.path.getsize(FINAL_MP3) / (1024 * 1024)
        # Get duration via ffprobe
        result = subprocess.run(
            [FFMPEG.replace("ffmpeg", "ffprobe"), "-v", "quiet", "-show_entries",
             "format=duration", "-of", "csv=p=0", FINAL_MP3],
            capture_output=True, text=True,
        )
        mp3_dur = float(result.stdout.strip()) if result.stdout.strip() else 0
        print(f"  Output: {FINAL_MP3}")
        print(f"  Size:   {size_mb:.1f} MB")
        print(f"  Duration: {mp3_dur:.1f}s ({mp3_dur/60:.1f}m)")
    else:
        print(f"  FAIL: {FINAL_MP3} not created")


if __name__ == "__main__":
    main()
