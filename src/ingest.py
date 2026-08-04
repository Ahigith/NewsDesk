"""Pull headlines from RSS feeds. No article bodies -- headline + dek only."""
from __future__ import annotations

import hashlib
import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import feedparser
import requests

# Identify as a browser. This is not optional politeness-theatre: official
# institutions (ECB, Bank of England, BEA) sit behind WAFs that silently reject
# unrecognised user-agents, while commercial news sites don't care. Using a
# custom UA string meant every central bank feed came back empty while all 33
# news outlets worked -- which looked like dead URLs and wasn't.
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "application/rss+xml, application/xml, text/xml, application/atom+xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def _clean(text: str, limit: int = 400) -> str:
    """RSS summaries are usually HTML. Flatten to plain text."""
    if not text:
        return ""
    text = TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text).strip()
    return text[:limit]


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    return None


def fetch_raw(url: str, timeout: int = 25):
    """Fetch a feed over HTTP ourselves, then hand the bytes to feedparser.

    Letting feedparser do its own fetching hides the HTTP status code, so a 403
    is indistinguishable from an empty feed. Doing it here means we can set real
    browser headers and report what actually happened.
    """
    resp = SESSION.get(url, timeout=timeout)
    resp.raise_for_status()
    return feedparser.parse(resp.content), resp.status_code


def fetch_feed(feed: dict, lookback_hours: int, per_feed_cap: int) -> list[dict]:
    """Fetch one feed. Never raises -- a dead feed must not kill the run."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    try:
        parsed, _ = fetch_raw(feed["url"])
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {feed['name']}: {type(exc).__name__}: {exc}")
        return []

    if parsed.get("bozo") and not parsed.entries:
        print(f"  ! {feed['name']}: unparseable ({parsed.get('bozo_exception')})")
        return []

    out = []
    for entry in parsed.entries[: per_feed_cap * 3]:
        title = _clean(entry.get("title", ""), 300)
        link = entry.get("link", "")
        if not title or not link:
            continue

        published = _published(entry)
        # Feeds that omit dates are kept; we would rather over-include than
        # silently drop a central bank release with a missing timestamp.
        if published and published < cutoff:
            continue

        out.append(
            {
                "id": hashlib.sha1(link.encode()).hexdigest()[:12],
                "title": title,
                "summary": _clean(entry.get("summary", "") or entry.get("description", "")),
                "url": link,
                "source": feed["name"],
                "tier": feed.get("tier", 5),
                "hint": feed.get("hint", ""),
                "published": published.isoformat() if published else None,
                "published_ts": published.timestamp() if published else 0.0,
            }
        )
        if len(out) >= per_feed_cap:
            break

    print(f"  + {feed['name']}: {len(out)}")
    return out


def fetch_all(feeds: list[dict], lookback_hours: int = 30, per_feed_cap: int = 25) -> list[dict]:
    """Fetch every feed in parallel, drop exact-URL duplicates, sort newest first."""
    print(f"Fetching {len(feeds)} feeds (lookback {lookback_hours}h)...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = pool.map(lambda f: fetch_feed(f, lookback_hours, per_feed_cap), feeds)

    seen: set[str] = set()
    articles: list[dict] = []
    for batch in batches:
        for art in batch:
            if art["id"] in seen:
                continue
            seen.add(art["id"])
            articles.append(art)

    articles.sort(key=lambda a: a["published_ts"], reverse=True)
    print(f"Total unique URLs: {len(articles)}")
    return articles
