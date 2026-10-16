"""RSS/Atom-Feeds einsammeln, vorfiltern und speichern.

Live geprüft am 2026-10-16 per httpx (HTTP 200, gültiges XML):
  - tagesschau Wirtschaft  https://www.tagesschau.de/wirtschaft/index~rss2.xml   ok
  - Handelsblatt Energie   https://www.handelsblatt.com/contentexport/feed/energie ok
  - Spiegel Wirtschaft     https://www.spiegel.de/wirtschaft/index.rss             ok
Nicht erreichbar (404), daher nicht aufgenommen: energate messenger (/rss),
BMWK-Pressemitteilungen, Bundesnetzagentur-Presse, DEHSt-Aktuelles, Deutschlandfunk Wirtschaft,
DW Wirtschaft. heise/golem bewusst nicht.
"""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from heizoel import db
from heizoel.config import settings

log = logging.getLogger(__name__)

FEEDS: list[tuple[str, str]] = [
    ("tagesschau Wirtschaft", "https://www.tagesschau.de/wirtschaft/index~rss2.xml"),
    ("Handelsblatt Energie", "https://www.handelsblatt.com/contentexport/feed/energie"),
    ("Spiegel Wirtschaft", "https://www.spiegel.de/wirtschaft/index.rss"),
]

KEYWORDS = [
    "OPEC", "Brent", "Rohöl", "Ölpreis", "Raffinerie", "Hormus", "Sanktion", "Iran",
    "Russland", "Heizöl", "CO2-Preis", "CO₂", "BEHG", "Emissionshandel", "ETS2",
    "Energiesteuer", "Mineralöl", "Diesel", "Gasöl", "Tankstelle", "Fördermenge",
]  # fmt: skip

USER_AGENT = "Mozilla/5.0 (compatible; heizoel-news/0.1)"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _clean(text: str | None, limit: int | None = None) -> str:
    if not text:
        return ""
    out = html.unescape(_TAG_RE.sub(" ", text))
    out = _WS_RE.sub(" ", out).strip()
    return out[:limit] if limit else out


def _child_text(el: ET.Element, *names: str) -> str | None:
    for child in el:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text.strip()
    return None


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atom_link(entry: ET.Element) -> str | None:
    fallback = None
    for child in entry:
        if _local(child.tag) == "link":
            href = child.get("href")
            if href and child.get("rel", "alternate") == "alternate":
                return href
            fallback = fallback or href
    return fallback


def parse_feed(xml_text: str, feed_name: str) -> list[dict]:
    """Parst RSS 2.0 und Atom. Einträge ohne Titel oder URL werden übersprungen."""
    root = ET.fromstring(xml_text.lstrip("﻿").strip().encode("utf-8"))
    items: list[dict] = []
    for el in root.iter():
        kind = _local(el.tag)
        if kind == "item":
            url = _child_text(el, "link")
            published = _child_text(el, "pubDate", "date")
            summary = _child_text(el, "description", "encoded")
        elif kind == "entry":
            url = _atom_link(el)
            published = _child_text(el, "published", "updated")
            summary = _child_text(el, "summary", "content")
        else:
            continue
        title = _clean(_child_text(el, "title"))
        if not title or not url:
            continue
        items.append(
            {
                "url": url.strip(),
                "feed": feed_name,
                "title": title,
                "published": _iso(published),
                "summary": _clean(summary, 600),
            }
        )
    return items


def fetch_all(client: httpx.Client | None = None) -> list[dict]:
    own = client is None
    client = client or httpx.Client(
        timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    )
    items: list[dict] = []
    try:
        for name, url in FEEDS:
            try:
                resp = client.get(url)
                resp.raise_for_status()
                parsed = parse_feed(resp.text, name)
                log.info("Feed %s: %d Einträge", name, len(parsed))
                items.extend(parsed)
            except Exception as exc:  # noqa: BLE001 - ein defekter Feed darf den Lauf nicht stoppen
                log.warning("Feed %s fehlgeschlagen: %s", name, exc)
    finally:
        if own:
            client.close()
    return items


def prefilter(items: list[dict]) -> list[dict]:
    """Keyword-Treffer, ohne Duplikate (URL), neueste zuerst, höchstens news_max_items."""
    needles = [k.lower() for k in KEYWORDS]
    seen: set[str] = set()
    hits: list[dict] = []
    for item in items:
        if item["url"] in seen:
            continue
        haystack = f"{item.get('title', '')} {item.get('summary') or ''}".lower()
        if any(n in haystack for n in needles):
            seen.add(item["url"])
            hits.append(item)
    hits.sort(key=lambda i: i.get("published") or "", reverse=True)
    return hits[: settings.news_max_items]


def store_items(items: list[dict]) -> None:
    db.executemany(
        "INSERT OR IGNORE INTO news_items (url, feed, title, published, summary) "
        "VALUES (:url, :feed, :title, :published, :summary)",
        [
            {
                "url": i["url"],
                "feed": i["feed"],
                "title": i["title"],
                "published": i.get("published"),
                "summary": i.get("summary"),
            }
            for i in items
        ],
    )
