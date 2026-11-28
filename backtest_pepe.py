"""PEPE/USD V2 research backtest.
Kraken public OHLC only. No API keys and no real orders.

Notes:
- Kraken OHLC returns at most the most recent 720 candles per request.
- This script therefore tests only the available recent 15-minute history.
- Signals use completed candles; entries occur at the next candle open.
- If stop and target are both touched in one candle, stop is assumed first.
- Strategy is a GWVSA-inspired baseline, not a full SMT/order-flow implementation.
"""

import json
import math
import urllib.request
from datetime import datetime, timezone

PAIR = "PEPEUSD"
INTERVAL = 15

START_BALANCE = 2000.0
RISK_USD = 15.0
MAX_EXPOSURE = 1000.0
MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT = 40.0
MAX_HOLD_BARS = 96             # 24 hours on 15-minute candles

ATR_PERIOD = 14
STOP_ATR_MULTIPLE = 1.5
TAKE_PROFIT_R = 2.5

FEE_PER_SIDE = 0.004           # 0.4%; replace with your actual Kraken tier
SLIPPAGE_PER_SIDE = 0.0015     # 0.15% assumed each way
MIN_WARMUP = 220


def fetch_ohlc():
    url = (
        "https://api.kraken.com/0/public/OHLC"
        f"?pair={PAIR}&interval={INTERVAL}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "PEPE-paper-backtest/2.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)

    if data.get("error"):
        raise RuntimeError(f"Kraken API error: {data['error']}")

    pair_key = next(k for k in data["result"] if k != "last")
    rows = data["result"][pair_key]

    # Kraken's final row is the currently forming candle; exclude it.
    bars = []
    for row in rows[:-1]:
        bars.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[6]),
        })
    return bars


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for x in values[period:]:
        value += alpha * (x - value)
    return value


