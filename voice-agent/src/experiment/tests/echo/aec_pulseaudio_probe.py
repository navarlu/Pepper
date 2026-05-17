"""Phase 1 alt — PulseAudio `module-echo-cancel` (WebRTC AEC) probe.

⚠ AEC2, not AEC3. Ubuntu 24.04 ships `libwebrtc-audio-processing 0.3.1`
(2019) which only contains the legacy `EchoCancellationImpl` (AEC2 era).
`EchoCanceller3` was added in libwebrtc-audio-processing 1.x and has
not been packaged. So this probe is testing WebRTC AEC2, which is the
same algorithmic generation as SpeexDSP — expect comparable ceilings,
not the dramatic step-up of AEC3.

Architecture mirrors `aec_offline_probe.py` (SpeexDSP):
- Launch a private PulseAudio daemon under a temp PULSE_RUNTIME_PATH.
- Load module-echo-cancel with aec_method=webrtc against the DJI mic
  and a null sink (ref_sink) whose monitor is the AEC reference.
- Stream the test WAV simultaneously to (a) Pepper via the robot
  bridge TCP socket and (b) the AEC reference sink via paplay, with
  a tuned leading-silence padding so the reference lines up with the
  ~1 s NAOqi playback latency.
- Capture raw mic, AEC-cleaned mic, and ref_sink.monitor in parallel
  via parec. The third capture is the alignment sanity probe.
- Compute ERLE the same way the SpeexDSP probe does.

How it works
------------

1. Launches a private PulseAudio daemon under a fresh
   `PULSE_RUNTIME_PATH=/tmp/pa_aec_<rand>`. The system has PipeWire
   running for the desktop, but our daemon is fully isolated — its
   socket sits in a temp dir and never registers as the system
   default. Tear-down on exit.

2. Loads four modules (handcrafted .pa script — no /etc/pulse
   defaults touched):
     - `module-native-protocol-unix`: the Unix socket server.
     - `module-alsa-source device=hw:CARD=MINI,DEV=0` → `dji_mic`:
       opens the DJI USB mic directly at the ALSA hw: layer. Pulse
       picks the format the hw accepts (S24LE 2ch 48 kHz).
     - `module-null-sink sink_name=ref_sink`: a silent sink whose
       monitor is the AEC reference signal.
     - `module-echo-cancel source_master=dji_mic
       sink_master=ref_sink aec_method=webrtc`: creates `ec_source`
       (echo-cancelled mic) and `ec_sink` (the sink we play the
       reference into). The module internally pulls
       `ref_sink.monitor` as the AEC reference.

3. Plays the test WAV simultaneously to:
     - Pepper's chest speaker via the robot bridge TCP socket
       (identical 50 ms-chunk mono 16 kHz path used by every other
       probe).
     - `ec_sink` (via paplay), so module-echo-cancel knows what was
       sent to the speaker.

4. Captures two streams in parallel for the playback duration plus a
   tail, both via parec:
     - `mic_raw_pa.wav` from `dji_mic` — what the mic actually
       hears (Pepper's echo + room noise + your voice).
     - `mic_aec_pa.wav` from `ec_source` — same mic after AEC.

5. Computes ERLE the same way the SpeexDSP probe does, so the
   numbers are directly comparable.

NAOqi latency note
------------------
Pepper buffers ~1 s of audio before playing it. The SpeexDSP probe
had to pre-align the reference because its filter is causal and
~500 ms long. WebRTC AEC3 has an internal delay-estimator and
defaults to "delay-agnostic" mode in this module build — it should
handle Pepper's 1-second buffer without an explicit hint.

Run
---
1. Stop the experiment audio path so /dev/snd and the robot bridge are free:
       docker compose -f docker/docker-compose.experiment.yml stop audio-bridge user-client
2. Confirm Pepper's outputSampleRate is 16 kHz (see
   project_naoqi_outputsamplerate.md — bridge.py sets it in the wrong
   order, so it sometimes sticks at 48 kHz).
3. Run:
       uv run python voice-agent/src/experiment/tests/echo/aec_pulseaudio_probe.py
4. Restart the audio path when done:
       docker compose -f docker/docker-compose.experiment.yml start audio-bridge user-client
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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


# ── Robot bridge / audio constants (match SpeexDSP probe) ────────────
TCP_HOST = os.environ.get("TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("TCP_PORT", "55555"))

SR = 16000
CHANNELS = 1
SAMP_WIDTH = 2  # int16

CHUNK_MS = 50
SLEEP_BETWEEN_CHUNKS_S = 0.045

# Capture tail. WebRTC's delay estimator + Pepper's ~1 s NAOqi buffer
# means we need at least 2 s after playback-done before stopping the
# captures, or the cleaned mic will miss the echo's tail.
TAIL_S = 3.0

# Latency hint passed to paplay (ms). Lower = less PA buffering on
# the reference path, which means we know more precisely when the
# reference signal reaches the AEC module.
PAPLAY_LATENCY_MS = 50

# Leading silence prepended to the AEC reference before sending it
# to ec_sink. Pepper's NAOqi adds ~1 s of buffering on its end, but
# paplay itself also buffers a few hundred ms before reaching the
# sink. Net effect: with predelay=0 the AEC reference arrives a few
# hundred ms BEHIND the mic echo; with predelay=950 ref leads mic
# by ~650 ms (measured via envelope correlation of refmon vs
# mic_raw). WebRTC AEC wants reference to lead mic by 50–150 ms
# (causal filter, but small lead lets it track the impulse). So we
# want net (predelay - measured_overshoot) ≈ +100 ms → predelay
# around 300 ms here. Tune up/down 100 ms if Pepper's buffer state
# drifts.
REF_PREDELAY_MS = 300

# ERLE measurement window — same convention as the SpeexDSP probe so
# numbers are directly comparable.
ERLE_REF_LEVEL_FRAC = 0.05


# ── Dependency probes ───────────────────────────────────────────────
try:
    from scipy.signal import resample_poly  # type: ignore
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

for tool in ("pulseaudio", "pactl", "paplay", "parec"):
    if shutil.which(tool) is None:
        print(f"ERROR: required tool '{tool}' not in PATH. "
              f"Install via `sudo apt install pulseaudio pulseaudio-utils`.",
              file=sys.stderr)
        sys.exit(1)


# ── WAV helpers (identical to SpeexDSP probe) ───────────────────────
def load_wav_as_int16_mono_sr(path: Path, target_sr: int) -> np.ndarray:
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
                f"WAV is {sr} Hz, need {target_sr} Hz, scipy missing for resampling."
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


def db_rms(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    f = x.astype(np.float64) / 32768.0
    rms = np.sqrt(np.mean(f * f) + 1e-20)
    return 20.0 * np.log10(rms + 1e-20)


def compute_erle(mic_raw: np.ndarray, mic_proc: np.ndarray, ref: np.ndarray) -> dict:
    """ERLE over the active region.

    Unlike the SpeexDSP probe (where the reference is pre-aligned with
    the mic so its envelope marks the active window directly), the PA
    captures start ~0.5 s before playback and Pepper itself adds ~1 s
    of NAOqi-buffer delay. The reference array we pass in starts at
    capture-sample-0 with no time alignment, so its envelope is the
    WRONG window for masking. Instead we mask on the RAW MIC's
    envelope — wherever the mic actually heard something, that's
    where the echo to cancel lives."""
    n = min(len(mic_raw), len(mic_proc), len(ref))
    mic_raw = mic_raw[:n]
    mic_proc = mic_proc[:n]
    ref = ref[:n]
    win = max(1, SR // 10)
    mic_env = np.abs(mic_raw.astype(np.float32))
    if n >= win:
        kernel = np.ones(win, dtype=np.float32) / win
        mic_env = np.convolve(mic_env, kernel, mode="same")
    peak = float(mic_env.max())
    if peak < 50.0:
        return {
            "erle_db": 0.0,
            "raw_db": db_rms(mic_raw),
            "proc_db": db_rms(mic_proc),
            "ref_db": db_rms(ref),
            "active_fraction": 0.0,
            "note": "raw mic essentially silent — Pepper may not have played",
        }
    thresh = peak * 0.1  # 0.1 of peak: catches all loud regions, ignores room noise
    mask = mic_env > thresh
    return {
        "erle_db": db_rms(mic_raw[mask]) - db_rms(mic_proc[mask]),
        "raw_db": db_rms(mic_raw[mask]),
        "proc_db": db_rms(mic_proc[mask]),
        "ref_db": db_rms(ref) if ref.any() else -120.0,
        "active_fraction": float(mask.sum()) / n,
    }


# ── Robot bridge sender (identical to SpeexDSP probe) ───────────────
def stream_to_robot_bridge(pcm: np.ndarray, sock: socket.socket) -> None:
    bytes_per_chunk = SR * CHUNK_MS // 1000 * SAMP_WIDTH
    raw = pcm.astype(np.int16).tobytes()
    i = 0
    n = len(raw)
    while i < n:
        chunk = raw[i : i + bytes_per_chunk]
        header = len(chunk).to_bytes(4, "big")
        sock.sendall(header + chunk)
        i += bytes_per_chunk
        time.sleep(SLEEP_BETWEEN_CHUNKS_S)


# ── PulseAudio daemon lifecycle ─────────────────────────────────────
class PulseAudioDaemon:
    """Private PulseAudio instance under an isolated PULSE_RUNTIME_PATH.

    Started via `pulseaudio --daemonize=no -n -F <pulse.pa>` — the `-n`
    flag tells PA *not* to load the system default config, only our
    handcrafted script. Tears the daemon down on exit; never touches
    host audio state."""

    # aec_args tuning for the WebRTC backend in libwebrtc-util.so 0.3.1:
    # - analog_gain_control=0 / digital_gain_control=0  → disable AGC.
    #   Default AGC was boosting the cleaned mic by ~12 dB (yielding
    #   *negative* ERLE in the first run — the AEC mic was louder than
    #   the raw mic). For an ERLE measurement we want unity gain.
    # - noise_suppression=0  → off so we measure raw echo cancellation,
    #   not "AEC + NS combined" attenuation.
    # - high_pass_filter=0  → off for the same reason.
    # - extended_filter=1  → uses the long-tail adaptive filter
    #   (~500 ms vs the default ~128 ms). Needed because Pepper's
    #   speaker reverb + NAOqi buffer push the effective echo path
    #   well past the default filter length.
    # Note: this PA's WebRTC version pre-dates `delay_agnostic`, so
    # we rely on the extended filter + WebRTC's built-in delay
    # estimator to handle Pepper's ~1 s playback latency.
    PULSE_CONFIG = """\
