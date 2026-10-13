# SDTP_Rabon: SPX / VIX Premium Tracker (Full App)

import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta
import pytz
import os
import json
import time
from crypto15.fair_value import prob_up, annual_to_per_sec

# --- CONFIG ---
POLYGON_API_KEY = "v_RMe6XWZMNAcBDzm4PQYNleDd482Ead"
SPX_SYMBOL = "^GSPC"
VIX_SYMBOL = "^VIX"

# --- UTILITY ---
def round_to_nearest(x, base):
    return base * round(x / base)

def get_today_open(symbol):
    ticker = yf.Ticker(symbol)
    df = ticker.history(period="1d", interval="1m")
    return df.iloc[0]['Open'] if not df.empty else None

def get_expected_move(vix_open):
    return round((vix_open / 100) * np.sqrt(1 / 12) * get_today_open(SPX_SYMBOL), 2)

def get_10min_ohlc():
    ticker = yf.Ticker(SPX_SYMBOL)
    df = ticker.history(interval="15m", period="7d")
    df = df[['Open', 'High', 'Low', 'Close']]
    df.reset_index(inplace=True)
    # Fix: Check for correct column name after reset_index
    if 'Datetime' in df.columns:
        df.rename(columns={"Datetime": "Timestamp"}, inplace=True)
    elif 'index' in df.columns:
        df.rename(columns={"index": "Timestamp"}, inplace=True)
    return df

def get_option_premiums(strike):
    now = datetime.now(pytz.timezone('US/Eastern'))
    date_str = now.strftime('%Y-%m-%d')
    url = f"https://api.polygon.io/v3/snapshot/options/SPX/{date_str}?strike_price={strike}&apiKey={POLYGON_API_KEY}"
    res = requests.get(url)
    if res.status_code == 200:
        data = res.json()
        call = data.get("results", {}).get("call")
        return {
            "strike": strike,
            "call_bid": call.get("last_quote", {}).get("bid") if call else None,
            "call_ask": call.get("last_quote", {}).get("ask") if call else None
        }
    return {"strike": strike, "call_bid": None, "call_ask": None}

def get_historical_ohlc(date_str):
    start = pd.to_datetime(date_str)
    end = start + pd.Timedelta(days=1)
    ticker = yf.Ticker(SPX_SYMBOL)
    df = ticker.history(start=start, end=end, interval="15m")
    df = df[['Open', 'High', 'Low', 'Close']]
    df.reset_index(inplace=True)
    # Fix: Check for correct column name after reset_index
    if 'Datetime' in df.columns:
        df.rename(columns={"Datetime": "Timestamp"}, inplace=True)
    elif 'index' in df.columns:
        df.rename(columns={"index": "Timestamp"}, inplace=True)
    return df

# --- MAIN APP ---
st.set_page_config(layout="wide", page_title="SPX/VIX Premium Tracker")

st.title("📈 SPX / VIX Premium Tracker (SDTP_Rabon)")

tabs = st.tabs(["📅 Last 7 Days", "📆 Historical", "₿ 15m Crypto"])

# --- TODAY TAB ---
with tabs[0]:
    vix_open = get_today_open(VIX_SYMBOL)
    spx_open = get_today_open(SPX_SYMBOL)
    expected_move = get_expected_move(vix_open) if vix_open else None
    spx_ohlc_df = get_10min_ohlc()

    rounded_down = round_to_nearest(spx_open, 5) if spx_open else None
    rounded_up = rounded_down + 5 if rounded_down else None

    if vix_open:
        st.metric("VIX Open", f"{vix_open:.2f}")
    if spx_open:
        st.metric("SPX Open", f"{spx_open:.2f}")
    if expected_move:
        st.metric("Expected Move", f"±{expected_move:.2f}")

    st.subheader("📊 SPX OHLC (10-Min Intervals - Last 7 Days)")
    if not spx_ohlc_df.empty:
        st.dataframe(spx_ohlc_df, use_container_width=True)
    else:
        st.warning("No SPX OHLC data available.")

    if rounded_down and rounded_up:
        st.subheader("🪙 Option Premiums (Rounded Strikes near SPX Open)")
        col1, col2 = st.columns(2)
        with col1:
            st.markdown(f"**Strike {rounded_down}**")
            opt1 = get_option_premiums(rounded_down)
            st.json(opt1)
        with col2:
            st.markdown(f"**Strike {rounded_up}**")
            opt2 = get_option_premiums(rounded_up)
            st.json(opt2)
    else:
        st.warning("Could not determine nearest strikes.")

