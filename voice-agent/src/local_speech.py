import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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


class FasterWhisperSTT(stt.STT):
    def __init__(
        self,
        *,
        model: str = "small",
        language: str = "en",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
    ) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
            )
        )
        self._model_name = model
        self._language = language
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
        language: Optional[str],
    ) -> tuple[str, str]:
        segments, info = self._model.transcribe(
            audio_16k,
            language=language,
            beam_size=1,
            best_of=1,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        detected_language = info.language or (language or self._language)
        return text, detected_language

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

        requested_language: Optional[str] = None
        if language is not NOT_GIVEN:
            requested_language = language
        elif self._language:
            requested_language = self._language

        text, detected_language = await asyncio.to_thread(
            self._recognize_sync,
            audio_16k,
            requested_language,
        )

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

    def _synthesize_sync(self, text: str) -> list[bytes]:
        chunks = self._piper_tts._voice.synthesize(
            text,
            syn_config=SynthesisConfig(
                speaker_id=self._piper_tts._opts.speaker_id,
                length_scale=self._piper_tts._opts.length_scale,
                noise_scale=self._piper_tts._opts.noise_scale,
                noise_w_scale=self._piper_tts._opts.noise_w_scale,
            ),
        )
        return [chunk.audio_int16_bytes for chunk in chunks]

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=str(uuid.uuid4()),
            sample_rate=self._piper_tts.sample_rate,
            num_channels=self._piper_tts.num_channels,
            mime_type="audio/raw",
        )
        pcm_chunks = await asyncio.to_thread(self._synthesize_sync, self.input_text)
        for chunk in pcm_chunks:
            output_emitter.push(chunk)
        output_emitter.flush()


class PiperTTS(tts.TTS):
    def __init__(
        self,
        *,
        model_path: str | Path,
        use_cuda: bool = False,
        speaker_id: int | None = None,
        length_scale: float = 1.0,
        noise_scale: float = 0.667,
        noise_w_scale: float = 0.8,
    ) -> None:
        resolved_model_path = Path(model_path).expanduser().resolve()
        if not resolved_model_path.exists():
            raise FileNotFoundError(
                f"Piper model not found: {resolved_model_path}. "
                "Set CASCADE_TTS_MODEL_PATH to a valid .onnx file."
            )

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
