from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_config
from src.scanner import MarketScanner


def main() -> None:
    parser = argparse.ArgumentParser(description="Data-only crypto futures signal scanner")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle")
    args = parser.parse_args()

    config = load_config(str(Path(args.config)))
    scanner = MarketScanner(config)

    if args.once:
        scanner.run_once()
        return

    scanner.run_forever()


if __name__ == "__main__":
    main()
