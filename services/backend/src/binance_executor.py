"""Binance Futures order executor for demo/live trading.

Places real orders on Binance Futures (testnet or production) when the
signal engine decides to open or close a trade. Uses MARKET orders only
since the testnet doesn't support stop order types.

The engine's existing TP/SL/trailing/exit logic decides when to close —
this module just executes the open and close via the API.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    requests = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class BinanceExecutor:
    """Executes trades on Binance Futures (demo or live)."""

    TESTNET_BASE = "https://testnet.binancefuture.com"
    LIVE_BASE = "https://fapi.binance.com"

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        demo: bool = True,
        risk_per_trade_usd: float = 10.0,
        max_position_usd: float = 100.0,
        enabled: bool = True,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.demo = demo
        self.base_url = self.TESTNET_BASE if demo else self.LIVE_BASE
        self.risk_per_trade_usd = risk_per_trade_usd
        self.max_position_usd = max_position_usd
        self.enabled = enabled and bool(api_key) and bool(secret_key)
        self._exchange_info: Dict[str, Dict] = {}
        self._active_position: Optional[Dict[str, Any]] = None

        if self.enabled:
            if requests is None:
                logger.error("requests library not installed — executor disabled")
                self.enabled = False
            else:
                self._load_exchange_info()

    @classmethod
    def from_env(cls, config: Dict) -> "BinanceExecutor":
        """Create executor from environment variables and config."""
        if load_dotenv:
            env_path = Path(__file__).resolve().parent.parent / ".env"
            if env_path.exists():
                load_dotenv(str(env_path))

        api_key = os.getenv("BINANCE_API_KEY", "")
        secret_key = os.getenv("BINANCE_SECRET_KEY", "")
        demo = os.getenv("BINANCE_DEMO", "1") == "1"

        # Use actual account balance for position sizing
        executor = cls(
            api_key=api_key,
            secret_key=secret_key,
            demo=demo,
            risk_per_trade_usd=10.0,
            max_position_usd=100.0,
            enabled=bool(api_key and secret_key),
        )

        account_cfg = config.get("account", {})
        risk_per_trade_pct = float(account_cfg.get("risk_per_trade_pct", 0.02))
        max_position_pct = float(account_cfg.get("max_position_pct", 0.05))
        configured_balance = float(account_cfg.get("starting_balance_usd", 0.0) or 0.0)

        # Fetch real balance and size using configured wallet risk percentage.
        if executor.enabled:
            try:
                live_balance = executor.get_balance()
                executor.risk_per_trade_usd = live_balance * risk_per_trade_pct
                executor.max_position_usd = live_balance * max_position_pct
                logger.info(
                    "Live Binance sizing active: wallet_balance=%.2f USDT, configured_starting_balance=%.2f USDT, "
                    "risk_per_trade=%.2f USDT, max_position=%.2f USDT, risk_pct=%.4f, max_position_pct=%.4f, demo=%s",
                    live_balance,
                    configured_balance,
                    executor.risk_per_trade_usd,
                    executor.max_position_usd,
                    risk_per_trade_pct,
                    max_position_pct,
                    executor.demo,
                )
            except Exception:
                logger.warning(
                    "Falling back to default Binance sizing values; unable to fetch live wallet balance.",
                    exc_info=True,
                )

        return executor

    # ── API request helpers ──────────────────────────────────────────

    def _sign(self, params: Dict) -> str:
        query = urlencode(params)
        return hmac.new(
            self.secret_key.encode(), query.encode(), hashlib.sha256
        ).hexdigest()

    def _request(
        self, method: str, path: str, params: Optional[Dict] = None, signed: bool = True
    ) -> Dict:
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 10000
            params["signature"] = self._sign(params)

        url = f"{self.base_url}{path}"
        headers = {"X-MBX-APIKEY": self.api_key}

        try:
            resp = requests.request(
                method, url, headers=headers, params=params, timeout=15
            )
            data = resp.json()
            if "code" in data and int(data.get("code", 0)) < 0:
                logger.error("Binance API error: %s", data)
            return data
        except Exception as exc:
            logger.error("Binance request failed: %s", exc)
            return {"error": str(exc)}

    # ── Exchange info ────────────────────────────────────────────────

    def _load_exchange_info(self) -> None:
        try:
            data = self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
            for sym in data.get("symbols", []):
                filters = {f["filterType"]: f for f in sym.get("filters", [])}
                self._exchange_info[sym["symbol"]] = {
                    "pricePrecision": sym.get("pricePrecision", 2),
                    "quantityPrecision": sym.get("quantityPrecision", 3),
                    "minQty": float(filters.get("LOT_SIZE", {}).get("minQty", 0.001)),
                    "stepSize": float(filters.get("LOT_SIZE", {}).get("stepSize", 0.001)),
                    "minNotional": float(
                        filters.get("MIN_NOTIONAL", {}).get("notional", 5)
                    ),
                    "tickSize": float(filters.get("PRICE_FILTER", {}).get("tickSize", 0.01)),
                }
            logger.info("Loaded exchange info for %d symbols", len(self._exchange_info))
        except Exception as exc:
            logger.error("Failed to load exchange info: %s", exc)

    def _get_precision(self, symbol: str) -> Dict:
        return self._exchange_info.get(symbol, {
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "minQty": 0.001,
            "stepSize": 0.001,
            "minNotional": 5,
            "tickSize": 0.01,
        })

    def _round_quantity(self, symbol: str, qty: float) -> float:
        info = self._get_precision(symbol)
        step = info["stepSize"]
        precision = info["quantityPrecision"]
        rounded = round(qty - (qty % step), precision)
        return max(rounded, info["minQty"])

    def _round_price(self, symbol: str, price: float) -> float:
        info = self._get_precision(symbol)
        tick = info.get("tickSize", 0.01)
        if tick > 0:
            price = round(price - (price % tick), 10)
        # Also respect pricePrecision
        precision = info["pricePrecision"]
        return round(price, precision)

    # ── Account info ─────────────────────────────────────────────────

    def get_account(self) -> Dict:
        if not self.enabled:
            return {"error": "executor disabled"}
        return self._request("GET", "/fapi/v2/account")

    def get_balance(self) -> float:
        account = self.get_account()
        return float(account.get("availableBalance", 0))

    def get_position(self, symbol: str) -> Optional[Dict]:
        if not self.enabled:
            return None
        positions = self._request("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        if isinstance(positions, list):
            for p in positions:
                if p.get("symbol") == symbol and float(p.get("positionAmt", 0)) != 0:
                    return p
        return None

    # ── Order execution ──────────────────────────────────────────────

    def _get_current_price(self, symbol: str) -> float:
        """Get current market price for a symbol."""
        try:
            data = self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol}, signed=False)
            return float(data.get("price", 0))
        except Exception:
            return 0.0

    def calculate_quantity(self, symbol: str, entry_price: float, stop_loss: float) -> float:
        """Calculate position size based on risk per trade."""
        # Use current price for more accurate sizing (testnet prices may differ)
        current_price = self._get_current_price(symbol) or entry_price

        sl_distance = abs(entry_price - stop_loss)
        if sl_distance <= 0:
            return 0.0

        # Risk amount / SL distance = quantity
        raw_qty = self.risk_per_trade_usd / sl_distance
        qty = self._round_quantity(symbol, raw_qty)

        # Ensure notional value meets minimum using current price
        info = self._get_precision(symbol)
        notional = qty * current_price
        if notional < info["minNotional"]:
            qty = self._round_quantity(symbol, info["minNotional"] / current_price * 1.1)

        # Cap at max position
        if qty * current_price > self.max_position_usd:
            qty = self._round_quantity(symbol, self.max_position_usd / current_price)

        return qty

    def open_trade(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> Dict[str, Any]:
        """Open a position via MARKET order."""
        if not self.enabled:
            return {"status": "disabled", "executed": False}

        # Check for existing position
        existing = self.get_position(symbol)
        if existing:
            logger.warning("Already have position in %s: %s", symbol, existing.get("positionAmt"))
            return {"status": "already_open", "executed": False, "position": existing}

        qty = self.calculate_quantity(symbol, entry_price, stop_loss)
        if qty <= 0:
            return {"status": "invalid_quantity", "executed": False}

        order_side = "BUY" if side == "LONG" else "SELL"
        logger.info(
            "Opening %s %s: qty=%s, entry=~%s, SL=%s, TP=%s",
            side, symbol, qty, entry_price, stop_loss, take_profit,
        )

        result = self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": order_side,
            "type": "MARKET",
            "quantity": qty,
        })

        if result.get("orderId"):
            # Wait for fill and get actual entry
            time.sleep(1)
            position = self.get_position(symbol)
            actual_entry = float(position["entryPrice"]) if position else entry_price
            actual_qty = abs(float(position["positionAmt"])) if position else qty

            self._active_position = {
                "symbol": symbol,
                "side": side,
                "quantity": actual_qty,
                "entry_price": actual_entry,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "order_id": result["orderId"],
                "opened_at": time.time(),
            }

            # Try to place a LIMIT order for TP
            tp_side = "SELL" if side == "LONG" else "BUY"
            tp_price = self._round_price(symbol, take_profit)
            tp_result = self._request("POST", "/fapi/v1/order", {
                "symbol": symbol,
                "side": tp_side,
                "type": "LIMIT",
                "price": tp_price,
                "quantity": actual_qty,
                "timeInForce": "GTC",
                "reduceOnly": "true",
            })
            tp_order_id = tp_result.get("orderId")
            if tp_order_id:
                self._active_position["tp_order_id"] = tp_order_id
                logger.info("TP LIMIT placed at %s (order %s)", tp_price, tp_order_id)

            return {
                "status": "filled",
                "executed": True,
                "order_id": result["orderId"],
                "entry_price": actual_entry,
                "quantity": actual_qty,
                "notional": round(actual_qty * actual_entry, 2),
                "tp_order_id": tp_order_id,
            }
        else:
            logger.error("Order failed: %s", result)
            return {
                "status": "failed",
                "executed": False,
                "error": result.get("msg", str(result)),
            }

    def close_trade(self, symbol: str, side: str, reason: str = "") -> Dict[str, Any]:
        """Close position via MARKET order."""
        if not self.enabled:
            return {"status": "disabled", "executed": False}

        position = self.get_position(symbol)
        if not position:
            self._active_position = None
            return {"status": "no_position", "executed": False}

        qty = abs(float(position["positionAmt"]))
        if qty <= 0:
            self._active_position = None
            return {"status": "no_position", "executed": False}

        # Cancel any open orders first (TP limit)
        self._request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})

        close_side = "SELL" if side == "LONG" else "BUY"
        logger.info("Closing %s %s: qty=%s, reason=%s", side, symbol, qty, reason)

        result = self._request("POST", "/fapi/v1/order", {
            "symbol": symbol,
            "side": close_side,
            "type": "MARKET",
            "quantity": qty,
            "reduceOnly": "true",
        })

        if result.get("orderId"):
            exit_info = {
                "status": "closed",
                "executed": True,
                "order_id": result["orderId"],
                "reason": reason,
            }

            # Get final PnL from position
            time.sleep(0.5)
            unrealized = float(position.get("unRealizedProfit", 0))
            entry_price = float(position.get("entryPrice", 0))
            exit_info["entry_price"] = entry_price
            exit_info["unrealized_pnl"] = unrealized
            exit_info["quantity"] = qty

            self._active_position = None
            return exit_info
        else:
            logger.error("Close order failed: %s", result)
            return {
                "status": "close_failed",
                "executed": False,
                "error": result.get("msg", str(result)),
            }

    def has_open_position(self, symbol: str) -> bool:
        """Check if there's an active position for a symbol."""
        if not self.enabled:
            return False
        return self.get_position(symbol) is not None

    def get_active_position_info(self) -> Optional[Dict]:
        return self._active_position

    def status(self) -> Dict[str, Any]:
        """Get executor status for dashboard."""
        if not self.enabled:
            return {"enabled": False, "demo": self.demo, "reason": "no API keys"}
        try:
            balance = self.get_balance()
            return {
                "enabled": True,
                "demo": self.demo,
                "base_url": self.base_url,
                "balance": balance,
                "risk_per_trade": self.risk_per_trade_usd,
                "max_position": self.max_position_usd,
                "sizing": {
                    "mode": "live_balance_pct",
                    "risk_per_trade": self.risk_per_trade_usd,
                    "max_position": self.max_position_usd,
                },
                "active_position": self._active_position,
            }
        except Exception as exc:
            return {"enabled": True, "demo": self.demo, "error": str(exc)}
