"""`python -m src.diagnose_data` -- find out which market-data sources your
network can actually reach, and how fast.

Written because verify_data reported everything dead in two different ways:
Stooq returned 200 with a body that wasn't CSV, and FRED timed out at the
network level. Those need opposite fixes, so this measures rather than guesses.

Prints a verdict at the end. Paste the whole output back.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta

import requests

TIMEOUT = 40
BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

results: dict[str, bool] = {}


def probe(label: str, url: str, expect: str, headers=None, attempts: int = 2) -> bool:
    """expect: 'csv' | 'json' | 'any'"""
    for attempt in range(1, attempts + 1):
        started = time.time()
        try:
            r = requests.get(url, headers=headers or BROWSER, timeout=TIMEOUT)
            elapsed = time.time() - started
            head = (r.text or "")[:110].replace("\n", "\\n").replace("\r", "")
            ok = r.status_code == 200

            if ok and expect == "csv":
                ok = "Date" in r.text[:60] or "observation_date" in r.text[:60]
            elif ok and expect == "json":
                try:
                    body = json.loads(r.text)
                    ok = bool(body.get("chart", {}).get("result"))
                except Exception:
                    ok = False

            flag = "PASS" if ok else "FAIL"
            print(f"  [{flag}] {label}")
            print(f"         {elapsed:5.1f}s  HTTP {r.status_code}  {len(r.content):>7} bytes")
            print(f"         body: {head or '(empty)'}")
            if ok:
                return True
            if attempt < attempts:
                print(f"         retrying ({attempt + 1}/{attempts})...")
        except Exception as exc:
            elapsed = time.time() - started
            print(f"  [FAIL] {label}")
            print(f"         {elapsed:5.1f}s  {type(exc).__name__}: {str(exc)[:90]}")
            if attempt < attempts:
                print(f"         retrying ({attempt + 1}/{attempts})...")
    return False


d2 = date.today()
d1 = d2 - timedelta(days=30)

print("=" * 74)
print("1. FRED  (US yields + all macro series)")
print("=" * 74)
results["fred_short"] = probe(
    "FRED DGS10, 90-day window",
    f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd={d2 - timedelta(days=90)}",
    "csv",
)
results["fred_full"] = probe(
    "FRED DGS10, full history (bigger download)",
    "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10",
    "csv",
)

print()
print("=" * 74)
print("2. STOOQ  (currently used for indices, FX, commodities)")
print("=" * 74)
results["stooq_daily"] = probe(
    "Stooq daily history ^spx",
    f"https://stooq.com/q/d/l/?s=^spx&d1={d1:%Y%m%d}&d2={d2:%Y%m%d}&i=d",
    "csv",
)
results["stooq_quote"] = probe(
    "Stooq light quote ^spx",
    "https://stooq.com/q/l/?s=^spx&f=sd2t2ohlcv&h&e=csv",
    "csv",
)

print()
print("=" * 74)
print("3. YAHOO FINANCE  (candidate replacement for Stooq)")
print("=" * 74)
results["yahoo_us"] = probe(
    "Yahoo chart ^GSPC (S&P 500)",
    "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=1mo&interval=1d",
    "json",
)
results["yahoo_in"] = probe(
    "Yahoo chart ^NSEI (Nifty 50)",
    "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?range=1mo&interval=1d",
    "json",
)
results["yahoo_fx"] = probe(
    "Yahoo chart USDINR=X",
    "https://query1.finance.yahoo.com/v8/finance/chart/USDINR=X?range=1mo&interval=1d",
    "json",
)

print()
print("=" * 74)
print("VERDICT")
print("=" * 74)
fred_ok = results["fred_short"] or results["fred_full"]
stooq_ok = results["stooq_daily"] or results["stooq_quote"]
yahoo_ok = results["yahoo_us"] or results["yahoo_in"]

print(f"  FRED   : {'reachable' if fred_ok else 'NOT reachable'}"
      f"{'  (short window only -- needs smaller requests)' if results['fred_short'] and not results['fred_full'] else ''}")
print(f"  Stooq  : {'reachable' if stooq_ok else 'NOT reachable'}")
print(f"  Yahoo  : {'reachable' if yahoo_ok else 'NOT reachable'}")
print()
if yahoo_ok and not stooq_ok:
    print("  -> Switch the ticker bar from Stooq to Yahoo.")
elif stooq_ok:
    print("  -> Stooq works; the earlier failure was transient or rate-limiting.")
else:
    print("  -> Neither ticker source works. Likely a network-level block")
    print("     (ISP, VPN, corporate DNS, or antivirus TLS inspection).")
    print("     Worth retrying on a different network before changing code --")
    print("     and note GitHub Actions runs on its own network, so the")
    print("     dashboard may build fine in the cloud even if it fails locally.")
if not fred_ok:
    print("  -> FRED unreachable locally. Same caveat: test on another network.")
