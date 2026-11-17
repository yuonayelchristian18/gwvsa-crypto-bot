
import csv
import os
import time
import requests
import pandas as pd

# ==========================================
# GWVSA KRAKEN PAPER SCANNER
# ==========================================

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
ATR_PERIOD = 14
VOLUME_PERIOD = 20

MIN_CANDLES = 210
MAX_CANDLES = 720

CSV_FILE = "gwvsa_signals.csv"

session = requests.Session()
session.headers.update({
    "User-Agent": "GWVSA-Kraken-Scanner/1.0"
})


# ==========================================
# KRAKEN API
# ==========================================

def api_get(endpoint, params=None):
    url = API_URL + endpoint

    for attempt in range(3):
        try:
            response = session.get(
                url,
                params=params,
                timeout=30
            )
            response.raise_for_status()

            data = response.json()

            if data.get("error"):
                raise RuntimeError(
                    "Kraken API error: " + str(data["error"])
                )

            return data.get("result", {})

        except Exception as error:
            print(
                f"[API ERROR] Attempt {attempt + 1}: {error}"
            )
            time.sleep(2)

    raise RuntimeError(
        f"Could not retrieve Kraken endpoint: {endpoint}"
    )


# ==========================================
# ASSET NAME NORMALIZATION
# ==========================================

def normalize_asset(asset):
    asset = str(asset).upper()

    aliases = {
        "XXBT": "BTC",
        "XBT": "BTC",
        "XETH": "ETH",
        "XXRP": "XRP",
        "XRP": "XRP",
        "XDG": "DOGE",
        "XXDG": "DOGE",
        "ZCAD": "CAD",
        "CAD": "CAD",
        "ADA": "ADA",
        "SOL": "SOL",
    }

    if asset in aliases:
        return aliases[asset]

    # Kraken sometimes prefixes asset codes with X or Z.
    if len(asset) > 3 and asset[0] in ("X", "Z"):
        shortened = asset[1:]
        if shortened in aliases:
            return aliases[shortened]

    return asset


# ==========================================
# ADA MARKET DIAGNOSTIC
# ==========================================

def check_ada_markets(pairs):
    print("\n======================================")
    print("ADA MARKET CHECK")
    print("======================================")

    found = False

    for pair_id, info in pairs.items():
        wsname = info.get("wsname", "")
        altname = info.get("altname", "")

        base = normalize_asset(info.get("base", ""))
        quote = normalize_asset(info.get("quote", ""))

        if (
            base == "ADA"
            or "ADA" in wsname.upper()
            or "ADA" in altname.upper()
        ):
            found = True

            print(
                f"Pair ID: {pair_id} | "
                f"WebSocket name: {wsname} | "
                f"Alt name: {altname} | "
                f"Base: {base} | Quote: {quote}"
            )

    if not found:
        print("No ADA markets found in Kraken AssetPairs.")

    print("======================================\n")


# ==========================================
# MARKET DISCOVERY
# ==========================================

def discover_markets(pairs):
    discovered = {}

    print("\nDISCOVERED MARKETS:")

    for requested in MARKETS:
        requested_base, requested_quote = requested.split("/")

        target_base = normalize_asset(requested_base)
        target_quote = normalize_asset(requested_quote)

        matched = None

        for pair_id, info in pairs.items():
            base = normalize_asset(info.get("base", ""))
            quote = normalize_asset(info.get("quote", ""))

            if base == target_base and quote == target_quote:
                matched = pair_id
                break

        if matched:
            info = pairs[matched]

            discovered[requested] = matched

            print(
                f"{requested} -> {matched} | "
                f"Kraken name: {info.get('altname', '')}"
            )
        else:
            print(f"[PAIR NOT FOUND] {requested}")

    return discovered


# ==========================================
# OHLC DATA
# ==========================================

def get_ohlc(pair, interval):
    result = api_get(
        "/0/public/OHLC",
        {
            "pair": pair,
            "interval": interval
        }
    )

    data_key = next(
        (key for key in result if key != "last"),
        None
    )

    if not data_key:
        raise ValueError(f"No OHLC data returned for {pair}")

    rows = result[data_key]

    if len(rows) < MIN_CANDLES:
        raise ValueError(
            f"Not enough candles for {pair}, "
            f"interval {interval}: {len(rows)}"
        )

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

    for column in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[column] = pd.to_numeric(
            df[column],
            errors="coerce"
        )

    df["time"] = pd.to_numeric(
        df["time"],
        errors="coerce"
    )

    df = df.dropna().reset_index(drop=True)

    # Kraken's last OHLC candle is still forming.
    # Exclude it so signals use completed candles only.
    df = df.iloc[:-1].copy()

    if len(df) < MIN_CANDLES:
        raise ValueError(
            f"Not enough completed candles for {pair}"
        )

    return df.tail(MAX_CANDLES).reset_index(drop=True)


