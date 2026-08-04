"""Scoring engine: categorize, summarize and rank headlines by market impact.

The model backend is pluggable -- see src/llm.py. Default is Groq's free tier.
Set LLM_PROVIDER=none to skip the model entirely and use the rule-based scorer
at the bottom of this file, which costs nothing and needs no account.

Whatever the backend, three things keep the request count tiny enough to sit
inside a free tier: batching (a dozen headlines per call), deduping before
scoring, and a hard MAX_ARTICLES_TO_SCORE cap. A daily run is 10-15 requests.
"""
from __future__ import annotations

import os
import re

from src import llm

REGIONS = ["US", "Europe", "UK", "India", "China", "Japan", "Global", "Other"]

# Stories touching these regions get a relevance bump on top of their objective
# global-impact score. Keeps the rubric honest -- the model still judges what
# actually moves world markets -- while surfacing what YOU care about. An RBI
# repo decision genuinely matters less to global rates than an FOMC decision;
# it matters much more to you. Set NEWSDESK_HOME_REGIONS="" to switch this off.
HOME_REGIONS = [
    r.strip() for r in os.environ.get("NEWSDESK_HOME_REGIONS", "India,US").split(",") if r.strip()
]
HOME_BOOST = int(os.environ.get("NEWSDESK_HOME_BOOST", "12"))

CATEGORIES = [
    "Macroeconomics",
    "Monetary Policy",
    "Financial Markets",
    "Corporate Releases",
    "Geopolitics & Trade",
    "Technology",
    "Energy & Commodities",
]


SYSTEM_PROMPT = f"""You are a sell-side market strategist triaging the overnight news flow for a morning meeting. You rank stories by how much they should move an institutional investor's positioning today.

For each article you return one object with these fields:
  i                  : the integer index you were given (echo it back exactly)
  category           : exactly one of {CATEGORIES}
  takeaway           : ONE sentence, max 22 words, stating the market-relevant fact.
                       Write what happened and why it matters. Never restate the
                       headline verbatim. No hedging, no "this article discusses".
  asset_classes      : subset of ["Equities","Rates","FX","Commodities","Credit"]. May be empty.
  sentiment          : "Bullish" | "Bearish" | "Neutral"  (for risk assets)
  key_entities       : up to 4 tickers, institutions, or currencies e.g. ["FOMC","USD","SPX"]
  region             : the single region whose markets this most affects, one of
                       {REGIONS}. Use "Global" only when the impact is genuinely
                       cross-regional (oil shocks, Fed policy, a world trade war),
                       not merely because a story is internationally reported.
  s_entity           : 0-100, see rubric
  s_scope            : 0-100, see rubric
  s_surprise         : 0-100, see rubric
  dup_of             : integer index of an EARLIER article in this same batch
                       that reports THE SAME UNDERLYING EVENT, else null.
                       Same event means same release, same decision, same deal --
                       not merely the same topic. "US CPI came in at 0.4%" and
                       "US PPI came in at 0.4%" are DIFFERENT events. "Fed holds
                       rates" and "Federal Reserve keeps policy unchanged" are
                       the SAME event. When unsure, use null.

SCORING RUBRIC

s_entity — who is involved:
  100  G7 central banks (Fed/ECB/BOJ/BOE), sovereign debt events, G7 head-of-state policy
   75  Mega-cap earnings (Mag 7), systemically important financials, OPEC+ policy,
        PBOC, RBI monetary policy decisions, India Union Budget, SEBI market-structure
        rules, Nifty-50 heavyweight earnings (Reliance, HDFC Bank, TCS, Infosys, ICICI)
   40  Mid-cap corporate news, regional regulators, second-tier sovereigns,
        state-level Indian policy, mid-cap NSE/BSE listings
   10  Non-systemic single-company updates, routine commentary, opinion pieces

Note on India: treat RBI, the Finance Ministry, MOSPI data releases (CPI, WPI, IIP,
GDP) and SEBI as first-order authorities for Indian assets. An RBI repo-rate change
is to INR and Indian rates what an FOMC decision is to USD -- score s_scope by the
breadth of the impact, and do not mark Indian macro down merely for being non-G7.

s_scope — how far it reaches:
  100  Cross-asset: moves global rates AND FX AND major equity indices together
   60  One asset class, or one country's macro picture
   20  Industry-niche, single small-cap, or purely local impact

s_surprise — is this new information:
  100  Materially diverges from consensus: unexpected rate decision, big CPI/NFP miss,
       surprise M&A, unanticipated geopolitical escalation
   50  Happened as expected, or confirms a well-telegraphed path
   10  Recycled background, scheduled speech with no policy change, explainer, opinion

Be strict. Most days, fewer than five stories deserve a total above 80. Routine
market wrap-ups and opinion columns should score below 30. An article about a
company you have never heard of is almost never Tier 1.

Return ONLY a JSON object of the form {{"results": [ ...one object per article... ]}}.
Every article you were given must appear exactly once. No prose, no markdown fences."""


