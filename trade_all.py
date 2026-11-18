import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# GWVSA PAPER TRADING BOT V5.2
# Kraken public data ONLY. NO REAL ORDERS.
# ============================================================

API = "https://api.kraken.com/0/public"

STARTING_BALANCE = 2000.00
RISK_PER_TRADE = 2.00
MAX_POSITION_VALUE = 15.00

FEE_RATE = 0.001
SLIPPAGE = 0.0015
REWARD_RISK = 2.0

# Entry filter settings (easy to tune)
RSI_MIN = 30
RSI_MAX = 80          # was 75
MIN_CONFIRMATIONS = 1  # out of 3 on the 5m chart (was 2)
REQUIRE_RETEST = False       # breakout alone is enough
ALLOW_MOMENTUM_ENTRY = True  # third setup: 15m momentum

# Set FORCE_TEST_TRADE=1 in the environment to open one test
# paper trade on FORCE_TEST_MARKET, ignoring all filters.
FORCE_TEST_TRADE = os.environ.get("FORCE_TEST_TRADE") == "1"
FORCE_TEST_MARKET = "SOL/CAD"

MARKETS = [
    "XBT/CAD",
    "ETH/CAD",
    "SOL/CAD",
    "XRP/CAD",
    "DOGE/CAD",
    "PEPE/CAD",
]

MARKET_ALIASES = {
    "DOGE/CAD": ["DOGE/CAD", "XDG/CAD"],
}

STATE_FILE = "gwvsa_state.json"
TRADE_FILE = "gwvsa_trades.csv"

TRADE_HEADERS = [
    "time_utc", "market", "side", "quantity",
    "entry_price", "exit_price", "stop_loss",
    "take_profit", "net_pnl_cad", "result"
]


# ============================================================
# KRAKEN API
# ============================================================

def api_get(endpoint, params=None):
    query = urllib.parse.urlencode(params or {})
    url = f"{API}/{endpoint}?{query}"

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "GWVSA-PaperTrader/5.0"}
    )

    with urllib.request.urlopen(request, timeout=25) as response:
        data = json.loads(response.read().decode("utf-8"))

    if data.get("error"):
        raise RuntimeError("; ".join(data["error"]))

    return data["result"]


def normalize_pair(value):
    return (
        str(value).upper()
        .replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
    )


def discover_markets():
    result = api_get("AssetPairs")
    found = {}

    for requested in MARKETS:
        aliases = MARKET_ALIASES.get(requested, [requested])
        wanted = {normalize_pair(alias) for alias in aliases}

        for pair_key, info in result.items():
            names = [
                info.get("wsname", ""),
                info.get("altname", ""),
                pair_key
            ]

            if any(
                normalize_pair(name) in wanted
                for name in names if name
            ):
                found[requested] = info.get("altname", pair_key)
                break

    return found, result


def get_candles(pair, interval):
    result = api_get("OHLC", {
        "pair": pair,
        "interval": interval
    })

    candle_key = next(
        (key for key in result if key != "last"),
        None
    )

    if not candle_key:
        return []

    # Exclude the currently forming candle.
    rows = result[candle_key][:-1]

    return [
        {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6])
        }
        for row in rows
    ]


def get_ticker_price(pair):
    result = api_get("Ticker", {"pair": pair})

    if not result:
        raise RuntimeError("Ticker returned no market data")

    ticker = next(iter(result.values()))
    return float(ticker["c"][0])


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        return None

    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for price in values[period:]:
        result = (price - result) * multiplier + result

    return result


def rsi(values, period=14):
    if len(values) < period + 1:
        return None

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    recent = changes[-period:]

    gains = [max(x, 0) for x in recent]
    losses = [max(-x, 0) for x in recent]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


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
            abs(current["low"] - previous_close)
        ))

    return sum(ranges[-period:]) / period


# ============================================================
# FLEXIBLE TREND FILTER
# ============================================================

