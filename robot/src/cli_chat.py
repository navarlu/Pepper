
from letta_io import (
    send_to_ego,
    append_block_text,
    send_to_ego_sse_stream,
    sanitize_letta_text,
)
import subprocess

import shutil
import os
from dotenv import load_dotenv
from pathlib import Path
DEBUG_ENV = False
# Resolve project root (two levels up from this file)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"

# Load .env from root folder
load_dotenv(dotenv_path=ENV_PATH)
if DEBUG_ENV: print(f"[ENV] Loaded .env from {ENV_PATH}")
if DEBUG_ENV: print(f"[ENV] OPENAI_API_KEY present: {'OPENAI_API_KEY' in os.environ}")
CONVERSATION_BLOCK_ID = os.getenv("CONVERSATION_BLOCK_ID")
EGO_AGENT_ID = os.getenv("EGO_AGENT_ID")
ROBOT_TARGET = (os.getenv("ROBOT_TARGET") or "real").strip().lower()
if ROBOT_TARGET not in {"real", "virtual"}:
    ROBOT_TARGET = "real"
if DEBUG_ENV: print(f"[ENV] Using CONVERSATION_BLOCK_ID={CONVERSATION_BLOCK_ID}")
if DEBUG_ENV: print(f"[ENV] Using EGO_AGENT_ID={EGO_AGENT_ID}")
import threading
import requests


def pepper_say(
    text,
    animated=True,
    language=None,
    speed=None,
    pitchShift=None,
    volume=None,
    url="http://localhost:5000/say"
):
    """
    Send text to Pepper's /say route so she speaks it aloud.
    Parameters match the JSON body expected by choregraphe_bridge_Pepper.py.
    """
    try:
        # Build JSON payload dynamically (skip None fields)
        print(f"\n\n[cli] Sending to Pepper say: {text}")
        payload = {"text": text}
        if animated is not None:
            payload["animated"] = bool(animated)
        if language:
            payload["language"] = language
        if speed is not None:
            payload["speed"] = float(speed)
        if pitchShift is not None:
            payload["pitchShift"] = float(pitchShift)
        if volume is not None:
            payload["volume"] = float(volume)

        import requests
        #r = requests.post(url, json=payload, timeout=1)
        print("Pepper silenced for testing.")
        return
        if r.status_code != 200:
            print("[cli] Pepper say error:", r.text)
        else:
            print("[cli] Pepper speaking:", text)
    except Exception as e:
        print("[cli] Could not send to Pepper:", e)

def call_superego_maybe_update():
    # Keep the path you use in your repo. If you run workers via module path, reuse that.
    run_worker("Hybrid/superego_worker.py")  # now calls maybe_update_goal_block_once()

def id_react_user_text(user_text: str):
    try:
        # Direct Python call avoids spawning a new process, but if your environment requires
        # the worker to run as a module, switch to run_worker with an argv scheme.
        from id_worker import react_to_user_text
        react_to_user_text(user_text)
    except Exception:
        # Fallback to process
        run_worker("Hybrid/id_worker.py")

def id_react_ego_start(pompt):
    try:
        from id_worker import react_to_ego_start
        react_to_ego_start(pompt)
    except Exception:
        run_worker("Hybrid/id_worker.py")

def run_worker(module: str):
    exe = shutil.which("python") or shutil.which("python3")
    if not exe:
        print(f"[cli] Python interpreter not found to run {module}.")
        return
    try:
        subprocess.run([exe, module], check=False)
    except Exception as e:
        print(f"[cli] Worker {module} error: {e}")