def _order_for_batching(articles: list[dict]) -> list[int]:
    """Return indices sorted so lexically-similar headlines share a batch.

    `dup_of` can only reference articles in the same API call, so co-locating
    similar headlines is what makes stage-2 dedupe work at all.
    """
    from src.dedupe import normalize

    return sorted(range(len(articles)), key=lambda i: normalize(articles[i]["title"]))


def merge_duplicates(scored: list[dict]) -> list[dict]:
    """Fold LLM-flagged duplicates into their parent story.

    The survivor keeps the highest importance score of the group -- if any
    outlet's framing made the model realize the story was Tier 1, that judgment
    should win.
    """
    by_id = {s["id"]: s for s in scored}
    absorbed: set[str] = set()

    for story in scored:
        parent_id = story.pop("_dup_of_id", None)
        if not parent_id or parent_id == story["id"] or parent_id not in by_id:
            continue
        parent = by_id[parent_id]
        if parent["id"] in absorbed:      # don't chain merges
            continue
        parent["coverage_count"] += story.get("coverage_count", 1)
        parent["also_covered_by"] = sorted(
            set(parent.get("also_covered_by", [])) | {story["source"]} | set(story.get("also_covered_by", []))
        )
        parent["other_urls"] = (parent.get("other_urls", []) +
                                [{"source": story["source"], "url": story["url"]}])[:5]
        if story["importance"] > parent["importance"]:
            parent["importance"] = story["importance"]
            parent["tier"] = story["tier"]
            parent["takeaway"] = story["takeaway"]
        absorbed.add(story["id"])

    out = [s for s in scored if s["id"] not in absorbed]
    if absorbed:
        print(f"  LLM merged {len(absorbed)} further duplicates")
    return out


