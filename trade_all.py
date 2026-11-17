
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# GWVSA PAPER TRADING BOT
# Kraken public market data only. NO REAL ORDERS.
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
    "time_utc",
    "market",
    "side",
    "quantity",
    "entry_price",
    "exit_price",
    "stop_loss",
    "take_profit",
    "net_pnl_cad",
    "result",
]


# ============================================================
# KRAKEN DATA
# ============================================================

def api_get(endpoint, params=None):
    params = params or {}
    query = urllib.parse.urlencode(params)
    url = f"{API}/{endpoint}?{query}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "GWVSA-PaperTrader/2.0"},
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))

    return data["result"]


def normalize_pair(value):
    """Normalize pair labels for comparison."""
    return (
        str(value)
        .upper()
        .replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def discover_markets():
    """
    Discover actual Kraken pair identifiers using AssetPairs.
    Match the displayed wsname first, then base/quote assets.
    """
    result = api_get("AssetPairs")
    found = {}

    for requested in MARKETS:
        wanted = normalize_pair(requested)

        for pair_key, info in result.items():
            wsname = info.get("wsname", "")
            altname = info.get("altname", pair_key)

            if wsname and normalize_pair(wsname) == wanted:
                found[requested] = altname
                break

    return found, result


def get_candles(pair, interval):
    result = api_get(
        "OHLC",
        {"pair": pair, "interval": interval},
    )

    candle_key = next(
        (key for key in result if key != "last"),
        None,
    )

    if candle_key is None:
        return []

    rows = result[candle_key]

    # Kraken includes the currently forming candle.
    # Use completed candles only.
    rows = rows[:-1]

    candles = []

    for row in rows:
        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })

    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)
    result = sum(values[:period]) / period

    for price in values[period:]:
        result = (
            (price - result) * multiplier
            + result
        )

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]

    gains = [max(change, 0) for change in recent]
    losses = [max(-change, 0) for change in recent]

    average_gain = sum(gains) / period
    average_loss = sum(losses) / period

    if average_loss == 0:
        return 100.0

    rs = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + rs))


def atr(candles, period=14):
    if len(candles) < period + 1:
        return None

    ranges = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1]["close"]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        )

        ranges.append(true_range)

    return sum(ranges[-period:]) / period


def trend_report(candles):
    if len(candles) < 210:
        return {
            "valid": False,
            "reason": f"Need 210 candles; received {len(candles)}",
        }

    closes = [c["close"] for c in candles]

    e8 = ema(closes, 8)
    e40 = ema(closes, 40)
    e200 = ema(closes, 200)
    price = closes[-1]

    bullish = (
        e8 is not None
        and e40 is not None
        and e200 is not None
        and price > e200
        and e8 > e40 > e200
    )

    return {
        "valid": True,
        "bullish": bullish,
        "price": price,
        "ema8": e8,
        "ema40": e40,
        "ema200": e200,
        "reason": (
            "Bullish: price above EMA200 and EMA8 > EMA40 > EMA200"
            if bullish
            else "Rejected: bullish EMA alignment not satisfied"
        ),
    }


def print_trend(market, timeframe, report):
    print(f"\n{market} - {timeframe} TREND")

    if not report["valid"]:
        print("  ", report["reason"])
        return

    print(f"  Price:  {report['price']:.8f}")
    print(f"  EMA 8:  {report['ema8']:.8f}")
    print(f"  EMA 40: {report['ema40']:.8f}")
    print(f"  EMA 200:{report['ema200']:.8f}")
    print(f"  Result: {report['reason']}")


# ============================================================
# PAPER ACCOUNT FILES
# ============================================================

def ensure_trade_file():
    if not os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "w", newline="") as file:
            writer = csv.writer(file)
            writer.writerow(TRADE_HEADERS)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as file:
            state = json.load(file)
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

    state.setdefault("cash", STARTING_BALANCE)
    state.setdefault("positions", {})
    state.setdefault("last_signal", {})
    state.setdefault("last_processed_5m", {})
    state.setdefault("total_trades", 0)
    state.setdefault("winning_trades", 0)
    state.setdefault("losing_trades", 0)
    state.setdefault("realized_pnl", 0.0)

    return state


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w") as file:
        json.dump(state, file, indent=2)

    os.replace(temporary, STATE_FILE)


