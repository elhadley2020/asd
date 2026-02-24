import requests
import pandas as pd
import numpy as np
from datetime import datetime

# =====================================
# CONFIG
# =====================================

OANDA_TOKEN = "YOUR_OANDA_API_TOKEN"
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BASE_URL = "https://api-fxpractice.oanda.com/v3"
HEADERS = {"Authorization": f"Bearer {OANDA_TOKEN}"}

GRANULARITY = "M1"
START_DATE = "2025-01-01T00:00:00Z"
END_DATE   = "2025-01-07T00:00:00Z"

STARTING_EQUITY = 10000
RISK_PER_TRADE = 0.02
SPREAD_COST = 0.00005

# =====================================
# GET ALL FOREX INSTRUMENTS
# =====================================

def get_all_forex_pairs():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/instruments"
    r = requests.get(url, headers=HEADERS)
    instruments = r.json()["instruments"]

    forex_pairs = []

    for inst in instruments:
        name = inst["name"]
        if "_" in name and len(name) == 7:
            base, quote = name.split("_")
            if base.isalpha() and quote.isalpha():
                forex_pairs.append(name)

    return forex_pairs


# =====================================
# DOWNLOAD DATA
# =====================================

def get_data(instrument):
    all_data = []
    current_from = START_DATE

    while True:
        url = f"{BASE_URL}/instruments/{instrument}/candles"
        params = {
            "granularity": GRANULARITY,
            "from": current_from,
            "to": END_DATE,
            "price": "M",
            "count": 5000
        }

        r = requests.get(url, headers=HEADERS, params=params)
        data = r.json().get("candles", [])

        if len(data) == 0:
            break

        for c in data:
            all_data.append({
                "time": c["time"],
                "close": float(c["mid"]["c"])
            })

        last_time = data[-1]["time"]

        if last_time == current_from:
            break

        current_from = last_time

        if last_time >= END_DATE:
            break

    df = pd.DataFrame(all_data)
    df["time"] = pd.to_datetime(df["time"])
    df = df.drop_duplicates().reset_index(drop=True)

    return df


# =====================================
# INDICATORS
# =====================================

def add_indicators(df):
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    df["ema_slope"] = df["ema200"].pct_change()

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()

    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    return df


# =====================================
# PORTFOLIO BACKTEST
# =====================================

def backtest_portfolio(data_dict):

    equity = STARTING_EQUITY
    peak = equity
    max_dd = 0

    position = None
    entry_price = 0
    current_pair = None

    trades = []

    # Align by shortest dataset
    min_len = min(len(df) for df in data_dict.values())

    for i in range(200, min_len):

        # If no position → scan all pairs
        if position is None:
            for pair, df in data_dict.items():

                row = df.iloc[i]
                prev = df.iloc[i-1]

                if abs(row["ema_slope"]) < 0.00001:
                    continue

                trend = "bullish" if row["close"] > row["ema200"] else "bearish"

                if trend == "bullish":
                    if row["macd_hist"] > 0 and row["macd_hist"] > prev["macd_hist"]:
                        position = "long"
                        entry_price = row["close"]
                        current_pair = pair
                        break

                if trend == "bearish":
                    if row["macd_hist"] < 0 and row["macd_hist"] < prev["macd_hist"]:
                        position = "short"
                        entry_price = row["close"]
                        current_pair = pair
                        break

        # If position open → check exit only on that pair
        else:
            df = data_dict[current_pair]
            row = df.iloc[i]
            prev = df.iloc[i-1]

            risk_amount = equity * RISK_PER_TRADE

            if position == "long" and row["macd_hist"] < prev["macd_hist"]:
                pnl_pct = (row["close"] - entry_price) / entry_price
                pnl_pct -= SPREAD_COST
                equity += risk_amount * pnl_pct * 10
                trades.append(pnl_pct)
                position = None

            elif position == "short" and row["macd_hist"] > prev["macd_hist"]:
                pnl_pct = (entry_price - row["close"]) / entry_price
                pnl_pct -= SPREAD_COST
                equity += risk_amount * pnl_pct * 10
                trades.append(pnl_pct)
                position = None

        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity)/peak)

    return equity, trades, max_dd


# =====================================
# RUN EVERYTHING
# =====================================

print("Fetching all forex pairs...")
pairs = get_all_forex_pairs()
print("Total forex pairs:", len(pairs))

data_dict = {}

for pair in pairs:
    print("Downloading:", pair)
    df = get_data(pair)
    df = add_indicators(df)
    if len(df) > 300:
        data_dict[pair] = df

print("Running portfolio backtest...")
final_equity, trades, max_dd = backtest_portfolio(data_dict)

print("\n===== PORTFOLIO RESULTS =====")
print("Pairs Tested:", len(data_dict))
print("Trades:", len(trades))
print("Final Equity:", round(final_equity, 2))

if len(trades) > 0:
    print("Win Rate:", round((np.array(trades) > 0).mean()*100, 2), "%")
    print("Average Trade %:", round(np.mean(trades)*100, 4))

print("Max Drawdown %:", round(max_dd*100, 2))
