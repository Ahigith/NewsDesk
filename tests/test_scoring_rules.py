"""Calibration tests for the rule-based scorer (LLM_PROVIDER=none).

This path runs with no model and no account, so nothing else catches it when a
keyword edit quietly breaks the ranking. Written after a real bug: "repo" was
matching inside "Reported", which scored a biotech board reshuffle as a rates
story. Run: python -m tests.test_scoring_rules
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import score as S  # noqa: E402


def rank(title, summary="", coverage=1):
    art = {"title": title, "summary": summary, "coverage_count": coverage, "hint": ""}
    r = S._heuristic([art])[0]
    total = 0.35 * r["s_entity"] + 0.35 * r["s_scope"] + 0.30 * r["s_surprise"]
    return round(total), r


def main():
    fails = []

    def expect(cond, msg):
        if not cond:
            fails.append(msg)

    # --- no substring false positives (the "Reported" / "repo" bug) ---
    _, r = rank("Small-cap biotech Zynex announces board reshuffle",
                "Reported by Yahoo Finance.")
    expect("Rates" not in r["asset_classes"],
           f'"Reported" leaked into Rates: {r["asset_classes"]}')
    _, r = rank("Company updates corporate governance policy")
    expect("Rates" not in r["asset_classes"], '"corporate" leaked into Rates')
    _, r = rank("Federal agency issues new guidance")
    expect(r["s_entity"] != 100.0, '"Federal agency" wrongly matched "federal reserve"/"fed"')

    # --- ordering invariants that must hold ---
    cpi, _ = rank("US CPI rises 0.4% in July, hotter than the 0.2% consensus")
    fomc, _ = rank("Federal Reserve issues FOMC statement: rates held at 3.75-4.00%")
    rbi, _ = rank("RBI holds repo rate at 5.50%, cuts CRR by 25bp")
    biotech, _ = rank("Small-cap biotech Zynex announces board reshuffle")
    opinion, _ = rank("Opinion: why the bond market keeps getting it wrong")
    wrap, _ = rank("Markets wrap: stocks drift ahead of jobs data")

    expect(cpi > biotech, f"CPI ({cpi}) should outrank a biotech reshuffle ({biotech})")
    expect(fomc > biotech, f"FOMC ({fomc}) should outrank a biotech reshuffle ({biotech})")
    expect(rbi > biotech, f"RBI ({rbi}) should outrank a biotech reshuffle ({biotech})")
    expect(opinion < 40, f"opinion piece should score <40, got {opinion}")
    expect(wrap < 55, f"markets wrap should score <55, got {wrap}")
    expect(cpi >= 70, f"a consensus-beating CPI print should be tier 1, got {cpi}")
    expect(rbi >= 70, f"an RBI repo decision should be tier 1, got {rbi}")

    # --- region detection ---
    for title, want in [
        ("US CPI rises 0.4% in July", "US"),
        ("RBI holds repo rate at 5.50%", "India"),
        ("Nifty 50 closes at record high", "India"),
        ("ECB signals pause as eurozone inflation cools", "Europe"),
        ("Bank of England holds Bank Rate", "UK"),
        ("PBOC cuts reserve requirement ratio", "China"),
        ("BOJ shifts yield curve control", "Japan"),
        ("Brent crude rallies on supply concerns", "Global"),
    ]:
        got = S._guess_region(title.lower())
        expect(got == want, f'region for "{title[:38]}": got {got}, want {want}')

    # --- "record high" must not read as surprise ---
    expect(S._surprise_score("nifty closes at record high") <= 45,
           "record high should not count as a surprise")

    # --- sentiment ---
    _, r = rank("Nvidia beats on earnings, shares rally")
    expect(r["sentiment"] == "Bullish", f'expected Bullish, got {r["sentiment"]}')
    _, r = rank("Shares tumble as company cuts guidance")
    expect(r["sentiment"] == "Bearish", f'expected Bearish, got {r["sentiment"]}')

    # --- every score stays in range ---
    for t in ["", "a", "Fed CPI GDP PPI surge plunge shock beats misses unexpected"]:
        total, r = rank(t)
        expect(0 <= total <= 100, f"score out of range for {t!r}: {total}")
        expect(r["region"] in S.REGIONS, f"bad region {r['region']!r}")

    if fails:
        print("FAILURES:")
        for f in fails:
            print("  x " + f)
        return 1
    print(f"Calibration OK.  CPI={cpi} FOMC={fomc} RBI={rbi} "
          f"biotech={biotech} wrap={wrap} opinion={opinion}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
