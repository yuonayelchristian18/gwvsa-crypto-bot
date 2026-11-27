#!/usr/bin/env python3
"""
gwvsa_bot.py - Python port of "Gann-Wyckoff-VSA v7.2 | Final Course Edition" (TradingView Pine v6)

Commands
  python gwvsa_bot.py selftest                      # runs on synthetic data, no internet needed
  python gwvsa_bot.py backtest --days 365           # Coinbase public candles, no API key
  python gwvsa_bot.py paper                         # one paper-trading pass (run hourly)
  python gwvsa_bot.py paper --loop                  # keeps running, one pass per closed candle
  python gwvsa_bot.py report                        # stats of paper/live closed trades
  python gwvsa_bot.py live --confirm-live           # REAL ORDERS on Kraken spot (long only). Read README first.

What is ported (defaults = the Pine defaults: "Original v6.4.1" frequency, balanced scoring 7/13 -> 9/15):
  Gann 1-bar swings + trend, sections, Gann eighths, Fibonacci, VSA patterns + background, Wyckoff phase proxy,
  confirmed HTF pivot trend, setup queue (5 bars), breakout-or-retest entry (no chase), fixed structural stop
  (swing -/+ 0.5 ATR, never moved), 2R target re-anchored to the actual fill, risk-based sizing,
  exits on opposite confirmed setup and on Gann trend flip.
Not ported: Gann 2-bar engine (off by default in Pine), T1/T2 partial exits (off by default), all drawing code.

Honest limits: this is a faithful *mechanical* port, not proof of profit. Pine pivot rules and TradingView's
broker emulator differ in small ways, so trade lists will be close to, not identical to, TradingView's.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

NAN = float("nan")


def isn(x):
    return x is None or x != x


# ============================================================================ CONFIG
@dataclass
class Cfg:
    timeframe_sec: int = 3600
    htf_sec: int = 14400                 # Pine default HTF = 240 (4h), built by resampling chart candles
    frequency: str = "original"          # original | more | max
    enable_longs: bool = True
    enable_shorts: bool = False          # spot accounts cannot short; True only for research backtests
    trade_mode: str = "both"             # both | continuation | reversal
    entry_mode: str = "breakout_or_retest"  # breakout_or_retest | breakout_only | retest_only | original
    atr_len: int = 14
    atr_stop_buffer: float = 0.5
    reward_risk: float = 2.0
    min_risk_atr: float = 0.25
    max_risk_atr: float = 4.0
    pivot_prox_atr: float = 1.0
    structure_lookback: int = 20
    level_tol_atr: float = 0.40
    fib_tol_atr: float = 0.40
    vol_len: int = 20
    spread_len: int = 20
    high_vol: float = 1.20
    very_high_vol: float = 1.80
    low_vol: float = 0.80
    wide_spread: float = 1.20
    narrow_spread: float = 0.80
    upthrust_min_vol: float = 1.20
    bg_memory: int = 30
    phase_lookback: int = 60
    require_vsa_background: bool = True
    allow_supplemental: bool = True
    use_htf: bool = True
    min_grade: int = 7
    require_location: bool = True
    balanced: bool = True
    balanced_setup_min: int = 7
    balanced_entry_min: int = 9
    balanced_trend_only: bool = True
    use_section_score: bool = True
    use_gann_levels: bool = True
    use_fib: bool = True
    setup_expiry: int = 5
    retest_expiry: int = 5
    retest_tol_atr: float = 0.15
    breakout_min_vol: float = 1.0
    retest_max_vol: float = 1.10
    require_retest: bool = False
    require_close_break: bool = True
    exit_on_opposite: bool = True
    exit_on_trend_flip: bool = True
    require_participation: bool = True
    require_room: bool = True
    obstacle_lookback: int = 100
    min_room_r: float = 1.0
    require_wyckoff: bool = True
    # account / costs (costs per side, in percent)
    risk_usd: float = 2.0
    equity: float = 400.0
    fee_pct: float = 0.10
    slip_pct: float = 0.15
    qty_step: float = 1e-6
    min_notional: float = 10.0


class Pend:
    """One pending (armed) setup, long or short."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.active = False
        self.trigger = NAN
        self.stop = NAN
        self.setup_bar = -1
        self.score = 0
        self.name = ""
        self.brk_seen = False
        self.brk_bar = -1
        self.brk_vol = False


