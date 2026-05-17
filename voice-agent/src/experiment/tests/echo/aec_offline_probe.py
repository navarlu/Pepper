"""Phase 1 — offline AEC probe.

Proof-of-concept that `webrtc-audio-processing` (Google's APM) can
cancel Pepper's TTS bleed before it reaches STT. No agent, no
LiveKit, no API key required — fully self-contained.

What this script does:

  1. Connects to `robot/src/bridge.py` over TCP using the SAME socket
     protocol that `services/src/live/audio_bridge.py` uses
     (`<4-byte BE length><PCM>` frames). Production audio-bridge must
     be stopped first since the robot accepts one TCP client at a time.

  2. Streams a pre-recorded WAV through Pepper's chest speaker at
     real-time pacing (50 ms chunks, light sleep between sends so
     NAOqi's queue does not back up).

  3. Captures the USB mic in parallel via `sounddevice`. The mic
     hears Pepper's voice through the air — that is the "echo" we
     want to cancel.

  4. Holds a COPY of the same WAV in memory as the AEC reference.
     This is the cleanest possible reference: no clock drift, no
     LiveKit jitter, no decoding loss. If AEC cannot work in this
     setup it will not work in production either.

  5. Runs the captured mic through `webrtc-audio-processing`'s AEC
     module with the WAV as the reverse stream (= what the speaker
     is playing).

  6. Writes `mic_raw.wav`, `ref.wav`, `mic_aec.wav` and prints the
     ERLE (Echo Return Loss Enhancement) in dB — the headline
     metric. >= 15 dB is good; 20-30 dB is great.

How to run, on the RPi (the mic and the robot bridge both have to
be reachable):

    docker compose -f docker/docker-compose.experiment.yml stop audio-bridge
    uv run python voice-agent/src/experiment/tests/echo/aec_offline_probe.py

Drop your own test WAV at:
    voice-agent/src/experiment/tests/echo/inputs/test_phrase.wav

If that file is missing the script generates a 3-second sine sweep
(100 Hz -> 8 kHz). The sweep is actually a better stress test for
AEC than speech because it has energy across all frequencies, but
running it once with a real TTS phrase is the most representative.

When done, restart audio-bridge:
    docker compose -f docker/docker-compose.experiment.yml start audio-bridge
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np


# ── Paths ────────────────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
INPUT_DIR = THIS_DIR / "inputs"
OUTPUT_DIR = THIS_DIR / "outputs"
DEFAULT_WAV = INPUT_DIR / "test_phrase.wav"


# ── Tunables (env-overridable; defaults match production) ────────────
# Robot bridge — same constants `services/src/live/audio_bridge.py`
# reads from `services/src/live/config.py`. The bridge listens on
# Pepper at TCP_HOST:TCP_PORT.
TCP_HOST = os.environ.get("TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("TCP_PORT", "55555"))

# Sample rate used through this script. PEPPER_STREAM_RATE in production
# defaults to 16000; APM supports 8/16/32/48 kHz. Keeping everything at
# 16 kHz mono int16 makes the test plumbing trivial.
SR = 16000
CHANNELS = 1
SAMP_WIDTH = 2  # int16

# Mic capture: sounddevice. Specific device can be pinned via env var.
# Falls back to USER_MIC_DEVICE so the test uses the same mic as
# production `user_client.py` when run on the RPi without extra config.
# Accepts an int index or a substring of the device name.
MIC_DEVICE_RAW = os.environ.get("MIC_DEVICE") or os.environ.get("USER_MIC_DEVICE")
if MIC_DEVICE_RAW is not None and MIC_DEVICE_RAW.strip().lstrip("-").isdigit():
    MIC_DEVICE: int | str | None = int(MIC_DEVICE_RAW)
elif MIC_DEVICE_RAW:
    MIC_DEVICE = MIC_DEVICE_RAW
else:
    MIC_DEVICE = None

# Playback pacing. The robot's NAOqi queue plays at real-time; if we
# push faster than real-time the queue backs up (this is the same
# problem the production silence-gate solves). 50 ms chunks with a
# 45 ms sleep between sends keeps the queue gently full.
CHUNK_MS = 50
SLEEP_BETWEEN_CHUNKS_S = 0.045

# Wait this long after the last chunk before we stop the mic. Needs
# to cover NAOqi's playback buffer — measured at ~1.0 s on this
# Pepper — plus the reverb tail. Set to 3 s so the captured mic
# window has the FULL aligned playback inside it; otherwise the AEC
# only gets to adapt on a small fraction of the recording.
TAIL_S = 3.0

# AEC frame size. SpeexDSP accepts any frame size; 40 ms (640 samples
# at 16 kHz) gives a good convergence/latency trade and showed the
# best ERLE in offline sweeps.
APM_FRAME_MS = 40
APM_FRAME_SAMPLES = SR * APM_FRAME_MS // 1000          # 640 samples @ 16 kHz
APM_FRAME_BYTES = APM_FRAME_SAMPLES * SAMP_WIDTH       # 1280 bytes

# Safety margin subtracted from the measured envelope delay before
# pre-aligning the reference. SpeexDSP's adaptive filter is causal —
# it can model "reference leads mic" but not the reverse. If we
# pre-align by exactly the envelope-correlation peak we usually
# overshoot the true delay by 20–40 ms (the envelope smooths peaks
# away from the speech onset edge), tipping ERLE into the near-zero
# regime. Subtracting a small margin keeps us safely on the causal
# side; the filter then learns the residual offset internally.
PREALIGN_SAFETY_MS = 30

# ERLE measurement window: we compute ERLE only over the region where
# the reference signal is actually loud (otherwise dividing by silence
# gives infinite gains). Threshold is fraction of peak.
ERLE_REF_LEVEL_FRAC = 0.05


# ── Dependency probes ───────────────────────────────────────────────
try:
    import sounddevice as sd  # type: ignore
except ImportError:
    print("ERROR: sounddevice not installed. `pip install sounddevice`")
    sys.exit(1)

try:
    from scipy.signal import correlate, resample_poly  # type: ignore
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

# Speex DSP for AEC. The PyPI `webrtc-audio-processing` binding was
# tried first but is unfixable on ARM (bundles x86 SSE source files
# with unconditional references; the package has not been maintained
# since 2018). SpeexDSP is older but compiles cleanly on ARM, has a
# simple ctypes-style Python binding, and gives 10-20 dB ERLE which
# is plenty to validate the principle.
#
# Install on the RPi:
#   sudo apt install libspeexdsp-dev
#   uv pip install speexdsp
#
# The PyPI `speexdsp` package ships a SWIG wrapper that does
# `import imp` — a stdlib module removed in Python 3.12. The C
# extension itself is fine; we pre-load it under the name the wrapper
# expects and stub `imp` so the wrapper falls through to that.
def _install_speexdsp_py312_shim() -> None:
    import glob
    import importlib.util
    import os
    import types

    if "_speexdsp" in sys.modules:
        return
    so_path = None
    for entry in sys.path:
        if not entry:
            continue
        matches = glob.glob(os.path.join(entry, "speexdsp", "_speexdsp*.so"))
        if matches:
            so_path = matches[0]
            break
    if so_path is None:
        return
    spec = importlib.util.spec_from_file_location("_speexdsp", so_path)
    if spec is None or spec.loader is None:
        return
    ext = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ext)
    sys.modules["_speexdsp"] = ext

    imp_stub = sys.modules.setdefault("imp", types.ModuleType("imp"))

    def _find_module_raises(*_args, **_kwargs):
        raise ImportError("py3.12 imp shim — falling back to pre-loaded _speexdsp")

    imp_stub.find_module = _find_module_raises
    imp_stub.load_module = _find_module_raises


_install_speexdsp_py312_shim()

try:
    from speexdsp import EchoCanceller  # type: ignore
    SPEEX_AVAILABLE = True
    SPEEX_IMPORT_ERROR = None
except ImportError as exc:
    SPEEX_AVAILABLE = False
    SPEEX_IMPORT_ERROR = exc
    EchoCanceller = None  # type: ignore


# ── Helpers ──────────────────────────────────────────────────────────
def load_wav_as_int16_mono_sr(path: Path, target_sr: int) -> np.ndarray:
    """Read a WAV, downmix to mono, resample to target_sr, return int16."""
    with wave.open(str(path), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sw != 2:
        raise ValueError(f"{path.name}: sample width {sw}*8 bits not supported, expected 16-bit")
    pcm = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int16)
    if sr != target_sr:
        if not HAS_SCIPY:
            raise RuntimeError(
                f"WAV is {sr} Hz, need {target_sr} Hz, and scipy is missing for resampling. "
                "Install scipy or re-encode the WAV."
            )
        # resample_poly works on float, normalize-then-renormalize.
        f = pcm.astype(np.float32) / 32768.0
        f = resample_poly(f, target_sr, sr)
        pcm = np.clip(f * 32768.0, -32768, 32767).astype(np.int16)
    return pcm


def save_wav_int16_mono(path: Path, pcm: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.astype(np.int16).tobytes())


def generate_chirp_pcm(seconds: float = 3.0, f0: float = 100.0, f1: float = 8000.0) -> np.ndarray:
    """Linear sine sweep, useful as a synthetic test signal when no
    real-speech WAV is available. Has energy across the whole band
    so the APM's filter adaptation has plenty to lock onto.
    """
    t = np.linspace(0.0, seconds, int(SR * seconds), endpoint=False, dtype=np.float64)
    k = (f1 - f0) / seconds
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    s = 0.5 * np.sin(phase)
    return (s * 32767.0).astype(np.int16)


def estimate_delay_samples(mic: np.ndarray, ref: np.ndarray, max_lag_s: float = 2.0) -> int:
    """How many samples the mic lags behind the reference.

    Pepper's chest-speaker echo path has substantial non-linear
    distortion and reverb; raw-sample cross-correlation picks up
    spurious noise peaks because most of the mic energy is NOT a
    linear copy of the reference. Comparing slow envelopes (signal
    loudness over time) is much more robust — speech bursts in both
    streams line up at the same place even when individual samples
    don't.

    Searches a wide window because NAOqi's audio-output buffer
    typically adds ~500–1500 ms on top of the trivial speaker→mic
    propagation delay.
    """
    if not HAS_SCIPY:
        return 0
    from scipy.signal import hilbert, butter, sosfiltfilt
    n = min(len(mic), len(ref))
    if n < SR:
        return 0
    # |hilbert|, low-pass to ~50 Hz: extracts the speech-energy envelope.
    sos = butter(4, 50, fs=SR, output="sos")
    env_m = sosfiltfilt(sos, np.abs(hilbert(mic[:n].astype(np.float32))))
    env_r = sosfiltfilt(sos, np.abs(hilbert(ref[:n].astype(np.float32))))
    env_m -= env_m.mean()
    env_r -= env_r.mean()
    corr = correlate(env_m, env_r, mode="full")
    center = len(corr) // 2
    max_lag = int(SR * max_lag_s)
    window = corr[center : center + max_lag + 1]  # only positive lags
    delay = int(np.argmax(window))
    return delay


def db_rms(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    f = x.astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(f * f) + 1e-20)
    return 20.0 * np.log10(rms + 1e-20)


def compute_erle(mic_raw: np.ndarray, mic_proc: np.ndarray, ref: np.ndarray) -> dict:
    """Compute ERLE only over the region where the reference (speaker)
    is loud — otherwise we'd be measuring noise floor reduction, not
    echo cancellation.

    Returns dict of measurements.
    """
    n = min(len(mic_raw), len(mic_proc), len(ref))
    mic_raw = mic_raw[:n]
    mic_proc = mic_proc[:n]
    ref = ref[:n]

    # Reference envelope (abs value over 100 ms windows).
    win = max(1, SR // 10)
    ref_env = np.abs(ref.astype(np.float32))
    if n >= win:
        kernel = np.ones(win, dtype=np.float32) / win
        ref_env = np.convolve(ref_env, kernel, mode="same")
    peak = float(ref_env.max())
    if peak < 1.0:
        return {
            "erle_db": 0.0,
            "raw_db": db_rms(mic_raw),
            "proc_db": db_rms(mic_proc),
            "ref_db": db_rms(ref),
            "active_fraction": 0.0,
            "note": "reference signal was silent; ERLE not meaningful",
        }
    thresh = peak * ERLE_REF_LEVEL_FRAC
    mask = ref_env > thresh
    active_fraction = float(mask.sum()) / n

    raw_db = db_rms(mic_raw[mask])
    proc_db = db_rms(mic_proc[mask])
    return {
        "erle_db": raw_db - proc_db,
        "raw_db": raw_db,
        "proc_db": proc_db,
        "ref_db": db_rms(ref[mask]),
        "active_fraction": active_fraction,
    }


# ── Robot-bridge sender ─────────────────────────────────────────────
def stream_to_robot_bridge(pcm: np.ndarray, sock: socket.socket, on_progress=None) -> None:
    """Pace PCM into the robot bridge socket the same way audio_bridge
    does (`<4-byte BE length><PCM>`). Real-time pacing so NAOqi's
    queue doesn't back up.
    """
    bytes_per_chunk = SR * CHUNK_MS // 1000 * SAMP_WIDTH
    raw = pcm.astype(np.int16).tobytes()
    sent_total = 0
    n = len(raw)
    i = 0
    chunk_idx = 0
    while i < n:
        chunk = raw[i : i + bytes_per_chunk]
        header = len(chunk).to_bytes(4, "big")
        sock.sendall(header + chunk)
        sent_total += len(chunk)
        chunk_idx += 1
        if on_progress is not None and chunk_idx % 20 == 0:
            on_progress(sent_total, n)
        i += bytes_per_chunk
        time.sleep(SLEEP_BETWEEN_CHUNKS_S)


# ── Mic capture ─────────────────────────────────────────────────────
class MicCapture:
    """Background sounddevice InputStream that appends int16 mono
    samples into a thread-safe list. Stop with `.stop()`.

    Captures at the device's NATIVE sample rate (most USB mics only
    accept one rate) and the device's preferred channel count, then
    downmixes to mono and resamples to `SR` in `.stop()`. That way
    ALSA never has to deal with rate / channel conversion."""

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._native_sr: int = SR
        self._native_channels: int = CHANNELS

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"  [mic] status={status}", flush=True)
        # indata is float32 by default; downmix to mono in-callback so
        # the chunk list stays mono no matter how many channels the
        # device delivers.
        if indata.ndim > 1 and indata.shape[1] > 1:
            samples = indata.mean(axis=1)
        elif indata.ndim > 1:
            samples = indata[:, 0]
        else:
            samples = indata
        samples_i16 = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)
        with self._lock:
            self._chunks.append(samples_i16.copy())

    def start(self) -> None:
        device = MIC_DEVICE
        # Probe the device to find what rates/channels it actually
        # accepts. USB Audio Class devices usually expose exactly one
        # native rate (DJI MIC MINI = 48 kHz) and reject anything else
        # at the hardware level.
        try:
            info = sd.query_devices(device, "input") if device is not None \
                else sd.query_devices(kind="input")
            self._native_sr = int(info.get("default_samplerate", SR))
            self._native_channels = int(info.get("max_input_channels", CHANNELS))
        except Exception:
            # Fall back to defaults; the InputStream open below will
            # surface a clearer error if these are wrong.
            self._native_sr = SR
            self._native_channels = CHANNELS

        kwargs: dict = dict(
            samplerate=self._native_sr,
            channels=self._native_channels,
            dtype="float32",
            callback=self._callback,
            blocksize=self._native_sr // 20,  # 50 ms blocks
        )
        if device is not None:
            kwargs["device"] = device
        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
        except Exception as exc:
            # Dump the FULL device list so the user can see exactly what
            # PortAudio found. Common causes when this returns 0 devices:
            #   - production user-client container has the mic exclusively
            #     (stop it: `docker compose -f docker/docker-compose.experiment.yml
            #     stop user-client`)
            #   - user not in `audio` group → can't read /dev/snd/*
            #   - no mic plugged in (check `arecord -l`)
            print(f"\n[probe] ERROR opening mic: {exc!r}", flush=True)
            try:
                devs = sd.query_devices()
                total = len(devs) if hasattr(devs, "__len__") else 0
                inputs = [
                    (i, d) for i, d in enumerate(devs)
                    if d.get("max_input_channels", 0) > 0
                ]
                print(
                    f"[probe] PortAudio sees {total} device(s), "
                    f"{len(inputs)} with input channels:",
                    flush=True,
                )
                if not inputs:
                    print(
                        "  (none — no usable input devices on this host)",
                        flush=True,
                    )
                else:
                    for i, d in inputs:
                        print(
                            f"  [{i}] {d.get('name')!r}  "
                            f"in_ch={d.get('max_input_channels')} "
                            f"sr={d.get('default_samplerate')}",
                            flush=True,
                        )
                if total > 0 and not inputs:
                    print("[probe] (output-only devices, suppressed)", flush=True)
            except Exception as exc2:
                print(f"[probe] could not list devices: {exc2!r}", flush=True)
            print(
                "[probe] Diagnose with:\n"
                "    arecord -l\n"
                "    groups | grep audio\n"
                "    docker compose -f docker/docker-compose.experiment.yml ps user-client\n"
                "  If user-client is up, stop it first:\n"
                "    docker compose -f docker/docker-compose.experiment.yml stop user-client\n"
                "  Or pin the mic explicitly:\n"
                "    MIC_DEVICE=2 uv run python ...\n"
                "    MIC_DEVICE='USB' uv run python ...",
                flush=True,
            )
            raise
        print(
            f"[probe] mic_capture_started device={device!r} "
            f"native_sr={self._native_sr} native_channels={self._native_channels} "
            f"(will downmix to mono and resample to {SR} Hz on stop)",
            flush=True,
        )

    def stop(self) -> np.ndarray:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            if not self._chunks:
                return np.zeros(0, dtype=np.int16)
            pcm = np.concatenate(self._chunks)
        # Resample from native_sr down to SR (e.g., 48 kHz -> 16 kHz)
        # so the rest of the pipeline can assume a single sample rate.
        if self._native_sr == SR:
            return pcm
        if not HAS_SCIPY:
            raise RuntimeError(
                f"Mic captured at {self._native_sr} Hz, need {SR} Hz, "
                f"and scipy is missing for resampling. "
                f"Install scipy or set MIC_DEVICE to a device that supports {SR} Hz natively."
            )
        f = pcm.astype(np.float32) / 32768.0
        f = resample_poly(f, SR, self._native_sr)
        return np.clip(f * 32768.0, -32768, 32767).astype(np.int16)


# ── AEC ─────────────────────────────────────────────────────────────
# Speex AEC filter length (in samples). This is the tail length the
# adaptive filter can cancel — i.e. the impulse-response length it
# can model. Once the bulk NAOqi-buffer latency is taken out by
# pre-alignment, only the residual delay + speaker/room reverb
# remain — ~400 ms is plenty. Going larger costs convergence speed
# without buying noticeable ERLE on this hardware.
SPEEX_FILTER_MS = 384
SPEEX_FILTER_SAMPLES = SR * SPEEX_FILTER_MS // 1000


def run_speex_aec(mic: np.ndarray, ref: np.ndarray) -> np.ndarray:
    """Push mic + reference through SpeexDSP's echo canceller frame
    by frame. SpeexDSP's API is simpler than WebRTC APM — no separate
    stream-delay hint, the adaptive filter just converges on whatever
    delay it sees.

    Frames must be exactly APM_FRAME_SAMPLES (10 ms at 16 kHz). We
    zero-pad both signals to a common multiple of that.
    """
    if not SPEEX_AVAILABLE or EchoCanceller is None:
        raise RuntimeError(
            f"speexdsp not importable: {SPEEX_IMPORT_ERROR!r}\n"
            "Install on the RPi:\n"
            "  sudo apt install libspeexdsp-dev\n"
            "  uv pip install speexdsp"
        )

    n = max(len(mic), len(ref))
    n = ((n + APM_FRAME_SAMPLES - 1) // APM_FRAME_SAMPLES) * APM_FRAME_SAMPLES
    mic_p = np.zeros(n, dtype=np.int16)
    ref_p = np.zeros(n, dtype=np.int16)
    mic_p[: len(mic)] = mic
    ref_p[: len(ref)] = ref

    # speexdsp-python signature: EchoCanceller.create(frame_size,
    # filter_length, sample_rate). All in samples / Hz.
    ec = EchoCanceller.create(APM_FRAME_SAMPLES, SPEEX_FILTER_SAMPLES, SR)

    out = np.zeros(n, dtype=np.int16)
    for i in range(0, n, APM_FRAME_SAMPLES):
        near_bytes = mic_p[i : i + APM_FRAME_SAMPLES].tobytes()
        far_bytes = ref_p[i : i + APM_FRAME_SAMPLES].tobytes()
        cleaned = ec.process(near_bytes, far_bytes)
        out[i : i + APM_FRAME_SAMPLES] = np.frombuffer(cleaned, dtype=np.int16)
    return out


# ── Main ────────────────────────────────────────────────────────────
def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Load or synthesize the reference.
    if DEFAULT_WAV.exists():
        try:
            ref_pcm = load_wav_as_int16_mono_sr(DEFAULT_WAV, SR)
            print(
                f"[probe] reference_loaded path={DEFAULT_WAV.name} "
                f"duration_s={len(ref_pcm) / SR:.2f}",
                flush=True,
            )
        except Exception as exc:
            print(f"[probe] failed to load {DEFAULT_WAV}: {exc!r}", flush=True)
            return 2
    else:
        ref_pcm = generate_chirp_pcm(seconds=3.0)
        print(
            f"[probe] no input WAV found, using synthetic chirp duration_s={len(ref_pcm) / SR:.2f}",
            flush=True,
        )

    # 2. Connect to robot bridge.
    print(f"[probe] connecting to robot bridge {TCP_HOST}:{TCP_PORT}", flush=True)
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5.0)
        sock.connect((TCP_HOST, TCP_PORT))
        sock.settimeout(None)
    except Exception as exc:
        print(
            f"[probe] ERROR cannot connect to robot bridge: {exc!r}\n"
            "  - Is Pepper on?\n"
            "  - Is production audio-bridge stopped? "
            "(`docker compose -f docker/docker-compose.experiment.yml stop audio-bridge`)\n"
            f"  - Is {TCP_HOST}:{TCP_PORT} the right host/port?",
            flush=True,
        )
        return 3

    # 3. Start mic capture.
    mic = MicCapture()
    mic.start()

    # 4. Stream reference to robot bridge with pacing.
    t_send_start = time.monotonic()
    try:
        def _progress(sent, total):
            print(
                f"  [probe] tx_progress {sent / max(1, total) * 100.0:.0f}%",
                flush=True,
            )
        stream_to_robot_bridge(ref_pcm, sock, on_progress=_progress)
    except Exception as exc:
        print(f"[probe] ERROR streaming to bridge: {exc!r}", flush=True)
        mic.stop()
        sock.close()
        return 4
    t_send_end = time.monotonic()
    print(
        f"[probe] playback_done tx_wallclock_s={t_send_end - t_send_start:.2f} "
        f"ref_duration_s={len(ref_pcm) / SR:.2f}",
        flush=True,
    )

    # 5. Wait for tail, then stop mic.
    time.sleep(TAIL_S)
    mic_pcm = mic.stop()
    sock.close()
    print(f"[probe] mic_captured samples={len(mic_pcm)} duration_s={len(mic_pcm) / SR:.2f}", flush=True)

    # 6. Save the raw streams unconditionally — even if APM is missing.
    raw_path = OUTPUT_DIR / "mic_raw.wav"
    ref_path = OUTPUT_DIR / "ref.wav"
    save_wav_int16_mono(raw_path, mic_pcm, SR)
    save_wav_int16_mono(ref_path, ref_pcm, SR)
    print(f"[probe] wrote {raw_path}", flush=True)
    print(f"[probe] wrote {ref_path}", flush=True)

    # 7. Align mic against reference. Pepper's NAOqi audio buffer
    # adds ~1 s of latency on top of trivial speaker→mic propagation;
    # without alignment a sub-second SpeexDSP filter can't span it
    # and ERLE collapses to zero. Roll the ref forward so it lines
    # up with the mic; subtract PREALIGN_SAFETY_MS to stay on the
    # causal side of the alignment (the filter handles "ref leads
    # mic" but not the reverse).
    raw_delay_samples = estimate_delay_samples(mic_pcm, ref_pcm)
    safety_samples = PREALIGN_SAFETY_MS * SR // 1000
    delay_samples = max(0, raw_delay_samples - safety_samples)
    print(
        f"[probe] envelope_delay samples={raw_delay_samples} ms={raw_delay_samples * 1000 // SR} "
        f"-> prealign samples={delay_samples} ms={delay_samples * 1000 // SR} "
        f"(safety={PREALIGN_SAFETY_MS}ms)",
        flush=True,
    )

    # Pad reference so its sample n+delay aligns with mic sample n.
    aligned_ref = np.concatenate([
        np.zeros(delay_samples, dtype=np.int16),
        ref_pcm,
    ])

    # 8. Run SpeexDSP AEC.
    if not SPEEX_AVAILABLE:
        print(
            "\n[probe] speexdsp is not installed.\n"
            "  Raw WAVs were saved; the playback+capture loop works.\n"
            "  To complete Phase 1 on the RPi:\n"
            "    sudo apt install libspeexdsp-dev\n"
            "    uv pip install speexdsp\n"
            "  Then re-run this script.",
            flush=True,
        )
        return 5

    try:
        mic_aec = run_speex_aec(mic_pcm, aligned_ref)
    except Exception as exc:
        print(f"[probe] ERROR running Speex AEC: {exc!r}", flush=True)
        return 6

    aec_path = OUTPUT_DIR / "mic_aec.wav"
    save_wav_int16_mono(aec_path, mic_aec, SR)
    print(f"[probe] wrote {aec_path}", flush=True)

    # 9. Score it.
    measurements = compute_erle(mic_pcm, mic_aec, aligned_ref)
    print("\n[probe] ── results ──")
    print(f"  raw mic RMS    : {measurements['raw_db']:.1f} dBFS")
    print(f"  AEC mic RMS    : {measurements['proc_db']:.1f} dBFS")
    print(f"  reference RMS  : {measurements['ref_db']:.1f} dBFS")
    print(f"  ERLE           : {measurements['erle_db']:.1f} dB  "
          f"(target >= 15; great is 20-30)")
    print(f"  active region  : {measurements['active_fraction'] * 100.0:.0f}% of capture")
    if measurements.get("note"):
        print(f"  NOTE: {measurements['note']}")

    verdict = "PASS" if measurements["erle_db"] >= 15.0 else "FAIL"
    print(f"\n[probe] verdict: {verdict}")
    print("[probe] listen to outputs/mic_raw.wav vs outputs/mic_aec.wav to confirm by ear.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