def trend_report(candles, timeframe):
    if len(candles) < 210:
        return {
            "valid": False,
            "bullish": False,
            "reason": f"Need 210 candles; received {len(candles)}"
        }

    closes = [c["close"] for c in candles]

    price = closes[-1]
    e8 = ema(closes, 8)
    e40 = ema(closes, 40)
    e200 = ema(closes, 200)
    previous_e8 = ema(closes[:-5], 8)

    if None in (e8, e40, e200, previous_e8):
        return {
            "valid": False,
            "bullish": False,
            "reason": "Indicator data unavailable"
        }

    improving = e8 > previous_e8
    short_trend = e8 > e40

    if timeframe == "1H":
        # Looser 1H rule: EMA 8 rising and price above it.
        # (Old rule required EMA 8 above EMA 40 as well.)
        bullish = improving and price > e8

        reason = (
            "Supportive 1H momentum"
            if bullish else "1H momentum rejected"
        )

    else:
        # 4H is advisory only.
        bullish = (
            price >= e200 * 0.985 and short_trend
        ) or (
            price > e200 and improving
        )

        reason = (
            "Supportive 4H context"
            if bullish else "Bearish 4H context - advisory only"
        )

    return {
        "valid": True,
        "bullish": bullish,
        "price": price,
        "ema8": e8,
        "ema40": e40,
        "ema200": e200,
        "improving": improving,
        "reason": reason
    }


def print_trend(market, timeframe, report):
    print(f"\n{market} - {timeframe}")

    if not report["valid"]:
        print(report["reason"])
        return

    print(f"Price:   {report['price']:.8f}")
    print(f"EMA 8:   {report['ema8']:.8f}")
    print(f"EMA 40:  {report['ema40']:.8f}")
    print(f"EMA 200: {report['ema200']:.8f}")
    print(f"Result:  {report['reason']}")


# ============================================================
# ACCOUNT FILES
# ============================================================

def ensure_trade_file():
    if not os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "w", newline="") as file:
            csv.writer(file).writerow(TRADE_HEADERS)


def load_state():
    defaults = {
        "cash": STARTING_BALANCE,
        "positions": {},
        "last_signal": {},
        "last_processed_5m": {},
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "realized_pnl": 0.0
    }

    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as file:
            state = json.load(file)
    else:
        state = dict(defaults)

    for key, value in defaults.items():
        state.setdefault(key, value)

    return state


def save_state(state):
    temporary = STATE_FILE + ".tmp"

    with open(temporary, "w") as file:
        json.dump(state, file, indent=2)

    os.replace(temporary, STATE_FILE)


def log_trade(row):
    ensure_trade_file()

    with open(TRADE_FILE, "a", newline="") as file:
        csv.writer(file).writerow(row)


# ============================================================
# GWVSA ENTRY CHECK
# ============================================================

