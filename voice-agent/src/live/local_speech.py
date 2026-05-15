"""Local STT (Faster-Whisper) and TTS (Piper) adapters for LiveKit.

Two LiveKit plugin classes used only by `_build_local_session()` in
`agent.py`:

  - `FasterWhisperSTT` — wraps the `faster_whisper.WhisperModel` into
    the `livekit.agents.stt.STT` interface. One-shot recognition
    (no streaming), 16 kHz mono, VAD-filtered.
  - `PiperTTS` — wraps `piper.PiperVoice` into `livekit.agents.tts.TTS`.
    Chunked synthesis, native sample rate from the ONNX model.

Both report per-call timing via the optional `on_metrics` callback so
the agent's pipeline view (`[PIPE] stage=stt ...`) shows real
latencies.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from faster_whisper import WhisperModel
from livekit import rtc
from livekit.agents import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS, stt, tts
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from piper import PiperVoice, SynthesisConfig


def _resample_audio(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio.astype(np.float32, copy=False)
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    duration = audio.shape[0] / float(src_rate)
    dst_samples = max(1, int(duration * dst_rate))
    src_positions = np.linspace(0.0, 1.0, num=audio.shape[0], endpoint=False)
    dst_positions = np.linspace(0.0, 1.0, num=dst_samples, endpoint=False)
    return np.interp(dst_positions, src_positions, audio).astype(np.float32, copy=False)


logger = logging.getLogger("voice-agent")


class FasterWhisperSTT(stt.STT):
    """Faster-Whisper ONNX inference wrapped as a LiveKit `STT` plugin.

    Non-streaming: the agent's VAD chops user speech into utterances,
    hands each as a batched `AudioFrame` list, and we run one
    Whisper pass. Short quiet buffers are filtered via an RMS
    threshold (`min_energy`) to kill obvious mic-bleed hallucinations.
    """

    def __init__(
        self,
        *,
        model: str = "small",
        language: str = "en",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
        min_energy: float = 0.01,
        min_words_for_long_audio: int = 2,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._model_name = model
        self._language = language
        self._on_metrics = on_metrics
        self._min_energy = min_energy
        self._min_words_for_long_audio = min_words_for_long_audio
        self._model = WhisperModel(
            model,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )

    @property
    def model(self) -> str:
        return self._model_name

    @property
    def provider(self) -> str:
        return "local-faster-whisper"

    def _recognize_sync(
        self,
        audio_16k: np.ndarray,
        language: str | None,
    ) -> tuple[str, str]:
        segments, info = self._model.transcribe(
            audio_16k,
            language=language,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        detected_language = info.language or (language or self._language)
        return text, detected_language

    # region: whisper_recognize
    async def _recognize_impl(
        self,
        buffer: rtc.AudioFrame | list[rtc.AudioFrame],
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        del conn_options
        frame = rtc.combine_audio_frames(buffer)
        pcm = np.frombuffer(frame.data, dtype=np.int16)
        if frame.num_channels > 1:
            pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)

        audio = (pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
        audio_16k = _resample_audio(audio, src_rate=frame.sample_rate, dst_rate=16000)
        audio_duration_ms = round(len(audio_16k) / 16000.0 * 1000, 1)

        requested_language: str | None = None
        if language is not NOT_GIVEN:
            requested_language = language
        elif self._language:
            requested_language = self._language

        t0 = time.monotonic()
        text, detected_language = await asyncio.to_thread(
            self._recognize_sync,
            audio_16k,
            requested_language,
        )
        duration_ms = round((time.monotonic() - t0) * 1000, 1)

        # Filter hallucinations from quiet audio or mic bleed.
        rms = float(np.sqrt(np.mean(audio_16k ** 2)))
        if rms < self._min_energy:
            logger.info("stt_filtered reason=low_energy rms=%.4f text=%s", rms, text[:60])
            text = ""
    # endregion

        logger.info("stt_done duration_ms=%.1f audio_duration_ms=%.1f rms=%.4f text=%s", duration_ms, audio_duration_ms, rms, text[:80])

        if self._on_metrics:
            try:
                self._on_metrics({
                    "stage": "stt",
                    "duration_ms": duration_ms,
                    "audio_duration_ms": audio_duration_ms,
                    "text": text[:200],
                })
            except Exception:
                pass

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=str(uuid.uuid4()),
            alternatives=[
                stt.SpeechData(
                    language=detected_language,
                    text=text,
                )
            ],
        )

    async def aclose(self) -> None:
        return None


@dataclass
class PiperSynthesisOptions:
    speaker_id: int | None = None
    length_scale: float = 1.0
    noise_scale: float = 0.667
    noise_w_scale: float = 0.8


class PiperChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        piper_tts: "PiperTTS",
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=piper_tts, input_text=input_text, conn_options=conn_options)
        self._piper_tts = piper_tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=self._piper_tts.sample_rate,
            num_channels=self._piper_tts.num_channels,
            mime_type="audio/raw",
        )

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        t0 = time.monotonic()
        first_chunk_ms: float | None = None

        def worker() -> None:
            try:
                for chunk in self._piper_tts._voice.synthesize(
                    self.input_text,
                    syn_config=SynthesisConfig(
                        speaker_id=self._piper_tts._opts.speaker_id,
                        length_scale=self._piper_tts._opts.length_scale,
                        noise_scale=self._piper_tts._opts.noise_scale,
                        noise_w_scale=self._piper_tts._opts.noise_w_scale,
                    ),
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk.audio_int16_bytes)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        worker_task = asyncio.create_task(asyncio.to_thread(worker))

        chunk_count = 0
        while True:
            pcm = await queue.get()
            if pcm is None:
                break
            if first_chunk_ms is None:
                first_chunk_ms = (time.monotonic() - t0) * 1000.0
                logger.info(
                    "tts_first_chunk_ms=%.1f chars=%d",
                    first_chunk_ms, len(self.input_text),
                )
            output_emitter.push(pcm)
            chunk_count += 1

        output_emitter.flush()
        await worker_task

        duration_ms = round((time.monotonic() - t0) * 1000, 1)
        characters = len(self.input_text)
        logger.info(
            "tts_done duration_ms=%.1f first_chunk_ms=%.1f chunks=%d characters=%d text=%s",
            duration_ms, first_chunk_ms if first_chunk_ms is not None else -1.0,
            chunk_count, characters, self.input_text[:80],
        )

        on_metrics = self._piper_tts._on_metrics
        if on_metrics:
            try:
                on_metrics({
                    "stage": "tts",
                    "duration_ms": duration_ms,
                    "first_chunk_ms": first_chunk_ms,
                    "chunks": chunk_count,
                    "characters": characters,
                    "text": self.input_text[:200],
                })
            except Exception:
                pass


class PiperTTS(tts.TTS):
    """Piper (rhasspy/piper) ONNX voice wrapped as a LiveKit `TTS` plugin.

    Non-streaming: one synthesis pass per utterance, 16-bit PCM
    chunks pushed to the framework's audio emitter. The model file is
    resolved at construction time; a missing file raises rather than
    silently failing later.
    """

    def __init__(
        self,
        *,
        model_path: str | Path,
        use_cuda: bool = False,
        speaker_id: int | None = None,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        noise_w_scale: float = 0.8,
        on_metrics: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        resolved_model_path = Path(model_path).expanduser().resolve()
        if not resolved_model_path.exists():
            raise FileNotFoundError(
                f"Piper model not found: {resolved_model_path}. "
                "Set LOCAL_TTS_MODEL_PATH to a valid .onnx file."
            )

        self._on_metrics = on_metrics
        self._voice = PiperVoice.load(
            model_path=resolved_model_path,
            use_cuda=use_cuda,
        )
        self._opts = PiperSynthesisOptions(
            speaker_id=speaker_id,
            length_scale=length_scale,
            noise_scale=noise_scale,
            noise_w_scale=noise_w_scale,
        )
        self._model_path = str(resolved_model_path)

        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=self._voice.config.sample_rate,
            num_channels=1,
        )

    @property
    def model(self) -> str:
        return self._model_path

    @property
    def provider(self) -> str:
        return "local-piper"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return PiperChunkedStream(
            piper_tts=self,
            input_text=text,
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        return None
