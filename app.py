# SDTP_Rabon: SPX / VIX Premium Tracker (Full App)

import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from datetime import datetime, timedelta
import pytz

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

tabs = st.tabs(["📅 Last 7 Days", "📆 Historical"])

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
