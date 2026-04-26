"""UDB (FEL staff directory) scraper used by the `lookup_person` tool.

Pipeline: ``lookup_person(name)`` -> UDB search by surname -> one HTTP fetch
per matching UID -> parsed into a small, LLM-friendly dict.

UDB sometimes serves an off-network stub page on blocked networks. We
detect the stub markers on profile fetches and raise
``NotOnCzvutNetworkError`` so the tool can return a structured error.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import requests
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger("voice-agent.udb")

UDB_BASE_URL = "https://udb.fel.cvut.cz/udb.phtml"
USER_AGENT = "voice-agent/0.5 (pepper receptionist)"
REQUEST_TIMEOUT_S = 10

# Markers that identify the off-network stub page UDB sometimes returns
# instead of a real profile. The page fires on certain blocked networks;
# many ordinary Czech ISPs / mobile hotspots work fine, so this is a
# soft signal, not a hard "are you on campus?" check. We look for either
# language variant.
OFF_NETWORK_MARKERS = (
    "Vyhledávání studentů nebo partnerů FEL",
    "Search for students or FEL partners",
)

# Stable English keys the tool surfaces to the LLM, mapped to the label
# variants UDB actually uses on its profile pages.
_PROFILE_LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("Name", "Jméno"),
    "email": ("Email", "E-mail"),
    "phone": ("Phone", "Telefon"),
    "room": ("Room", "Místnost"),
    "department": ("Department", "Pracoviště"),
}

_WHITESPACE_RE = re.compile(r"\s+")


class NotOnCzvutNetworkError(RuntimeError):
    """Raised when UDB returns the off-network stub page.

    Name kept for compatibility with the upstream Pepper_Data scraper.
    In practice it fires on *some* blocked networks, not just "outside
    ČVUT" — empirically public Czech ISPs and mobile hotspots usually
    work. Switch network and retry if you hit it.
    """


# region: HTTP layer

def _build_profile_url(uid: str) -> str:
    dn = f"uid={uid},ou=People,o=feld.cvut.cz"
    return (
        f"{UDB_BASE_URL}"
        f"?_cmd=show"
        f"&odn={quote(dn, safe='')}"
        f"&_type=user"
        f"&setlang=en"
    )


def _build_user_search_url(surname: str) -> str:
    return (
        f"{UDB_BASE_URL}"
        f"?_type=user"
        f"&_cmd=base_search"
        f"&_reqn=1"
        f"&search={quote(surname, safe='')}"
    )


def fetch_profile_html(uid: str) -> str:
    url = _build_profile_url(uid)
    response = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S
    )
    response.raise_for_status()
    body = response.text
    # OFF_NETWORK_MARKERS are reliable on profile pages (they never appear
    # in a valid profile). Do not reuse this check on the search endpoint —
    # those markers live there as a permanent disclaimer.
    if any(marker in body for marker in OFF_NETWORK_MARKERS):
        raise NotOnCzvutNetworkError(
            "UDB returned the off-network stub page. Your current network "
            "is blocked — try another (eduroam / campus / FEL VPN / a "
            "different ISP or hotspot)."
        )
    return body


def fetch_user_search_html(surname: str) -> str:
    url = _build_user_search_url(surname)
    response = requests.get(
        url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT_S
    )
    response.raise_for_status()
    return response.text

# endregion


# region: HTML parsing

def _normalize(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _cell_text(cell: Tag) -> str:
    return _normalize(cell.get_text(separator=" "))


def _add_field(fields: dict[str, Any], label: str, value: str) -> None:
    if not label or not value:
        return
    if label not in fields:
        fields[label] = value
        return
    existing = fields[label]
    if isinstance(existing, list):
        existing.append(value)
    else:
        fields[label] = [existing, value]


def parse_search_results(html: str) -> list[str]:
    """Return UIDs found on a UDB search-results page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    uids: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "_type=user" not in href or "odn=" not in href:
            continue
        odn_values = parse_qs(urlparse(href).query).get("odn")
        if not odn_values:
            continue
        dn = odn_values[0]
        for part in dn.split(","):
            key, _, value = part.partition("=")
            if key.strip().lower() != "uid":
                continue
            uid = value.strip()
            if uid and uid not in seen:
                seen.add(uid)
                uids.append(uid)
            break
    return uids


