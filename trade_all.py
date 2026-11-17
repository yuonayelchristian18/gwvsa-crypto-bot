
import csv
import json
import os
import time
import requests
import pandas as pd

# ==================================================
# GWVSA PAPER TRADING BOT
# No real orders are sent to Kraken.
# ==================================================

API_URL = "https://api.kraken.com"

MARKETS = [
    "XBT/CAD",
    "ETH/CAD",
    "SOL/CAD",
    "XRP/CAD",
    "ADA/CAD",
    "DOGE/CAD",
]

STARTING_BALANCE = 2000.00
RISK_PER_TRADE = 2.00
MAX_ALLOCATION = 15.00
REWARD_RISK = 2.0

FEE_RATE = 0.001       # 0.10% per side
SLIPPAGE_RATE = 0.0015 # 0.15% per side

EMA_FAST = 8
EMA_MEDIUM = 40
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_PERIOD = 20

MIN_CANDLES = 210
MAX_CANDLES = 720

STATE_FILE = "gwvsa_state.json"
TRADES_FILE = "gwvsa_trades.csv"

session = requests.Session()
session.headers.update({
    "User-Agent": "GWVSA-Paper-Trader/1.0"
})

TRADE_FIELDS = [
    "entry_time",
    "exit_time",
    "pair",
    "side",
    "quantity",
    "entry_price",
    "exit_price",
    "stop_price",
    "target_price",
    "entry_fee",
    "exit_fee",
    "gross_pnl",
    "net_pnl",
    "result",
    "exit_reason",
]


# ==================================================
# STATE
# ==================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "cash": STARTING_BALANCE,
            "open_positions": {},
            "last_signal_times": {},
            "last_processed_candles": {},
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "realized_pnl": 0.0,
        }

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        state = json.load(file)

    state.setdefault("cash", STARTING_BALANCE)
    state.setdefault("open_positions", {})
    state.setdefault("last_signal_times", {})
    state.setdefault("last_processed_candles", {})
    state.setdefault("total_trades", 0)
    state.setdefault("winning_trades", 0)
    state.setdefault("losing_trades", 0)
    state.setdefault("realized_pnl", 0.0)

    return state


def save_state(state):
    temp_file = STATE_FILE + ".tmp"

    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    os.replace(temp_file, STATE_FILE)


# ==================================================
# API
# ==================================================

def api_get(endpoint, params=None):
    for attempt in range(3):
        try:
            response = session.get(
                API_URL + endpoint,
                params=params,
                timeout=30
            )
            response.raise_for_status()
            result = response.json()

            if result.get("error"):
                raise RuntimeError(str(result["error"]))

            return result.get("result", {})

        except Exception as error:
            print(f"[API ERROR] {attempt + 1}/3: {error}")
            time.sleep(2)

    raise RuntimeError("Kraken API request failed")


def normalize_asset(asset):
    asset = str(asset).upper()

    aliases = {
        "XXBT": "BTC",
        "XBT": "BTC",
        "XETH": "ETH",
        "XXRP": "XRP",
        "XDG": "DOGE",
        "XXDG": "DOGE",
        "ZCAD": "CAD",
        "BTC": "BTC",
        "ETH": "ETH",
        "XRP": "XRP",
        "DOGE": "DOGE",
        "ADA": "ADA",
        "SOL": "SOL",
        "CAD": "CAD",
    }

    if asset in aliases:
        return aliases[asset]

    if len(asset) > 3 and asset[0] in ("X", "Z"):
        shortened = asset[1:]
        if shortened in aliases:
            return aliases[shortened]

    return asset


def discover_markets(pairs):
    found = {}

    print("\nKRAKEN MARKET DISCOVERY")

    for requested in MARKETS:
        base, quote = requested.split("/")
        base = normalize_asset(base)
        quote = normalize_asset(quote)

        for pair_id, info in pairs.items():
            actual_base = normalize_asset(info.get("base", ""))
            actual_quote = normalize_asset(info.get("quote", ""))

            if actual_base == base and actual_quote == quote:
                found[requested] = pair_id
                print(
                    f"[FOUND] {requested} -> {pair_id} "
                    f"({info.get('altname', '')})"
                )
                break

        if requested not in found:
            print(f"[NOT FOUND] {requested}")

    return found


