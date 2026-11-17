
import os
import time
import csv
import requests
import pandas as pd

# ============================================================
# GWVSA KRAKEN BOT - COMPLETE PAPER TRADING SCANNER
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

# Risk and position settings
RISK_PER_TRADE_CAD = 2.00
MAX_NOTIONAL_CAD = 15.00
REWARD_RISK_RATIO = 2.0

# Indicators
EMA_FAST = 8
EMA_MEDIUM = 40
EMA_SLOW = 200
RSI_PERIOD = 14
VOLUME_PERIOD = 20
ATR_PERIOD = 14

# Keep this True. This version does not execute real orders.
DRY_RUN = True

CSV_FILE = "gwvsa_signals.csv"

session = requests.Session()
session.headers.update({
    "User-Agent": "GWVSA-Kraken-Scanner/1.0"
})


# ============================================================
# GENERAL API FUNCTION
# ============================================================

def api_get(endpoint, params=None):

    response = session.get(
        API_URL + endpoint,
        params=params,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    if data.get("error"):
        raise RuntimeError(str(data["error"]))

    return data["result"]


# ============================================================
# DISCOVER KRAKEN CAD MARKETS
# ============================================================

def discover_markets():

    all_pairs = api_get("/0/public/AssetPairs")

    discovered = {}

    aliases = {
        "XBT": ["XBT", "XXBT"],
        "ETH": ["ETH", "XETH"],
        "SOL": ["SOL"],
        "XRP": ["XRP", "XXRP"],
        "ADA": ["ADA"],
        "DOGE": ["DOGE", "XDG"],
    }

    for requested in MARKETS:

        base, quote = requested.split("/")

        matched = None

        # First try Kraken's display name.
        for pair_id, info in all_pairs.items():

            if info.get("wsname") == requested:
                matched = (pair_id, info)
                break

        # If display name does not match, try Kraken aliases.
        if matched is None:

            possible_names = set()

            for base_alias in aliases.get(base, [base]):

                for quote_alias in [quote, "ZCAD"]:

                    possible_names.add(
                        base_alias + quote_alias
                    )

            for pair_id, info in all_pairs.items():

                altname = info.get("altname", "")

                if altname in possible_names:
                    matched = (pair_id, info)
                    break

        if matched is None:

            print("[PAIR NOT FOUND]", requested)
            continue

        pair_id, info = matched

        discovered[requested] = {
            "id": pair_id,
            "altname": info.get("altname", pair_id),
            "ordermin": float(info.get("ordermin", 0)),
            "costmin": float(info.get("costmin", 0)),
            "lot_decimals": int(
                info.get("lot_decimals", 8)
            )
        }

    print("\nDISCOVERED MARKETS:")

    for name, details in discovered.items():

        print(
            name,
            "->",
            details["id"],
            "| Kraken name:",
            details["altname"]
        )

    return discovered


# ============================================================
# DOWNLOAD CANDLE DATA
# ============================================================

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

    if candle_key is None:
        return None

    rows = result[candle_key]

    if len(rows) < 210:
        print("[INSUFFICIENT CANDLES]", pair, interval)
        return None

    df = pd.DataFrame(
        rows,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "vwap",
            "volume",
            "count"
        ]
    )

    numeric_columns = [
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]

    for column in numeric_columns:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df = df.dropna().reset_index(drop=True)

    # Exclude the current incomplete candle.
    if len(df) > 1:
        df = df.iloc[:-1].copy()

    if len(df) < 205:
        return None

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

    # RSI
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

    # ATR
    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs()
        ],
        axis=1
    ).max(axis=1)

    df["atr"] = true_range.rolling(
        ATR_PERIOD
    ).mean()

    # Average volume
    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df


# ============================================================
# TREND CONFIRMATION
# ============================================================

