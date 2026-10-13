"""Fair value for Polymarket 15 minute Up/Down crypto markets.

Each market asks whether the coin's price at the end of a 15 minute window is
at or above its price at the start of the window. That is a cash-or-nothing
binary option, so with zero drift:

    P(Up) = N( ln(S / K) / (sigma * sqrt(t)) )

S is the live spot, K is the window's opening price ("price to beat"),
sigma is volatility per sqrt(second), t is seconds left in the window.
"""

import math

WINDOW_SECONDS = 900
SECONDS_PER_YEAR = 365 * 24 * 3600


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def window_start(ts):
    """Unix start of the 15 minute window that contains ts."""
    return int(ts) - int(ts) % WINDOW_SECONDS


def seconds_left(ts):
    return window_start(ts) + WINDOW_SECONDS - ts


def annual_to_per_sec(annual_vol):
    return annual_vol / math.sqrt(SECONDS_PER_YEAR)


def per_sec_to_annual(sigma):
    return sigma * math.sqrt(SECONDS_PER_YEAR)


def prob_up(spot, strike, sigma, secs_left):
    """Probability the window settles Up (end price >= start price)."""
    if secs_left <= 0 or sigma <= 0:
        return 1.0 if spot >= strike else 0.0
    d = math.log(spot / strike) / (sigma * math.sqrt(secs_left))
    return norm_cdf(d)


def prob_up_delta(spot, strike, sigma, secs_left):
    """Change in P(Up) per 1.00 move in spot. Shows how jumpy the quote is."""
    if secs_left <= 0 or sigma <= 0:
        return 0.0
    st_ = sigma * math.sqrt(secs_left)
    d = math.log(spot / strike) / st_
    return math.exp(-0.5 * d * d) / math.sqrt(2 * math.pi) / (spot * st_)


class EwmaVol:
    """Realized volatility per sqrt(second), from prices sampled about once a second.

    half_life is in seconds. Seeds from an annualized guess so the model has a
    number before enough ticks arrive.
    """

    def __init__(self, seed_annual_vol=0.5, half_life=300.0, floor_annual_vol=0.10):
        self.var = annual_to_per_sec(seed_annual_vol) ** 2
        self.half_life = half_life
        self.floor = annual_to_per_sec(floor_annual_vol)
        self.last_px = None
        self.last_ts = None

    def update(self, px, ts):
        if self.last_px is None:
            self.last_px, self.last_ts = px, ts
            return
        dt = ts - self.last_ts
        if dt <= 0:
            return
        r = math.log(px / self.last_px)
        alpha = 1.0 - 0.5 ** (dt / self.half_life)
        self.var = (1 - alpha) * self.var + alpha * (r * r / dt)
        self.last_px, self.last_ts = px, ts

    @property
    def sigma(self):
        return max(math.sqrt(self.var), self.floor)