# ==================================================
# MARKET DATA
# ==================================================

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
        raise ValueError("No candle data returned")

    rows = result[data_key]

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
        "open", "high", "low", "close", "volume"
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

    # Do not use Kraken's currently forming candle.
    df = df.iloc[:-1].copy()

    if len(df) < MIN_CANDLES:
        raise ValueError(
            f"Only {len(df)} completed candles available"
        )

    return df.tail(MAX_CANDLES).reset_index(drop=True)


# ==================================================
# INDICATORS
# ==================================================

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


def bullish_trend(df):
    last = df.iloc[-1]

    return bool(
        last["close"] > last["ema200"]
        and last["ema8"] > last["ema40"]
        and last["ema40"] > last["ema200"]
    )


# ==================================================
# GWVSA ENTRY CONDITIONS
# ==================================================

def find_bullish_sweep(df, lookback=10, recent=30):
    start = max(lookback, len(df) - recent - 1)

    for i in range(len(df) - 2, start - 1, -1):
        previous_low = df["low"].iloc[i-lookback:i].min()
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


def check_15m_entry(df):
    sweep = find_bullish_sweep(
        df,
        lookback=10,
        recent=30
    )

    if sweep is None:
        return None, "No bullish liquidity sweep"

    breakout = None

    for i in range(sweep["index"] + 1, len(df) - 1):
        start = max(0, i - 10)
        structure_level = df["high"].iloc[start:i].max()
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
    tolerance = float(last["atr"]) * 0.25

    retest = (
        last["low"] <= breakout["level"] + tolerance
        and last["close"] > breakout["level"]
        and last["close"] > last["open"]
    )

    if not retest:
        return None, "No bullish retest"

    if not 45 <= last["rsi"] <= 68:
        return None, "RSI outside entry range"

    if last["volume"] <= last["volume_avg"]:
        return None, "Volume confirmation failed"

    return {
        "entry": float(last["close"]),
        "sweep_low": sweep["low"],
        "atr": float(last["atr"]),
        "candle_time": int(last["time"])
    }, "GWVSA 15m entry passed"


def check_5m_confirmation(df):
    sweep = find_bullish_sweep(
        df,
        lookback=7,
        recent=7
    )

    if sweep is None:
        return False

    last = df.iloc[-1]

    return bool(last["close"] > last["open"])


# ==================================================
# PAPER POSITION SIZE
# ==================================================

def calculate_position(entry, stop, available_cash):
    if stop >= entry or entry <= 0:
        return None

    buy_price = entry * (1 + SLIPPAGE_RATE)
    stop_execution = stop * (1 - SLIPPAGE_RATE)

    # Include estimated entry and stop-exit fees in risk sizing.
    risk_per_unit = (
        buy_price
        - stop_execution
        + buy_price * FEE_RATE
        + stop_execution * FEE_RATE
    )

    if risk_per_unit <= 0:
        return None

    quantity_by_risk = RISK_PER_TRADE / risk_per_unit

    quantity_by_allocation = (
        MAX_ALLOCATION / buy_price
    )

    quantity_by_cash = (
        available_cash / (buy_price * (1 + FEE_RATE))
    )

    quantity = min(
        quantity_by_risk,
        quantity_by_allocation,
        quantity_by_cash
    )

    if quantity <= 0:
        return None

    notional = quantity * buy_price
    entry_fee = notional * FEE_RATE

    target = (
        entry
        + (entry - stop) * REWARD_RISK
    )

    return {
        "quantity": quantity,
        "entry_price": buy_price,
        "stop_price": stop,
        "target_price": target,
        "notional": notional,
        "entry_fee": entry_fee,
        "planned_risk": RISK_PER_TRADE
    }


# ==================================================
# TRADE HISTORY
# ==================================================

def save_trade(record):
    exists = os.path.exists(TRADES_FILE)

    with open(
        TRADES_FILE,
        "a",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=TRADE_FIELDS
        )

        if not exists or os.path.getsize(TRADES_FILE) == 0:
            writer.writeheader()

        writer.writerow(record)


# ==================================================
# OPEN PAPER POSITION
# ==================================================

