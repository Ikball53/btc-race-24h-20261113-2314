"""Washout-thrust BTC bot for the make.fun 24h cbBTC race.

Idea: this market grinds down most of the day and pays only on the rare
violent reversals. The bot sits in cash, waits for a washout (the price
falls hard in 15 minutes), then buys the first strong recovery thrust
after it. It never chases a rally that is already running, never holds
through the grind, and cuts any trade that stalls.

Entry (long only, whole cash):
  - a washout happened in the last 45 bars: some bar's 15-minute return
    is below -85 bps, and
  - this bar's 15-minute return is above +15 bps (the thrust is real).

Exit (sell everything), whichever comes first:
  - take profit at +120 bps over the fill,
  - stop loss at -60 bps under the fill,
  - trailing stop: 30 bps below the best close since the fill,
  - stall: 8 bars after the fill the trade is still under +15 bps.

If nothing has traded by bar 1200 the washout bar is lowered to -60 bps
and the thrust bar to +10 bps, so the bot still takes at least one
trade and meets the race guardrail instead of finishing flat.

All arithmetic is integer basis points on the bar closes, so two runs
agree byte for byte. No clock, no network, no randomness.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# Entry: washout depth (bps over 15 minutes), lookback window (bars),
# and the recovery thrust (bps over 15 minutes) that confirms it.
WASHOUT_BPS = 85
WASHOUT_WINDOW = 45
THRUST_BPS = 15

# Exits, all in bps against the fill price unless noted.
TAKE_PROFIT_BPS = 120
STOP_LOSS_BPS = 60
TRAIL_BPS = 30
STALL_BARS = 8
STALL_GAIN_BPS = 15

# Bars to wait after an exit before a fresh entry.
COOLDOWN_BARS = 30

# Late-window fallback: if no trade has filled by this bar, soften the
# entry bars so the bot still trades at least once.
FALLBACK_BAR = 1200
FALLBACK_WASHOUT_BPS = 60
FALLBACK_THRUST_BPS = 10

# Slippage cap on every order, in bps from the latest close.
MAX_SLIPPAGE_BPS = 200

# Bars of history needed before the first decision (washout window plus
# the 15-minute returns it scans).
WARMUP_BARS = 70


def bps_move(new_price, old_price):
    """Return of new_price over old_price in integer basis points."""
    return (new_price - old_price) * 10000 // old_price


class WashoutThrustBot:
    """One-asset trend-catcher: cash until a washout plus a thrust."""

    def __init__(self, state):
        self.state = state
        self.closes = []
        self.entry_price = 0
        self.entry_seq = 0
        self.peak_close = 0
        self.last_exit_seq = -10 ** 9
        self.trades = 0
        self.bought = False

    def note_fills(self, batch):
        """Fold this batch's fills into the trade record."""
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.entry_seq = self.state.seq
                self.peak_close = self.entry_price
                self.bought = True
            elif fill.get("side") == "sell":
                self.entry_price = 0
                self.peak_close = 0

    def washout_seen(self, washout_bps, window):
        """True when some bar in the window fell washout_bps in 15 minutes."""
        closes = self.closes
        start = max(16, len(closes) - window)
        for j in range(start, len(closes)):
            if bps_move(closes[j], closes[j - 15]) < -washout_bps:
                return True
        return False

    def decide(self, batch):
        """The orders for this batch, as shim order dicts."""
        state = self.state
        if state.warming_up:
            return []
        # Fills first: a fill can arrive on a batch that carries no bar,
        # and the exit logic below needs the entry price it reports.
        self.note_fills(batch)
        bars = [b for b in batch.get("bars", []) if b.get("symbol") == SYMBOL]
        if not bars:
            return []
        close = int(bars[0]["c"])
        self.closes.append(close)
        if len(self.closes) <= WARMUP_BARS:
            return []
        held = state.positions.get(SYMBOL)
        holding = held is not None and held.qty > 0
        if holding:
            return self.exit_orders(state)
        return self.entry_orders(state)

    def entry_orders(self, state):
        """Buy the whole cash on a washout plus a thrust, else nothing."""
        if state.seq - self.last_exit_seq < COOLDOWN_BARS:
            return []
        closes = self.closes
        thrust_now = bps_move(closes[-1], closes[-16])
        if self.trades == 0 and not self.bought and state.seq >= FALLBACK_BAR:
            washout_bps = FALLBACK_WASHOUT_BPS
            thrust_bps = FALLBACK_THRUST_BPS
        else:
            washout_bps = WASHOUT_BPS
            thrust_bps = THRUST_BPS
        if thrust_now <= thrust_bps:
            return []
        if not self.washout_seen(washout_bps, WASHOUT_WINDOW):
            return []
        spend = state.cash
        room = state.weight_room(SYMBOL)
        if room < spend:
            spend = room
        if spend < 1000000:
            return []
        return [{"side": "buy", "symbol": SYMBOL,
                 "notional_quote_units": spend,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def exit_orders(self, state):
        """Sell everything on take-profit, stop, trail or stall."""
        held = state.positions[SYMBOL]
        if self.entry_price <= 0 and held.qty > 0:
            # Fill record missed (for instance a gap bar): recover the
            # entry price from the position's cost, fees included.
            self.entry_price = held.cost * 10 ** 7 // held.qty
            self.peak_close = max(self.peak_close, self.entry_price)
        close = self.closes[-1]
        if close > self.peak_close:
            self.peak_close = close
        gain = bps_move(close, self.entry_price)
        trail = bps_move(self.peak_close, close)
        loss = -gain
        age = state.seq - self.entry_seq
        stalled = age >= STALL_BARS and gain < STALL_GAIN_BPS
        if (gain >= TAKE_PROFIT_BPS or loss >= STOP_LOSS_BPS
                or trail >= TRAIL_BPS or stalled):
            self.last_exit_seq = state.seq
            self.trades += 1
            return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                     "max_slippage_bps": MAX_SLIPPAGE_BPS}]
        return []


def on_batch(state, batch):
    if not hasattr(state, "bot"):
        state.bot = WashoutThrustBot(state)
    return state.bot.decide(batch)


makefun_bars.run(on_batch)
