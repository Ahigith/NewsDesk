"""Offline end-to-end test.

Runs the real dedupe -> score -> render path against fixture headlines and fake
market data, so the pipeline can be validated without network access or an API
key. Run: python -m tests.test_offline
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import build, dedupe, markets, score  # noqa: E402

HEADLINES = [
    ("Federal Reserve issues FOMC statement: rates held at 3.75-4.00%", "Fed", 1, "Monetary Policy"),
    ("Fed holds rates steady, signals one cut in 2026", "CNBC", 3, "Monetary Policy"),
    ("Fed signals one cut in 2026 as rates held steady", "FT", 2, "Monetary Policy"),
    ("US CPI rises 0.4% in July, hotter than the 0.2% consensus", "BLS", 1, "Macroeconomics"),
    ("Inflation surprise: US CPI climbs 0.4% in July", "WSJ Markets", 2, "Macroeconomics"),
    ("Nvidia beats on earnings, guides Q3 revenue above street", "WSJ Markets", 2, "Corporate Releases"),
    ("Brent crude rallies 3% after OPEC+ output cut extension", "CNBC", 3, "Energy & Commodities"),
    ("EU announces new tariffs on Chinese EV imports", "BBC World", 3, "Geopolitics & Trade"),
    ("Opinion: why the bond market keeps getting it wrong", "Guardian Business", 4, "Financial Markets"),
    ("Small-cap biotech Zynex announces board reshuffle", "Yahoo Finance", 4, "Corporate Releases"),
    ("Apple unveils on-device model for developers", "The Verge", 4, "Technology"),
    ("Markets wrap: stocks drift ahead of jobs data", "Yahoo Finance", 4, "Financial Markets"),
    # --- India ---
    ("RBI holds repo rate at 5.50%, cuts CRR by 25bp", "RBI press releases", 1, "Monetary Policy"),
    ("India CPI inflation eases to 3.1% in July, below RBI's 4% target", "PIB Finance Ministry", 1, "Macroeconomics"),
    ("Nifty 50 closes at record high as banks rally", "Economic Times Markets", 2, "Financial Markets"),
    ("Reliance Industries posts 12% jump in Q1 net profit", "Business Standard Markets", 2, "Corporate Releases"),
    ("Rupee weakens past 87/USD on oil import demand", "Mint Markets", 2, "Financial Markets"),
    ("SEBI tightens derivatives position limits for retail traders", "Moneycontrol Business", 3, "Financial Markets"),
]


def fixture_articles() -> list[dict]:
    now = time.time()
    out = []
    for i, (title, src, tier, hint) in enumerate(HEADLINES):
        url = f"https://example.com/{i}"
        out.append(
            {
                "id": hashlib.sha1(url.encode()).hexdigest()[:12],
                "title": title,
                "summary": f"{title}. Reported by {src}.",
                "url": url,
                "source": src,
                "tier": tier,
                "hint": hint,
                "published": None,
                "published_ts": now - i * 600,
            }
        )
    return out


def fake_markets():
    markets.ticker_bar = lambda specs: [
        {"label": s["label"], "value": "4,982", "change": "+0.42%",
         "direction": "up" if i % 2 else "down", "as_of": "2026-08-03",
         "spark": [1, 2, 3], "pct": 0.42}
        for i, s in enumerate(specs)
    ]
    markets.yield_curves = lambda cfg: {
        "United States": [
            {"tenor": t, "yield": y, "month_ago": y - 0.08, "as_of": "2026-08-03"}
            for t, y in [("3M", 3.92), ("2Y", 3.71), ("5Y", 3.88), ("10Y", 4.21), ("30Y", 4.55)]
        ]
    }
    markets.macro_scorecard = lambda specs: [
        {"country": s["country"], "metric": s["metric"], "value": 2.4,
         "prior": 2.2, "direction": "up", "as_of": "2026-07-01"} for s in specs[:6]
    ]
    markets.economic_calendar = lambda w, days_ahead=7: [
        {"date": "2026-08-06", "name": "CPI", "kind": "economic"},
        {"date": "2026-08-07", "name": "Employment Situation", "kind": "economic"},
    ]
    markets.earnings_calendar = lambda **kw: [
        {"date": "2026-08-05", "name": "AAPL — Apple Inc.", "detail": "EPS est 1.62",
         "market_cap": 3e12, "kind": "earnings"}
    ]


def main() -> int:
    fake_markets()
    build.ingest.fetch_all = lambda feeds, **kw: fixture_articles()

    payload = build.build()
    build.render(payload)

    failures = []
    s = payload["stats"]

    # India coverage must survive the whole pipeline
    india = [st for st in payload["stories"] if st["region"] == "India"]
    if len(india) < 4:
        failures.append(f"expected >=4 India stories, got {len(india)}")
    if "India" not in payload["regions"]:
        failures.append("India missing from region filter list")
    if s.get("india", 0) < 4:
        failures.append(f"stats.india wrong: {s.get('india')}")
    rbi = next((st for st in payload["stories"] if "RBI holds repo" in st["title"]), None)
    if rbi is None:
        failures.append("RBI story vanished")
    elif rbi["home_boost"] <= 0:
        failures.append("India story did not receive the home-region boost")
    elif rbi["importance"] != min(100, rbi["global_importance"] + rbi["home_boost"]):
        failures.append("boost arithmetic inconsistent")

    if s["raw"] != len(HEADLINES):
        failures.append(f"expected {len(HEADLINES)} raw, got {s['raw']}")
    if s["deduped"] >= s["raw"]:
        failures.append("dedupe collapsed nothing")
    if not payload["hero"]:
        failures.append("hero section empty")
    if not payload["ticker"]:
        failures.append("ticker empty")

    for story in payload["stories"]:
        if not 0 <= story["importance"] <= 100:
            failures.append(f"score out of range: {story['importance']}")
        if story["tier"] not in (1, 2, 3):
            failures.append(f"bad tier: {story['tier']}")
        if story["category"] not in score.CATEGORIES:
            failures.append(f"bad category: {story['category']}")
        if story["region"] not in score.REGIONS:
            failures.append(f"bad region: {story['region']}")
        if not story["takeaway"]:
            failures.append("empty takeaway")

    html = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    for needle in ["The <span>Desk</span>", "Executive Briefing", "Macro Scorecard",
                   "Sovereign Yield Curves", "id=\"feed\"", "curvedata",
                   "id=\"regions\"", "data-region=\"India\"", "Everywhere"]:
        if needle not in html:
            failures.append(f"template missing: {needle}")
    if len(html) < 8000:
        failures.append(f"html suspiciously short: {len(html)} bytes")

    latest = json.loads((ROOT / "site" / "data" / "latest.json").read_text())
    if latest["stats"] != s:
        failures.append("latest.json out of sync")

    print("\n--- sorted output ---")
    for st in payload["stories"]:
        boost = f"+{st['home_boost']}" if st["home_boost"] else "  "
        print(f"  {st['importance']:>3}{boost:>4}  T{st['tier']}  {st['region']:<7} "
              f"{st['category'][:18]:<18} {st['title'][:50]}")

    print(f"\nHTML: {len(html):,} bytes")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  x " + f)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
