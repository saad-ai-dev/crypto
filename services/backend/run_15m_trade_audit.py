from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from pymongo import MongoClient

from src.binance_futures_rest import BinanceFuturesRestClient
from src.config import load_config
from src.indicators import atr, ema, rsi
from src.models import Candle, MarketContext
from src.strategy import MarketStructure, StrategyEngine


REASON_RE = re.compile(
    r"trend=(?P<trend>[A-Z]+)\s+\|\s+"
    r"EMA\((?P<ema_fast_period>\d+)/(?P<ema_slow_period>\d+)\)="
    r"(?P<ema_fast>[-0-9.]+)/(?P<ema_slow>[-0-9.]+),\s+"
    r"RSI=(?P<rsi>[-0-9.]+),\s+ATR%=(?P<atr_pct>[-0-9.]+),\s+ADX=(?P<adx>[-0-9.]+),\s+"
    r"SR=(?P<support>[^/]+)/(?P<resistance>[^,]+),\s+funding=(?P<funding>[-0-9.]+)"
)


def _timeframe_ms(timeframe: str) -> int:
    raw = str(timeframe or "").strip().lower()
    if raw.endswith("m"):
        return max(1, int(raw[:-1] or "1")) * 60_000
    if raw.endswith("h"):
        return max(1, int(raw[:-1] or "1")) * 3_600_000
    if raw.endswith("d"):
        return max(1, int(raw[:-1] or "1")) * 86_400_000
    raise ValueError(f"Unsupported timeframe: {timeframe!r}")


def _signal_type_from_reason(reason: str) -> str:
    upper = str(reason or "").upper()
    if "BB_REVERSION" in upper:
        return "BB_REVERSION"
    if "SUPERTREND" in upper:
        return "SUPERTREND"
    if "PULLBACK" in upper:
        return "PULLBACK"
    if "CROSSOVER" in upper:
        return "CROSSOVER"
    return "UNKNOWN"


def _parse_reason(reason: str) -> Dict[str, object]:
    parsed: Dict[str, object] = {
        "signal_type": _signal_type_from_reason(reason),
        "trend": None,
        "ema_fast": None,
        "ema_slow": None,
        "rsi": None,
        "atr_pct": None,
        "adx": None,
        "support": None,
        "resistance": None,
        "funding": None,
    }
    match = REASON_RE.search(str(reason or ""))
    if not match:
        return parsed

    parsed.update(
        {
            "trend": match.group("trend"),
            "ema_fast": float(match.group("ema_fast")),
            "ema_slow": float(match.group("ema_slow")),
            "rsi": float(match.group("rsi")),
            "atr_pct": float(match.group("atr_pct")),
            "adx": float(match.group("adx")),
            "support": None if match.group("support").lower() == "na" else float(match.group("support")),
            "resistance": None if match.group("resistance").lower() == "na" else float(match.group("resistance")),
            "funding": float(match.group("funding")),
        }
    )
    return parsed


def _fetch_historical_klines(
    client: BinanceFuturesRestClient,
    symbol: str,
    interval: str,
    end_ms: int,
    limit: int,
) -> List[Candle]:
    payload = client._get_json(  # noqa: SLF001 - public Binance endpoint via existing client
        "/fapi/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "endTime": str(end_ms),
            "limit": str(limit),
        },
    )
    candles: List[Candle] = []
    for item in payload:
        candles.append(
            Candle(
                open_time_ms=int(item[0]),
                open=float(item[1]),
                high=float(item[2]),
                low=float(item[3]),
                close=float(item[4]),
                volume=float(item[5]),
                close_time_ms=int(item[6]),
            )
        )
    return [c for c in candles if int(c.close_time_ms) <= int(end_ms)]


def _structure_to_dict(structure: MarketStructure) -> Dict[str, object]:
    return {
        "support": structure.support,
        "resistance": structure.resistance,
        "support_touches": structure.support_touches,
        "resistance_touches": structure.resistance_touches,
        "hvn_support": structure.hvn_support,
        "hvn_resistance": structure.hvn_resistance,
    }


