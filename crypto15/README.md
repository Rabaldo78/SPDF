# 15 Minute Crypto Paper Quoter

Paper market making for the Polymarket 15 minute Up/Down markets on BTC, ETH, SOL and XRP. Nothing here places a real order.

## Run it

    pip install -r requirements.txt
    python -m crypto15.run_quoter          # terminal 1, leave it running
    streamlit run app.py                   # terminal 2, open the "15m Crypto" tab

From a US IP, Binance.com refuses the connection. Use Binance.US instead:

    BINANCE_HOST=binance.us python -m crypto15.run_quoter

## How it prices

Spot is the median of the Coinbase and Binance mids, streamed over websockets. The strike is the spot price seen in the first 3 seconds of the window. If the quoter starts mid-window, it sits that window out. Fair value is P(Up) = N(ln(S/K) / (sigma * sqrt(t))), with sigma from an EWMA of 1 second returns.

Quotes sit around fair value. The spread widens with how far spot can move during our latency, and it skews against inventory. The quoter pulls both sides in the last 30 seconds, when fair value is under 0.03 or over 0.97, and when the two venues disagree by more than 15 bps.

A paper quote fills only when a real Polymarket trade prints through it. Trades at exactly our price do not count unless `--fill-at-touch` is set.

## Known gaps

Polymarket settles on the Chainlink price, not on Coinbase or Binance, so our strike and final price can differ slightly from the official ones. Maker rebates are not counted. Paper fills ignore queue position and the fact that our own orders would change the book.