# ============================================================================ ENGINE
class Engine:
    """Feed it CLOSED candles one at a time with on_bar(). Mirrors the Pine script's bar logic and order."""

    def __init__(self, cfg: Cfg):
        c = self.c = cfg
        self.freq_more = c.frequency != "original"
        self.freq_max = c.frequency == "max"
        self.eff_use_htf = c.use_htf and not self.freq_more
        self.eff_min_grade = 6 if self.freq_max else (max(c.min_grade, c.balanced_setup_min) if c.balanced else c.min_grade)
        self.eff_entry_min = 7 if self.freq_max else c.balanced_entry_min
        self.gate_location = c.require_location and not self.freq_max
        self.gate_background = c.require_vsa_background and not self.freq_max
        self.gate_wyckoff = c.require_wyckoff and not self.freq_more
        self.gate_participation = c.require_participation and not self.freq_max
        self.gate_room = c.require_room and not self.freq_more
        # auto HTF: if configured HTF is not higher than the chart, move to the next interval
        self.htf_sec = c.htf_sec if c.htf_sec > c.timeframe_sec else (86400 if c.timeframe_sec < 86400 else 604800)
        self.i = -1
        self.O, self.H, self.L, self.C, self.V, self.SP = [], [], [], [], [], []
        self.atr = NAN
        self._tr = []
        self.vavg = NAN
        self._vh = []
        # Gann 1-bar swing state
        self.sdir = 0
        self.rH = self.rL = NAN
        self.rHb = self.rLb = -1
        self.lastSH = self.prevSH = self.lastSL = self.prevSL = NAN
        self.lastSHb = self.lastSLb = -1
        self.aTop = self.aBot = NAN
        self.aTopBar = self.aBotBar = -1
        self.aTopBroken = self.aBotBroken = True
        self.sec_trend = 0
        self.sec_count = 0
        self.last_bull_ev = None
        self.last_bear_ev = None
        self.prev_bull_bg = False
        self.prev_bear_bg = False
        # HTF resampler state
        self.htf_bucket = None
        self.htf_h = self.htf_l = NAN
        self.hh, self.hl, self.piv_h, self.piv_l = [], [], [], []
        self.htf_bias = 0
        # setup queues and order state
        self.pend = {1: Pend(), -1: Pend()}
        self.order_in_flight = False

    # ---- helpers
    @staticmethod
    def _rma(prev, x, n, hist):
        if isn(prev):
            hist.append(x)
            return sum(hist[-n:]) / n if len(hist) >= n else NAN
        return (prev * (n - 1) + x) / n

    def _htf_bucket_id(self, t):
        if self.htf_sec == 604800:
            return int((t - 4 * 86400) // 604800)  # weeks start Monday
        return int(t // self.htf_sec)

    def _htf_update(self, t, h, l):
        b = self._htf_bucket_id(t)
        if self.htf_bucket is None:
            self.htf_bucket, self.htf_h, self.htf_l = b, h, l
        elif b != self.htf_bucket:  # previous HTF bar just completed
            self.hh.append(self.htf_h)
            self.hl.append(self.htf_l)
            j = len(self.hh) - 1
            if j >= 2:  # ta.pivothigh(high,1,1) / pivotlow(low,1,1)
                if self.hh[j - 1] > self.hh[j - 2] and self.hh[j - 1] > self.hh[j]:
                    self.piv_h.append(self.hh[j - 1])
                if self.hl[j - 1] < self.hl[j - 2] and self.hl[j - 1] < self.hl[j]:
                    self.piv_l.append(self.hl[j - 1])
            if len(self.piv_h) >= 2 and len(self.piv_l) >= 2:
                th, ph, bl, pl = self.piv_h[-1], self.piv_h[-2], self.piv_l[-1], self.piv_l[-2]
                self.htf_bias = 1 if (th > ph and bl > pl) else -1 if (th < ph and bl < pl) else 0
            else:
                self.htf_bias = 0
            self.htf_bucket, self.htf_h, self.htf_l = b, h, l
        else:
            self.htf_h = max(self.htf_h, h)
            self.htf_l = min(self.htf_l, l)

    # ---- main per-bar logic
    def on_bar(self, t, o, h, l, c, v, pos, qty_fn):
        """pos: current position side (1/-1/0). Returns {'entry': dict|None, 'close': bool, 'info': dict}."""
        cf = self.c
        self.i += 1
        i = self.i
        self.O.append(o); self.H.append(h); self.L.append(l); self.C.append(c); self.V.append(v)
        self._htf_update(t, h, l)
        prev_c = self.C[i - 1] if i > 0 else NAN
        tr = h - l if i == 0 else max(h - l, abs(h - prev_c), abs(l - prev_c))
        self.atr = self._rma(self.atr, tr, cf.atr_len, self._tr)
        self.vavg = self._rma(self.vavg, v, cf.vol_len, self._vh)
        atr = self.atr
        spread = h - l
        self.SP.append(spread)
        avg_spread = sum(self.SP[-cf.spread_len:]) / cf.spread_len if i + 1 >= cf.spread_len else NAN
        recent_low = min(self.L[i - cf.structure_lookback:i]) if i >= cf.structure_lookback else NAN
        recent_high = max(self.H[i - cf.structure_lookback:i]) if i >= cf.structure_lookback else NAN
        if i >= cf.phase_lookback - 1:
            ph_lo, ph_hi = min(self.L[i - cf.phase_lookback + 1:i + 1]), max(self.H[i - cf.phase_lookback + 1:i + 1])
            phase_mid = (ph_lo + ph_hi) * 0.5
        else:
            phase_mid = NAN

        # ---- Gann bar types
        if i > 0:
            ph, pl = self.H[i - 1], self.L[i - 1]
            g_up = h > ph and l >= pl
            g_down = h <= ph and l < pl
            g_inside = h <= ph and l >= pl
            g_out = h > ph and l < pl
        else:
            g_up = g_down = g_inside = g_out = False
        g_final = (-1 if o > c else 1) if g_out else 1 if g_up else -1 if g_down else 0

        # ---- Gann 1-bar swing
        high_piv = low_piv = False
        if i > 0:
            if self.sdir == 0:
                if g_final != 0:
                    self.sdir = g_final
                    self.rH, self.rL, self.rHb, self.rLb = h, l, i, i
            elif not g_inside:
                if self.sdir == 1:
                    reverse_down = g_down or (g_out and o > c)
                    if g_up or g_out:
                        if isn(self.rH) or h >= self.rH:
                            self.rH, self.rHb = h, i
                    if reverse_down:
                        self.prevSH, self.lastSH, self.lastSHb = self.lastSH, self.rH, self.rHb
                        high_piv = True
                        self.aTop, self.aTopBar, self.aTopBroken = self.lastSH, self.lastSHb, False
                        self.sdir = -1
                        self.rL, self.rLb, self.rH, self.rHb = l, i, h, i
                else:
                    reverse_up = g_up or (g_out and o <= c)
                    if g_down or g_out:
                        if isn(self.rL) or l <= self.rL:
                            self.rL, self.rLb = l, i
                    if reverse_up:
                        self.prevSL, self.lastSL, self.lastSLb = self.lastSL, self.rL, self.rLb
                        low_piv = True
                        self.aBot, self.aBotBar, self.aBotBroken = self.lastSL, self.lastSLb, False
                        self.sdir = 1
                        self.rH, self.rHb, self.rL, self.rLb = h, i, l, i
        # swing-entry level breaks (completed bar)
        if i > 0:
            top_cond = (c > self.aTop) if cf.require_close_break else (h > self.aTop)
            bot_cond = (c < self.aBot) if cf.require_close_break else (l < self.aBot)
            if (not self.aTopBroken) and not isn(self.aTop) and i > self.aTopBar and top_cond:
                self.aTopBroken = True
            if (not self.aBotBroken) and not isn(self.aBot) and i > self.aBotBar and bot_cond:
                self.aBotBroken = True

        trend = 0
        if not (isn(self.lastSH) or isn(self.prevSH) or isn(self.lastSL) or isn(self.prevSL)):
            ht, hb = self.lastSH > self.prevSH, self.lastSL > self.prevSL
            lt, lb = self.lastSH < self.prevSH, self.lastSL < self.prevSL
            trend = 1 if (ht and hb) else -1 if (lt and lb) else 0
        trend_up, trend_down = trend == 1, trend == -1

        # ---- sections
        if trend != 0 and trend != self.sec_trend:
            self.sec_trend, self.sec_count = trend, 0
        if (high_piv and trend_up) or (low_piv and trend_down):
            self.sec_count += 1
        sec_early = 0 < self.sec_count <= 2
        sec_mature = self.sec_count >= 3

        # ---- Gann eighths / Fibonacci
        rng_valid = (not isn(self.lastSH)) and (not isn(self.lastSL)) and self.lastSH > self.lastSL
        swing_range = self.lastSH - self.lastSL if rng_valid else NAN
        last_leg_up = rng_valid and self.lastSHb > self.lastSLb
        last_leg_down = rng_valid and self.lastSLb > self.lastSHb
        gann_tol = atr * cf.level_tol_atr
        fib_tol = atr * cf.fib_tol_atr

        def touch(level, tol):
            return (not isn(level)) and l <= level + tol and h >= level - tol

        gann_levels = [self.lastSL + swing_range * k / 8.0 for k in range(1, 8)] if rng_valid else []
        gann_loc = cf.use_gann_levels and rng_valid and any(touch(g, gann_tol) for g in gann_levels)
        fib_r = (0.382, 0.5, 0.618, 0.786)
        bull_fib = cf.use_fib and last_leg_up and any(touch(self.lastSH - swing_range * r, fib_tol) for r in fib_r)
        bear_fib = cf.use_fib and last_leg_down and any(touch(self.lastSL + swing_range * r, fib_tol) for r in fib_r)
        g500 = gann_levels[3] if rng_valid else NAN
        bull_fifty = cf.use_gann_levels and touch(g500, gann_tol) and c > g500
        bear_fifty = cf.use_gann_levels and touch(g500, gann_tol) and c < g500

        near_sl = (not isn(self.lastSL)) and abs(l - self.lastSL) <= atr * cf.pivot_prox_atr
        near_sh = (not isn(self.lastSH)) and abs(h - self.lastSH) <= atr * cf.pivot_prox_atr
        at_recent_low = (not isn(recent_low)) and l <= recent_low
        at_recent_high = (not isn(recent_high)) and h >= recent_high
        at_support = near_sl or at_recent_low
        at_resist = near_sh or at_recent_high
        bull_loc = at_support or gann_loc or bull_fib
        bear_loc = at_resist or gann_loc or bear_fib

        # ---- VSA measurements
        vsa_up = i > 0 and c > prev_c
        vsa_down = i > 0 and c < prev_c
        vsa_inside = g_inside
        sr = spread / avg_spread if (not isn(avg_spread) and avg_spread > 0) else NAN
        cl = (c - l) / spread if spread > 0 else 0.5
        vr = v / self.vavg if (not isn(self.vavg) and self.vavg > 0) else NAN
        has_vol = (not isn(vr)) and v > 0
        vol_high = has_vol and vr >= cf.high_vol
        vol_low = has_vol and vr <= cf.low_vol
        vol_decl = i > 0 and v < self.V[i - 1]
        sp_wide = (not isn(sr)) and sr >= cf.wide_spread
        sp_narrow = (not isn(sr)) and sr <= cf.narrow_spread

        # ---- raw Wyckoff / VSA patterns
        shakeout = vsa_down and vol_high and sp_wide and cl >= 0.70 and bull_loc
        sell_climax = vsa_down and vol_high and sp_wide and 0.50 <= cl <= 0.85 and (trend_down or at_recent_low)
        stopping = vsa_down and vol_high and sp_wide and 0.35 <= cl <= 0.65 and bull_loc
        absorption = vsa_down and vol_high and sp_narrow and 0.55 <= cl <= 0.90 and (trend_down or at_recent_low)
        test_rise_base = vsa_down and vol_low and sp_narrow and 0.15 <= cl <= 0.55
        upthrust = vsa_up and sp_wide and cl <= 0.30 and has_vol and vr >= cf.upthrust_min_vol and bear_loc
        buy_climax = vsa_up and vol_high and sp_wide and 0.15 <= cl <= 0.50 and (trend_up or at_recent_high)
        no_demand_base = (vsa_up or vsa_inside) and vol_low and vol_decl and sp_narrow and cl <= 0.55
        effort_warn = vsa_up and vol_high and sp_narrow and 0.35 <= cl <= 0.65 and (trend_up or at_recent_high)
        test_fall_base = vsa_up and vol_low and sp_narrow and 0.45 <= cl <= 0.85
        supp = cf.allow_supplemental
        spring = supp and (not isn(self.lastSL)) and l < self.lastSL and c > self.lastSL and cl >= 0.55 and has_vol and vr >= 1.0
        trap_up = supp and (not isn(self.lastSH)) and h > self.lastSH and c < self.lastSH and cl <= 0.45 and has_vol and vr >= 1.0
        no_supply_base = supp and (vsa_down or vsa_inside) and vol_low and vol_decl and sp_narrow and cl >= 0.45

        bull_strength = shakeout or sell_climax or stopping or absorption or spring
        bear_weak = upthrust or buy_climax or effort_warn or trap_up
        if bull_strength:
            self.last_bull_ev = i
        if bear_weak:
            self.last_bear_ev = i
        bull_age = i - self.last_bull_ev if self.last_bull_ev is not None else 100000
        bear_age = i - self.last_bear_ev if self.last_bear_ev is not None else 100000
        m = cf.bg_memory
        bull_bg = bull_age <= m and (bull_age < bear_age or bear_age > m)
        bear_bg = bear_age <= m and (bear_age < bull_age or bull_age > m)
        p_bull_bg, p_bear_bg = self.prev_bull_bg, self.prev_bear_bg

        test_rising = test_rise_base and trend_up and near_sl and (p_bull_bg or bull_age <= m)
        no_supply = no_supply_base and trend_up and near_sl and (p_bull_bg or bull_age <= m)
        no_demand = no_demand_base and trend_down and near_sh and (p_bear_bg or bear_age <= m)
        test_falling = test_fall_base and trend_down and near_sh and (p_bear_bg or bear_age <= m)

        bull_pattern = bull_strength or test_rising or no_supply
        bear_pattern = bear_weak or no_demand or test_falling
        bull_name = ("Spring" if spring else "Shakeout" if shakeout else "Selling Climax" if sell_climax else
                     "Stopping Volume" if stopping else "Possible Absorption" if absorption else
                     "Test (rising)" if test_rising else "No Supply" if no_supply else "Bullish VSA")
        bear_name = ("Trap-Up" if trap_up else "Upthrust" if upthrust else "Buying Climax" if buy_climax else
                     "Effort-Result Warning" if effort_warn else "No Demand" if no_demand else
                     "Test (falling)" if test_falling else "Bearish VSA")

        if bull_strength:
            bull_vol_ok = (has_vol and vr >= 1.0) if spring else vol_high
        elif test_rising:
            bull_vol_ok = vol_low
        elif no_supply:
            bull_vol_ok = vol_low and vol_decl
        else:
            bull_vol_ok = False
        if bear_weak:
            if upthrust:
                bear_vol_ok = has_vol and vr >= max(cf.upthrust_min_vol, cf.high_vol)
            elif trap_up:
                bear_vol_ok = has_vol and vr >= 1.0
            else:
                bear_vol_ok = vol_high
        elif no_demand:
            bear_vol_ok = vol_low and vol_decl
        elif test_falling:
            bear_vol_ok = vol_low
        else:
            bear_vol_ok = False

        # ---- Wyckoff phase proxy
        if trend_up:
            phase = "Distribution" if (bear_bg and c >= phase_mid) else "Markup"
        elif trend_down:
            phase = "Accumulation" if (bull_bg and c <= phase_mid) else "Markdown"
        else:
            phase = "Accumulation" if (bull_bg and c <= phase_mid) else "Distribution" if (bear_bg and c >= phase_mid) else "Range"

        # ---- HTF filter (bias from last completed HTF bar)
        htf_bull, htf_bear = self.htf_bias == 1, self.htf_bias == -1

        # ---- setup context
        allow_cont = cf.trade_mode in ("both", "continuation")
        allow_rev = ((not cf.balanced) or (not cf.balanced_trend_only)) and cf.trade_mode in ("both", "reversal")
        bull_cont = allow_cont and bull_pattern and trend_up
        bull_rev = allow_rev and bull_strength and (not trend_up) and (trend_down or phase == "Accumulation" or at_recent_low)
        bear_cont = allow_cont and bear_pattern and trend_down
        bear_rev = allow_rev and bear_weak and (not trend_down) and (trend_up or phase == "Distribution" or at_recent_high)
        bull_struct_ok = bull_cont or bull_rev
        bear_struct_ok = bear_cont or bear_rev
        bull_bg_ok = bull_strength if bull_rev else (bull_bg or p_bull_bg)
        bear_bg_ok = bear_weak if bear_rev else (bear_bg or p_bear_bg)
        bull_sec_al = (self.sec_count == 0 or sec_early) if bull_cont else (self.sec_count == 0 or sec_mature) if bull_rev else False
        bear_sec_al = (self.sec_count == 0 or sec_early) if bear_cont else (self.sec_count == 0 or sec_mature) if bear_rev else False

        use_htf_score = cf.balanced or self.eff_use_htf
        bull_score = ((2 if bull_cont else 1 if bull_rev else 0) + (2 if bull_strength else 1 if (test_rising or no_supply) else 0) +
                      (2 if p_bull_bg else 1 if (bull_bg or bull_strength) else 0) + (1 if at_support else 0) +
                      (1 if gann_loc else 0) + (1 if bull_fib else 0) + (1 if bull_fifty else 0) +
                      (1 if (use_htf_score and htf_bull) else 0) +
                      (1 if (cf.use_section_score and self.sec_count > 0 and bull_sec_al) else 0))
        bear_score = ((2 if bear_cont else 1 if bear_rev else 0) + (2 if bear_weak else 1 if (no_demand or test_falling) else 0) +
                      (2 if p_bear_bg else 1 if (bear_bg or bear_weak) else 0) + (1 if at_resist else 0) +
                      (1 if gann_loc else 0) + (1 if bear_fib else 0) + (1 if bear_fifty else 0) +
                      (1 if (use_htf_score and htf_bear) else 0) +
                      (1 if (cf.use_section_score and self.sec_count > 0 and bear_sec_al) else 0))

        def pillars(pattern, vol_ok, struct_ok, bg_ok, loc, htf_ok_dir, wy_ok):
            core = pattern and vol_ok and struct_ok
            return (core and ((not self.gate_background) or bg_ok) and ((not self.gate_location) or loc) and
                    ((not self.eff_use_htf) or htf_ok_dir) and ((not self.gate_wyckoff) or wy_ok))

        bull_ok = pillars(bull_pattern, bull_vol_ok, bull_struct_ok, bull_bg_ok, bull_loc, htf_bull, phase in ("Accumulation", "Markup"))
        bear_ok = pillars(bear_pattern, bear_vol_ok, bear_struct_ok, bear_bg_ok, bear_loc, htf_bear, phase in ("Distribution", "Markdown"))
        valid_trig = {1: (not isn(self.aTop)) and not self.aTopBroken, -1: (not isn(self.aBot)) and not self.aBotBroken}
        qualified = {
            1: has_vol and bull_ok and bull_score >= self.eff_min_grade and valid_trig[1],
            -1: has_vol and bear_ok and bear_score >= self.eff_min_grade and valid_trig[-1],
        }
        score = {1: bull_score, -1: bear_score}
        name = {1: bull_name, -1: bear_name}
        cont = {1: bull_cont, -1: bear_cont}

        # ---- setup queues (arm a setup; the Gann swing level is the later trigger)
        for s in (1, -1):
            P = self.pend[s]
            if qualified[s] and not qualified[-s]:
                trig = self.aTop if s == 1 else self.aBot
                if (not P.active) or (trig != P.trigger and score[s] >= P.score):
                    if s == 1:
                        struct = min(l, self.lastSL) if (cont[1] and not isn(self.lastSL)) else l
                        stop = struct - atr * cf.atr_stop_buffer
                    else:
                        struct = max(h, self.lastSH) if (cont[-1] and not isn(self.lastSH)) else h
                        stop = struct + atr * cf.atr_stop_buffer
                    P.active, P.trigger, P.stop, P.setup_bar = True, trig, stop, i
                    P.score, P.name = score[s], name[s]
                    P.brk_seen, P.brk_bar, P.brk_vol = False, -1, False
        for s in (1, -1):
            P = self.pend[s]
            if P.active:
                expired = (not P.brk_seen) and i - P.setup_bar > cf.setup_expiry
                rt_expired = P.brk_seen and i - P.brk_bar > cf.retest_expiry
                invalid = i > P.setup_bar and ((l <= P.stop) if s == 1 else (h >= P.stop))
                if expired or rt_expired or invalid:
                    P.reset()

        # ---- breakout / retest / gates
        risk_ok = {1: False, -1: False}
        gates_ok = {1: False, -1: False}
        for s in (1, -1):
            P = self.pend[s]
            if not P.active:
                continue
            if cf.require_close_break:
                brk = i > P.setup_bar and ((c > P.trigger) if s == 1 else (c < P.trigger))
            else:
                brk = i > P.setup_bar and ((h > P.trigger) if s == 1 else (l < P.trigger))
            if brk and not P.brk_seen:
                P.brk_seen, P.brk_bar = True, i
                P.brk_vol = has_vol and vr >= cf.breakout_min_vol
            touch_rt = P.brk_seen and i > P.brk_bar and ((l <= P.trigger + atr * cf.retest_tol_atr) if s == 1 else (h >= P.trigger - atr * cf.retest_tol_atr))
            if s == 1:
                rt_quality = touch_rt and c > P.trigger and cl >= 0.55 and has_vol and vr <= cf.retest_max_vol
            else:
                rt_quality = touch_rt and c < P.trigger and cl <= 0.45 and has_vol and vr <= cf.retest_max_vol
            final = P.score + (1 if P.brk_vol else 0) + (1 if rt_quality else 0)
            bal_ok = (not cf.balanced) or final >= self.eff_entry_min
            first_break = brk and P.brk_bar == i
            if cf.require_retest or cf.entry_mode == "retest_only":
                event = rt_quality
            elif cf.entry_mode == "breakout_only":
                event = first_break
            elif cf.entry_mode == "breakout_or_retest":
                event = first_break or rt_quality
            else:
                event = brk
            confirmed = event and bal_ok
            if not confirmed:
                continue
            dist = (c - P.stop) if s == 1 else (P.stop - c)
            r_atr = dist / atr if (not isn(atr) and atr > 0) else NAN
            r_ok = dist > 0 and (not isn(r_atr)) and cf.min_risk_atr <= r_atr <= cf.max_risk_atr
            part_ok = (not self.gate_participation) or P.brk_vol or rt_quality
            room_ok = True
            if self.gate_room and i >= cf.obstacle_lookback and r_ok:
                if s == 1:
                    res = max(self.H[i - cf.obstacle_lookback:i])
                    room_ok = res <= c or res - c >= dist * cf.min_room_r
                else:
                    sup = min(self.L[i - cf.obstacle_lookback:i])
                    room_ok = sup >= c or c - sup >= dist * cf.min_room_r
            risk_ok[s] = r_ok
            gates_ok[s] = r_ok and part_ok and room_ok

        # ---- entry submission (the broker fills at the next open)
        entry = None
        can_submit = pos == 0 and not self.order_in_flight
        for s in (1, -1):
            enabled = cf.enable_longs if s == 1 else cf.enable_shorts
            if can_submit and enabled and gates_ok[s] and not gates_ok[-s] and entry is None:
                P = self.pend[s]
                qty = qty_fn(c, P.stop)
                if qty > 0:
                    entry = {"side": s, "stop": P.stop, "qty": qty, "signal_close": c, "name": P.name,
                             "score": P.score, "time": t}
                    self.pend[1].reset()
                    self.pend[-1].reset()
                    self.order_in_flight = True

        # ---- exits that close at the next open (stop/target are handled by the broker)
        close = False
        if cf.exit_on_opposite:
            if pos > 0 and risk_ok[-1]:
                close = True
                self.pend[-1].reset()
            if pos < 0 and risk_ok[1]:
                close = True
                self.pend[1].reset()
        if cf.exit_on_trend_flip:
            if (pos > 0 and trend_down) or (pos < 0 and trend_up):
                close = True

        self.prev_bull_bg, self.prev_bear_bg = bull_bg, bear_bg
        info = {"trend": trend, "phase": phase, "htf": self.htf_bias, "bull_score": bull_score, "bear_score": bear_score,
                "atr": atr, "armed_long": self.pend[1].active, "armed_short": self.pend[-1].active}
        return {"entry": entry, "close": close, "info": info}


# ============================================================================ SIZING / SIMULATION
def make_qty_fn(cfg: Cfg, equity: float):
    def f(entry, stop):
        dist = abs(entry - stop)
        per_unit = dist + entry * cfg.fee_pct / 100.0 * 2.0       # cost-adjusted loss per unit (course p.109)
        if not per_unit > 0:
            return 0.0
        qty = cfg.risk_usd / per_unit
        qty = min(qty, equity / entry)                            # no leverage
        qty = math.floor(qty / cfg.qty_step) * cfg.qty_step
        return qty if qty * entry >= cfg.min_notional else 0.0
    return f


def _exit_trade(pos, price, t, reason, cfg):
    s, q, e = pos["side"], pos["qty"], pos["entry"]
    fee = (e + price) * q * cfg.fee_pct / 100.0
    pnl = (price - e) * q * s - fee
    return {"symbol": pos.get("symbol", ""), "side": "long" if s == 1 else "short", "entry_time": pos["entry_time"],
            "exit_time": t, "entry": e, "exit": price, "stop": pos["stop"], "qty": q, "pnl": pnl,
            "R": pnl / cfg.risk_usd, "reason": reason, "setup": pos.get("setup", ""), "score": pos.get("score", 0)}


def intrabar_exit(pos, o, h, l, cfg):
    """Returns (price, reason) if stop/target hit on this bar. Stop wins if both are touched (conservative)."""
    slip = cfg.slip_pct / 100.0
    if pos["side"] == 1:
        if l <= pos["stop"]:
            return min(pos["stop"], o) * (1 - slip), "stop"
        if h >= pos["target"]:
            return max(pos["target"], o), "target"
    else:
        if h >= pos["stop"]:
            return max(pos["stop"], o) * (1 + slip), "stop"
        if l <= pos["target"]:
            return min(pos["target"], o), "target"
    return None


def simulate(candles, cfg: Cfg, symbol="", final_pos=None, equity=None):
    """candles: list of (t,o,h,l,c,v) closed bars. Returns (closed_trades, last_bar_decision).
    final_pos overrides the position the engine sees on the LAST bar (used by paper/live to reflect reality)."""
    eng = Engine(cfg)
    qty_fn = make_qty_fn(cfg, equity if equity else cfg.equity)
    slip = cfg.slip_pct / 100.0
    trades, pos, pend_entry, pend_close, last_dec = [], None, None, False, None
    n = len(candles)
    for k, (t, o, h, l, c, v) in enumerate(candles):
        if pos and pend_close:
            px = o * (1 - slip) if pos["side"] == 1 else o * (1 + slip)
            trades.append(_exit_trade(pos, px, t, "signal exit", cfg))
            pos, pend_close, eng.order_in_flight = None, False, False
        if pend_entry and pos is None:
            s = pend_entry["side"]
            fill = o * (1 + slip) if s == 1 else o * (1 - slip)
            risk = (fill - pend_entry["stop"]) * s
            pos = {"symbol": symbol, "side": s, "qty": pend_entry["qty"], "entry": fill, "stop": pend_entry["stop"],
                   "entry_time": t, "setup": pend_entry["name"], "score": pend_entry["score"]}
            # target is re-anchored to the actual fill and the ORIGINAL stop (course p.105)
            pos["target"] = fill + s * max(risk, 0.0) * cfg.reward_risk
            pend_entry = None
        if pos:
            res = intrabar_exit(pos, o, h, l, cfg)
            if res or (pos["entry"] - pos["stop"]) * pos["side"] <= 0:
                px, why = res if res else (pos["entry"], "fill past stop")
                trades.append(_exit_trade(pos, px, t, why, cfg))
                pos, pend_close, eng.order_in_flight = None, False, False
        last = k == n - 1
        side = pos["side"] if pos else 0
        if last and final_pos is not None:
            side = final_pos
            eng.order_in_flight = final_pos != 0
        dec = eng.on_bar(t, o, h, l, c, v, side, qty_fn)
        if last:
            last_dec = dec
        if dec["entry"] and not (last and final_pos is not None):
            pend_entry = dec["entry"]
        if dec["close"] and pos:
            pend_close = True
    return trades, last_dec


# ============================================================================ STATS
def summarize(trades):
    n = len(trades)
    if n == 0:
        return {"n": 0, "win": 0.0, "net_R": 0.0, "pf": 0.0, "avg_R": 0.0, "max_dd_R": 0.0}
    rs = [x["R"] for x in sorted(trades, key=lambda x: x["exit_time"])]
    gw = sum(r for r in rs if r > 0)
    gl = -sum(r for r in rs if r < 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win": 100.0 * sum(1 for r in rs if r > 0) / n, "net_R": sum(rs),
            "pf": (gw / gl) if gl > 0 else float("inf"), "avg_R": sum(rs) / n, "max_dd_R": dd}


def fmt(s):
    return (f"trades {s['n']:4d} | win {s['win']:5.1f}% | net {s['net_R']:+7.1f}R | PF {s['pf']:.2f} | "
            f"avg {s['avg_R']:+.2f}R | maxDD {s['max_dd_R']:.1f}R")


def print_report(trades, title):
    print(f"\n=== {title} ===")
    print("ALL      ", fmt(summarize(trades)))
    ts = sorted(trades, key=lambda x: x["exit_time"])
    if len(ts) >= 10:
        cut = int(len(ts) * 0.7)
        print("EARLY 70%", fmt(summarize(ts[:cut])))
        print("LATE  30%", fmt(summarize(ts[cut:])))
    if len(ts) < 30:
        print("WARNING: fewer than 30 trades - this is not enough evidence about anything.")
    elif summarize(ts)["pf"] < 1.1:
        print("VERDICT: no proven edge (profit factor under 1.1 after costs).")


# ============================================================================ DATA
GRAN = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}


def fetch_coinbase(product, gran, start_ts, end_ts):
    import requests
    out, cur_end = {}, end_ts
    while cur_end > start_ts:
        cur_start = max(start_ts, cur_end - 299 * gran)
        r = requests.get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                         params={"granularity": gran, "start": datetime.fromtimestamp(cur_start, timezone.utc).isoformat(),
                                 "end": datetime.fromtimestamp(cur_end, timezone.utc).isoformat()},
                         headers={"User-Agent": "gwvsa-bot"}, timeout=20)
        r.raise_for_status()
        for t, lo, hi, op, cl, vo in r.json():
            out[int(t)] = (int(t), float(op), float(hi), float(lo), float(cl), float(vo))
        cur_end = cur_start
        time.sleep(0.2)
    now = time.time()
    return [r for _, r in sorted(out.items()) if r[0] + gran <= now]   # closed candles only


