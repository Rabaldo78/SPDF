"""Polymarket market discovery (Gamma API) and live order books (CLOB websocket).

Read-only. Nothing in this module places or signs orders.

Each 15 minute window is its own market, with a slug like
btc-updown-15m-1760371200 (the window's Unix start time). Outcome 0 is "Up",
outcome 1 is "Down".
"""

import asyncio
import json
import logging
import time

import requests
import websockets

log = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def slug_for(coin, start_ts):
    return f"{coin.lower()}-updown-15m-{int(start_ts)}"


def _as_list(v):
    return json.loads(v) if isinstance(v, str) else (v or [])


def find_market(coin, start_ts, session=requests):
    """Return {'slug', 'condition_id', 'up_token', 'down_token'} or None."""
    slug = slug_for(coin, start_ts)
    try:
        r = session.get(f"{GAMMA_URL}/markets", params={"slug": slug}, timeout=5)
        r.raise_for_status()
        rows = r.json()
    except Exception as e:
        log.warning("gamma lookup failed for %s: %s", slug, e)
        return None
    if not rows:
        return None
    m = rows[0]
    tokens = _as_list(m.get("clobTokenIds"))
    outcomes = [o.lower() for o in _as_list(m.get("outcomes"))]
    if len(tokens) != 2:
        return None
    up_i = outcomes.index("up") if "up" in outcomes else 0
    return {
        "slug": slug,
        "condition_id": m.get("conditionId"),
        "up_token": tokens[up_i],
        "down_token": tokens[1 - up_i],
    }


class TokenBook:
    def __init__(self):
        self.bids = {}
        self.asks = {}
        self.ts = 0.0

    def set_snapshot(self, bids, asks):
        self.bids = {float(l["price"]): float(l["size"]) for l in bids}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks}
        self.ts = time.time()

    def set_level(self, side, price, size):
        levels = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.ts = time.time()

    @property
    def best_bid(self):
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self):
        return min(self.asks) if self.asks else None

    @property
    def mid(self):
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2


class MarketFeed:
    """Keeps order books for a set of token ids and reports trades.

    on_trade(token_id, price, size, taker_side) fires on each last_trade_price
    event. taker_side is "BUY" or "SELL" from the taker's point of view.
    """

    def __init__(self, on_trade=None):
        self.books = {}
        self.on_trade = on_trade
        self._ws = None
        self._wanted = set()

    def book(self, token):
        return self.books.setdefault(token, TokenBook())

    async def subscribe(self, tokens):
        self._wanted |= set(tokens)
        for t in tokens:
            self.book(t)
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"assets_ids": list(tokens), "operation": "subscribe"}))
            except Exception as e:
                log.warning("polymarket subscribe failed, will resubscribe on reconnect: %s", e)

    def forget(self, tokens):
        for t in tokens:
            self._wanted.discard(t)
            self.books.pop(t, None)

    def handle(self, msg):
        kind = msg.get("event_type")
        if kind == "book":
            self.book(msg["asset_id"]).set_snapshot(
                msg.get("bids", msg.get("buys", [])), msg.get("asks", msg.get("sells", [])))
        elif kind == "price_change":
            if "price_changes" in msg:
                for ch in msg["price_changes"]:
                    self.book(ch["asset_id"]).set_level(ch["side"], float(ch["price"]), float(ch["size"]))
            else:
                b = self.book(msg["asset_id"])
                for ch in msg.get("changes", []):
                    b.set_level(ch["side"], float(ch["price"]), float(ch["size"]))
        elif kind == "last_trade_price" and self.on_trade:
            self.on_trade(msg["asset_id"], float(msg["price"]), float(msg.get("size", 0)),
                          str(msg.get("side", "")).upper())

    async def run(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(MARKET_WS_URL, ping_interval=None, max_size=2**23) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"assets_ids": list(self._wanted), "type": "market"}))
                    log.info("polymarket connected, %d tokens", len(self._wanted))
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            if raw == "PONG":
                                continue
                            payload = json.loads(raw)
                            for msg in payload if isinstance(payload, list) else [payload]:
                                self.handle(msg)
                    finally:
                        pinger.cancel()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("polymarket feed dropped: %s; reconnecting in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                self._ws = None

    @staticmethod
    async def _ping(ws):
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")
