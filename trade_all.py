import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# GWVSA PAPER TRADING BOT V5.3 (ALL CAD CRYPTO)
# Kraken public data ONLY. NO REAL ORDERS.
# ============================================================

API = "https://api.kraken.com/0/public"

STARTING_BALANCE = 2000.00
RISK_PER_TRADE = 2.00
MAX_POSITION_VALUE = 200.00

FEE_RATE = 0.001
SLIPPAGE = 0.0015
# Dynamic take-profit tiers: (name, min net profit, max net profit) in CAD.
# A stronger setup earns a bigger profit target.
TIERS = [
    ("SMALL", 1.00, 5.00),
    ("MEDIUM", 6.00, 25.00),
    ("STRONG", 26.00, 100.00),
    ("EXCEPTIONAL", 101.00, 1000.00),
]

# Optional: move the stop to breakeven, then trail the high, once a
# trade is up by TRAIL_START_R x its risk.
USE_TRAILING_STOP = True
TRAIL_START_R = 1.0
TRAIL_ATR_MULT = 1.5
TRAIL_TIGHTEN = 0.5   # trail gets up to 50% tighter as price nears target

# Entry signal tuning
DISPLACEMENT_ATR = 1.0   # impulsive candle body must be >= this x ATR
MIN_STOP_PCT = 0.004     # stop at least 0.4% below entry (outside noise)
MAX_STOP_PCT = 0.08      # skip setups needing a stop wider than 8%

# Execution realism
MAX_ENTRY_DRIFT = 0.01      # skip if live ask is >1% above the signal close
MAX_TARGET_MOVE_PCT = 0.25  # a target never needs more than a 25% price move

# Slippage by 24h CAD volume: (minimum volume, slippage). Thin coins slip more.
SLIPPAGE_TIERS = [
    (1_000_000, 0.0010),
    (100_000, 0.0015),
    (20_000, 0.0030),
    (5_000, 0.0050),
    (0, 0.0100),
]

# Entry filter settings (easy to tune)
RSI_MIN = 30
RSI_MAX = 80
MIN_CONFIRMATIONS = 2        # out of 3 on the 5m chart
REQUIRE_RETEST = False       # breakout alone is enough
ALLOW_MOMENTUM_ENTRY = True  # third setup: 15m momentum

# Set FORCE_TEST_TRADE=1 in the environment to open one test
# paper trade on FORCE_TEST_MARKET, ignoring all filters.
FORCE_TEST_TRADE = os.environ.get("FORCE_TEST_TRADE") == "1"
FORCE_TEST_MARKET = "SOL/CAD"

# All-crypto scanning settings
QUOTE = "CAD"
MIN_24H_VOLUME_CAD = 500    # skip dead/illiquid coins (0 = no filter)
MAX_OPEN_POSITIONS = 8      # 8 x $200 fits in the $2000 account
CALL_DELAY = 1.1            # seconds between Kraken calls

# Stablecoins and fiat aren't worth trading.
EXCLUDE_BASES = {
    "USDT", "USDC", "DAI", "PYUSD", "USDG", "EURT", "TUSD",
    "USD", "EUR", "GBP", "AUD", "CAD", "CHF", "JPY",
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

MARKET_SLIPPAGE = {}


def slippage_from_volume(volume_cad):
    for minimum, slip in SLIPPAGE_TIERS:
        if volume_cad >= minimum:
            return slip

    return SLIPPAGE_TIERS[-1][1]


def slippage_for(market):
    return MARKET_SLIPPAGE.get(market, SLIPPAGE)


def api_get(endpoint, params=None):
    query = urllib.parse.urlencode(params or {})
    url = f"{API}/{endpoint}?{query}"

    for attempt in range(3):
        time.sleep(CALL_DELAY)

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "GWVSA-PaperTrader/5.0"}
        )

        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.loads(response.read().decode("utf-8"))

        if data.get("error"):
            message = "; ".join(data["error"])

            if "rate limit" in message.lower() and attempt < 2:
                time.sleep(15)
                continue

            raise RuntimeError(message)

        return data["result"]


def discover_markets(held=()):
    result = api_get("AssetPairs")
    candidates = {}

    for key, info in result.items():
        if key.endswith(".d"):
            continue

        if info.get("status", "online") != "online":
            continue

        ws = info.get("wsname", "")

        if "/" not in ws:
            continue

        base, quote = ws.split("/")

        if quote != QUOTE or base in EXCLUDE_BASES:
            continue

        base = "DOGE" if base == "XDG" else base

        candidates[f"{base}/{quote}"] = {
            "pair": info.get("altname", key),
            "key": key
        }

    if not candidates:
        return {}, result

    tickers = api_get("Ticker", {
        "pair": ",".join(c["pair"] for c in candidates.values())
    })

    scored = []

    for market, c in candidates.items():
        t = tickers.get(c["key"]) or tickers.get(c["pair"])
        volume = float(t["v"][1]) * float(t["p"][1]) if t else 0.0

        # Always keep markets with open positions so they get managed.
        if market in held or volume >= MIN_24H_VOLUME_CAD:
            scored.append((volume, market, c["pair"]))
            MARKET_SLIPPAGE[market] = slippage_from_volume(volume)
        else:
            print(f"Skipped {market}: 24h volume ${volume:,.0f} CAD")

    print(f"Kraken lists {len(candidates)} online {QUOTE} pairs; "
          f"{len(scored)} kept")

    scored.sort(reverse=True)  # most liquid first
    found = {market: pair for _, market, pair in scored}

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