def log_trade(row):
    ensure_trade_file()

    with open(TRADE_FILE, "a", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(row)


# ============================================================
# GWVSA ENTRY CONDITIONS
# ============================================================

def check_gwvsa_entry(candles15, candles5):
    """
    Simplified GWVSA entry:
    1. Recent liquidity sweep below a prior low.
    2. Recovery above the swept level.
    3. Break of structure.
    4. Retest of the breakout level.
    5. Bullish 5-minute candle with volume confirmation.

    This is an approximation, not a full SMT or double-bottom detector.
    """

    if len(candles15) < 30:
        return None, "Not enough 15m candles"

    if len(candles5) < 25:
        return None, "Not enough 5m candles"

    closes = [c["close"] for c in candles15]
    current = candles15[-1]

    current_rsi = rsi(closes)
    current_atr = atr(candles15)

    if current_rsi is None or current_atr is None:
        return None, "RSI or ATR unavailable"

    if not 35 <= current_rsi <= 70:
        return None, f"RSI filter rejected: {current_rsi:.2f}"

    # Find a recent sweep in the previous eight completed candles.
    sweep_index = None
    swing_low = None

    start = max(10, len(candles15) - 9)
    end = len(candles15) - 1

    for i in range(start, end):
        earlier = candles15[i - 10:i]

        if len(earlier) < 5:
            continue

        prior_low = min(c["low"] for c in earlier)

        if (
            candles15[i]["low"] < prior_low
            and candles15[i]["close"] > prior_low
        ):
            sweep_index = i
            swing_low = prior_low

    if sweep_index is None:
        return None, "No recent liquidity sweep"

    after_sweep = candles15[sweep_index + 1:-1]

    if not after_sweep:
        return None, "No candles after liquidity sweep"

    breakout_level = max(
        candle["high"] for candle in after_sweep
    )

    if current["close"] <= breakout_level:
        return None, "No 15m structure break"

    if current["low"] > breakout_level * 1.003:
        return None, "No 15m retest"

    # Latest completed 5m candle confirmation.
    five = candles5[-1]

    previous_volumes = [
        c["volume"] for c in candles5[-21:-1]
    ]

    if len(previous_volumes) < 20:
        return None, "Not enough volume history"

    average_volume = sum(previous_volumes) / len(previous_volumes)

    if five["close"] <= five["open"]:
        return None, "5m candle is not bullish"

    if five["volume"] < average_volume:
        return None, "5m volume below average"

    stop = min(current["low"], swing_low) - (
        0.25 * current_atr
    )

    reference_entry = current["close"]
    estimated_buy = reference_entry * (1 + SLIPPAGE)

    if stop >= estimated_buy:
        return None, "Stop price is not below entry"

    return {
        "candle_time": current["time"],
        "entry_reference": reference_entry,
        "stop": stop,
        "atr": current_atr,
        "rsi": current_rsi,
    }, "Valid GWVSA entry"


# ============================================================
# PAPER BUY AND SELL
# ============================================================

def open_position(state, market, signal, latest_5m_time):
    if market in state["positions"]:
        print(f"{market}: already has an open paper position")
        return

    buy_price = signal["entry_reference"] * (1 + SLIPPAGE)
    stop = signal["stop"]

    risk_per_unit = buy_price - stop

    if risk_per_unit <= 0:
        print(f"{market}: invalid position risk")
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
        print(f"{market}: insufficient paper cash")
        return

    notional = quantity * buy_price
    entry_fee = notional * FEE_RATE
    total_cost = notional + entry_fee

    if total_cost > state["cash"]:
        print(f"{market}: insufficient paper cash after fee")
        return

    target = buy_price + (
        risk_per_unit * REWARD_RISK
    )

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

    print("\n========== PAPER BUY ==========")
    print(f"Market:       {market}")
    print(f"Quantity:     {quantity:.8f}")
    print(f"Buy price:    {buy_price:.8f}")
    print(f"Stop loss:    {stop:.8f}")
    print(f"Take profit:  {target:.8f}")
    print(f"Entry fee:    ${entry_fee:.4f} CAD")
    print(f"Position cost:${total_cost:.4f} CAD")
    print("================================")


def close_position(state, market, exit_reference, reason):
    position = state["positions"][market]

    sell_price = exit_reference * (1 - SLIPPAGE)
    proceeds = position["quantity"] * sell_price
    exit_fee = proceeds * FEE_RATE

    state["cash"] += proceeds - exit_fee

    net_pnl = (
        proceeds
        - exit_fee
        - position["entry_cost"]
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

    print("\n========== PAPER SELL ==========")
    print(f"Market:       {market}")
    print(f"Reason:       {reason}")
    print(f"Sell price:   {sell_price:.8f}")
    print(f"Exit fee:     ${exit_fee:.4f} CAD")
    print(f"NET P/L:      ${net_pnl:.4f} CAD")
    print("================================")

    del state["positions"][market]
    state["last_processed_5m"].pop(market, None)


def manage_position(state, market, candles5):
    if market not in state["positions"]:
        return

    if not candles5:
        return

    position = state["positions"][market]
    last_time = state["last_processed_5m"].get(market, 0)

    new_candles = [
        candle for candle in candles5
        if candle["time"] > last_time
    ]

    if not new_candles:
        print(f"{market}: no new completed 5m candle for exit check")
        return

    oldest_available = candles5[0]["time"]

    # Avoid pretending we checked stop/target levels when data is missing.
    if last_time and last_time < oldest_available - 300:
        print(
            f"WARNING {market}: 5m history gap detected. "
            "Position not automatically closed."
        )
        return

    for candle in new_candles:
        stop_hit = candle["low"] <= position["stop"]
        target_hit = candle["high"] >= position["target"]

        # If both levels were touched in one candle, assume stop first.
        if stop_hit:
            close_position(
                state,
                market,
                position["stop"],
                "STOP_LOSS",
            )
            return

        if target_hit:
            close_position(
                state,
                market,
                position["target"],
                "TAKE_PROFIT",
            )
            return

        state["last_processed_5m"][market] = candle["time"]

    print(f"{market}: open paper position monitored")


# ============================================================
# ACCOUNT REPORT
# ============================================================

def print_status(state, latest_prices):
    print("\n========== GWVSA PAPER ACCOUNT ==========")
    print(f"Starting balance: ${STARTING_BALANCE:.2f} CAD")
    print(f"Available cash:   ${state['cash']:.2f} CAD")
    print(f"Open positions:   {len(state['positions'])}")
    print(f"Closed trades:    {state['total_trades']}")
    print(f"Wins:             {state['winning_trades']}")
    print(f"Losses:           {state['losing_trades']}")
    print(f"Realized P/L:     ${state['realized_pnl']:.2f} CAD")

    open_value = 0.0

    for market, position in state["positions"].items():
        mark_price = latest_prices.get(
            market, position["entry"]
        )
        open_value += position["quantity"] * mark_price

    equity = state["cash"] + open_value

    print(f"Open position value: ${open_value:.2f} CAD")
    print(f"Estimated equity:    ${equity:.2f} CAD")
    print("=========================================\n")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\nStarting GWVSA PAPER TRADING BOT")
    print("Paper trading only. No real Kraken orders.")
    print("Time:", datetime.now(timezone.utc).isoformat())

    ensure_trade_file()
    state = load_state()
    latest_prices = {}

    try:
        pairs, all_pairs = discover_markets()
    except Exception as error:
        print("Could not load Kraken market list:", error)
        save_state(state)
        print_status(state, latest_prices)
        return

    print("\n========== KRAKEN MARKET DISCOVERY ==========")

    for market in MARKETS:
        if market in pairs:
            print(f"{market}: FOUND as {pairs[market]}")
        else:
            print(f"{market}: NOT FOUND")

    print("\nOther available CAD pairs on Kraken:")

    cad_pairs = sorted({
        info.get("wsname", "")
        for info in all_pairs.values()
        if info.get("wsname", "").endswith("/CAD")
    })

    for name in cad_pairs[:40]:
        print(" ", name)

    print("=============================================\n")

    for market in MARKETS:
        pair = pairs.get(market)

        if not pair:
            print(f"{market}: skipped - pair not discovered")
            continue

        print(f"\n\n########## CHECKING {market} ##########")

        try:
            candles5 = get_candles(pair, 5)
            candles15 = get_candles(pair, 15)
            candles60 = get_candles(pair, 60)
            candles240 = get_candles(pair, 240)

            if candles5:
                latest_prices[market] = candles5[-1]["close"]

            print(
                f"Completed candles: "
                f"5m={len(candles5)}, "
                f"15m={len(candles15)}, "
                f"1H={len(candles60)}, "
                f"4H={len(candles240)}"
            )

            # Check exits before looking for new entries.
            manage_position(state, market, candles5)

            if market in state["positions"]:
                print(f"{market}: existing position remains open")
                continue

            report1h = trend_report(candles60)
            report4h = trend_report(candles240)

            print_trend(market, "1H", report1h)
            print_trend(market, "4H", report4h)

            if not report1h["valid"] or not report4h["valid"]:
                print(f"{market}: rejected because trend data is insufficient")
                continue

            if not report1h["bullish"]:
                print(f"{market}: ENTRY REJECTED - 1H trend is not bullish")
                continue

            if not report4h["bullish"]:
                print(f"{market}: ENTRY REJECTED - 4H trend is not bullish")
                continue

            signal, reason = check_gwvsa_entry(
                candles15,
                candles5,
            )

            print(f"{market}: GWVSA entry check: {reason}")

            if not signal:
                continue

            if (
                state["last_signal"].get(market)
                == signal["candle_time"]
            ):
                print(f"{market}: this signal was already processed")
                continue

            open_position(
                state,
                market,
                signal,
                candles5[-1]["time"],
            )

        except Exception as error:
            print(f"{market}: ERROR: {type(error).__name__}: {error}")

    save_state(state)
    print_status(state, latest_prices)
    print("GWVSA paper trading run completed.")


if __name__ == "__main__":
    main()