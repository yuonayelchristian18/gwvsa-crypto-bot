
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# GWVSA PAPER TRADING BOT
# Kraken public market data only - NO REAL ORDERS
# ============================================================

API = "https://api.kraken.com/0/public"

STARTING_BALANCE = 2000.00
RISK_PER_TRADE = 2.00
MAX_POSITION_VALUE = 15.00
FEE_RATE = 0.001       # 0.10% per side
SLIPPAGE = 0.0015      # 0.15% per side
REWARD_RISK = 2.0

MARKETS = [
    "XBT/CAD",
    "ETH/CAD",
    "SOL/CAD",
    "XRP/CAD",
    "ADA/CAD",
    "DOGE/CAD",
]

STATE_FILE = "gwvsa_state.json"
TRADE_FILE = "gwvsa_trades.csv"

TRADE_HEADERS = [
    "time", "market", "side", "quantity",
    "entry_price", "exit_price", "stop_loss",
    "take_profit", "net_pnl", "result"
]


def api_get(endpoint, params=None):
    params = params or {}
    url = API + "/" + endpoint + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url, headers={"User-Agent": "GWVSA-PaperBot/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.loads(response.read().decode())

    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))

    return data["result"]


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
    else:
        state = {
            "cash": STARTING_BALANCE,
            "positions": {},
            "last_signal": {},
            "last_processed_5m": {},
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "realized_pnl": 0.0,
        }

    state.setdefault("positions", {})
    state.setdefault("last_signal", {})
    state.setdefault("last_processed_5m", {})
    return state


def save_state(state):
    temp_file = STATE_FILE + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, STATE_FILE)


def ensure_trade_file():
    if not os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "w", newline="") as f:
            csv.writer(f).writerow(TRADE_HEADERS)


def log_trade(row):
    ensure_trade_file()
    with open(TRADE_FILE, "a", newline="") as f:
        csv.writer(f).writerow(row)


def get_market_pairs():
    result = api_get("AssetPairs")
    found = {}

    for requested in MARKETS:
        for key, info in result.items():
            wsname = info.get("wsname", "")
            altname = info.get("altname", "")
            if wsname.upper() == requested.upper():
                found[requested] = altname
                break

    return found


def get_candles(pair, interval):
    result = api_get("OHLC", {
        "pair": pair,
        "interval": interval
    })

    keys = [k for k in result if k != "last"]
    if not keys:
        return []

    rows = result[keys[0]]

    # Kraken includes a currently forming candle at the end.
    # Exclude it and use completed candles only.
    completed = rows[:-1]

    candles = []
    for row in completed:
        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })

    return candles


def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2 / (period + 1)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes[-period:]]
    losses = [max(-x, 0) for x in changes[-period:]]

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []
    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))

    return sum(ranges[-period:]) / period


def bullish_trend(candles):
    if len(candles) < 210:
        return False

    closes = [c["close"] for c in candles]

    e8 = ema(closes, 8)
    e40 = ema(closes, 40)
    e200 = ema(closes, 200)

    return (
        e8 is not None
        and e40 is not None
        and e200 is not None
        and closes[-1] > e200
        and e8 > e40 > e200
    )


def check_gwvsa_entry(candles15, candles5):
    """
    Simplified GWVSA setup:
    - Recent liquidity sweep below a prior swing low.
    - Recovery above that swing low.
    - Structure break and retest.
    - Bullish 5-minute confirmation.
    """

    if len(candles15) < 30 or len(candles5) < 25:
        return None

    closes = [c["close"] for c in candles15]
    current = candles15[-1]

    current_rsi = rsi(closes)
    current_atr = atr(candles15)

    if current_rsi is None or current_atr is None:
        return None

    if not 35 <= current_rsi <= 70:
        return None

    # Look for a sweep in the previous eight completed candles.
    sweep_index = None
    swing_low = None

    for i in range(len(candles15) - 9, len(candles15) - 1):
        previous = candles15[max(0, i - 10):i]
        if len(previous) < 5:
            continue

        level = min(c["low"] for c in previous)

        if (
            candles15[i]["low"] < level
            and candles15[i]["close"] > level
        ):
            sweep_index = i
            swing_low = level
            break

    if sweep_index is None:
        return None

    later = candles15[sweep_index + 1:-1]
    if not later:
        return None

    breakout_level = max(c["high"] for c in later)

    # Current 15-minute candle must break and retest structure.
    if current["close"] <= breakout_level:
        return None

    if current["low"] > breakout_level * 1.003:
        return None

    # Confirm bullish momentum on the latest completed 5m candle.
    five = candles5[-1]
    average_volume = sum(
        c["volume"] for c in candles5[-21:-1]
    ) / 20

    if five["close"] <= five["open"]:
        return None

    if five["volume"] < average_volume:
        return None

    stop = min(current["low"], swing_low) - (0.25 * current_atr)

    estimated_entry = current["close"] * (1 + SLIPPAGE)

    if stop >= estimated_entry:
        return None

    return {
        "candle_time": current["time"],
        "entry_reference": current["close"],
        "stop": stop,
        "atr": current_atr,
        "rsi": current_rsi,
    }