def get_ask_price(pair):
    result = api_get("Ticker", {"pair": pair})

    if not result:
        raise RuntimeError("Ticker returned no market data")

    ticker = next(iter(result.values()))
    ask = float(ticker["a"][0])

    return ask if ask > 0 else float(ticker["c"][0])


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

def last_swing_high(candles, before, lookback=15):
    """Most recent fractal swing high (higher than 2 candles each side)."""
    for k in range(before - 3, max(before - lookback, 2) - 1, -1):
        high = candles[k]["high"]

        if all(
            high > candles[j]["high"] for j in (k - 2, k - 1, k + 1, k + 2)
        ):
            return high

    return None


def check_gwvsa_entry(candles15, candles5, slip=SLIPPAGE):
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
    sweep_index = None

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
            sweep_index = i

    # Breakout and retest.
    previous_highs = [c["high"] for c in candles15[-9:-1]]
    breakout_level = max(previous_highs)

    structure_break = current["close"] > breakout_level

    retest = (
        current["low"] <= breakout_level * 1.005
        and current["close"] > breakout_level
    )

    breakout_ok = structure_break and (retest or not REQUIRE_RETEST)
    retest_ok = structure_break and retest

    # Market structure shift: after the sweep, price closes above the
    # high that formed just before it.
    mss = False

    if sweep_found:
        reference_high = last_swing_high(candles15, sweep_index)

        if reference_high is None:
            reference_high = max(
                c["high"]
                for c in candles15[max(0, sweep_index - 5):sweep_index]
            )

        mss = any(
            c["close"] > reference_high
            for c in candles15[sweep_index + 1:]
        )

    # Displacement: a strong bullish candle closing near its high that
    # leaves a fair value gap (the next candle's low stays above the
    # high of the candle before it).
    displacement = False
    end = len(candles15) - 1

    for b in (end - 2, end - 1):
        before = candles15[b - 1]
        mid = candles15[b]
        after = candles15[b + 1]

        body = mid["close"] - mid["open"]
        span = mid["high"] - mid["low"]

        if (
            span > 0
            and body >= DISPLACEMENT_ATR * current_atr
            and (mid["close"] - mid["low"]) / span >= 0.75
            and after["low"] > before["high"]
        ):
            displacement = True

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

    # 5-minute confirmation: best score among the last 3 closed
    # 5m candles, so one weak candle doesn't block a good setup.
    confirmations = 0

    for k in range(1, 4):
        five = candles5[-k]
        previous_five = candles5[-k - 1]

        previous_volumes = [
            c["volume"] for c in candles5[-k - 20:-k]
        ]
        average_volume = sum(previous_volumes) / len(previous_volumes)

        bullish_candle = five["close"] > five["open"]
        bullish_break = five["close"] > previous_five["high"]
        volume_confirmed = five["volume"] >= average_volume * 0.80

        score = sum([
            bullish_candle,
            bullish_break,
            volume_confirmed
        ])

        confirmations = max(confirmations, score)

    if confirmations < MIN_CONFIRMATIONS:
        return None, (
            f"5m confirmation rejected "
            f"(best of last 3 candles: {confirmations}/3)"
        )

    # Stop goes where the setup is proven wrong.
    if sweep_found:
        stop = swing_low - (0.25 * current_atr)
    elif breakout_ok:
        stop = min(
            breakout_level - (0.50 * current_atr),
            current["low"] - (0.20 * current_atr)
        )
    else:
        stop = (
            min(c["low"] for c in candles15[-8:])
            - (0.20 * current_atr)
        )

    estimated_buy = current["close"] * (1 + slip)

    # Not inside normal noise, and not so far away it is dead weight.
    stop = min(stop, estimated_buy * (1 - MIN_STOP_PCT))

    if (estimated_buy - stop) / estimated_buy > MAX_STOP_PCT:
        return None, f"Stop too wide (over {MAX_STOP_PCT:.0%})"

    return {
        "candle_time": current["time"],
        "entry_reference": current["close"],
        "stop": stop,
        "atr": current_atr,
        "rsi": current_rsi,
        "confirmations": confirmations,
        "sweep": sweep_found,
        "breakout": breakout_ok,
        "mss": mss,
        "displacement": displacement,
        "retest": retest_ok,
        "setup": (
            "SWEEP" if sweep_found
            else "BREAKOUT" if breakout_ok
            else "MOMENTUM"
        )
    }, "Flexible GWVSA entry confirmed"


