import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# GWVSA PAPER TRADING BOT V8.3
# Kraken public data ONLY. NO REAL ORDERS.
#
# What changed from V8.2:
#  - Fixed a replay bug: after entry, the bot used to replay the
#    5m candle that was still forming when it bought, which holds
#    price action from BEFORE the entry. That could stop a trade
#    out at a price it never traded at (STOP_LOSS_GAP seconds after
#    entry). Now only candles that opened at/after entry_time are
#    replayed.
#  - Cooldown: after a stop-out, a market is skipped for
#    COOLDOWN_SECONDS (default 4 hours) so the bot can't re-buy the
#    same coin straight away.
#  - Version bump archives V8.2 records so V8.3 stats start clean.
#
# V8.2 (vs V8.1):
#  - Scans ALL Kraken markets: crypto, tokenized stocks (xStocks,
#    e.g. AAPLx/USD) and forex (EUR/USD, GBP/USD, AUD/USD).
#    Each asset class can be switched on/off, and has its own
#    minimum-volume filter. MAX_MARKETS = None scans everything.
#
# V8.1 (vs V8):
#  - Risk per trade raised 30x: $2 -> $60 (3% of the $2,000 account).
#  - Max position value raised $200 -> $1,200 so it no longer caps
#    the larger risk.
#  - Max open positions lowered 8 -> 2 (big positions use most cash).
#  - Minimum 24h volume raised to $500k CAD so large orders don't
#    slip badly in thin coins.
#  - Version bump archives V8 records so V8.1 stats start clean.
#
# From V8:
#  - Trailing stop is an on/off switch, OFF by default.
#  - Momentum entries OFF by default.
#  - 5m confirmation needs all 3 checks.
#  - Every trade row logs setup, score, tier, R multiple, RSI,
#    confirmations, hold time and P/L in R.
#  - Account report shows average win, average loss, profit
#    factor, expectancy and break-even win rate.
# ============================================================

BOT_VERSION = "V8.3"

API = "https://api.kraken.com/0/public"

STARTING_BALANCE = 2000.00
RISK_PER_TRADE = 60.00        # was 2.00 (30x)
MAX_POSITION_VALUE = 1200.00  # was 200.00

FEE_RATE = 0.001
SLIPPAGE = 0.0015

# Take-profit as a multiple of the stop distance, by setup score.
# (max score, R multiple, label)
R_TIERS = [
    (4, 1.5, "SMALL"),
    (7, 2.0, "MEDIUM"),
    (9, 2.5, "STRONG"),
    (99, 3.0, "EXCEPTIONAL"),
]
MIN_NET_PROFIT = 1.00       # skip trades whose target nets under this (CAD)
MIN_NET_RR = 1.3            # net profit at target must be >= 1.3x net loss at stop
MAX_TARGET_MOVE_PCT = 0.25  # a target never needs more than a 25% move

# Trailing stop: breakeven, then trail the high once up TRAIL_START_R x risk.
# OFF by default: V7's trail was tighter than normal 15m noise and cut
# winners short. Turn on to compare (change only one thing at a time).
USE_TRAILING_STOP = False
TRAIL_START_R = 1.5
TRAIL_ATR_MULT = 2.5
TRAIL_TIGHTEN = 0.5

# Entry signal tuning
DISPLACEMENT_ATR = 1.0
MIN_STOP_PCT = 0.004
MAX_STOP_PCT = 0.08
COST_STOP_MULT = 3.0        # stop must be >= 3x round-trip cost (fees+slippage)

# Execution realism
MAX_ENTRY_DRIFT = 0.01

# Cooldown after a stop-out: don't re-enter the same market for this long.
COOLDOWN_SECONDS = 4 * 3600

# Slippage by 24h CAD volume: (minimum volume, slippage).
SLIPPAGE_TIERS = [
    (1_000_000, 0.0010),
    (100_000, 0.0015),
    (20_000, 0.0030),
    (5_000, 0.0050),
    (0, 0.0100),
]

