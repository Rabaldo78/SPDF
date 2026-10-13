"""Coinbase and Binance spot feeds over websockets.

Both feeds push top of book as it changes, so the quoter reprices on every
tick instead of polling. A venue that goes quiet for STALE_SECONDS drops out
of the composite price.

Binance.com refuses US IP addresses. Set BINANCE_HOST=binance.us to use
Binance.US, which quotes USD pairs instead of USDT.
"""

import asyncio
import json
import logging
import os
import statistics
import time

import websockets

log = logging.getLogger(__name__)

STALE_SECONDS = 5.0

COINS = ["BTC", "ETH", "SOL", "XRP"]

COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"


def binance_config():
    host = os.environ.get("BINANCE_HOST", "binance.com").lower()
    if host == "binance.us":
        return "wss://stream.binance.us:9443/stream?streams=", "usd"
    return "wss://stream.binance.com:9443/stream?streams=", "usdt"


class VenueQuote:
    __slots__ = ("bid", "ask", "ts")

    def __init__(self, bid, ask, ts):
        self.bid, self.ask, self.ts = bid, ask, ts

    @property
    def mid(self):
        return (self.bid + self.ask) / 2


class SpotBook:
    """Latest top of book per coin per venue, plus a composite mid."""

    def __init__(self, on_tick=None):
        self.quotes = {c: {} for c in COINS}
        self.on_tick = on_tick

    def update(self, coin, venue, bid, ask, ts=None):
        if bid <= 0 or ask <= 0 or bid > ask:
            return
        self.quotes[coin][venue] = VenueQuote(bid, ask, ts or time.time())
        if self.on_tick:
            self.on_tick(coin)

    def fresh(self, coin, now=None):
        now = now or time.time()
        return {v: q for v, q in self.quotes[coin].items() if now - q.ts <= STALE_SECONDS}

    def mid(self, coin, now=None):
        fresh = self.fresh(coin, now)
        if not fresh:
            return None
        return statistics.median(q.mid for q in fresh.values())

    def divergence_bps(self, coin, now=None):
        """Gap between the highest and lowest venue mid, in basis points."""
        fresh = self.fresh(coin, now)
        if len(fresh) < 2:
            return 0.0
        mids = [q.mid for q in fresh.values()]
        return (max(mids) - min(mids)) / min(mids) * 1e4


async def _run_forever(name, connect_and_read):
    backoff = 1
    while True:
        try:
            await connect_and_read()
            backoff = 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network drops are routine; reconnect
            log.warning("%s feed dropped: %s; reconnecting in %ss", name, e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


async def coinbase_feed(book, coins=COINS):
    products = {f"{c}-USD": c for c in coins}

    async def read():
        async with websockets.connect(COINBASE_URL, ping_interval=20, max_size=2**22) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": list(products),
                "channels": ["ticker"],
            }))
            log.info("coinbase connected")
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") != "ticker":
                    continue
                coin = products.get(msg.get("product_id"))
                if coin and msg.get("best_bid") and msg.get("best_ask"):
                    book.update(coin, "coinbase", float(msg["best_bid"]), float(msg["best_ask"]))

    await _run_forever("coinbase", read)


async def binance_feed(book, coins=COINS):
    base, quote_ccy = binance_config()
    symbols = {f"{c.lower()}{quote_ccy}": c for c in coins}
    url = base + "/".join(f"{s}@bookTicker" for s in symbols)

    async def read():
        async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
            log.info("binance connected (%s)", url.split("/")[2])
            async for raw in ws:
                msg = json.loads(raw)
                data = msg.get("data", msg)
                coin = symbols.get(str(data.get("s", "")).lower())
                if coin:
                    book.update(coin, "binance", float(data["b"]), float(data["a"]))

    await _run_forever("binance", read)