# ============================================================
# PAPER BUY
# ============================================================

def breakeven_price(entry, slip=SLIPPAGE):
    """Stop price that exits at net zero after fees and slippage."""
    return entry * (1 + FEE_RATE) / ((1 - slip) * (1 - FEE_RATE))


def opportunity_tier(signal):
    """Score the setup (1-11) and return (tier name, net profit target)."""
    score = {"SWEEP": 2, "BREAKOUT": 2, "MOMENTUM": 1}.get(
        signal.get("setup"), 1
    )

    if signal.get("sweep") and signal.get("breakout"):
        score += 1

    score += signal.get("confirmations", 0)  # 0-3

    if signal.get("four_h_bullish"):
        score += 2

    for key in ("mss", "displacement", "retest"):
        if signal.get(key):
            score += 1

    if score <= 4:
        index, fraction = 0, (score - 1) / 3
    elif score <= 7:
        index, fraction = 1, (score - 5) / 2
    elif score <= 9:
        index, fraction = 2, score - 8
    else:
        index, fraction = 3, score - 10

    fraction = min(max(fraction, 0), 1)
    name, low, high = TIERS[index]

    return name, round(low + (high - low) * fraction, 2)


def open_position(state, market, signal, latest_5m_time):
    if market in state["positions"]:
        return

    slip = slippage_for(market)
    buy_price = signal["entry_reference"] * (1 + slip)
    stop = signal["stop"]

    risk_per_unit = buy_price - stop

    if risk_per_unit <= 0:
        print(f"{market}: invalid risk")
        return

    # Real loss per unit if stopped out: entry fee + exit slippage + exit fee.
    loss_per_unit = (
        buy_price * (1 + FEE_RATE)
        - stop * (1 - slip) * (1 - FEE_RATE)
    )

    quantity_by_risk = RISK_PER_TRADE / loss_per_unit
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

    tier, target_profit = opportunity_tier(signal)

    # Keep the target reachable: never ask for more than a
    # MAX_TARGET_MOVE_PCT price move on this position.
    target_profit = round(min(target_profit, notional * MAX_TARGET_MOVE_PCT), 2)

    if target_profit < TIERS[0][1]:
        print(
            f"{market}: skipped - position too small for the "
            f"${TIERS[0][1]:.2f} minimum target"
        )
        return

    # Exit price that nets exactly target_profit after fees and slippage.
    target = (target_profit + total_cost) / (
        quantity * (1 - FEE_RATE) * (1 - slip)
    )

    state["cash"] -= total_cost

    position = {
        "quantity": quantity,
        "entry": buy_price,
        "stop": stop,
        "target": target,
        "risk": risk_per_unit,
        "atr": signal.get("atr"),
        "tier": tier,
        "target_profit": target_profit,
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
        "OPEN_" + signal["setup"] + "_" + tier
    ])

    print("\n========== PAPER BUY ==========")
    print(f"Market:        {market}")
    print(f"Setup:         {signal['setup']}")
    tags = [
        name for key, name in (
            ("sweep", "sweep"), ("mss", "structure shift"),
            ("displacement", "displacement"), ("retest", "retest"),
            ("four_h_bullish", "4H trend"),
        ) if signal.get(key)
    ]
    print(f"Confluence:    {', '.join(tags) or 'none'}")
    print(f"Quantity:      {quantity:.8f}")
    print(f"Buy price:     {buy_price:.8f}")
    print(f"Stop loss:     {stop:.8f}")
    print(f"Take profit:   {target:.8f}")
    print(f"Opportunity:   {tier} (target ${target_profit:.2f} CAD)")
    print(f"Risk budget:   ${RISK_PER_TRADE:.2f} CAD")
    print(f"Position cost: ${total_cost:.4f} CAD")
    print("================================")


# ============================================================
# PAPER SELL
# ============================================================