def _round_or_none(value: Optional[float], places: int = 6) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), places)


def _candle_to_dict(candle: Candle) -> Dict[str, float]:
    return {
        "open_time_ms": int(candle.open_time_ms),
        "close_time_ms": int(candle.close_time_ms),
        "open": round(float(candle.open), 6),
        "high": round(float(candle.high), 6),
        "low": round(float(candle.low), 6),
        "close": round(float(candle.close), 6),
        "volume": round(float(candle.volume), 6),
    }


def _close_position(candle: Candle) -> Optional[float]:
    candle_range = float(candle.high) - float(candle.low)
    if candle_range <= 0:
        return None
    return round((float(candle.close) - float(candle.low)) / candle_range, 4)


def _body_pct(candle: Candle) -> Optional[float]:
    if float(candle.open) == 0:
        return None
    return round((float(candle.close) - float(candle.open)) / float(candle.open), 4)


def _build_pullback_checks(
    engine: StrategyEngine,
    side: str,
    candles: List[Candle],
    entry: float,
    ema_fast_v: float,
    rsi_v: float,
    atr_v: float,
) -> Dict[str, object]:
    if len(candles) < 3:
        return {}
    prev_close = candles[-2].close
    prev2_close = candles[-3].close
    checks: Dict[str, object] = {
        "pullback_touch_dist_atr": None,
        "pullback_bounce_or_reject": None,
        "pullback_confirmed": None,
        "pullback_rsi_in_range": None,
    }
    if atr_v <= 0:
        return checks

    if side == "LONG":
        checks["pullback_touch_dist_atr"] = abs(candles[-1].low - ema_fast_v) / atr_v
        checks["pullback_bounce_or_reject"] = entry > ema_fast_v and prev_close <= ema_fast_v * 1.002
        checks["pullback_confirmed"] = (
            prev_close >= prev2_close * (1 - engine.params.pullback_confirmation_slack_pct)
            and entry >= prev_close * (1 - engine.params.pullback_confirmation_slack_pct)
        )
        checks["pullback_rsi_in_range"] = engine.params.long_rsi_min <= rsi_v <= engine.params.long_rsi_max
    else:
        checks["pullback_touch_dist_atr"] = abs(candles[-1].high - ema_fast_v) / atr_v
        checks["pullback_bounce_or_reject"] = entry < ema_fast_v and prev_close >= ema_fast_v * 0.998
        checks["pullback_confirmed"] = (
            prev_close <= prev2_close * (1 + engine.params.pullback_confirmation_slack_pct)
            and entry <= prev_close * (1 + engine.params.pullback_confirmation_slack_pct)
        )
        checks["pullback_rsi_in_range"] = engine.params.short_rsi_min <= rsi_v <= engine.params.short_rsi_max
    return checks


