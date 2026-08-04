"""Unit tests for the LLM-duplicate merge path, which the offline fixture run
cannot exercise (it never calls the model). Run: python -m tests.test_dedupe_stage2
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.score import _order_for_batching, merge_duplicates  # noqa: E402


def story(sid, title, imp, src, cov=1, dup=None):
    d = {"id": sid, "title": title, "importance": imp, "tier": 1 if imp >= 70 else 3,
         "source": src, "url": f"http://x/{sid}", "coverage_count": cov,
         "also_covered_by": [], "other_urls": [], "takeaway": f"take-{sid}"}
    if dup:
        d["_dup_of_id"] = dup
    return d


def main():
    fails = []

    # 1. child folds into parent; parent inherits the higher score
    out = merge_duplicates([
        story("a", "Fed holds rates", 55, "CNBC", cov=2),
        story("b", "Federal Reserve keeps policy unchanged", 81, "FT", dup="a"),
        story("c", "Nvidia beats", 40, "WSJ"),
    ])
    ids = {s["id"] for s in out}
    if ids != {"a", "c"}:
        fails.append(f"expected {{a,c}} survivors, got {ids}")
    parent = next(s for s in out if s["id"] == "a")
    if parent["importance"] != 81:
        fails.append(f"parent should inherit 81, got {parent['importance']}")
    if parent["tier"] != 1:
        fails.append("parent tier should upgrade to 1")
    if parent["coverage_count"] != 3:
        fails.append(f"coverage should be 3, got {parent['coverage_count']}")
    if "FT" not in parent["also_covered_by"]:
        fails.append("FT missing from also_covered_by")

    # 2. dangling / self-referential parents are ignored, not crashed on
    out = merge_duplicates([story("a", "x", 50, "S", dup="ghost"), story("b", "y", 50, "S", dup="b")])
    if len(out) != 2:
        fails.append(f"bad dup refs should be ignored, got {len(out)} survivors")

    # 3. no _dup_of_id keys leak into the published payload
    if any("_dup_of_id" in s for s in out):
        fails.append("_dup_of_id leaked into output")

    # 4. batching order co-locates similar headlines
    arts = [{"title": t} for t in [
        "Nvidia beats on earnings", "Fed holds rates steady",
        "Apple unveils new model", "Fed signals one cut in 2026"]]
    order = _order_for_batching(arts)
    pos = {arts[i]["title"]: p for p, i in enumerate(order)}
    if abs(pos["Fed holds rates steady"] - pos["Fed signals one cut in 2026"]) != 1:
        fails.append(f"Fed headlines not adjacent after ordering: {order}")

    if fails:
        print("FAILURES:")
        for f in fails:
            print("  x " + f)
        return 1
    print("All stage-2 dedupe checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
