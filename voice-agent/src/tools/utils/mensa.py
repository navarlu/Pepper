"""Fetch and parse the Charles Square Food Counter menu from Agata.

The Agata page at https://agata.suz.cvut.cz/jidelnicky/indexTyden.php
publishes one week per URL. The base URL (without `clTyden`) lists the
weeks that are currently available; we follow each link, parse out the
day cards, and return a flat list covering this week plus next week.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

MENSA_BASE_URL = (
    "https://agata.suz.cvut.cz/jidelnicky/indexTyden.php?clPodsystem=8&lang=en"
)
MENSA_NAME = "Charles Square Food Counter"
_USER_AGENT = "Pepper-FEE-agent/1.0"
_REQUEST_TIMEOUT_S = 8.0
_DATE_RE = re.compile(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
# Strip leading portion weight: "120 g Spaghetti..." → "Spaghetti..."
# Also handles "1 kg ...", "200g ..." (no space), and trailing "(120 g)".
_WEIGHT_PREFIX_RE = re.compile(r"^\s*\d+(?:[.,]\d+)?\s*(?:g|kg|ks|ml|l)\b\s*", re.IGNORECASE)
_WEIGHT_PAREN_RE = re.compile(r"\s*\(\s*\d+(?:[.,]\d+)?\s*(?:g|kg|ks|ml|l)\s*\)", re.IGNORECASE)
_WEEKDAYS_EN = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}

# Map Agata category labels (English page) to short, voice-friendly tags.
# Anything unknown falls through as the lowercased original.
_CATEGORY_TAGS = {
    "soups": "soup",
    "soup": "soup",
    "main dishes": "main",
    "main dish": "main",
    "salads": "salad",
    "salad": "salad",
    "vegetarian": "vegetarian",
    "vegetarian dishes": "vegetarian",
    "vegetarian food": "vegetarian",
    "meals without meat": "vegetarian",
    "speciality of the day": "special",
    "specialty of the day": "special",
    "snacks": "snack",
    "snack": "snack",
    "desserts": "dessert",
    "dessert": "dessert",
    "pasta": "pasta",
    "pizza": "pizza",
}


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _strip_portion(name: str) -> str:
    """Drop portion weights from a dish name so the agent doesn't read
    out 'one hundred twenty grams' over voice."""
    cleaned = _WEIGHT_PREFIX_RE.sub("", name or "")
    cleaned = _WEIGHT_PAREN_RE.sub("", cleaned)
    return cleaned.strip().lstrip("-,").strip()


def _parse_czech_date(text: str) -> str:
    match = _DATE_RE.search(text or "")
    if not match:
        return ""
    day, month, year = (int(part) for part in match.groups())
    return datetime(year, month, day).date().isoformat()


def _english_weekday(date_iso: str, fallback: str) -> str:
    if not date_iso:
        return fallback
    try:
        return _WEEKDAYS_EN[datetime.fromisoformat(date_iso).date().weekday()]
    except ValueError:
        return fallback


def _category_tag(raw: str) -> str:
    key = (raw or "").strip().lower()
    return _CATEGORY_TAGS.get(key, key)


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": _USER_AGENT})
    with urlopen(req, timeout=_REQUEST_TIMEOUT_S) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, "replace")


def _extract_week_urls(html: str, base_url: str) -> list[str]:
    """Pull the list of `?clTyden=NNNN` links the page advertises.
    The hrefs are query-only ('?clPodsystem=8&clTyden=4145&lang=en'),
    so we resolve them against the full base URL — including the
    `indexTyden.php` script name — to keep that path intact."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        if "clTyden=" not in a["href"]:
            continue
        full = urljoin(base_url, a["href"])
        if full not in seen:
            seen.add(full)
            urls.append(full)
    return urls


def _parse_days(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    days: list[dict[str, Any]] = []
    for card in soup.select(".card"):
        header_el = card.select_one(".card-header")
        if header_el is None:
            continue
        rows = card.select(".menu-row")
        if not rows:
            continue

        header = _clean(header_el.get_text(" ", strip=True))
        date_iso = _parse_czech_date(header)
        weekday = _english_weekday(date_iso, header)

        dishes: list[dict[str, str]] = []
        for row in rows:
            type_el = row.select_one(".menu-type")
            name_el = row.select_one(".menu-name")
            if name_el is None:
                continue
            category = _category_tag(
                _clean(type_el.get_text(" ", strip=True)) if type_el else ""
            )
            name = _strip_portion(_clean(name_el.get_text(" ", strip=True)))
            if name:
                dishes.append({"category": category, "name": name})

        if dishes:
            days.append({
                "weekday": weekday,
                "date": date_iso,
                "dishes": dishes,
            })
    return days


def fetch_mensa_menu() -> dict[str, Any]:
    """Return all currently published days for the Charles Square Food
    Counter (typically this week + next week)."""
    base_html = _fetch(MENSA_BASE_URL)
    week_urls = _extract_week_urls(base_html, MENSA_BASE_URL)

    all_days: list[dict[str, Any]] = []
    for url in week_urls:
        try:
            week_html = _fetch(url)
        except Exception:
            continue
        all_days.extend(_parse_days(week_html))

    # Dedupe by date and sort chronologically; days without an iso date
    # (parsing edge case) are dropped to keep the structure clean.
    seen_dates: set[str] = set()
    unique: list[dict[str, Any]] = []
    for day in sorted(all_days, key=lambda d: d.get("date") or ""):
        date_iso = day.get("date")
        if not date_iso or date_iso in seen_dates:
            continue
        seen_dates.add(date_iso)
        unique.append(day)

    return {"canteen": MENSA_NAME, "days": unique}
