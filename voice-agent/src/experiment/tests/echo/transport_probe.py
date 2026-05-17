"""Transport-latency probe for Pepper's chest-speaker.

What this script does:

  Plays the SAME reference WAV through Pepper's chest speaker via
  several different NAOqi audio transports and measures end-to-end
  latency by capturing the USB mic in parallel and locating a sharp
  4 kHz click prepended to the audio.

Methods compared (all bypass the production bridge / audio-bridge —
this test connects to Pepper directly via `qi.Session`):

  1. qi_send_remote_buffer   `ALAudioDevice.sendRemoteBufferToOutput`
                             called from a tight in-process loop, no
                             TCP hop. Same NAOqi API as production.
  2. qi_play_file            `ALAudioPlayer.playFile` on a WAV
                             scp'd into /tmp on Pepper first.
  3. qi_play_web_stream      `ALAudioPlayer.playWebStream` against a
                             tiny http.server running on the RPi.

For each method we write:
  outputs/transport_<method>_mic.wav   captured mic
  outputs/transport_<method>_ref.wav   reference fed to the speaker
And we print a per-method one-way latency in ms (click_send_call ->
click_audible_at_mic).

How to run, on the RPi. Production must be stopped so two qi
clients don't fight over the audio device AND user-client doesn't
hold the USB mic open:

    docker compose -f docker/docker-compose.yml stop bridge audio-bridge user-client
    uv run python voice-agent/src/experiment/tests/echo/transport_probe.py

Drop your own test WAV at:
    voice-agent/src/experiment/tests/echo/inputs/test_phrase.wav

When done, restart production:
    docker compose -f docker/docker-compose.yml start bridge audio-bridge user-client
"""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np


# ── qi-python environment auto-bootstrap ────────────────────────────
# qi-python is built outside the system Python path. The bridge
# container points PYTHONPATH/LD_LIBRARY_PATH at these directories;
# we replicate that here so the script "just works" via `uv run`.
QI_PYTHONPATH = "/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release"
QI_LD_LIBRARY_PATH = (
    "/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib"
    ":/home/lucas/Projects/FEL/QI_test/local_qi/lib"
)
_REEXEC_MARKER = "_TRANSPORT_PROBE_REEXEC_DONE"


def _ensure_qi_env() -> None:
    if os.environ.get(_REEXEC_MARKER):
        return
    try:
        import qi  # noqa: F401
        os.environ[_REEXEC_MARKER] = "1"
        return
    except Exception:
        pass
    new_env = dict(os.environ)
    pp = new_env.get("PYTHONPATH", "")
    new_env["PYTHONPATH"] = QI_PYTHONPATH + (":" + pp if pp else "")
    ld = new_env.get("LD_LIBRARY_PATH", "")
    new_env["LD_LIBRARY_PATH"] = QI_LD_LIBRARY_PATH + (":" + ld if ld else "")
    new_env[_REEXEC_MARKER] = "1"
    print(
        f"[probe] re-exec with qi env "
        f"(PYTHONPATH+={QI_PYTHONPATH}, LD_LIBRARY_PATH+={QI_LD_LIBRARY_PATH})",
        flush=True,
    )
    os.execvpe(sys.executable, [sys.executable, __file__] + sys.argv[1:], new_env)


_ensure_qi_env()
import qi  # noqa: E402  (only importable after env bootstrap)


# ── Paths ────────────────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
INPUT_DIR = THIS_DIR / "inputs"
OUTPUT_DIR = THIS_DIR / "outputs"
DEFAULT_WAV = INPUT_DIR / "test_phrase.wav"


# ── Tunables (env-overridable; defaults match production) ────────────
# Pepper qi endpoint. Override with PEPPER_QI_URL if needed.
PEPPER_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://10.42.0.205:9559")

# Sample rate the test runs at — matches production PEPPER_STREAM_RATE.
SR = 16000
CHANNELS = 1
SAMP_WIDTH = 2  # int16

# Mic capture. Same env-var pattern as `aec_offline_probe.py`.
MIC_DEVICE_RAW = os.environ.get("MIC_DEVICE") or os.environ.get("USER_MIC_DEVICE")
if MIC_DEVICE_RAW is not None and MIC_DEVICE_RAW.strip().lstrip("-").isdigit():
    MIC_DEVICE: int | str | None = int(MIC_DEVICE_RAW)