def open_position(state, pair, entry_data):
    if pair in state["open_positions"]:
        return

    entry = entry_data["entry"]
    stop = entry_data["sweep_low"] - (
        0.2 * entry_data["atr"]
    )

    trade = calculate_position(
        entry,
        stop,
        state["cash"]
    )

    if trade is None:
        print("[SKIP] Position sizing failed")
        return

    total_cost = trade["notional"] + trade["entry_fee"]

    if total_cost > state["cash"]:
        print("[SKIP] Insufficient paper cash")
        return

    state["cash"] -= total_cost

    state["open_positions"][pair] = {
        "entry_time": int(time.time()),
        "entry_candle_time": entry_data["candle_time"],
        "entry_price": trade["entry_price"],
        "reference_entry": entry,
        "stop_price": trade["stop_price"],
        "target_price": trade["target_price"],
        "quantity": trade["quantity"],
        "notional": trade["notional"],
        "entry_fee": trade["entry_fee"],
        "last_processed_5m": entry_data["candle_time"]
    }

    state["last_signal_times"][pair] = entry_data["candle_time"]

    print("\n========== PAPER BUY ==========")
    print(f"Market: {pair}")
    print(f"Entry: {trade['entry_price']:.8f}")
    print(f"Stop: {trade['stop_price']:.8f}")
    print(f"Target: {trade['target_price']:.8f}")
    print(f"Quantity: {trade['quantity']:.8f}")
    print(f"Allocation: ${trade['notional']:.2f}")
    print(f"Entry fee: ${trade['entry_fee']:.4f}")
    print("Simulated only — no Kraken order sent")
    print("================================\n")


# ==================================================
# CLOSE PAPER POSITION
# ==================================================

def close_position(
    state,
    pair,
    position,
    reference_exit,
    reason,
    candle_time
):
    if reason == "STOP LOSS":
        exit_price = reference_exit * (1 - SLIPPAGE_RATE)
    else:
        exit_price = reference_exit * (1 - SLIPPAGE_RATE)

    quantity = position["quantity"]
    proceeds = quantity * exit_price
    exit_fee = proceeds * FEE_RATE

    gross_pnl = (
        quantity * (exit_price - position["entry_price"])
    )

    net_pnl = (
        proceeds
        - exit_fee
        - position["notional"]
        - position["entry_fee"]
    )

    state["cash"] += proceeds - exit_fee
    state["realized_pnl"] += net_pnl
    state["total_trades"] += 1

    if net_pnl > 0:
        state["winning_trades"] += 1
        result = "WIN"
    else:
        state["losing_trades"] += 1
        result = "LOSS"

    save_trade({
        "entry_time": position["entry_time"],
        "exit_time": int(time.time()),
        "pair": pair,
        "side": "LONG",
        "quantity": round(quantity, 10),
        "entry_price": round(position["entry_price"], 10),
        "exit_price": round(exit_price, 10),
        "stop_price": round(position["stop_price"], 10),
        "target_price": round(position["target_price"], 10),
        "entry_fee": round(position["entry_fee"], 6),
        "exit_fee": round(exit_fee, 6),
        "gross_pnl": round(gross_pnl, 6),
        "net_pnl": round(net_pnl, 6),
        "result": result,
        "exit_reason": reason
    })

    del state["open_positions"][pair]

    print("\n========== PAPER SELL ==========")
    print(f"Market: {pair}")
    print(f"Exit reason: {reason}")
    print(f"Exit price: {exit_price:.8f}")
    print(f"Net P/L: ${net_pnl:.4f} CAD")
    print(f"Result: {result}")
    print("================================\n")


# ==================================================
# MANAGE EXISTING POSITIONS
# ==================================================

def manage_position(state, pair, df_5m):
    position = state["open_positions"].get(pair)

    if not position:
        return

    last_processed = int(
        position.get("last_processed_5m", 0)
    )

    # Process every completed 5m candle after entry.
    candles = df_5m[
        df_5m["time"] > last_processed
    ]

    for _, candle in candles.iterrows():
        candle_time = int(candle["time"])

        low = float(candle["low"])
        high = float(candle["high"])

        stop_hit = low <= position["stop_price"]
        target_hit = high >= position["target_price"]

        # Conservative assumption if both levels hit
        # during the same candle: stop loss happens first.
        if stop_hit:
            close_position(
                state,
                pair,
                position,
                position["stop_price"],
                "STOP LOSS",
                candle_time
            )
            return

        if target_hit:
            close_position(
                state,
                pair,
                position,
                position["target_price"],
                "TAKE PROFIT",
                candle_time
            )
            return

        position["last_processed_5m"] = candle_time

    state["open_positions"][pair] = position