def cached_candles(product, tf, days):
    gran = GRAN[tf]
    os.makedirs("cache", exist_ok=True)
    path = f"cache/{product}_{tf}_{days}d_{datetime.now(timezone.utc):%Y%m%d}.json"
    if os.path.exists(path):
        return [tuple(r) for r in json.load(open(path))]
    rows = fetch_coinbase(product, gran, int(time.time()) - days * 86400, int(time.time()))
    json.dump(rows, open(path, "w"))
    return rows


DEFAULT_SYMBOLS = ("BTC-USD,ETH-USD,SOL-USD,XRP-USD,DOGE-USD,ADA-USD,AVAX-USD,LINK-USD,LTC-USD,DOT-USD,"
                   "BCH-USD,ATOM-USD,NEAR-USD,AAVE-USD,UNI-USD,SHIB-USD,HBAR-USD,XLM-USD")


# ============================================================================ BROKERS
class KrakenBroker:
    """Kraken SPOT, long only. UNTESTED against a live account - verify with tiny size first."""

    def __init__(self):
        import ccxt
        self.ex = ccxt.kraken({"apiKey": os.environ["KRAKEN_API_KEY"], "secret": os.environ["KRAKEN_API_SECRET"],
                               "enableRateLimit": True})
        self.ex.load_markets()

    @staticmethod
    def sym(product):
        return product.replace("-", "/")

    def balance_usd(self):
        return float(self.ex.fetch_balance()["free"].get("USD", 0.0))

    def buy_with_stop(self, product, qty, stop):
        s = self.sym(product)
        mk = self.ex.markets.get(s)
        if not mk:
            raise RuntimeError(f"{s} not on Kraken")
        qty = float(self.ex.amount_to_precision(s, qty))
        mn = (mk.get("limits", {}).get("amount", {}) or {}).get("min")
        if mn and qty < mn:
            raise RuntimeError(f"qty {qty} below Kraken minimum {mn}")
        o = self.ex.fetch_order(self.ex.create_order(s, "market", "buy", qty)["id"], s)
        fill = float(o.get("average") or o.get("price"))
        filled = float(o.get("filled") or qty)
        sl = self.ex.create_order(s, "market", "sell", filled, None, {"stopLossPrice": float(self.ex.price_to_precision(s, stop))})
        return fill, filled, sl["id"]

    def stop_fill(self, product, stop_id):
        o = self.ex.fetch_order(stop_id, self.sym(product))
        if o.get("status") == "closed":
            return float(o.get("average") or o.get("price"))
        return None

    def market_sell(self, product, qty, stop_id):
        s = self.sym(product)
        try:
            self.ex.cancel_order(stop_id, s)
        except Exception as e:  # stop may already have triggered
            print("cancel stop:", e)
        o = self.ex.fetch_order(self.ex.create_order(s, "market", "sell", qty)["id"], s)
        return float(o.get("average") or o.get("price"))


