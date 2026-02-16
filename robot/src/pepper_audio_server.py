#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

from collections import deque
import socket
import struct
import os
import sys
import audioop
import time

from bridge import connect_session, DEFAULT_QI_URL, to_text

_ALLOWED_RATES = set([16000, 22050, 44100, 48000])


def _resolve_stream_rate():
    raw = int(os.environ.get("PEPPER_STREAM_RATE", "16000"))
    if raw not in _ALLOWED_RATES:
        print("[pepper_audio] Unsupported PEPPER_STREAM_RATE=", raw, "fallback to 16000")
        return 16000
    return raw


TARGET_RATE = _resolve_stream_rate()
TCP_HOST = "127.0.0.1"
TCP_PORT = 55555
PEPPER_CHUNK_LIMIT = 16384   # max frames for sendRemoteBufferToOutput
DEFAULT_OUTPUT_VOLUME = int(os.environ.get("PEPPER_OUTPUT_VOLUME", "55"))
PLAYBACK_BATCH_FRAMES = int(os.environ.get("PEPPER_PLAYBACK_BATCH_FRAMES", "1600"))
MAX_BUFFER_FRAMES = int(os.environ.get("PEPPER_MAX_BUFFER_FRAMES", "19200"))


def recv_all(conn, size):
    """Receive exactly size bytes from conn or None on EOF."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def mono16_to_stereo16(raw_mono):
    """
    raw_mono: bytes, int16 mono.
    Return bytes, int16 stereo interleaved (L,R,L,R,...).
    """
    # Use C-optimized conversion to avoid Python-loop jitter.
    return audioop.tostereo(raw_mono, 2, 1, 1)

def main():
    qi_url = os.environ.get("PEPPER_URL") or DEFAULT_QI_URL
    print("[pepper_audio] Python version:", sys.version)
    print("[pepper_audio] Connecting to Pepper:", qi_url)

    sess = connect_session(qi_url)
    audio = sess.service("ALAudioDevice")

    try:
        audio.openAudioOutputs()
    except Exception as e:
        print("[pepper_audio] openAudioOutputs warning:", to_text(e))

    try:
        audio.setParameter("outputSampleRate", TARGET_RATE)
        print("[pepper_audio] set outputSampleRate to", TARGET_RATE)
    except Exception as e:
        print("[pepper_audio] setParameter warning:", to_text(e))

    try:
        audio.setOutputVolume(DEFAULT_OUTPUT_VOLUME)
        current_volume = audio.getOutputVolume()
        print("[pepper_audio] output volume set to", current_volume)
    except Exception as e:
        print("[pepper_audio] setOutputVolume warning:", to_text(e))

    if PLAYBACK_BATCH_FRAMES > PEPPER_CHUNK_LIMIT:
        print(
            "[pepper_audio] PLAYBACK_BATCH_FRAMES too high, clamping to",
            PEPPER_CHUNK_LIMIT,
        )
    batch_frames = min(PLAYBACK_BATCH_FRAMES, PEPPER_CHUNK_LIMIT)
    batch_bytes = batch_frames * 4  # int16 stereo => 4 bytes per frame
    max_buffer_frames = max(MAX_BUFFER_FRAMES, batch_frames)
    max_buffer_bytes = max_buffer_frames * 4
    send_warn_threshold_ms = (float(batch_frames) / float(TARGET_RATE)) * 2000.0
    print(
        "[pepper_audio] buffering:",
        "batch_frames=", batch_frames,
        "max_buffer_frames=", max_buffer_frames,
    )

    # TCP server: receive mono 48kHz PCM from Python 3 process
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(1)
    print("[pepper_audio] Waiting for Python3 client on %s:%d..." % (TCP_HOST, TCP_PORT))

    conn, addr = server.accept()
    print("[pepper_audio] Client connected:", addr)
    frames_sent_total = 0
    recv_chunks_total = 0
    send_calls_total = 0
    last_chunk_ts = time.time()
    recv_intervals_ms_sum = 0.0
    send_durations_ms_sum = 0.0
    max_recv_interval_ms = 0.0
    max_send_duration_ms = 0.0
    dropped_frames_total = 0
    queued_bytes = 0
    stereo_queue = deque()

    try:
        while True:
            # Read 4-byte length header
            header = recv_all(conn, 4)
            if not header:
                print("[pepper_audio] client disconnected (no header)")
                break

            size = struct.unpack(">I", header)[0]

            # Sanity check
            if size <= 0 or size > 2 ** 20:
                print("[pepper_audio] invalid size:", size)
                break

            # Read 'size' bytes of mono PCM
            chunk = recv_all(conn, size)
            if not chunk:
                print("[pepper_audio] client disconnected (no chunk)")
                break
            now_ts = time.time()
            recv_interval_ms = (now_ts - last_chunk_ts) * 1000.0
            last_chunk_ts = now_ts

            # mono int16 -> stereo int16 interleaved
            stereo = mono16_to_stereo16(chunk)

            nb_frames = len(stereo) // 4  # 2 channels * 2 bytes
            if nb_frames > PEPPER_CHUNK_LIMIT:
                stereo = stereo[:PEPPER_CHUNK_LIMIT * 4]
                nb_frames = PEPPER_CHUNK_LIMIT

            stereo_queue.append(stereo)
            queued_bytes += len(stereo)
            recv_chunks_total += 1
            recv_intervals_ms_sum += recv_interval_ms
            if recv_interval_ms > max_recv_interval_ms:
                max_recv_interval_ms = recv_interval_ms

            if queued_bytes > max_buffer_bytes:
                overflow_bytes = queued_bytes - max_buffer_bytes
                dropped_bytes = 0
                while stereo_queue and dropped_bytes < overflow_bytes:
                    head = stereo_queue[0]
                    need = overflow_bytes - dropped_bytes
                    if len(head) <= need:
                        dropped_bytes += len(head)
                        queued_bytes -= len(head)
                        stereo_queue.popleft()
                    else:
                        stereo_queue[0] = head[need:]
                        dropped_bytes += need
                        queued_bytes -= need
                        break

                dropped_frames = dropped_bytes // 4
                dropped_frames_total += dropped_frames
                try:
                    audio.flushAudioOutputs()
                except Exception:
                    pass
                print(
                    "[pepper_audio] WARNING buffer overflow:",
                    "dropped_frames=", dropped_frames,
                    "dropped_frames_total=", dropped_frames_total,
                    "buffered_frames=", queued_bytes // 4,
                )

            while queued_bytes >= batch_bytes:
                need_bytes = batch_bytes
                parts = []
                while need_bytes > 0 and stereo_queue:
                    head = stereo_queue[0]
                    if len(head) <= need_bytes:
                        parts.append(head)
                        need_bytes -= len(head)
                        queued_bytes -= len(head)
                        stereo_queue.popleft()
                    else:
                        parts.append(head[:need_bytes])
                        stereo_queue[0] = head[need_bytes:]
                        queued_bytes -= need_bytes
                        need_bytes = 0

                payload = "".join(parts)
                send_start_ts = time.time()
                audio.sendRemoteBufferToOutput(batch_frames, payload)
                send_duration_ms = (time.time() - send_start_ts) * 1000.0
                send_calls_total += 1
                frames_sent_total += batch_frames
                send_durations_ms_sum += send_duration_ms
                if send_duration_ms > max_send_duration_ms:
                    max_send_duration_ms = send_duration_ms
                if send_calls_total == 1:
                    print(
                        "[pepper_audio] First playback batch sent:",
                        "batch_frames=", batch_frames,
                        "recv_interval_ms=", round(recv_interval_ms, 2),
                        "send_duration_ms=", round(send_duration_ms, 2),
                    )
                if send_duration_ms > send_warn_threshold_ms:
                    print(
                        "[pepper_audio] WARNING slow sendRemoteBufferToOutput:",
                        "send_duration_ms=", round(send_duration_ms, 2),
                        "batch_frames=", batch_frames,
                    )

            if recv_chunks_total == 1:
                print(
                    "[pepper_audio] First chunk received:",
                    "bytes=", len(chunk),
                    "frames=", nb_frames,
                    "recv_interval_ms=", round(recv_interval_ms, 2),
                )
            elif recv_chunks_total % 200 == 0:
                avg_recv_interval_ms = recv_intervals_ms_sum / float(recv_chunks_total)
                avg_send_duration_ms = (
                    send_durations_ms_sum / float(send_calls_total)
                    if send_calls_total
                    else 0.0
                )
                print(
                    "[pepper_audio] stream heartbeat:",
                    "recv_chunks=", recv_chunks_total,
                    "send_calls=", send_calls_total,
                    "frames_total=", frames_sent_total,
                    "buffered_frames=", queued_bytes // 4,
                    "dropped_frames_total=", dropped_frames_total,
                    "avg_recv_interval_ms=", round(avg_recv_interval_ms, 2),
                    "avg_send_duration_ms=", round(avg_send_duration_ms, 2),
                    "max_recv_interval_ms=", round(max_recv_interval_ms, 2),
                    "max_send_duration_ms=", round(max_send_duration_ms, 2),
                )

            # No explicit sleep: recv_all() already blocks on incoming real-time chunks.
            # Additional sleeps can cause underruns/overruns and "freeze then catch-up".
    finally:
        conn.close()
        server.close()
        print("[pepper_audio] server shut down")


if __name__ == "__main__":
    main()