def check_trend(df, timeframe):

    if df is None or len(df) < 205:
        return False

    df = add_indicators(df)

    last = df.iloc[-1]

    bullish = (
        last["close"] > last["ema200"]
        and last["ema8"] > last["ema40"]
        and last["ema40"] > last["ema200"]
    )

    print(
        timeframe,
        "Close:", round(last["close"], 6),
        "EMA8:", round(last["ema8"], 6),
        "EMA40:", round(last["ema40"], 6),
        "EMA200:", round(last["ema200"], 6),
        "RSI:", round(last["rsi"], 2),
        "Bullish:", bullish
    )

    return bullish


# ============================================================
# BULLISH LIQUIDITY SWEEP
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

    for index in range(
        end_index - 1,
        start - 1,
        -1
    ):

        sweep_low = find_bullish_sweep(df, index)

        if sweep_low is not None:
            return sweep_low

    return None


# ============================================================
# 15-MINUTE STRUCTURE BREAK AND RETEST
# ============================================================

def bullish_structure_retest(df):

    if df is None or len(df) < 220:
        return None

    df = add_indicators(df)

    current_index = len(df) - 1
    breakout_index = current_index - 1

    current = df.iloc[current_index]
    breakout = df.iloc[breakout_index]

    # Recent liquidity sweep.
    sweep_low = recent_bullish_sweep(
        df,
        breakout_index,
        lookback=10
    )

    if sweep_low is None:
        print("[15M] No bullish liquidity sweep")
        return None

    # Structure level before the breakout.
    previous_structure = df.iloc[
        breakout_index - 5:breakout_index
    ]

    structure_high = previous_structure["high"].max()

    # Bullish break of structure.
    structure_break = (
        breakout["close"] > structure_high
        and breakout["close"] > breakout["open"]
    )

    if not structure_break:

        print("[15M] No bullish structure break")
        return None

    # Retest of broken structure.
    retest = (
        current["low"] <= structure_high * 1.002
        and current["close"] > structure_high
        and current["close"] > current["open"]
    )

    if not retest:

        print("[15M] No successful retest")
        return None

    # RSI confirmation.
    rsi_ok = (
        45 <= current["rsi"] <= 68
    )

    if not rsi_ok:

        print("[15M] RSI confirmation failed")
        return None

    # Breakout volume confirmation.
    volume_ok = (
        breakout["volume"] > breakout["volume_avg"]
    )

    if not volume_ok:

        print("[15M] Volume confirmation failed")
        return None

    if pd.isna(current["atr"]) or current["atr"] <= 0:
        print("[15M] ATR unavailable")
        return None

    return {
        "sweep_low": float(sweep_low),
        "structure": float(structure_high),
        "atr": float(current["atr"]),
        "rsi": float(current["rsi"]),
        "candle_time": int(current["time"])
    }


# ============================================================
# 5-MINUTE CONFIRMATION
# ============================================================

def five_minute_confirmation(df):

    if df is None or len(df) < 30:
        return False

    start = len(df) - 7

    for index in range(start, len(df)):

        if index < 20:
            continue

        if find_bullish_sweep(df, index) is not None:
            print("[5M] Bullish liquidity confirmation found")
            return True

    print("[5M] No liquidity confirmation")

    return False


# ============================================================
# POSITION SIZING
# ============================================================

def calculate_trade(entry, stop):

    risk_distance = entry - stop

    if risk_distance <= 0:
        return None

    # Quantity based on planned risk.
    risk_quantity = (
        RISK_PER_TRADE_CAD / risk_distance
    )

    # Limit the CAD value of the position.
    allocation_quantity = (
        MAX_NOTIONAL_CAD / entry
    )

    quantity = min(
        risk_quantity,
        allocation_quantity
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
# DUPLICATE SIGNAL CHECK
# ============================================================

def signal_already_logged(pair, candle_time):

    if not os.path.exists(CSV_FILE):
        return False

    try:

        with open(CSV_FILE, "r", newline="") as file:

            reader = csv.DictReader(file)

            for row in reader:

                if (
                    row.get("pair") == pair
                    and row.get("candle_time") == str(candle_time)
                ):
                    return True

    except Exception:
        return False

    return False


# ============================================================
# SAVE SIGNAL
# ============================================================

def save_signal(signal):

    fields = [
        "time",
        "pair",
        "candle_time",
        "entry",
        "stop",
        "target",
        "quantity",
        "notional",
        "planned_risk",
        "rsi",
        "status"
    ]

    exists = os.path.exists(CSV_FILE)

    with open(CSV_FILE, "a", newline="") as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fields
        )

        if not exists:
            writer.writeheader()

        writer.writerow(signal)


