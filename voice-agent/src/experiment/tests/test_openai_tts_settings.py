"""Probe OpenAI TTS settings used by the experiment agent.

This is a diagnostic script, not a pytest test. It exercises both:

1. Raw OpenAI SDK streaming bytes.
2. The LiveKit OpenAI TTS plugin used by `agent_4o.py`.

Run from the repo root:

    uv run python voice-agent/src/experiment/tests/test_openai_tts_settings.py

Configuration is intentionally via constants/env vars, not CLI flags,
to match the project convention in CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import signal
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit.agents.types import APIConnectOptions, NOT_GIVEN
from livekit.plugins import openai as lk_openai
from openai import OpenAI


REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_PATH = REPO_ROOT / ".env"
OUTPUT_DIR = Path(__file__).resolve().parent / "outputs" / "openai_tts_settings"

MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
VOICE = os.environ.get("OPENAI_TTS_VOICE", "nova")
INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Speak in a friendly, warm, conversational tone — like a receptionist.",
)

TEXT_SHORT = "Hello, how can I help you today?"
TEXT_ROOM = (
    "Head up to the second floor. Go to your right. "
    "Room E-230 is along that corridor."
)

RUNS_PER_CASE = int(os.environ.get("OPENAI_TTS_TEST_RUNS", "2"))
LAYER_FILTER = {
    name.strip()
    for name in os.environ.get("OPENAI_TTS_TEST_LAYERS", "").split(",")
    if name.strip()
}
PER_CASE_TIMEOUT_SECONDS = float(os.environ.get("OPENAI_TTS_TEST_TIMEOUT", "35"))
CONNECT_TIMEOUT_SECONDS = float(os.environ.get("OPENAI_TTS_TEST_CONNECT_TIMEOUT", "10"))

# Keep this compact by default so it is usable during live experiment
# debugging. Add more formats here when needed.
CASES: list[dict[str, Any]] = [
    {"name": "mp3_short_no_instr", "text": TEXT_SHORT, "format": "mp3", "instructions": None},
    {"name": "mp3_short_instr", "text": TEXT_SHORT, "format": "mp3", "instructions": INSTRUCTIONS},
    {"name": "opus_short_no_instr", "text": TEXT_SHORT, "format": "opus", "instructions": None},
    {"name": "opus_short_instr", "text": TEXT_SHORT, "format": "opus", "instructions": INSTRUCTIONS},
    {"name": "pcm_short_no_instr", "text": TEXT_SHORT, "format": "pcm", "instructions": None},
    {"name": "pcm_short_instr", "text": TEXT_SHORT, "format": "pcm", "instructions": INSTRUCTIONS},
    {"name": "mp3_room_instr", "text": TEXT_ROOM, "format": "mp3", "instructions": INSTRUCTIONS},
    {"name": "opus_room_instr", "text": TEXT_ROOM, "format": "opus", "instructions": INSTRUCTIONS},
    {"name": "pcm_room_instr", "text": TEXT_ROOM, "format": "pcm", "instructions": INSTRUCTIONS},
]
CASE_NAME_FILTER = {
    name.strip()
    for name in os.environ.get("OPENAI_TTS_TEST_CASES", "").split(",")
    if name.strip()
}
if CASE_NAME_FILTER:
    CASES = [case for case in CASES if case["name"] in CASE_NAME_FILTER]


@dataclass
class TimeoutGuard:
    seconds: float

    def __enter__(self) -> None:
        def _raise_timeout(_signum, _frame) -> None:
            raise TimeoutError(f"case exceeded {self.seconds:.1f}s")

        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _load_env() -> None:
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=False)
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError(f"OPENAI_API_KEY is not set; checked {ENV_PATH}")


def _ext_for_format(response_format: str) -> str:
    return "s16le.pcm" if response_format == "pcm" else response_format


def _write_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def _base_row(layer: str, case: dict[str, Any], run_index: int) -> dict[str, Any]:
    return {
        "layer": layer,
        "case": case["name"],
        "run": run_index,
        "model": MODEL,
        "voice": VOICE,
        "format": case["format"],
        "chars": len(case["text"]),
        "instructions": bool(case["instructions"]),
        "timeout_s": PER_CASE_TIMEOUT_SECONDS,
    }


def probe_openai_sdk(
    client: OpenAI,
    case: dict[str, Any],
    run_index: int,
    out_dir: Path,
) -> dict[str, Any]:
    row = _base_row("openai_sdk", case, run_index)
    out_path = out_dir / f"{row['layer']}_{case['name']}_r{run_index}.{_ext_for_format(case['format'])}"
    request: dict[str, Any] = {
        "model": MODEL,
        "voice": VOICE,
        "input": case["text"],
        "response_format": case["format"],
        "timeout": PER_CASE_TIMEOUT_SECONDS,
    }
    if case["instructions"]:
        request["instructions"] = case["instructions"]

    t0 = time.monotonic()
    first_byte_at: float | None = None
    bytes_total = 0
    chunks = 0

    try:
        with TimeoutGuard(PER_CASE_TIMEOUT_SECONDS):
            with out_path.open("wb") as f:
                with client.audio.speech.with_streaming_response.create(**request) as response:
                    row["http_status"] = getattr(response, "status_code", None)
                    row["request_id"] = getattr(response, "request_id", None)
                    for chunk in response.iter_bytes():
                        if first_byte_at is None:
                            first_byte_at = time.monotonic()
                        chunks += 1
                        bytes_total += len(chunk)
                        f.write(chunk)

        row["ok"] = True
        row["error"] = None
    except Exception as exc:
        row["ok"] = False
        row["error"] = f"{type(exc).__name__}: {exc}"

    total_ms = (time.monotonic() - t0) * 1000.0
    row.update(
        {
            "first_byte_ms": (
                (first_byte_at - t0) * 1000.0 if first_byte_at is not None else None
            ),
            "total_ms": total_ms,
            "chunks": chunks,
            "bytes": bytes_total,
            "output": str(out_path.relative_to(REPO_ROOT)) if bytes_total else None,
        }
    )
    return row


def _frame_bytes(frame) -> bytes:
    data = getattr(frame, "data", b"")
    if hasattr(data, "tobytes"):
        return data.tobytes()
    return bytes(data)


def _write_wav(path: Path, frames: list[Any]) -> None:
    if not frames:
        return

    first = frames[0]
    sample_rate = int(getattr(first, "sample_rate", 24000))
    num_channels = int(getattr(first, "num_channels", 1))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(num_channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        for frame in frames:
            wav.writeframes(_frame_bytes(frame))


async def probe_livekit_plugin(
    case: dict[str, Any],
    run_index: int,
    out_dir: Path,
) -> dict[str, Any]:
    row = _base_row("livekit_plugin", case, run_index)
    out_path = out_dir / f"{row['layer']}_{case['name']}_r{run_index}.wav"

    kwargs: dict[str, Any] = {
        "model": MODEL,
        "voice": VOICE,
        "response_format": case["format"],
    }
    kwargs["instructions"] = case["instructions"] if case["instructions"] else NOT_GIVEN

    tts = lk_openai.TTS(**kwargs)
    conn_options = APIConnectOptions(max_retry=0, timeout=CONNECT_TIMEOUT_SECONDS)
    t0 = time.monotonic()
    first_frame_at: float | None = None
    frames = []
    audio_duration_s = 0.0

    def _on_error(err) -> None:
        print(f"    [livekit:error] {case['name']} r{run_index}: {err}", flush=True)

    tts.on("error", _on_error)

    try:
        async with asyncio.timeout(PER_CASE_TIMEOUT_SECONDS):
            stream = tts.synthesize(case["text"], conn_options=conn_options)
            async with stream:
                async for event in stream:
                    if first_frame_at is None:
                        first_frame_at = time.monotonic()
                    frames.append(event.frame)
                    audio_duration_s += float(getattr(event.frame, "duration", 0.0))

        _write_wav(out_path, frames)
        row["ok"] = bool(frames)
        row["error"] = None if frames else "NO_AUDIO_FRAMES"
    except Exception as exc:
        row["ok"] = False
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await tts.aclose()

    total_ms = (time.monotonic() - t0) * 1000.0
    row.update(
        {
            "first_frame_ms": (
                (first_frame_at - t0) * 1000.0 if first_frame_at is not None else None
            ),
            "total_ms": total_ms,
            "frames": len(frames),
            "audio_duration_s": audio_duration_s,
            "output": str(out_path.relative_to(REPO_ROOT)) if frames else None,
        }
    )
    return row


def _print_row(row: dict[str, Any]) -> None:
    first = row.get("first_byte_ms", row.get("first_frame_ms"))
    first_s = "None" if first is None else f"{first:.0f}ms"
    status = "ok" if row["ok"] else "FAIL"
    payload = row.get("bytes", row.get("frames"))
    print(
        f"{status:4s} {row['layer']:14s} {row['case']:22s} "
        f"r{row['run']} first={first_s:>8s} total={row['total_ms']:.0f}ms "
        f"payload={payload} err={row.get('error')}",
        flush=True,
    )


async def main() -> None:
    _load_env()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d_%H%M%S")
    summary_jsonl = OUTPUT_DIR / f"summary_{stamp}.jsonl"
    summary_csv = OUTPUT_DIR / f"summary_{stamp}.csv"

    print("[openai-tts-test] starting", flush=True)
    print(f"[openai-tts-test] output_dir={OUTPUT_DIR.relative_to(REPO_ROOT)}", flush=True)
    print(
        f"[openai-tts-test] model={MODEL} voice={VOICE} runs={RUNS_PER_CASE} "
        f"timeout={PER_CASE_TIMEOUT_SECONDS}s",
        flush=True,
    )

    client = OpenAI(max_retries=0)
    rows: list[dict[str, Any]] = []

    for run_index in range(1, RUNS_PER_CASE + 1):
        for case in CASES:
            if not LAYER_FILTER or "openai_sdk" in LAYER_FILTER:
                sdk_row = probe_openai_sdk(client, case, run_index, OUTPUT_DIR)
                rows.append(sdk_row)
                _write_jsonl(summary_jsonl, sdk_row)
                _print_row(sdk_row)

            if not LAYER_FILTER or "livekit_plugin" in LAYER_FILTER:
                plugin_row = await probe_livekit_plugin(case, run_index, OUTPUT_DIR)
                rows.append(plugin_row)
                _write_jsonl(summary_jsonl, plugin_row)
                _print_row(plugin_row)

    fieldnames = sorted({key for row in rows for key in row})
    with summary_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"[openai-tts-test] jsonl={summary_jsonl.relative_to(REPO_ROOT)}", flush=True)
    print(f"[openai-tts-test] csv={summary_csv.relative_to(REPO_ROOT)}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