# ==================================================
# EQUITY AND PERFORMANCE
# ==================================================

def print_performance(state, prices):
    open_value = 0.0

    for pair, position in state["open_positions"].items():
        mark_price = prices.get(
            pair,
            position["entry_price"]
        )

        open_value += (
            position["quantity"] * mark_price
        )

    equity = state["cash"] + open_value

    unrealized_pnl = (
        equity
        - STARTING_BALANCE
        - state["realized_pnl"]
    )

    total_pnl = equity - STARTING_BALANCE

    trades = state["total_trades"]

    win_rate = (
        state["winning_trades"] / trades * 100
        if trades else 0
    )

    print("\n======================================")
    print("GWVSA PAPER PERFORMANCE")
    print("======================================")
    print(f"Starting balance: ${STARTING_BALANCE:.2f}")
    print(f"Available cash: ${state['cash']:.2f}")
    print(f"Open positions: {len(state['open_positions'])}")
    print(f"Open position value: ${open_value:.2f}")
    print(f"Account equity: ${equity:.2f}")
    print(f"Realized P/L: ${state['realized_pnl']:.2f}")
    print(f"Unrealized P/L: ${unrealized_pnl:.2f}")
    print(f"Total P/L: ${total_pnl:.2f}")
    print(f"Completed trades: {trades}")
    print(f"Wins: {state['winning_trades']}")
    print(f"Losses: {state['losing_trades']}")
    print(f"Win rate: {win_rate:.2f}%")
    print("======================================\n")


# ==================================================
# SCAN MARKET
# ==================================================

def scan_market(state, pair, pair_id, prices):
    print(f"\n========== SCANNING {pair} ==========")

    try:
        df_4h = add_indicators(get_ohlc(pair_id, 240))
        df_1h = add_indicators(get_ohlc(pair_id, 60))
        df_15m = add_indicators(get_ohlc(pair_id, 15))
        df_5m = add_indicators(get_ohlc(pair_id, 5))

    except Exception as error:
        print(f"[DATA ERROR] {pair}: {error}")
        return

    current_price = float(df_5m.iloc[-1]["close"])
    prices[pair] = current_price

    # Manage an existing position before considering a new entry.
    if pair in state["open_positions"]:
        manage_position(state, pair, df_5m)

        if pair in state["open_positions"]:
            print("[POSITION OPEN] Waiting for stop or target")
            return

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

    last_signal = state["last_signal_times"].get(pair)

    if last_signal == entry_data["candle_time"]:
        print("[DUPLICATE] This signal was already processed")
        return

    open_position(
        state,
        pair,
        entry_data
    )


# ==================================================
# MAIN
# ==================================================

def main():
    print("\n======================================")
    print("       GWVSA PAPER TRADING BOT")
    print("======================================")
    print("Mode: SIMULATED TRADING ONLY")
    print(f"Starting balance: ${STARTING_BALANCE:.2f} CAD")
    print(f"Planned risk: ${RISK_PER_TRADE:.2f} CAD")
    print(f"Maximum allocation: ${MAX_ALLOCATION:.2f} CAD")
    print(f"Reward/risk: 1:{REWARD_RISK}")
    print(f"Fee per side: {FEE_RATE * 100:.2f}%")
    print(f"Slippage per side: {SLIPPAGE_RATE * 100:.2f}%")
    print("Daily trade limit: NONE")
    print("======================================")

    state = load_state()
    prices = {}

    try:
        pairs = api_get("/0/public/AssetPairs")
        markets = discover_markets(pairs)

        for pair, pair_id in markets.items():
            scan_market(
                state,
                pair,
                pair_id,
                prices
            )
            save_state(state)
            time.sleep(1)

    except Exception as error:
        print(f"[BOT ERROR] {error}")
        save_state(state)
        raise

    print_performance(state, prices)
    save_state(state)

    print("GWVSA PAPER TRADING RUN COMPLETED")


if __name__ == "__main__":
    main()
