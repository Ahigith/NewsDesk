# The Desk — a daily market & macro brief that runs itself

A static news dashboard that rebuilds every weekday morning, ranks overnight
stories by market impact, and publishes to a free URL you can share with friends.

Covers **US, Europe, and India** across macro, monetary policy, markets,
corporate results, geopolitics and tech — with a home-region weighting so Indian
and US stories surface above equally-newsworthy items elsewhere.

**No server. No database. No Redis. No monthly bill — free tier end to end.**

---

## Why this shape, and not the stack you sketched

Your original architecture (FastAPI + Next.js + PostgreSQL + Redis on a VPS) is
the correct design for a product with users, logins, and live data. It is the
wrong design for *this*, and here is the reasoning — worth understanding, because
it's the same trade-off you'll face on every side project.

| Your plan | What's here | Why |
|---|---|---|
| FastAPI backend | A Python script | The dashboard changes once a day. A server that sits idle 86,399 seconds out of 86,400 is pure overhead. |
| PostgreSQL | JSON files in `data/`, committed to git | You need ~200 KB/day of append-only history. Git already versions, backs up, and diffs it for free. If you later want SQL, `pandas.read_json` over the folder gets you there in five lines. |
| Redis cache | Nothing | Caching exists to avoid recomputing on request. Nothing is computed on request — the HTML is already built. |
| Next.js frontend | One HTML file | Zero build step, zero npm, loads instantly, works forever. |
| Paid frontier model | Free-tier Groq/Gemini, or no model at all | This is classification, not reasoning. Daily volume (~15 requests) fits inside free tiers with room to spare, and a rule-based scorer covers the no-account case. |
| Vector embeddings for dedupe | Two-stage: fuzzy match, then the LLM | Neither alone works. `"US CPI rises 0.4%"` and `"US PPI rises 0.4%"` are 93% string-similar but different events. So stage 1 merges only near-identical rewrites, and the scorer — which already reads every headline — flags same-event duplicates in the same API call. No embedding cost. |
| Playwright scraping | RSS only | Scraping paywalled sites is fragile and legally murky. RSS gives you headline + summary, which is all the scorer needs. |

The real insight: **your dashboard is a batch job, not an application.** Batch
jobs want cron and files. Applications want servers and databases. Picking the
wrong one costs you money and, worse, maintenance attention.

If this ever grows real users, per-user watchlists, or intraday refresh — then
you graduate to your original stack. Not before.

---

## Where Claude Code fits (and how you stop depending on it)

Claude Code's job here is **authoring**, not **running**. Use it to write and
edit the code — adding feeds, tuning the scoring prompt, restyling the page.
Once the repo is pushed, GitHub Actions executes it on a schedule with no AI
tool in the loop at all.

The only ongoing AI dependency is one HTTPS request inside `src/llm.py` — the
same category of dependency as calling FRED, and pointed at whichever free
provider you choose. Want to remove even that? `LLM_PROVIDER=none` drops it
entirely and the dashboard still builds. There is no point at which this project
requires an Anthropic subscription, an API bill, or Claude Code to keep running.

---

## Setup (about 20 minutes)

### 1. Get the code onto GitHub

Create a new **public** repo (public is required for free GitHub Pages), then
from this folder:

```bash
git init
git add .
git commit -m "initial"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/newsdesk.git
git push -u origin main
```

### 2. Get a model API key (free)

The scoring model is pluggable. Pick one and set `LLM_PROVIDER` to match:

| `LLM_PROVIDER` | Cost | Sign-up | Notes |
|---|---|---|---|
| **`groq`** *(default)* | Free | <https://console.groq.com/keys> | No card. Llama 3.3 70B, very fast. Best starting point. |
| `gemini` | Free tier | <https://aistudio.google.com/apikey> | No card. Gemini 2.0 Flash. Strongest free option for strict JSON. |
| `cerebras` | Free tier | <https://cloud.cerebras.ai> | No card. Llama 3.3 70B. |
| `openrouter` | Free `:free` models | <https://openrouter.ai/keys> | No card. Lowest daily cap of the four. |
| `anthropic` | ~$3-5/mo | <https://console.anthropic.com> | Paid. Noticeably better takeaways and dedupe. |
| `none` | Free | - | No model at all. Rule-based scoring, see below. |

**Why a free tier is genuinely enough here.** After deduping and the article
cap, a daily run is about 10-15 requests totalling well under 200k tokens.
Groq's free tier alone allows that many times over. This isn't squeezing a
production workload into a free plan - the workload is simply small.

