"""PEPE/USD V2.1 research backtest.
Kraken public OHLC only. No API keys and no real orders.

Fetches 15m, 1h and 4h candles independently so the 4h EMA-50
has enough history. Kraken's public OHLC endpoint returns up to
720 recent candles per interval; this is still a limited sample.

Signals use completed candles; entries occur at the next 15m open.
If stop and target are both touched in one candle, stop is assumed first.
"""

import json
import urllib.request
from datetime import datetime, timezone

PAIR = "PEPEUSD"
START_BALANCE = 2000.0
RISK_USD = 15.0
MAX_EXPOSURE = 1000.0
MAX_TRADES_PER_DAY = 3
DAILY_LOSS_LIMIT = 40.0
MAX_HOLD_BARS = 96

ATR_PERIOD = 14
STOP_ATR_MULTIPLE = 1.5
TAKE_PROFIT_R = 2.5
FEE_PER_SIDE = 0.004
SLIPPAGE_PER_SIDE = 0.0015
MIN_WARMUP_15M = 220
MIN_WARMUP_4H = 55


def fetch_ohlc(interval):
    url = (
        "https://api.kraken.com/0/public/OHLC"
        f"?pair={PAIR}&interval={interval}"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "PEPE-paper-backtest/2.1"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)

    if data.get("error"):
        raise RuntimeError(f"Kraken API error ({interval}m): {data['error']}")

    pair_key = next(k for k in data["result"] if k != "last")
    rows = data["result"][pair_key]
    # Exclude the currently forming final candle.
    return [
        {
            "time": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[6]),
        }
        for r in rows[:-1]
    ]


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
    true_ranges = []
    for j in range(index - period + 1, index + 1):
        prev_close = bars[j - 1]["close"]
        true_ranges.append(max(
            bars[j]["high"] - bars[j]["low"],
            abs(bars[j]["high"] - prev_close),
            abs(bars[j]["low"] - prev_close),
        ))
    return sum(true_ranges) / len(true_ranges)


def trend_side(higher_bars, timestamp, minutes, fast=20, slow=50):
    # A higher-timeframe candle is usable only after its close time.
    closes = [
        b["close"] for b in higher_bars
        if b["time"] + minutes * 60 <= timestamp
    ]
    if len(closes) < slow:
        return 0
    fast_value = ema(closes, fast)
    slow_value = ema(closes, slow)
    if fast_value is None or slow_value is None:
        return 0
    return 1 if fast_value > slow_value else -1 if fast_value < slow_value else 0


def signal_at(bars15, bars1h, bars4h, i):
    closes = [b["close"] for b in bars15[:i + 1]]
    e8, e40, e200 = ema(closes, 8), ema(closes, 40), ema(closes, 200)
    if None in (e8, e40, e200):
        return 0

    side = 1 if e8 > e40 > e200 else -1 if e8 < e40 < e200 else 0
    if side == 0:
        return 0

    signal_time = bars15[i]["time"] + 15 * 60
    side_1h = trend_side(bars1h, signal_time, 60)
    side_4h = trend_side(bars4h, signal_time, 240)
    if side != side_1h or side != side_4h:
        return 0

    prior = bars15[i - 20:i]
    if len(prior) < 20:
        return 0
    prior_high = max(b["high"] for b in prior)
    prior_low = min(b["low"] for b in prior)
    current = bars15[i]
    previous = bars15[i - 1]

    bullish_sweep = current["low"] < prior_low and current["close"] > prior_low
    bearish_sweep = current["high"] > prior_high and current["close"] < prior_high
    bullish_confirm = (
        current["close"] > current["open"]
        and current["close"] > previous["high"]
    )
    bearish_confirm = (
        current["close"] < current["open"]
        and current["close"] < previous["low"]
    )

    if side == 1 and bullish_sweep and bullish_confirm:
        return 1
    if side == -1 and bearish_sweep and bearish_confirm:
        return -1
    return 0


def slipped_price(price, side, is_entry):
    is_buy = (side == 1 and is_entry) or (side == -1 and not is_entry)
    return price * (1 + SLIPPAGE_PER_SIDE if is_buy else 1 - SLIPPAGE_PER_SIDE)


