from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable, List

from src.cache_loader import load_market_datasets_from_cache
from src.config import load_config
from src.ml_pipeline import MLWalkForwardOptimizer, WalkForwardResult


def list_missing_cache_files(cache_dir: str, symbols: Iterable[str], timeframes: Iterable[str]) -> List[str]:
    base = Path(cache_dir)
    missing: List[str] = []
    for symbol in symbols:
        for name in (f"{symbol}_premium.json", f"{symbol}_open_interest.json"):
            path = base / name
            if not path.exists():
                missing.append(str(path))
        for timeframe in timeframes:
            path = base / f"{symbol}_{timeframe}_klines.json"
            if not path.exists():
                missing.append(str(path))
    return missing


def should_apply_candidate_over_baseline(candidate: WalkForwardResult, baseline: WalkForwardResult) -> bool:
    expectancy_delta = float(candidate.expectancy_r) - float(baseline.expectancy_r)
    if float(candidate.expectancy_r) <= 0.0:
        return False
    if expectancy_delta < 0.01:
        return False
    if float(candidate.win_rate) + 0.02 < float(baseline.win_rate):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="ML walk-forward optimizer for crypto futures signals")
    parser.add_argument("--config", default="config.json", help="Path to config JSON")
    parser.add_argument(
        "--cache-dir",
        default="/Users/user/Desktop/Work/gotoapi/crypto/data/live",
        help="Directory containing live cached Binance JSON files",
    )
    parser.add_argument(
        "--symbols",
        default="BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,TRXUSDT",
        help="Comma-separated symbols",
    )
    parser.add_argument("--timeframes", default="5m,15m", help="Comma-separated timeframes")
    parser.add_argument("--target-trades", type=int, default=200)
    parser.add_argument("--target-wins", type=int, default=150)
    parser.add_argument("--max-candidates", type=int, default=48)
    parser.add_argument("--max-candles", type=int, default=500, help="Use only most recent N candles per market")
    parser.add_argument(
        "--single-strategy",
        action="store_true",
        help="Skip candidate search and evaluate only the strategy currently in config.json",
    )
    parser.add_argument(
        "--initial-train-frac",
        type=float,
        default=0.55,
        help="Initial training fraction for walk-forward when --single-strategy is used",
    )
    parser.add_argument(
        "--fee-bps-per-side",
        type=float,
        default=-1.0,
        help="Trading fee in bps per side; use negative value to read from config.execution",
    )
    parser.add_argument(
        "--slippage-bps-per-side",
        type=float,
        default=-1.0,
        help="Slippage in bps per side; use negative value to read from config.execution",
    )
    parser.add_argument("--apply-best", action="store_true")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(str(config_path))

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    missing_files = list_missing_cache_files(args.cache_dir, symbols, timeframes)
    if missing_files:
        print(
            json.dumps(
                {
                    "type": "OPTIMIZATION_SKIPPED",
                    "reason": "MISSING_CACHE_FILES",
                    "cache_dir": str(Path(args.cache_dir).resolve()),
                    "missing_count": len(missing_files),
                    "missing_files": missing_files[:12],
                }
            )
        )
        raise SystemExit(3)

    datasets = load_market_datasets_from_cache(args.cache_dir, symbols, timeframes)
    if args.max_candles > 0:
        for dataset in datasets:
            if len(dataset.candles) > args.max_candles:
                dataset.candles = dataset.candles[-args.max_candles :]

    acct = config["account"]
    risk_usd = float(acct["starting_balance_usd"]) * float(acct["risk_per_trade_pct"])
    execution_cfg = config.get("execution", {})
    fee_bps = (
        float(args.fee_bps_per_side)
        if args.fee_bps_per_side >= 0
        else float(execution_cfg.get("fee_bps_per_side", 0.0))
    )
    slippage_bps = (
        float(args.slippage_bps_per_side)
        if args.slippage_bps_per_side >= 0
        else float(execution_cfg.get("slippage_bps_per_side", 0.0))
    )

    optimizer = MLWalkForwardOptimizer(
        risk_usd=risk_usd,
        fee_bps_per_side=fee_bps,
        slippage_bps_per_side=slippage_bps,
    )
    baseline_samples = optimizer.generate_samples(datasets=datasets, strategy_payload=config["strategy"])
    baseline_result = optimizer.walk_forward(
        samples=baseline_samples,
        strategy_payload=config["strategy"],
        target_trades=args.target_trades,
        initial_train_frac=args.initial_train_frac,
    )
    if args.single_strategy:
        result = baseline_result
        tested = 1
        applied_strategy_source = "baseline"
        selection_reason = "single_strategy_requested"
    else:
        candidate_result, tested = optimizer.optimize(
            datasets=datasets,
            base_strategy=config["strategy"],
            target_trades=args.target_trades,
            target_wins=args.target_wins,
            max_candidates=args.max_candidates,
        )
        if should_apply_candidate_over_baseline(candidate_result, baseline_result):
            result = candidate_result
            applied_strategy_source = "optimized_candidate"
            selection_reason = "candidate_outperformed_baseline"
        else:
            result = baseline_result
            applied_strategy_source = "baseline"
            selection_reason = "baseline_retained"

    report = {
        "tested_candidates": tested,
        "target_trades": args.target_trades,
        "target_wins": args.target_wins,
        "fee_bps_per_side": fee_bps,
        "slippage_bps_per_side": slippage_bps,
        "selected_trades": result.total_selected_trades,
        "wins": result.wins,
        "losses": result.losses,
        "win_rate": round(result.win_rate, 4),
        "expectancy_r": round(result.expectancy_r, 4),
        "expectancy_usd_per_trade": round(result.expectancy_r * risk_usd, 6),
        "target_reached": result.total_selected_trades >= args.target_trades and result.wins >= args.target_wins,
        "tested_signals": result.tested_signals,
        "best_strategy": result.strategy,
        "applied_strategy_source": applied_strategy_source,
        "selection_reason": selection_reason,
        "baseline": {
            "selected_trades": baseline_result.total_selected_trades,
            "wins": baseline_result.wins,
            "losses": baseline_result.losses,
            "win_rate": round(baseline_result.win_rate, 4),
            "expectancy_r": round(baseline_result.expectancy_r, 4),
        },
        "folds": [
            {
                "fold": f.fold_index,
                "threshold": f.threshold,
                "trades": f.trades,
                "wins": f.wins,
                "losses": f.losses,
                "win_rate": round(f.win_rate, 4),
                "expectancy_r": round(f.expectancy_r, 4),
            }
            for f in result.folds
        ],
        "per_market": result.per_market,
    }

    print(json.dumps(report))

    if args.apply_best:
        latest = load_config(str(config_path))
        latest["strategy"] = result.strategy
        latest.setdefault("ml", {})
        latest["ml"]["last_walk_forward"] = {
            "selected_trades": result.total_selected_trades,
            "wins": result.wins,
            "losses": result.losses,
            "win_rate": round(result.win_rate, 4),
            "expectancy_r": round(result.expectancy_r, 4),
        }
        config_path.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