.fail
load-module module-native-protocol-unix
load-module module-alsa-source device=hw:CARD=MINI,DEV=0 source_name=dji_mic
load-module module-null-sink sink_name=ref_sink rate=16000 channels=1
load-module module-echo-cancel source_master=dji_mic sink_master=ref_sink source_name=ec_source sink_name=ec_sink aec_method=webrtc rate=16000 channels=1 aec_args="analog_gain_control=0 digital_gain_control=0 noise_suppression=0 high_pass_filter=0 extended_filter=1"
"""

    REQUIRED_SOURCES = {"dji_mic", "ec_source"}
    REQUIRED_SINKS = {"ref_sink", "ec_sink"}

    def __init__(self) -> None:
        self._runtime_dir = Path(tempfile.mkdtemp(prefix="pa_aec_", dir="/tmp"))
        os.chmod(self._runtime_dir, 0o700)
        self._config_path = self._runtime_dir / "pulse.pa"
        self._log_path = self._runtime_dir / "pulse.log"
        self._proc: subprocess.Popen | None = None

    @property
    def runtime_dir(self) -> Path:
        return self._runtime_dir

    def env(self) -> dict[str, str]:
        e = os.environ.copy()
        e["PULSE_RUNTIME_PATH"] = str(self._runtime_dir)
        # Some pactl invocations honour XDG_RUNTIME_DIR for fallback.
        e["XDG_RUNTIME_DIR"] = str(self._runtime_dir)
        return e

    def start(self) -> None:
        self._config_path.write_text(self.PULSE_CONFIG)
        log = open(self._log_path, "w")
        self._proc = subprocess.Popen(
            [
                "pulseaudio",
                "--daemonize=no",
                "--exit-idle-time=-1",
                "--disallow-exit",
                "-n",                     # ignore system /etc/pulse/default.pa
                "-F", str(self._config_path),
            ],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            preexec_fn=os.setsid,         # own session for clean teardown
        )
        sock = self._runtime_dir / "native"
        for _ in range(50):
            if sock.exists():
                break
            time.sleep(0.1)
        else:
            self._dump_log()
            raise RuntimeError(
                f"PulseAudio socket {sock} did not appear within 5 s"
            )

        # Wait for the AEC modules to register their nodes.
        for _ in range(50):
            srcs = self._list_short("sources")
            sinks = self._list_short("sinks")
            if (self.REQUIRED_SOURCES.issubset(srcs)
                    and self.REQUIRED_SINKS.issubset(sinks)):
                break
            time.sleep(0.1)
        else:
            self._dump_log()
            raise RuntimeError(
                "AEC modules never registered "
                f"(srcs={self._list_short('sources')} "
                f"sinks={self._list_short('sinks')})"
            )

        # Mic source defaults to 33% volume on a fresh PA instance —
        # boost to 100% so the AEC reference and the mic capture
        # match levels. Same goes for ec_source so we get unattenuated
        # AEC output.
        for src in ("dji_mic", "ec_source"):
            subprocess.run(
                ["pactl", "set-source-volume", src, "100%"],
                env=self.env(),
                check=False,
            )

        print(
            f"[pulse] daemon ready pid={self._proc.pid} "
            f"runtime_dir={self._runtime_dir} "
            f"sources={sorted(self.REQUIRED_SOURCES)} "
            f"sinks={sorted(self.REQUIRED_SINKS)}",
            flush=True,
        )

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
                try:
                    self._proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    os.killpg(self._proc.pid, signal.SIGKILL)
                    self._proc.wait(timeout=1.0)
            except ProcessLookupError:
                pass
        try:
            shutil.rmtree(self._runtime_dir)
        except Exception:
            pass

    def _list_short(self, what: str) -> set[str]:
        r = subprocess.run(
            ["pactl", "list", what, "short"],
            env=self.env(), capture_output=True, text=True, timeout=3.0,
        )
        names: set[str] = set()
        for line in r.stdout.splitlines():
            cols = line.split("\t")
            if len(cols) >= 2:
                names.add(cols[1])
        return names

    def _dump_log(self) -> None:
        if not self._log_path.exists():
            return
        print("\n--- pulseaudio log ---", file=sys.stderr)
        try:
            print(self._log_path.read_text(), file=sys.stderr)
        except Exception:
            pass
        print("--- end pulseaudio log ---\n", file=sys.stderr)


# ── parec / paplay wrappers ────────────────────────────────────────
def parec_to_wav(env: dict[str, str], source: str, out_wav: Path
                 ) -> subprocess.Popen:
    """Start a parec process that records `source` and writes a WAV
    file. Returns the Popen so caller can stop it with SIGINT (which
    parec flushes cleanly to disk)."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "parec",
        f"--device={source}",
        "--format=s16le",
        f"--rate={SR}",
        "--channels=1",
        "--file-format=wav",
        str(out_wav),
    ]
    return subprocess.Popen(
        cmd, env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


def paplay_pcm(env: dict[str, str], sink: str, pcm: np.ndarray) -> None:
    """Play raw mono int16 PCM to a Pulse sink via paplay (stdin).
    Blocks until playback drains; raises on paplay failure with the
    captured stderr so we know what went wrong."""
    # paplay reads from stdin when no file argument is given. (`-` as
    # a filename is interpreted literally and triggers ENOENT.)
    cmd = [
        "paplay",
        f"--device={sink}",
        "--format=s16le",
        f"--rate={SR}",
        "--channels=1",
        "--raw",
        f"--latency-msec={PAPLAY_LATENCY_MS}",
    ]
    proc = subprocess.Popen(
        cmd, env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None
        # Write in small chunks so paplay can consume in real time —
        # writing a 7.5 s buffer in one go fills the pipe and paplay's
        # default latency (~2 s) can spill stderr-relevant errors
        # before we even start writing.
        data = pcm.astype(np.int16).tobytes()
        chunk_bytes = SR * SAMP_WIDTH // 5   # 200 ms at a time
        for i in range(0, len(data), chunk_bytes):
            proc.stdin.write(data[i : i + chunk_bytes])
            proc.stdin.flush()
        proc.stdin.close()
        rc = proc.wait(timeout=max(10.0, len(pcm) / SR + 5.0))
        if rc != 0:
            err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
            raise RuntimeError(f"paplay exited rc={rc} sink={sink} stderr={err.strip()!r}")
    except BrokenPipeError as exc:
        # paplay died before we finished writing — surface its stderr.
        err = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
        proc.wait(timeout=2.0)
        raise RuntimeError(f"paplay broken pipe sink={sink} stderr={err.strip()!r}") from exc
    except Exception:
        proc.kill()
        proc.wait()
        raise


# ── Main ───────────────────────────────────────────────────────────
def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Load reference WAV.
    if not DEFAULT_WAV.exists():
        print(f"[probe] ERROR no input WAV at {DEFAULT_WAV}", flush=True)
        return 2
    ref_pcm = load_wav_as_int16_mono_sr(DEFAULT_WAV, SR)
    print(f"[probe] reference_loaded path={DEFAULT_WAV.name} "
          f"duration_s={len(ref_pcm) / SR:.2f}", flush=True)

    # 2. Start private PulseAudio + AEC stack.
    pa = PulseAudioDaemon()
    try:
        pa.start()
    except Exception as exc:
        print(f"[probe] ERROR starting pulseaudio: {exc}", flush=True)
        pa.stop()
        return 3
    env = pa.env()

    try:
        # 3. Connect to robot bridge.
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
                "  - Is audio-bridge stopped?\n"
                f"  - Is {TCP_HOST}:{TCP_PORT} the right host/port?",
                flush=True,
            )
            return 4

        raw_path = OUTPUT_DIR / "mic_raw_pa.wav"
        aec_path = OUTPUT_DIR / "mic_aec_pa.wav"
        refmon_path = OUTPUT_DIR / "ref_monitor_pa.wav"
        ref_path = OUTPUT_DIR / "ref_pa.wav"

        # 4. Start three captures in parallel: raw mic, AEC mic, and
        # the actual signal the AEC module is receiving as reference
        # (ref_sink.monitor). The third capture is a sanity probe —
        # if it's silent the AEC saw no reference signal; if it's
        # populated but ERLE is still tiny, the alignment is wrong.
        rec_raw = parec_to_wav(env, "dji_mic", raw_path)
        rec_aec = parec_to_wav(env, "ec_source", aec_path)
        rec_refmon = parec_to_wav(env, "ref_sink.monitor", refmon_path)
        time.sleep(0.5)  # let parec streams settle
        print(f"[probe] capture_started raw={raw_path.name} aec={aec_path.name} "
              f"refmon={refmon_path.name}", flush=True)

        # 5. Play in parallel: Pepper (TCP) + AEC reference (Pulse sink).
        # Pre-delay the AEC reference so it lines up with when the
        # mic actually hears Pepper. See REF_PREDELAY_MS comment.
        predelay_samples = REF_PREDELAY_MS * SR // 1000
        ref_for_aec = np.concatenate([
            np.zeros(predelay_samples, dtype=np.int16),
            ref_pcm,
        ])
        print(
            f"[probe] ref_predelay_ms={REF_PREDELAY_MS} "
            f"paplay_latency_ms={PAPLAY_LATENCY_MS}",
            flush=True,
        )

        def _play_pepper():
            stream_to_robot_bridge(ref_pcm, sock)

        def _play_ref():
            paplay_pcm(env, "ec_sink", ref_for_aec)

        t_pepper = threading.Thread(target=_play_pepper, name="pepper-tx")
        t_ref = threading.Thread(target=_play_ref, name="aec-ref-tx")
        t_start = time.monotonic()
        t_pepper.start()
        t_ref.start()
        t_pepper.join()
        t_ref.join()
        print(
            f"[probe] playback_done tx_wallclock_s={time.monotonic() - t_start:.2f} "
            f"ref_duration_s={len(ref_pcm) / SR:.2f}",
            flush=True,
        )

        # 6. Wait for tail (covers NAOqi's ~1 s buffer + reverb), then
        # stop captures.
        time.sleep(TAIL_S)
        for p in (rec_raw, rec_aec, rec_refmon):
            p.send_signal(signal.SIGINT)  # parec flushes on SIGINT
            try:
                p.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        sock.close()

        # 7. Save the original reference for downstream analysis tools.
        save_wav_int16_mono(ref_path, ref_pcm, SR)
        print(f"[probe] wrote {raw_path}", flush=True)
        print(f"[probe] wrote {aec_path}", flush=True)
        print(f"[probe] wrote {ref_path}", flush=True)

        # 8. Read back the two captures.
        def read_wav(p: Path) -> np.ndarray:
            with wave.open(str(p), "rb") as wf:
                nch = wf.getnchannels()
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
            if nch > 1:
                pcm = pcm.reshape(-1, nch).mean(axis=1).astype(np.int16)
            return pcm

        mic_raw = read_wav(raw_path)
        mic_aec = read_wav(aec_path)
        # Captures may differ by a few frames in length due to
        # start/stop jitter.
        n = min(len(mic_raw), len(mic_aec))
        mic_raw = mic_raw[:n]
        mic_aec = mic_aec[:n]

        # 9. Build an "active region" mask. Captures began ~0.5 s
        # before playback; align the reference to start at sample 0
        # plus the parec start delay so the mask covers the right
        # window. The leading silence in mic_raw/mic_aec carries no
        # echo, so missing it from the mask is fine.
        ref_aligned = np.zeros(n, dtype=np.int16)
        copy_len = min(len(ref_pcm), n)
        ref_aligned[:copy_len] = ref_pcm[:copy_len]

        m = compute_erle(mic_raw, mic_aec, ref_aligned)
        print("\n[probe] ── results (PulseAudio WebRTC AEC2) ──")
        print(f"  raw mic RMS    : {m['raw_db']:.1f} dBFS")
        print(f"  AEC mic RMS    : {m['proc_db']:.1f} dBFS")
        print(f"  reference RMS  : {m['ref_db']:.1f} dBFS")
        print(f"  ERLE           : {m['erle_db']:.1f} dB  "
              f"(target >= 15; SpeexDSP probe peaks ~10 dB)")
        print(f"  active region  : {m['active_fraction'] * 100.0:.0f}% of capture")
        print(f"  NOTE: this is AEC2 (libwebrtc 0.3.1) — same generation as")
        print(f"        SpeexDSP. AEC3 would need libwebrtc-audio-processing >= 1.0.")
        if m.get("note"):
            print(f"  NOTE: {m['note']}")

        verdict = "PASS" if m["erle_db"] >= 15.0 else "FAIL"
        print(f"\n[probe] verdict: {verdict}")
        print(f"[probe] compare by ear: outputs/mic_raw_pa.wav vs outputs/mic_aec_pa.wav")
        return 0

    finally:
        pa.stop()


if __name__ == "__main__":
    sys.exit(main())
