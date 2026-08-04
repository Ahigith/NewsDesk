"""`python -m src.verify_feeds` -- check which feeds are still alive.

Reports the HTTP status and which User-Agent worked, because "0 entries" and
"403 Forbidden" are very different problems, and because some publishers accept
only a browser UA while others accept only a declared bot. Run this whenever the
story count looks thin.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from src.ingest import fetch_raw

ROOT = Path(__file__).resolve().parent.parent


def check(feed: dict) -> tuple[dict, int, str]:
    try:
        parsed, status, ua = fetch_raw(feed["url"])
        return feed, len(parsed.entries), f"{status} via {ua}"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        response = getattr(exc, "response", None)
        if response is not None:
            detail = f"HTTP {response.status_code}"
        return feed, 0, detail


def main() -> int:
    feeds = yaml.safe_load(open(ROOT / "config" / "feeds.yml", encoding="utf-8"))["feeds"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(check, feeds))

    dead = []
    for feed, count, status in results:
        flag = "OK  " if count else "DEAD"
        print(f"{flag} {count:>3} entries  [{status:>16}]  {feed['name']}")
        if not count:
            dead.append((feed["name"], status))

    print(f"\n{len(feeds) - len(dead)}/{len(feeds)} alive")
    if dead:
        print("\nDead:")
        for name, status in dead:
            print(f"  x {name}  ({status})")
        print("\nBoth a browser UA and a declared-bot UA were tried on each feed.")
        print("If both failed, the URL has genuinely moved or the block is")
        print("stricter than headers can solve -- delete it from config/feeds.yml.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
