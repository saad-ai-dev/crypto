from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.bulk_backtester import BulkBacktester
from src.cache_loader import load_market_datasets_from_cache
from src.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Live multi-coin bulk optimization/backtest")
    parser.add_argument("--config", default="config.json", help="Path to config.json")
    parser.add_argument("--target-trades", type=int, default=200, help="Total closed trades target")
    parser.add_argument("--min-wins", type=int, default=150, help="Minimum wins target")
    parser.add_argument(
        "--symbols",
        default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,TRXUSDT",
        help="Comma-separated Binance futures symbols",
    )
    parser.add_argument("--timeframes", default="5m,15m", help="Comma-separated timeframes")
    parser.add_argument("--history-limit", type=int, default=1500, help="Candles per market")
    parser.add_argument(
        "--target-per-market",
        type=int,
        default=20,
        help="Max closed trades collected from each symbol/timeframe market",
    )
    parser.add_argument(
        "--apply-best",
        action="store_true",
        help="Write best strategy params back to config.json",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="Use pre-fetched live JSON files from this directory instead of direct API calls",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(str(config_path))

    # Hard guard for real-market-only run.
    config.setdefault("data_source", {})["force_mock"] = False
    config["data_source"]["allow_mock_fallback"] = False

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    runner = BulkBacktester(config)
    if args.cache_dir:
        datasets = load_market_datasets_from_cache(
            cache_dir=args.cache_dir,
            symbols=symbols,
            timeframes=timeframes,
        )
    else:
        datasets = runner.build_datasets(
            symbols=symbols,
            timeframes=timeframes,
            history_limit=args.history_limit,
        )

    best, tested = runner.optimize(
        datasets=datasets,
        target_total_trades=args.target_trades,
        min_wins_target=args.min_wins,
        target_per_market=args.target_per_market,
    )

    report = {
        "tested_candidates": tested,
        "target_trades": args.target_trades,
        "target_wins": args.min_wins,
        "actual_trades": best.trades,
        "actual_wins": best.wins,
        "actual_losses": best.losses,
        "win_rate": round(best.win_rate, 4),
        "expectancy_r": round(best.expectancy_r, 4),
        "expectancy_usd_per_trade": round(best.expectancy_usd, 6),
        "best_strategy": best.strategy,
        "per_market": best.per_market,
    }

    print(json.dumps(report, indent=2))

    if args.apply_best:
        cfg_latest = load_config(str(config_path))
        cfg_latest.setdefault("data_source", {})["force_mock"] = False
        cfg_latest["data_source"]["allow_mock_fallback"] = False
        cfg_latest["strategy"] = best.strategy
        config_path.write_text(json.dumps(cfg_latest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