def parse_profile(html: str) -> dict[str, Any]:
    """Return every label/value pair on a UDB profile page."""
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, Any] = {}
    # Two-cell <tr> rows — the common layout.
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) != 2:
            continue
        label = _cell_text(cells[0]).rstrip(":")
        value = _cell_text(cells[1])
        _add_field(fields, label, value)
    # Fallback: <dl><dt>label</dt><dd>value</dd></dl> pages.
    if not fields:
        for dl in soup.find_all("dl"):
            for term in dl.find_all("dt"):
                definition = term.find_next_sibling("dd")
                if definition is None:
                    continue
                label = _normalize(term.get_text(separator=" ")).rstrip(":")
                value = _normalize(definition.get_text(separator=" "))
                _add_field(fields, label, value)
    return fields

# endregion


# region: public lookup

@dataclass
class PersonMatch:
    uid: str
    name: str | None
    email: str | None
    phone: str | None
    room: str | None
    department: str | None
    source_url: str
    raw: dict[str, Any]


def _pick(fields: dict[str, Any], labels: tuple[str, ...]) -> str | None:
    for label in labels:
        value = fields.get(label)
        if value:
            if isinstance(value, list):
                return "; ".join(str(v) for v in value if v)
            return str(value)
    return None


def _split_name(raw: str) -> tuple[str | None, str]:
    tokens = raw.strip().split()
    if not tokens:
        raise ValueError("empty name")
    if len(tokens) == 1:
        return None, tokens[0]
    return " ".join(tokens[:-1]), tokens[-1]


def _fold(text: str) -> str:
    """Lowercase + strip diacritics for forgiving substring matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def lookup_person(name: str) -> dict[str, Any]:
    """Look up FEL staff by name. Returns all matches.

    `name` can be surname alone ("Hoffmann") or a full name
    ("Matej Hoffmann"). The last token is always used as the UDB search
    term; leading tokens narrow results via substring match on the
    returned Name field.
    """
    first, surname = _split_name(name)
    logger.info("udb_lookup_start query=%s surname=%s", name, surname)

    search_html = fetch_user_search_html(surname)
    uids = parse_search_results(search_html)

    matches: list[PersonMatch] = []
    for uid in uids:
        html = fetch_profile_html(uid)
        fields = parse_profile(html)
        matches.append(
            PersonMatch(
                uid=uid,
                name=_pick(fields, _PROFILE_LABEL_ALIASES["name"]),
                email=_pick(fields, _PROFILE_LABEL_ALIASES["email"]),
                phone=_pick(fields, _PROFILE_LABEL_ALIASES["phone"]),
                room=_pick(fields, _PROFILE_LABEL_ALIASES["room"]),
                department=_pick(fields, _PROFILE_LABEL_ALIASES["department"]),
                source_url=_build_profile_url(uid),
                raw=fields,
            )
        )

    if first and matches:
        # Soft filter by first-name substring (diacritic-insensitive). If
        # the filter would leave zero hits, keep the unfiltered list so
        # the LLM still sees every candidate.
        needle = _fold(first)
        filtered = [m for m in matches if m.name and needle in _fold(m.name)]
        if filtered:
            matches = filtered

    logger.info(
        "udb_lookup_done surname=%s candidates=%d returned=%d",
        surname, len(uids), len(matches),
    )
    return {
        "status": "ok" if matches else "not_found",
        "query": name,
        "surname": surname,
        "count": len(matches),
        "matches": [asdict(m) for m in matches],
    }

# endregion
