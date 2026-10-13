"""Run the paper quoter against live Coinbase, Binance and Polymarket data.

    python -m crypto15.run_quoter                 # all four coins
    python -m crypto15.run_quoter --coins BTC ETH
    BINANCE_HOST=binance.us python -m crypto15.run_quoter

Writes to data/:
    quoter_state.json  live snapshot the Streamlit tab reads (once a second)
    fills.csv          every paper fill, with fair value at fill and 10s later
    settlements.csv    P&L per coin per 15 minute window
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import time
from dataclasses import asdict

from .fair_value import WINDOW_SECONDS, window_start
from .feeds import COINS, SpotBook, binance_feed, coinbase_feed
from .polymarket import MarketFeed, find_market
from .quoter import CoinQuoter, QuoteParams

log = logging.getLogger("crypto15")

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
SEED_VOL = {"BTC": 0.45, "ETH": 0.60, "SOL": 0.80, "XRP": 0.80}


def append_csv(path, rows):
    if not rows:
        return
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        if new:
            w.writeheader()
        w.writerows(rows)


class Runner:
    def __init__(self, coins, params):
        self.coins = coins
        self.quoters = {c: CoinQuoter(c, params, SEED_VOL.get(c, 0.6)) for c in coins}
        self.book = SpotBook(on_tick=self.on_tick)
        self.markets = MarketFeed(on_trade=self.on_trade)
        self.token_coin = {}
        self.looked_up = set()
        os.makedirs(DATA_DIR, exist_ok=True)

    def on_tick(self, coin):
        if coin not in self.quoters:
            return
        mid = self.book.mid(coin)
        if mid is not None:
            self.quoters[coin].on_spot(mid, time.time(), self.book.divergence_bps(coin))

    def on_trade(self, token, price, size, side):
        coin = self.token_coin.get(token)
        if coin:
            self.quoters[coin].on_trade(token, price, size, side, time.time())

    async def discover(self):
        """Find each window's market as it opens and keep the books subscribed."""
        while True:
            start = window_start(time.time())
            for coin in self.coins:
                if (coin, start) in self.looked_up:
                    continue
                m = await asyncio.to_thread(find_market, coin, start)
                if not m:
                    continue
                self.looked_up.add((coin, start))
                for t in (m["up_token"], m["down_token"]):
                    self.token_coin[t] = coin
                await self.markets.subscribe([m["up_token"], m["down_token"]])
                self.quoters[coin].set_market(start, m)
                log.info("tracking %s", m["slug"])
            self._drop_old(start)
            await asyncio.sleep(1)

    def _drop_old(self, start):
        for coin, ws in list(self.looked_up):
            if ws < start - WINDOW_SECONDS:
                self.looked_up.discard((coin, ws))
        live = {q.window.market[k] for q in self.quoters.values()
                if q.window and q.window.market for k in ("up_token", "down_token")}
        stale = [t for t in self.token_coin if t not in live]
        self.markets.forget(stale)
        for t in stale:
            del self.token_coin[t]

    async def report(self):
        while True:
            await asyncio.sleep(1)
            now = time.time()
            fills, settles = [], []
            for q in self.quoters.values():
                fills += [asdict(f) for f in q.new_fills if f.fv_markout is not None or now - f.ts > 60]
                q.new_fills = [f for f in q.new_fills if f.fv_markout is None and now - f.ts <= 60]
                settles += q.settlements
                q.settlements = []
            append_csv(os.path.join(DATA_DIR, "fills.csv"), fills)
            append_csv(os.path.join(DATA_DIR, "settlements.csv"), settles)
            for s in settles:
                log.info("settled %s %s: %s, %d fills, pnl %.2f",
                         s["coin"], s["window"], s["result"], s["fills"], s["pnl"])
            state = {
                "ts": now,
                "venues": {c: {v: {"bid": q.bid, "ask": q.ask, "age": round(now - q.ts, 2)}
                               for v, q in self.book.quotes[c].items()} for c in self.coins},
                "coins": {},
            }
            for c, q in self.quoters.items():
                snap = q.snapshot(now)
                m = q.window.market if q.window else None
                if m:
                    b = self.markets.book(m["up_token"])
                    snap.update(pm_bid=b.best_bid, pm_ask=b.best_ask, pm_mid=b.mid)
                state["coins"][c] = snap
            tmp = os.path.join(DATA_DIR, "quoter_state.json.tmp")
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, os.path.join(DATA_DIR, "quoter_state.json"))

    async def run(self):
        await asyncio.gather(
            coinbase_feed(self.book, self.coins),
            binance_feed(self.book, self.coins),
            self.markets.run(),
            self.discover(),
            self.report(),
        )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--coins", nargs="+", default=COINS, choices=COINS)
    ap.add_argument("--size", type=float, default=QuoteParams.size)
    ap.add_argument("--half-spread", type=float, default=QuoteParams.base_half_spread)
    ap.add_argument("--fill-at-touch", action="store_true",
                    help="count trades at our price as fills (optimistic)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    params = QuoteParams(size=args.size, base_half_spread=args.half_spread, fill_at_touch=args.fill_at_touch)
    try:
        asyncio.run(Runner(args.coins, params).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
