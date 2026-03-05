#!/usr/bin/env python3
"""Enroll a speaker's voice for Eagle speaker verification.

Usage: python3 enroll-voice.py --name Ryan [--device <mic>] [--duration 40]

Records speech, creates an Eagle speaker profile, and saves it as
~/.jarvis/data/eagle_profile_{name}.bin. The wake-listener loads all
profiles at startup and identifies who is speaking.
"""
import argparse
import logging
import os
import sys
import time
import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("enroll-voice")

ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "c7cXxfOZp42ls99y7tlBYNfwnIHS9yt9J/nyZAq5xaRz9PSzLm/JtQ==")
PROFILE_DIR = os.path.expanduser("~/.jarvis/data")


def main():
    parser = argparse.ArgumentParser(description="Enroll voice for Eagle speaker verification")
    parser.add_argument("--name", required=True, help="Speaker name (e.g. Ryan, Sarah)")
    parser.add_argument("--device", default=None, help="Mic device index or name")
    parser.add_argument("--output", default=None, help="Output profile path (auto-generated from name if omitted)")
    parser.add_argument("--duration", type=float, default=40.0, help="Recording duration in seconds")
    args = parser.parse_args()

    # Build output path from name if not specified
    if args.output is None:
        safe_name = args.name.strip().lower().replace(" ", "_")
        args.output = os.path.join(PROFILE_DIR, f"eagle_profile_{safe_name}.bin")

    import pveagle
    import pyaudio

    profiler = pveagle.create_profiler(access_key=ACCESS_KEY)
    sample_rate = profiler.sample_rate
    frame_length = profiler.min_enroll_samples

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

    stream = pa.open(
        format=pyaudio.paInt16, channels=1, rate=sample_rate,
        input=True, input_device_index=device_index,
        frames_per_buffer=frame_length,
    )

    print(f"\nRecording {args.duration}s of speech for voice enrollment.")
    print("   Please speak naturally -- read something aloud, count numbers, say commands you'd normally use.")
    print("   Press Ctrl+C to stop early.\n")
    time.sleep(1)

    total_frames = int(args.duration * sample_rate / frame_length)
    enroll_percentage = 0.0

    try:
        for i in range(total_frames):
            pcm = np.frombuffer(stream.read(frame_length, exception_on_overflow=False), dtype=np.int16)
            enroll_percentage, feedback = profiler.enroll(pcm)
            elapsed = (i + 1) * frame_length / sample_rate
            feedback_str = ""
            if feedback.name != "AUDIO_OK":
                feedback_str = f"  [{feedback.name}]"
            sys.stdout.write(f"\r   Enrolled: {enroll_percentage:.0f}%  ({elapsed:.1f}s){feedback_str}   ")
            sys.stdout.flush()
            if enroll_percentage >= 100.0:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    print()

    if enroll_percentage < 100.0:
        print(f"\n  Enrollment only {enroll_percentage:.0f}% complete.")
        print("   Eagle requires 100% enrollment to create a profile.")
        print("   Tips: speak louder, closer to the mic, minimize background noise.")
        print("   Try again with: python3 enroll-voice.py --duration 60")
        profiler.delete()
        return 1

    # Export and save profile
    profile = profiler.export()
    profiler.delete()

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    profile_bytes = profile.to_bytes()
    with open(args.output, "wb") as f:
        f.write(profile_bytes)

    print(f"\n  Voice profile saved to: {args.output} ({len(profile_bytes)} bytes)")
    print(f"   The wake-listener will load this automatically on next restart.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