def _evaluate_basis(
    engine: StrategyEngine,
    symbol: str,
    timeframe: str,
    side: str,
    expected_signal_type: str,
    candles: List[Candle],
    market: MarketContext,
    trade: Dict[str, object],
) -> Dict[str, object]:
    close_prices = [c.close for c in candles]
    last = candles[-1]
    entry = float(last.close)
    ema_fast_v = ema(close_prices, engine.params.ema_fast)
    ema_slow_v = ema(close_prices, engine.params.ema_slow)
    rsi_v = rsi(close_prices, engine.params.rsi_period)
    atr_v = atr(candles, engine.params.atr_period)
    atr_pct = atr_v / entry if entry else 0.0
    regime = engine.regime_detector.detect(candles, close_prices, ema_fast_v, ema_slow_v)
    structure = engine._build_market_structure(candles, entry)
    trend_bias = engine._macro_trend_bias(close_prices, entry, ema_fast_v, ema_slow_v)

    diagnostics: Dict[str, int] = {}
    replay_signal = engine.evaluate(symbol, timeframe, candles, market, diagnostics=diagnostics)

    basis: Dict[str, object] = {
        "entry": round(entry, 6),
        "ema_fast": round(ema_fast_v, 6),
        "ema_slow": round(ema_slow_v, 6),
        "rsi": round(rsi_v, 4),
        "atr": round(atr_v, 6),
        "atr_pct": round(atr_pct, 6),
        "regime": regime.regime,
        "adx": round(regime.adx, 4),
        "trend_bias": trend_bias,
        "structure": _structure_to_dict(structure),
        "near_structure": engine._is_near_structure(side, entry, atr_v, structure),
        "reward_room": engine._has_reward_room(side, entry, atr_v, structure),
        "parsed_reason": _parse_reason(str(trade.get("reason") or "")),
        "entry_candle": _candle_to_dict(last),
        "entry_candle_close_position": _close_position(last),
        "entry_candle_body_pct": _body_pct(last),
        "replay_signal": None,
        "diagnostics": diagnostics,
        "flags": [],
        "verdict": "UNKNOWN",
    }
    if len(candles) >= 2:
        basis["prev_candle"] = _candle_to_dict(candles[-2])
        basis["prev_candle_close_position"] = _close_position(candles[-2])
        basis["prev_candle_body_pct"] = _body_pct(candles[-2])
    basis.update(_build_pullback_checks(engine, side, candles, entry, ema_fast_v, rsi_v, atr_v))

    if replay_signal is not None:
        basis["replay_signal"] = {
            "side": replay_signal.side,
            "timeframe": replay_signal.timeframe,
            "entry": replay_signal.entry,
            "stop_loss": replay_signal.stop_loss,
            "take_profit": replay_signal.take_profit,
            "confidence": replay_signal.confidence,
            "reason": replay_signal.reason,
            "signal_type": _signal_type_from_reason(replay_signal.reason),
        }

    if replay_signal is None:
        basis["verdict"] = "RULES_MISMATCH"
        basis["flags"].append("replay_did_not_generate_signal")
    elif replay_signal.side != side:
        basis["verdict"] = "SIDE_MISMATCH"
        basis["flags"].append("replay_generated_opposite_side")
    elif _signal_type_from_reason(replay_signal.reason) != expected_signal_type:
        basis["verdict"] = "TYPE_MISMATCH"
        basis["flags"].append("replay_generated_different_signal_type")
    else:
        basis["verdict"] = "RULES_MATCHED"

    if side == "SHORT" and trend_bias == "NEUTRAL":
        basis["flags"].append("short_taken_with_neutral_macro_trend")
    if side == "LONG" and trend_bias == "NEUTRAL":
        basis["flags"].append("long_taken_with_neutral_macro_trend")

    exit_type = str((trade.get("trade_meta") or {}).get("exit_type") or "")
    hold_minutes = (trade.get("trade_meta") or {}).get("hold_minutes")
    if exit_type == "ADVERSE_CUT":
        basis["flags"].append("fast_adverse_move")
    if isinstance(hold_minutes, (int, float)) and float(hold_minutes) <= 15.0 and str(trade.get("result")) == "LOSS":
        basis["flags"].append("failed_within_one_bar")
    if (
        isinstance(basis.get("pullback_touch_dist_atr"), (int, float))
        and float(basis["pullback_touch_dist_atr"]) > 0.9
    ):
        basis["flags"].append("pullback_touch_was_marginal")

    reason_parsed = basis["parsed_reason"]
    deltas = {
        "ema_fast_delta": None,
        "ema_slow_delta": None,
        "rsi_delta": None,
        "atr_pct_delta": None,
        "adx_delta": None,
    }
    for key, observed in (
        ("ema_fast_delta", reason_parsed.get("ema_fast")),
        ("ema_slow_delta", reason_parsed.get("ema_slow")),
        ("rsi_delta", reason_parsed.get("rsi")),
        ("atr_pct_delta", reason_parsed.get("atr_pct")),
        ("adx_delta", reason_parsed.get("adx")),
    ):
        if observed is None:
            continue
        actual = {
            "ema_fast_delta": ema_fast_v,
            "ema_slow_delta": ema_slow_v,
            "rsi_delta": rsi_v,
            "atr_pct_delta": atr_pct,
            "adx_delta": regime.adx,
        }[key]
        deltas[key] = round(float(actual) - float(observed), 6)
    basis["market_deltas"] = deltas

    return basis


