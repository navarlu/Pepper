"""Example team's tools — same format as teaching/tool-playground/tools.py."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


# ─── Tool 1: get_weather ─────────────────────────────────────────────────

def get_weather(city: str) -> str:
    fake_db = {
        "prague": "18°C, sunny, light wind from the west",
        "tokyo": "23°C, humid, scattered clouds",
        "reykjavik": "4°C, overcast, chance of rain",
    }
    return fake_db.get(
        city.lower(),
        f"No data for {city}. Try Prague, Tokyo, or Reykjavik.",
    )


GET_WEATHER_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get the current weather for a given city. "
            "Use whenever the user asks about weather, temperature, "
            "or what it's like outside in a specific place."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name, e.g. 'Prague' or 'Tokyo'.",
                },
            },
            "required": ["city"],
            "additionalProperties": False,
        },
    },
}


# ─── Tool 2: get_current_time ────────────────────────────────────────────

def get_current_time() -> str:
    now = datetime.now(ZoneInfo("Europe/Prague"))
    return now.strftime("%H:%M on %A, %B %d, %Y (Prague time)")


GET_CURRENT_TIME_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": (
            "Get the current Prague time and date. Use whenever the "
            "user asks what time it is or what today's date is."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
}


# ─── Registry ────────────────────────────────────────────────────────────

TOOLS = [
    {"schema": GET_WEATHER_SCHEMA,      "function": get_weather},
    {"schema": GET_CURRENT_TIME_SCHEMA, "function": get_current_time},
]
