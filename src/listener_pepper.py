#!/usr/bin/env python
# -*- coding: utf-8 -*-

import asyncio
import os
import audioop

from livekit import rtc

from bridge import connect_session, DEFAULT_QI_URL, to_text

# -------------------------------------------------------------------
# LiveKit config
# -------------------------------------------------------------------

LIVEKIT_URL = "ws://127.0.0.1:7880"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoibGlzdGVuZXItcHl0aG9uIiwidmlkZW8iOnsicm9vbUpvaW4iOnRydWUsInJvb20iOiJ0ZXN0LXJvb20tMTg3OTVmNDU4YzUzMWVkODQ4YjA5YmI3IiwiY2FuUHVibGlzaCI6dHJ1ZSwiY2FuU3Vic2NyaWJlIjp0cnVlLCJjYW5QdWJsaXNoRGF0YSI6dHJ1ZSwiYWdlbnQiOnRydWV9LCJzdWIiOiJsaXN0ZW5lci1weXRob24iLCJpc3MiOiJkZXZrZXkiLCJuYmYiOjE3NjM1NDU0ODEsImV4cCI6MTc2MzU2NzA4MX0.nHOD_HhgnDVMDS89j-l7U6SRN3D-LMnRx7Ds4Cmo-2k"
# -------------------------------------------------------------------
# Pepper audio config
# -------------------------------------------------------------------

TARGET_RATE = 48000      # Pepper output sample rate
ATTENUATION = 0.4        # 1.0 = original loudness, <1.0 = quieter
PEPPER_CHUNK_LIMIT = 16384  # max frames for sendRemoteBufferToOutput


def setup_pepper_audio(qi_url=None):
    """
    Connect to Pepper via qi and return an ALAudioDevice instance.
    """
    qi_url = qi_url or os.environ.get("PEPPER_URL") or DEFAULT_QI_URL
    print("[listener] Connecting to Pepper at:", qi_url)

    sess = connect_session(qi_url)
    audio = sess.service("ALAudioDevice")

    # Try to make sure outputs are ready and sample rate is correct
    try:
        audio.openAudioOutputs()
    except Exception as e:
        print("[listener] openAudioOutputs warning:", to_text(e))

    try:
        audio.setParameter("outputSampleRate", TARGET_RATE)
        print("[listener] set outputSampleRate to", TARGET_RATE)
    except Exception as e:
        print("[listener] setParameter('outputSampleRate') warning:", to_text(e))

    return audio


def stream_frame_to_pepper(audio_dev, frame, state_container):
    """
    Take a livekit rtc.AudioFrame and stream it to Pepper via ALAudioDevice.

    state_container: dict holding "state" for audioop.ratecv across frames.
    """
    # LiveKit AudioFrame properties
    rate = frame.sample_rate         # int
    nch = frame.num_channels         # int
    # frame.data is int16 interleaved; treat as raw bytes
    raw = bytes(frame.data)

    if not raw:
        return

    # Ensure 16-bit samples (audioop assumes that)
    sampwidth = 2  # int16

    # Downmix to mono if needed
    if nch == 1:
        mono = raw
    elif nch >= 2:
        # tomono expects interleaved channels
        mono = audioop.tomono(raw, sampwidth, 0.5, 0.5)
    else:
        # Fallback: treat as mono
        mono = raw

    # Resample to TARGET_RATE if needed
    state = state_container.get("state")
    if rate != TARGET_RATE:
        mono, state = audioop.ratecv(
            mono, sampwidth, 1, rate, TARGET_RATE, state
        )
        state_container["state"] = state

    # Apply attenuation
    if ATTENUATION != 1.0:
        mono = audioop.mul(mono, sampwidth, ATTENUATION)

    # Convert mono -> stereo interleaved
    stereo = audioop.tostereo(mono, sampwidth, 1, 1)

    # Each frame = 2 channels * 2 bytes = 4 bytes
    nb_frames = len(stereo) // 4

    # Safety: Pepper expects nb_frames <= PEPPER_CHUNK_LIMIT
    if nb_frames > PEPPER_CHUNK_LIMIT:
        stereo = stereo[:PEPPER_CHUNK_LIMIT * 4]
        nb_frames = PEPPER_CHUNK_LIMIT

    # sendRemoteBufferToOutput(nbOfFrames, buffer)
    audio_dev.sendRemoteBufferToOutput(nb_frames, stereo)


async def main():
    # Connect to Pepper audio first
    pepper_audio = setup_pepper_audio()

    room = rtc.Room()
    await room.connect(LIVEKIT_URL, TOKEN)
    print("Connected to room:", room.name)

    @room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        print("[listener] track_subscribed:", publication.sid, "kind=", track.kind)

        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        audio_stream = rtc.AudioStream(track)

        async def pepper_playback_task():
            # state for audioop.ratecv (kept across frames)
            rate_state = {"state": None}

            async for event in audio_stream:
                frame = event.frame
                stream_frame_to_pepper(pepper_audio, frame, rate_state)

        asyncio.create_task(pepper_playback_task())

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await room.disconnect()
        print("[listener] disconnected from room")


if __name__ == "__main__":
    asyncio.run(main())
