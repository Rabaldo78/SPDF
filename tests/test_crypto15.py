import math

import pytest

from crypto15.fair_value import EwmaVol, annual_to_per_sec, per_sec_to_annual, prob_up, window_start
from crypto15.feeds import SpotBook
from crypto15.polymarket import MarketFeed, slug_for
from crypto15.quoter import CoinQuoter, QuoteParams

SIG = annual_to_per_sec(0.45)
T0 = 1_760_371_200  # a window boundary
MKT = {"slug": "btc-updown-15m-1760371200", "condition_id": "c", "up_token": "UP", "down_token": "DN"}


def test_window_math():
    assert T0 % 900 == 0
    assert window_start(T0 + 899.9) == T0
    assert window_start(T0 + 900) == T0 + 900
    assert slug_for("BTC", T0) == "btc-updown-15m-1760371200"


def test_prob_up():
    assert prob_up(100, 100, SIG, 450) == pytest.approx(0.5)
    assert prob_up(100.05, 100, SIG, 450) > 0.5
    assert prob_up(99.95, 100, SIG, 450) < 0.5
    # same move matters more with less time left
    assert prob_up(100.05, 100, SIG, 30) > prob_up(100.05, 100, SIG, 600)
    assert prob_up(100, 100, SIG, 0) == 1.0  # tie resolves Up
    assert prob_up(99.9, 100, SIG, 0) == 0.0
    # symmetry
    assert prob_up(101, 100, SIG, 300) + prob_up(100 * 100 / 101, 100, SIG, 300) == pytest.approx(1.0)


def test_ewma_vol_tracks_realized():
    v = EwmaVol(seed_annual_vol=0.1, half_life=60, floor_annual_vol=0.01)
    px, sd = 100.0, annual_to_per_sec(0.8)
    import random
    random.seed(1)
    for t in range(3000):
        px *= math.exp(random.gauss(0, sd))
        v.update(px, t)
    assert per_sec_to_annual(v.sigma) == pytest.approx(0.8, rel=0.25)


def test_spot_book_composite_and_staleness():
    b = SpotBook()
    b.update("BTC", "coinbase", 100.0, 100.2, ts=10)
    b.update("BTC", "binance", 100.1, 100.3, ts=10)
    assert b.mid("BTC", now=11) == pytest.approx(100.15)
    assert b.divergence_bps("BTC", now=11) == pytest.approx(0.1 / 100.1 * 1e4)
    assert b.mid("BTC", now=20) is None
    b.update("BTC", "binance", 101, 100, ts=10)  # crossed quote ignored
    assert b.quotes["BTC"]["binance"].bid == 100.1


def quoter_at_open(**kw):
    q = CoinQuoter("BTC", QuoteParams(**kw), seed_annual_vol=0.45)
    q.on_spot(100_000.0, T0 + 1)
    q.set_market(T0, MKT)
    q.on_spot(100_000.0, T0 + 2)
    return q


def test_no_strike_when_started_mid_window():
    q = CoinQuoter("BTC")
    q.on_spot(100_000.0, T0 + 400)
    q.set_market(T0, MKT)
    q.on_spot(100_000.0, T0 + 401)
    assert q.window.strike is None and q.bid is None and q.ask is None


def test_quotes_straddle_fair_value():
    q = quoter_at_open()
    assert q.fv == pytest.approx(0.5)
    assert q.bid < q.fv < q.ask
    assert q.bid >= 0.01 and q.ask <= 0.99


def test_fill_rules_and_down_token_mapping():
    q = quoter_at_open(size=10)
    bid, ask = q.bid, q.ask
    q.on_trade("UP", bid, 50, "SELL", T0 + 3)        # at touch: not filled by default
    assert q.window.inventory == 0
    q.on_trade("UP", bid - 0.01, 50, "SELL", T0 + 3)  # through our bid: we buy 10
    assert q.window.inventory == 10
    assert q.window.cash == pytest.approx(-10 * bid)
    # Taker buys Down, which is a taker sell of Up below our bid, so we buy
    q.on_trade("DN", 1 - (q.bid - 0.01), 50, "BUY", T0 + 4)
    assert q.window.inventory == 20
    # Taker sells Down cheap -> buys Up above our ask -> we sell
    q.on_trade("DN", 1 - (q.ask + 0.01), 50, "SELL", T0 + 5)
    assert q.window.inventory == 10
    q.on_trade("OTHER", 0.01, 50, "SELL", T0 + 5)
    assert q.window.inventory == 10


def test_fill_at_touch_option():
    q = quoter_at_open(size=10, fill_at_touch=True)
    q.on_trade("UP", q.bid, 50, "SELL", T0 + 3)
    assert q.window.inventory == 10


def test_inventory_skew_and_limits():
    q = quoter_at_open(size=100, max_inventory=200)
    bid0 = q.bid
    q.on_trade("UP", 0.01, 1000, "SELL", T0 + 3)
    assert q.window.inventory == 100
    assert q.bid <= bid0  # long, so quotes lean lower
    q.on_trade("UP", 0.01, 1000, "SELL", T0 + 4)
    assert q.window.inventory == 200
    assert q.bid is None and q.ask is not None


def test_stops_near_end_and_when_venues_disagree():
    q = quoter_at_open()
    q.on_spot(100_000.0, T0 + 880)
    assert q.bid is None and q.ask is None
    q2 = quoter_at_open(max_divergence_bps=10)
    q2.on_spot(100_000.0, T0 + 10, divergence_bps=20)
    assert q2.bid is None


def test_settlement_pnl():
    q = quoter_at_open(size=10)
    bid = q.bid
    q.on_trade("UP", bid - 0.01, 10, "SELL", T0 + 3)
    q.on_spot(100_050.0, T0 + 900.2)  # first tick of next window settles the last one
    s = q.settlements[0]
    assert s["result"] == "Up"
    assert s["pnl"] == pytest.approx(10 * (1 - bid))
    assert q.window.start == T0 + 900 and q.window.strike == 100_050.0


def test_markout_recorded():
    q = quoter_at_open(size=10, markout_s=10)
    q.on_trade("UP", q.bid - 0.01, 10, "SELL", T0 + 3)
    q.on_spot(100_100.0, T0 + 14)
    f = q.window.fills[0]
    assert f.fv_markout is not None and f.fv_markout > f.fv


def test_market_feed_parsing():
    trades = []
    mf = MarketFeed(on_trade=lambda *a: trades.append(a))
    mf.handle({"event_type": "book", "asset_id": "UP",
               "bids": [{"price": "0.48", "size": "100"}, {"price": "0.47", "size": "5"}],
               "asks": [{"price": "0.52", "size": "80"}]})
    b = mf.book("UP")
    assert (b.best_bid, b.best_ask, b.mid) == (0.48, 0.52, 0.50)
    mf.handle({"event_type": "price_change", "price_changes": [
        {"asset_id": "UP", "price": "0.49", "size": "20", "side": "BUY"},
        {"asset_id": "UP", "price": "0.52", "size": "0", "side": "SELL"}]})
    assert b.best_bid == 0.49 and b.best_ask is None
    mf.handle({"event_type": "price_change", "asset_id": "UP",
               "changes": [{"price": "0.53", "size": "7", "side": "SELL"}]})
    assert b.best_ask == 0.53
    mf.handle({"event_type": "last_trade_price", "asset_id": "UP", "price": "0.49", "size": "3", "side": "sell"})
    assert trades == [("UP", 0.49, 3.0, "SELL")]
