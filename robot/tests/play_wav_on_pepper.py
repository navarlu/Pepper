#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function, unicode_literals

import time
import wave
import os
import sys

SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

QI_PY_PATH = os.environ.get(
    "PEPPER_QI_PY_PATH",
    "/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release",
)
BOOST_LIB = os.environ.get(
    "PEPPER_BOOST_LIB",
    "/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib",
)
QI_NATIVE = os.environ.get(
    "PEPPER_QI_NATIVE_LIB",
    "/home/lucas/Projects/FEL/QI_test/local_qi/lib",
)


def ensure_qi_runtime():
    """Make the host-side ARM64 libqi build visible before importing utils."""
    try:
        import qi  # noqa: F401
        return
    except ImportError:
        pass

    if os.path.isdir(QI_PY_PATH) and QI_PY_PATH not in sys.path:
        sys.path.insert(0, QI_PY_PATH)

    ld_paths = [p for p in (BOOST_LIB, QI_NATIVE) if os.path.isdir(p)]
    current_ld = os.environ.get("LD_LIBRARY_PATH", "")
    missing_ld = [p for p in ld_paths if p not in current_ld.split(":")]

    current_pythonpath = os.environ.get("PYTHONPATH", "")
    missing_pythonpath = (
        os.path.isdir(QI_PY_PATH) and QI_PY_PATH not in current_pythonpath.split(":")
    )

    if (missing_ld or missing_pythonpath) and not os.environ.get("PEPPER_QI_BOOTSTRAPPED"):
        env = os.environ.copy()
        if missing_ld:
            env["LD_LIBRARY_PATH"] = ":".join(missing_ld + ([current_ld] if current_ld else []))
        if missing_pythonpath:
            env["PYTHONPATH"] = QI_PY_PATH + (":" + current_pythonpath if current_pythonpath else "")
        env["PEPPER_QI_BOOTSTRAPPED"] = "1"
        os.execvpe(sys.executable, [sys.executable] + sys.argv, env)


ensure_qi_runtime()

import audioop

from utils import connect_session, to_text

# WAV to play
WAV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "hello.wav")

PEPPER_QI_URL = "tcp://10.42.0.205:9559"
TARGET_RATE = 48000      # Pepper output rate
CHUNK_MS = 20            # chunk size in ms
ATTENUATION = 0.1        # 1.0 = normal volume, <1 = quieter


def iter_wav_chunks(path, chunk_ms=CHUNK_MS):
    """
    Yield mono 16-bit PCM chunks at TARGET_RATE.
    Handles stereo→mono and resampling.
    """
    wf = wave.open(path, "rb")
    try:
        nch = wf.getnchannels()
        rate = wf.getframerate()
        sampwidth = wf.getsampwidth()

        if sampwidth != 2:
            raise ValueError("WAV must be 16-bit PCM (sampwidth=2 bytes).")
        if nch not in (1, 2):
            raise ValueError("WAV must be mono or stereo.")

        frames_per_chunk = int(rate * (chunk_ms / 1000.0))
        state = None

        while True:
            data = wf.readframes(frames_per_chunk)
            if not data:
                break

            # Downmix stereo → mono if needed
            if nch == 2:
                data = audioop.tomono(data, 2, 0.5, 0.5)

            # Resample to TARGET_RATE
            if rate != TARGET_RATE:
                data, state = audioop.ratecv(
                    data, 2, 1, rate, TARGET_RATE, state
                )

            # Apply attenuation
            if ATTENUATION != 1.0:
                data = audioop.mul(data, 2, ATTENUATION)

            yield data

    finally:
        wf.close()


def play_on_pepper(wav_path, qi_url=None, chunk_ms=CHUNK_MS):
    qi_url = qi_url or PEPPER_QI_URL
    print("[player] Using qi_url:", qi_url)
    print("[player] WAV file:", wav_path)

    sess = connect_session(qi_url)
    audio = sess.service("ALAudioDevice")

    # Make sure the output device is configured correctly
    try:
        audio.openAudioOutputs()
    except:
        pass

    try:
        audio.setParameter("outputSampleRate", TARGET_RATE)
    except:
        pass

    chunk_sec = float(chunk_ms) / 1000.0

    for mono_chunk in iter_wav_chunks(wav_path, chunk_ms):
        if not mono_chunk:
            continue

        # mono → stereo (interleaved)
        stereo_chunk = audioop.tostereo(mono_chunk, 2, 1, 1)

        # Frames = stereo samples (L+R), 4 bytes per frame
        nb_frames = len(stereo_chunk) // 4

        # NAOqi safety limit (should not be reached in 20ms chunks)
        if nb_frames > 16384:
            stereo_chunk = stereo_chunk[:16384 * 4]
            nb_frames = 16384

        audio.sendRemoteBufferToOutput(nb_frames, stereo_chunk)
        time.sleep(chunk_sec)

    print("[player] Done streaming file.")


def main():
    try:
        play_on_pepper(WAV_PATH)
    except Exception as e:
        print("[player] ERROR:", to_text(e))


if __name__ == "__main__":
    main()