def _fetch_trade_docs(
    mongo_uri: str,
    mongo_db: str,
    timeframe: str,
    side: Optional[str] = None,
    opened_after_ms: Optional[int] = None,
    opened_before_ms: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, object]]:
    client = MongoClient(mongo_uri)
    db = client[mongo_db]
    query: Dict[str, object] = {"synthetic": {"$ne": True}, "timeframe": timeframe}
    if side:
        query["side"] = str(side).upper()
    if opened_after_ms is not None or opened_before_ms is not None:
        opened_query: Dict[str, int] = {}
        if opened_after_ms is not None:
            opened_query["$gte"] = int(opened_after_ms)
        if opened_before_ms is not None:
            opened_query["$lte"] = int(opened_before_ms)
        query["opened_at_ms"] = opened_query
    cursor = db.trade_history.find(
        query,
        {"_id": 0},
    ).sort("opened_at_ms", 1)
    docs = list(cursor)
    if limit is not None and limit > 0:
        docs = docs[-limit:]
    return docs


def _trade_summary_rows(audits: Iterable[Dict[str, object]]) -> Dict[str, object]:
    rows = list(audits)
    verdicts = Counter(str(row["basis"]["verdict"]) for row in rows)
    flags = Counter(flag for row in rows for flag in row["basis"]["flags"])
    by_result = Counter(str(row.get("result")) for row in rows)
    by_side = Counter(str(row.get("side")) for row in rows)
    return {
        "count": len(rows),
        "by_result": dict(by_result),
        "by_side": dict(by_side),
        "verdicts": dict(verdicts),
        "top_flags": [{"flag": key, "count": value} for key, value in flags.most_common(15)],
    }


def _render_markdown(report: Dict[str, object]) -> str:
    lines: List[str] = []
    lines.append("# 15m Trade Audit")
    lines.append("")
    lines.append(f"- Generated UTC: `{report['generated_at_utc']}`")
    lines.append(f"- Trades audited: `{report['summary']['count']}`")
    lines.append(f"- Results: `{report['summary']['by_result']}`")
    lines.append(f"- Verdicts: `{report['summary']['verdicts']}`")
    lines.append("")
    lines.append("## Top Flags")
    lines.append("")
    for item in report["summary"]["top_flags"]:
        lines.append(f"- `{item['flag']}`: `{item['count']}`")
    lines.append("")
    lines.append("## Trades")
    lines.append("")
    for trade in report["trades"]:
        basis = trade["basis"]
        lines.append(
            f"### {trade['symbol']} {trade['side']} {trade['timeframe']} {trade['result']} "
            f"({trade['event_time']})"
        )
        lines.append("")
        lines.append(f"- Verdict: `{basis['verdict']}`")
        lines.append(f"- Flags: `{basis['flags']}`")
        lines.append(
            f"- Replay: `{basis['replay_signal']['side'] if basis['replay_signal'] else None}` "
            f"`{basis['replay_signal']['signal_type'] if basis['replay_signal'] else None}`"
        )
        lines.append(
            f"- Basis: regime=`{basis['regime']}` trend=`{basis['trend_bias']}` "
            f"EMA=`{basis['ema_fast']:.4f}/{basis['ema_slow']:.4f}` "
            f"RSI=`{basis['rsi']}` ATR%=`{basis['atr_pct']}` ADX=`{basis['adx']}`"
        )
        lines.append(
            f"- Structure: near=`{basis['near_structure']}` room=`{basis['reward_room']}` "
            f"support=`{basis['structure']['support']}` resistance=`{basis['structure']['resistance']}`"
        )
        if basis.get("pullback_touch_dist_atr") is not None:
            lines.append(
                f"- Pullback checks: touch_atr=`{_round_or_none(basis.get('pullback_touch_dist_atr'), 4)}` "
                f"bounce_or_reject=`{basis.get('pullback_bounce_or_reject')}` "
                f"confirmed=`{basis.get('pullback_confirmed')}` "
                f"rsi_ok=`{basis.get('pullback_rsi_in_range')}`"
            )
        lines.append("")
    return "\n".join(lines)


