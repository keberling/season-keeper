"""Plex watchlist feeds: RSS when configured, otherwise the owner's Discover list."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

from app.config import Settings
from app.plex import extract_guids, tvdb_from_guids

logger = logging.getLogger("trimarr.watchlist")

DISCOVER_HOST = "https://discover.provider.plex.tv"
METADATA_HOST = "https://metadata.provider.plex.tv"
_IMDB = re.compile(r"imdb://(tt\d+)", re.IGNORECASE)
_TMDB = re.compile(r"tmdb://(\d+)", re.IGNORECASE)
_PLEX_KEY = re.compile(r"plex://(?:show|movie)/([0-9a-z]+)", re.IGNORECASE)


@dataclass(frozen=True)
class WatchlistItem:
    media: str
    title: str
    year: int | None
    added_by: str
    plex_guid: str | None = None
    imdb_id: str | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None

    def label(self) -> str:
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title

    def with_ids(
        self,
        *,
        imdb_id: str | None = None,
        tvdb_id: int | None = None,
        tmdb_id: int | None = None,
        plex_guid: str | None = None,
    ) -> WatchlistItem:
        return WatchlistItem(
            media=self.media,
            title=self.title,
            year=self.year,
            added_by=self.added_by,
            plex_guid=plex_guid or self.plex_guid,
            imdb_id=imdb_id or self.imdb_id,
            tvdb_id=tvdb_id or self.tvdb_id,
            tmdb_id=tmdb_id or self.tmdb_id,
        )


def identity_key(item: WatchlistItem) -> str:
    if item.imdb_id:
        return f"{item.media}:imdb:{item.imdb_id}"
    if item.tvdb_id:
        return f"{item.media}:tvdb:{item.tvdb_id}"
    if item.tmdb_id:
        return f"{item.media}:tmdb:{item.tmdb_id}"
    if item.plex_guid:
        return f"{item.media}:plex:{item.plex_guid}"
    return f"{item.media}:title:{item.title.strip().lower()}"


def ids_from_guids(guids: list[str]) -> tuple[str | None, int | None, int | None]:
    imdb_id = None
    tmdb_id = None
    for guid in guids:
        if imdb_id is None:
            imdb = _IMDB.search(guid)
            if imdb:
                imdb_id = imdb.group(1)
        if tmdb_id is None:
            tmdb = _TMDB.search(guid)
            if tmdb:
                tmdb_id = int(tmdb.group(1))
    return imdb_id, tvdb_from_guids(guids), tmdb_id


def plex_metadata_key(guid: str | None) -> str | None:
    if not guid:
        return None
    match = _PLEX_KEY.search(guid)
    if not match:
        return None
    return match.group(1)


def _local(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def _child_text(item: ET.Element, name: str) -> str:
    for child in item:
        if _local(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return ""


def _media_kind(category: str, guid: str, link: str) -> str:
    blob = f"{category} {guid} {link}".lower()
    if "movie" in blob:
        return "movie"
    return "show"


def parse_rss(xml_text: str, *, default_author: str) -> list[WatchlistItem]:
    root = ET.fromstring(xml_text)
    items: list[WatchlistItem] = []
    for node in root.iter():
        if _local(node.tag) != "item":
            continue
        title = _child_text(node, "title") or "Untitled"
        author = _child_text(node, "author") or _child_text(node, "creator") or default_author
        category = _child_text(node, "category")
        guid = _child_text(node, "guid")
        link = _child_text(node, "link")
        blob = ET.tostring(node, encoding="unicode")
        guids = [guid] if guid else []
        guids.extend(_IMDB.findall(blob))
        # findall returns the group when the pattern has one. Re-scan the raw guid forms.
        for match in _IMDB.finditer(blob):
            guids.append(match.group(0))
        for match in _TMDB.finditer(blob):
            guids.append(match.group(0))
        for match in re.finditer(r"(?:thetvdb|tvdb)://\d+", blob, re.IGNORECASE):
            guids.append(match.group(0))
        imdb_id, tvdb_id, tmdb_id = ids_from_guids(guids)
        plex_guid = guid if guid.startswith("plex://") else None
        if plex_guid is None:
            found = _PLEX_KEY.search(blob)
            if found:
                kind = "movie" if "movie" in found.group(0) else "show"
                plex_guid = f"plex://{kind}/{found.group(1)}"
        year = None
        year_match = re.search(r"\((\d{4})\)\s*$", title)
        if year_match:
            year = int(year_match.group(1))
            title = title[: year_match.start()].strip() or title
        items.append(
            WatchlistItem(
                media=_media_kind(category, guid, link),
                title=title,
                year=year,
                added_by=author or default_author,
                plex_guid=plex_guid,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
            )
        )
    return items


def _plex_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-Plex-Token": token,
        "X-Plex-Product": "Trimarr",
        "X-Plex-Version": "1.0.0",
        "X-Plex-Client-Identifier": "trimarr",
    }


def parse_discover_metadata(payload: dict, *, added_by: str) -> list[WatchlistItem]:
    container = payload.get("MediaContainer") or payload
    rows = container.get("Metadata") or []
    items: list[WatchlistItem] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("type") or "").lower()
        media = "movie" if kind == "movie" else "show"
        guids = extract_guids(row)
        imdb_id, tvdb_id, tmdb_id = ids_from_guids(guids)
        plex_guid = next((guid for guid in guids if guid.startswith("plex://")), None)
        year = row.get("year") or None
        if year == 0:
            year = None
        items.append(
            WatchlistItem(
                media=media,
                title=str(row.get("title") or "Untitled"),
                year=int(year) if year else None,
                added_by=added_by,
                plex_guid=plex_guid,
                imdb_id=imdb_id,
                tvdb_id=tvdb_id,
                tmdb_id=tmdb_id,
            )
        )
    return items


def _discover_page(client: httpx.Client, token: str, start: int, size: int) -> tuple[list[dict], int]:
    response = client.get(
        f"{DISCOVER_HOST}/library/sections/watchlist/all",
        params={"includeGuids": 1, "X-Plex-Container-Start": start, "X-Plex-Container-Size": size},
        headers=_plex_headers(token),
        follow_redirects=True,
    )
    response.raise_for_status()
    payload = response.json()
    container = payload.get("MediaContainer") or payload
    rows = [row for row in (container.get("Metadata") or []) if isinstance(row, dict)]
    total = int(container.get("totalSize") or len(rows))
    return rows, total


def fetch_discover_watchlist(client: httpx.Client, token: str) -> list[WatchlistItem]:
    start = 0
    size = 100
    collected: list[dict] = []
    while start < 5000:
        rows, total = _discover_page(client, token, start, size)
        collected.extend(rows)
        start += len(rows)
        if not rows or start >= total:
            break
    return parse_discover_metadata({"MediaContainer": {"Metadata": collected}}, added_by="You")


def fetch_metadata(client: httpx.Client, token: str, key: str) -> dict | None:
    headers = _plex_headers(token)
    for host in (DISCOVER_HOST, METADATA_HOST):
        try:
            response = client.get(
                f"{host}/library/metadata/{key}",
                params={"includeGuids": 1},
                headers=headers,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            continue
        if response.status_code >= 400:
            continue
        payload = response.json()
        container = payload.get("MediaContainer") or payload
        rows = container.get("Metadata") or []
        if rows and isinstance(rows[0], dict):
            return rows[0]
    return None


def resolve_item(client: httpx.Client, token: str, item: WatchlistItem) -> WatchlistItem:
    if item.tvdb_id and item.imdb_id and item.tmdb_id:
        return item
    key = plex_metadata_key(item.plex_guid)
    if key is None or not token.strip():
        return item
    raw = fetch_metadata(client, token, key)
    if raw is None:
        return item
    filled = parse_discover_metadata({"MediaContainer": {"Metadata": [raw]}}, added_by=item.added_by)
    if not filled:
        return item
    found = filled[0]
    return item.with_ids(
        imdb_id=found.imdb_id,
        tvdb_id=found.tvdb_id,
        tmdb_id=found.tmdb_id,
        plex_guid=found.plex_guid,
    )


def _read_rss(client: httpx.Client, url: str, label: str, default_author: str) -> tuple[list[WatchlistItem], str | None]:
    """Read one feed. A redirect, a non-200, or a non-XML body is an error, not an empty list."""
    try:
        response = client.get(
            url,
            headers={"Accept": "application/rss+xml, application/xml, text/xml, */*"},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        logger.warning("Could not read the %s (%s)", label, exc.__class__.__name__)
        return [], f"Could not read the {label}. The request failed."
    if response.status_code != 200:
        logger.warning("Could not read the %s (HTTP %s)", label, response.status_code)
        return [], f"The {label} returned HTTP {response.status_code}."
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        logger.warning("Could not read the %s because the feed was not valid RSS", label)
        return [], f"The {label} feed was not valid RSS."
    if _local(root.tag).lower() not in {"rss", "feed", "rdf"}:
        logger.warning("Could not read the %s because the feed was not valid RSS", label)
        return [], f"The {label} feed was not valid RSS."
    return parse_rss(response.text, default_author=default_author), None


def load_watchlist(settings: Settings, client: httpx.Client) -> tuple[list[WatchlistItem], list[str]]:
    """RSS when either feed is set. Discover is the owner list only, used when both are blank.

    The second value is plain-English errors. A failed feed is not treated as empty,
    so items that are still in it are not marked seen and can be picked up later.
    """
    feeds = settings.watchlist_rss_urls
    items: list[WatchlistItem] = []
    errors: list[str] = []
    if feeds:
        for label, url in feeds:
            author = "You" if label == "My Watchlist" else "A friend"
            found, error = _read_rss(client, url, label, author)
            if error:
                errors.append(error)
                continue
            items.extend(found)
    elif settings.plex_token.strip():
        try:
            items.extend(fetch_discover_watchlist(client, settings.plex_token.strip()))
        except httpx.HTTPError as exc:
            logger.warning("Could not read the Plex Discover watchlist (%s)", exc.__class__.__name__)
            errors.append("Could not read the Plex Discover watchlist. The request failed.")
        except ValueError:
            logger.warning("Could not read the Plex Discover watchlist because the response was not JSON")
            errors.append("The Plex Discover watchlist was not valid JSON.")
    else:
        return [], []

    resolved: list[WatchlistItem] = []
    seen: set[str] = set()
    for item in items:
        filled = resolve_item(client, settings.plex_token.strip(), item)
        key = identity_key(filled)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(filled)
    return resolved, errors