# ==========================================
# TECHNICAL INDICATORS
# ==========================================

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

    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)

    avg_gain = gains.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD
    ).mean()

    avg_loss = losses.ewm(
        alpha=1 / RSI_PERIOD,
        adjust=False,
        min_periods=RSI_PERIOD
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

    df["atr"] = true_range.rolling(
        ATR_PERIOD
    ).mean()

    df["volume_avg"] = df["volume"].rolling(
        VOLUME_PERIOD
    ).mean()

    return df.dropna().reset_index(drop=True)


# ==========================================
# TREND CONFIRMATION
# ==========================================

def bullish_trend(df):
    last = df.iloc[-1]

    return bool(
        last["close"] > last["ema200"]
        and last["ema8"] > last["ema40"]
        and last["ema40"] > last["ema200"]
    )


def print_trend(label, df):
    last = df.iloc[-1]

    print(
        f"{label} Close: {last['close']:.8f} "
        f"EMA8: {last['ema8']:.8f} "
        f"EMA40: {last['ema40']:.8f} "
        f"EMA200: {last['ema200']:.8f} "
        f"RSI: {last['rsi']:.2f} "
        f"Bullish: {bullish_trend(df)}"
    )


# ==========================================
# BULLISH LIQUIDITY SWEEP
# ==========================================

def find_bullish_sweep(df, lookback=10, recent=20):
    """
    Finds a candle that takes liquidity below
    previous lows and closes back above that level.
    """

    start = max(lookback, len(df) - recent - 1)

    for i in range(len(df) - 2, start - 1, -1):
        previous_low = df["low"].iloc[
            i - lookback:i
        ].min()

        candle = df.iloc[i]

        if (
            candle["low"] < previous_low
            and candle["close"] > previous_low
            and candle["close"] > candle["open"]
        ):
            return {
                "index": i,
                "low": float(candle["low"]),
                "level": float(previous_low)
            }

    return None


# ==========================================
# 15-MINUTE STRUCTURE BREAK + RETEST
# ==========================================

def check_15m_entry(df):
    """
    Approximation of the GWVSA sequence:
    liquidity sweep -> structure break -> retest.
    """

    sweep = find_bullish_sweep(
        df,
        lookback=10,
        recent=30
    )

    if sweep is None:
        return None, "No bullish liquidity sweep"

    sweep_index = sweep["index"]

    # Look for a structure break after the sweep.
    breakout = None

    for i in range(sweep_index + 1, len(df) - 1):
        prior_start = max(0, i - 10)

        structure_level = df["high"].iloc[
            prior_start:i
        ].max()

        candle = df.iloc[i]

        if (
            candle["close"] > structure_level
            and candle["close"] > candle["open"]
        ):
            breakout = {
                "index": i,
                "level": float(structure_level)
            }
            break

    if breakout is None:
        return None, "No bullish structure break"

    last = df.iloc[-1]

    breakout_level = breakout["level"]

    tolerance = float(last["atr"]) * 0.25

    # Price must retest the breakout level and close above it.
    retest = (
        last["low"] <= breakout_level + tolerance
        and last["close"] > breakout_level
        and last["close"] > last["open"]
    )

    if not retest:
        return None, "No bullish retest"

    if not (45 <= last["rsi"] <= 68):
        return None, "15m RSI outside 45-68 range"

    if last["volume"] <= last["volume_avg"]:
        return None, "Breakout volume not above average"

    return {
        "sweep_low": sweep["low"],
        "entry": float(last["close"]),
        "atr": float(last["atr"]),
        "candle_time": int(last["time"])
    }, "15m entry confirmed"


# ==========================================
# 5-MINUTE CONFIRMATION
# ==========================================

def check_5m_confirmation(df):
    sweep = find_bullish_sweep(
        df,
        lookback=7,
        recent=7
    )

    if sweep is None:
        return False

    last = df.iloc[-1]

    return bool(
        last["close"] > last["open"]
    )


# ==========================================
# POSITION SIZE AND TARGET
# ==========================================

def calculate_trade(entry, stop):
    risk_distance = entry - stop

    if risk_distance <= 0:
        return None

    quantity_by_risk = (
        RISK_PER_TRADE_CAD / risk_distance
    )

    quantity_by_allocation = (
        MAX_NOTIONAL_CAD / entry
    )

    quantity = min(
        quantity_by_risk,
        quantity_by_allocation
    )

    if quantity <= 0:
        return None

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


# ==========================================
# DUPLICATE CHECK
# ==========================================

def signal_already_logged(pair, candle_time):
    if not os.path.exists(CSV_FILE):
        return False

    with open(
        CSV_FILE,
        "r",
        newline="",
        encoding="utf-8"
    ) as file:

        reader = csv.DictReader(file)

        for row in reader:
            if (
                row.get("pair") == pair
                and row.get("candle_time") == str(candle_time)
            ):
                return True

    return False


# ==========================================
# SAVE SIGNAL
# ==========================================

def save_signal(signal):
    file_exists = os.path.exists(CSV_FILE)

    fieldnames = [
        "timestamp",
        "pair",
        "candle_time",
        "entry",
        "stop",
        "target",
        "quantity",
        "notional",
        "planned_risk",
        "reward_risk"
    ]

    with open(
        CSV_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames
        )

        if not file_exists:
            writer.writeheader()

        writer.writerow(signal)


# ==========================================
# SCAN ONE MARKET
# ==========================================

def scan_market(requested_pair, kraken_pair):
    print("\n======================================")
    print(f"SCANNING: {requested_pair}")
    print("======================================")

    try:
        df_4h = add_indicators(
            get_ohlc(kraken_pair, 240)
        )

        df_1h = add_indicators(
            get_ohlc(kraken_pair, 60)
        )

        df_15m = add_indicators(
            get_ohlc(kraken_pair, 15)
        )

        df_5m = add_indicators(
            get_ohlc(kraken_pair, 5)
        )

    except Exception as error:
        print(f"[DATA ERROR] {requested_pair}: {error}")
        return

    print_trend("4H", df_4h)
    print_trend("1H", df_1h)

    if not bullish_trend(df_4h):
        print("[NO ENTRY] 4H trend failed")
        return

    if not bullish_trend(df_1h):
        print("[NO ENTRY] 1H trend failed")
        return

    entry_data, reason = check_15m_entry(df_15m)

    if entry_data is None:
        print(f"[NO ENTRY] {reason}")
        return

    print(f"[15M] {reason}")

    if not check_5m_confirmation(df_5m):
        print("[NO ENTRY] 5m confirmation failed")
        return

    print("[5M] Bullish confirmation passed")

    entry = entry_data["entry"]

    stop = (
        entry_data["sweep_low"]
        - 0.2 * entry_data["atr"]
    )

    trade = calculate_trade(entry, stop)

    if trade is None:
        print("[NO ENTRY] Invalid stop or position size")
        return

    candle_time = entry_data["candle_time"]

    if signal_already_logged(
        requested_pair,
        candle_time
    ):
        print("[DUPLICATE] Signal already logged")
        return

    signal = {
        "timestamp": int(time.time()),
        "pair": requested_pair,
        "candle_time": candle_time,
        "entry": round(entry, 8),
        "stop": round(trade["stop"], 8),
        "target": round(trade["target"], 8),
        "quantity": round(trade["quantity"], 8),
        "notional": round(trade["notional"], 4),
        "planned_risk": round(trade["planned_risk"], 4),
        "reward_risk": REWARD_RISK_RATIO
    }

    save_signal(signal)

    print("\n<==== GWVSA PAPER SIGNAL ====>")
    print(f"Market: {requested_pair}")
    print(f"Entry: {entry:.8f}")
    print(f"Stop: {trade['stop']:.8f}")
    print(f"Target: {trade['target']:.8f}")
    print(f"Quantity: {trade['quantity']:.8f}")
    print(f"Allocation: ${trade['notional']:.2f} CAD")
    print(f"Planned risk: ${trade['planned_risk']:.2f} CAD")
    print(f"Reward-to-risk: 1:{REWARD_RISK_RATIO}")
    print("<============================>\n")


# ==========================================
# MAIN
# ==========================================

def main():
    print("\n======================================")
    print("       GWVSA KRAKEN SCANNER")
    print("======================================")

    print("Mode: PAPER SCAN ONLY")
    print(f"Risk per trade: ${RISK_PER_TRADE_CAD}")
    print(f"Maximum allocation: ${MAX_NOTIONAL_CAD}")
    print("Daily trade limit: NONE")
    print(f"Markets requested: {len(MARKETS)}")

    try:
        pairs = api_get("/0/public/AssetPairs")

        # Inspect ADA listings before discovering markets.
        check_ada_markets(pairs)

        markets = discover_markets(pairs)

        if not markets:
            print("[ERROR] No requested markets were found.")
            return

        for requested_pair, kraken_pair in markets.items():
            scan_market(
                requested_pair,
                kraken_pair
            )

            # Small pause between market scans.
            time.sleep(1)

    except Exception as error:
        print(f"[SCANNER ERROR] {error}")
        raise

    print("\n======================================")
    print("       GWVSA SCAN COMPLETED")
    print("======================================")


if __name__ == "__main__":
    main()