def run_audit(
    config_path: str,
    mongo_uri: str,
    mongo_db: str,
    timeframe: str,
    side: Optional[str] = None,
    opened_after_ms: Optional[int] = None,
    opened_before_ms: Optional[int] = None,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    config = load_config(config_path)
    engine = StrategyEngine.from_dict(config["strategy"])
    client = BinanceFuturesRestClient(allow_mock_fallback=False, force_mock=False)

    lookback = max(
        260,
        int(config.get("live_loop", {}).get("lookback_candles", 260)),
        engine.params.ema_slow + 60,
        engine.params.sr_zone_lookback + 10,
    )
    trades = _fetch_trade_docs(
        mongo_uri,
        mongo_db,
        timeframe,
        side=side,
        opened_after_ms=opened_after_ms,
        opened_before_ms=opened_before_ms,
        limit=limit,
    )
    audits: List[Dict[str, object]] = []

    for trade in trades:
        symbol = str(trade["symbol"])
        opened_at_ms = int(trade["opened_at_ms"])
        parsed_reason = _parse_reason(str(trade.get("reason") or ""))
        funding = float(parsed_reason.get("funding") or 0.0)
        candles = _fetch_historical_klines(client, symbol, timeframe, opened_at_ms, lookback)
        if not candles:
            audits.append(
                {
                    **trade,
                    "basis": {
                        "verdict": "DATA_ERROR",
                        "flags": ["no_binance_candles_found"],
                        "replay_signal": None,
                        "diagnostics": {},
                    },
                }
            )
            continue

        market = MarketContext(
            mark_price=float(trade.get("entry") or candles[-1].close),
            funding_rate=funding,
            open_interest=0.0,
        )
        basis = _evaluate_basis(
            engine=engine,
            symbol=symbol,
            timeframe=timeframe,
            side=str(trade["side"]),
            expected_signal_type=str((trade.get("trade_meta") or {}).get("signal_type") or parsed_reason["signal_type"]),
            candles=candles,
            market=market,
            trade=trade,
        )
        audits.append({**trade, "basis": basis})
        time.sleep(0.05)

    summary = _trade_summary_rows(audits)
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "config_path": str(Path(config_path).resolve()),
        "mongo_uri": mongo_uri,
        "mongo_db": mongo_db,
        "timeframe": timeframe,
        "side": str(side).upper() if side else None,
        "opened_after_ms": opened_after_ms,
        "opened_before_ms": opened_before_ms,
        "summary": summary,
        "trades": audits,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit 15m trades against Binance historical candles and strategy replay.")
    parser.add_argument("--config", default="config.json", help="Path to backend config JSON")
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27017", help="MongoDB URI")
    parser.add_argument("--mongo-db", default="crypto_trading_live", help="MongoDB database name")
    parser.add_argument("--timeframe", default="15m", help="Timeframe to audit")
    parser.add_argument("--side", default=None, help="Optional trade side filter: LONG or SHORT")
    parser.add_argument("--opened-after-ms", type=int, default=None, help="Only audit trades opened at/after this ms")
    parser.add_argument("--opened-before-ms", type=int, default=None, help="Only audit trades opened at/before this ms")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit to only audit the most recent N trades")
    parser.add_argument(
        "--output-json",
        default="/tmp/crypto-runtime/15m_trade_audit.json",
        help="Where to write the JSON report",
    )
    parser.add_argument(
        "--output-md",
        default="/tmp/crypto-runtime/15m_trade_audit.md",
        help="Where to write the markdown report",
    )
    args = parser.parse_args()

    report = run_audit(
        config_path=str(Path(args.config).resolve()),
        mongo_uri=args.mongo_uri,
        mongo_db=args.mongo_db,
        timeframe=args.timeframe,
        side=args.side,
        opened_after_ms=args.opened_after_ms,
        opened_before_ms=args.opened_before_ms,
        limit=args.limit,
    )

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    output_md.write_text(_render_markdown(report), encoding="utf-8")
    print(json.dumps({"output_json": str(output_json), "output_md": str(output_md), "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