**The trade-off, stated honestly.** Smaller free models are worse at two things:
writing a sharp one-line takeaway, and spotting that two differently-worded
headlines describe the same event (the stage-2 dedupe). Ranking quality is
close. If takeaways start reading like restated headlines, that's the model, not
the prompt - try `gemini` before paying for anything.

### 3. Get a FRED key (free, 30 seconds, no card)

<https://fredaccount.stlouisfed.org/apikeys>. Powers the economic calendar.
Skip it if you like; that panel just stays empty.

### 4. Add both as repo secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `GROQ_API_KEY` | your Groq key (or whichever provider you chose) |
| `FRED_API_KEY` | your FRED key |

If you picked a provider other than Groq, also add a repo **variable** (same
page, Variables tab) named `LLM_PROVIDER` set to `gemini`, `cerebras`,
`openrouter`, `anthropic` or `none`.

### 5. Turn on Pages

Repo → **Settings → Pages → Source: GitHub Actions**.

### 6. Run it once by hand

Repo → **Actions → Daily brief → Run workflow**. Takes 2–4 minutes.
Your site appears at `https://YOUR-USERNAME.github.io/newsdesk/`.

That's it. It now runs itself at 10:00 UTC every weekday.

---

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then paste your keys into .env
set -a; source .env; set +a # Windows PowerShell: use $env:ANTHROPIC_API_KEY="..."
python -m src.build
open site/index.html
```

Useful flags and checks:

```bash
python -m src.build --no-markets   # news only, skips all market data (fast)
python -m src.verify_feeds         # which RSS feeds are alive right now
python -m src.verify_data          # which market symbols / FRED series resolve
python -m tests.test_offline       # full pipeline, no network, no API key
python -m tests.test_dedupe_stage2 # duplicate-merge logic
python -m tests.test_scoring_rules # rule-based scorer calibration
python -m tests.test_json_parser   # tolerance for messy model output
```

```bash
LLM_PROVIDER=none python -m src.build   # no model, no account, no cost
```

If the chosen provider's key is missing, or a batch fails or gets rate-limited,
the pipeline doesn't fail - it falls back to the rule-based scorer for whatever
went unscored, and tells you how many stories that affected.

---

## Customizing

**Change which regions you care about** → the `NEWSDESK_HOME_REGIONS` env var,
default `India,US`. Stories in those regions get `NEWSDESK_HOME_BOOST` points
(default 12) added to their objective score. Set it to `""` to rank purely on
global market impact. See the India section below for why this exists.

**Add a news source** → append to `config/feeds.yml`, then run
`python -m src.verify_feeds` to confirm the URL actually returns entries.
`tier: 1` means "authoritative, wins ties when deduping".

**Change what counts as important** → edit `SYSTEM_PROMPT` in `src/score.py`.
The weights (0.35 entity / 0.35 scope / 0.30 surprise) are applied in Python at
the bottom of that file, so you can retune them without touching the prompt.

**Add a ticker or macro series** → `config/markets.yml`, then run
`python -m src.verify_data` to confirm every symbol still resolves. FRED series
IDs come from the URL of any chart on <https://fred.stlouisfed.org>; Stooq
symbols from the URL on <https://stooq.com>.

**Restyle** → all CSS lives in the `<style>` block at the top of
`src/template.html`.

**Change the schedule** → the `cron` line in `.github/workflows/daily.yml`.

---

## Running with no model at all

`LLM_PROVIDER=none` skips the API entirely. The rule-based scorer in
`src/score.py` approximates the same three-part rubric using word-boundary
keyword matching: entity tier from who is named (central banks and scheduled
statistical releases at the top, unknown small-caps at the bottom), scope from
how many asset classes the headline touches plus how widely it is syndicated,
and surprise from language like *"hotter than"*, *"beats"*, *"unexpectedly"*.

On the test fixtures it ranks:

```
81  US CPI rises 0.4%, hotter than the 0.2% consensus
75  Federal Reserve issues FOMC statement
75  RBI holds repo rate at 5.50%, cuts CRR by 25bp
41  Markets wrap: stocks drift ahead of jobs data
36  Small-cap biotech announces board reshuffle
30  Opinion: why the bond market keeps getting it wrong
```

That ordering is the whole point, and `tests/test_scoring_rules.py` locks it in.

**What you lose without a model:** the takeaway line falls back to the
publisher's own summary rather than an actual insight, and stage-2 dedupe stops
working, so a few more near-duplicates get through. **What you keep:** the
ranking, categories, regions, asset-class tags, and the entire dashboard. This
is a reasonable permanent setup, not just a fallback.

One caution: keyword rules are blind to entities they have never heard of. An
important story about a company not on the list defaults to the middle of the
pack. Add names to `_ENTITY_75` in `src/score.py` as you spot gaps.

---

## India coverage

India is a first-class region here, not an afterthought, because a US-only feed
would miss half of what you actually need.

**Sources.** RBI press releases and notifications, PIB's Finance Ministry feed,
Economic Times (markets + economy), Business Standard, Mint, Moneycontrol, and
Hindu BusinessLine. RBI and PIB sit at `tier: 1` alongside the Fed and the BLS —
the repo-rate decision, the CPI/WPI print and the Union Budget land there before
any journalist writes them up.

**Market data.** Nifty 50, Sensex and USD/INR in the ticker bar; USD/INR, the
10Y G-sec yield, CPI, industrial production and GDP growth in the macro
scorecard.

**Scoring.** This needed a real design decision. The objective rubric ranks by
global market impact, and on that measure an RBI repo decision genuinely matters
less to world rates than an FOMC decision. But it matters far *more* to you. So
rather than distorting the rubric, the two are kept separate:

```
importance = global_impact_score  +  home_region_boost
```

Both are stored on every story (`global_importance`, `home_boost`, `importance`),
so you can always see what the model actually judged versus what your own
preferences added. The green region badge on the dashboard marks boosted stories.
The rubric itself was also amended to place RBI, SEBI, MOSPI releases and
Nifty-50 heavyweight earnings at the same entity tier as the PBOC and OPEC+ —
that part is a genuine correction, not a preference.

**Known gaps, stated plainly:**

- **No India yield curve by tenor.** FRED carries only a single India long-term
  yield. The full G-sec curve lives with CCIL/RBI, which has no free API. The
  bottom-left chart is US-only; the India 10Y appears in the scorecard instead.
- **Several India FRED series are OECD-sourced and fragile.** FRED retired a
  batch of OECD MEI series, and `INDCPIALLMINMEI` / `INDPROINDQISMEI` may be
  among the casualties. **Run `python -m src.verify_data` before your first real
  build** — it checks every symbol and prints exactly which lines to delete.
  `DEXINUS` and `IRLTLT01INM156N` are the two most durable India series.
- **The economic calendar is US-only.** It's driven by FRED's release calendar,
  which doesn't cover MOSPI or RBI. Indian release dates would need scraping
  RBI's calendar page — doable, but it's a separate piece of work. RBI's own RSS
  feed does surface the releases as they happen.

---

## What it costs

Running the recommended setup: **nothing.**

| Item | Cost |
|---|---|
| GitHub Actions (public repo) | Free — unlimited minutes |
| GitHub Pages hosting | Free |
| RSS feeds, Stooq, FRED, Nasdaq calendar | Free, no keys except FRED |
| Groq / Gemini / Cerebras / OpenRouter free tier | **$0** |
| `LLM_PROVIDER=none` | **$0** |
| Anthropic (optional upgrade, ~200 stories/day) | ~$3-5/month |

---

## Known limits, stated plainly

- **Market data is end-of-day, not live.** Stooq and FRED publish daily closes.
  For a 6am prep dashboard that's the right data anyway. Live tickers would
  require a paid feed and a client-side fetch.
- **RSS URLs rot.** Reuters and Bloomberg already killed their public feeds, and
  Indian outlets reshuffle their feed paths fairly often. Run `verify_feeds`
  monthly.
- **The earnings calendar is best-effort.** It uses an undocumented Nasdaq
  endpoint that sometimes rate-limits. It fails quietly.
- **Free-tier models are rate-limited.** The pipeline backs off and retries on
  429s, then falls back to rule-based scoring for anything still unscored. A run
  never fails outright, but on a bad day some takeaways will be plainer.
- **The scorer is a triage aid, not a signal.** It ranks what to read first. It
  has no view on what to buy.
- **Headlines only.** We store and display headline + publisher summary, and
  always link out. Don't modify this to scrape article bodies from paywalled
  outlets — that's both a legal and a technical dead end.

---

## Project layout

```
config/feeds.yml        RSS sources, with tier and category hints
config/markets.yml      tickers, yield curves, macro series, watched releases
src/ingest.py           parallel RSS fetch -> normalized article dicts
src/dedupe.py           fuzzy clustering of the same story across outlets
src/llm.py              pluggable model backend (Groq/Gemini/Cerebras/OR/Anthropic)
src/score.py            batched scoring, weighted rubric, rule-based fallback
src/markets.py          Stooq / FRED / calendars, all fail-soft
src/build.py            orchestrator: run this
src/template.html       the entire frontend, one file
src/verify_feeds.py     RSS feed health check
src/verify_data.py      market symbol + FRED series health check
data/YYYY-MM-DD.json    daily archive (this is your database)
site/                   published output
```