# Entry filter settings
RSI_MIN = 30
RSI_MAX = 80
MIN_CONFIRMATIONS = 2       # 2 of 3 (V8 used 3 of 3; lowered for more trades)
ALLOW_PULLBACK_1H = True    # also accept 1H pullbacks in an uptrend (EMA8>EMA40>EMA200, price>EMA40)
REQUIRE_RETEST = False
ALLOW_MOMENTUM_ENTRY = False  # was True; momentum entries lost most often
ALLOW_4H_BULLISH = True   # hard filter: skip anything the 4H trend rejects

FORCE_TEST_TRADE = os.environ.get("FORCE_TEST_TRADE") == "1"
FORCE_TEST_MARKET = "SOL/CAD"

# All-crypto scanning settings
QUOTE = "CAD"
USE_USD_PAIRS = True
MAX_MARKETS = None            # None = scan every market that passes the filters

# Which asset classes to scan.
INCLUDE_CRYPTO = True
INCLUDE_STOCKS = True         # Kraken tokenized stocks, e.g. AAPLx/USD
INCLUDE_FOREX = True          # fiat pairs quoted in USD, e.g. EUR/USD

# Minimum 24h volume (CAD) per asset class; large orders need liquidity.
# 0 = scan everything. Thin markets still get small positions because of
# MAX_VOLUME_SHARE below.
MIN_24H_VOLUME_CAD = {
    "crypto": 0,
    "stock": 0,
    "forex": 0,
}

# A position may be at most this share of the market's 24h volume, so big
# orders aren't placed in thin markets (0.005 = 0.5%).
MAX_VOLUME_SHARE = 0.005
MAX_OPEN_POSITIONS = 2        # was 8; $1,200 positions can't be held 8 at a time
CALL_DELAY = 1.1

# Stablecoins, fiat and gold/commodity tokens aren't worth trading.
EXCLUDE_BASES = {
    "USDT", "USDC", "DAI", "PYUSD", "USDG", "EURT", "TUSD",
    "USD", "CAD",
    "USD1", "RLUSD", "EURQ", "USDQ", "EURR", "FDUSD", "USDD", "UST", "AUDX",
    "EURC", "USDE", "USDS", "USDR", "BUIDL", "USTB",
    "PAXG", "XAUT",
}

# Fiat currencies treated as forex when quoted against USD.
FOREX_BASES = {"EUR", "GBP", "AUD", "NZD", "CHF", "JPY", "CAD"}

STATE_FILE = "gwvsa_state.json"
TRADE_FILE = "gwvsa_trades.csv"
ARCHIVE_DIR = "archive"

TRADE_HEADERS = [
    "time_utc", "market", "side", "quantity",
    "entry_price", "exit_price", "stop_loss",
    "take_profit", "net_pnl_cad", "result",
    "setup", "score", "tier", "r_multiple",
    "rsi", "confirmations", "hold_minutes", "pnl_r"
]


# ============================================================
# KRAKEN API
# ============================================================

MARKET_SLIPPAGE = {}
MARKET_VOLUME = {}   # 24h volume in CAD
MARKET_FX = {}   # CAD per 1 unit of the market's quote currency


