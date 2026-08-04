"""Orchestrator. Run once a day: `python -m src.build`

Pipeline: RSS -> dedupe -> LLM score -> market data -> JSON -> static HTML.
Output lands in site/ (published) and data/ (archived; git history is your
database, which is why this project needs no Postgres).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

from src import ingest, markets, score

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
SITE = ROOT / "site"


def load_yaml(name: str) -> dict:
    with open(CONFIG / name, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def build(skip_markets: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    feeds_cfg = load_yaml("feeds.yml")
    mkt_cfg = load_yaml("markets.yml")

    raw = ingest.fetch_all(feeds_cfg["feeds"], lookback_hours=30, per_feed_cap=25)
    stories = dedupe_stage(raw)

    cap = os.environ.get("MAX_ARTICLES_TO_SCORE", "").strip()
    print("Scoring...")
    scored = score.score(stories, max_articles=int(cap) if cap.isdigit() else None)

    if skip_markets:
        tiles, curves, scorecard, econ, earn = [], {}, [], [], []
    else:
        tiles = markets.ticker_bar(mkt_cfg["ticker_bar"])
        curves = markets.yield_curves(mkt_cfg["yield_curves"])
        scorecard = markets.macro_scorecard(mkt_cfg["macro_scorecard"])
        econ = markets.economic_calendar(mkt_cfg["watched_releases"])
        earn = markets.earnings_calendar()

    payload = {
        "generated_at": now.isoformat(),
        "generated_human": now.strftime("%A, %d %B %Y · %H:%M UTC"),
        "hero": [s for s in scored if s["importance"] >= 70][:5] or scored[:5],
        "stories": scored,
        "categories": sorted({s["category"] for s in scored}),
        "regions": sorted({s["region"] for s in scored}),
        "ticker": tiles,
        "yield_curves": curves,
        "macro": scorecard,
        "economic_calendar": econ,
        "earnings_calendar": earn,
        "next_release": markets.next_release_countdown(econ) if econ else None,
        "stats": {
            "raw": len(raw),
            "deduped": len(stories),
            "scored": len(scored),
            "tier1": sum(1 for s in scored if s["tier"] == 1),
            "india": sum(1 for s in scored if s["region"] == "India"),
            "sources": len(feeds_cfg["feeds"]),
        },
    }
    return payload


def dedupe_stage(raw: list[dict]) -> list[dict]:
    from src import dedupe

    return dedupe.cluster(raw)


def render(payload: dict) -> None:
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "src")),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("template.html")
    html = template.render(**payload)

    SITE.mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    (SITE / "index.html").write_text(html, encoding="utf-8")
    (SITE / "data" / "latest.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    DATA.mkdir(exist_ok=True)
    stamp = payload["generated_at"][:10]
    archive = DATA / f"{stamp}.json"
    archive.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    shutil.copy(archive, SITE / "data" / f"{stamp}.json")

    print(f"\nWrote {SITE / 'index.html'}")
    print(f"Archived {archive}")
    print(
        "Stats: {raw} raw -> {deduped} deduped -> {scored} scored "
        "({tier1} tier-1) from {sources} feeds".format(**payload["stats"])
    )


if __name__ == "__main__":
    render(build(skip_markets="--no-markets" in sys.argv))
