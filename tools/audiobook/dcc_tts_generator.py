#!/usr/bin/env python3
"""
DCC Audiobook TTS Generator
Reads speaker-tagged JSON from the DCC parser and generates a complete
audiobook chapter MP3 using the ElevenLabs V3 API.

Usage:
    python3 dcc_tts_generator.py [--dry-run N] [--input FILE] [--output-dir DIR]
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
import argparse
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = "sk_3012c5eed997818a3ba698ee536138c5ec92a83ba64bfbd3"
TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_128"

DEFAULT_INPUT = Path(__file__).resolve().parents[3] / "audiobook_output" / "chapter_001_tagged.json"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[3] / "audiobook_output"

# Voice mapping: speaker -> (voice_id, default voice_settings)
VOICE_MAP = {
    "carl": {
        "voice_id": "iP95p4xoKVk53GoZ742B",
        "narration": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.3, "speed": 1.0},
        "spoken":    {"stability": 0.4, "similarity_boost": 0.75, "style": 0.6, "speed": 1.0},
    },
    "donut": {
        "voice_id": "FGY2WhTYpPnrIDTdsKH5",
        "default": {"stability": 0.35, "similarity_boost": 0.8, "style": 0.8, "speed": 1.0},
    },
    "mordecai": {
        "voice_id": "cjVigY5qzO86Huf0OWal",
        "default": {"stability": 0.6, "similarity_boost": 0.8, "style": 0.4, "speed": 1.0},
    },
    "system": {
        "voice_id": "onwK4e9ZLuTAKqWW03F9",
        "default": {"stability": 0.7, "similarity_boost": 0.9, "style": 0.2, "speed": 0.95},
    },
    "hedy the gremlin": {
        "voice_id": "N2lVS1w4EtoT3dr4eOWO",
        "default": {"stability": 0.4, "similarity_boost": 0.75, "style": 0.7, "speed": 1.0},
    },
    "waldrip chris": {
        "voice_id": "N2lVS1w4EtoT3dr4eOWO",
        "default": {"stability": 0.35, "similarity_boost": 0.75, "style": 0.8, "speed": 1.0},
    },
    "justice light": {
        "voice_id": "JBFqnCBsd6RMkjVDRZzb",
        "default": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.5, "speed": 1.0},
    },
    "rosetta": {
        "voice_id": "EXAVITQu4vr4xnSDxMaL",
        "default": {"stability": 0.5, "similarity_boost": 0.75, "style": 0.5, "speed": 1.0},
    },
}

# Emotion -> V3 audio tag prefix
EMOTION_TAGS = {
    "excited":  "[excited] ",
    "dramatic": "[dramatic] ",
    "sarcastic": "[sarcastically] ",
    "somber":   "[sadly] ",
    "urgent":   "[urgently] ",
    "angry":    "[angrily] ",
    "shocked":  "[shocked] ",
    "amused":   "[amused] ",
    "neutral":  "",
}

# Segment types that never get emotion tags
NO_EMOTION_TYPES = {"system_message", "description_box"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_voice_config(segment):
    """Return (voice_id, voice_settings) for a segment."""
    speaker = segment["speaker"].lower()
    seg_type = segment["type"]

    entry = VOICE_MAP.get(speaker, VOICE_MAP["carl"])
    voice_id = entry["voice_id"]

    # Carl has different settings for narration vs spoken
    if speaker == "carl":
        if "narration" in seg_type:
            settings = dict(entry["narration"])
        else:
            settings = dict(entry["spoken"])
    else:
        settings = dict(entry["default"])

    # description_box gets slower speed
    if seg_type == "description_box":
        settings["speed"] = 0.9

    return voice_id, settings


def prepare_text(segment):
    """Apply text transforms and prepend emotion audio tags."""
    text = segment["text"]
    seg_type = segment["type"]
    emotion = segment.get("emotion", "neutral")

    # donut_chat: convert ALL CAPS to normal case
    if seg_type == "donut_chat":
        # Convert words that are all-caps (3+ chars) to title case
        words = text.split()
        converted = []
        for w in words:
            # Preserve punctuation: strip trailing punct, check if alpha part is all caps
            stripped = w.rstrip(".,!?;:'\"")
            suffix = w[len(stripped):]
            if len(stripped) >= 2 and stripped.isalpha() and stripped.isupper():
                converted.append(stripped.capitalize() + suffix)
            elif stripped.isupper() and len(stripped) >= 2:
                converted.append(stripped.capitalize() + suffix)
            else:
                converted.append(w)
        text = " ".join(converted)

    # Prepend emotion tag (skip for system_message and description_box)
    if seg_type not in NO_EMOTION_TYPES:
        tag = EMOTION_TAGS.get(emotion, "")
        if tag:
            text = tag + text

    return text


def call_tts(voice_id, text, voice_settings, retries=3):
    """Call ElevenLabs TTS API. Returns (audio_bytes, elapsed_seconds)."""
    url = TTS_URL.format(voice_id=voice_id)
    payload = json.dumps({
        "text": text,
        "model_id": "eleven_v3",
        "voice_settings": voice_settings,
    }).encode("utf-8")

    headers = {
        "xi-api-key": API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    for attempt in range(retries):
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio = resp.read()
                elapsed = time.time() - t0
                return audio, elapsed
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 5 * (attempt + 1)
                print(f"  Rate limited (429). Waiting {wait}s before retry {attempt + 2}/{retries}...")
                time.sleep(wait)
            else:
                body = e.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"ElevenLabs API error {e.code}: {body}") from e
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                raise


def concatenate_segments(segments_data, segment_dir, output_path):
    """Concatenate individual segment MP3s with appropriate silence gaps using ffmpeg."""
    import subprocess
    import tempfile
    import struct
    import wave

    special_types = {"system_message", "description_box"}

    def make_silence_mp3(duration_ms, tmp_dir):
        """Generate a silent MP3 of given duration using ffmpeg."""
        silence_path = os.path.join(tmp_dir, f"silence_{duration_ms}ms.mp3")
        if not os.path.exists(silence_path):
            subprocess.run([
                "ffmpeg", "-y", "-f", "lavfi",
                "-i", f"anullsrc=r=44100:cl=stereo",
                "-t", str(duration_ms / 1000.0),
                "-b:a", "128k", "-q:a", "2",
                silence_path
            ], capture_output=True, check=True)
        return silence_path

    # Build the concat list with silence gaps
    with tempfile.TemporaryDirectory() as tmp_dir:
        silence_500 = make_silence_mp3(500, tmp_dir)
        silence_1000 = make_silence_mp3(1000, tmp_dir)
        silence_1500 = make_silence_mp3(1500, tmp_dir)

        concat_list = []
        prev_speaker = None
        prev_type = None

        for seg in segments_data:
            seg_file = segment_dir / f"chapter_001_seg_{seg['id']:03d}.mp3"
            if not seg_file.exists():
                print(f"  WARNING: Missing segment file {seg_file.name}, skipping")
                continue

            current_speaker = seg["speaker"]
            current_type = seg["type"]

            # Add silence gap before this segment
            if concat_list:  # not the first segment
                if current_type in special_types or prev_type in special_types:
                    concat_list.append(silence_1500)
                elif current_speaker != prev_speaker:
                    concat_list.append(silence_1000)
                else:
                    concat_list.append(silence_500)

            concat_list.append(str(seg_file))
            prev_speaker = current_speaker
            prev_type = current_type

        # Write ffmpeg concat file
        concat_file = os.path.join(tmp_dir, "concat.txt")
        with open(concat_file, "w") as f:
            for path in concat_list:
                # ffmpeg concat demuxer requires escaped single quotes in paths
                safe = path.replace("'", "'\\''")
                f.write(f"file '{safe}'\n")

        # Run ffmpeg concat
        result = subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", concat_file,
            "-c:a", "libmp3lame", "-b:a", "128k",
            str(output_path)
        ], capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-500:]}")

    # Get duration from ffprobe
    probe = subprocess.run([
        "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
        "-of", "csv=p=0", str(output_path)
    ], capture_output=True, text=True)
    duration_s = float(probe.stdout.strip()) if probe.stdout.strip() else 0.0
    return duration_s


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DCC Audiobook TTS Generator")
    parser.add_argument("--dry-run", type=int, default=0,
                        help="Only process first N segments (0 = all)")
    parser.add_argument("--input", type=str, default=str(DEFAULT_INPUT),
                        help="Path to tagged JSON file")
    parser.add_argument("--output-dir", type=str, default=str(DEFAULT_OUTPUT_DIR),
                        help="Output directory for audio files")
    parser.add_argument("--skip-concat", action="store_true",
                        help="Skip final concatenation step")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    segment_dir = output_dir / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)

    # Load tagged JSON
    with open(input_path, "r") as f:
        data = json.load(f)

    segments = data["segments"]
    total = len(segments)

    if args.dry_run > 0:
        segments = segments[:args.dry_run]
        print(f"DRY RUN: Processing first {args.dry_run} of {total} segments\n")

    process_count = len(segments)
    total_chars = 0
    generated = 0
    skipped = 0
    start_time = time.time()

    print(f"DCC TTS Generator — Chapter {data.get('chapter', '?')}")
    print(f"Segments: {process_count} of {total}")
    print(f"Output: {segment_dir}\n")

    for i, seg in enumerate(segments, 1):
        seg_id = seg["id"]
        seg_file = segment_dir / f"chapter_001_seg_{seg_id:03d}.mp3"

        # Resume support: skip if file already exists and is non-empty
        if seg_file.exists() and seg_file.stat().st_size > 0:
            skipped += 1
            text = prepare_text(seg)
            total_chars += len(text)
            print(f"[{i}/{process_count}] SKIP (exists) seg_{seg_id:03d}.mp3")
            continue

        voice_id, settings = get_voice_config(seg)
        text = prepare_text(seg)
        total_chars += len(text)

        # Get voice name for display
        speaker = seg["speaker"].lower()
        voice_entry = VOICE_MAP.get(speaker, VOICE_MAP["carl"])
        truncated = text[:50].replace("\n", " ")
        if len(text) > 50:
            truncated += "..."

        try:
            audio_bytes, elapsed = call_tts(voice_id, text, settings)
            with open(seg_file, "wb") as f:
                f.write(audio_bytes)
            generated += 1
            size_kb = len(audio_bytes) / 1024
            print(f"[{i}/{process_count}] {seg['type']} ({seg.get('emotion', 'neutral')}) -> {seg['speaker']}: \"{truncated}\" ({elapsed:.1f}s, {size_kb:.0f}KB)")
        except Exception as e:
            print(f"[{i}/{process_count}] ERROR seg_{seg_id:03d}: {e}")

        # Rate limiting
        time.sleep(0.5)

    elapsed_total = time.time() - start_time

    print(f"\n--- Generation Complete ---")
    print(f"Generated: {generated} | Skipped: {skipped} | Total chars: {total_chars:,}")
    print(f"Time: {elapsed_total:.1f}s")

    # Concatenation
    if not args.skip_concat and not args.dry_run:
        print(f"\nConcatenating {total} segments into final MP3...")
        final_path = output_dir / "chapter_001_final.mp3"
        try:
            duration = concatenate_segments(data["segments"], segment_dir, final_path)
            size_mb = final_path.stat().st_size / (1024 * 1024)
            print(f"\n=== FINAL OUTPUT ===")
            print(f"File: {final_path}")
            print(f"Duration: {duration / 60:.1f} minutes ({duration:.0f}s)")
            print(f"Size: {size_mb:.1f} MB")
            print(f"Total segments: {total}")
            print(f"Total characters: {total_chars:,}")
        except Exception as e:
            print(f"Concatenation error: {e}")
    elif args.dry_run:
        print(f"\nDry run — skipping concatenation.")


if __name__ == "__main__":
    main()
