#!/usr/bin/env python3
"""Enroll a speaker's voice for ECAPA-TDNN speaker verification.

Usage: python3 enroll-voice.py --name Ryan [--device <mic>] [--duration 40]

Records speech, extracts an ECAPA-TDNN embedding, and saves it as
~/.jarvis/data/speaker_profile_{name}.npy. The wake-listener loads all
profiles at startup and identifies who is speaking.
"""
import argparse
import io
import logging
import os
import sys
import time
import wave
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("enroll-voice")

SAMPLE_RATE = 16000
PROFILE_DIR = os.path.expanduser("~/.jarvis/data")


def main():
    parser = argparse.ArgumentParser(description="Enroll voice for speaker verification")
    parser.add_argument("--name", required=True, help="Speaker name (e.g. Ryan, Sarah)")
    parser.add_argument("--device", default=None, help="Mic device index or name")
    parser.add_argument("--output", default=None, help="Output profile path (auto-generated from name if omitted)")
    parser.add_argument("--duration", type=float, default=40.0, help="Recording duration in seconds")
    args = parser.parse_args()

    import pyaudio

    pa = pyaudio.PyAudio()
    device_index = None
    if args.device is not None:
        try:
            device_index = int(args.device)
        except ValueError:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if str(args.device).lower() in info.get("name", "").lower() and int(info.get("maxInputChannels", 0)) > 0:
                    device_index = i
                    break

    frame_length = 4000  # 250ms chunks
    stream = pa.open(
        format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
        input=True, input_device_index=device_index,
        frames_per_buffer=frame_length,
    )

    print(f"\nRecording {args.duration}s of speech for voice enrollment.")
    print("   Please speak naturally -- read something aloud, count numbers, say commands you'd normally use.")
    print("   Press Ctrl+C to stop early.\n")
    time.sleep(1)

    total_frames = int(args.duration * SAMPLE_RATE / frame_length)
    all_audio = []

    try:
        for i in range(total_frames):
            pcm = np.frombuffer(stream.read(frame_length, exception_on_overflow=False), dtype=np.int16)
            all_audio.append(pcm)
            elapsed = (i + 1) * frame_length / SAMPLE_RATE
            sys.stdout.write(f"\r   Recording: {elapsed:.1f}s / {args.duration}s   ")
            sys.stdout.flush()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    print()

    all_pcm = np.concatenate(all_audio)
    duration = len(all_pcm) / SAMPLE_RATE

    if duration < 5:
        print(f"\n  Recording too short ({duration:.1f}s). Need at least 5 seconds.")
        return 1

    # Build WAV bytes
    buf = io.BytesIO()
    wf = wave.open(buf, "wb")
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SAMPLE_RATE)
    wf.writeframes(all_pcm.tobytes())
    wf.close()

    # Add parent dir to path for speaker_verify import
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from speaker_verify import enroll_speaker

    result = enroll_speaker(args.name, buf.getvalue())

    if result.get("ok"):
        print(f"\n  Voice profile saved to: {result['profile_path']}")
        print(f"   Duration: {result['duration']} of audio processed")
        print(f"   The wake-listener will load this automatically on next restart.")
        return 0
    else:
        print(f"\n  Enrollment failed: {result.get('error', 'unknown')}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
