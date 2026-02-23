from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from typing import Dict, List

from .models import Candle, MarketContext
from .mock_data import MockFuturesData

BASE_URLS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
]


class BinanceFuturesRestClient:
    def __init__(
        self,
        timeout_seconds: int = 12,
        retries: int = 3,
        retry_sleep: float = 0.8,
        allow_mock_fallback: bool = False,
        force_mock: bool = False,
        mock_seed: int = 42,
    ):
        self.timeout_seconds = timeout_seconds
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.allow_mock_fallback = allow_mock_fallback
        self.force_mock = force_mock
        self.mock = MockFuturesData(seed=mock_seed)
        self.used_mock = False

    def _get_json(self, path: str, params: Dict[str, str]) -> Dict:
        if self.force_mock:
            raise RuntimeError("force_mock is enabled")

        query = urllib.parse.urlencode(params)
        last_error = None
        for base_url in BASE_URLS:
            url = f"{base_url}{path}?{query}" if query else f"{base_url}{path}"

            for _ in range(self.retries):
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "crypto-data-bot/1.0"})
                    with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
                        return json.loads(response.read().decode("utf-8"))
                except Exception as exc:
                    last_error = exc
                    time.sleep(self.retry_sleep)

            # Fallback to curl for environments where Python DNS resolution is blocked
            # but CLI curl is allowed.
            for _ in range(self.retries):
                try:
                    proc = subprocess.run(
                        ["curl", "-sS", "--max-time", str(self.timeout_seconds), url],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if proc.returncode == 0 and proc.stdout.strip():
                        return json.loads(proc.stdout)
                    last_error = RuntimeError(proc.stderr.strip() or "curl returned no data")
                except Exception as exc:
                    last_error = exc
                time.sleep(self.retry_sleep)

        raise RuntimeError(f"Failed request across Binance hosts: {last_error}")

    def fetch_klines(self, symbol: str, interval: str, limit: int = 300) -> List[Candle]:
        try:
            payload = self._get_json(
                "/fapi/v1/klines",
                {"symbol": symbol, "interval": interval, "limit": str(limit)},
            )
        except Exception:
            if not self.allow_mock_fallback and not self.force_mock:
                raise
            self.used_mock = True
            return self.mock.klines(symbol=symbol, interval=interval, limit=limit)

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
        return candles

    def fetch_market_context(self, symbol: str) -> MarketContext:
        try:
            premium = self._get_json("/fapi/v1/premiumIndex", {"symbol": symbol})
            open_interest = self._get_json("/fapi/v1/openInterest", {"symbol": symbol})
            return MarketContext(
                mark_price=float(premium["markPrice"]),
                funding_rate=float(premium["lastFundingRate"]),
                open_interest=float(open_interest["openInterest"]),
            )
        except Exception:
            if not self.allow_mock_fallback and not self.force_mock:
                raise
            self.used_mock = True
            # Use latest mock candle close as mark price base.
            mock_candle = self.mock.klines(symbol=symbol, interval="5m", limit=2)[-1]
            return self.mock.market_context(symbol=symbol, mark_price=mock_candle.close)
