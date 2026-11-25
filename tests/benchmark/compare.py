"""Compare two benchmark runs: per-query latency delta and correctness against golden answers.

Usage:
    python -m tests.benchmark.compare --run-a results/run_A.json --run-b results/run_B.json \
        --golden tests/benchmark/golden_answers.json

Correctness: when a query has a golden answer, a run is correct if its result count (length of the response's
``results`` list, or the body itself when it is a list) is within ``tolerance`` of ``expected``. Without a
golden answer a run is correct if the response is 2xx. A regression is a query that is correct in run A and
incorrect in run B. Exit status is 1 if any regression is found.
"""

import argparse
import json
import statistics
import sys
from pathlib import Path


def result_count(body: object) -> int | None:
    """Return the number of results in a response body, or None when it has no result list."""
    if isinstance(body, list):
        return len(body)
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        return len(body["results"])
    return None


def is_correct(result: dict, golden: dict | None) -> bool:
    """Check one result against its golden answer, or for a 2xx status when there is none."""
    if not 200 <= result["status"] < 300:
        return False
    if golden is None:
        return True
    count = result_count(result["body"])
    return count is not None and abs(count - golden["expected"]) <= golden.get("tolerance", 0)


def load_results(path: Path) -> dict[str, dict]:
    """Load a run file as a mapping of query id to result."""
    return {r["id"]: r for r in json.loads(path.read_text())["results"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-a", required=True, type=Path, help="Baseline run (for example the Neptune stack)")
    parser.add_argument("--run-b", required=True, type=Path, help="Candidate run (for example the Aurora stack)")
    parser.add_argument("--golden", type=Path, default=Path(__file__).parent / "golden_answers.json")
    args = parser.parse_args()

    run_a, run_b = load_results(args.run_a), load_results(args.run_b)
    golden = {k: v for k, v in json.loads(args.golden.read_text()).items() if not k.startswith("_")}

    rows, regressions, deltas = [], [], []
    for query_id in sorted(run_a.keys() & run_b.keys()):
        a, b = run_a[query_id], run_b[query_id]
        delta = b["latencyMs"] - a["latencyMs"]
        deltas.append(delta)
        ok_a, ok_b = is_correct(a, golden.get(query_id)), is_correct(b, golden.get(query_id))
        if ok_a and not ok_b:
            regressions.append(query_id)
        rows.append((query_id, a["latencyMs"], b["latencyMs"], delta, "ok" if ok_b else "FAIL"))

    print("| query | A ms | B ms | delta ms | B correct |")
    print("|---|---|---|---|---|")
    for query_id, a_ms, b_ms, delta, verdict in rows:
        print(f"| {query_id} | {a_ms:.1f} | {b_ms:.1f} | {delta:+.1f} | {verdict} |")

    if deltas:
        print(
            f"\nQueries compared: {len(deltas)}; median delta {statistics.median(deltas):+.1f} ms; "
            f"mean delta {statistics.fmean(deltas):+.1f} ms"
        )
    print(f"Correctness regressions: {len(regressions)}")
    for query_id in regressions:
        print(f"  - {query_id}")
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(main())