# ============================================================================ PAPER / LIVE RUNNER
STATE_FILE = "bot_state.json"


def load_state():
    if os.path.exists(STATE_FILE):
        return json.load(open(STATE_FILE))
    return {"open": {}, "closed": [], "last_bar": {}}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), indent=1)


def run_once(cfg, symbols, tf, mode, max_open, broker=None):
    st = load_state()
    gran = GRAN[tf]
    slip = cfg.slip_pct / 100.0
    live = mode == "live"
    equity = broker.balance_usd() if live else cfg.equity
    kill = os.path.exists("STOP")
    for sym in symbols:
        try:
            candles = fetch_coinbase(sym, gran, int(time.time()) - 900 * gran, int(time.time()))
        except Exception as e:
            print(f"{sym}: data error {e}")
            continue
        if len(candles) < 300:
            print(f"{sym}: not enough candles")
            continue
        ts = candles[-1][0]
        if st["last_bar"].get(sym) == ts:
            continue
        usd_vol = sum(r[4] * r[5] for r in candles[-24:]) / 24
        pos = st["open"].get(sym)
        # 1) manage an open position on bars that closed since last pass
        if pos:
            for (t, o, h, l, c, v) in candles:
                if t <= pos["checked"]:
                    continue
                pos["checked"] = t
                if live:
                    fill = broker.stop_fill(sym, pos["stop_id"])
                    px, why = (fill, "stop") if fill else (None, None)
                    if px is None and h >= pos["target"]:
                        px, why = broker.market_sell(sym, pos["qty"], pos["stop_id"]), "target"
                else:
                    res = intrabar_exit(pos, o, h, l, cfg)
                    px, why = res if res else (None, None)
                if px is not None:
                    tr = _exit_trade({**pos, "symbol": sym}, px, t, why, cfg)
                    st["closed"].append(tr)
                    print(f"{sym}: CLOSED {why} {tr['R']:+.2f}R")
                    del st["open"][sym]
                    pos = None
                    break
        # 2) engine decision on the last closed bar
        _, dec = simulate(candles, cfg, sym, final_pos=(1 if pos else 0), equity=equity)
        if pos and dec["close"]:
            px = broker.market_sell(sym, pos["qty"], pos["stop_id"]) if live else candles[-1][4] * (1 - slip)
            tr = _exit_trade({**pos, "symbol": sym}, px, ts, "signal exit", cfg)
            st["closed"].append(tr)
            print(f"{sym}: CLOSED signal exit {tr['R']:+.2f}R")
            del st["open"][sym]
            pos = None
        # 3) new entry
        e = dec["entry"]
        if e and not pos and e["side"] == 1:
            if kill or len(st["open"]) >= max_open or usd_vol < 20000:
                print(f"{sym}: signal skipped (kill-switch / max open / thin volume)")
            else:
                fill = candles[-1][4] * (1 + slip)
                stop_id = None
                if live:
                    try:
                        fill, e["qty"], stop_id = broker.buy_with_stop(sym, e["qty"], e["stop"])
                    except Exception as ex_:
                        print(f"{sym}: LIVE ORDER FAILED: {ex_}")
                        fill = None
                if fill and fill > e["stop"]:
                    risk = fill - e["stop"]
                    st["open"][sym] = {"side": 1, "qty": e["qty"], "entry": fill, "stop": e["stop"],
                                       "target": fill + risk * cfg.reward_risk, "entry_time": ts,
                                       "checked": ts, "setup": e["name"], "score": e["score"], "stop_id": stop_id}
                    print(f"{sym}: {mode.upper()} LONG {e['name']} score {e['score']} qty {e['qty']:.6f} "
                          f"entry {fill:.6g} stop {e['stop']:.6g} target {fill + risk * cfg.reward_risk:.6g}")
        st["last_bar"][sym] = ts
        time.sleep(0.2)
    save_state(st)
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z] open {len(st['open'])} | closed {len(st['closed'])}")


