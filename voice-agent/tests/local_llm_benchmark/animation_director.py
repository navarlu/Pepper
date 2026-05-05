"""AnimationDirector: pick a body animation for every agent reply by
running a forced one-tool LLM pass that REUSES the same chat history
the agent has been building. Same LLM, same context — we just append a
short picker instruction and force `tool_choice` to play_animation.

Wired from `livekit_console.py` to `session.on("conversation_item_added")`.
When an assistant message lands in chat_ctx, the director:
  1. Copies the agent's current ChatContext (so we don't mutate it).
  2. Appends a small user-role instruction telling the LLM to pick an
     animation matching the meaning of its last reply.
  3. Calls `session.llm.chat(...)` with `tools=[play_animation]` and
     `tool_choice` forcing that tool. vLLM with `--tool-call-parser
     llama3_json` honors this and emits exactly one tool call.
  4. Dispatches the chosen animation through `trigger_animation`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from livekit.agents import llm

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.tools import trigger_animation  # noqa: E402

# Reuse the same play_animation tool object the LLM would have seen if
# we'd left it in LIVEKIT_TOOLS — its docstring + arg schema describe
# the choice space already.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools.play_animation import play_animation  # noqa: E402

logger = logging.getLogger("animation-director")

_ANIMATION_GROUPS = ("greeting", "bow", "explain", "happy", "thinking", "dont_know")

PICKER_INSTRUCTION = (
    "Now read ONLY your most recent assistant reply above (ignore the "
    "rest of the conversation) and pick the body animation that "
    "matches THAT reply's mood. Each option below is equally likely; "
    "read literally what your last sentence does.\n"
    "  - greeting: the reply contains 'hello', 'hi', 'welcome', "
    "'good morning' (any opening salutation).\n"
    "  - bow: the reply is a sign-off — 'goodbye', 'see you', "
    "'have a nice day', 'come back anytime', 'thanks'.\n"
    "  - happy: the reply is an enthusiastic / playful / affirming "
    "reaction — 'I'm glad', 'great!', 'of course!', responses to "
    "jokes or compliments.\n"
    "  - thinking: the reply asks the user a clarifying question "
    "(ends with '?' and is asking for missing info) or says 'let me "
    "check'.\n"
    "  - dont_know: the reply apologises or admits a limit — 'sorry', "
    "'I couldn't find', 'I don't have that information'.\n"
    "  - explain: ONLY if none of the above fit and the reply is "
    "delivering plain factual content (a phone number, a route, a "
    "schedule, a description).\n"
    "Call play_animation exactly once. Don't pick explain by default — "
    "scan greeting/bow/happy/thinking/dont_know first."
)


class AnimationDirector:
    """One forced LLM pass per assistant message, reusing the agent's
    own chat history."""

    def __init__(
        self,
        llm_instance,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._llm = llm_instance
        self._timeout = request_timeout_s
        self._inflight: asyncio.Task | None = None

    def schedule(self, chat_ctx: llm.ChatContext) -> None:
        """Fire-and-forget entry. Cancels any earlier in-flight pick so
        we don't fire stale animations on rapid turn-taking."""
        if chat_ctx is None:
            return
        if self._inflight and not self._inflight.done():
            self._inflight.cancel()
        # Snapshot the items now so later mutations don't race us.
        snapshot = self._snapshot_ctx(chat_ctx)
        self._inflight = asyncio.create_task(self._pick_and_fire(snapshot))

    @staticmethod
    def _snapshot_ctx(src: llm.ChatContext) -> llm.ChatContext:
        """Build a fresh ChatContext containing the same items + our
        picker instruction at the end. Avoids mutating the agent's ctx."""
        out = llm.ChatContext()
        for item in src.items:
            out.items.append(item)
        out.add_message(role="user", content=PICKER_INSTRUCTION)
        return out

    async def _pick_and_fire(self, picker_ctx: llm.ChatContext) -> None:
        try:
            t0 = time.perf_counter()
            chosen = await asyncio.wait_for(
                self._call_picker(picker_ctx),
                timeout=self._timeout,
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("animation_pick_timeout after %.1fs", self._timeout)
            return
        except Exception as exc:
            logger.warning("animation_pick_failed error=%r", exc)
            return

        if not chosen:
            logger.warning("animation_pick_empty")
            return

        logger.info("animation_pick group=%s elapsed_ms=%.0f", chosen, elapsed_ms)
        try:
            await trigger_animation(chosen)
        except Exception as exc:
            logger.warning("animation_dispatch_failed group=%s error=%r", chosen, exc)

    async def _call_picker(self, picker_ctx: llm.ChatContext) -> str | None:
        stream = self._llm.chat(
            chat_ctx=picker_ctx,
            tools=[play_animation],
            tool_choice={
                "type": "function",
                "function": {"name": "play_animation"},
            },
            parallel_tool_calls=False,
        )
        seen_text: list[str] = []
        seen_calls: list[tuple[str, str]] = []
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            if delta is None:
                continue
            if delta.content:
                seen_text.append(delta.content)
            for tc in (delta.tool_calls or []):
                seen_calls.append((tc.name or "", tc.arguments or ""))
                if tc.name != "play_animation":
                    continue
                try:
                    args = json.loads(tc.arguments or "{}")
                except json.JSONDecodeError:
                    continue
                chosen = args.get("name")
                if chosen in _ANIMATION_GROUPS:
                    return chosen
        logger.warning(
            "animation_pick_no_match seen_text=%r seen_calls=%r",
            "".join(seen_text)[:200], seen_calls[:3],
        )
        return None