def fx_for(market):
    return MARKET_FX.get(market, 1.0)


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
    last_error = None

    for attempt in range(4):
        time.sleep(CALL_DELAY)

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "GWVSA-PaperTrader/8.3"}
        )

        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            last_error = error
            # Back off on rate limits / server errors, then retry.
            if error.code in (429, 500, 502, 503, 504):
                time.sleep(10 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            last_error = error
            time.sleep(5 * (attempt + 1))
            continue

        if data.get("error"):
            message = "; ".join(data["error"])
            last_error = RuntimeError(message)

            if "rate limit" in message.lower() or "busy" in message.lower():
                time.sleep(15)
                continue

            raise last_error

        return data["result"]

    raise RuntimeError(f"Kraken request failed after retries: {last_error}")


def discover_markets(held=()):
    result = api_get("AssetPairs")
    cad, usd = {}, {}
    fx_pair = None

    for key, info in result.items():
        if key.endswith(".d"):
            continue

        if info.get("status", "online") != "online":
            continue

        ws = info.get("wsname", "")

        if "/" not in ws:
            continue

        base, quote = ws.split("/")

        if ws == "USD/CAD":
            fx_pair = info.get("altname", key)
            continue

        base = "DOGE" if base == "XDG" else base
        base = "BTC" if base == "XBT" else base

        # Work out the asset class.
        if base in FOREX_BASES and quote == "USD" and base != "USD":
            cls = "forex"
        elif (
            len(base) > 1 and base.endswith("x")
            and base[:-1] == base[:-1].upper() and base[:-1].isalnum()
        ):
            cls = "stock"       # xStocks tokenized equities/ETFs
        elif base == base.upper() and base not in EXCLUDE_BASES:
            cls = "crypto"
        else:
            continue            # stablecoins, gold tokens, odd symbols

        if (
            (cls == "crypto" and not INCLUDE_CRYPTO)
            or (cls == "stock" and not INCLUDE_STOCKS)
            or (cls == "forex" and not INCLUDE_FOREX)
        ):
            continue

        # Forex and stocks are only supported against USD (or CAD
        # for crypto), since the bot converts everything to CAD.
        if cls != "crypto" and quote != "USD":
            continue

        entry = {"pair": info.get("altname", key), "key": key, "cls": cls}

        # Keep the original Kraken base name in the market label so
        # open positions from earlier versions still match (XBT, DOGE).
        label_base = "XBT" if base == "BTC" else base

        if quote == QUOTE:
            cad[f"{label_base}/{quote}"] = dict(entry, fx=1.0)
        elif USE_USD_PAIRS and quote == "USD":
            usd[f"{label_base}/{quote}"] = entry

    candidates = dict(cad)

    if usd:
        try:
            if not fx_pair:
                raise RuntimeError("USD/CAD pair not found")

            fx = get_ticker_price(fx_pair)  # CAD per USD

            for market, entry in usd.items():
                candidates[market] = dict(entry, fx=fx)
        except Exception as error:
            print("USD pairs skipped (no exchange rate):", error)

    if not candidates:
        return {}, result

    items = list(candidates.items())
    tickers = {}

    for i in range(0, len(items), 40):
        chunk = items[i:i + 40]
        tickers.update(api_get("Ticker", {
            "pair": ",".join(c["pair"] for _, c in chunk)
        }))

    # When a coin has both a CAD and a USD market, use the busier one
    # (or the one already holding a position).
    best = {}

    for market, c in candidates.items():
        t = tickers.get(c["key"]) or tickers.get(c["pair"])
        volume = (
            float(t["v"][1]) * float(t["p"][1]) * c["fx"] if t else 0.0
        )
        base = market.split("/")[0]
        rank = (market in held, volume)

        if base not in best or rank > best[base][0]:
            best[base] = (rank, volume, market, c)

    scored = []
    low_volume = 0
    class_counts = {"crypto": 0, "stock": 0, "forex": 0}

    for _, volume, market, c in best.values():
        cls = c.get("cls", "crypto")

        if market in held or volume >= MIN_24H_VOLUME_CAD[cls]:
            scored.append((volume, market, c["pair"], c["fx"]))
            class_counts[cls] += 1
        else:
            low_volume += 1

    scored.sort(reverse=True)  # most liquid first

    chosen = [x for x in scored if x[1] in held]
    others = [x for x in scored if x[1] not in held]

    if MAX_MARKETS is not None:
        others = others[:max(MAX_MARKETS - len(chosen), 0)]

    chosen += others
    chosen.sort(reverse=True)

    found = {}

    for volume, market, pair, fx in chosen:
        found[market] = pair
        MARKET_SLIPPAGE[market] = slippage_from_volume(volume)
        MARKET_VOLUME[market] = volume
        MARKET_FX[market] = fx

    print(
        f"Kraken lists {len(candidates)} usable markets; "
        f"{low_volume} skipped for low volume; scanning {len(found)}"
    )
    print(
        f"Passing volume filter: {class_counts['crypto']} crypto, "
        f"{class_counts['stock']} stocks, {class_counts['forex']} forex"
    )

    return found, result


def get_candles(pair, interval, fx=1.0):
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
            "open": float(row[1]) * fx,
            "high": float(row[2]) * fx,
            "low": float(row[3]) * fx,
            "close": float(row[4]) * fx,
            "volume": float(row[6])
        }
        for row in rows
    ]


