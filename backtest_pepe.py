"""GWVSA 7.2 port for PEPE/USD. PAPER ONLY: public data, no keys, no orders.

Ports the Pine script's DEFAULT settings (Original frequency, balanced mode,
trend continuation only, breakout or retest, 2R fixed target, no partials).
Visuals, alerts and optional switches are not ported. Not yet verified
against TradingView's trade list.
"""
import json
import urllib.request
from collections import Counter
from datetime import datetime, timezone

PAIR = "PEPEUSD"
INTERVAL = 15              # chart minutes
HTF = 240
CAPITAL = 10000.0          # same as the Pine script
RISK_PCT = 0.01
COST_RESERVE = 0.0005      # used for position sizing
TICK = 0.000000001         # 1 tick slippage on market and stop fills
WARMUP = 100


def fetch(interval):
    url = f"https://api.kraken.com/0/public/OHLC?pair={PAIR}&interval={interval}"
    req = urllib.request.Request(url, headers={"User-Agent": "gwvsa-paper"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("error"):
        raise RuntimeError(data["error"])
    key = next(k for k in data["result"] if k != "last")
    return [dict(t=int(x[0]), o=float(x[1]), h=float(x[2]), l=float(x[3]),
                 c=float(x[4]), v=float(x[6])) for x in data["result"][key][:-1]]


def rma(values, n):
    out, prev = [], None
    for i, x in enumerate(values):
        if i == n - 1:
            prev = sum(values[:n]) / n
        elif i >= n:
            prev = (prev * (n - 1) + x) / n
        out.append(prev)
    return out


def htf_bias(bars):
    """Confirmed 1/1 pivot structure: list of (close_time, bias)."""
    tops, bottoms, out = [], [], []
    for i, b in enumerate(bars):
        if i >= 2:
            a, m = bars[i - 2], bars[i - 1]
            if m["h"] >= a["h"] and m["h"] > b["h"]:
                tops.append(m["h"])
            if m["l"] <= a["l"] and m["l"] < b["l"]:
                bottoms.append(m["l"])
        bias = 0
        if len(tops) > 1 and len(bottoms) > 1:
            if tops[-1] > tops[-2] and bottoms[-1] > bottoms[-2]:
                bias = 1
            elif tops[-1] < tops[-2] and bottoms[-1] < bottoms[-2]:
                bias = -1
        out.append((b["t"] + HTF * 60, bias))
    return out


def simulate(bars, htf, commission):
    tr = [b["h"] - b["l"] if i == 0 else max(
        b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]),
        abs(b["l"] - bars[i - 1]["c"])) for i, b in enumerate(bars)]
    atrs = rma(tr, 14)
    vol_avg = rma([b["v"] for b in bars], 20)
    bias_list = htf_bias(htf)
    hi_i, bias, h_bias = 0, 0, bias_list

    # Gann 1-bar swing state
    direction, run_hi, run_lo, run_hi_bar, run_lo_bar = 0, None, None, 0, 0
    sw_hi = prev_hi = sw_lo = prev_lo = None
    sw_hi_bar = sw_lo_bar = 0
    top = bottom = None
    top_bar = bottom_bar = 0
    top_broken = bottom_broken = True
    section, section_trend = 0, 0

    last_event = {1: -100000, -1: -100000}
    prev_background = {1: False, -1: False}
    pending = {1: None, -1: None}
    pos = order = None
    close_next = False
    equity, trades, stats = CAPITAL, [], Counter()

    def close_trade(price, reason, when):
        nonlocal equity, pos
        d, q = pos["d"], pos["qty"]
        pnl = d * (price - pos["entry"]) * q - commission * q * (price + pos["entry"])
        equity += pnl
        trades.append(dict(side="LONG" if d == 1 else "SHORT", entry=pos["entry"],
                           exit=price, pnl=round(pnl, 2), reason=reason, time=when))
        pos = None

    for i, b in enumerate(bars):
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        closed_now, just_filled = False, False

        # ---- fills from orders placed on earlier bars ----
        if close_next and pos:
            close_trade(o - pos["d"] * TICK, pos["exit_reason"], b["t"])
            closed_now = True
        close_next = False
        if order:
            pos = dict(order, entry=o + order["d"] * TICK)
            order, just_filled = None, True
        if pos:
            d, stop, target = pos["d"], pos["stop"], pos["target"]
            path = [o, h, l, c] if h - o < o - l else [o, l, h, c]
            fill = None
            if d * (o - stop) <= 0:
                fill = (o - d * TICK, "stop")
            elif d * (o - target) >= 0:
                fill = (o, "target")
            else:
                for point in path[1:]:
                    if d * (point - stop) <= 0:
                        fill = (stop - d * TICK, "stop")
                        break
                    if d * (point - target) >= 0:
                        fill = (target, "target")
                        break
            if fill:
                close_trade(*fill, b["t"])
                closed_now = True
        if pos and just_filled:  # re-anchor 2R after the observed fill
            risk = pos["d"] * (pos["entry"] - pos["stop"])
            if risk > 0:
                pos["target"] = pos["entry"] + pos["d"] * risk * 2

        if i == 0:
            continue

        # ---- Gann swings ----
        p = bars[i - 1]
        up = h > p["h"] and l >= p["l"]
        down = h <= p["h"] and l < p["l"]
        inside = h <= p["h"] and l >= p["l"]
        outside = h > p["h"] and l < p["l"]
        high_pivot = low_pivot = False
        if direction == 0:
            first = (-1 if o > c else 1) if outside else 1 if up else -1 if down else 0
            if first:
                direction, run_hi, run_lo = first, h, l
                run_hi_bar = run_lo_bar = i
        elif not inside:
            if direction == 1:
                if (up or outside) and h >= run_hi:
                    run_hi, run_hi_bar = h, i
                if down or (outside and o > c):
                    prev_hi, sw_hi, sw_hi_bar = sw_hi, run_hi, run_hi_bar
                    high_pivot = True
                    top, top_bar, top_broken = sw_hi, sw_hi_bar, False
                    direction, run_hi, run_lo = -1, h, l
                    run_hi_bar = run_lo_bar = i
            else:
                if (down or outside) and l <= run_lo:
                    run_lo, run_lo_bar = l, i
                if up or (outside and o <= c):
                    prev_lo, sw_lo, sw_lo_bar = sw_lo, run_lo, run_lo_bar
                    low_pivot = True
                    bottom, bottom_bar, bottom_broken = sw_lo, sw_lo_bar, False
                    direction, run_hi, run_lo = 1, h, l
                    run_hi_bar = run_lo_bar = i
        if top is not None and not top_broken and i > top_bar and c > top:
            top_broken = True
        if bottom is not None and not bottom_broken and i > bottom_bar and c < bottom:
            bottom_broken = True

        trend = 0
        if None not in (sw_hi, prev_hi, sw_lo, prev_lo):
            if sw_hi > prev_hi and sw_lo > prev_lo:
                trend = 1
            elif sw_hi < prev_hi and sw_lo < prev_lo:
                trend = -1
        if trend and trend != section_trend:
            section_trend, section = trend, 0
        if (high_pivot and trend == 1) or (low_pivot and trend == -1):
            section += 1

        if i < 59 or atrs[i] is None:
            continue

        # ---- location: swings, Gann eighths, Fibonacci ----
        atr = atrs[i]

        def touch(level):
            return l <= level + 0.4 * atr and h >= level - 0.4 * atr

        rec_lo = min(x["l"] for x in bars[max(0, i - 20):i])
        rec_hi = max(x["h"] for x in bars[max(0, i - 20):i])
        window = bars[i - 59:i + 1]
        phase_mid = (min(x["l"] for x in window) + max(x["h"] for x in window)) / 2
        near_lo = sw_lo is not None and abs(l - sw_lo) <= atr
        near_hi = sw_hi is not None and abs(h - sw_hi) <= atr
        support = near_lo or l <= rec_lo
        resistance = near_hi or h >= rec_hi
        gann = fifty = False
        fib = {1: False, -1: False}
        rng = sw_hi - sw_lo if sw_hi is not None and sw_lo is not None else 0
        if rng > 0:
            gann = any(touch(sw_lo + rng * k / 8) for k in range(1, 8))
            fifty = touch(sw_lo + rng / 2)
            ratios = (0.382, 0.5, 0.618, 0.786)
            if sw_hi_bar > sw_lo_bar:
                fib[1] = any(touch(sw_hi - rng * r) for r in ratios)
            if sw_lo_bar > sw_hi_bar:
                fib[-1] = any(touch(sw_lo + rng * r) for r in ratios)

        # ---- VSA ----
        spread = h - l
        avg_spread = sum(x["h"] - x["l"] for x in bars[i - 19:i + 1]) / 20
        sr = spread / avg_spread if avg_spread else None
        cl = (c - l) / spread if spread else 0.5
        vr = b["v"] / vol_avg[i] if vol_avg[i] and b["v"] > 0 else None
        usable = vr is not None
        hv = usable and vr >= 1.2
        lv = usable and vr <= 0.8
        falling_vol = b["v"] < p["v"]
        wide = sr is not None and sr >= 1.2
        narrow = sr is not None and sr <= 0.8
        v_up, v_down = c > p["c"], c < p["c"]
        loc = {1: support or gann or fib[1], -1: resistance or gann or fib[-1]}

        shakeout = v_down and hv and wide and cl >= 0.7 and loc[1]
        sell_climax = v_down and hv and wide and 0.5 <= cl <= 0.85 and (trend == -1 or l <= rec_lo)
        stopping = v_down and hv and wide and 0.35 <= cl <= 0.65 and loc[1]
        absorption = v_down and hv and narrow and 0.55 <= cl <= 0.9 and (trend == -1 or l <= rec_lo)
        spring = sw_lo is not None and l < sw_lo < c and cl >= 0.55 and usable and vr >= 1
        upthrust = v_up and wide and cl <= 0.3 and usable and vr >= 1.2 and loc[-1]
        buy_climax = v_up and hv and wide and 0.15 <= cl <= 0.5 and (trend == 1 or h >= rec_hi)
        effort = v_up and hv and narrow and 0.35 <= cl <= 0.65 and (trend == 1 or h >= rec_hi)
        trap_up = sw_hi is not None and h > sw_hi > c and cl <= 0.45 and usable and vr >= 1

        event = {1: shakeout or sell_climax or stopping or absorption or spring,
                 -1: upthrust or buy_climax or effort or trap_up}
        for d in (1, -1):
            if event[d]:
                last_event[d] = i
        age = {d: i - last_event[d] for d in (1, -1)}
        background = {d: age[d] <= 30 and (age[d] < age[-d] or age[-d] > 30) for d in (1, -1)}
        was = dict(prev_background)
        prev_background = background

        test_rising = (v_down and lv and narrow and 0.15 <= cl <= 0.55 and trend == 1
                       and near_lo and (was[1] or age[1] <= 30))
        no_supply = ((v_down or inside) and lv and falling_vol and narrow and cl >= 0.45
                     and trend == 1 and near_lo and (was[1] or age[1] <= 30))
        no_demand = ((v_up or inside) and lv and falling_vol and narrow and cl <= 0.55
                     and trend == -1 and near_hi and (was[-1] or age[-1] <= 30))
        test_falling = (v_up and lv and narrow and 0.45 <= cl <= 0.85 and trend == -1
                        and near_hi and (was[-1] or age[-1] <= 30))
        minor = {1: test_rising or no_supply, -1: no_demand or test_falling}

        vol_ok = {
            1: ((usable and vr >= 1) if spring else hv) if event[1]
               else lv if test_rising else (lv and falling_vol) if no_supply else False,
            -1: (hv if upthrust else (usable and vr >= 1) if trap_up else hv) if event[-1]
               else (lv and falling_vol) if no_demand else lv if test_falling else False,
        }

        if trend == 1:
            phase = "Distribution" if background[-1] and c >= phase_mid else "Markup"
        elif trend == -1:
            phase = "Accumulation" if background[1] and c <= phase_mid else "Markdown"
        elif background[1] and c <= phase_mid:
            phase = "Accumulation"
        elif background[-1] and c >= phase_mid:
            phase = "Distribution"
        else:
            phase = "Range"

        while hi_i < len(h_bias) and h_bias[hi_i][0] <= b["t"]:
            bias = h_bias[hi_i][1]
            hi_i += 1

        if i < WARMUP:
            continue

        # ---- qualified setups ----
        score, qualified = {}, {}
        for d in (1, -1):
            pattern = event[d] or minor[d]
            cont = pattern and trend == d
            s = (2 if cont else 0)
            s += 2 if event[d] else 1 if minor[d] else 0
            s += 2 if was[d] else 1 if (background[d] or event[d]) else 0
            s += int(support if d == 1 else resistance)
            s += int(gann) + int(fib[d]) + int(bias == d)
            s += int(fifty and d * (c - (sw_lo + rng / 2)) > 0)
            s += int(section > 0 and cont and section <= 2)
            score[d] = s
            phases = ("Accumulation", "Markup") if d == 1 else ("Distribution", "Markdown")
            level, broken = (top, top_broken) if d == 1 else (bottom, bottom_broken)
            qualified[d] = bool(
                usable and cont and vol_ok[d] and (background[d] or was[d]) and loc[d]
                and bias == d and phase in phases and s >= 7
                and level is not None and not broken)
            stats["VSA patterns"] += bool(pattern)
            stats["qualified setups"] += qualified[d]

        for d in (1, -1):
            if not (qualified[d] and not qualified[-d]):
                continue
            trigger = top if d == 1 else bottom
            old = pending[d]
            if old is None or (trigger != old["trigger"] and score[d] >= old["score"]):
                if d == 1:
                    stop = (min(l, sw_lo) if sw_lo is not None else l) - 0.5 * atr
                else:
                    stop = (max(h, sw_hi) if sw_hi is not None else h) + 0.5 * atr
                pending[d] = dict(trigger=trigger, stop=stop, bar=i, score=score[d],
                                  break_bar=None, strong=False)

        # ---- triggers, retests, entry gates ----
        risk_ok = {1: False, -1: False}
        gates = {1: False, -1: False}
        for d in (1, -1):
            s = pending[d]
            if s is None:
                continue
            expired = (s["break_bar"] is None and i - s["bar"] > 5) or (
                s["break_bar"] is not None and i - s["break_bar"] > 5)
            invalid = i > s["bar"] and (l <= s["stop"] if d == 1 else h >= s["stop"])
            if expired or invalid:
                pending[d] = None
                continue
            broke = i > s["bar"] and d * (c - s["trigger"]) > 0
            if broke and s["break_bar"] is None:
                s["break_bar"], s["strong"] = i, usable and vr >= 1.0
                stats["trigger breaks"] += 1
            first_break = broke and s["break_bar"] == i
            retest = False
            if s["break_bar"] is not None and i > s["break_bar"]:
                edge = s["trigger"] + d * 0.15 * atr
                touched = l <= edge if d == 1 else h >= edge
                shape = cl >= 0.55 if d == 1 else cl <= 0.45
                retest = bool(touched and d * (c - s["trigger"]) > 0 and shape
                              and usable and vr <= 1.1)
            if not (first_break or retest):
                continue
            if s["score"] + int(s["strong"]) + int(retest) < 9:
                continue
            risk = d * (c - s["stop"])
            risk_ok[d] = risk > TICK and 0.25 <= risk / atr <= 4.0
            past = bars[max(0, i - 100):i]
            wall = max(x["h"] for x in past) if d == 1 else min(x["l"] for x in past)
            room = d * (wall - c) <= 0 or d * (wall - c) >= risk
            gates[d] = bool(risk_ok[d] and (s["strong"] or retest) and room)

        if pos is None and not closed_now:
            for d in (1, -1):
                if gates[d] and not gates[-d]:
                    stop = pending[d]["stop"]
                    per_unit = abs(c - stop) + c * COST_RESERVE * 2
                    qty = min(equity * RISK_PCT / per_unit, equity / c)
                    qty = int(qty / 0.0001) * 0.0001
                    if qty >= 0.0001:
                        order = dict(d=d, qty=qty, stop=stop, target=c + (c - stop) * 2)
                        pending = {1: None, -1: None}
                        stats["orders"] += 1
                    break

        # ---- thesis exits (fill at next open) ----
        if pos:
            d = pos["d"]
            if risk_ok[-d]:
                close_next, pos["exit_reason"] = True, "opposite setup"
                pending[-d] = None
            elif trend == -d:
                close_next, pos["exit_reason"] = True, "trend flip"

    return equity, trades, stats, pos, order, pending