elif MIC_DEVICE_RAW:
    MIC_DEVICE = MIC_DEVICE_RAW
else:
    MIC_DEVICE = None

# Pacing for `qi_send_remote_buffer`. Matches the bridge's defaults:
# 1600 frames @ 16 kHz = 100 ms per batch; sleep slightly less than
# one batch duration so NAOqi's internal queue stays gently full
# without growing.
BATCH_FRAMES = 1600
SLEEP_BETWEEN_BATCHES_S = 0.090

# Click prepended to the reference for unambiguous t=0 detection.
# A loud, narrow-band tone burst has a sharp envelope edge that
# cross-correlates cleanly against the same template in the mic
# capture, independent of the speech content that follows.
CLICK_FREQ_HZ = 4000.0
CLICK_DURATION_MS = 30
CLICK_GAP_MS = 70           # silence between click and main audio
CLICK_AMPLITUDE = 0.85      # full-scale fraction

# Mic tail after the send finishes. Needs to cover NAOqi's playback
# buffer (~1 s on Pepper) plus reverb. 3 s is comfortable for a few
# seconds of WAV; bump if your test WAV is short.
TAIL_S = 3.0

# Pause between methods so previous playback fully drains before the
# next mic capture starts. Larger than TAIL_S for safety.
INTER_METHOD_PAUSE_S = 1.5

# Methods to run. Edit this list to skip a method, or to run only one.
# Each entry must match a `method_*` function below. `qi_play_web_stream`
# is omitted by default because NAOqi 2.5's `playWebStream` does not
# actually HTTP-fetch — it falls through to a local-file open and fails.
METHODS = [
    "qi_send_remote_buffer",
    "qi_play_file",
    "ssh_paplay",
]

# SSH config for `qi_play_file`. Default Pepper user is `nao`.
PEPPER_SSH_USER = os.environ.get("PEPPER_SSH_USER", "nao")
PEPPER_SSH_HOST = os.environ.get(
    "PEPPER_SSH_HOST",
    PEPPER_QI_URL.split("//", 1)[-1].split(":", 1)[0],
)
PEPPER_REMOTE_PATH = "/tmp/transport_probe.wav"
# If you have sshpass installed and want password auth, set this to
# the password. Otherwise we rely on your existing SSH key.
PEPPER_SSH_PASSWORD = "Argus"

# HTTP server port for `qi_play_web_stream`. Picked above the usual
# user-space range so no other dev service collides with it.
WEBSTREAM_PORT = int(os.environ.get("WEBSTREAM_PORT", "18080"))


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


# ── WAV helpers ─────────────────────────────────────────────────────
def load_wav_as_int16_mono_sr(path: Path, target_sr: int) -> np.ndarray:
    with wave.open(str(path), "rb") as wf:
        nch = wf.getnchannels()
        sw = wf.getsampwidth()
        sr = wf.getframerate()
        nframes = wf.getnframes()
        raw = wf.readframes(nframes)
    if sw != 2:
        raise ValueError(f"{path.name}: sample width {sw}*8 bits not supported")
    pcm = np.frombuffer(raw, dtype=np.int16)
    if nch > 1:
        pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int16)
    if sr != target_sr:
        if not HAS_SCIPY:
            raise RuntimeError(
                f"WAV is {sr} Hz, need {target_sr} Hz, scipy missing"
            )
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
    t = np.linspace(0.0, seconds, int(SR * seconds), endpoint=False, dtype=np.float64)
    k = (f1 - f0) / seconds
    phase = 2 * np.pi * (f0 * t + 0.5 * k * t * t)
    s = 0.5 * np.sin(phase)
    return (s * 32767.0).astype(np.int16)


def make_click_template() -> np.ndarray:
    """Narrow 4 kHz tone burst with a quick raised-cosine envelope.

    The envelope avoids spectral splatter at the edges, which would
    smear the cross-correlation peak and degrade timing accuracy.
    """
    n = SR * CLICK_DURATION_MS // 1000
    t = np.arange(n, dtype=np.float64) / SR
    s = np.sin(2 * np.pi * CLICK_FREQ_HZ * t)
    # 5 ms raised-cosine ramp at each end.
    ramp = SR * 5 // 1000
    if ramp * 2 < n:
        win = np.ones(n, dtype=np.float64)
        ri = np.arange(ramp, dtype=np.float64)
        rc = 0.5 - 0.5 * np.cos(np.pi * ri / ramp)
        win[:ramp] = rc
        win[-ramp:] = rc[::-1]
        s = s * win
    s = s * CLICK_AMPLITUDE
    return (s * 32767.0).astype(np.int16)


