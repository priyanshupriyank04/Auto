import streamlit as st
import pandas as pd
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from streamlit_autorefresh import st_autorefresh

# Refresh every 5 seconds
st_autorefresh(interval=5000, key="data_refresh")

# --- Constants & Config ---
IST = ZoneInfo("Asia/Kolkata")
LOGS_DIR = Path("logs")
TRADES_DIR = LOGS_DIR / "trades"
HEARTBEAT_FILE = LOGS_DIR / "heartbeat.txt"

st.set_page_config(
    page_title="Hyperliquid Breakout Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Style Customization ---
st.markdown("""
    <style>
    .main {
        background-color: #0e1117;
    }
    .stMetric {
        background-color: #1a1c24;
        padding: 15px;
        border-radius: 10px;
        border: 1px solid #2e313d;
    }
    .status-live {
        color: #00ff00;
        font-weight: bold;
    }
    .status-offline {
        color: #ff4b4b;
        font-weight: bold;
    }
    </style>
    """, unsafe_allow_html=True)

# --- Utility Functions ---
def get_current_date_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")

def check_bot_status() -> bool:
    """Checks if the bot is live by looking at heartbeat file timestamp."""
    if not HEARTBEAT_FILE.exists():
        return False
    
    last_mod = HEARTBEAT_FILE.stat().st_mtime
    # If the file hasn't been updated in 30 seconds, consider it offline
    return (time.time() - last_mod) < 30

def read_heartbeat() -> str:
    if HEARTBEAT_FILE.exists():
        try:
            with open(HEARTBEAT_FILE, "r") as f:
                return f.read().strip()
        except:
            return "Error reading heartbeat."
    return "Heartbeat file not found."

def load_today_pnl(date_str: str) -> pd.DataFrame:
    pnl_file = LOGS_DIR / f"pnl_{date_str}.csv"
    if pnl_file.exists():
        try:
            df = pd.read_csv(pnl_file)
            return df
        except:
            return pd.DataFrame()
    return pd.DataFrame()

def load_today_trades(date_str: str) -> list:
    trades_file = TRADES_DIR / f"{date_str}_trades.json"
    if trades_file.exists():
        try:
            with open(trades_file, "r") as f:
                return json.load(f)
        except:
            return []
    return []

def format_trades_for_table(trades_list: list) -> pd.DataFrame:
    if not trades_list:
        return pd.DataFrame()
    
    processed_data = []
    for t in trades_list:
        row = {
            "Time (IST)": str(t.get("timestamp_ist", "")).split("T")[-1].split(".")[0],
            "Event": str(t.get("event", "")).replace("_", " ").title(),
        }
        details = t.get("details", {})
        info_parts = []
        
        if "range_high" in details:
            info_parts.append(f"Range: [{details['range_high']:.0f} - {details['range_low']:.0f}]")
        if "range_size" in details:
            info_parts.append(f"Size: {details['range_size']:.1f}")
        
        if "side" in details:
            row["Side"] = str(details["side"]).upper()
        if "size" in details:
            row["Qty"] = details["size"]
        
        p_text = ""
        if "price" in details: p_text = f"${details['price']:,.2f}"
        elif "entry_price" in details: p_text = f"${details['entry_price']:,.2f}"
        
        sl_tp = []
        if "sl" in details: sl_tp.append(f"SL:{details['sl']}")
        if "tp" in details: sl_tp.append(f"TP:{details['tp']}")
        if "exit_type" in details: sl_tp.append(f"Reason:{details['exit_type'].upper()}")
        
        if sl_tp:
            info_parts.append(" | ".join(sl_tp))
        
        resp = details.get("response", {})
        if isinstance(resp, dict):
            row["Status"] = resp.get("status", "N/A")
            inner_resp = resp.get("response", {})
            if isinstance(inner_resp, dict):
                statuses = inner_resp.get("data", {}).get("statuses", [{}])
                if statuses:
                    order_info = statuses[0]
                    if "filled" in order_info:
                        row["Fill Price"] = f"${float(order_info['filled'].get('avgPx', 0)):,.2f}"
                        row["OID"] = order_info["filled"].get("oid")
                    elif "error" in order_info:
                        row["Status"] = f"ERR: {order_info['error']}"

        row["Event Details"] = p_text if not info_parts else f"{p_text} {' '.join(info_parts)}" if p_text else " ".join(info_parts)
        processed_data.append(row)
    
    df = pd.DataFrame(processed_data)
    cols = ["Time (IST)", "Event", "Event Details", "Side", "Qty", "Fill Price", "Status", "OID"]
    return df[[c for c in cols if c in df.columns]]

# --- Sidebar ---
st.sidebar.title("⚡ HL Breakout Bot")
is_live = check_bot_status()
if is_live:
    st.sidebar.markdown('Status: <span class="status-live">● LIVE</span>', unsafe_allow_html=True)
else:
    st.sidebar.markdown('Status: <span class="status-offline">● OFFLINE</span>', unsafe_allow_html=True)

nav = st.sidebar.radio("Navigation", ["Live Dashboard", "History"])

st.sidebar.divider()
current_time = datetime.now(IST).strftime("%H:%M:%S")
st.sidebar.write(f"**IST Time:** {current_time}")
st.sidebar.write(f"**Date:** {get_current_date_str()}")

# Refresh logic
if st.sidebar.button("Manual Refresh"):
    st.rerun()

# --- Main Page: Live Dashboard ---
if nav == "Live Dashboard":
    st.title("🚀 Live Strategy Monitor")
    
    # 1. Summary Metrics
    m1, m2, m3, m4 = st.columns(4)
    
    today_pnl_df = load_today_pnl(get_current_date_str())
    total_pnl = 0.0
    win_rate = 0.0
    trades_count = 0
    
    if not today_pnl_df.empty:
        total_pnl = today_pnl_df['pnl_usd'].sum()
        trades_count = len(today_pnl_df)
        wins = len(today_pnl_df[today_pnl_df['pnl_usd'] > 0])
        win_rate = (wins / trades_count * 100) if trades_count > 0 else 0
    
    heartbeat_text = read_heartbeat()
    # Try to extract equity from heartbeat text
    equity = 0.0
    if "| Eq: $" in heartbeat_text:
        try:
            equity = float(heartbeat_text.split("| Eq: $")[1].split()[0])
        except:
            pass

    m1.metric("Account Equity", f"${equity:,.2f}")
    m2.metric("Today's PnL", f"${total_pnl:,.2f}", delta=f"{total_pnl:.2f}")
    m3.metric("Win Rate", f"{win_rate:.1f}%")
    m4.metric("Trades Today", trades_count)

    st.divider()

    # 2. Status & Metrics (Parsed from heartbeat)
    st.subheader("📡 Bot Status & Live Execution")
    
    # Extract values from heartbeat string for UI
    h_data = {
        "time": "N/A", "price": 0.0, "range": "N/A", "status": "Offline",
        "armed": "N/A", "side": "No", "sl_count": "0/3", "tp": "None",
        "halted": "No", "equity": 0.0, "current_sl": None
    }
    
    if heartbeat_text and "|" in heartbeat_text:
        try:
            parts = heartbeat_text.split(" | ")
            h_data["time"] = str(heartbeat_text.split("]")[0])[1:]
            h_data["price"] = float(heartbeat_text.split("BTC: $")[1].split()[0])
            h_data["range"] = heartbeat_text.split("Range: ")[1].split(" | ")[0]
            h_data["status"] = heartbeat_text.split("Status: ")[1].split(" | ")[0]
            h_data["armed"] = heartbeat_text.split("Armed: ")[1].split(" | ")[0]
            h_data["side"] = heartbeat_text.split("Side: ")[1].split(" | ")[0]
            h_data["sl_count"] = heartbeat_text.split("SLs: ")[1].split(" | ")[0]
            h_data["tp"] = heartbeat_text.split("TP: ")[1].split(" | ")[0]
            h_data["halted"] = heartbeat_text.split("Halted: ")[1].split(" | ")[0]
            h_data["equity"] = float(heartbeat_text.split("Eq: $")[1].split()[0])
            
            # SL is optional only if in trade
            if "SL: $" in heartbeat_text:
                 h_data["current_sl"] = float(heartbeat_text.split("SL: $")[1].split()[0])
        except Exception:
            pass

    # Visual Layout
    col_status, col_range, col_armed, col_trade = st.columns(4)
    
    with col_status:
        st.write("**Bot Status**")
        status_text = str(h_data['status'])
        status_color = "green" if status_text != "Offline" else "gray"
        st.markdown(f"#### :{status_color}[{status_text.replace('_', ' ')}]")
        st.write(f"Last Update: {h_data['time']}")

    with col_range:
        st.write("**Morning Range**")
        st.markdown(f"#### {h_data['range']}")
        st.progress(1.0 if h_data['status'] != 'Offline' else 0.0)

    with col_armed:
        st.write("**Breakout Armed**")
        armed_text = str(h_data['armed'])
        armed_color = "orange" if armed_text == "ARMED" else "gray"
        st.markdown(f"#### :{armed_color}[{armed_text}]")
        st.write(f"SL Trailing: {h_data['sl_count']}")

    with col_trade:
        st.write("**Current Market Side**")
        side_val = str(h_data["side"]).upper()
        if side_val == "LONG":
            st.markdown(f"#### :green[▲ {side_val}]")
        elif side_val == "SHORT":
            st.markdown(f"#### :red[▼ {side_val}]")
        else:
            st.markdown(f"#### {side_val}")
        
        if h_data["current_sl"]:
            st.markdown(f"### :red[SL: ${h_data['current_sl']:,.0f}]")

    st.divider()

    # 3. Live Price & Current Trade Info
    c1, c2 = st.columns([2, 1])
    with c1:
        st.write("**Market Price (BTC)**")
        st.title(f"${h_data['price']:,.2f}")
    
    with c2:
        # Halted state warning
        if h_data["halted"] == "Yes":
            if h_data["tp"] == "Hit":
                st.success("🎯 Trading Halted for Day (Target Hit!)")
            else:
                st.error("⚠️ Trading Halted for Day (Max SLs Hit)")
    
    # 4. Tables Section
    tab1, tab2, tab3 = st.tabs(["📊 Closed Trades (PnL)", "📝 Activity Log (JSON)", "💻 Raw Heartbeat"])
    
    with tab1:
        if not today_pnl_df.empty:
            st.dataframe(today_pnl_df.sort_index(ascending=False), use_container_width=True, hide_index=True)
        else:
            st.info("No closed trades logged for today yet.")

    with tab2:
        today_trades = load_today_trades(get_current_date_str())
        if today_trades:
            df_trades = format_trades_for_table(today_trades)
            st.dataframe(df_trades.sort_index(ascending=False), use_container_width=True, hide_index=True)
            with st.expander("Show Full Raw JSON Log"):
                st.json(today_trades)
        else:
            st.info("No activity events found for today.")

    with tab3:
        st.code(heartbeat_text, language="text")

# --- History Page ---
elif nav == "History":
    st.title("📜 Trade History")
    
    all_dates = set()
    if LOGS_DIR.exists():
        for f in LOGS_DIR.glob("pnl_*.csv"):
            all_dates.add(f.name.replace("pnl_", "").replace(".csv", ""))
    if TRADES_DIR.exists():
        for f in TRADES_DIR.glob("*_trades.json"):
            all_dates.add(f.name.replace("_trades.json", ""))
    
    sorted_dates = sorted(list(all_dates), reverse=True)
    
    if not sorted_dates:
        st.warning("No history found in logs directory.")
    else:
        selected_date = st.selectbox("Select Date to View", sorted_dates)
        hist_pnl_df = load_today_pnl(selected_date)
        hist_trades = load_today_trades(selected_date)
        
        c1, c2 = st.columns(2)
        with c1:
            total_h = hist_pnl_df['pnl_usd'].sum() if not hist_pnl_df.empty else 0
            st.metric(f"Total PnL ({selected_date})", f"${total_h:,.2f}")
        with c2:
            count_h = len(hist_pnl_df) if not hist_pnl_df.empty else 0
            st.metric("Total Trades", count_h)

        st.divider()
        
        tab_h1, tab_h2 = st.tabs(["📈 PnL Data", "📜 Activity Log"])
        
        with tab_h1:
            if not hist_pnl_df.empty:
                st.dataframe(hist_pnl_df, use_container_width=True, hide_index=True)
            else:
                st.info("No PnL CSV found for this date.")

        with tab_h2:
            if hist_trades:
                df_hist = format_trades_for_table(hist_trades)
                st.dataframe(df_hist.sort_index(ascending=True), use_container_width=True, hide_index=True)
                with st.expander("View Raw JSON Archive"):
                    st.json(hist_trades)
            else:
                st.info("No activity JSON found for this date.")

# Auto-refresh handled by st_autorefresh at top