def atr(bars, index, period=ATR_PERIOD):
    if index < period:
        return None
    trs = []
    start = index - period + 1
    for j in range(start, index + 1):
        prev_close = bars[j - 1]["close"]
        tr = max(
            bars[j]["high"] - bars[j]["low"],
            abs(bars[j]["high"] - prev_close),
            abs(bars[j]["low"] - prev_close),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


def aggregate_completed(bars, minutes):
    """Aggregate 15m bars into completed higher-timeframe candles."""
    seconds = minutes * 60
    groups = {}
    for b in bars:
        bucket = (b["time"] // seconds) * seconds
        g = groups.setdefault(bucket, {
            "time": bucket,
            "open": b["open"],
            "high": b["high"],
            "low": b["low"],
            "close": b["close"],
            "volume": 0.0,
            "count": 0,
        })
        g["high"] = max(g["high"], b["high"])
        g["low"] = min(g["low"], b["low"])
        g["close"] = b["close"]
        g["volume"] += b["volume"]
        g["count"] += 1

    expected = minutes // INTERVAL
    result = []
    for g in groups.values():
        if g["count"] == expected:
            result.append(g)
    return sorted(result, key=lambda x: x["time"])


def latest_completed_index(higher_bars, timestamp):
    """Last higher-timeframe candle whose close is no later than timestamp."""
    found = -1
    for i, b in enumerate(higher_bars):
        if b["time"] + 4 * 60 * 60 <= timestamp:
            found = i
        else:
            break
    return found


def trend_side(higher_bars, timestamp, fast=20, slow=50, minutes=60):
    # Only use candles fully closed before the signal timestamp.
    seconds = minutes * 60
    eligible = [b["close"] for b in higher_bars if b["time"] + seconds <= timestamp]
    if len(eligible) < slow:
        return 0
    fast_ema = ema(eligible, fast)
    slow_ema = ema(eligible, slow)
    return 1 if fast_ema > slow_ema else -1 if fast_ema < slow_ema else 0


def signal_at(bars, i, hourly, four_hourly):
    """Return +1 long, -1 short, or 0. Signal is evaluated at bar i close."""
    closes = [b["close"] for b in bars[:i + 1]]
    if len(closes) < MIN_WARMUP:
        return 0

    e8 = ema(closes, 8)
    e40 = ema(closes, 40)
    e200 = ema(closes, 200)
    if e8 is None or e40 is None or e200 is None:
        return 0

    side = 1 if e8 > e40 > e200 else -1 if e8 < e40 < e200 else 0
    if side == 0:
        return 0

    ts = bars[i]["time"] + INTERVAL * 60
    h_side = trend_side(hourly, ts, 20, 50, 60)
    h4_side = trend_side(four_hourly, ts, 20, 50, 240)
    if side != h_side or side != h4_side:
        return 0

    # Liquidity sweep: current completed candle pierces the prior 20-bar
    # extreme and closes back inside that prior range.
    lookback = bars[i - 20:i]
    prior_high = max(b["high"] for b in lookback)
    prior_low = min(b["low"] for b in lookback)
    current = bars[i]
    bullish_sweep = current["low"] < prior_low and current["close"] > prior_low
    bearish_sweep = current["high"] > prior_high and current["close"] < prior_high

    # Simple reversal confirmation: candle closes in direction of the trend.
    bullish_confirm = current["close"] > current["open"] and current["close"] > bars[i - 1]["high"]
    bearish_confirm = current["close"] < current["open"] and current["close"] < bars[i - 1]["low"]

    if side == 1 and bullish_sweep and bullish_confirm:
        return 1
    if side == -1 and bearish_sweep and bearish_confirm:
        return -1
    return 0


def execute_price(raw_price, side, is_entry):
    # Adverse slippage: pay more on buys and receive less on sells.
    buy = (side == 1 and is_entry) or (side == -1 and not is_entry)
    return raw_price * (1 + SLIPPAGE_PER_SIDE if buy else 1 - SLIPPAGE_PER_SIDE)


def close_position(position, raw_exit, reason, balance):
    side = position["side"]
    entry_fill = position["entry_fill"]
    exit_fill = execute_price(raw_exit, side, False)
    qty = position["qty"]
    gross = qty * (exit_fill - entry_fill) * side
    fees = qty * (entry_fill + exit_fill) * FEE_PER_SIDE
    pnl = gross - fees
    balance += pnl
    return balance, {
        "day": position["day"],
        "side": "LONG" if side == 1 else "SHORT",
        "entry": round(position["entry_fill"], 10),
        "exit": round(exit_fill, 10),
        "qty": round(qty, 2),
        "pnl": round(pnl, 2),
        "reason": reason,
    }


def run():
    bars = fetch_ohlc()
    if len(bars) < MIN_WARMUP + 50:
        print(f"Not enough completed 15m candles: {len(bars)}")
        return

    hourly = aggregate_completed(bars, 60)
    four_hourly = aggregate_completed(bars, 240)

    balance = START_BALANCE
    peak = START_BALANCE
    max_drawdown = 0.0
    trades = []
    position = None
    daily_count = {}
    daily_realized = {}

    for i in range(MIN_WARMUP, len(bars) - 1):
        bar = bars[i]
        day = datetime.fromtimestamp(bar["time"], timezone.utc).strftime("%Y-%m-%d")

        # Manage an existing position using this completed candle.
        if position:
            side = position["side"]
            stop_hit = bar["low"] <= position["stop"] if side == 1 else bar["high"] >= position["stop"]
            target_hit = bar["high"] >= position["target"] if side == 1 else bar["low"] <= position["target"]

            if stop_hit:
                raw_exit = min(bar["open"], position["stop"]) if side == 1 else max(bar["open"], position["stop"])
                balance, trade = close_position(position, raw_exit, "stop", balance)
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None
            elif target_hit:
                balance, trade = close_position(position, position["target"], "target", balance)
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None
            elif i - position["entry_index"] >= MAX_HOLD_BARS:
                balance, trade = close_position(position, bar["close"], "time_exit", balance)
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None

        peak = max(peak, balance)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - balance) / peak)

        # Only open after a signal on this candle, at the next candle's open.
        if position is not None:
            continue
        if daily_count.get(day, 0) >= MAX_TRADES_PER_DAY:
            continue
        if daily_realized.get(day, 0.0) <= -DAILY_LOSS_LIMIT:
            continue
        if balance <= 0:
            break

        side = signal_at(bars, i, hourly, four_hourly)
        if side == 0:
            continue

        a = atr(bars, i)
        if a is None or a <= 0:
            continue

        next_open = bars[i + 1]["open"]
        entry_fill = execute_price(next_open, side, True)
        stop_distance = STOP_ATR_MULTIPLE * a
        stop_pct = stop_distance / entry_fill
        # Approximate risk includes round-trip fees and slippage.
        cost_fraction = 2 * FEE_PER_SIDE + 2 * SLIPPAGE_PER_SIDE
        risk_fraction = stop_pct + cost_fraction
        if risk_fraction <= 0:
            continue

        risk_budget = min(RISK_USD, max(0.0, balance * 0.01))
        notional = min(risk_budget / risk_fraction, MAX_EXPOSURE, balance)
        if notional <= 0:
            continue
        qty = notional / entry_fill

        stop = entry_fill - side * stop_distance
        target = entry_fill + side * stop_distance * TAKE_PROFIT_R

        position = {
            "side": side,
            "day": day,
            "entry_index": i + 1,
            "entry_fill": entry_fill,
            "qty": qty,
            "stop": stop,
            "target": target,
        }
        daily_count[day] = daily_count.get(day, 0) + 1

    # Mark any remaining position to the last completed close.
    if position:
        last = bars[-1]
        balance, trade = close_position(position, last["close"], "end_of_data", balance)
        trades.append(trade)

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    total = len(trades)
    net = balance - START_BALANCE
    profit_factor = sum(wins) / sum(losses) if losses else float("inf")
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0

    print("PEPE V2 BACKTEST — PAPER ONLY")
    print(f"Pair: {PAIR} | timeframe: {INTERVAL}m")
    print(f"Completed candles tested: {len(bars)}")
    print(f"Start ${START_BALANCE:.2f} -> End ${balance:.2f} | Net ${net:+.2f}")
    print(f"Closed trades: {total} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {100 * len(wins) / total:.1f}%" if total else "Win rate: n/a")
    print(f"Average win: ${avg_win:.2f} | Average loss: ${avg_loss:.2f}")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Maximum drawdown: {100 * max_drawdown:.1f}%")
    print(f"Assumed fee/side: {100 * FEE_PER_SIDE:.2f}% | slippage/side: {100 * SLIPPAGE_PER_SIDE:.2f}%")

    print("\\nLast 10 trades:")
    for t in trades[-10:]:
        print(
            f"{t['day']} {t['side']} P/L ${t['pnl']:+.2f} "
            f"({t['reason']}) entry {t['entry']:.10f} exit {t['exit']:.10f}"
        )

    with open("pepe_v2_results.json", "w") as f:
        json.dump({
            "pair": PAIR,
            "start_balance": START_BALANCE,
            "end_balance": round(balance, 2),
            "net": round(net, 2),
            "trades": trades,
        }, f, indent=2)
    print("\\nSaved trade details to pepe_v2_results.json")


if __name__ == "__main__":
    run()