def close_position(position, raw_exit, reason, balance):
    side = position["side"]
    exit_fill = slipped_price(raw_exit, side, False)
    qty = position["qty"]
    gross = qty * (exit_fill - position["entry_fill"]) * side
    fees = qty * (position["entry_fill"] + exit_fill) * FEE_PER_SIDE
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
    bars15 = fetch_ohlc(15)
    bars1h = fetch_ohlc(60)
    bars4h = fetch_ohlc(240)

    print(
        f"Data loaded: 15m={len(bars15)}, 1h={len(bars1h)}, "
        f"4h={len(bars4h)} completed candles"
    )

    if len(bars15) < MIN_WARMUP_15M + 2:
        print("Not enough 15m history for EMA-200.")
        return
    if len(bars4h) < MIN_WARMUP_4H:
        print("Not enough 4h history for EMA-50. Cannot test this filter reliably.")
        return

    balance = START_BALANCE
    peak = START_BALANCE
    max_drawdown = 0.0
    trades = []
    position = None
    daily_count = {}
    daily_realized = {}
    signal_checks = 0
    trend_aligned = 0
    valid_signals = 0

    for i in range(MIN_WARMUP_15M, len(bars15) - 1):
        bar = bars15[i]
        day = datetime.fromtimestamp(bar["time"], timezone.utc).strftime("%Y-%m-%d")

        if position:
            side = position["side"]
            stop_hit = (
                bar["low"] <= position["stop"] if side == 1
                else bar["high"] >= position["stop"]
            )
            target_hit = (
                bar["high"] >= position["target"] if side == 1
                else bar["low"] <= position["target"]
            )

            if stop_hit:
                raw_exit = (
                    min(bar["open"], position["stop"]) if side == 1
                    else max(bar["open"], position["stop"])
                )
                balance, trade = close_position(position, raw_exit, "stop", balance)
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None
            elif target_hit:
                balance, trade = close_position(
                    position, position["target"], "target", balance
                )
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None
            elif i - position["entry_index"] >= MAX_HOLD_BARS:
                balance, trade = close_position(
                    position, bar["close"], "time_exit", balance
                )
                trades.append(trade)
                daily_realized[day] = daily_realized.get(day, 0.0) + trade["pnl"]
                position = None

        peak = max(peak, balance)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - balance) / peak)

        if position is not None:
            continue
        if daily_count.get(day, 0) >= MAX_TRADES_PER_DAY:
            continue
        if daily_realized.get(day, 0.0) <= -DAILY_LOSS_LIMIT or balance <= 0:
            continue

        signal_checks += 1
        side = signal_at(bars15, bars1h, bars4h, i)
        if side:
            valid_signals += 1
        else:
            continue

        a = atr(bars15, i)
        if a is None or a <= 0:
            continue

        entry_fill = slipped_price(bars15[i + 1]["open"], side, True)
        stop_distance = STOP_ATR_MULTIPLE * a
        stop_pct = stop_distance / entry_fill
        estimated_cost_fraction = 2 * FEE_PER_SIDE + 2 * SLIPPAGE_PER_SIDE
        risk_fraction = stop_pct + estimated_cost_fraction
        if risk_fraction <= 0:
            continue

        risk_budget = min(RISK_USD, balance * 0.01)
        notional = min(risk_budget / risk_fraction, MAX_EXPOSURE, balance)
        if notional <= 0:
            continue

        qty = notional / entry_fill
        position = {
            "side": side,
            "day": day,
            "entry_index": i + 1,
            "entry_fill": entry_fill,
            "qty": qty,
            "stop": entry_fill - side * stop_distance,
            "target": entry_fill + side * stop_distance * TAKE_PROFIT_R,
        }
        daily_count[day] = daily_count.get(day, 0) + 1

    if position:
        balance, trade = close_position(
            position, bars15[-1]["close"], "end_of_data", balance
        )
        trades.append(trade)

    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    total = len(trades)
    gross_wins = sum(wins)
    gross_losses = sum(losses)
    profit_factor = gross_wins / gross_losses if gross_losses else (
        float("inf") if gross_wins else 0.0
    )

    print("\\nPEPE V2.1 BACKTEST — PAPER ONLY")
    print(f"Pair: {PAIR} | signal timeframe: 15m")
    print(f"Start ${START_BALANCE:.2f} -> End ${balance:.2f} | Net ${balance - START_BALANCE:+.2f}")
    print(f"Signal checks: {signal_checks} | qualifying entries: {valid_signals}")
    print(f"Closed trades: {total} | Wins: {len(wins)} | Losses: {len(losses)}")
    print(f"Win rate: {100 * len(wins) / total:.1f}%" if total else "Win rate: n/a")
    print(f"Average win: ${sum(wins) / len(wins):.2f}" if wins else "Average win: n/a")
    print(f"Average loss: ${sum(losses) / len(losses):.2f}" if losses else "Average loss: n/a")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Maximum drawdown: {100 * max_drawdown:.1f}%")
    print(f"Fees/side: {100 * FEE_PER_SIDE:.2f}% | slippage/side: {100 * SLIPPAGE_PER_SIDE:.2f}%")

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
            "net": round(balance - START_BALANCE, 2),
            "trades": trades,
        }, f, indent=2)
    print("\\nSaved trade details to pepe_v2_results.json")


if __name__ == "__main__":
    run()
