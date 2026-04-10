"""Continuous monitor for Binance demo USDT balance and trade quality."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("CRYPTO_RUNTIME_DIR", "/tmp/crypto-runtime"))
EVENTS_FILE = RUNTIME_DIR / "live_events.jsonl"
REPORTS_FILE = RUNTIME_DIR / "monitor_reports.jsonl"
DEFAULT_INTERVAL_SEC = 300


def load_env_file() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
        return
    except Exception:
        pass

    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return

    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def trade_key(trade: Dict[str, Any]) -> str:
    return "|".join(
        [
            str(trade.get("symbol") or ""),
            str(trade.get("side") or ""),
            str(trade.get("opened_at_ms") or ""),
            str(trade.get("closed_at_ms") or ""),
            str(trade.get("entry") or ""),
            str(trade.get("exit_price") or ""),
            str(trade.get("reason") or ""),
        ]
    )


def get_session_trades() -> List[Dict[str, Any]]:
    trades: List[Dict[str, Any]] = []
    seen: set[str] = set()
    if not EVENTS_FILE.exists():
        return trades

    with EVENTS_FILE.open(encoding="utf-8", errors="ignore") as fp:
        for raw in fp:
            line = raw.strip().lstrip("\x07")
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "TRADE_RESULT":
                continue
            trade = event.get("trade") or {}
            if not isinstance(trade, dict) or not trade:
                continue
            key = trade_key(trade)
            if key in seen:
                continue
            seen.add(key)
            row = dict(trade)
            row["binance_open"] = bool(event.get("binance_executed", False))
            row["binance_close"] = bool(event.get("binance_closed", False))
            row["quality_flags"] = assess_trade_quality(row)
            trades.append(row)
    return trades


def assess_trade_quality(trade: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    reason = str(trade.get("reason") or "")
    pnl_r = safe_float(trade.get("pnl_r"))
    opened = int(trade.get("opened_at_ms") or 0)
    closed = int(trade.get("closed_at_ms") or 0)
    hold_minutes = (closed - opened) / 60000.0 if opened and closed and closed >= opened else None

    if "ADVERSE_CUT" in reason:
        flags.append("adverse_cut")
    if "crossover" in reason.lower():
        flags.append("crossover_setup")
    if pnl_r <= -0.95:
        flags.append("full_r_loss")
    if hold_minutes is not None and hold_minutes <= 15 and pnl_r < 0:
        flags.append("fast_loss")
    if not trade.get("binance_open"):
        flags.append("paper_only")
    if trade.get("binance_open") and not trade.get("binance_close"):
        flags.append("binance_close_unconfirmed")
    return flags


def get_binance_account_snapshot() -> Dict[str, Any]:
    load_env_file()
    try:
        from src.binance_executor import BinanceExecutor

        config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        executor = BinanceExecutor.from_env(config)
        if not executor.enabled:
            return {"enabled": False, "error": "No Binance demo/live API keys loaded"}

        account = executor.get_account()
        assets = account.get("assets", [])
        usdt_asset = next((a for a in assets if str(a.get("asset")) == "USDT"), {})
        positions = [
            p
            for p in account.get("positions", [])
            if safe_float(p.get("positionAmt")) != 0.0
        ]
        return {
            "enabled": True,
            "demo": executor.demo,
            "wallet_balance": safe_float(account.get("totalWalletBalance")),
            "available_balance": safe_float(account.get("availableBalance")),
            "unrealized_pnl": safe_float(account.get("totalUnrealizedProfit")),
            "usdt_wallet_balance": safe_float(usdt_asset.get("walletBalance")),
            "usdt_available_balance": safe_float(usdt_asset.get("availableBalance")),
            "open_positions": [
                {
                    "symbol": p.get("symbol"),
                    "side": "LONG" if safe_float(p.get("positionAmt")) > 0 else "SHORT",
                    "amount": safe_float(p.get("positionAmt")),
                    "entry_price": safe_float(p.get("entryPrice")),
                    "unrealized_pnl": safe_float(p.get("unrealizedProfit") or p.get("unRealizedProfit")),
                }
                for p in positions
            ],
        }
    except Exception as exc:
        return {"enabled": False, "error": str(exc)}


def summarize(trades: List[Dict[str, Any]], account: Dict[str, Any]) -> Dict[str, Any]:
    wins = [t for t in trades if t.get("result") == "WIN"]
    losses = [t for t in trades if t.get("result") == "LOSS"]
    suspicious = [t for t in trades if t.get("quality_flags")]
    flag_counts = Counter(flag for t in suspicious for flag in t.get("quality_flags", []))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "total_pnl_r": round(sum(safe_float(t.get("pnl_r")) for t in trades), 6),
        "total_pnl_usd": round(sum(safe_float(t.get("pnl_usd")) for t in trades), 6),
        "suspicious_trade_count": len(suspicious),
        "suspicious_flag_counts": dict(flag_counts),
        "account": account,
        "recent_trades": trades[-5:],
    }


def write_report(summary: Dict[str, Any]) -> None:
    REPORTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REPORTS_FILE.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(summary) + "\n")


def print_report(summary: Dict[str, Any], check_num: int) -> None:
    account = summary.get("account", {})
    print("\n" + "=" * 72)
    print(f"CHECK #{check_num}  {summary['generated_at']}")
    print("=" * 72)
    print(
        f"Trades {summary['trade_count']} | Wins {summary['wins']} | "
        f"Losses {summary['losses']} | Win rate {summary['win_rate'] * 100:.1f}%"
    )
    print(
        f"Closed PnL: {summary['total_pnl_r']:+.3f}R | "
        f"${summary['total_pnl_usd']:+.2f}"
    )
    if account.get("enabled"):
        print(
            f"USDT wallet: ${account.get('usdt_wallet_balance', 0.0):.2f} | "
            f"USDT available: ${account.get('usdt_available_balance', 0.0):.2f} | "
            f"Unrealized: ${account.get('unrealized_pnl', 0.0):+.2f}"
        )
        print(
            f"Open positions: {len(account.get('open_positions', []))} | "
            f"Total wallet: ${account.get('wallet_balance', 0.0):.2f}"
        )
    else:
        print(f"Binance account unavailable: {account.get('error')}")
    print(f"Suspicious trades: {summary['suspicious_trade_count']} {summary['suspicious_flag_counts']}")
    for trade in summary.get("recent_trades", []):
        print(
            f"{trade.get('symbol','?'):<16} {trade.get('side','?'):<5} "
            f"{trade.get('result','?'):<5} {safe_float(trade.get('pnl_r')):+.3f}R "
            f"flags={','.join(trade.get('quality_flags', [])) or '-'}"
        )
    print("=" * 72)


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor Binance demo USDT and trade quality")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SEC)
    args = parser.parse_args()

    check_num = 0
    while True:
        check_num += 1
        trades = get_session_trades()
        account = get_binance_account_snapshot()
        summary = summarize(trades, account)
        print_report(summary, check_num)
        write_report(summary)
        time.sleep(max(30, int(args.interval)))


if __name__ == "__main__":
    main()
