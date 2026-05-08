"""Fetch and parse public FEE subject timetable pages."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

logger = logging.getLogger("voice-agent")

TIMETABLE_INDEX_URL = "https://intranet.fel.cvut.cz/en/education/rozvrhy-ng.B252/public/html/predmety/indexa.html"
USER_AGENT = "Pepper-FEE-agent/1.0"
GRID_START_TIME = "07:30"
GRID_MINUTES_PER_UNIT = 5
MAX_RESULTS = 24

_CODE_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,15}\b")
_CODE_TOKEN_RE = re.compile(r"[A-Z]{2,}")
_ACTIVITY_LABELS = {
    "prednaska": "lecture",
    "cviceni": "exercise",
    "laborator": "laboratory",
    "ostatni": "other",
}
_DAY_ALIASES = {
    "monday": "Monday",
    "mon": "Monday",
    "pondeli": "Monday",
    "pondělí": "Monday",
    "tuesday": "Tuesday",
    "tue": "Tuesday",
    "utery": "Tuesday",
    "úterý": "Tuesday",
    "wednesday": "Wednesday",
    "wed": "Wednesday",
    "streda": "Wednesday",
    "středa": "Wednesday",
    "thursday": "Thursday",
    "thu": "Thursday",
    "ctvrtek": "Thursday",
    "čtvrtek": "Thursday",
    "friday": "Friday",
    "fri": "Friday",
    "patek": "Friday",
    "pátek": "Friday",
}
_DAYS_EN = {
    "pondělí": "Monday",
    "úterý": "Tuesday",
    "středa": "Wednesday",
    "čtvrtek": "Thursday",
    "pátek": "Friday",
    "Monday": "Monday",
    "Tuesday": "Tuesday",
    "Wednesday": "Wednesday",
    "Thursday": "Thursday",
    "Friday": "Friday",
}


def _clean(text: str) -> str:
    return " ".join((text or "").replace("\xa0", " ").split())


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", _clean(text).lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _fetch_html(url: str) -> str:
    logger.info("timetable_fetch url=%s", url)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=10.0) as response:
        return response.read().decode(response.headers.get_content_charset() or "utf-8", "replace")


def _subject_code(text: str) -> str:
    match = _CODE_RE.search(_clean(text).upper())
    return match.group(0) if match else ""


def _code_tokens(text: str) -> list[str]:
    return [
        token for token in _CODE_TOKEN_RE.findall(_clean(text).upper())
        if len(token) >= 3
    ]


def _parse_subject_label(label: str) -> tuple[str, str]:
    cleaned = _clean(label)
    if " - " not in cleaned:
        return _subject_code(cleaned), cleaned
    name, code = cleaned.rsplit(" - ", 1)
    return _subject_code(code), name.strip()


def _load_subject_index() -> list[dict[str, str]]:
    soup = BeautifulSoup(_fetch_html(TIMETABLE_INDEX_URL), "html.parser")
    subjects: list[dict[str, str]] = []
    for link in soup.find_all("a", href=True):
        label = _clean(link.get_text(" ", strip=True))
        if " - " not in label:
            continue
        code, name = _parse_subject_label(label)
        if not code:
            continue
        subjects.append({
            "code": code,
            "name": name,
            "label": f"{code} - {name}",
            "url": urljoin(TIMETABLE_INDEX_URL, link["href"]),
        })
    return subjects


def _resolve_subject(subject: str) -> tuple[dict[str, str] | None, list[dict[str, str]]]:
    query = _clean(subject)
    query_lower = query.lower()
    query_folded = _fold(query)
    query_code = _subject_code(query)
    query_tokens = _code_tokens(query)
    subjects = _load_subject_index()

    if query_code:
        exact = [item for item in subjects if item["code"].upper() == query_code]
        if exact:
            return exact[0], []

        containing_code = [
            item for item in subjects
            if query_code in item["code"].upper()
        ]
        if len(containing_code) == 1:
            return containing_code[0], []
        if containing_code:
            return None, containing_code[:8]

    for token in query_tokens:
        token_matches = [
            item for item in subjects
            if token in item["code"].upper()
        ]
        if len(token_matches) == 1:
            return token_matches[0], []
        if token_matches:
            return None, token_matches[:8]

    contains = [
        item for item in subjects
        if query_lower in item["name"].lower()
        or query_lower in item["code"].lower()
        or query_lower in item["label"].lower()
        or query_folded in _fold(item["name"])
        or query_folded in _fold(item["label"])
    ]
    if len(contains) == 1:
        return contains[0], []
    if len(contains) > 1 and query_code and query_code.isalpha():
        practical_matches = [
            item for item in contains
            if item["code"].upper().endswith(f"{query_code}1")
        ]
        if len(practical_matches) == 1:
            return practical_matches[0], []
    if contains:
        return None, contains[:8]

    words = [word for word in query_lower.split() if len(word) > 2]
    ranked = [
        item for item in subjects
        if all(word in _fold(item["label"]) for word in words)
    ]
    if len(ranked) == 1:
        return ranked[0], []
    return None, ranked[:8]


def _time_from_units(units: int) -> str:
    base = datetime.strptime(GRID_START_TIME, "%H:%M")
    return (base + timedelta(minutes=units * GRID_MINUTES_PER_UNIT)).strftime("%H:%M")


def _format_teacher_western(raw: str) -> str:
    """Czech timetables list teachers as `Surname I.` — flip to `I. Surname`
    so the agent reads the first initial first and parses naturally."""
    parts = raw.strip().rstrip(".").split()
    if len(parts) < 2:
        return raw
    initial = parts[-1]
    if len(initial) == 1 and initial.isalpha():
        return f"{initial}. {' '.join(parts[:-1])}"
    return raw


def _class_name(cell: Any) -> str:
    classes = cell.get("class") or []
    for class_name in classes:
        if class_name in _ACTIVITY_LABELS:
            return class_name
    return ""


def _parse_event(cell: Any, day: str, start_units: int, subject: dict[str, str], week_label: str) -> dict[str, Any]:
    lines = [_clean(part) for part in cell.stripped_strings if _clean(part)]
    room = lines[0] if lines else ""
    details = lines[2:]
    capacity = ""
    if details and details[-1].startswith("("):
        capacity = details.pop().strip("()")
    group = ""
    if details and re.fullmatch(r"\d+", details[-1]):
        group = details.pop()
    raw_teachers: list[str] = []
    current: list[str] = []
    for part in details:
        if part == ",":
            if current:
                raw_teachers.append(_clean(" ".join(current)))
                current = []
        else:
            current.append(part)
    if current:
        raw_teachers.append(_clean(" ".join(current)))
    teachers = [_format_teacher_western(t) for t in raw_teachers]
    class_name = _class_name(cell)
    duration_units = int(cell.get("colspan") or 1)

    return {
        "subject_code": subject["code"],
        "subject_name": subject["name"],
        "activity": _ACTIVITY_LABELS.get(class_name, "other"),
        "day": _DAYS_EN.get(day, day),
        "day_local": day,
        "start": _time_from_units(start_units),
        "end": _time_from_units(start_units + duration_units),
        "room": room,
        "teachers": teachers,
        "group": group,
        "capacity": capacity,
        "weeks": week_label,
    }


def _week_label(table: Any) -> str:
    head = table.select_one(".head-name")
    text = _clean(head.get_text(" ", strip=True) if head else "")
    match = re.search(r"\)\s*-\s*(.+)$", text)
    return match.group(1).strip() if match else text


def _english_weeks(label: str) -> str:
    return (
        _clean(label)
        .replace("týden", "week")
        .replace("týdny", "weeks")
        .replace("týd.", "weeks")
    )


def _parse_subject_page(subject: dict[str, str]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(_fetch_html(subject["url"]), "html.parser")
    events: list[dict[str, Any]] = []
    tables = soup.select("table.timetable")
    if not tables:
        return events
    if len(tables) > 1:
        logger.info(
            "subject_page_multiple_tables subject=%s tables=%d (using first only)",
            subject.get("code", "?"), len(tables),
        )
    table = tables[0]
    week_label = _week_label(table)
    for row in table.find_all("tr"):
        day_cell = row.find("td", class_="cell-day")
        if day_cell is None:
            continue
        day = _clean(day_cell.get_text(" ", strip=True))
        current_units = 0
        for cell in row.find_all("td", recursive=False)[1:]:
            colspan = int(cell.get("colspan") or 1)
            if _class_name(cell):
                event = _parse_event(cell, day, current_units, subject, week_label)
                event["weeks"] = _english_weeks(week_label)
                events.append(event)
            current_units += colspan
    return events


def _normalise_activity(activity: str) -> str:
    value = _clean(activity).lower()
    if value in ("", "all", "any"):
        return ""
    if value in ("lecture", "lectures", "prednaska", "přednáška"):
        return "lecture"
    if value in ("exercise", "exercises", "tutorial", "seminar", "cviceni", "cvičení"):
        return "exercise"
    if value in ("lab", "labs", "laboratory", "laboratories", "laborator", "laboratoř"):
        return "laboratory"
    return value


def _activity_matches(event: dict[str, Any], wanted: str) -> bool:
    if not wanted:
        return True
    activity = str(event.get("activity", ""))
    if wanted == "laboratory":
        return activity in ("laboratory", "exercise")
    return activity == wanted


def _event_signature(event: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(event.get("activity", "")),
        str(event.get("day", "")),
        str(event.get("start", "")),
        str(event.get("end", "")),
        str(event.get("room", "")),
        str(event.get("weeks", "")),
    )


def _slot_signature(event: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(event.get("activity", "")),
        str(event.get("day", "")),
        str(event.get("start", "")),
        str(event.get("end", "")),
        str(event.get("room", "")),
    )


def _dedupe_by_slot(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse events that share day/time/room but differ only by week range."""
    seen: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for event in events:
        key = _slot_signature(event)
        existing = seen.get(key)
        if existing is None:
            seen[key] = dict(event)
            continue
        existing_weeks = str(existing.get("weeks") or "")
        new_weeks = str(event.get("weeks") or "")
        if new_weeks and new_weeks not in existing_weeks:
            existing["weeks"] = (
                f"{existing_weeks}; {new_weeks}" if existing_weeks else new_weeks
            )
    return list(seen.values())


