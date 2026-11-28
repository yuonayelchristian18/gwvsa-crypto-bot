"""Gold (PAXG/USD) paper bot: one trade per UTC day. Public data only, no real orders."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

START_BALANCE = 2000.0
RISK_USD = 20.0     # planned max loss per trade, fees included
STOP_PCT = 0.01     # stop 1% from entry
TARGET_PCT = 0.03   # target 3% from entry
FEE = 0.004         # assumed fee per side; check your Kraken tier
STATE = "gold_state.json"


def fetch(url, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    with urllib.request.urlopen(url, data=body, timeout=25) as r:
        return json.load(r)


def candles(minutes):
    d = fetch(f"https://api.kraken.com/0/public/OHLC?pair=PAXGUSD&interval={minutes}")
    if d.get("error"):
        raise RuntimeError(d["error"])
    rows = next(v for k, v in d["result"].items() if k != "last")
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]


def ema(values, n):
    out, k = values[0], 2 / (n + 1)
    for v in values[1:]:
        out += k * (v - out)
    return out


def close_trade(s, price, reason):
    p = s["position"]
    gross = p["qty"] * (price - p["entry"]) * p["side"]
    pnl = gross - p["qty"] * (price + p["entry"]) * FEE
    s["balance"] += pnl
    s["trades"].append({**p, "exit": price, "pnl": round(pnl, 2), "reason": reason})
    s["position"] = None
    return f"CLOSED ({reason}) at {price:.2f}, P/L ${pnl:+.2f}"


def main():
    try:
        with open(STATE) as f:
            s = json.load(f)
    except FileNotFoundError:
        s = {"balance": START_BALANCE, "position": None, "trades": [], "last_day": ""}

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    bars = candles(15)
    now_ts, price = bars[-1][0], bars[-1][4]
    messages = []

    p = s["position"]
    if p:
        side = p["side"]
        for t, o, high, low, _ in bars[:-1]:
            if t <= p["checked"]:
                continue
            p["checked"] = t
            worst, best = (low, high) if side == 1 else (high, low)
            if (worst - p["stop"]) * side <= 0:
                fill = o if (o - p["stop"]) * side < 0 else p["stop"]  # gaps fill worse
                messages.append(close_trade(s, fill, "stop"))
                break
            if (best - p["target"]) * side >= 0:
                messages.append(close_trade(s, p["target"], "target"))
                break
    if s["position"] and s["position"]["day"] != today:
        messages.append(close_trade(s, price, "end of day"))

    if not s["position"] and s["last_day"] != today:
        closes = [b[4] for b in candles(60)[:-1]]
        side = 1 if ema(closes, 20) >= ema(closes, 50) else -1
        size = RISK_USD / (STOP_PCT + 2 * FEE)
        s["position"] = {"day": today, "side": side, "entry": price, "qty": size / price,
                         "stop": price * (1 - side * STOP_PCT),
                         "target": price * (1 + side * TARGET_PCT), "checked": now_ts}
        s["last_day"] = today
        q = s["position"]
        messages.append(f"OPENED {'LONG' if side == 1 else 'SHORT'} ${size:.0f} at {price:.2f}, "
                        f"stop {q['stop']:.2f}, target {q['target']:.2f}")

    q = s["position"]
    if q:
        gross = q["qty"] * (price - q["entry"]) * q["side"]
        net = gross - q["qty"] * (price + q["entry"]) * FEE
        print(f"OPEN: price {price:.2f}, P/L if closed now ${net:+.2f}")

    with open(STATE, "w") as f:
        json.dump(s, f, indent=1)

    wins = sum(t["pnl"] > 0 for t in s["trades"])
    report = "\n".join(["GOLD paper bot (PAXG/USD)", *messages,
                        f"Balance ${s['balance']:.2f} | closed {len(s['trades'])} | wins {wins}"])
    print(report)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if messages and token and chat:
        fetch(f"https://api.telegram.org/bot{token}/sendMessage", {"chat_id": chat, "text": report})


if __name__ == "__main__":
    main()
