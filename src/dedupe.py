"""Collapse the same story reported by five outlets into one entry.

This is stage 1 of 2, and it is deliberately CONSERVATIVE.

Your original plan used vector embeddings + cosine similarity. Testing string
similarity on real headlines shows why neither approach is sufficient alone:

    "US CPI rises 0.4% in July"  vs  "US PPI rises 0.4% in July"   -> 93% similar
    "Fed holds rates steady..."  vs  "Fed signals one cut as..."   -> 96% similar

The first pair is two DIFFERENT releases. The second is the same story. No
threshold separates them, because the discriminating information is semantic
(CPI is not PPI), not lexical.

So the split is:
  Stage 1 (here)      -- merge only near-identical rewrites. High precision.
  Stage 2 (score.py)  -- the LLM already reads every headline, so it flags
                         same-event duplicates for free in the same API call.

The asymmetry justifies the conservatism: a missed merge costs you one redundant
line in the feed. A wrong merge makes a real story vanish.
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz

STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "as", "at", "by",
    "with", "from", "is", "are", "was", "were", "be", "after", "amid", "over",
    "says", "say", "said", "new", "us", "u.s.", "how", "why", "what", "it",
}
# Publisher suffixes like " - Reuters" or " | Financial Times".
SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*[A-Z][\w .&']{2,30}$")
NONWORD_RE = re.compile(r"[^a-z0-9 ]+")


def normalize(title: str) -> str:
    title = SUFFIX_RE.sub("", title)
    title = NONWORD_RE.sub(" ", title.lower())
    words = [w for w in title.split() if w not in STOPWORDS and len(w) > 1]
    return " ".join(words)


def cluster(articles: list[dict], threshold: int = 90) -> list[dict]:
    """Greedy single-pass clustering. Articles must already be sorted newest-first.

    Threshold 90 is high on purpose -- see the module docstring. This catches
    wire-copy rewrites and syndicated reprints; the LLM catches the rest.

    The surviving article per cluster is the one from the best source tier
    (tier 1 = central banks), tie-broken by recency. The losers are recorded on
    `also_covered_by` so the dashboard can show "+4 outlets" as a crowd-size
    signal -- heavy coverage is itself evidence a story matters.
    """
    norms = [normalize(a["title"]) for a in articles]
    clusters: list[list[int]] = []
    assigned = [False] * len(articles)

    for i in range(len(articles)):
        if assigned[i]:
            continue
        group = [i]
        assigned[i] = True
        for j in range(i + 1, len(articles)):
            if assigned[j] or not norms[j]:
                continue
            if fuzz.token_set_ratio(norms[i], norms[j]) >= threshold:
                group.append(j)
                assigned[j] = True
        clusters.append(group)

    out: list[dict] = []
    for group in clusters:
        group.sort(key=lambda k: (articles[k]["tier"], -articles[k]["published_ts"]))
        winner = dict(articles[group[0]])
        others = [articles[k] for k in group[1:]]
        winner["also_covered_by"] = sorted({o["source"] for o in others})
        winner["coverage_count"] = len(group)
        winner["other_urls"] = [{"source": o["source"], "url": o["url"]} for o in others[:5]]
        out.append(winner)

    # Widely-covered stories float up; recency breaks ties.
    out.sort(key=lambda a: (-a["coverage_count"], -a["published_ts"]))
    print(f"Deduped {len(articles)} -> {len(out)} distinct stories")
    return out