def main():
    print("Talk to Ego. Type 'exit' to quit.")
    while True:
        try:
            user_in = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            break

        if user_in.lower() in {"exit", "quit"}:
            break

        # 1) Log USER turn immediately
        try:
            append_block_text(CONVERSATION_BLOCK_ID, f"USER: {user_in}")
        except Exception as e:
            print(f"[cli] Warning: failed to append USER turn to conversation_log: {e}")

        # 2) Kick fast, parallel reactions:
        #    - Id reacts to the user text (listening/curious/etc.)
        #    - Superego may or may not update (LLM decides inside the worker)
        #print(f"Reacting to user text {user_in}")
        threading.Thread(target=id_react_user_text, args=(user_in,), daemon=True).start()
        #threading.Thread(target=call_superego_maybe_update, daemon=True).start()

        # 3) Stream Ego via SSE; on first chunk, trigger Id 'start' reaction
        first_chunk = True
        full_reply_parts = []
        try:
            for delta in send_to_ego_sse_stream(
                EGO_AGENT_ID,
                user_in,
                stream_tokens=True,
                include_reasoning=False,
            ):
                clean_delta = sanitize_letta_text(delta, preserve_whitespace=True)
                if not clean_delta:
                    continue
                if first_chunk:
                    first_chunk = False
                    #print("Ego: ", end="", flush=True)
                #print(clean_delta, end="", flush=True)
                full_reply_parts.append(clean_delta)
            
            # If we got no chunks at all, do a one-shot fallback
            if first_chunk:
                print("Error! No response from Ego.")
            else:
                print()  # newline after the stream
        except Exception as e:
            print(f"\n[cli] SSE streaming failed: {e}")
            reply = send_to_ego(EGO_AGENT_ID, user_in)
            print(f"Ego: {reply}")
            full_reply_parts = [reply]

        reply_text = sanitize_letta_text("".join(full_reply_parts))
        #print(f"Ego (full): {reply_text}")
# --- Sanitize trailing artifacts and any unbalanced closing quote ---
        import json

        # 1) If entire string is JSON-quoted, unquote it
        try:
            if reply_text.startswith('"') and reply_text.endswith('"'):
                reply_text = json.loads(reply_text)
        except Exception:
            pass

        # 2) Remove backslash-escaped quotes that slipped through
        reply_text = reply_text.replace('\\"', '"')
        print(f"[cli] Full Ego reply sanitized: {reply_text}")
        # 3) Remove a single trailing, *unbalanced* quote (avoid killing legit quotes)
        def _strip_trailing_unbalanced_quote(s: str) -> str:
            # Count unescaped quotes
            cnt = 0
            i = 0
            while i < len(s):
                if s[i] == '"':
                    # is it escaped?
                    esc = False
                    j = i - 1
                    while j >= 0 and s[j] == '\\':
                        esc = not esc
                        j -= 1
                    if not esc:
                        cnt += 1
                i += 1
            if cnt % 2 == 1 and s.rstrip().endswith('"'):
                # Drop exactly one trailing unescaped quote
                return s.rstrip()[:-1].rstrip()
            return s

        reply_text = _strip_trailing_unbalanced_quote(reply_text)
        if DEBUG_ENV: print(f"Reacting to ego start {reply_text}")
        threading.Thread(target=id_react_ego_start, args=(reply_text,), daemon=True).start()
        if ROBOT_TARGET == "real":
            threading.Thread(
                target=pepper_say,
                args=(reply_text,),
                kwargs={
                    "animated": True,
                    "language": "English",
                    "speed": 50,
                    "pitchShift": 0.5,
                    "volume": 0.2,
                    # "url": "http://192.168.1.42:5000/say",  # uncomment if Flask bridge runs elsewhere
                },
                daemon=True,
            ).start()
        elif DEBUG_ENV:
            print("[cli] ROBOT_TARGET set to virtual; skipping pepper_say()")
        # 4) Append EGO turn to conversation_log
        try:
            append_block_text(CONVERSATION_BLOCK_ID, f"EGO: {reply_text}")
        except Exception as e:
            print(f"[cli] Warning: failed to append EGO turn to conversation_log: {e}")

        
        # 6) If you still want the slow, end-of-turn state snapshot worker:
        #    (leave it if you like the JSON state in Letta)
        # run_worker("Hybrid/id_worker.py")

if __name__ == "__main__":
    main()
