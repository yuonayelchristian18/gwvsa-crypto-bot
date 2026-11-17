
import os
import time
import csv
import requests
import pandas as pd

# ============================================================
# GWVSA KRAKEN SCANNER - PAPER / DRY-RUN VERSION
# ============================================================

API_URL = "https://api.kraken.com"

MARKETS = [
    "XBT/CAD",
    "ETH/CAD",
    "SOL/CAD",
    "XRP/CAD",
    "ADA/CAD",
    "DOGE/CAD",
]

RISK_PER_TRADE_CAD = 2.00
MAX_NOTIONAL_CAD = 15.00
REWARD_RISK_RATIO = 2.0

EMA_FAST = 8
EMA_MEDIUM = 40
EMA_SLOW = 200

RSI_PERIOD = 14
VOLUME_PERIOD = 20
ATR_PERIOD = 14

SCAN_TIMEFRAMES = {
    "5m": 5,
    "15m": 15,
    "1h": 60,
    "4h": 240,
}

DRY_RUN = True
CSV_FILE = "gwvsa_signals.csv"

session = requests.Session()
session.headers.update({
    "User-Agent": "GWVSA-Kraken-Scanner/1.0"
})


# ============================================================
# KRAKEN API
# ============================================================

def api_get(endpoint, params=None):
    response = session.get(
        API_URL + endpoint,
        params=params,
        timeout=25
    )
    response.raise_for_status()

    data = response.json()

    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    return data["result"]


def discover_markets():
    all_pairs = api_get("/0/public/AssetPairs")
    discovered = {}

    for requested in MARKETS:
        for pair_id, info in all_pairs.items():
            if info.get("wsname") == requested:
                discovered[requested] = {
                    "id": pair_id,
                    "altname": info.get("altname", pair_id),
                    "ordermin": float(info.get("ordermin", 0)),
                    "lot_decimals": int(info.get("lot_decimals", 8)),
                }
                break

        if requested not in discovered:
            print("[PAIR NOT FOUND]", requested)

    return discovered


def get_candles(pair, interval):
    result = api_get(
        "/0/public/OHLC",
        {
            "pair": pair,
            "interval": interval
        }
    )

    candle_key = next(
        (key for key in result if key != "last"),
        None
    )

    if not candle_key:
        return None

    rows = result[candle_key]

    if len(rows) < 210:
        return None

    df = pd.DataFrame(
        rows,
        columns=[
            "time", "open", "high", "low",
            "close", "vwap", "volume", "count"
        ]
    )

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_numeric(df["time"], errors="coerce")

    df = df.dropna().reset_index(drop=True)

    # Kraken's final candle may still be forming.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    return df.reset_index(drop=True)


# ============================================================
# INDICATORS
# ============================================================

def add_indicators(df):
    df = df.copy()

    df["ema8"] = df["close"].ewm(
        span=EMA_FAST,
        adjust=False
    ).mean()

    df["ema40"] = df["close"].ewm(
        span=EMA_MEDIUM,
        adjust=False
    ).mean()

    df["ema200"] = df["close"].ewm(
        span=EMA_SLOW,
        adjust=False
    ).mean()

    delta = df["close"].diff()

    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)

    avg_gain = gain.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False
    ).mean()

    avg_loss = loss.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False
    ).mean()

    rs = avg_gain / avg_loss.replace(0, float("nan"))

    df["rsi"] = 100 - (100 / (1 + rs))
    df["rsi"] = df["rsi"].fillna(50)

    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = true_range.rolling(ATR_PERIOD).mean()

    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df


# ============================================================
# HIGHER TIMEFRAME TREND
# ============================================================

def bullish_trend(df):
    if df is None or len(df) < 205:
        return False

    df = add_indicators(df)
    last = df.iloc[-1]

    return (
        last["close"] > last["ema200"]
        and last["ema8"] > last["ema40"]
        and last["ema40"] > last["ema200"]
    )


# ============================================================
# LIQUIDITY SWEEP
# ============================================================

def find_bullish_sweep(df, index):
    if index < 20:
        return None

    previous = df.iloc[index - 20:index]

    candle = df.iloc[index]

    previous_low = previous["low"].min()

    swept = (
        candle["low"] < previous_low
        and candle["close"] > previous_low
    )

    if swept:
        return float(candle["low"])

    return None


def recent_bullish_sweep(df, end_index, lookback=10):
    start = max(20, end_index - lookback)

    for index in range(end_index - 1, start - 1, -1):
        sweep_low = find_bullish_sweep(df, index)

        if sweep_low is not None:
            return sweep_low

    return None


# ============================================================
# STRUCTURE SHIFT AND RETEST
# ============================================================

def bullish_structure_retest(df):
    if df is None or len(df) < 220:
        return None

    df = add_indicators(df)

    current_index = len(df) - 1
    breakout_index = current_index - 1

    current = df.iloc[current_index]
    breakout = df.iloc[breakout_index]

    # Find a recent liquidity sweep before the breakout.
    sweep_low = recent_bullish_sweep(
        df,
        breakout_index,
        lookback=10
    )

    if sweep_low is None:
        return None

    # Structure level from the candles before the breakout.
    previous_structure = df.iloc[
        breakout_index - 5:breakout_index
    ]

    structure_high = previous_structure["high"].max()

    # Break of structure.
    structure_break = (
        breakout["close"] > structure_high
        and breakout["close"] > breakout["open"]
    )

    if not structure_break:
        return None

    # Retest of the broken structure.
    retest = (
        current["low"] <= structure_high * 1.002
        and current["close"] > structure_high
        and current["close"] > current["open"]
    )

    if not retest:
        return None

    # RSI pullback / momentum confirmation.
    rsi_ok = 45 <= current["rsi"] <= 68

    # Volume confirmation on the breakout candle.
    volume_ok = (
        breakout["volume"] > breakout["volume_avg"]
    )

    if not rsi_ok or not volume_ok:
        return None

    return {
        "sweep_low": sweep_low,
        "structure": float(structure_high),
        "atr": float(current["atr"]),
        "rsi": float(current["rsi"])
    }