def _payload(batch: list[dict], offset: int) -> str:
    lines = []
    for idx, art in enumerate(batch):
        lines.append(
            json.dumps(
                {
                    "i": offset + idx,
                    "source": art["source"],
                    "hint": art.get("hint", ""),
                    "outlets_covering": art.get("coverage_count", 1),
                    "title": art["title"],
                    "dek": art.get("summary", "")[:240],
                }
            )
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rule-based scorer: the LLM_PROVIDER=none path, and the fallback whenever a
# batch fails. It approximates the same three-part rubric with keyword lists.
#
# It is meaningfully worse than a model at two things: writing a takeaway (it
# can only reuse the publisher's summary) and judging surprise (it reads
# adjectives, not consensus). It is roughly as good at entity weighting, which
# is most of what determines the ranking. Perfectly usable; just don't expect
# the one-line takeaways to be insightful.
# ---------------------------------------------------------------------------

def _kw(*words: str) -> re.Pattern:
    """Compile a word-boundary matcher.

    Plain substring matching is a trap here: "repo" matches inside "Reported",
    which scored a biotech board reshuffle as a rates story. Every keyword list
    below goes through this.

    The trailing guard is `(?!\w)` rather than `\b` because several keywords end
    in punctuation ("u.s.", "s&p"), where `\b` behaves the wrong way round.
    """
    return re.compile(
        r"\b(?:" + "|".join(re.escape(w) for w in words) + r")(?!\w)", re.I
    )


# Scheduled statistical releases belong at the top tier alongside central banks --
# they are the single most reliable market-moving events on the calendar.
_ENTITY_100 = _kw(
    "fomc", "federal reserve", "fed", "ecb", "european central bank",
    "bank of england", "bank of japan", "boj", "sovereign default", "debt ceiling",
    "credit rating", "treasury secretary",
    "repo rate", "policy rate", "rate decision", "monetary policy", "rate cut",
    "rate hike", "crr", "interest rate decision",
    "cpi", "ppi", "pce", "gdp", "nonfarm", "non-farm", "payrolls", "jobs report",
    "employment report", "unemployment rate", "retail sales", "pmi", "inflation data",
    "wpi", "iip", "jolts", "consumer price", "producer price",
)
_ENTITY_75 = _kw(
    "rbi", "reserve bank of india", "pboc", "people's bank",
    "opec", "union budget", "sebi", "imf", "world bank", "mospi", "boe",
    "nvidia", "apple", "microsoft", "amazon", "alphabet", "google", "meta",
    "tesla", "jpmorgan", "goldman", "reliance", "hdfc", "tcs", "infosys",
    "icici", "tata", "berkshire", "tsmc", "samsung", "adani",
)
_ENTITY_10 = _kw(
    "opinion", "explainer", "podcast", "column", "commentary", "preview", "recap",
    "what to know", "what to watch", "here's why", "how to", "best of",
)

_ASSETS = {
    "Rates": _kw("yield", "yields", "bond", "bonds", "treasury", "treasuries", "g-sec",
                 "gilt", "gilts", "bund", "rate", "rates", "repo", "duration", "coupon"),
    "FX": _kw("dollar", "rupee", "euro", "yen", "currency", "forex", "fx", "usd",
              "inr", "eur", "jpy", "peg", "devaluation"),
    "Equities": _kw("stock", "stocks", "shares", "equity", "equities", "index",
                    "nifty", "sensex", "s&p", "nasdaq", "earnings", "ipo", "buyback"),
    "Commodities": _kw("oil", "crude", "brent", "gold", "copper", "gas", "commodity",
                       "commodities", "wheat", "lng"),
    "Credit": _kw("credit", "default", "spread", "spreads", "downgrade", "bankruptcy",
                  "loan", "loans", "npa", "npas"),
}

# "record high" is deliberately absent: indexes make record highs constantly and
# it is not new information.
_SURPRISE_HI = _kw(
    "surprise", "surprised", "unexpected", "unexpectedly", "shock", "shocks",
    "hotter than", "cooler than", "beats", "missed", "misses", "tops estimates",
    "below estimates", "above forecast", "below forecast", "above consensus",
    "below consensus", "plunge", "plunges", "surge", "surges", "soars", "slump",
    "slumps", "emergency", "halts", "suspends", "bans", "unscheduled", "snap",
)
_SURPRISE_LO = _kw(
    "as expected", "in line with", "unchanged", "holds steady", "reiterates",
    "scheduled", "annual meeting", "opinion", "explainer", "preview", "wrap",
    "roundup", "what to watch", "ahead of",
)

_BULL = _kw("rally", "rallies", "surge", "surges", "gains", "jumps", "beats", "rises",
            "soars", "upgrade", "raises guidance", "tops", "climbs", "rebound", "eases")
_BEAR = _kw("falls", "plunge", "plunges", "slump", "misses", "drops", "downgrade",
            "cuts guidance", "slides", "tumbles", "warning", "layoffs", "default",
            "recession", "weakens")

_REGION_RULES = [
    ("India", _kw("india", "indian", "rbi", "sensex", "nifty", "rupee", "inr", "sebi",
                  "mumbai", "delhi", "repo rate", "reliance", "infosys", "tcs", "hdfc",
                  "icici", "adani", "tata", "nse", "bse", "mospi", "gst", "crr")),
    ("US", _kw("us", "u.s.", "usa", "american", "fed", "fomc", "federal reserve",
               "treasury", "wall street", "nasdaq", "s&p 500", "washington",
               "nonfarm", "non-farm", "white house", "congress", "dollar")),
    ("Europe", _kw("ecb", "euro area", "eurozone", "germany", "german", "france",
                   "french", "brussels", "eu", "european union", "italy", "spain")),
    ("UK", _kw("uk", "britain", "british", "bank of england", "gilt", "gilts", "london")),
    ("China", _kw("china", "chinese", "pboc", "beijing", "yuan", "renminbi",
                  "hong kong", "shanghai")),
    ("Japan", _kw("japan", "japanese", "boj", "yen", "nikkei", "tokyo")),
]


def _entity_score(text: str) -> float:
    if _ENTITY_100.search(text):
        return 100.0
    if _ENTITY_75.search(text):
        return 75.0
    if _ENTITY_10.search(text):
        return 10.0
    return 40.0


def _detect_assets(text: str) -> list[str]:
    return [name for name, pattern in _ASSETS.items() if pattern.search(text)]


def _scope_score(assets: list[str], coverage: int, entity: float) -> float:
    base = {0: 25.0, 1: 60.0, 2: 75.0}.get(len(assets), 100.0)
    # A top-tier release moves everything even when the headline names no asset.
    if entity >= 100:
        base = max(base, 75.0)
    # Heavy syndication is itself evidence of breadth.
    return min(100.0, base + min(20, (coverage - 1) * 7))


def _surprise_score(text: str) -> float:
    if _SURPRISE_LO.search(text):
        return 20.0
    hits = len(set(_SURPRISE_HI.findall(text)))
    return min(100.0, 45.0 + hits * 22) if hits else 45.0


def _guess_region(text: str) -> str:
    for name, pattern in _REGION_RULES:
        if pattern.search(text):
            return name
    return "Global"


def _heuristic(articles: list[dict]) -> list[dict]:
    out = []
    for art in articles:
        text = (art["title"] + " " + art.get("summary", "")).lower()
        coverage = art.get("coverage_count", 1)
        assets = _detect_assets(text)
        entity = _entity_score(text)

        bull = len(_BULL.findall(text))
        bear = len(_BEAR.findall(text))
        sentiment = "Bullish" if bull > bear else ("Bearish" if bear > bull else "Neutral")

        summary = (art.get("summary") or "").strip()
        takeaway = summary if len(summary) > 40 else art["title"]

        out.append(
            {
                "region": _guess_region(text),
                "category": art.get("hint") or "Financial Markets",
                "takeaway": takeaway[:180],
                "asset_classes": assets[:3],
                "sentiment": sentiment,
                "key_entities": [],
                "s_entity": entity,
                "s_scope": _scope_score(assets, coverage, entity),
                "s_surprise": _surprise_score(text),
            }
        )
    return out


def score(articles: list[dict], max_articles: int | None = None) -> list[dict]:
    if max_articles:
        articles = articles[:max_articles]
    if not articles:
        return []

    provider_name, cfg = llm.active_provider()

    if cfg is None:
        print("   Using rule-based scorer (no model, no cost).")
        results = _heuristic(articles)
    else:
        batch_size = cfg["batch"]
        print(f"   Provider: {provider_name} / {cfg['model']} (batches of {batch_size})")
        order = _order_for_batching(articles)
        ordered = [articles[i] for i in order]
        results: list[dict | None] = [None] * len(articles)

        for start in range(0, len(ordered), batch_size):
            batch = ordered[start : start + batch_size]
            print(f"  scoring {start + 1}-{start + len(batch)} of {len(ordered)}")
            try:
                text = llm.complete_json(cfg, SYSTEM_PROMPT, _payload(batch, start))
                items = llm.extract_results(text)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! batch failed ({type(exc).__name__}: {exc}) -- will use fallback")
                continue

            if not items:
                print("  ! batch returned nothing parseable -- will use fallback")
                continue

            for item in items:
                pos = item.get("i")
                if not isinstance(pos, int) or not start <= pos < start + len(batch):
                    continue
                dup = item.get("dup_of")
                # Translate batch-local index -> stable article id.
                if isinstance(dup, int) and start <= dup < pos:
                    item["_dup_of_id"] = ordered[dup]["id"]
                results[order[pos]] = item

        missing = [i for i, r in enumerate(results) if r is None]
        if missing:
            pct = 100 * len(missing) / len(articles)
            print(f"  ! {len(missing)} of {len(articles)} unscored ({pct:.0f}%); rule-based fallback")
            for i, item in zip(missing, _heuristic([articles[i] for i in missing])):
                results[i] = item

    scored = []
    for art, res in zip(articles, results):
        entity = float(res.get("s_entity", 30) or 0)
        scope = float(res.get("s_scope", 30) or 0)
        surprise = float(res.get("s_surprise", 30) or 0)
        # The weights from your spec. Computed in Python, not by the model, so
        # the arithmetic is deterministic and auditable.
        total = round(0.35 * entity + 0.35 * scope + 0.30 * surprise)

        region = res.get("region")
        if region not in REGIONS:
            region = "Other"
        # Personal-relevance boost, applied transparently and after the objective
        # score, so `components` still shows what the model actually judged.
        boosted = min(100, total + HOME_BOOST) if region in HOME_REGIONS else total

        category = res.get("category")
        if category not in CATEGORIES:
            category = art.get("hint") if art.get("hint") in CATEGORIES else "Financial Markets"

        merged = dict(art)
        merged["source_tier"] = art.get("tier", 5)  # feed priority, before we reuse the key
        merged.update(
            {
                "category": category,
                "takeaway": (res.get("takeaway") or "").strip() or art["title"],
                "asset_classes": res.get("asset_classes") or [],
                "sentiment": res.get("sentiment") or "Neutral",
                "key_entities": (res.get("key_entities") or [])[:4],
                "region": region,
                "components": {"entity": entity, "scope": scope, "surprise": surprise},
                "global_importance": total,
                "home_boost": boosted - total,
                "importance": boosted,
                "tier": 1 if boosted >= 70 else (2 if boosted >= 45 else 3),
            }
        )
        if res.get("_dup_of_id"):
            merged["_dup_of_id"] = res["_dup_of_id"]
        scored.append(merged)

    scored = merge_duplicates(scored)
    scored.sort(key=lambda a: (-a["importance"], -a["published_ts"]))
    return scored
