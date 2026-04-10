from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.issue11_validation import compare_summaries, load_trade_records, summarize_records


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue #11 validation report from trade history JSONL.")
    parser.add_argument(
        "--history",
        default="/tmp/crypto-runtime/live_events.jsonl",
        help="Current runtime events JSONL file",
    )
    parser.add_argument("--baseline", default="", help="Optional baseline history JSONL file for before/after comparison")
    args = parser.parse_args()

    current_path = Path(args.history).resolve()
    current_rows = load_trade_records(current_path)
    current_summary = summarize_records(current_rows)
    payload = {
        "history_file": str(current_path),
        "current": current_summary,
    }

    if args.baseline:
        baseline_path = Path(args.baseline).resolve()
        baseline_rows = load_trade_records(baseline_path)
        baseline_summary = summarize_records(baseline_rows)
        payload["baseline_file"] = str(baseline_path)
        payload["baseline"] = baseline_summary
        payload["delta"] = compare_summaries(baseline_summary, current_summary)

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