def close_position(state, market, exit_reference, reason):
    position = state["positions"][market]

    sell_price = exit_reference * (1 - slippage_for(market))
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
    position.setdefault("risk", position["entry"] - position["stop"])

    for candle in new_candles:
        stop_hit = candle["low"] <= position["stop"]
        target_hit = candle["high"] >= position["target"]

        # Stop first if both were reached in one candle.
        if stop_hit:
            reason = (
                "TRAILING_STOP"
                if position["stop"] >= position["entry"]
                else "STOP_LOSS"
            )
            # A candle that opens below the stop fills at the open.
            if candle["open"] < position["stop"]:
                reason += "_GAP"

            fill = min(position["stop"], candle["open"])
            close_position(state, market, fill, reason)
            return

        if target_hit:
            # A candle that opens above the target fills at the open.
            fill = max(position["target"], candle["open"])
            close_position(state, market, fill, "TAKE_PROFIT")
            return

        # Dynamic take-profit: breakeven, then trail the high.
        risk = position["risk"]

        if (
            USE_TRAILING_STOP
            and candle["high"] >= position["entry"] + TRAIL_START_R * risk
        ):
            gap = position["target"] - position["entry"]
            progress = min(max(
                (candle["high"] - position["entry"]) / gap, 0
            ), 1)

            distance = (
                TRAIL_ATR_MULT * (1 - TRAIL_TIGHTEN * progress)
                * position["atr"]
                if position.get("atr") else risk
            )
            new_stop = max(
                breakeven_price(position["entry"], slippage_for(market)),
                candle["high"] - distance
            )

            if new_stop > position["stop"]:
                position["stop"] = new_stop

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
        # Value at what a sale would really return (slippage + exit fee).
        open_value += (
            position["quantity"] * price
            * (1 - slippage_for(market)) * (1 - FEE_RATE)
        )

    equity = state["cash"] + open_value

    open_cost = sum(p["entry_cost"] for p in state["positions"].values())

    print(f"Open position value: ${open_value:.2f} CAD (after exit costs)")
    print(f"Unrealized P/L:      ${open_value - open_cost:.2f} CAD")
    print(f"Estimated equity:    ${equity:.2f} CAD")
    print(f"Total P/L vs start:  ${equity - STARTING_BALANCE:.2f} CAD")
    print("=========================================")


# ============================================================
# MAIN
# ============================================================

def main():
    print("\nGWVSA FLEXIBLE PAPER TRADING BOT V5.3 (ALL CAD CRYPTO)")
    print("Paper trading only. No real Kraken orders.")
    print("Time:", datetime.now(timezone.utc).isoformat())

    if FORCE_TEST_TRADE:
        print(f"FORCE_TEST_TRADE is ON ({FORCE_TEST_MARKET})")

    ensure_trade_file()
    state = load_state()
    latest_prices = {}

    try:
        pairs, all_pairs = discover_markets(set(state["positions"]))
    except Exception as error:
        print("Kraken market discovery error:", error)
        save_state(state)
        print_status(state, latest_prices)
        return

    markets = list(pairs)
    print(f"\nScanning {len(markets)} {QUOTE} markets")

    for market in state["positions"]:
        if market not in pairs:
            print(f"WARNING {market}: open position but pair not found")

    for market in markets:
        pair = pairs[market]
        print(f"\n######## CHECKING {market} ########")

        try:
            forced = FORCE_TEST_TRADE and market == FORCE_TEST_MARKET

            # Manage open positions first.
            if market in state["positions"]:
                candles5 = get_candles(pair, 5)

                if candles5:
                    latest_prices[market] = candles5[-1]["close"]

                manage_position(state, market, pair, candles5)

                if market in state["positions"]:
                    print(f"{market}: existing position remains open")
                    continue

            if len(state["positions"]) >= MAX_OPEN_POSITIONS:
                print(f"{market}: max open positions reached")
                continue

            four_h = False

            # Cheap 1H check first; only fetch more data if it passes.
            if not forced:
                report1h = trend_report(get_candles(pair, 60), "1H")
                print_trend(market, "1H", report1h)

                if not report1h["valid"] or not report1h["bullish"]:
                    print(f"{market}: 1H trend rejected")
                    continue

                report4h = trend_report(get_candles(pair, 240), "4H")
                print_trend(market, "4H", report4h)
                four_h = report4h["valid"] and report4h["bullish"]

                if not report4h["valid"] or not report4h["bullish"]:
                    print(
                        f"{market}: 4H warning (advisory); "
                        "continuing to entry test."
                    )

            candles15 = get_candles(pair, 15)
            candles5 = get_candles(pair, 5)

            if candles5:
                latest_prices[market] = candles5[-1]["close"]

            # Test mode: open one paper trade, skip all filters.
            if forced and candles15 and candles5:
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

            signal, reason = check_gwvsa_entry(
                candles15, candles5, slippage_for(market)
            )

            print(f"{market}: ENTRY CHECK: {reason}")

            if not signal:
                continue

            if state["last_signal"].get(market) == signal["candle_time"]:
                print(f"{market}: signal already processed")
                continue

            # Enter at the live ask, not the stale candle close.
            ask = get_ask_price(pair)
            drift = ask / signal["entry_reference"] - 1

            if drift > MAX_ENTRY_DRIFT:
                print(
                    f"{market}: skipped - ask is {drift:.2%} above "
                    "the signal close"
                )
                continue

            signal["entry_reference"] = ask
            signal["four_h_bullish"] = four_h

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
