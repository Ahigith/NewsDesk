"""Tolerant-JSON parser tests. Small free models wrap output in prose or
markdown fences and sometimes truncate; all of that must be recoverable.
Run: python -m tests.test_json_parser
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.llm import extract_results as e
cases = [
 ('{"results":[{"i":0,"category":"X"}]}', 1, "plain object"),
 ("```json\n{\"results\":[{\"i\":0},{\"i\":1}]}\n```", 2, "markdown fenced"),
 ("```\n[{\"i\":0},{\"i\":1},{\"i\":2}]\n```", 3, "bare fence"),
 ('Here you go:\n[{"i":0},{"i":1},{"i":2}]', 3, "prose prefix"),
 ('{"results":[{"i":0},{"i":1},{"i":2', 2, "truncated mid-object"),
 ('[{"i":0,"a":{"b":1}},{"i":1}]', 2, "nested objects"),
 ('{"items":[{"i":5}]}', 1, "alt key name"),
 ('total garbage', 0, "junk"),
 ('', 0, "empty"),
]
bad=0
for t,exp,label in cases:
    got=len(e(t)); ok = got==exp
    bad += not ok
    print(('ok  ' if ok else 'FAIL'), f'{got}/{exp}', label)
print("\nAll parser cases passed." if not bad else f"\n{bad} FAILURES")
sys.exit(1 if bad else 0)
