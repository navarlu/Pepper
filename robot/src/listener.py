import asyncio
from livekit import rtc
import sounddevice as sd  # for playback; you can replace with your own pipeline

LIVEKIT_URL = "ws://127.0.0.1:7880"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJuYW1lIjoibGlzdGVuZXItcHl0aG9uIiwidmlkZW8iOnsicm9vbUpvaW4iOnRydWUsInJvb20iOiJ0ZXN0LXJvb20tMTg3OTVjZmZjNGQwNTY1MzBiZDFjNjI2IiwiY2FuUHVibGlzaCI6dHJ1ZSwiY2FuU3Vic2NyaWJlIjp0cnVlLCJjYW5QdWJsaXNoRGF0YSI6dHJ1ZSwiYWdlbnQiOnRydWV9LCJzdWIiOiJsaXN0ZW5lci1weXRob24iLCJpc3MiOiJkZXZrZXkiLCJuYmYiOjE3NjM1NDI5ODIsImV4cCI6MTc2MzU2NDU4Mn0.xWDZgGnmL1o_Nvs5lS0l41hWpP3X8LjPZwqNFkr_aNw"
async def main():
    room = rtc.Room()

    await room.connect(LIVEKIT_URL, TOKEN)
    print("Connected to room:", room.name)

    @room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        print(f"Subscribed to track: {publication.sid} from {participant.identity}")

        # 1) If identity does NOT start with "agent", unsubscribe immediately
        if not participant.identity.startswith("agent"):
            print(f"Ignoring and unsubscribing from {participant.identity}")
            try:
                publication.set_subscribed(False)
            except Exception as e:
                print(f"Unsubscribe failed: {e}")
            return

        # 2) Only handle audio for the agent
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        audio_stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=16000,
            num_channels=1,
        )

        async def playback_task():
            import sounddevice as sd
            with sd.RawOutputStream(
                samplerate=16000,
                channels=1,
                dtype="int16",
                blocksize=0,
            ) as stream:
                async for event in audio_stream:
                    frame = event.frame
                    stream.write(bytes(frame.data))

        asyncio.create_task(playback_task())

    try:
        while True:
            await asyncio.sleep(1)
    finally:
        await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