def open_position(state, market, signal, latest_5m_time):
    if market in state["positions"]:
        return

    reference = signal["entry_reference"]
    buy_price = reference * (1 + SLIPPAGE)
    stop = signal["stop"]

    risk_per_unit = buy_price - stop
    if risk_per_unit <= 0:
        return

    quantity_by_risk = RISK_PER_TRADE / risk_per_unit
    quantity_by_allocation = MAX_POSITION_VALUE / buy_price

    affordable_quantity = state["cash"] / (
        buy_price * (1 + FEE_RATE)
    )

    quantity = min(
        quantity_by_risk,
        quantity_by_allocation,
        affordable_quantity,
    )

    if quantity <= 0:
        return

    notional = quantity * buy_price
    entry_fee = notional * FEE_RATE
    total_cost = notional + entry_fee

    if total_cost > state["cash"]:
        return

    target = buy_price + (risk_per_unit * REWARD_RISK)

    state["cash"] -= total_cost

    state["positions"][market] = {
        "quantity": quantity,
        "entry": buy_price,
        "stop": stop,
        "target": target,
        "entry_cost": total_cost,
        "entry_time": int(time.time()),
        "signal_time": signal["candle_time"],
    }

    state["last_signal"][market] = signal["candle_time"]
    state["last_processed_5m"][market] = latest_5m_time

    print(
        f"PAPER BUY {market} | "
        f"quantity={quantity:.8f} | "
        f"entry={buy_price:.6f} | "
        f"stop={stop:.6f} | target={target:.6f}"
    )


def close_position(state, market, exit_reference, reason):
    position = state["positions"][market]

    # Adverse slippage on simulated sell.
    sell_price = exit_reference * (1 - SLIPPAGE)
    proceeds = position["quantity"] * sell_price
    exit_fee = proceeds * FEE_RATE

    state["cash"] += proceeds - exit_fee

    net_pnl = (
        proceeds - exit_fee - position["entry_cost"]
    )

    state["realized_pnl"] += net_pnl
    state["total_trades"] += 1

    if net_pnl >= 0:
        state["winning_trades"] += 1
    else:
        state["losing_trades"] += 1

    log_trade([
        datetime.now(timezone.utc).isoformat(),
        market,
        "SELL",
        round(position["quantity"], 8),
        round(position["entry"], 8),
        round(sell_price, 8),
        round(position["stop"], 8),
        round(position["target"], 8),
        round(net_pnl, 4),
        reason,
    ])

    print(
        f"PAPER SELL {market} | {reason} | "
        f"net P/L: ${net_pnl:.2f} CAD"
    )

    del state["positions"][market]


def manage_position(state, market, candles5):
    if market not in state["positions"]:
        return

    if not candles5:
        return

    position = state["positions"][market]
    last_time = state["last_processed_5m"].get(market, 0)

    new_candles = [
        c for c in candles5 if c["time"] > last_time
    ]

    if not new_candles:
        return

    oldest_available = candles5[0]["time"]

    # Do not silently ignore a gap in historical stop/target data.
    if last_time and last_time < oldest_available - 300:
        print(
            f"WARNING: {market} has a 5m data gap. "
            "Position kept open; review it manually."
        )
        return

    for candle in new_candles:
        stop_hit = candle["low"] <= position["stop"]
        target_hit = candle["high"] >= position["target"]

        # Conservative assumption if both levels hit in one candle.
        if stop_hit:
            close_position(
                state, market, position["stop"], "STOP_LOSS"
            )
            return

        if target_hit:
            close_position(
                state, market, position["target"], "TAKE_PROFIT"
            )
            return

        state["last_processed_5m"][market] = candle["time"]


def print_status(state):
    open_positions = state["positions"]

    print("\n========== GWVSA PAPER ACCOUNT ==========")
    print(f"Starting balance: ${STARTING_BALANCE:.2f} CAD")
    print(f"Available cash:   ${state['cash']:.2f} CAD")
    print(f"Open positions:   {len(open_positions)}")
    print(f"Closed trades:    {state['total_trades']}")
    print(f"Wins:             {state['winning_trades']}")
    print(f"Losses:           {state['losing_trades']}")
    print(f"Realized P/L:     ${state['realized_pnl']:.2f} CAD")

    open_value = sum(
        p["quantity"] * p["entry"]
        for p in open_positions.values()
    )

    equity = state["cash"] + open_value
    print(f"Estimated equity: ${equity:.2f} CAD")
    print("=========================================\n")


def main():
    print("Starting GWVSA PAPER TRADING BOT")
    print("Paper trading only. No real orders.")

    ensure_trade_file()
    state = load_state()

    try:
        pairs = get_market_pairs()
    except Exception as error:
        print("Could not load Kraken markets:", error)
        return

    for market in MARKETS:
        pair = pairs.get(market)

        if not pair:
            print(f"Market not found on Kraken: {market}")
            continue

        try:
            candles5 = get_candles(pair, 5)
            candles15 = get_candles(pair, 15)
            candles60 = get_candles(pair, 60)
            candles240 = get_candles(pair, 240)

            if not candles5 or not candles15:
                print(f"{market}: insufficient candle data")
                continue

            # Manage existing simulated position first.
            manage_position(state, market, candles5)

            if market in state["positions"]:
                continue

            if len(candles5) < 25:
                continue

            # Confirm higher-timeframe trend.
            if not bullish_trend(candles60):
                print(f"{market}: 1H trend filter rejected")
                continue

            if not bullish_trend(candles240):
                print(f"{market}: 4H trend filter rejected")
                continue

            signal = check_gwvsa_entry(candles15, candles5)

            if not signal:
                print(f"{market}: no valid GWVSA entry")
                continue

            if state["last_signal"].get(market) == signal["candle_time"]:
                continue

            open_position(
                state,
                market,
                signal,
                candles5[-1]["time"],
            )

        except Exception as error:
            print(f"{market}: ERROR: {error}")

    save_state(state)
    print_status(state)
    print("GWVSA run completed.")


if __name__ == "__main__":
    main()
