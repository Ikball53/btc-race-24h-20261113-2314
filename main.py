"""Washout-thrust BTC race bot: buy the sharp dip-reversal, ride it with a trail.

The bot sits in cash while the market grinds down and only buys when a real
washout reverses: the worst 15-minute return over the last 45 bars is at most
-85 bps and the current 15-minute return is back up at least +15 bps. It goes
all in with the whole cash, one position at a time, and exits on a +120 bps
take-profit, a -60 bps stop-loss, a 30 bps trailing stop below the best close
since the fill, or a stall exit when the trade is still under +15 bps eight
bars after the fill. After each exit it waits 30 bars before trading again.
If nothing has traded by bar 1200 the entry softens to -60 / +10 bps so the
bot still takes at least one trade and meets the race guardrail.

All arithmetic is integer basis points on bar closes; no clock, no network,
no randomness, so two runs on the same data produce identical orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
SLIP_BPS = 100

WASHOUT_BPS = 85
THRUST_BPS = 15
SOFT_BAR = 1200
SOFT_WASHOUT_BPS = 60
SOFT_THRUST_BPS = 10
TAKE_PROFIT_BPS = 120
STOP_LOSS_BPS = 60
TRAIL_BPS = 30
STALL_BARS = 8
STALL_PROFIT_BPS = 15
COOLDOWN_BARS = 30
LAST_ENTRY_BAR = 1380
LOOKBACK_BARS = 45
RETURN_BARS = 15
MIN_HISTORY_BARS = 60


class Trader:
    """The bot's whole memory: closes seen, the open trade, and the cooldown."""

    def __init__(self):
        self.closes = []
        self.entry_price = 0
        self.fill_seq = -1
        self.peak_close = 0
        self.cooldown_until = -1
        self.round_trips = 0

    def note_fill(self, batch):
        """Fold this batch's fills in: a buy sets the entry, a sell closes it."""
        for fill in batch.get("fills", []):
            if fill.get("symbol") != SYMBOL or fill.get("status") != "filled":
                continue
            if fill.get("side") == "buy":
                self.entry_price = int(fill["price"])
                self.fill_seq = batch["seq"]
                self.peak_close = self.closes[-1] if self.closes else self.entry_price
            elif fill.get("side") == "sell":
                self.entry_price = 0
                self.fill_seq = -1
                self.peak_close = 0
                self.round_trips += 1
                self.cooldown_until = batch["seq"] + COOLDOWN_BARS

    def worst_fifteen(self):
        """The worst 15-bar return over the recent window, in basis points."""
        closes = self.closes
        worst = 0
        start = max(RETURN_BARS, len(closes) - LOOKBACK_BARS)
        for j in range(start, len(closes)):
            ret = (closes[j] - closes[j - RETURN_BARS]) * 10000 // closes[j - RETURN_BARS]
            if ret < worst:
                worst = ret
        return worst

    def current_fifteen(self):
        """The current 15-bar return, in basis points."""
        closes = self.closes
        return (closes[-1] - closes[-1 - RETURN_BARS]) * 10000 // closes[-1 - RETURN_BARS]


TRADER = Trader()


def on_batch(state, batch):
    TRADER.note_fill(batch)
    if any(bar.get("symbol") == SYMBOL for bar in batch.get("bars", [])):
        close = state.close(SYMBOL)
        if close is not None:
            TRADER.closes.append(close)
    close = state.close(SYMBOL)
    if close is None or state.warming_up:
        return []

    holding = state.positions.get(SYMBOL)
    if holding is not None and holding.qty > 0:
        if TRADER.entry_price <= 0:
            return []
        if close > TRADER.peak_close:
            TRADER.peak_close = close
        profit = (close - TRADER.entry_price) * 10000 // TRADER.entry_price
        bars_held = state.seq - TRADER.fill_seq
        trail_line = TRADER.peak_close - TRADER.peak_close * TRAIL_BPS // 10000
        stalled = bars_held >= STALL_BARS and profit < STALL_PROFIT_BPS
        if (
            profit >= TAKE_PROFIT_BPS
            or profit <= -STOP_LOSS_BPS
            or close <= trail_line
            or stalled
        ):
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": holding.qty,
                    "max_slippage_bps": SLIP_BPS,
                }
            ]
        return []

    if (
        state.seq < MIN_HISTORY_BARS
        or state.seq > LAST_ENTRY_BAR
        or state.seq < TRADER.cooldown_until
        or len(TRADER.closes) <= RETURN_BARS
        or state.cash <= 0
    ):
        return []

    washout = WASHOUT_BPS
    thrust = THRUST_BPS
    if TRADER.round_trips == 0 and state.seq >= SOFT_BAR:
        washout = SOFT_WASHOUT_BPS
        thrust = SOFT_THRUST_BPS
    if TRADER.worst_fifteen() <= -washout and TRADER.current_fifteen() >= thrust:
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": SLIP_BPS,
            }
        ]
    return []


makefun_bars.run(on_batch)