def get_ticker_price(pair, fx=1.0):
    result = api_get("Ticker", {"pair": pair})

    if not result:
        raise RuntimeError("Ticker returned no market data")

    ticker = next(iter(result.values()))
    return float(ticker["c"][0]) * fx


def get_ask_price(pair, fx=1.0):
    result = api_get("Ticker", {"pair": pair})

    if not result:
        raise RuntimeError("Ticker returned no market data")

    ticker = next(iter(result.values()))
    ask = float(ticker["a"][0])

    return (ask if ask > 0 else float(ticker["c"][0])) * fx


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
# TREND FILTERS
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
    neutral = False

    if timeframe == "1H":
        # EMA 8 rising and price above it.
        bullish = improving and price > e8

        if ALLOW_PULLBACK_1H and not bullish:
            bullish = e8 > e40 > e200 and price > e40

        reason = (
            "Supportive 1H momentum"
            if bullish else "1H momentum rejected"
        )

    else:
        bullish = (
            price >= e200 * 0.985 and short_trend
        ) or (
            price > e200 and improving
        )

        neutral = (not bullish) and price > e8 and improving

        reason = (
            "Supportive 4H context" if bullish
            else "Neutral 4H context" if neutral
            else "Bearish 4H context"
        )

    return {
        "valid": True,
        "bullish": bullish,
        "neutral": neutral,
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

def archive_file(path, tag):
    """Move a record file into ARCHIVE_DIR with a version tag and timestamp."""
    if not os.path.exists(path):
        return

    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(os.path.basename(path))
    destination = os.path.join(ARCHIVE_DIR, f"{base}_{tag}_{stamp}{ext}")

    os.replace(path, destination)
    print(f"Archived {path} -> {destination}")


def archive_old_records():
    """If the saved state came from another bot version, archive it and
    the trade log so the new version's stats start clean."""
    if not os.path.exists(STATE_FILE):
        return

    version = None

    try:
        with open(STATE_FILE, "r") as file:
            version = json.load(file).get("version")
    except Exception:
        pass

    if version == BOT_VERSION:
        return

    tag = version or "pre-v8"
    print(f"State is from {tag}; archiving old records for {BOT_VERSION}.")

    archive_file(STATE_FILE, tag)
    archive_file(TRADE_FILE, tag)


def ensure_trade_file():
    if os.path.exists(TRADE_FILE):
        with open(TRADE_FILE, "r", newline="") as file:
            first_row = next(csv.reader(file), None)

        if first_row == TRADE_HEADERS:
            return

        # Old column layout: archive it rather than mixing formats.
        archive_file(TRADE_FILE, "oldheaders")

    with open(TRADE_FILE, "w", newline="") as file:
        csv.writer(file).writerow(TRADE_HEADERS)


def load_state():
    defaults = {
        "version": BOT_VERSION,
        "cash": STARTING_BALANCE,
        "positions": {},
        "last_signal": {},
        "last_processed_5m": {},
        "last_exit": {},
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "realized_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0
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

    # Pad short rows so every row matches the header width.
    row = list(row) + [""] * (len(TRADE_HEADERS) - len(row))

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

    # Market structure shift.
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

    # Displacement with a fair value gap.
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

    # Momentum setup.
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

    # 5-minute confirmation: best of the last 3 closed 5m candles.
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
            f"(best of last 3 candles: {confirmations}/3, "
            f"need {MIN_CONFIRMATIONS})"
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

    # Stop must clear normal noise AND round-trip trading costs.
    round_trip_cost = 2 * FEE_RATE + 2 * slip
    min_stop_pct = max(MIN_STOP_PCT, COST_STOP_MULT * round_trip_cost)
    stop = min(stop, estimated_buy * (1 - min_stop_pct))

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


def setup_score(signal):
    """Score the setup (1-11)."""
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

    return score


def r_multiple_for(score):
    for max_score, r_multiple, label in R_TIERS:
        if score <= max_score:
            return r_multiple, label

    return R_TIERS[-1][1], R_TIERS[-1][2]


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

    # Liquidity cap: don't be a big share of a thin market's daily volume.
    volume = MARKET_VOLUME.get(market)

    if volume is not None:
        quantity = min(quantity, MAX_VOLUME_SHARE * volume / buy_price)

    if quantity <= 0:
        print(f"{market}: insufficient paper cash")
        return

    notional = quantity * buy_price
    entry_fee = notional * FEE_RATE
    total_cost = notional + entry_fee

    if total_cost > state["cash"]:
        print(f"{market}: insufficient cash after fee")
        return

    score = setup_score(signal)
    r_multiple, tier = r_multiple_for(score)

    # Target sits r_multiple x the stop distance above entry, capped
    # at MAX_TARGET_MOVE_PCT.
    target = buy_price + r_multiple * risk_per_unit
    target = min(target, buy_price * (1 + MAX_TARGET_MOVE_PCT))

    net_at_target = (
        quantity * target * (1 - slip) * (1 - FEE_RATE) - total_cost
    )
    net_at_stop = quantity * loss_per_unit

    if net_at_target < MIN_NET_PROFIT:
        print(
            f"{market}: skipped - target nets only "
            f"${net_at_target:.2f} (min ${MIN_NET_PROFIT:.2f})"
        )
        return

    if net_at_target < MIN_NET_RR * net_at_stop:
        print(
            f"{market}: skipped - net reward/risk "
            f"{net_at_target / net_at_stop:.2f} below {MIN_NET_RR}"
        )
        return

    target_profit = round(net_at_target, 2)

    state["cash"] -= total_cost

    position = {
        "quantity": quantity,
        "entry": buy_price,
        "stop": stop,
        "target": target,
        "risk": risk_per_unit,
        "atr": signal.get("atr"),
        "tier": tier,
        "score": score,
        "r_multiple": r_multiple,
        "target_profit": target_profit,
        "net_at_stop": net_at_stop,
        "setup": signal.get("setup"),
        "rsi": signal.get("rsi"),
        "confirmations": signal.get("confirmations"),
        "entry_cost": total_cost,
        "entry_time": int(time.time()),
        "signal_time": signal["candle_time"]
    }

    state["positions"][market] = position
    state["last_signal"][market] = signal["candle_time"]
    state["last_processed_5m"][market] = latest_5m_time

    save_state(state)

    rsi_value = signal.get("rsi")

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
        f"OPEN_{signal['setup']}_{tier}_score{score}_{r_multiple}R",
        signal.get("setup", ""),
        score,
        tier,
        r_multiple,
        round(rsi_value, 2) if rsi_value else "",
        signal.get("confirmations", ""),
        "",
        ""
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
    print(
        f"Opportunity:   {tier} score {score}, {r_multiple}R "
        f"(net target ${target_profit:.2f}, "
        f"net loss at stop ${net_at_stop:.2f} CAD)"
    )
    print(f"Position cost: ${total_cost:.4f} CAD")
    print("================================")


# ============================================================
# PAPER SELL
# ============================================================

def close_position(state, market, exit_reference, reason, exit_time=None):
    position = state["positions"][market]

    sell_price = exit_reference * (1 - slippage_for(market))
    proceeds = position["quantity"] * sell_price
    exit_fee = proceeds * FEE_RATE

    state["cash"] += proceeds - exit_fee

    net_pnl = proceeds - exit_fee - position["entry_cost"]

    state["realized_pnl"] += net_pnl
    state["total_trades"] += 1

    # Start the cooldown clock after a stop-out.
    if reason.startswith(("STOP_LOSS", "TRAILING_STOP")):
        state["last_exit"][market] = time.time()

    if net_pnl >= 0:
        state["winning_trades"] += 1
        state["gross_profit"] += net_pnl
    else:
        state["losing_trades"] += 1
        state["gross_loss"] += -net_pnl

    # Hold time uses the exit candle's close when we know it, so a run
    # that replays old candles doesn't overstate how long the trade lasted.
    entry_time = position.get("entry_time")
    finished = exit_time if exit_time else time.time()
    hold_minutes = (
        round(max(finished - entry_time, 0) / 60, 1) if entry_time else ""
    )

    planned_loss = position.get("net_at_stop")
    pnl_r = round(net_pnl / planned_loss, 2) if planned_loss else ""
    rsi_value = position.get("rsi")

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
        position.get("setup", ""),
        position.get("score", ""),
        position.get("tier", ""),
        position.get("r_multiple", ""),
        round(rsi_value, 2) if rsi_value else "",
        position.get("confirmations", ""),
        hold_minutes,
        pnl_r
    ])

    print("\n========== PAPER SELL ==========")
    print(f"Market:       {market}")
    print(f"Reason:       {reason}")
    print(f"Sell price:   {sell_price:.8f}")
    print(f"NET P/L:      ${net_pnl:.4f} CAD")
    if pnl_r != "":
        print(f"P/L in R:     {pnl_r}R")
    if hold_minutes != "":
        print(f"Held:         {hold_minutes} min")
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
                current_price = get_ticker_price(pair, fx_for(market))
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

    if last_time < oldest_available - 300:
        print(
            f"WARNING {market}: candle history gap "
            "exceeds available history."
        )

        try:
            current_price = get_ticker_price(pair, fx_for(market))
            reason = "GAP_EXIT_CURRENT_TICKER"
        except Exception as error:
            print("Ticker fallback error:", error)
            current_price = candles5[-1]["close"]
            reason = "GAP_EXIT_LAST_COMPLETED_CANDLE"

        close_position(state, market, current_price, reason)
        return

    # Only replay candles that opened at or after entry. The candle that
    # was still forming when we bought includes price action from before
    # the entry, so it must not be allowed to trigger the stop.
    entry_time = position.get("entry_time", 0)

    new_candles = [
        c for c in candles5
        if c["time"] > last_time and c["time"] >= entry_time
    ]

    if not new_candles:
        print(f"{market}: waiting for new 5m candle")
        return

    print(
        f"{market}: replaying {len(new_candles)} "
        "missed/new 5m candle(s)"
    )

    position.setdefault("risk", position["entry"] - position["stop"])

    for candle in new_candles:
        stop_hit = candle["low"] <= position["stop"]
        target_hit = candle["high"] >= position["target"]
        candle_close_time = candle["time"] + 300

        # Stop first if both were reached in one candle (pessimistic).
        if stop_hit:
            reason = (
                "TRAILING_STOP"
                if position["stop"] >= position["entry"]
                else "STOP_LOSS"
            )
            if candle["open"] < position["stop"]:
                reason += "_GAP"

            fill = min(position["stop"], candle["open"])
            close_position(
                state, market, fill, reason, candle_close_time
            )
            return

        if target_hit:
            fill = max(position["target"], candle["open"])
            close_position(
                state, market, fill, "TAKE_PROFIT", candle_close_time
            )
            return

        # Breakeven, then trail the high (only when enabled).
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
    print(f"Bot version:      {BOT_VERSION}")
    print(f"Starting balance: ${STARTING_BALANCE:.2f} CAD")
    print(f"Available cash:   ${state['cash']:.2f} CAD")
    print(f"Open positions:   {len(state['positions'])}")
    print(f"Closed trades:    {state['total_trades']}")
    print(f"Wins:             {state['winning_trades']}")
    print(f"Losses:           {state['losing_trades']}")

    if state["total_trades"]:
        wins = state["winning_trades"]
        losses = state["losing_trades"]
        rate = wins / state["total_trades"]

        print(f"Win rate:         {rate:.0%}")

        avg_win = state["gross_profit"] / wins if wins else 0.0
        avg_loss = state["gross_loss"] / losses if losses else 0.0
        expectancy = state["realized_pnl"] / state["total_trades"]

        print(f"Average win:      ${avg_win:.2f} CAD")
        print(f"Average loss:     ${avg_loss:.2f} CAD")
        print(f"Expectancy/trade: ${expectancy:.2f} CAD")

        if state["gross_loss"] > 0:
            factor = state["gross_profit"] / state["gross_loss"]
            print(f"Profit factor:    {factor:.2f}")

        if avg_win > 0 and avg_loss > 0:
            needed = avg_loss / (avg_win + avg_loss)
            print(f"Break-even win rate at these averages: {needed:.0%}")

        if state["total_trades"] < 100:
            print(
                f"Note: only {state['total_trades']} trades; "
                "wait for 100+ before judging the strategy."
            )

    print(f"Realized P/L:     ${state['realized_pnl']:.2f} CAD")

    open_value = 0.0

    for market, position in state["positions"].items():
        price = latest_prices.get(market, position["entry"])
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
    print(f"\nGWVSA PAPER TRADING BOT {BOT_VERSION} (ALL MARKETS)")
    print("Paper trading only. No real Kraken orders.")
    print("Time:", datetime.now(timezone.utc).isoformat())
    print(
        f"Settings: trailing stop {'ON' if USE_TRAILING_STOP else 'OFF'}, "
        f"momentum entries {'ON' if ALLOW_MOMENTUM_ENTRY else 'OFF'}, "
        f"min confirmations {MIN_CONFIRMATIONS}/3, "
        f"1H pullbacks {'ON' if ALLOW_PULLBACK_1H else 'OFF'}, "
        f"cooldown {COOLDOWN_SECONDS / 3600:.0f}h"
    )
    print(
        f"Sizing: risk ${RISK_PER_TRADE:.2f}/trade, "
        f"max position ${MAX_POSITION_VALUE:.2f}, "
        f"max open {MAX_OPEN_POSITIONS}"
    )

    if FORCE_TEST_TRADE:
        print(f"FORCE_TEST_TRADE is ON ({FORCE_TEST_MARKET})")

    archive_old_records()
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
    print(f"\nScanning {len(markets)} markets")

    for market in state["positions"]:
        if market not in pairs:
            print(f"WARNING {market}: open position but pair not found")

    # Manage every open position first, so exits free up slots
    # before we look for new entries.
    ordered = (
        [m for m in markets if m in state["positions"]]
        + [m for m in markets if m not in state["positions"]]
    )

    for index, market in enumerate(ordered):
        # Slots are full and this market isn't held. Held markets are
        # sorted first, so everything left is also a skip: stop here
        # and log it once instead of once per market.
        if (
            market not in state["positions"]
            and len(state["positions"]) >= MAX_OPEN_POSITIONS
        ):
            print(
                f"\nMax open positions ({MAX_OPEN_POSITIONS}) reached; "
                f"skipping {len(ordered) - index} remaining markets"
            )
            break

        pair = pairs[market]
        fx = fx_for(market)
        print(f"\n######## CHECKING {market} ########")

        try:
            forced = FORCE_TEST_TRADE and market == FORCE_TEST_MARKET

            if market in state["positions"]:
                candles5 = get_candles(pair, 5, fx)

                if candles5:
                    latest_prices[market] = candles5[-1]["close"]

                manage_position(state, market, pair, candles5)

                if market in state["positions"]:
                    print(f"{market}: existing position remains open")
                    continue

            # Cooldown after a stop-out: don't chase the same market.
            if (
                not forced
                and time.time() - state["last_exit"].get(market, 0)
                < COOLDOWN_SECONDS
            ):
                print(f"{market}: cooling down after stop-out")
                continue

            four_h = False

            if not forced:
                # Cheap 1H check first.
                report1h = trend_report(get_candles(pair, 60, fx), "1H")
                print_trend(market, "1H", report1h)

                if not report1h["valid"] or not report1h["bullish"]:
                    print(f"{market}: 1H trend rejected")
                    continue

                                four_h = report4h["valid"] and report4h["bullish"]
                four_h_ok = four_h or (
                    ALLOW_NEUTRAL_4H
                    and report4h["valid"]
                    and report4h.get("neutral", False)
                )

                if REQUIRE_4H_BULLISH and not four_h_ok:
                    print(f"{market}: 4H not bullish; skipping")
                    continue


                if REQUIRE_4H_BULLISH and not four_h:
                    print(f"{market}: 4H not bullish; skipping")
                    continue


            candles15 = get_candles(pair, 15, fx)
            candles5 = get_candles(pair, 5, fx)

            if candles5:
                latest_prices[market] = candles5[-1]["close"]

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
            ask = get_ask_price(pair, fx)
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