# ============================================================================ SELF TEST
def synthetic_candles(n=6000, seed=7):
    rnd = random.Random(seed)
    t, price, rows, drift, left = 1_700_000_000 // 3600 * 3600, 100.0, [], 0.0, 0
    for _ in range(n):
        if left <= 0:
            drift, left = rnd.choice([-0.0015, 0.0, 0.0018]), rnd.randint(40, 200)
        left -= 1
        o = price
        c = o * (1 + drift + rnd.gauss(0, 0.008))
        h = max(o, c) * (1 + abs(rnd.gauss(0, 0.004)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, 0.004)))
        v = math.exp(rnd.gauss(8, 0.5)) * (1 + 6 * abs(c - o) / o * 50)
        rows.append((t, o, h, l, c, v))
        price, t = c, t + 3600
    return rows


def selftest():
    cfg = Cfg(enable_shorts=True)
    trades, dec = simulate(synthetic_candles(), cfg, "SYNTH")
    print_report(trades, "SELFTEST (synthetic data - proves the code runs, says nothing about profit)")
    assert all(x["qty"] > 0 for x in trades)
    assert all(abs(x["stop"]) > 0 for x in trades)
    print("last-bar decision:", dec["info"])
    print("selftest OK")


# ============================================================================ CLI
def main():
    ap = argparse.ArgumentParser(description="Gann-Wyckoff-VSA v7.2 bot")
    ap.add_argument("cmd", choices=["selftest", "backtest", "paper", "live", "report"])
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--tf", default="1h", choices=list(GRAN))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--risk", type=float, default=2.0, help="USD risked per trade (1R)")
    ap.add_argument("--equity", type=float, default=400.0)
    ap.add_argument("--fee", type=float, default=0.10, help="percent per side")
    ap.add_argument("--slip", type=float, default=0.15, help="percent per side")
    ap.add_argument("--frequency", default="original", choices=["original", "more", "max"])
    ap.add_argument("--shorts", action="store_true", help="research backtest only; spot cannot short")
    ap.add_argument("--max-open", type=int, default=3)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--confirm-live", action="store_true")
    a = ap.parse_args()

    cfg = Cfg(timeframe_sec=GRAN[a.tf], risk_usd=a.risk, equity=a.equity, fee_pct=a.fee, slip_pct=a.slip,
              frequency=a.frequency, enable_shorts=a.shorts and a.cmd == "backtest")
    symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]

    if a.cmd == "selftest":
        selftest()
    elif a.cmd == "backtest":
        allt = []
        for sym in symbols:
            try:
                candles = cached_candles(sym, a.tf, a.days)
            except Exception as e:
                print(f"{sym}: data error {e}")
                continue
            trades, _ = simulate(candles, cfg, sym)
            allt += trades
            print(f"{sym:10s} bars {len(candles):5d} | {fmt(summarize(trades))}")
        print_report(allt, f"ALL SYMBOLS | {a.tf} | {a.days}d | fee {a.fee}% slip {a.slip}% per side")
        if allt:
            with open("backtest_trades.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(allt[0].keys()))
                w.writeheader()
                w.writerows(allt)
            print("trades saved to backtest_trades.csv")
    elif a.cmd == "report":
        st = load_state()
        print_report(st["closed"], "PAPER/LIVE CLOSED TRADES")
        print("open positions:", json.dumps(st["open"], indent=1))
    else:
        broker = None
        if a.cmd == "live":
            if not a.confirm_live:
                sys.exit("Refusing to trade real money without --confirm-live.")
            broker = KrakenBroker()
        while True:
            run_once(cfg, symbols, a.tf, a.cmd, a.max_open, broker)
            if not a.loop:
                break
            time.sleep(max(30, GRAN[a.tf] - (time.time() % GRAN[a.tf]) + 20))


if __name__ == "__main__":
    main()