# ============================================================
# FIVE-MINUTE LIQUIDITY CONFIRMATION
# ============================================================

def five_minute_confirmation(df):
    if df is None or len(df) < 30:
        return False

    start = len(df) - 7

    for index in range(start, len(df)):
        if index < 20:
            continue

        if find_bullish_sweep(df, index) is not None:
            return True

    return False


# ============================================================
# POSITION SIZING
# ============================================================

def calculate_trade(entry, stop):
    risk_distance = entry - stop

    if risk_distance <= 0:
        return None

    risk_based_quantity = (
        RISK_PER_TRADE_CAD / risk_distance
    )

    allocation_limited_quantity = (
        MAX_NOTIONAL_CAD / entry
    )

    quantity = min(
        risk_based_quantity,
        allocation_limited_quantity
    )

    notional = quantity * entry
    planned_risk = quantity * risk_distance

    target = (
        entry
        + risk_distance * REWARD_RISK_RATIO
    )

    return {
        "quantity": quantity,
        "notional": notional,
        "planned_risk": planned_risk,
        "stop": stop,
        "target": target
    }


# ============================================================
# SIGNAL LOG
# ============================================================

def save_signal(signal):
    exists = os.path.exists(CSV_FILE)

    fields = [
        "time",
        "pair",
        "entry",
        "stop",
        "target",
        "quantity",
        "notional",
        "planned_risk",
        "rsi",
        "status"
    ]

    with open(CSV_FILE, "a", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        if not exists:
            writer.writeheader()

        writer.writerow(signal)


# ============================================================
# MAIN GWVSA SCANNER
# ============================================================

def scan_market(pair_name, pair_info):
    print("\nScanning:", pair_name)

    try:
        candles_4h = get_candles(
            pair_info["altname"], 240
        )

        candles_1h = get_candles(
            pair_info["altname"], 60
        )

        candles_15m = get_candles(
            pair_info["altname"], 15
        )

        candles_5m = get_candles(
            pair_info["altname"], 5
        )

        if any(
            df is None
            for df in [
                candles_4h,
                candles_1h,
                candles_15m,
                candles_5m
            ]
        ):
            print("[SKIP] Not enough candle data")
            return

        # Higher-timeframe trend alignment.
        if not bullish_trend(candles_4h):
            print("[NO ENTRY] 4H trend not bullish")
            return

        if not bullish_trend(candles_1h):
            print("[NO ENTRY] 1H trend not bullish")
            return

        # 15-minute setup.
        setup = bullish_structure_retest(candles_15m)

        if setup is None:
            print("[NO ENTRY] No valid 15M setup")
            return

        # 5-minute liquidity confirmation.
        if not five_minute_confirmation(candles_5m):
            print("[NO ENTRY] No 5M liquidity confirmation")
            return

        latest = candles_15m.iloc[-1]

        entry = float(latest["close"])

        # Stop below the liquidity sweep with an ATR buffer.
        stop = (
            setup["sweep_low"]
            - setup["atr"] * 0.2
        )

        trade = calculate_trade(entry, stop)

        if trade is None:
            print("[SKIP] Invalid stop distance")
            return

        if trade["notional"] < 1:
            print("[SKIP] Position too small")
            return

        signal = {
            "time": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC",
                time.gmtime()
            ),
            "pair": pair_name,
            "entry": round(entry, 8),
            "stop": round(trade["stop"], 8),
            "target": round(trade["target"], 8),
            "quantity": round(trade["quantity"], 8),
            "notional": round(trade["notional"], 2),
            "planned_risk": round(
                trade["planned_risk"], 2
            ),
            "rsi": round(setup["rsi"], 2),
            "status": "PAPER_SIGNAL"
        }

        save_signal(signal)

        print("\n========== GWVSA BUY SIGNAL ==========")
        for key, value in signal.items():
            print(f"{key}: {value}")
        print("======================================")

        if DRY_RUN:
            print("[DRY RUN] No real order was placed.")

    except Exception as error:
        print("[ERROR]", pair_name, str(error))


# ============================================================
# RUN ALL MARKETS
# ============================================================

def main():
    print("======================================")
    print("       GWVSA KRAKEN SCANNER")
    print("======================================")
    print("Mode:", "DRY RUN" if DRY_RUN else "LIVE")
    print("Risk per trade: $", RISK_PER_TRADE_CAD)
    print("Maximum allocation: $", MAX_NOTIONAL_CAD)
    print("Daily trade limit: NONE")
    print("======================================")

    markets = discover_markets()

    for pair_name in MARKETS:
        pair_info = markets.get(pair_name)

        if pair_info is None:
            continue

        scan_market(pair_name, pair_info)

    print("\nScan completed.")


if __name__ == "__main__":
    main()