# ============================================================
# SCAN ONE MARKET
# ============================================================

def scan_market(pair_name, pair_info):

    print("\n======================================")
    print("SCANNING:", pair_name)
    print("======================================")

    try:

        pair = pair_info["altname"]

        # Download all four timeframes.
        candles_4h = get_candles(pair, 240)
        candles_1h = get_candles(pair, 60)
        candles_15m = get_candles(pair, 15)
        candles_5m = get_candles(pair, 5)

        if any(
            df is None
            for df in [
                candles_4h,
                candles_1h,
                candles_15m,
                candles_5m
            ]
        ):

            print("[SKIP] Candle data unavailable")
            return

        # 4H trend.
        trend_4h = check_trend(
            candles_4h,
            "4H"
        )

        # 1H trend.
        trend_1h = check_trend(
            candles_1h,
            "1H"
        )

        if not trend_4h:

            print("[NO ENTRY] 4H trend failed")
            return

        if not trend_1h:

            print("[NO ENTRY] 1H trend failed")
            return

        # 15M entry conditions.
        setup = bullish_structure_retest(
            candles_15m
        )

        if setup is None:
            print("[NO ENTRY] 15M setup failed")
            return

        # 5M liquidity confirmation.
        if not five_minute_confirmation(candles_5m):
            print("[NO ENTRY] 5M confirmation failed")
            return

        # Entry based on latest completed 15M candle.
        latest = candles_15m.iloc[-1]

        entry = float(latest["close"])

        # Stop below liquidity sweep.
        stop = (
            setup["sweep_low"]
            - setup["atr"] * 0.2
        )

        trade = calculate_trade(
            entry,
            stop
        )

        if trade is None:

            print("[SKIP] Invalid stop-loss")
            return

        # Respect Kraken's minimum volume and cost.
        if trade["quantity"] < pair_info["ordermin"]:

            print("[SKIP] Below Kraken minimum order volume")
            return

        if trade["notional"] < pair_info["costmin"]:

            print("[SKIP] Below Kraken minimum order cost")
            return

        # Avoid recording the same candle twice
        # when the CSV is available between runs.
        if signal_already_logged(
            pair_name,
            setup["candle_time"]
        ):

            print("[DUPLICATE] Signal already logged")
            return

        signal = {
            "time": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC",
                time.gmtime()
            ),

            "pair": pair_name,

            "candle_time": setup["candle_time"],

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

        print("\n========== GWVSA SIGNAL ==========")

        for key, value in signal.items():
            print(key, ":", value)

        print("==================================")

        if DRY_RUN:
            print("[DRY RUN] No real order was placed.")

    except Exception as error:

        print("[ERROR]", pair_name, str(error))


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    print("\n======================================")
    print("       GWVSA KRAKEN SCANNER")
    print("======================================")

    print("Mode:", "DRY RUN" if DRY_RUN else "LIVE")

    print("Risk per trade: $", RISK_PER_TRADE_CAD)

    print("Maximum allocation: $", MAX_NOTIONAL_CAD)

    print("Daily trade limit: NONE")

    print("Markets:", len(MARKETS))

    print("======================================")

    markets = discover_markets()

    if not markets:

        print("[ERROR] No CAD markets discovered")
        return

    for pair_name in MARKETS:

        pair_info = markets.get(pair_name)

        if pair_info is None:
            continue

        scan_market(
            pair_name,
            pair_info
        )

        # Small pause between market scans.
        time.sleep(1)

    print("\n======================================")
    print("GWVSA SCAN COMPLETED")
    print("======================================")


if __name__ == "__main__":
    main()