def report(label, result):
    equity, trades, stats, pos, order, pending = result
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [-t["pnl"] for t in trades if t["pnl"] <= 0]
    print(f"\n=== {label} ===")
    print(f"Closed equity ${CAPITAL:.2f} -> ${equity:.2f}")
    print(f"Closed trades {len(trades)} | wins {len(wins)} | losses {len(losses)}")
    if trades:
        print(f"Win rate {100 * len(wins) / len(trades):.1f}%")
    if sum(losses):
        print(f"Profit factor {sum(wins) / sum(losses):.2f}")
    for name in ("VSA patterns", "qualified setups", "trigger breaks", "orders"):
        print(f"  {name}: {stats[name]}")
    for t in trades[-10:]:
        when = datetime.fromtimestamp(t["time"], timezone.utc).strftime("%m-%d %H:%M")
        print(f"  {when} {t['side']} ${t['pnl']:+.2f} ({t['reason']})")
    print("Open paper position:", pos or "none")
    print("Order for next candle:", order or "none")
    print("Armed setups:", {("long" if d == 1 else "short"): s["trigger"]
                            for d, s in pending.items() if s} or "none")


if __name__ == "__main__":
    bars, htf = fetch(INTERVAL), fetch(HTF)
    print(f"GWVSA 7.2 | {PAIR} {INTERVAL}m | PAPER ONLY | candles: {len(bars)}")
    results = {}
    for label, fee in (("Pine fee 0.05% per side", 0.0005),
                       ("Fee 0.40% per side", 0.004)):
        results[label] = simulate(bars, htf, fee)
        report(label, results[label])
    with open("gwvsa_pepe_results.json", "w") as f:
        json.dump({k: v[1] for k, v in results.items()}, f, indent=2)