def check_gwvsa_entry(candles15, candles5):
    if len(candles15) < 30:
        return None, "Not enough 15m candles"

    if len(candles5) < 25:
        return None, "Not enough 5m candles"

    current = candles15[-1]
    closes = [c["close"] for c in candles15]

    current_rsi = rsi(closes)
    current_atr = atr(candles15)

    if current_rsi is None or current_atr is None:
        return None, "RSI or ATR unavailable"

    if not RSI_MIN <= current_rsi <= RSI_MAX:
        return None, f"RSI rejected: {current_rsi:.2f}"

    # Liquidity sweep and recovery.
    sweep_found = False
    swing_low = None

    start = max(10, len(candles15) - 9)

    for i in range(start, len(candles15) - 1):
        previous = candles15[i - 10:i]
        prior_low = min(c["low"] for c in previous)

        if (
            candles15[i]["low"] < prior_low
            and candles15[i]["close"] > prior_low
        ):
            sweep_found = True
            swing_low = candles15[i]["low"]

    # Breakout and retest.
    previous_highs = [c["high"] for c in candles15[-9:-1]]
    breakout_level = max(previous_highs)

    structure_break = current["close"] > breakout_level

    retest = (
        current["low"] <= breakout_level * 1.005
        and current["close"] > breakout_level
    )

    breakout_ok = structure_break and (retest or not REQUIRE_RETEST)

    # Momentum setup: price above 15m EMA 20 with healthy RSI.
    e8_15 = ema(closes, 8)
    e20_15 = ema(closes, 20)
    momentum_setup = False

    if ALLOW_MOMENTUM_ENTRY and not sweep_found and not breakout_ok:
        if (
            e20_15
            and current["close"] > e20_15
            and 40 <= current_rsi <= 75
        ):
            momentum_setup = True

    if not sweep_found and not breakout_ok and not momentum_setup:
        return None, (
            "No qualifying sweep, breakout or momentum setup "
            f"(RSI {current_rsi:.1f}, "
            f"close {current['close']:.6g}, "
            f"15m EMA8 {e8_15:.6g}, EMA20 {e20_15:.6g}, "
            f"breakout level {breakout_level:.6g})"
        )

    # 5-minute confirmation.
    five = candles5[-1]
    previous_five = candles5[-2]

    previous_volumes = [c["volume"] for c in candles5[-21:-1]]
    average_volume = sum(previous_volumes) / len(previous_volumes)

    bullish_candle = five["close"] > five["open"]
    bullish_break = five["close"] > previous_five["high"]
    volume_confirmed = five["volume"] >= average_volume * 0.80

    confirmations = sum([
        bullish_candle,
        bullish_break,
        volume_confirmed
    ])

    if confirmations < MIN_CONFIRMATIONS:
        return None, (
            f"5m confirmation rejected "
            f"({confirmations}/3 conditions)"
        )

    recent_low = min(c["low"] for c in candles15[-8:])

    if sweep_found:
        recent_low = min(recent_low, swing_low)

    stop = recent_low - (0.20 * current_atr)
    estimated_buy = current["close"] * (1 + SLIPPAGE)

    if stop >= estimated_buy:
        return None, "Invalid stop-loss"

    return {
        "candle_time": current["time"],
        "entry_reference": current["close"],
        "stop": stop,
        "atr": current_atr,
        "rsi": current_rsi,
        "setup": (
            "SWEEP" if sweep_found
            else "BREAKOUT" if breakout_ok
            else "MOMENTUM"
        )
    }, "Flexible GWVSA entry confirmed"


# ============================================================
# PAPER BUY
# ============================================================

def open_position(state, market, signal, latest_5m_time):
    if market in state["positions"]:
        return

    buy_price = signal["entry_reference"] * (1 + SLIPPAGE)
    stop = signal["stop"]

    risk_per_unit = buy_price - stop

    if risk_per_unit <= 0:
        print(f"{market}: invalid risk")
        return

    quantity_by_risk = RISK_PER_TRADE / risk_per_unit
    quantity_by_allocation = MAX_POSITION_VALUE / buy_price

    affordable_quantity = state["cash"] / (
        buy_price * (1 + FEE_RATE)
    )

    quantity = min(
        quantity_by_risk,
        quantity_by_allocation,
        affordable_quantity
    )

    if quantity <= 0:
        print(f"{market}: insufficient paper cash")
        return

    notional = quantity * buy_price
    entry_fee = notional * FEE_RATE
    total_cost = notional + entry_fee

    if total_cost > state["cash"]:
        print(f"{market}: insufficient cash after fee")
        return

    target = buy_price + risk_per_unit * REWARD_RISK

    state["cash"] -= total_cost

    position = {
        "quantity": quantity,
        "entry": buy_price,
        "stop": stop,
        "target": target,
        "entry_cost": total_cost,
        "entry_time": int(time.time()),
        "signal_time": signal["candle_time"]
    }

    state["positions"][market] = position
    state["last_signal"][market] = signal["candle_time"]
    state["last_processed_5m"][market] = latest_5m_time

    save_state(state)

    log_trade([
        datetime.now(timezone.utc).isoformat(),
        market,
        "BUY",
        round(quantity, 8),
        round(buy_price, 8),
        "",
        round(stop, 8),
        round(target, 8),
        "",
        "OPEN_" + signal["setup"]
    ])

    print("\n========== PAPER BUY ==========")
    print(f"Market:        {market}")
    print(f"Setup:         {signal['setup']}")
    print(f"Quantity:      {quantity:.8f}")
    print(f"Buy price:     {buy_price:.8f}")
    print(f"Stop loss:     {stop:.8f}")
    print(f"Take profit:   {target:.8f}")
    print(f"Risk budget:   ${RISK_PER_TRADE:.2f} CAD")
    print(f"Position cost: ${total_cost:.4f} CAD")
    print("================================")


