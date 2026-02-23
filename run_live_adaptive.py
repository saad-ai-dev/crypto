from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.config import load_config
from src.live_adaptive_trader import LiveAdaptivePaperTrader


def main() -> None:
    parser = argparse.ArgumentParser(description="Live adaptive paper trader (real market data, no order placement)")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    args = parser.parse_args()

    config = load_config(str(Path(args.config)))
    trader = LiveAdaptivePaperTrader(config)
    result = trader.run()
    print(json.dumps({"type": "FINAL", "result": result}, indent=2))


if __name__ == "__main__":
    main()
