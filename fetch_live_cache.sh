#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="${1:-/Users/user/Desktop/Work/gotoapi/crypto/data/live}"
SYMBOLS="${2:-BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,ADAUSDT,BNBUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,TRXUSDT}"
TIMEFRAMES="${3:-5m,15m}"
LIMIT="${4:-1500}"

mkdir -p "$OUT_DIR"
IFS=',' read -r -a SYM_ARR <<< "$SYMBOLS"
IFS=',' read -r -a TF_ARR <<< "$TIMEFRAMES"

BINANCE_HOSTS=("fapi.binance.com" "fapi1.binance.com" "fapi2.binance.com")

curl_binance() {
  local path_query="$1"
  local out_file="$2"
  local ok=0

  for host in "${BINANCE_HOSTS[@]}"; do
    if curl -sS --retry 3 --retry-delay 1 --max-time 30 \
      "https://${host}${path_query}" \
      -o "${out_file}.tmp"; then
      mv "${out_file}.tmp" "$out_file"
      ok=1
      break
    fi
  done

  if [ "$ok" -ne 1 ]; then
    rm -f "${out_file}.tmp"
    echo "Failed to fetch ${path_query} from all Binance hosts" >&2
    return 1
  fi
}

for symbol in "${SYM_ARR[@]}"; do
  symbol="${symbol// /}"
  [ -z "$symbol" ] && continue

  curl_binance \
    "/fapi/v1/premiumIndex?symbol=${symbol}" \
    "$OUT_DIR/${symbol}_premium.json"

  curl_binance \
    "/fapi/v1/openInterest?symbol=${symbol}" \
    "$OUT_DIR/${symbol}_open_interest.json"

  for tf in "${TF_ARR[@]}"; do
    tf="${tf// /}"
    [ -z "$tf" ] && continue

    curl_binance \
      "/fapi/v1/klines?symbol=${symbol}&interval=${tf}&limit=${LIMIT}" \
      "$OUT_DIR/${symbol}_${tf}_klines.json"
  done
done

echo "Live cache written to: $OUT_DIR"
