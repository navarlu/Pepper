r"""Interactive chat console for the benchmark agent.

Talks to the same one-tool agent the benchmark uses (same SYSTEM_PROMPT +
tools, same Responses-API loop in agent.py). The answer is **streamed
token-by-token** as it arrives, and every tool call + returned payload is
printed. Handy for eyeballing behavior before scaling the item set.

Defaults: gpt-5.4-nano, reasoning effort "none" (thinking off).

Run:  docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\chat.py

In-chat commands:
  /model <name>    switch model (resets the conversation)
  /effort <level>  none | low | medium | high | xhigh (resets)
  /reset           clear the conversation, keep model + effort
  /tools           list the available tools
  /help            show this help
  /exit, /quit     leave
"""
import json

from openai import OpenAI

import config
from agent import TOOLS, new_state, run_turn

if config.MOVE_BODY_MODE == "inline":
    from tools.move_body import InlineGestureParser, play_animation

DEFAULT_MODEL = config.MODELS[1]  # gpt-5.4-nano
DEFAULT_EFFORT = "none"           # thinking off


def handle_turn(client, model, state, user_text, effort):
    """Run one user turn: print tool calls, stream the answer live, then stats.

    In inline gesture mode the streamed text is filtered through an
    InlineGestureParser: [AnimationName] tags fire play_animation the moment
    they complete and are shown as a ⟦name -> movement⟧ marker at their
    position in the answer.
    """
    started = {"text": False}

    def ensure_header():
        if not started["text"]:
            print("\npepper> ", end="", flush=True)
            started["text"] = True

    def on_tool(name, args, result):
        arg_str = ", ".join(f"{k}={v!r}" for k, v in args.items())
        print(f"  [tool] {name}({arg_str})")
        print(f"  [tool] -> {json.dumps(result, ensure_ascii=False)}")

    parser = None
    if config.MOVE_BODY_MODE == "inline":
        def on_gesture(name):
            ensure_header()
            play_animation(name)
            print(f"⟦{name}⟧", end="", flush=True)

        parser = InlineGestureParser(on_gesture)

    def on_text(delta):
        if parser is not None:
            delta = parser.feed(delta)
            if not delta:
                return
        ensure_header()
        print(delta, end="", flush=True)

    try:
        trace = run_turn(client, model, user_text, state,
                         on_tool=on_tool, on_text=on_text, effort=effort)
    except Exception as e:  # keep the REPL alive on API errors
        print(f"\n  [error] API call failed: {e}")
        return

    if parser is not None:
        tail = parser.flush()
        if tail:
            ensure_header()
            print(tail, end="", flush=True)

    if started["text"]:
        print()  # end the streamed answer line
    elif trace["final_text"]:
        text = trace["final_text"]
        if parser is not None:
            text = parser.feed(text) + parser.flush()
        print(f"\npepper> {text}")

    if parser is not None:
        for candidate, suggestions in parser.misses:
            hint = f" — closest: {', '.join(suggestions)}" if suggestions else ""
            print(f"  [move] unresolved tag [{candidate}]{hint}")

    ttft = trace["ttft_answer_ms"]
    gen = trace["gen_ttft_ms"]
    ttft_s = f"{ttft:.0f}ms" if ttft is not None else "n/a"
    gen_s = f"{gen:.0f}ms" if gen is not None else "n/a"
    print(
        f"  [{model} | effort={trace['effort']} | "
        f"{trace['input_tokens']}+{trace['output_tokens']} tok (in+out) "
        f"| TTFT {ttft_s} (gen {gen_s}) | total {trace['total_ms']:.0f}ms]"
    )


def main():
    if not config.OPENAI_API_KEY:
        raise SystemExit("Set OPENAI_API_KEY in the environment (or a .env file).")

    client = OpenAI(api_key=config.OPENAI_API_KEY)
    model = DEFAULT_MODEL
    effort = DEFAULT_EFFORT
    state = new_state()

    print("ReceptionistBench chat. Type /help for commands, /exit to quit.")
    print(f"Model: {model}   Effort: {effort}   Tools: {', '.join(TOOLS)}\n")

    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not user:
            continue

        if user.startswith("/"):
            parts = user.split(maxsplit=1)
            cmd = parts[0].lower()
            if cmd in ("/exit", "/quit"):
                print("bye")
                return
            if cmd == "/reset":
                state = new_state()
                print("  [conversation reset]")
                continue
            if cmd == "/tools":
                for name, (_fn, schema) in TOOLS.items():
                    print(f"  {name}: {schema['function']['description']}")
                continue
            if cmd == "/model":
                if len(parts) < 2:
                    print(f"  current model: {model}   (available: {', '.join(config.MODELS)})")
                else:
                    model = parts[1].strip()
                    state = new_state()
                    print(f"  [model set to {model}, conversation reset]")
                continue
            if cmd == "/effort":
                if len(parts) < 2:
                    print(f"  current effort: {effort}   (none|low|medium|high|xhigh)")
                else:
                    effort = parts[1].strip()
                    state = new_state()
                    print(f"  [effort set to {effort}, conversation reset]")
                continue
            if cmd == "/help":
                print("  /model <name>    switch model (resets)     /reset  clear chat")
                print("  /effort <level>  none|low|medium|high|xhigh  /tools  list tools")
                print("  /exit            quit")
                continue
            print(f"  [unknown command {cmd}; /help for commands]")
            continue

        handle_turn(client, model, state, user, effort)


if __name__ == "__main__":
    main()