# --- HISTORICAL TAB ---
with tabs[1]:
    st.subheader("📆 Select Historical Date")
    date_input = st.date_input("Choose date", value=datetime.today() - timedelta(days=1), max_value=datetime.today())
    if date_input:
        date_str = date_input.strftime('%Y-%m-%d')
        historical_df = get_historical_ohlc(date_str)
        if not historical_df.empty:
            st.subheader(f"🕰️ SPX OHLC on {date_str}")
            st.dataframe(historical_df, use_container_width=True)
        else:
            st.warning(f"No data available for {date_str}.")

# --- 15 MINUTE CRYPTO TAB ---
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

with tabs[2]:
    st.subheader("Fair Value Calculator")
    c1, c2, c3, c4 = st.columns(4)
    fv_spot = c1.number_input("Spot", value=100000.0, step=10.0)
    fv_strike = c2.number_input("Price to beat", value=100000.0, step=10.0)
    fv_mins = c3.number_input("Minutes left", value=7.5, min_value=0.0, max_value=15.0, step=0.5)
    fv_vol = c4.number_input("Annual vol %", value=45.0, min_value=1.0, step=1.0)
    p_up = prob_up(fv_spot, fv_strike, annual_to_per_sec(fv_vol / 100), fv_mins * 60)
    m1, m2 = st.columns(2)
    m1.metric("Fair value Up", f"{p_up:.3f}")
    m2.metric("Fair value Down", f"{1 - p_up:.3f}")

    @st.fragment(run_every=1)
    def live_quoter_panel():
        st.subheader("Live Paper Quoter")
        path = os.path.join(DATA_DIR, "quoter_state.json")
        if not os.path.exists(path):
            st.info("Start the quoter in a terminal: python -m crypto15.run_quoter")
            return
        try:
            with open(path) as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        age = time.time() - state["ts"]
        if age > 5:
            st.warning(f"Quoter state is {age:.0f}s old. Is the quoter running?")
        rows = list(state["coins"].values())
        cols = ["coin", "spot", "strike", "secs_left", "sigma_annual", "fv",
                "bid", "ask", "pm_bid", "pm_ask", "inventory", "cash", "divergence_bps", "market"]
        st.dataframe(pd.DataFrame(rows).reindex(columns=cols), use_container_width=True, hide_index=True)
        venues = [{"coin": c, "venue": v, **q} for c, vs in state["venues"].items() for v, q in vs.items()]
        if venues:
            st.caption("Spot venues (age in seconds)")
            st.dataframe(pd.DataFrame(venues), use_container_width=True, hide_index=True)

    live_quoter_panel()

    settle_path = os.path.join(DATA_DIR, "settlements.csv")
    if os.path.exists(settle_path):
        st.subheader("Paper P&L by Window")
        sdf = pd.read_csv(settle_path)
        traded = sdf[sdf["fills"] > 0]
        s1, s2, s3 = st.columns(3)
        s1.metric("Total P&L", f"${sdf['pnl'].sum():.2f}")
        s2.metric("Windows traded", len(traded))
        s3.metric("Win rate", f"{(traded['pnl'] > 0).mean():.0%}" if len(traded) else "n/a")
        st.dataframe(sdf.groupby("coin")["pnl"].agg(["sum", "count", "mean"]), use_container_width=True)
    fills_path = os.path.join(DATA_DIR, "fills.csv")
    if os.path.exists(fills_path):
        fdf = pd.read_csv(fills_path)
        sign = fdf["side"].map({"BUY": 1, "SELL": -1})
        fdf["edge_at_fill"] = sign * (fdf["fv"] - fdf["price"])
        fdf["edge_10s"] = sign * (fdf["fv_markout"] - fdf["price"])
        st.caption("Edge per share: positive means the fill beat fair value. "
                   "If edge_10s is much lower than edge_at_fill, faster traders are picking off the quotes.")
        st.dataframe(fdf.tail(200), use_container_width=True, hide_index=True)