def _filter_events(
    events: list[dict[str, Any]],
    wanted_activity: str,
    wanted_day: str,
) -> list[dict[str, Any]]:
    return [
        event for event in events
        if _activity_matches(event, wanted_activity)
        and (not wanted_day or event.get("day_local") == wanted_day)
    ]


def _matching_code_suffix(subject: str, code: str) -> str:
    tokens = _code_tokens(subject)
    for token in tokens:
        code_upper = code.upper()
        if token in code_upper:
            return code_upper[code_upper.index(token):]
    return _subject_code(subject)


def _compatible_alternatives(subject: str, alternatives: list[dict[str, str]]) -> bool:
    if len(alternatives) < 2:
        return False
    suffixes = {_matching_code_suffix(subject, item["code"]) for item in alternatives}
    suffixes.discard("")
    return len(suffixes) == 1


def _normalise_day(day: str) -> str:
    value = _clean(day).lower()
    now = datetime.now(ZoneInfo("Europe/Prague")).date()
    if value == "today":
        return now.strftime("%A")
    if value == "tomorrow":
        return (now + timedelta(days=1)).strftime("%A")
    return _DAY_ALIASES.get(value, value)


def fetch_subject_schedule(subject: str, activity: str = "", day: str = "") -> dict[str, Any]:
    """Fetch timetable events for a public subject schedule."""
    query = _clean(subject)
    if not query:
        return {"status": "error", "error": "missing_subject", "message": "subject is required"}
    wanted_activity = _normalise_activity(activity)
    wanted_day = _normalise_day(day)

    resolved, alternatives = _resolve_subject(query)
    if resolved is None:
        if _compatible_alternatives(query, alternatives):
            schedules: list[dict[str, Any]] = []
            signatures: set[tuple[str, str, str, str, str, str]] = set()
            common_events: list[dict[str, Any]] = []
            for alternative in alternatives:
                filtered = _filter_events(
                    _parse_subject_page(alternative),
                    wanted_activity,
                    wanted_day,
                )
                schedules.append({
                    "subject": alternative,
                    "count": len(filtered),
                    "events": filtered[:MAX_RESULTS],
                })
                for event in filtered:
                    signatures.add(_event_signature(event))
            for signature in sorted(signatures):
                for schedule in schedules:
                    match = next(
                        (
                            event for event in schedule["events"]
                            if _event_signature(event) == signature
                        ),
                        None,
                    )
                    if match is not None:
                        common_events.append({
                            key: value
                            for key, value in match.items()
                            if key not in ("subject_code", "subject_name")
                        })
                        break

            logger.info(
                "subject_schedule_grouped query=%s activity=%s day=%s subjects=%s common_count=%d",
                query,
                wanted_activity or "-",
                wanted_day or "-",
                [item["code"] for item in alternatives],
                len(common_events),
            )
            return {
                "status": "ok",
                "resolution": "multiple_codes_same_subject",
                "requested_subject": query,
                "requested_activity": activity,
                "requested_day": day,
                "resolved_day": wanted_day,
                "subjects": alternatives,
                "count": len(common_events),
                "events": common_events[:MAX_RESULTS],
                "schedules": schedules,
                "message": (
                    "Found matching timetable variants for these subject codes. "
                    "Mention the codes and summarize the shared events."
                ),
                "source": TIMETABLE_INDEX_URL,
            }
        return {
            "status": "ambiguous" if alternatives else "not_found",
            "query": query,
            "matches": alternatives,
            "message": "Please specify the subject code." if alternatives else "No matching subject was found.",
            "source": TIMETABLE_INDEX_URL,
        }

    events = _parse_subject_page(resolved)
    filtered = _filter_events(events, wanted_activity, wanted_day)
    deduped = _dedupe_by_slot(filtered)

    logger.info(
        "subject_schedule_done subject=%s activity=%s day=%s count=%d unique=%d",
        resolved["code"], wanted_activity or "-", wanted_day or "-",
        len(filtered), len(deduped),
    )
    message = ""
    if not deduped:
        message = "No matching timetable event was found for the requested filters."
    return {
        "status": "ok",
        "subject": resolved,
        "requested_subject": query,
        "requested_activity": activity,
        "requested_day": day,
        "resolved_day": wanted_day,
        "count": len(deduped),
        "events": deduped[:MAX_RESULTS],
        "message": message,
        "source": resolved["url"],
    }
