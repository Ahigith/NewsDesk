"""`python -m src.verify_data` -- check every symbol and series ID in markets.yml.

Worth running after any edit to config/markets.yml, and once a quarter anyway.
Stooq retires ticker symbols and FRED periodically discontinues series (it
retired a large batch of OECD-sourced series, which is where several of the
India and Euro Area entries come from). This tells you exactly which lines to
delete instead of leaving silently-empty tiles on the dashboard.
"""
from pathlib import Path

import yaml

from src import markets

ROOT = Path(__file__).resolve().parent.parent
OK, BAD = "OK  ", "DEAD"


def check_stooq(symbol: str) -> tuple[bool, str]:
    series = markets.stooq_series(symbol, days=30)
    return (len(series) >= 2, f"{len(series)} obs, last {series[-1][0]}" if series else "no data")


def check_fred(series_id: str) -> tuple[bool, str]:
    series = markets.fred_series(series_id)
    return (len(series) >= 2, f"{len(series)} obs, last {series[-1][0]}" if series else "no data")


def main() -> int:
    cfg = yaml.safe_load(open(ROOT / "config" / "markets.yml", encoding="utf-8"))
    dead: list[str] = []

    print("=== ticker_bar ===")
    for spec in cfg["ticker_bar"]:
        kind = "stooq" if spec.get("stooq") else "fred"
        ident = spec.get("stooq") or spec.get("fred")
        good, note = (check_stooq if kind == "stooq" else check_fred)(ident)
        print(f"{OK if good else BAD} {spec['label']:<14} {kind}:{ident:<22} {note}")
        if not good:
            dead.append(f"ticker_bar / {spec['label']} ({ident})")

    print("\n=== yield_curves ===")
    for country, tenors in cfg["yield_curves"].items():
        for tenor, sid in tenors.items():
            good, note = check_fred(sid)
            print(f"{OK if good else BAD} {country} {tenor:<5} fred:{sid:<22} {note}")
            if not good:
                dead.append(f"yield_curves / {country} {tenor} ({sid})")

    print("\n=== macro_scorecard ===")
    for spec in cfg["macro_scorecard"]:
        good, note = check_fred(spec["fred"])
        label = f"{spec['country']} {spec['metric']}"
        print(f"{OK if good else BAD} {label:<34} fred:{spec['fred']:<22} {note}")
        if not good:
            dead.append(f"macro_scorecard / {label} ({spec['fred']})")

    print()
    if dead:
        print(f"{len(dead)} dead entries -- remove these lines from config/markets.yml:")
        for d in dead:
            print("  x " + d)
        return 1
    print("All symbols and series resolved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
