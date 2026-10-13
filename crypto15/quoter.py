"""Paper quoter for Polymarket 15 minute Up/Down markets.

Pure logic with the clock passed in, so it can be tested offline and replayed.
No orders are sent anywhere.

Positions are kept in "Up" terms. Selling Up at p pays the same as buying
Down at 1 - p, so a Down trade at q is treated as an Up trade at 1 - q with
the taker side flipped.

Fill rule: a resting paper quote fills only when a real trade prints through
it (a taker sells below our bid, or buys above our ask). Trades at exactly our
price are ignored by default because we cannot know our queue position. Set
fill_at_touch=True for an optimistic upper bound.
"""

import math
from dataclasses import dataclass, field

from .fair_value import EwmaVol, WINDOW_SECONDS, prob_up, prob_up_delta, window_start

TICK = 0.01


@dataclass
class QuoteParams:
    size: float = 25.0             # shares per quote
    base_half_spread: float = 0.02
    latency_s: float = 0.25        # how stale our quote may be when hit
    latency_z: float = 2.0         # spread cover for spot moving during latency_s
    max_inventory: float = 200.0   # shares, either direction
    inventory_skew: float = 0.02   # price shift at full inventory
    stop_before_end_s: float = 30.0
    min_fv: float = 0.03           # stop quoting when the market is nearly decided
    max_divergence_bps: float = 15.0
    strike_capture_s: float = 3.0  # strike must be seen within this many seconds of window open
    markout_s: float = 10.0
    fill_at_touch: bool = False


@dataclass
class Fill:
    ts: float
    coin: str
    window: int
    side: str           # we BUY or SELL Up
    price: float
    size: float
    fv: float
    spot: float
    fv_markout: float = None


@dataclass
class Window:
    start: int
    strike: float = None
    market: dict = None
    inventory: float = 0.0
    cash: float = 0.0
    fills: list = field(default_factory=list)


def _floor_tick(p):
    return math.floor(p / TICK + 1e-9) * TICK


def _ceil_tick(p):
    return math.ceil(p / TICK - 1e-9) * TICK


class CoinQuoter:
    def __init__(self, coin, params=None, seed_annual_vol=0.5):
        self.coin = coin
        self.p = params or QuoteParams()
        self.vol = EwmaVol(seed_annual_vol=seed_annual_vol)
        self.window = None
        self.spot = None
        self.divergence_bps = 0.0
        self.last_vol_sample = 0.0
        self.bid = self.ask = self.fv = None
        self.pending_markouts = []
        self.settlements = []
        self.new_fills = []

    # --- inputs ---

    def on_spot(self, spot, now, divergence_bps=0.0):
        self.spot = spot
        self.divergence_bps = divergence_bps
        if now - self.last_vol_sample >= 1.0:
            self.vol.update(spot, now)
            self.last_vol_sample = now
        self._roll(now)
        self._requote(now)
        self._check_markouts(now)

    def on_trade(self, token, price, size, taker_side, now):
        w = self.window
        if not w or not w.market or self.bid is None and self.ask is None:
            return
        if token == w.market["down_token"]:
            price = 1.0 - price
            taker_side = "SELL" if taker_side == "BUY" else "BUY"
        elif token != w.market["up_token"]:
            return
        through = (lambda a, b: a <= b) if self.p.fill_at_touch else (lambda a, b: a < b - 1e-9)
        if taker_side == "SELL" and self.bid is not None and through(price, self.bid):
            self._fill("BUY", self.bid, min(size, self.p.size), now)
        elif taker_side == "BUY" and self.ask is not None and through(-price, -self.ask):
            self._fill("SELL", self.ask, min(size, self.p.size), now)

    def set_market(self, start, market):
        if self.window and self.window.start == start:
            self.window.market = market

    # --- internals ---

    def _roll(self, now):
        start = window_start(now)
        if self.window and start > self.window.start:
            self._settle(self.spot)
            self.window = None
        if self.window is None:
            self.window = Window(start=start)
            if now - start <= self.p.strike_capture_s:
                self.window.strike = self.spot

    def _settle(self, final_spot):
        w = self.window
        if w.strike is None:
            return
        up = final_spot >= w.strike
        pnl = w.cash + w.inventory * (1.0 if up else 0.0)
        self.settlements.append({
            "coin": self.coin, "window": w.start, "strike": w.strike, "final": final_spot,
            "result": "Up" if up else "Down", "fills": len(w.fills),
            "end_inventory": w.inventory, "pnl": round(pnl, 4),
        })

    def _requote(self, now):
        self.bid = self.ask = None
        w = self.window
        if not w or w.strike is None or self.spot is None:
            self.fv = None
            return
        left = w.start + WINDOW_SECONDS - now
        sigma = self.vol.sigma
        self.fv = prob_up(self.spot, w.strike, sigma, left)
        if (w.market is None or left < self.p.stop_before_end_s
                or self.divergence_bps > self.p.max_divergence_bps
                or not self.p.min_fv <= self.fv <= 1 - self.p.min_fv):
            return
        # Spread covers the fair-value move from spot drifting during our latency.
        move = self.spot * sigma * math.sqrt(self.p.latency_s) * self.p.latency_z
        half = self.p.base_half_spread + prob_up_delta(self.spot, w.strike, sigma, left) * move
        skew = -self.p.inventory_skew * w.inventory / self.p.max_inventory
        bid = max(TICK, _floor_tick(self.fv - half + skew))
        ask = min(1 - TICK, _ceil_tick(self.fv + half + skew))
        if w.inventory < self.p.max_inventory and bid < self.fv:
            self.bid = round(bid, 2)
        if w.inventory > -self.p.max_inventory and ask > self.fv:
            self.ask = round(ask, 2)

    def _fill(self, side, price, size, now):
        w = self.window
        sign = 1 if side == "BUY" else -1
        w.inventory += sign * size
        w.cash -= sign * size * price
        f = Fill(now, self.coin, w.start, side, price, size, round(self.fv, 4), self.spot)
        w.fills.append(f)
        self.new_fills.append(f)
        self.pending_markouts.append(f)
        self._requote(now)

    def _check_markouts(self, now):
        keep = []
        for f in self.pending_markouts:
            if now - f.ts < self.p.markout_s:
                keep.append(f)
            elif self.window and self.window.start == f.window and self.fv is not None:
                f.fv_markout = round(self.fv, 4)
        self.pending_markouts = keep

    def snapshot(self, now):
        w = self.window
        return {
            "coin": self.coin,
            "spot": self.spot,
            "strike": w.strike if w else None,
            "window": w.start if w else None,
            "secs_left": round(w.start + WINDOW_SECONDS - now, 1) if w else None,
            "sigma_annual": round(self.vol.sigma * math.sqrt(365 * 24 * 3600), 4),
            "fv": round(self.fv, 4) if self.fv is not None else None,
            "bid": self.bid,
            "ask": self.ask,
            "inventory": w.inventory if w else 0.0,
            "cash": round(w.cash, 4) if w else 0.0,
            "market": w.market["slug"] if w and w.market else None,
            "divergence_bps": round(self.divergence_bps, 2),
        }