# ============================================================
# PAPER SELL
# ============================================================

def close_position(state, market, exit_reference, reason):
    position = state["positions"][market]

    sell_price = exit_reference * (1 - SLIPPAGE)
    proceeds = position["quantity"] * sell_price
    exit_fee = proceeds * FEE_RATE

    state["cash"] += proceeds - exit_fee

    net_pnl = proceeds - exit_fee - position["entry_cost"]

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
        reason
    ])

    print("\n========== PAPER SELL ==========")
    print(f"Market:       {market}")
    print(f"Reason:       {reason}")
    print(f"Sell price:   {sell_price:.8f}")
    print(f"NET P/L:      ${net_pnl:.4f} CAD")
    print("================================")

    del state["positions"][market]
    state["last_processed_5m"].pop(market, None)

    save_state(state)


# ============================================================
# STOP-LOSS / TAKE-PROFIT MONITORING
# ============================================================

def manage_position(state, market, pair, candles5):
    if market not in state["positions"]:
        return

    if not candles5:
        print(f"{market}: cannot monitor - no 5m candles")
        return

    position = state["positions"][market]
    last_time = state["last_processed_5m"].get(market, 0)

    if not last_time:
        entry_time = position.get("entry_time", 0)

        if entry_time:
            last_time = int(entry_time)
        else:
            try:
                current_price = get_ticker_price(pair)
            except Exception:
                current_price = candles5[-1]["close"]

            print(f"WARNING {market}: no entry checkpoint")
            close_position(
                state, market, current_price,
                "GAP_EXIT_NO_CHECKPOINT"
            )
            return

    oldest_available = candles5[0]["time"]
    latest_available = candles5[-1]["time"]

    # If missed candles are older than available history,
    # use a labelled current-market-price gap exit.
    if last_time < oldest_available - 300:
        print(
            f"WARNING {market}: candle history gap "
            "exceeds available history."
        )

        try:
            current_price = get_ticker_price(pair)
            reason = "GAP_EXIT_CURRENT_TICKER"
        except Exception as error:
            print("Ticker fallback error:", error)
            current_price = candles5[-1]["close"]
            reason = "GAP_EXIT_LAST_COMPLETED_CANDLE"

        close_position(state, market, current_price, reason)
        return

    new_candles = [
        c for c in candles5 if c["time"] > last_time
    ]

    if not new_candles:
        print(f"{market}: waiting for new 5m candle")
        return

    print(
        f"{market}: replaying {len(new_candles)} "
        "missed/new 5m candle(s)"
    )

    # Process every available candle chronologically.
    for candle in new_candles:
        stop_hit = candle["low"] <= position["stop"]
        target_hit = candle["high"] >= position["target"]

        # Stop first if both were reached in one candle.
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
        save_state(state)

    print(
        f"{market}: monitored through candle "
        f"{latest_available}"
    )


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
        price = latest_prices.get(market, position["entry"])
        open_value += position["quantity"] * price

    equity = state["cash"] + open_value

    print(f"Open position value: ${open_value:.2f} CAD")
    print(f"Estimated equity:    ${equity:.2f} CAD")
    print("=========================================")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\nGWVSA FLEXIBLE PAPER TRADING BOT V5.2")
    print("Paper trading only. No real Kraken orders.")
    print("Time:", datetime.now(timezone.utc).isoformat())

    if FORCE_TEST_TRADE:
        print(f"FORCE_TEST_TRADE is ON ({FORCE_TEST_MARKET})")

    ensure_trade_file()
    state = load_state()
    latest_prices = {}

    try:
        pairs, all_pairs = discover_markets()
    except Exception as error:
        print("Kraken market discovery error:", error)
        save_state(state)
        print_status(state, latest_prices)
        return

    print("\n========== MARKET DISCOVERY ==========")

    for market in MARKETS:
        if market in pairs:
            print(f"{market}: FOUND as {pairs[market]}")
        else:
            print(f"{market}: NOT FOUND")

    for market in MARKETS:
        pair = pairs.get(market)

        if not pair:
            print(f"{market}: skipped - Kraken pair not found")
            continue

        print(f"\n######## CHECKING {market} ########")

        try:
            candles5 = get_candles(pair, 5)
            candles15 = get_candles(pair, 15)
            candles60 = get_candles(pair, 60)
            candles240 = get_candles(pair, 240)

            print(
                f"Candles: 5m={len(candles5)}, "
                f"15m={len(candles15)}, "
                f"1H={len(candles60)}, "
                f"4H={len(candles240)}"
            )

            if candles5:
                latest_prices[market] = candles5[-1]["close"]

            # Manage open positions before new entries.
            manage_position(state, market, pair, candles5)

            if market in state["positions"]:
                print(f"{market}: existing position remains open")
                continue

            # Test mode: open one paper trade, skip all filters.
            if (
                FORCE_TEST_TRADE
                and market == FORCE_TEST_MARKET
                and candles15
                and candles5
            ):
                last = candles15[-1]
                current_atr = atr(candles15)

                if current_atr:
                    print(f"{market}: FORCED TEST TRADE")
                    open_position(
                        state,
                        market,
                        {
                            "candle_time": last["time"],
                            "entry_reference": last["close"],
                            "stop": last["close"] - 1.5 * current_atr,
                            "atr": current_atr,
                            "rsi": 0,
                            "setup": "FORCE_TEST"
                        },
                        candles5[-1]["time"]
                    )
                    continue

            report1h = trend_report(candles60, "1H")
            report4h = trend_report(candles240, "4H")

            print_trend(market, "1H", report1h)
            print_trend(market, "4H", report4h)

            if not report1h["valid"]:
                print(f"{market}: insufficient 1H trend data")
                continue

            if not report1h["bullish"]:
                print(f"{market}: 1H trend rejected")
                continue

            # 4H is advisory, never a hard rejection.
            if report4h["valid"] and not report4h["bullish"]:
                print(
                    f"{market}: 4H bearish warning; "
                    "continuing to entry test."
                )

            if not report4h["valid"]:
                print(
                    f"{market}: 4H unavailable; "
                    "continuing to entry test."
                )

            signal, reason = check_gwvsa_entry(candles15, candles5)

            print(f"{market}: ENTRY CHECK: {reason}")

            if not signal:
                continue

            if state["last_signal"].get(market) == signal["candle_time"]:
                print(f"{market}: signal already processed")
                continue

            open_position(
                state,
                market,
                signal,
                candles5[-1]["time"]
            )

        except Exception as error:
            print(
                f"{market}: ERROR: "
                f"{type(error).__name__}: {error}"
            )

    save_state(state)
    print_status(state, latest_prices)
    print("\nGWVSA paper trading run completed.")


if __name__ == "__main__":
    main()
