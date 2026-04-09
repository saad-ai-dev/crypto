#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha1
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

try:
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import PyMongoError
except Exception:  # pragma: no cover - optional import guard for startup messaging
    ASCENDING = 1
    DESCENDING = -1
    MongoClient = None
    PyMongoError = Exception


class MongoStore:
    def __init__(self, uri: str, database: str, required: bool = True):
        self.uri = str(uri or "").strip()
        self.database = str(database or "").strip()
        self.required = bool(required)
        self.available = False
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self.client = None
        self.db = None
        self.events = None
        self.trades = None
        self.runtime_control = None
        self.config_snapshots = None

        self._connect()

    def _connect(self) -> None:
        if not self.uri:
            self.last_error = "MongoDB URI is empty"
            if self.required:
                raise RuntimeError(self.last_error)
            return
        if not self.database:
            self.last_error = "MongoDB database name is empty"
            if self.required:
                raise RuntimeError(self.last_error)
            return
        if MongoClient is None:
            self.last_error = "pymongo is not installed"
            if self.required:
                raise RuntimeError(self.last_error)
            return

        try:
            client = MongoClient(self.uri, serverSelectionTimeoutMS=5000, appname="crypto-dashboard")
            client.admin.command("ping")
            db = client[self.database]

            self.client = client
            self.db = db
            self.events = db["runtime_events"]
            self.trades = db["trade_history"]
            self.runtime_control = db["runtime_control"]
            self.config_snapshots = db["config_snapshots"]

            self._ensure_indexes()
            self._upsert_metadata()
            self.available = True
            self.last_error = None
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            self.last_error = str(exc)
            if self.required:
                raise RuntimeError(f"MongoDB connection failed: {exc}") from exc

    def _ensure_indexes(self) -> None:
        if not self.available or self.events is None:
            return
        self.events.create_index([("event_hash", ASCENDING)], unique=True)
        self.events.create_index([("time", DESCENDING)])
        self.events.create_index([("type", ASCENDING), ("time", DESCENDING)])

        self.trades.create_index([("trade_key", ASCENDING)], unique=True)
        self.trades.create_index([("closed_at_ms", DESCENDING)])
        self.trades.create_index([("symbol", ASCENDING), ("closed_at_ms", DESCENDING)])

        self.runtime_control.create_index([("updated_at", DESCENDING)])
        self.config_snapshots.create_index([("saved_at", DESCENDING)])

    def _upsert_metadata(self) -> None:
        if self.db is None:
            return
        meta = self.db["metadata"]
        meta.update_one(
            {"_id": "app"},
            {
                "$set": {
                    "name": "crypto-dashboard",
                    "database": self.database,
                    "uri": self.uri,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            },
            upsert=True,
        )

    @staticmethod
    def _event_hash(event: Dict[str, Any]) -> str:
        raw = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
        return sha1(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _trade_key(event: Dict[str, Any], trade: Dict[str, Any]) -> str:
        symbol = str(trade.get("symbol") or "").upper()
        timeframe = str(trade.get("timeframe") or "")
        side = str(trade.get("side") or "")
        result = str(trade.get("result") or "")
        opened = trade.get("opened_at_ms")
        closed = trade.get("closed_at_ms")
        event_time = str(event.get("time") or "")
        return f"{symbol}|{timeframe}|{side}|{result}|{opened}|{closed}|{event_time}"

    @staticmethod
    def _clean_output(doc: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(doc)
        out.pop("_id", None)
        return out

    def persist_event(self, event: Dict[str, Any], source: str) -> None:
        if not self.available or not isinstance(event, dict) or not event:
            return
        event_hash = self._event_hash(event)
        doc = copy.deepcopy(event)
        doc["event_hash"] = event_hash
        doc["source"] = source
        doc["ingested_at"] = datetime.now(timezone.utc).isoformat()

        try:
            with self._lock:
                self.events.update_one({"event_hash": event_hash}, {"$setOnInsert": doc}, upsert=True)
            if event.get("type") == "TRADE_RESULT":
                self.persist_trade_result(event, source)
        except PyMongoError as exc:
            self.last_error = str(exc)

    def persist_trade_result(self, event: Dict[str, Any], source: str) -> None:
        if not self.available:
            return
        trade = event.get("trade") or {}
        if not isinstance(trade, dict) or not trade:
            return

        trade_key = self._trade_key(event, trade)
        doc = {
            "trade_key": trade_key,
            "event_hash": self._event_hash(event),
            "source": source,
            "event_time": event.get("time"),
            "cycle": event.get("cycle"),
            "symbol": trade.get("symbol"),
            "timeframe": trade.get("timeframe"),
            "side": trade.get("side"),
            "entry": trade.get("entry"),
            "take_profit": trade.get("take_profit"),
            "stop_loss": trade.get("stop_loss"),
            "exit_price": trade.get("exit_price"),
            "result": trade.get("result"),
            "opened_at_ms": trade.get("opened_at_ms"),
            "closed_at_ms": trade.get("closed_at_ms"),
            "pnl_r": trade.get("pnl_r"),
            "pnl_usd": trade.get("pnl_usd"),
            "reason": trade.get("reason"),
            "summary": event.get("summary"),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self._lock:
                self.trades.update_one({"trade_key": trade_key}, {"$setOnInsert": doc}, upsert=True)
        except PyMongoError as exc:
            self.last_error = str(exc)

    def persist_trade_record(self, record: Dict[str, Any], source: str) -> None:
        if not self.available or not isinstance(record, dict) or not record:
            return
        trade_key = self._trade_key({"time": record.get("event_time")}, record)
        doc = {
            "trade_key": trade_key,
            "event_hash": None,
            "source": source,
            "event_time": record.get("event_time"),
            "cycle": record.get("cycle"),
            "symbol": record.get("symbol"),
            "timeframe": record.get("timeframe"),
            "side": record.get("side"),
            "entry": record.get("entry"),
            "take_profit": record.get("take_profit"),
            "stop_loss": record.get("stop_loss"),
            "exit_price": record.get("exit_price"),
            "result": record.get("result"),
            "opened_at_ms": record.get("opened_at_ms"),
            "closed_at_ms": record.get("closed_at_ms"),
            "pnl_r": record.get("pnl_r"),
            "pnl_usd": record.get("pnl_usd"),
            "reason": record.get("reason"),
            "est_cost_usd": record.get("est_cost_usd"),
            "net_pnl_usdt": record.get("net_pnl_usdt"),
            "binance_executed": record.get("binance_executed"),
            "synthetic": record.get("synthetic", False),
            "history_source": record.get("history_source"),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self._lock:
                self.trades.update_one({"trade_key": trade_key}, {"$setOnInsert": doc}, upsert=True)
        except PyMongoError as exc:
            self.last_error = str(exc)

    def persist_runtime_control(self, payload: Dict[str, Any]) -> None:
        if not self.available:
            return
        doc = {
            "symbols": payload.get("symbols", []),
            "updated_at": payload.get("updated_at"),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self._lock:
                self.runtime_control.insert_one(doc)
        except PyMongoError as exc:
            self.last_error = str(exc)

    def persist_config_snapshot(self, config: Dict[str, Any]) -> None:
        if not self.available:
            return
        doc = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "config": copy.deepcopy(config),
        }
        try:
            with self._lock:
                self.config_snapshots.insert_one(doc)
        except PyMongoError as exc:
            self.last_error = str(exc)

    def fetch_trade_history(self, limit: int, source_file: str) -> Dict[str, Any]:
        size = max(1, min(int(limit), 5000))
        if not self.available:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": 0,
                "items": [],
                "source_file": source_file,
                "storage": "mongodb",
                "storage_error": self.last_error,
            }
        try:
            items = [
                self._clean_output(row)
                for row in self.trades.find({}, {"_id": 0})
                .sort([("closed_at_ms", DESCENDING), ("event_time", DESCENDING)])
                .limit(size)
            ]
            total = self.trades.count_documents({})
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": int(total),
                "items": items,
                "source_file": source_file,
                "storage": "mongodb",
            }
        except PyMongoError as exc:
            self.last_error = str(exc)
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": 0,
                "items": [],
                "source_file": source_file,
                "storage": "mongodb",
                "storage_error": self.last_error,
            }

    def status(self) -> Dict[str, Any]:
        return {
            "enabled": bool(self.uri),
            "available": self.available,
            "database": self.database,
            "uri": self.uri,
            "error": self.last_error,
        }


class EventStateCache:
    def __init__(self, events_file: Path, mongo_store: Optional[MongoStore] = None):
        self.events_file = events_file
        self.mongo_store = mongo_store
        self._lock = threading.Lock()
        self._position = 0
        self._file_identity: Optional[tuple[int, int]] = None
        self._recent_possible_ttl_sec = 90
        self._recent_possible_limit = 300
        self._state = self._default_state()

    def _default_state(self) -> Dict[str, Any]:
        return {
            "status": "WAITING",
            "last_update": None,
            "stage": None,
            "stage_message": None,
            "open_trade": None,
            "possible_trades": [],
            "possible_trades_live": [],
            "possible_trades_recent": [],
            "possible_trades_meta": {},
            "possible_probability_categories": {},
            "guard_event": None,
            "last_trade": None,
            "summary": None,
            "market": [],
            "recent_results": [],
            "recent_events": [],
            "events_file": str(self.events_file),
        }

    def _reset_state(self) -> None:
        self._position = 0
        self._file_identity = None
        self._state = self._default_state()

    @staticmethod
    def _clean_line(raw: str) -> str:
        return raw.strip().lstrip("\x07")

    def _append_recent_result(self, trade: Dict[str, Any]) -> None:
        recent = self._state["recent_results"]
        recent.insert(0, trade)
        del recent[25:]

    @staticmethod
    def _extract_symbols(event: Dict[str, Any]) -> list[str]:
        symbols: list[str] = []

        def add_symbol(raw: Any) -> None:
            clean = str(raw or "").strip().upper()
            if not clean or clean in symbols:
                return
            symbols.append(clean)

        add_symbol(event.get("symbol"))

        trade = event.get("trade")
        if isinstance(trade, dict):
            add_symbol(trade.get("symbol"))

        trades = event.get("trades")
        if isinstance(trades, list):
            for row in trades[:8]:
                if isinstance(row, dict):
                    add_symbol(row.get("symbol"))

        snapshots = event.get("snapshots")
        if isinstance(snapshots, list):
            for row in snapshots[:10]:
                if isinstance(row, dict):
                    add_symbol(row.get("symbol"))

        return symbols

    @staticmethod
    def _event_message(event: Dict[str, Any]) -> str:
        event_type = str(event.get("type") or "")
        if event_type == "RUN_STAGE":
            stage = str(event.get("stage") or "").strip()
            message = str(event.get("message") or "").strip()
            return f"{stage}: {message}" if stage else message
        if event_type == "LIVE_MARKET":
            snapshots = event.get("snapshots") or []
            return f"Market heartbeat received ({len(snapshots)} symbols)"
        if event_type == "POSSIBLE_TRADES":
            total = event.get("total_possible_trades")
            seen = event.get("total_candidates_seen")
            return f"Possible trades={total} from candidates={seen}"
        if event_type == "NO_SIGNAL":
            reason = event.get("reason")
            candidates = event.get("candidate_count")
            return f"{reason} (candidates={candidates})"
        if event_type == "OPEN_TRADE":
            symbol = event.get("symbol")
            side = event.get("side")
            timeframe = event.get("timeframe")
            entry = event.get("entry")
            return f"OPEN {symbol} {side} {timeframe} @ {entry}"
        if event_type == "TRADE_RESULT":
            trade = event.get("trade") or {}
            symbol = trade.get("symbol")
            result = trade.get("result")
            pnl_r = trade.get("pnl_r")
            return f"{symbol} {result} pnl_r={pnl_r}"
        if event_type == "EXECUTION_FILTER_RELAX":
            after = event.get("after") or {}
            return (
                f"Execution filters relaxed to conf={after.get('execute_min_confidence')}, "
                f"exp={after.get('execute_min_expectancy_r')}, score={after.get('execute_min_score')}"
            )

        message = event.get("message")
        if message:
            return str(message)
        reason = event.get("reason")
        if reason:
            return str(reason)
        return "Event processed."

    @staticmethod
    def _event_severity(event: Dict[str, Any]) -> str:
        event_type = str(event.get("type") or "")
        if event_type == "TRADE_RESULT":
            trade = event.get("trade") or {}
            result = str(trade.get("result") or "").upper()
            return "SUCCESS" if result == "WIN" else "DANGER"
        if event_type in {"OPEN_TRADE"}:
            return "ACTION"
        if event_type in {"NO_SIGNAL", "EXECUTION_FILTER_RELAX"}:
            return "WARN"
        if "FAILED" in event_type or "ERROR" in event_type:
            return "DANGER"
        return "INFO"

    def _append_recent_event(self, event: Dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            return
        symbols = self._extract_symbols(event)
        entry = {
            "time": event.get("time"),
            "type": event_type,
            "cycle": event.get("cycle"),
            "symbols": symbols[:12],
            "primary_symbol": symbols[0] if symbols else None,
            "message": self._event_message(event),
            "severity": self._event_severity(event),
        }
        recent = self._state["recent_events"]
        recent.insert(0, entry)
        del recent[120:]

    @staticmethod
    def _event_epoch(event_time: Optional[str]) -> float:
        if not event_time:
            return time.time()
        try:
            clean = str(event_time).strip().replace("Z", "+00:00")
            return datetime.fromisoformat(clean).timestamp()
        except ValueError:
            return time.time()

    @staticmethod
    def _trade_key(trade: Dict[str, Any]) -> str:
        symbol = str(trade.get("symbol") or "").upper()
        timeframe = str(trade.get("timeframe") or "")
        side = str(trade.get("side") or "")
        entry = trade.get("entry")
        take_profit = trade.get("take_profit")
        stop_loss = trade.get("stop_loss")
        return f"{symbol}|{timeframe}|{side}|{entry}|{take_profit}|{stop_loss}"

    def _merge_possible_trades(self, trades: list[Dict[str, Any]], event_time: Optional[str]) -> None:
        now_epoch = self._event_epoch(event_time)
        current_keys = set()
        merged: Dict[str, Dict[str, Any]] = {}

        for raw in trades:
            if not isinstance(raw, dict):
                continue
            key = self._trade_key(raw)
            current_keys.add(key)
            item = copy.deepcopy(raw)
            item["signal_state"] = "LIVE"
            item["last_seen_time"] = event_time
            item["last_seen_epoch"] = now_epoch
            merged[key] = item

        for old in self._state.get("possible_trades_recent", []):
            if not isinstance(old, dict):
                continue
            key = self._trade_key(old)
            if key in merged:
                continue
            seen_epoch = old.get("last_seen_epoch")
            if not isinstance(seen_epoch, (int, float)):
                seen_epoch = now_epoch
            if (now_epoch - float(seen_epoch)) > self._recent_possible_ttl_sec:
                continue
            carry = copy.deepcopy(old)
            carry["signal_state"] = "RECENT"
            merged[key] = carry

        rows = list(merged.values())
        rows.sort(key=lambda row: float(row.get("score") or 0.0), reverse=True)
        rows = rows[: self._recent_possible_limit]

        live_rows = [row for row in rows if row.get("signal_state") == "LIVE"]
        recent_rows = [row for row in rows if row.get("signal_state") == "RECENT"]
        self._state["possible_trades_live"] = live_rows
        self._state["possible_trades_recent"] = rows
        self._state["possible_trades"] = rows

    def _process_event(self, event: Dict[str, Any]) -> None:
        event_type = event.get("type")
        event_time = event.get("time")
        if event_time:
            self._state["last_update"] = event_time
        self._append_recent_event(event)

        if event_type == "RUN_STAGE":
            stage = str(event.get("stage") or "").strip()
            message = event.get("message")
            self._state["stage"] = stage or None
            self._state["stage_message"] = message
            self._state["status"] = stage or "WAITING"
            return

        if event_type == "LIVE_MARKET":
            self._state["market"] = event.get("snapshots", [])
            return

        if event_type == "NO_SIGNAL":
            self._state["status"] = "NO_SIGNAL"
            return

        if event_type == "OPEN_TRADE":
            self._state["status"] = "OPEN_TRADE"
            self._state["open_trade"] = {
                "symbol": event.get("symbol"),
                "timeframe": event.get("timeframe"),
                "side": event.get("side"),
                "entry": event.get("entry"),
                "take_profit": event.get("take_profit"),
                "stop_loss": event.get("stop_loss"),
                "confidence": event.get("confidence"),
                "trend_strength": event.get("trend_strength"),
                "cost_r": event.get("cost_r"),
                "score": event.get("score"),
                "win_probability": event.get("win_probability"),
                "probability_bucket": event.get("probability_bucket"),
                "probability_bucket_label": event.get("probability_bucket_label"),
                "reason": event.get("reason"),
                "cycle": event.get("cycle"),
                "time": event.get("time"),
                "signal_state": "LIVE",
                "updated_at": event.get("time"),
            }
            return

        if event_type == "POSSIBLE_TRADES":
            trades = event.get("trades")
            clean_trades = trades if isinstance(trades, list) else []
            self._merge_possible_trades(clean_trades, event_time)
            self._state["possible_trades_meta"] = {
                "cycle": event.get("cycle"),
                "max_parallel_candidates": event.get("max_parallel_candidates"),
                "possible_trades_limit": event.get("possible_trades_limit"),
                "total_candidates_seen": event.get("total_candidates_seen"),
                "total_possible_trades": event.get("total_possible_trades"),
                "min_candidate_confidence": event.get("min_candidate_confidence"),
                "min_candidate_expectancy_r": event.get("min_candidate_expectancy_r"),
                "blocked_symbols": event.get("blocked_symbols", []),
                "display_live_count": len(self._state.get("possible_trades_live", [])),
                "display_recent_count": len(recent_rows) if (recent_rows := [row for row in self._state.get("possible_trades_recent", []) if row.get("signal_state") == "RECENT"]) else 0,
            }
            cats = event.get("probability_categories")
            self._state["possible_probability_categories"] = cats if isinstance(cats, dict) else {}
            return

        if event_type == "BINANCE_ORDER":
            action = event.get("action")
            result = event.get("result") or {}
            open_trade = self._state.get("open_trade") or {}
            if action == "OPEN" and open_trade:
                open_trade["binance_executed"] = result.get("executed", False)
                open_trade["binance_order_id"] = result.get("order_id")
                open_trade["binance_entry_price"] = result.get("entry_price")
                open_trade["binance_quantity"] = result.get("quantity")
                open_trade["binance_notional"] = result.get("notional")
                open_trade["binance_status"] = result.get("status")
                self._state["open_trade"] = open_trade
            elif action == "CLOSE":
                if open_trade:
                    open_trade["binance_close_status"] = result.get("status")
                    self._state["open_trade"] = open_trade
            return

        if event_type in {"SYMBOL_COOLDOWN_APPLIED", "SYMBOL_COOLDOWN_CLEARED", "GUARD_RETUNE"}:
            self._state["guard_event"] = event
            return

        if event_type == "TRADE_RESULT":
            trade = event.get("trade") or {}
            self._state["status"] = trade.get("result", "TRADE_RESULT")
            prior_open = self._state.get("open_trade") or {}
            sticky = {
                "symbol": trade.get("symbol") or prior_open.get("symbol"),
                "timeframe": trade.get("timeframe") or prior_open.get("timeframe"),
                "side": trade.get("side") or prior_open.get("side"),
                "entry": trade.get("entry", prior_open.get("entry")),
                "take_profit": trade.get("take_profit", prior_open.get("take_profit")),
                "stop_loss": trade.get("stop_loss", prior_open.get("stop_loss")),
                "confidence": trade.get("confidence", prior_open.get("confidence")),
                "score": trade.get("score", prior_open.get("score")),
                "win_probability": prior_open.get("win_probability"),
                "probability_bucket": prior_open.get("probability_bucket"),
                "probability_bucket_label": prior_open.get("probability_bucket_label"),
                "reason": prior_open.get("reason"),
                "cycle": prior_open.get("cycle"),
                "time": prior_open.get("time"),
                "signal_state": "CLOSED",
                "closed_result": trade.get("result"),
                "closed_exit_price": trade.get("exit_price"),
                "closed_pnl_r": trade.get("pnl_r"),
                "closed_pnl_usd": trade.get("pnl_usd"),
                "closed_at_ms": trade.get("closed_at_ms"),
                "updated_at": event_time,
            }
            self._state["open_trade"] = sticky
            self._state["last_trade"] = trade
            self._state["summary"] = event.get("summary")
            if trade:
                self._append_recent_result(trade)
            return

        if event_type == "FINAL":
            result = event.get("result") or {}
            self._state["status"] = result.get("status", "FINAL")
            if isinstance(result.get("summary"), dict):
                self._state["summary"] = result.get("summary")

    def refresh(self) -> Dict[str, Any]:
        with self._lock:
            if not self.events_file.exists():
                state = copy.deepcopy(self._state)
                if self.mongo_store:
                    state["storage"] = self.mongo_store.status()
                state["generated_at"] = datetime.now(timezone.utc).isoformat()
                return state

            stats = self.events_file.stat()
            file_identity = (stats.st_dev, stats.st_ino)
            if self._file_identity != file_identity or stats.st_size < self._position:
                self._reset_state()
                self._file_identity = file_identity

            with self.events_file.open("r", encoding="utf-8", errors="ignore") as fp:
                fp.seek(self._position)
                for raw_line in fp:
                    line = self._clean_line(raw_line)
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        self._process_event(event)
                        if self.mongo_store:
                            self.mongo_store.persist_event(event, source=str(self.events_file))
                self._position = fp.tell()

            state = copy.deepcopy(self._state)
            if self.mongo_store:
                state["storage"] = self.mongo_store.status()
            state["generated_at"] = datetime.now(timezone.utc).isoformat()
            return state


class TradeHistoryCache:
    def __init__(self, history_file: Path, max_items: int = 2000, mongo_store: Optional[MongoStore] = None):
        self.history_file = history_file
        self.max_items = max(100, int(max_items))
        self.mongo_store = mongo_store
        self._lock = threading.Lock()
        self._position = 0
        self._file_identity: Optional[tuple[int, int]] = None
        self._items: list[Dict[str, Any]] = []
        self._keys: set[str] = set()
        self._open_trade_contexts: Dict[str, Dict[str, Any]] = {}
        self._binance_open_contexts: Dict[str, Dict[str, Any]] = {}
        self._config_file = (self.history_file.parent.parent / "config.json").resolve()

    def _execution_cost_bps_total(self) -> float:
        try:
            payload = json.loads(self._config_file.read_text(encoding="utf-8"))
            execution = payload.get("execution", {})
            fee_bps = float(execution.get("fee_bps_per_side", 0.0))
            slip_bps = float(execution.get("slippage_bps_per_side", 0.0))
            return (fee_bps + slip_bps) * 2.0
        except Exception:
            return 0.0

    def _estimated_cost_usd(self, trade: Dict[str, Any]) -> float:
        entry = float(trade.get("entry") or 0.0)
        exit_price = float(trade.get("exit_price") or 0.0)
        stop_loss = float(trade.get("stop_loss") or 0.0)
        pnl_usd = float(trade.get("pnl_usd") or 0.0)
        pnl_r = float(trade.get("pnl_r") or 0.0)
        if entry <= 0 or stop_loss <= 0 or pnl_r == 0:
            return 0.0

        risk_per_unit = abs(entry - stop_loss)
        if risk_per_unit <= 0:
            return 0.0

        risk_usd = abs(pnl_usd / pnl_r)
        if risk_usd <= 0:
            return 0.0

        qty = risk_usd / risk_per_unit
        avg_notional = ((entry + exit_price) / 2.0) * qty if exit_price > 0 else entry * qty
        return avg_notional * (self._execution_cost_bps_total() / 10000.0)

    def _reset(self) -> None:
        self._position = 0
        self._file_identity = None
        self._items = []
        self._keys = set()
        self._open_trade_contexts = {}
        self._binance_open_contexts = {}

    @staticmethod
    def _line(raw: str) -> str:
        return raw.strip().lstrip("\x07")

    @staticmethod
    def _trade_key(event: Dict[str, Any], trade: Dict[str, Any]) -> str:
        symbol = str(trade.get("symbol") or "").upper()
        timeframe = str(trade.get("timeframe") or "")
        side = str(trade.get("side") or "")
        result = str(trade.get("result") or "")
        opened = trade.get("opened_at_ms")
        closed = trade.get("closed_at_ms")
        event_time = str(event.get("time") or "")
        return f"{symbol}|{timeframe}|{side}|{result}|{opened}|{closed}|{event_time}"

    @staticmethod
    def _symbol_key(symbol: Any, side: Any) -> str:
        return f"{str(symbol or '').strip().upper()}|{str(side or '').strip().upper()}"

    @staticmethod
    def _event_ms(event_time: Optional[str]) -> Optional[int]:
        if not event_time:
            return None
        try:
            clean = str(event_time).strip().replace("Z", "+00:00")
            return int(datetime.fromisoformat(clean).timestamp() * 1000)
        except ValueError:
            return None

    def _normalize_record(self, event: Dict[str, Any], trade: Dict[str, Any]) -> Dict[str, Any]:
        pnl_usd = float(trade.get("pnl_usd") or 0.0)
        est_cost_usd = self._estimated_cost_usd(trade)
        net_pnl_usdt = pnl_usd - est_cost_usd
        return {
            "event_time": event.get("time"),
            "cycle": event.get("cycle"),
            "symbol": trade.get("symbol"),
            "timeframe": trade.get("timeframe"),
            "side": trade.get("side"),
            "entry": trade.get("entry"),
            "take_profit": trade.get("take_profit"),
            "stop_loss": trade.get("stop_loss"),
            "exit_price": trade.get("exit_price"),
            "result": trade.get("result"),
            "opened_at_ms": trade.get("opened_at_ms"),
            "closed_at_ms": trade.get("closed_at_ms"),
            "pnl_r": trade.get("pnl_r"),
            "pnl_usd": pnl_usd,
            "est_cost_usd": round(est_cost_usd, 6),
            "net_pnl_usdt": round(net_pnl_usdt, 6),
            "reason": trade.get("reason"),
            "binance_executed": event.get("binance_executed", False),
        }

    def _append_record(self, key: str, record: Dict[str, Any]) -> None:
        if key in self._keys:
            return
        self._keys.add(key)
        self._items.append(record)
        if len(self._items) > self.max_items:
            self._items = self._items[-self.max_items :]
            self._keys = {self._trade_key({"time": row.get("event_time")}, row) for row in self._items}

    def _drop_matching_synthetic(self, record: Dict[str, Any]) -> None:
        target_symbol = str(record.get("symbol") or "").upper()
        target_side = str(record.get("side") or "").upper()
        target_opened = record.get("opened_at_ms")
        target_closed = record.get("closed_at_ms")

        match_index: Optional[int] = None
        for idx in range(len(self._items) - 1, -1, -1):
            row = self._items[idx]
            if not row.get("synthetic"):
                continue
            if str(row.get("symbol") or "").upper() != target_symbol:
                continue
            if str(row.get("side") or "").upper() != target_side:
                continue

            row_opened = row.get("opened_at_ms")
            row_closed = row.get("closed_at_ms")
            if (
                isinstance(target_opened, int)
                and isinstance(row_opened, int)
                and target_opened == row_opened
            ):
                match_index = idx
                break
            if (
                isinstance(target_closed, int)
                and isinstance(row_closed, int)
                and abs(target_closed - row_closed) <= 5 * 60 * 1000
            ):
                match_index = idx
                break

        if match_index is None:
            return

        self._items.pop(match_index)
        self._keys = {self._trade_key({"time": row.get("event_time")}, row) for row in self._items}

    def _append(self, event: Dict[str, Any], trade: Dict[str, Any]) -> None:
        record = self._normalize_record(event, trade)
        self._drop_matching_synthetic(record)
        key = self._trade_key(event, trade)
        self._append_record(key, record)

    def _register_open_trade(self, event: Dict[str, Any]) -> None:
        key = self._symbol_key(event.get("symbol"), event.get("side"))
        self._open_trade_contexts[key] = {
            "symbol": event.get("symbol"),
            "timeframe": event.get("timeframe"),
            "side": event.get("side"),
            "entry": event.get("entry"),
            "take_profit": event.get("take_profit"),
            "stop_loss": event.get("stop_loss"),
            "opened_at_ms": self._event_ms(event.get("time")),
            "event_time": event.get("time"),
        }

    def _register_binance_open(self, event: Dict[str, Any]) -> None:
        key = self._symbol_key(event.get("symbol"), event.get("side"))
        result = event.get("result") or {}
        self._binance_open_contexts[key] = {
            "event_time": event.get("time"),
            "opened_at_ms": self._event_ms(event.get("time")),
            "entry_price": result.get("entry_price"),
            "quantity": result.get("quantity"),
            "notional": result.get("notional"),
            "executed": result.get("executed", False),
            "status": result.get("status"),
        }

    def _append_synthetic_binance_close(self, event: Dict[str, Any]) -> None:
        action = str(event.get("action") or "").strip().upper()
        if action not in {"CLOSE", "RETRY_CLOSE", "ORPHAN_CLOSE"}:
            return

        key = self._symbol_key(event.get("symbol"), event.get("side"))
        open_trade = self._open_trade_contexts.get(key, {})
        binance_open = self._binance_open_contexts.get(key, {})
        result = event.get("result") or {}

        pnl_usd = event.get("pnl")
        if pnl_usd is None:
            pnl_usd = result.get("unrealized_pnl")
        try:
            pnl_usd = float(pnl_usd)
        except (TypeError, ValueError):
            return

        side = str(event.get("side") or open_trade.get("side") or "").upper()
        entry = (
            result.get("entry_price")
            or binance_open.get("entry_price")
            or open_trade.get("entry")
        )
        quantity = result.get("quantity") or binance_open.get("quantity")
        stop_loss = open_trade.get("stop_loss")
        take_profit = open_trade.get("take_profit")
        timeframe = open_trade.get("timeframe")
        opened_at_ms = open_trade.get("opened_at_ms") or binance_open.get("opened_at_ms")
        closed_at_ms = self._event_ms(event.get("time"))

        try:
            entry_value = float(entry)
        except (TypeError, ValueError):
            entry_value = 0.0
        try:
            qty_value = float(quantity)
        except (TypeError, ValueError):
            qty_value = 0.0
        try:
            stop_value = float(stop_loss)
        except (TypeError, ValueError):
            stop_value = 0.0

        exit_price: Optional[float] = None
        if entry_value > 0 and qty_value > 0:
            price_delta = pnl_usd / qty_value
            exit_price = entry_value + price_delta if side == "LONG" else entry_value - price_delta

        pnl_r: Optional[float] = None
        if entry_value > 0 and stop_value > 0 and qty_value > 0:
            risk_usd = abs(entry_value - stop_value) * qty_value
            if risk_usd > 0:
                pnl_r = pnl_usd / risk_usd

        if pnl_usd > 0:
            trade_result = "WIN"
        elif pnl_usd < 0:
            trade_result = "LOSS"
        else:
            trade_result = "BREAKEVEN"

        trade = {
            "symbol": event.get("symbol") or open_trade.get("symbol"),
            "timeframe": timeframe,
            "side": side,
            "entry": entry_value if entry_value > 0 else entry,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "exit_price": exit_price,
            "result": trade_result,
            "opened_at_ms": opened_at_ms,
            "closed_at_ms": closed_at_ms,
            "pnl_r": pnl_r,
            "pnl_usd": pnl_usd,
            "reason": f"BINANCE_{action}",
        }
        synthetic_event = {
            "time": event.get("time"),
            "cycle": event.get("cycle"),
            "binance_executed": True,
        }
        record = self._normalize_record(synthetic_event, trade)
        record["synthetic"] = True
        record["history_source"] = "BINANCE_RECONCILED"
        record_key = self._trade_key(synthetic_event, trade)
        self._append_record(record_key, record)
        if self.mongo_store:
            self.mongo_store.persist_trade_record(record, source=str(self.history_file))

    def refresh(self, limit: int = 200) -> Dict[str, Any]:
        with self._lock:
            if not self.history_file.exists():
                if self.mongo_store and self.mongo_store.available:
                    return self.mongo_store.fetch_trade_history(limit=limit, source_file=str(self.history_file))
                return {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "count": 0,
                    "items": [],
                    "source_file": str(self.history_file),
                    "storage": "jsonl",
                }

            stats = self.history_file.stat()
            file_identity = (stats.st_dev, stats.st_ino)
            if self._file_identity != file_identity or stats.st_size < self._position:
                self._reset()
                self._file_identity = file_identity

            with self.history_file.open("r", encoding="utf-8", errors="ignore") as fp:
                fp.seek(self._position)
                for raw_line in fp:
                    line = self._line(raw_line)
                    if not line or not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if self.mongo_store:
                        self.mongo_store.persist_event(event, source=str(self.history_file))
                    event_type = event.get("type")
                    if event_type == "OPEN_TRADE":
                        self._register_open_trade(event)
                        continue
                    if event_type == "BINANCE_ORDER":
                        action = str(event.get("action") or "").strip().upper()
                        if action == "OPEN":
                            self._register_binance_open(event)
                        else:
                            self._append_synthetic_binance_close(event)
                        continue
                    if event_type != "TRADE_RESULT":
                        continue
                    trade = event.get("trade") or {}
                    if not isinstance(trade, dict) or not trade:
                        continue
                    self._append(event, trade)
                    if self.mongo_store:
                        self.mongo_store.persist_trade_result(event, source=str(self.history_file))
                self._position = fp.tell()

            if self.mongo_store and self.mongo_store.available:
                return self.mongo_store.fetch_trade_history(limit=limit, source_file=str(self.history_file))

            size = max(1, min(int(limit), self.max_items))
            items = list(reversed(self._items[-size:]))
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "count": len(self._items),
                "items": items,
                "source_file": str(self.history_file),
                "storage": "jsonl",
            }


class NewsFetcher:
    DEFAULT_FEEDS = [
        {"source": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"source": "Cointelegraph", "url": "https://cointelegraph.com/rss"},
        {"source": "CNBC Markets", "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
        {"source": "MarketWatch", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
        {"source": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
        # X/Twitter via RSSHub mirrors; availability varies by region/network.
        {"source": "X: @WatcherGuru", "url": "https://rsshub.app/twitter/user/WatcherGuru"},
        {"source": "X: @cz_binance", "url": "https://rsshub.app/twitter/user/cz_binance"},
    ]

    def __init__(self, refresh_seconds: int = 10, max_items: int = 30):
        self.refresh_seconds = max(1, int(refresh_seconds))
        self.max_items = max(5, int(max_items))
        self._lock = threading.Lock()
        self._last_fetch_mono = 0.0
        self._cache: Dict[str, Any] = {
            "generated_at": None,
            "count": 0,
            "items": [],
            "errors": [],
            "sources": [f["source"] for f in self.DEFAULT_FEEDS],
        }

    @staticmethod
    def _to_iso_date(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            # Keep unknown but non-empty date values so UI can still display context.
            return raw

    @staticmethod
    def _read_url(url: str, timeout_sec: int = 6) -> str:
        req = Request(url, headers={"User-Agent": "crypto-dashboard/1.0"})
        with urlopen(req, timeout=timeout_sec) as resp:  # nosec B310
            data = resp.read()
        return data.decode("utf-8", errors="ignore")

    def _parse_items(self, xml_text: str, source: str) -> list[Dict[str, Any]]:
        items: list[Dict[str, Any]] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return items

        if root.tag.lower().endswith("rss"):
            for node in root.findall("./channel/item"):
                title = (node.findtext("title") or "").strip()
                link = (node.findtext("link") or "").strip()
                published = self._to_iso_date(node.findtext("pubDate"))
                if not title and not link:
                    continue
                items.append(
                    {
                        "source": source,
                        "title": title or "(untitled)",
                        "link": link,
                        "published_at": published,
                    }
                )
            return items

        atom_ns = {"atom": "http://www.w3.org/2005/Atom"}
        for node in root.findall("atom:entry", atom_ns):
            title = (node.findtext("atom:title", "", atom_ns) or "").strip()
            link = ""
            link_node = node.find("atom:link", atom_ns)
            if link_node is not None:
                link = (link_node.attrib.get("href") or "").strip()
            published = self._to_iso_date(node.findtext("atom:published", "", atom_ns))
            if not published:
                published = self._to_iso_date(node.findtext("atom:updated", "", atom_ns))
            if not title and not link:
                continue
            items.append(
                {
                    "source": source,
                    "title": title or "(untitled)",
                    "link": link,
                    "published_at": published,
                }
            )
        return items

    def refresh(self, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            now_mono = time.monotonic()
            if (
                not force
                and self._cache["generated_at"] is not None
                and (now_mono - self._last_fetch_mono) < self.refresh_seconds
            ):
                return copy.deepcopy(self._cache)

            all_items: list[Dict[str, Any]] = []
            errors: list[Dict[str, str]] = []

            for feed in self.DEFAULT_FEEDS:
                source = feed["source"]
                url = feed["url"]
                try:
                    xml_text = self._read_url(url)
                    feed_items = self._parse_items(xml_text, source)
                    all_items.extend(feed_items[:8])
                except URLError as exc:
                    errors.append({"source": source, "error": str(exc.reason)})
                except Exception as exc:
                    errors.append({"source": source, "error": str(exc)})

            seen = set()
            deduped: list[Dict[str, Any]] = []
            for item in all_items:
                key = (item.get("link") or "", item.get("title") or "")
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(item)

            deduped.sort(key=lambda it: str(it.get("published_at") or ""), reverse=True)
            generated_at = datetime.now(timezone.utc).isoformat()

            self._cache = {
                "generated_at": generated_at,
                "count": len(deduped[: self.max_items]),
                "items": deduped[: self.max_items],
                "errors": errors[:20],
                "sources": [f["source"] for f in self.DEFAULT_FEEDS],
            }
            self._last_fetch_mono = now_mono
            return copy.deepcopy(self._cache)


class SymbolCatalog:
    def __init__(self, refresh_seconds: int = 300):
        self.refresh_seconds = max(30, int(refresh_seconds))
        self._lock = threading.Lock()
        self._last_fetch_mono = 0.0
        self._cache: Dict[str, Any] = {
            "generated_at": None,
            "symbols": [],
            "errors": [],
        }

    @staticmethod
    def _read_json(url: str, timeout_sec: int = 8) -> Any:
        req = Request(url, headers={"User-Agent": "crypto-dashboard/1.0"})
        with urlopen(req, timeout=timeout_sec) as resp:  # nosec B310
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    @staticmethod
    def _normalize_symbol(value: Any) -> str:
        return str(value or "").strip().upper()

    def _refresh_internal(self) -> None:
        now_mono = time.monotonic()
        if (
            self._cache.get("generated_at")
            and (now_mono - self._last_fetch_mono) < self.refresh_seconds
            and self._cache.get("symbols")
        ):
            return

        errors: list[str] = []
        symbol_order: Dict[str, float] = {}

        try:
            tickers = self._read_json("https://fapi.binance.com/fapi/v1/ticker/24hr")
            if isinstance(tickers, list):
                for row in tickers:
                    symbol = self._normalize_symbol(row.get("symbol"))
                    if not symbol.endswith("USDT"):
                        continue
                    try:
                        qv = float(row.get("quoteVolume", 0.0))
                    except (TypeError, ValueError):
                        qv = 0.0
                    symbol_order[symbol] = max(symbol_order.get(symbol, 0.0), qv)
        except Exception as exc:
            errors.append(f"ticker_24hr: {exc}")

        symbols: list[str] = []
        try:
            exchange_info = self._read_json("https://fapi.binance.com/fapi/v1/exchangeInfo")
            for row in exchange_info.get("symbols", []):
                symbol = self._normalize_symbol(row.get("symbol"))
                if (
                    symbol.endswith("USDT")
                    and row.get("contractType") == "PERPETUAL"
                    and row.get("status") == "TRADING"
                ):
                    symbols.append(symbol)
        except Exception as exc:
            errors.append(f"exchange_info: {exc}")

        if symbols:
            unique = sorted(set(symbols), key=lambda s: (-symbol_order.get(s, 0.0), s))
        else:
            unique = sorted(symbol_order.keys(), key=lambda s: (-symbol_order.get(s, 0.0), s))

        self._cache = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbols": unique,
            "errors": errors[:10],
        }
        self._last_fetch_mono = now_mono

    def get_symbols(self, query: str = "", limit: int = 500) -> Dict[str, Any]:
        with self._lock:
            self._refresh_internal()
            symbols = self._cache.get("symbols", [])
            q = self._normalize_symbol(query)
            if q:
                symbols = [s for s in symbols if q in s]
            limit = max(1, min(int(limit), 2000))
            return {
                "generated_at": self._cache.get("generated_at"),
                "count": len(symbols),
                "symbols": symbols[:limit],
                "errors": self._cache.get("errors", []),
            }


class AnalyticsEngine:
    """Computes analytics from trade history for dashboard charts."""

    def __init__(self, history_cache: TradeHistoryCache):
        self.history_cache = history_cache

    def compute(self) -> Dict[str, Any]:
        raw = self.history_cache.refresh(limit=2000)
        items = list(reversed(raw.get("items", [])))  # oldest first
        if not items:
            return {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "total_trades": 0,
                "summary": {},
                "equity_curve": [],
                "symbol_breakdown": [],
                "streaks": {},
                "drawdown": {},
                "pnl_distribution": {},
                "rolling_win_rate": [],
                "duration_stats": {},
                "profit_factor": 0.0,
            }

        # Equity curve
        equity = 0.0
        net_equity_usdt = 0.0
        equity_curve = []
        pnl_values = []
        wins = 0
        losses = 0
        total_win_r = 0.0
        total_loss_r = 0.0
        win_streak = 0
        loss_streak = 0
        max_win_streak = 0
        max_loss_streak = 0
        current_streak_type = None
        current_streak_count = 0
        durations = []

        for item in items:
            pnl_r = float(item.get("pnl_r") or 0)
            pnl_usd = float(item.get("pnl_usd") or 0)
            net_pnl_usdt = float(item.get("net_pnl_usdt") or pnl_usd)
            est_cost_usd = float(item.get("est_cost_usd") or 0)
            equity += pnl_usd
            net_equity_usdt += net_pnl_usdt
            pnl_values.append(pnl_r)
            result = str(item.get("result") or "").upper()

            opened = item.get("opened_at_ms")
            closed = item.get("closed_at_ms")
            if opened and closed:
                try:
                    dur_min = (int(closed) - int(opened)) / 60000.0
                    if dur_min > 0:
                        durations.append(dur_min)
                except (ValueError, TypeError):
                    pass

            ts = item.get("closed_at_ms") or item.get("event_time") or ""
            equity_curve.append(
                {
                    "time": ts,
                    "equity": round(equity, 4),
                    "net_equity_usdt": round(net_equity_usdt, 4),
                    "pnl_usd": round(pnl_usd, 4),
                    "net_pnl_usdt": round(net_pnl_usdt, 4),
                    "est_cost_usd": round(est_cost_usd, 4),
                }
            )

            if result == "WIN":
                wins += 1
                total_win_r += pnl_r
                if current_streak_type == "WIN":
                    current_streak_count += 1
                else:
                    current_streak_type = "WIN"
                    current_streak_count = 1
                max_win_streak = max(max_win_streak, current_streak_count)
            elif result == "LOSS":
                losses += 1
                total_loss_r += abs(pnl_r)
                if current_streak_type == "LOSS":
                    current_streak_count += 1
                else:
                    current_streak_type = "LOSS"
                    current_streak_count = 1
                max_loss_streak = max(max_loss_streak, current_streak_count)

        total = wins + losses
        win_rate = (wins / total) if total else 0.0
        profit_factor = (total_win_r / total_loss_r) if total_loss_r > 0 else 0.0
        avg_win_r = (total_win_r / wins) if wins else 0.0
        avg_loss_r = (total_loss_r / losses) if losses else 0.0
        expectancy_r = (win_rate * avg_win_r) - ((1 - win_rate) * avg_loss_r)

        # Drawdown
        peak = 0.0
        max_dd = 0.0
        running = 0.0
        running_net = 0.0
        dd_curve = []
        for item in items:
            running += float(item.get("pnl_usd") or 0)
            running_net += float(item.get("net_pnl_usdt") or item.get("pnl_usd") or 0)
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)
            ts = item.get("closed_at_ms") or item.get("event_time") or ""
            dd_curve.append({"time": ts, "drawdown": round(dd, 4), "net_usdt": round(running_net, 4)})

        # Symbol breakdown
        sym_stats: Dict[str, Dict] = {}
        for item in items:
            sym = str(item.get("symbol") or "UNKNOWN")
            if sym not in sym_stats:
                sym_stats[sym] = {"wins": 0, "losses": 0, "pnl_usd": 0.0, "net_pnl_usdt": 0.0, "cost_usd": 0.0, "pnl_r": 0.0}
            result = str(item.get("result") or "").upper()
            if result == "WIN":
                sym_stats[sym]["wins"] += 1
            elif result == "LOSS":
                sym_stats[sym]["losses"] += 1
            sym_stats[sym]["pnl_usd"] += float(item.get("pnl_usd") or 0)
            sym_stats[sym]["net_pnl_usdt"] += float(item.get("net_pnl_usdt") or item.get("pnl_usd") or 0)
            sym_stats[sym]["cost_usd"] += float(item.get("est_cost_usd") or 0)
            sym_stats[sym]["pnl_r"] += float(item.get("pnl_r") or 0)

        symbol_breakdown = []
        for sym, s in sorted(sym_stats.items()):
            sym_total = s["wins"] + s["losses"]
            symbol_breakdown.append({
                "symbol": sym,
                "trades": sym_total,
                "wins": s["wins"],
                "losses": s["losses"],
                "win_rate": round(s["wins"] / sym_total, 4) if sym_total else 0,
                "pnl_usd": round(s["pnl_usd"], 4),
                "net_pnl_usdt": round(s["net_pnl_usdt"], 4),
                "cost_usd": round(s["cost_usd"], 4),
                "pnl_r": round(s["pnl_r"], 4),
            })

        # PnL distribution buckets
        dist_buckets = {"<-1R": 0, "-1R to -0.5R": 0, "-0.5R to 0": 0, "0 to 0.5R": 0, "0.5R to 1R": 0, ">1R": 0}
        for v in pnl_values:
            if v < -1:
                dist_buckets["<-1R"] += 1
            elif v < -0.5:
                dist_buckets["-1R to -0.5R"] += 1
            elif v < 0:
                dist_buckets["-0.5R to 0"] += 1
            elif v < 0.5:
                dist_buckets["0 to 0.5R"] += 1
            elif v < 1:
                dist_buckets["0.5R to 1R"] += 1
            else:
                dist_buckets[">1R"] += 1

        # Rolling win rate (window of 10 trades)
        rolling = []
        window = 10
        for i in range(len(items)):
            start = max(0, i - window + 1)
            chunk = items[start : i + 1]
            chunk_wins = sum(1 for t in chunk if str(t.get("result") or "").upper() == "WIN")
            wr = chunk_wins / len(chunk)
            ts = items[i].get("closed_at_ms") or items[i].get("event_time") or ""
            rolling.append({"time": ts, "win_rate": round(wr, 4), "trade_num": i + 1})

        # Duration stats
        dur_stats = {}
        if durations:
            dur_stats = {
                "avg_minutes": round(sum(durations) / len(durations), 1),
                "min_minutes": round(min(durations), 1),
                "max_minutes": round(max(durations), 1),
                "count": len(durations),
            }

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_trades": total,
            "summary": {
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 4),
                "avg_win_r": round(avg_win_r, 4),
                "avg_loss_r": round(avg_loss_r, 4),
                "expectancy_r": round(expectancy_r, 4),
                "total_pnl_usd": round(equity, 4),
                "total_net_pnl_usdt": round(net_equity_usdt, 4),
                "total_est_cost_usd": round(sum(float(item.get("est_cost_usd") or 0) for item in items), 4),
                "avg_net_pnl_usdt": round(net_equity_usdt / total, 4) if total else 0.0,
            },
            "equity_curve": equity_curve,
            "symbol_breakdown": symbol_breakdown,
            "streaks": {
                "max_win_streak": max_win_streak,
                "max_loss_streak": max_loss_streak,
                "current_type": current_streak_type,
                "current_count": current_streak_count,
            },
            "drawdown": {"max_drawdown_usd": round(max_dd, 4), "curve": dd_curve},
            "pnl_distribution": dist_buckets,
            "rolling_win_rate": rolling,
            "duration_stats": dur_stats,
            "profit_factor": round(profit_factor, 4),
        }


class ConfigStore:
    EDITABLE_FIELDS: Dict[str, tuple[str, ...]] = {
        "account": ("starting_balance_usd", "risk_per_trade_pct", "paper_risk_usd"),
        "execution": ("fee_bps_per_side", "slippage_bps_per_side"),
        "strategy": ("atr_multiplier", "risk_reward", "min_confidence"),
        "live_loop": (
            "max_open_trades",
            "min_candidate_confidence",
            "min_candidate_expectancy_r",
            "execute_min_confidence",
            "execute_min_expectancy_r",
            "execute_min_score",
            "execute_min_win_probability",
            "max_wait_candles",
            "trail_trigger_r",
            "break_even_trigger_r",
            "max_adverse_r_cut",
        ),
    }

    def __init__(
        self,
        config_file: Path,
        runtime_control_file: Path,
        mongo_store: Optional[MongoStore] = None,
    ):
        self.config_file = config_file
        self.runtime_control_file = runtime_control_file
        self.mongo_store = mongo_store
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, Any]:
        if not self.config_file.exists():
            return {}
        return json.loads(self.config_file.read_text(encoding="utf-8"))

    def _save(self, config: Dict[str, Any]) -> None:
        self.config_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        if self.mongo_store:
            self.mongo_store.persist_config_snapshot(config)

    def _save_runtime_symbols(self, symbols: list[str]) -> None:
        payload = {
            "symbols": symbols,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.runtime_control_file.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_control_file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        if self.mongo_store:
            self.mongo_store.persist_runtime_control(payload)

    @staticmethod
    def _normalize_symbols(symbols: list[Any]) -> list[str]:
        out: list[str] = []
        for raw in symbols or []:
            clean = str(raw or "").strip().upper()
            if not clean or clean in out:
                continue
            out.append(clean)
        return out

    def get_options(self) -> Dict[str, Any]:
        with self._lock:
            config = self._load()
            live_loop = config.get("live_loop", {})
            live_symbols = [str(s).upper() for s in (live_loop.get("symbols") or [])]
            pair_symbols = [str(s).upper() for s in (config.get("pairs") or [])]
            all_symbols = sorted(set(live_symbols + pair_symbols))
            selected_symbol = live_symbols[0] if live_symbols else None
            return {
                "symbols": all_symbols,
                "selected_symbol": selected_symbol,
                "live_symbols": live_symbols,
                "selected_symbols": live_symbols,
                "runtime_settings": self._extract_runtime_settings(config),
            }

    def _extract_runtime_settings(self, config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        settings: Dict[str, Dict[str, Any]] = {}
        for section, keys in self.EDITABLE_FIELDS.items():
            source = config.get(section, {})
            settings[section] = {key: source.get(key) for key in keys}
        return settings

    @staticmethod
    def _coerce_number(raw: Any, allow_null: bool = False) -> Optional[float]:
        if raw is None or raw == "":
            if allow_null:
                return None
            raise ValueError("value is required")
        value = float(raw)
        if not value and value != 0:
            raise ValueError("invalid numeric value")
        return value

    def update_runtime_settings(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            config = self._load()

            account = config.setdefault("account", {})
            execution = config.setdefault("execution", {})
            strategy = config.setdefault("strategy", {})
            live_loop = config.setdefault("live_loop", {})

            account_payload = payload.get("account", {}) or {}
            execution_payload = payload.get("execution", {}) or {}
            strategy_payload = payload.get("strategy", {}) or {}
            live_loop_payload = payload.get("live_loop", {}) or {}

            if "starting_balance_usd" in account_payload:
                account["starting_balance_usd"] = self._coerce_number(account_payload.get("starting_balance_usd"))
            if "risk_per_trade_pct" in account_payload:
                risk_pct = self._coerce_number(account_payload.get("risk_per_trade_pct"))
                if risk_pct is None or risk_pct <= 0:
                    raise ValueError("account.risk_per_trade_pct must be > 0")
                account["risk_per_trade_pct"] = risk_pct
            if "paper_risk_usd" in account_payload:
                paper_risk = self._coerce_number(account_payload.get("paper_risk_usd"), allow_null=True)
                if paper_risk is not None and paper_risk <= 0:
                    raise ValueError("account.paper_risk_usd must be > 0")
                if paper_risk is None:
                    account.pop("paper_risk_usd", None)
                else:
                    account["paper_risk_usd"] = paper_risk

            for key in ("fee_bps_per_side", "slippage_bps_per_side"):
                if key in execution_payload:
                    execution[key] = self._coerce_number(execution_payload.get(key))

            for key in ("atr_multiplier", "risk_reward", "min_confidence"):
                if key in strategy_payload:
                    strategy[key] = self._coerce_number(strategy_payload.get(key))

            int_live_keys = {"max_open_trades", "max_wait_candles"}
            for key in self.EDITABLE_FIELDS["live_loop"]:
                if key not in live_loop_payload:
                    continue
                value = self._coerce_number(live_loop_payload.get(key))
                live_loop[key] = int(value) if key in int_live_keys and value is not None else value

            self._save(config)

            return {
                "ok": True,
                "message": "Runtime settings saved to config. Restart live trading to apply executor sizing changes immediately.",
                "runtime_settings": self._extract_runtime_settings(config),
            }

    def set_symbol(self, symbol: str) -> Dict[str, Any]:
        clean = str(symbol or "").strip().upper()
        if not clean:
            raise ValueError("symbol is required")

        return self.set_symbols([clean])

    def set_symbols(self, symbols: list[str]) -> Dict[str, Any]:
        clean_symbols = self._normalize_symbols(symbols)
        if not clean_symbols:
            raise ValueError("at least one symbol is required")

        with self._lock:
            config = self._load()
            live_loop = config.setdefault("live_loop", {})
            live_loop["symbols"] = clean_symbols

            pairs = config.setdefault("pairs", [])
            for symbol in clean_symbols:
                if symbol not in pairs:
                    pairs.append(symbol)

            self._save(config)
            self._save_runtime_symbols(clean_symbols)

        return {
            "ok": True,
            "symbol": clean_symbols[0],
            "symbols": clean_symbols,
            "message": "Saved and sent to runtime. No restart required (applies on next scan cycle).",
        }


class DashboardHandler(SimpleHTTPRequestHandler):
    cache: EventStateCache
    history_cache: TradeHistoryCache
    config_store: ConfigStore
    news_fetcher: NewsFetcher
    symbol_catalog: SymbolCatalog
    mongo_store: Optional[MongoStore]
    analytics_engine: AnalyticsEngine

    def __init__(
        self,
        *args: Any,
        directory: str,
        cache: EventStateCache,
        history_cache: TradeHistoryCache,
        config_store: ConfigStore,
        news_fetcher: NewsFetcher,
        symbol_catalog: SymbolCatalog,
        mongo_store: Optional[MongoStore],
        analytics_engine: AnalyticsEngine,
        **kwargs: Any,
    ):
        self.cache = cache
        self.history_cache = history_cache
        self.config_store = config_store
        self.news_fetcher = news_fetcher
        self.symbol_catalog = symbol_catalog
        self.mongo_store = mongo_store
        self.analytics_engine = analytics_engine
        super().__init__(*args, directory=directory, **kwargs)

    def _write_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self, max_bytes: int = 1_048_576) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        if length > max_bytes:
            self._write_json({"error": "Payload too large"}, status=413)
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/state":
            self._write_json(self.cache.refresh())
            return
        if path == "/api/history":
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or ["200"])[0])
            except ValueError:
                limit = 200
            self._write_json(self.history_cache.refresh(limit=limit))
            return
        if path == "/api/binance":
            try:
                from src.binance_executor import BinanceExecutor
                import json as _json
                config = _json.load(open(self.config_store.config_file, "r"))
                executor = BinanceExecutor.from_env(config)
                if not executor.enabled:
                    self._write_json({"enabled": False, "error": "No API keys"})
                    return
                account = executor.get_account()
                balance = float(account.get("totalWalletBalance", 0))
                available = float(account.get("availableBalance", 0))
                unrealized = float(account.get("totalUnrealizedProfit", 0))
                positions = [
                    {
                        "symbol": p["symbol"],
                        "side": "LONG" if float(p.get("positionAmt", 0)) > 0 else "SHORT",
                        "size": abs(float(p.get("positionAmt", 0))),
                        "entry": float(p.get("entryPrice", 0)),
                        "pnl": float(p.get("unrealizedProfit", 0)),
                        "margin": float(p.get("initialMargin", 0)),
                    }
                    for p in account.get("positions", [])
                    if float(p.get("positionAmt", 0)) != 0
                ]
                self._write_json({
                    "enabled": True,
                    "demo": executor.demo,
                    "balance": round(balance, 2),
                    "available": round(available, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "total_equity": round(balance + unrealized, 2),
                    "initial_balance": 5000.0,
                    "total_pnl": round(balance + unrealized - 5000.0, 2),
                    "open_positions": positions,
                    "time": datetime.now(timezone.utc).isoformat(),
                })
            except Exception as exc:
                self._write_json({"enabled": False, "error": str(exc)})
            return
        if path == "/api/health":
            self._write_json({"ok": True, "time": datetime.now(timezone.utc).isoformat()})
            return
        if path == "/api/storage":
            if self.mongo_store:
                status = self.mongo_store.status()
            else:
                status = {"enabled": False, "available": False, "database": None, "uri": None, "error": None}
            status["time"] = datetime.now(timezone.utc).isoformat()
            self._write_json(status)
            return
        if path == "/api/options":
            self._write_json(self.config_store.get_options())
            return
        if path == "/api/symbols":
            query = parse_qs(parsed.query)
            q = str((query.get("q") or [""])[0] or "")
            try:
                limit = int((query.get("limit") or ["500"])[0])
            except ValueError:
                limit = 500
            self._write_json(self.symbol_catalog.get_symbols(query=q, limit=limit))
            return
        if path == "/api/news":
            force_values = parse_qs(parsed.query).get("force", ["0"])
            force = str(force_values[0]).strip().lower() in {"1", "true", "yes"}
            self._write_json(self.news_fetcher.refresh(force=force))
            return
        if path == "/api/analytics":
            self._write_json(self.analytics_engine.compute())
            return
        if path != "/":
            requested = Path(self.directory) / path.lstrip("/")
            if not requested.exists() and "." not in Path(path).name:
              self.path = "/index.html"
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/config/symbol", "/api/config/symbols", "/api/config/runtime-settings"}:
            self._write_json({"ok": False, "error": "Not found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_json_body()
            if path == "/api/config/runtime-settings":
                result = self.config_store.update_runtime_settings(payload)
            elif path == "/api/config/symbols":
                symbols = payload.get("symbols")
                if not isinstance(symbols, list):
                    raise ValueError("symbols must be a list")
                result = self.config_store.set_symbols([str(s or "") for s in symbols])
            else:
                symbol = payload.get("symbol")
                result = self.config_store.set_symbol(str(symbol or ""))
            self._write_json(result)
        except json.JSONDecodeError:
            self._write_json({"ok": False, "error": "Invalid JSON body"}, status=HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._write_json({"ok": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto TP/SL frontend server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--events-file", default="../data/live_events.jsonl")
    parser.add_argument("--history-events-file", default="../data/live_events_history.jsonl")
    parser.add_argument("--config-file", default="../config.json")
    parser.add_argument("--runtime-control-file", default="../data/runtime_control.json")
    parser.add_argument("--news-refresh-sec", type=int, default=10)
    parser.add_argument("--news-max-items", type=int, default=30)
    parser.add_argument("--mongo-uri", default="mongodb://127.0.0.1:27017")
    parser.add_argument("--mongo-db", default="crypto_trading_live")
    parser.add_argument("--mongo-required", type=int, default=1)
    parser.add_argument("--static-dir", default=".")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent

    events_file = Path(args.events_file)
    if not events_file.is_absolute():
        events_file = (script_dir / events_file).resolve()
    events_file.parent.mkdir(parents=True, exist_ok=True)

    history_events_file = Path(args.history_events_file)
    if not history_events_file.is_absolute():
        history_events_file = (script_dir / history_events_file).resolve()
    history_events_file.parent.mkdir(parents=True, exist_ok=True)

    config_file = Path(args.config_file)
    if not config_file.is_absolute():
        config_file = (script_dir / args.config_file).resolve()

    runtime_control_file = Path(args.runtime_control_file)
    if not runtime_control_file.is_absolute():
        runtime_control_file = (script_dir / runtime_control_file).resolve()

    static_dir = Path(args.static_dir)
    if not static_dir.is_absolute():
        static_dir = (script_dir / static_dir).resolve()

    mongo_store = MongoStore(
        uri=args.mongo_uri,
        database=args.mongo_db,
        required=bool(int(args.mongo_required)),
    )

    cache = EventStateCache(events_file=events_file, mongo_store=mongo_store)
    history_cache = TradeHistoryCache(history_file=history_events_file, max_items=5000, mongo_store=mongo_store)
    config_store = ConfigStore(
        config_file=config_file,
        runtime_control_file=runtime_control_file,
        mongo_store=mongo_store,
    )
    news_fetcher = NewsFetcher(refresh_seconds=args.news_refresh_sec, max_items=args.news_max_items)
    symbol_catalog = SymbolCatalog(refresh_seconds=300)
    analytics_engine = AnalyticsEngine(history_cache=history_cache)

    def handler_factory(*h_args: Any, **h_kwargs: Any) -> DashboardHandler:
        return DashboardHandler(
            *h_args,
            directory=str(static_dir),
            cache=cache,
            history_cache=history_cache,
            config_store=config_store,
            news_fetcher=news_fetcher,
            symbol_catalog=symbol_catalog,
            mongo_store=mongo_store,
            analytics_engine=analytics_engine,
            **h_kwargs,
        )

    server = ThreadingHTTPServer((args.host, args.port), handler_factory)
    print(
        json.dumps(
            {
                "type": "FRONTEND_STARTED",
                "host": args.host,
                "port": args.port,
                "events_file": str(events_file),
                "history_events_file": str(history_events_file),
                "config_file": str(config_file),
                "runtime_control_file": str(runtime_control_file),
                "news_refresh_sec": args.news_refresh_sec,
                "news_max_items": args.news_max_items,
                "mongo_uri": args.mongo_uri,
                "mongo_db": args.mongo_db,
                "mongo_required": bool(int(args.mongo_required)),
                "mongo_status": mongo_store.status(),
                "static_dir": str(static_dir),
            }
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
