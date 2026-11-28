"""PEPE/USD backtest: one trade per UTC day, moving average rule, fees included. Public data only."""
import json
import urllib.request

START_BALANCE = 2000.0
STOP_PCT = 0.04      # stop 4% from entry
TARGET_PCT = 0.08    # target 8% from entry
FEE = 0.004          # assumed fee per side
PAIR = "PEPEUSD"


def daily_candles():
    url = f"https://api.kraken.com/0/public/OHLC?pair={PAIR}&interval=1440"
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.load(r)
    if d.get("error"):
        raise RuntimeError(d["error"])
    rows = next(v for k, v in d["result"].items() if k != "last")
    return [(float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows[:-1]]


def ema(values, n):
    out, k = values[0], 2 / (n + 1)
    for v in values[1:]:
        out += k * (v - out)
    return out


def run():
    bars = daily_candles()
    balance = peak = START_BALANCE
    max_dd = 0.0
    results = []
    for i in range(50, len(bars)):
        closes = [b[3] for b in bars[:i]]
        side = 1 if ema(closes, 20) >= ema(closes, 50) else -1
        entry, high, low, close = bars[i]
        stop = entry * (1 - side * STOP_PCT)
        target = entry * (1 + side * TARGET_PCT)
        qty = min(START_BALANCE, balance) / entry
        # If a day touches both levels, assume the stop came first.
        if (low <= stop) if side == 1 else (high >= stop):
            exit_price = stop
        elif (high >= target) if side == 1 else (low <= target):
            exit_price = target
        else:
            exit_price = close
        pnl = qty * (exit_price - entry) * side - qty * (exit_price + entry) * FEE
        balance += pnl
        results.append(pnl)
        peak = max(peak, balance)
        max_dd = max(max_dd, (peak - balance) / peak)

    if not results:
        print("Not enough price history.")
        return
    wins = [p for p in results if p > 0]
    losses = [-p for p in results if p <= 0]
    print(f"PEPE backtest, {len(results)} daily trades")
    print(f"Start ${START_BALANCE:.2f} -> End ${balance:.2f} (net ${balance - START_BALANCE:+.2f})")
    print(f"Win rate {100 * len(wins) / len(results):.1f}% | wins {len(wins)} | losses {len(losses)}")
    if wins:
        print(f"Average win ${sum(wins) / len(wins):.2f}")
    if losses:
        print(f"Average loss ${sum(losses) / len(losses):.2f}")
    print(f"Biggest drop from a peak: {100 * max_dd:.1f}%")


if __name__ == "__main__":
    run()
