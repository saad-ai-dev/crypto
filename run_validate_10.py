from __future__ import annotations

import argparse
from pathlib import Path

from src.config import load_config
from src.validator import TenTradeValidator


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate 10 sequential closed paper trades")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    args = parser.parse_args()

    config = load_config(str(Path(args.config)))
    validator = TenTradeValidator(config)
    validator.run()


if __name__ == "__main__":
    main()