def prepend_click(audio_pcm: np.ndarray) -> tuple[np.ndarray, int]:
    """Return (click+silence+audio, click_start_sample_in_ref)."""
    click = make_click_template()
    gap = np.zeros(SR * CLICK_GAP_MS // 1000, dtype=np.int16)
    out = np.concatenate([click, gap, audio_pcm.astype(np.int16)])
    return out, 0  # click starts at sample 0 of the reference


def mono16_to_stereo16_bytes(mono_pcm: np.ndarray) -> bytes:
    """Duplicate the mono channel to interleaved stereo int16 bytes
    (Pepper expects 2-channel int16 for sendRemoteBufferToOutput)."""
    mono = mono_pcm.astype(np.int16)
    stereo = np.empty(mono.size * 2, dtype=np.int16)
    stereo[0::2] = mono
    stereo[1::2] = mono
    return stereo.tobytes()


# ── Click localization ──────────────────────────────────────────────
CLICK_DETECT_THRESHOLD = 0.08


def find_click_sample(mic_pcm: np.ndarray) -> tuple[int, float]:
    """Locate the click in the mic capture via normalized cross-correlation
    against the same template that was prepended to the reference.

    Searches the entire mic buffer (older versions only looked at the
    first ~3 s, which missed `playFile` runs where the qi call returns
    before audio actually starts).

    Returns `(peak_sample_idx, peak_normalized_correlation)`. The caller
    decides whether the correlation is strong enough. A value below
    `CLICK_DETECT_THRESHOLD` (~0.08) typically means the click did not
    arrive — either it was filtered out by GStreamer / PA resampling,
    or the audio never actually played.
    """
    if not HAS_SCIPY:
        # Fallback: peak of |signal| over the entire capture — less
        # robust against loud speech, but better than nothing.
        if len(mic_pcm) == 0:
            return -1, 0.0
        return int(np.argmax(np.abs(mic_pcm))), 0.0

    template = make_click_template().astype(np.float32)
    template -= template.mean()
    tmpl_norm = np.linalg.norm(template) + 1e-9

    if len(mic_pcm) < len(template):
        return -1, 0.0
    haystack = mic_pcm.astype(np.float32)
    haystack -= haystack.mean()

    corr = correlate(haystack, template, mode="valid")
    haystack_sq = haystack * haystack
    window_energy = np.convolve(
        haystack_sq, np.ones(len(template), dtype=np.float32), mode="valid"
    )
    window_norm = np.sqrt(window_energy) + 1e-9
    norm_corr = corr / (window_norm * tmpl_norm)

    peak_idx = int(np.argmax(np.abs(norm_corr)))
    peak_val = float(np.abs(norm_corr[peak_idx]))
    return peak_idx, peak_val


# ── Mic capture ─────────────────────────────────────────────────────
class MicCapture:
    """Thread-safe background capture (mirrors aec_offline_probe.py)."""

    def __init__(self) -> None:
        self._chunks: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._stream: sd.InputStream | None = None
        self._native_sr: int = SR
        self._native_channels: int = CHANNELS
        self.capture_start_monotonic: float = 0.0

    def _callback(self, indata, frames, time_info, status):
        if status:
            print(f"  [mic] status={status}", flush=True)
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
        try:
            info = sd.query_devices(device, "input") if device is not None \
                else sd.query_devices(kind="input")
            self._native_sr = int(info.get("default_samplerate", SR))
            self._native_channels = int(info.get("max_input_channels", CHANNELS))
        except Exception:
            self._native_sr = SR
            self._native_channels = CHANNELS

        kwargs: dict = dict(
            samplerate=self._native_sr,
            channels=self._native_channels,
            dtype="float32",
            callback=self._callback,
            blocksize=self._native_sr // 20,
        )
        if device is not None:
            kwargs["device"] = device
        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
        except Exception as exc:
            # The user-client container holds the USB mic exclusively
            # in production. Dump the device list so Lucas can see
            # what PortAudio actually found.
            print(f"\n[probe] ERROR opening mic: {exc!r}", flush=True)
            try:
                devs = sd.query_devices()
                inputs = [
                    (i, d) for i, d in enumerate(devs)
                    if d.get("max_input_channels", 0) > 0
                ]
                print(f"[probe] PortAudio inputs ({len(inputs)}):", flush=True)
                for i, d in inputs:
                    print(
                        f"  [{i}] {d.get('name')!r}  "
                        f"in_ch={d.get('max_input_channels')} "
                        f"sr={d.get('default_samplerate')}",
                        flush=True,
                    )
            except Exception:
                pass
            print(
                "[probe] Stop user-client first:\n"
                "    docker compose -f docker/docker-compose.yml stop user-client\n"
                "  Or pin the mic explicitly: MIC_DEVICE=2 uv run python ...",
                flush=True,
            )
            raise
        self.capture_start_monotonic = time.monotonic()
        print(
            f"[probe] mic_capture_started device={device!r} "
            f"native_sr={self._native_sr} ch={self._native_channels}",
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
        if self._native_sr == SR:
            return pcm
        if not HAS_SCIPY:
            raise RuntimeError(
                f"Mic at {self._native_sr} Hz, need {SR} Hz, scipy missing"
            )
        f = pcm.astype(np.float32) / 32768.0
        f = resample_poly(f, SR, self._native_sr)
        return np.clip(f * 32768.0, -32768, 32767).astype(np.int16)


# ── HTTP server for playWebStream ───────────────────────────────────
def get_local_ip_to_pepper(pepper_host: str) -> str:
    """Local IP that Pepper can reach back. Opens a UDP socket toward
    Pepper and reads back the kernel-chosen source address — works
    regardless of how many interfaces the RPi has."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((pepper_host, 9559))
        return s.getsockname()[0]
    finally:
        s.close()


class _QuietHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """`SimpleHTTPRequestHandler` rooted at a chosen directory plus
    suppressed access-log spam (one line per range request is noisy)."""

    serve_root: Path = Path("/tmp")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.serve_root), **kwargs)

    def log_message(self, fmt, *args):
        print(f"  [http] {self.address_string()} {fmt % args}", flush=True)


class WebstreamServer:
    """Tiny threaded HTTP server that serves a single WAV file."""

    def __init__(self, serve_dir: Path, port: int):
        self._serve_dir = serve_dir
        self._port = port
        self._httpd: socketserver.TCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type(
            "_BoundHandler",
            (_QuietHTTPRequestHandler,),
            {"serve_root": self._serve_dir},
        )
        # ThreadingTCPServer so a slow GET doesn't block subsequent ones;
        # SO_REUSEADDR so we can rerun the script without TIME_WAIT pain.
        socketserver.TCPServer.allow_reuse_address = True
        self._httpd = socketserver.ThreadingTCPServer(("0.0.0.0", self._port), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        print(
            f"[probe] http_server_started port={self._port} dir={self._serve_dir}",
            flush=True,
        )

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        self._thread = None


# ── SCP helper ──────────────────────────────────────────────────────
def scp_to_pepper(local_path: Path, remote_path: str) -> None:
    """Push `local_path` to `nao@<pepper>:remote_path`. Tries sshpass
    if a password is configured, falls back to bare scp (key auth).
    """
    target = f"{PEPPER_SSH_USER}@{PEPPER_SSH_HOST}:{remote_path}"
    ssh_opts = ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    if PEPPER_SSH_PASSWORD:
        cmd = ["sshpass", "-p", PEPPER_SSH_PASSWORD, "scp", *ssh_opts, str(local_path), target]
    else:
        cmd = ["scp", *ssh_opts, str(local_path), target]
    print(f"[probe] scp {local_path.name} -> {target}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"scp failed (rc={proc.returncode})\nstderr: {proc.stderr.strip()}\n"
            f"hint: configure SSH keys for {PEPPER_SSH_USER}@{PEPPER_SSH_HOST}, "
            f"or set PEPPER_SSH_PASSWORD env var (requires sshpass)."
        )


# ── Per-method runners ──────────────────────────────────────────────
# Each runner takes (services_bag, ref_pcm) and returns
# t_send_call_monotonic — the moment audio was "submitted" to NAOqi.
# Mic capture is started by the caller BEFORE the runner.
def method_qi_send_remote_buffer(services, ref_pcm: np.ndarray) -> float:
    """Direct `sendRemoteBufferToOutput` from a tight loop. No bridge,
    no TCP. Same NAOqi API and pacing as production, minus every
    layer between us and qi.
    """
    audio = services["ALAudioDevice"]
    # Real-time pacing: feed BATCH_FRAMES at a time, sleep slightly
    # less than batch duration so the NAOqi queue gets fed without
    # backing up.
    stereo = mono16_to_stereo16_bytes(ref_pcm)
    bytes_per_frame = 4  # int16 stereo
    batch_bytes = BATCH_FRAMES * bytes_per_frame
    total_bytes = len(stereo)

    t_send_call = time.monotonic()
    sent = 0
    batch_idx = 0
    while sent < total_bytes:
        payload = stereo[sent : sent + batch_bytes]
        nb_frames = len(payload) // bytes_per_frame
        audio.sendRemoteBufferToOutput(nb_frames, payload)
        sent += len(payload)
        batch_idx += 1
        if batch_idx % 10 == 0:
            print(
                f"  [send_remote] tx_progress {sent * 100 // total_bytes}%",
                flush=True,
            )
        time.sleep(SLEEP_BETWEEN_BATCHES_S)
    print(
        f"  [send_remote] tx_done batches={batch_idx} bytes={total_bytes}",
        flush=True,
    )
    return t_send_call


def method_qi_play_file(services, ref_pcm: np.ndarray) -> float:
    """Upload a one-shot WAV to Pepper, then `ALAudioPlayer.playFile`.

    `playFile` is synchronous (blocks until playback ends). The qi
    call goes Pepper-side through GStreamer; this measures the
    GStreamer-decode + ALSA path, which is a different code path
    than `sendRemoteBufferToOutput`.
    """
    audio_player = services["ALAudioPlayer"]
    # 1) Write the ref to a local file (with click prefix already).
    local_tmp = OUTPUT_DIR / "_remote_upload.wav"
    save_wav_int16_mono(local_tmp, ref_pcm, SR)
    # 2) scp it to Pepper.
    scp_to_pepper(local_tmp, PEPPER_REMOTE_PATH)
    # 3) Trigger playFile. Start the timer at the qi call so the
    #    measured latency includes file-open and decode start.
    print(f"  [play_file] playFile({PEPPER_REMOTE_PATH})", flush=True)
    t_send_call = time.monotonic()
    audio_player.playFile(PEPPER_REMOTE_PATH)
    print(f"  [play_file] playFile returned", flush=True)
    return t_send_call


def method_qi_play_web_stream(services, ref_pcm: np.ndarray) -> float:
    """Serve the WAV over HTTP from the RPi; call `playWebStream`."""
    audio_player = services["ALAudioPlayer"]
    server: WebstreamServer = services["_webstream_server"]
    rpi_ip: str = services["_rpi_ip_for_pepper"]

    # Write the WAV into the server's serve dir so Pepper can GET it.
    wav_name = "transport_probe.wav"
    serve_path = server._serve_dir / wav_name
    save_wav_int16_mono(serve_path, ref_pcm, SR)

    url = f"http://{rpi_ip}:{WEBSTREAM_PORT}/{wav_name}"
    print(f"  [play_web] playWebStream({url})", flush=True)
    t_send_call = time.monotonic()
    audio_player.playWebStream(url, 1.0, 0.0)
    print(f"  [play_web] playWebStream returned", flush=True)
    return t_send_call


def method_ssh_paplay(services, ref_pcm: np.ndarray) -> float:
    """Stream raw PCM into Pepper's PulseAudio daemon via ssh + paplay.

    Bypasses NAOqi's `ALAudioDevice` queue entirely (which is where the
    ~1.3 s buffer lives) by talking to Pepper's underlying PA daemon
    directly. NAOqi keeps running; PA mixes our stream alongside any
    other NAOqi audio. No `closeAudioOutputs()` needed.

    `paplay --raw --latency-msec=30` honours the requested target latency
    and reports ~25 ms total (buffer + sink). Mono int16 @ 16 kHz, same
    as what we'd push to LiveKit.
    """
    paplay_remote_cmd = (
        f"paplay --raw --format=s16le --rate={SR} --channels=1 "
        f"--latency-msec=30"
    )
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
    ]
    target = f"{PEPPER_SSH_USER}@{PEPPER_SSH_HOST}"
    if PEPPER_SSH_PASSWORD:
        ssh_cmd = ["sshpass", "-p", PEPPER_SSH_PASSWORD, "ssh", *ssh_opts, target, paplay_remote_cmd]
    else:
        ssh_cmd = ["ssh", *ssh_opts, target, paplay_remote_cmd]

    print(f"  [ssh_paplay] spawning ssh+paplay", flush=True)
    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"ssh/sshpass not found: {exc!r}\n"
            "Install with: sudo apt install sshpass openssh-client"
        )

    # One-time setup: ssh handshake + paplay sink unsuspend. We want this
    # OUT of the latency measurement because in production this happens
    # once per session, not per utterance. 500 ms is conservative.
    time.sleep(0.5)

    # paplay expects mono int16 LE — feed bytes from the SAME reference
    # WAV (with click prepended) the qi methods use. No mono->stereo
    # conversion: that conversion lives inside NAOqi for sendRemoteBuffer,
    # but PA handles it on its own when needed.
    raw = ref_pcm.astype(np.int16).tobytes()
    bytes_per_batch = BATCH_FRAMES * 2  # mono int16
    total_bytes = len(raw)

    t_send_call = time.monotonic()
    sent = 0
    batch_idx = 0
    broken = False
    try:
        while sent < total_bytes:
            chunk = raw[sent : sent + bytes_per_batch]
            proc.stdin.write(chunk)
            proc.stdin.flush()
            sent += len(chunk)
            batch_idx += 1
            if batch_idx % 10 == 0:
                print(
                    f"  [ssh_paplay] tx_progress {sent * 100 // total_bytes}%",
                    flush=True,
                )
            time.sleep(SLEEP_BETWEEN_BATCHES_S)
    except (BrokenPipeError, OSError) as exc:
        broken = True
        print(f"  [ssh_paplay] pipe broke: {exc!r}", flush=True)

    print(
        f"  [ssh_paplay] tx_done batches={batch_idx} bytes={sent}/{total_bytes}"
        + (" (broken)" if broken else ""),
        flush=True,
    )
    # Stash the proc on `services` so the main loop can drain + clean it
    # up after the mic tail (closing stdin signals EOF; paplay drains
    # its ~25 ms buffer then exits cleanly).
    services["_ssh_paplay_proc"] = proc
    return t_send_call


METHOD_RUNNERS = {
    "qi_send_remote_buffer": method_qi_send_remote_buffer,
    "qi_play_file": method_qi_play_file,
    "qi_play_web_stream": method_qi_play_web_stream,
    "ssh_paplay": method_ssh_paplay,
}


# ── Main ────────────────────────────────────────────────────────────
def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    INPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Load or synthesize the source audio, then prepend the click.
    if DEFAULT_WAV.exists():
        try:
            base_pcm = load_wav_as_int16_mono_sr(DEFAULT_WAV, SR)
            print(
                f"[probe] reference_loaded path={DEFAULT_WAV.name} "
                f"duration_s={len(base_pcm) / SR:.2f}",
                flush=True,
            )
        except Exception as exc:
            print(f"[probe] failed to load {DEFAULT_WAV}: {exc!r}", flush=True)
            return 2
    else:
        base_pcm = generate_chirp_pcm(seconds=3.0)
        print(
            f"[probe] no input WAV — synthesizing chirp "
            f"duration_s={len(base_pcm) / SR:.2f}",
            flush=True,
        )

    ref_pcm, click_sample_in_ref = prepend_click(base_pcm)
    save_wav_int16_mono(OUTPUT_DIR / "transport_ref.wav", ref_pcm, SR)
    print(
        f"[probe] click_prepended click_dur_ms={CLICK_DURATION_MS} "
        f"gap_ms={CLICK_GAP_MS} ref_total_s={len(ref_pcm) / SR:.2f}",
        flush=True,
    )

    # 2. Connect qi.
    print(f"[probe] connecting to Pepper qi {PEPPER_QI_URL}", flush=True)
    sess = qi.Session()
    try:
        sess.connect(PEPPER_QI_URL)
    except Exception as exc:
        print(
            f"[probe] ERROR qi.connect failed: {exc!r}\n"
            "  - Is Pepper reachable on the network?\n"
            "  - Are bridge + audio-bridge containers stopped?",
            flush=True,
        )
        return 3
    print("[probe] qi connected", flush=True)

    services: dict[str, object] = {}
    try:
        services["ALAudioDevice"] = sess.service("ALAudioDevice")
        print("[probe] got ALAudioDevice", flush=True)
    except Exception as exc:
        print(f"[probe] ERROR ALAudioDevice unavailable: {exc!r}", flush=True)
        return 4

    needs_player = "qi_play_file" in METHODS or "qi_play_web_stream" in METHODS
    if needs_player:
        try:
            services["ALAudioPlayer"] = sess.service("ALAudioPlayer")
            print("[probe] got ALAudioPlayer", flush=True)
            # bridge.py mutes ALAudioPlayer during animations; if the
            # bridge container was killed mid-animation in a prior run,
            # the mute state persists on Pepper. Force volume back up
            # so playFile actually emits audio.
            try:
                services["ALAudioPlayer"].setVolume(1.0)
                print("[probe] ALAudioPlayer setVolume(1.0)", flush=True)
            except Exception as exc:
                print(f"[probe] setVolume warning: {exc!r}", flush=True)
        except Exception as exc:
            print(
                f"[probe] WARNING ALAudioPlayer unavailable: {exc!r} — "
                f"will skip play_file / play_web_stream",
                flush=True,
            )
            services["ALAudioPlayer"] = None

    # 3. Configure audio output. `setParameter('outputSampleRate', ...)`
    #    must be called BEFORE openAudioOutputs() or it is silently
    #    ignored (NAOqi 2.5 gotcha, documented elsewhere in this repo).
    audio = services["ALAudioDevice"]
    try:
        audio.closeAudioOutputs()
    except Exception:
        pass
    try:
        audio.setParameter("outputSampleRate", SR)
        print(f"[probe] set outputSampleRate to {SR}", flush=True)
    except Exception as exc:
        print(f"[probe] setParameter warning: {exc!r}", flush=True)
    try:
        audio.openAudioOutputs()
        print("[probe] openAudioOutputs ok", flush=True)
    except Exception as exc:
        print(f"[probe] openAudioOutputs warning: {exc!r}", flush=True)

    # 4. Spin up the HTTP server if we're going to need it.
    webstream_server: WebstreamServer | None = None
    if "qi_play_web_stream" in METHODS and services.get("ALAudioPlayer") is not None:
        webstream_dir = OUTPUT_DIR / "_webstream"
        webstream_dir.mkdir(parents=True, exist_ok=True)
        webstream_server = WebstreamServer(webstream_dir, WEBSTREAM_PORT)
        webstream_server.start()
        rpi_ip = get_local_ip_to_pepper(PEPPER_SSH_HOST)
        services["_webstream_server"] = webstream_server
        services["_rpi_ip_for_pepper"] = rpi_ip
        print(f"[probe] webstream reachable as http://{rpi_ip}:{WEBSTREAM_PORT}/", flush=True)

    # 5. Run each method sequentially.
    results: list[dict] = []
    for method_name in METHODS:
        runner = METHOD_RUNNERS.get(method_name)
        if runner is None:
            print(f"[probe] unknown method {method_name!r}, skipping", flush=True)
            continue
        if method_name in ("qi_play_file", "qi_play_web_stream") and services.get("ALAudioPlayer") is None:
            print(f"[probe] skipping {method_name} (no ALAudioPlayer)", flush=True)
            continue

        print(f"\n[probe] ── running method: {method_name} ──", flush=True)

        # Clear any lingering audio from the previous method.
        try:
            audio.flushAudioOutputs()
        except Exception as exc:
            print(f"[probe] flushAudioOutputs warning: {exc!r}", flush=True)
        time.sleep(0.2)

        mic = MicCapture()
        try:
            mic.start()
        except Exception as exc:
            print(f"[probe] ERROR mic start failed: {exc!r}", flush=True)
            return 5

        # A tiny pre-roll so the mic InputStream's first callback is in
        # the bag before we trigger playback. Without this, the very
        # first ~20 ms of audio can land in a half-armed buffer and the
        # click cross-correlation gets shifted by that amount.
        time.sleep(0.10)

        try:
            t_send_call = runner(services, ref_pcm)
        except Exception as exc:
            print(f"[probe] method {method_name} raised: {exc!r}", flush=True)
            mic.stop()
            continue

        # Wait for the audio to finish + tail for buffer drain + reverb.
        ref_duration_s = len(ref_pcm) / SR
        time.sleep(ref_duration_s + TAIL_S)

        # If this method spawned an ssh+paplay subprocess, close stdin
        # so paplay drains its buffer and exits cleanly. Done BEFORE
        # stopping the mic so the drain tail still gets captured.
        ssh_proc = services.pop("_ssh_paplay_proc", None)
        if ssh_proc is not None:
            try:
                ssh_proc.stdin.close()
            except Exception:
                pass
            try:
                ssh_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                ssh_proc.kill()
            except Exception:
                pass
            # Surface any stderr (paplay errors, ssh disconnects, etc).
            try:
                err = (ssh_proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                if err:
                    print(f"  [{method_name}] ssh/paplay stderr: {err}", flush=True)
            except Exception:
                pass

        mic_pcm = mic.stop()

        # Persist outputs unconditionally so you can listen.
        mic_path = OUTPUT_DIR / f"transport_{method_name}_mic.wav"
        save_wav_int16_mono(mic_path, mic_pcm, SR)
        print(f"  [{method_name}] wrote {mic_path}", flush=True)

        # Locate the click in the mic capture and compute latency.
        # Always log the peak value so we can debug a missing detection
        # (low peak = audio probably didn't reach the mic / template
        # was filtered out by resampling somewhere in the chain).
        click_idx, click_corr = find_click_sample(mic_pcm)
        capture_to_send_offset_s = t_send_call - mic.capture_start_monotonic
        if click_idx < 0 or click_corr < CLICK_DETECT_THRESHOLD:
            print(
                f"  [{method_name}] click not detected "
                f"(peak_corr={click_corr:.3f} < {CLICK_DETECT_THRESHOLD}, "
                f"best_sample={click_idx}) — latency UNKNOWN.",
                flush=True,
            )
            results.append({
                "method": method_name,
                "latency_ms": None,
                "peak_corr": click_corr,
            })
            time.sleep(INTER_METHOD_PAUSE_S)
            continue

        click_arrival_s_in_capture = click_idx / SR
        latency_s = click_arrival_s_in_capture - capture_to_send_offset_s
        latency_ms = latency_s * 1000.0
        print(
            f"  [{method_name}] click@mic_sample={click_idx} "
            f"({click_arrival_s_in_capture * 1000:.0f} ms after capture start, "
            f"peak_corr={click_corr:.2f}), "
            f"send_call_offset={capture_to_send_offset_s * 1000:.0f} ms, "
            f"-> one-way latency = {latency_ms:.0f} ms",
            flush=True,
        )
        results.append({
            "method": method_name,
            "latency_ms": latency_ms,
            "peak_corr": click_corr,
        })

        # Drain before next method.
        try:
            audio.flushAudioOutputs()
        except Exception:
            pass
        time.sleep(INTER_METHOD_PAUSE_S)

    # 6. Cleanup.
    if webstream_server is not None:
        webstream_server.stop()
    try:
        audio.flushAudioOutputs()
    except Exception:
        pass

    # 7. Summary table.
    print("\n[probe] ── summary ──")
    print(f"  {'method':<24} {'latency':>10}   {'peak_corr':>9}")
    print(f"  {'-' * 24} {'-' * 10}   {'-' * 9}")
    for r in results:
        lat = r["latency_ms"]
        latstr = f"{lat:.0f} ms" if lat is not None else "n/a"
        corr = r.get("peak_corr", 0.0)
        print(f"  {r['method']:<24} {latstr:>10}   {corr:>9.3f}")
    print(
        "\n[probe] tip: listen to outputs/transport_<method>_mic.wav for each "
        "method to sanity-check the latency numbers."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
