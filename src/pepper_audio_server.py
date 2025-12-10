#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

import socket
import struct
import os
import time
import array
import sys

from bridge import connect_session, DEFAULT_QI_URL, to_text

TARGET_RATE = 48000
TCP_HOST = "127.0.0.1"
TCP_PORT = 55555
PEPPER_CHUNK_LIMIT = 16384   # max frames for sendRemoteBufferToOutput


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
    samples = array.array('h', raw_mono)  # 'h' = signed short
    stereo = array.array('h')
    for s in samples:
        stereo.append(s)
        stereo.append(s)
    return stereo.tostring()  # Python 2

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

    # TCP server: receive mono 48kHz PCM from Python 3 process
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(1)
    print("[pepper_audio] Waiting for Python3 client on %s:%d..." % (TCP_HOST, TCP_PORT))

    conn, addr = server.accept()
    print("[pepper_audio] Client connected:", addr)

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

            # mono int16 -> stereo int16 interleaved
            stereo = mono16_to_stereo16(chunk)

            nb_frames = len(stereo) // 4  # 2 channels * 2 bytes
            if nb_frames > PEPPER_CHUNK_LIMIT:
                stereo = stereo[:PEPPER_CHUNK_LIMIT * 4]
                nb_frames = PEPPER_CHUNK_LIMIT

            audio.sendRemoteBufferToOutput(nb_frames, stereo)

            # ~20ms pacing; sender is already chunked, this avoids tight spin
            time.sleep(0.02)
    finally:
        conn.close()
        server.close()
        print("[pepper_audio] server shut down")


if __name__ == "__main__":
    main()
