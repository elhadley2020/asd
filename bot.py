import requests
import pandas as pd
import numpy as np
import time

# =======================
# CONFIGURATION
# =======================
API_KEY = ""
ACCOUNT_ID = ""
BASE_URL = "https://api-fxpractice.oanda.com/v3"

PAIRS = ["EUR_USD","GBP_USD","USD_JPY","AUD_USD","USD_CHF","NZD_USD","GBP_JPY","EUR_JPY","AUD_JPY","EUR_GBP"]
RISK_PERCENT = 0.01  # 1% per trade
MAX_OPEN_TRADES = 2
STOP_MULTIPLIER = 1.5
TAKE_PROFIT_RR = 2
CHECK_INTERVAL = 60  # seconds

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# =======================
# HELPER FUNCTIONS
# =======================


def format_price(pair, price):
    if "JPY" in pair:
        return f"{price:.3f}"
    else:
        return f"{price:.5f}"

def fetch_candles(pair, count=300, granularity="H1"):
    url = f"{BASE_URL}/instruments/{pair}/candles"
    params = {"count": count, "granularity": granularity, "price": "M"}
    r = requests.get(url, headers=HEADERS, params=params)
    data = r.json()["candles"]

    df = pd.DataFrame(data)
    df["close"] = df["mid"].apply(lambda x: float(x["c"]))
    df["high"] = df["mid"].apply(lambda x: float(x["h"]))
    df["low"] = df["mid"].apply(lambda x: float(x["l"]))
    df.index = pd.to_datetime(df["time"])

    return df


def calculate_indicators(df):
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["close"].shift()),
            abs(df["low"] - df["close"].shift())
        )
    )

    df["atr"] = df["tr"].rolling(14).mean()
    df["atr20_avg"] = df["atr"].rolling(20).mean()

    return df


def get_open_trades():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/openTrades"
    r = requests.get(url, headers=HEADERS)
    return r.json()["trades"]


def calculate_position_size(equity, stop_distance):
    risk_amount = equity * RISK_PERCENT
    units = risk_amount / stop_distance
    return max(1, int(units))


def place_order(pair, units, stop_price, take_profit_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"

    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": format_price(pair, stop_price)},
            "takeProfitOnFill": {"price": format_price(pair, take_profit_price)}
        }
    }

    r = requests.post(url, headers=HEADERS, json=data)
    print("Placed order:", r.json())
    return r.json()


def modify_stop(trade_id, new_stop_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    data = {"stopLoss": {"price": str(new_stop_price)}}
    r = requests.patch(url, headers=HEADERS, json=data)
    print(f"Modified stop {trade_id}:", r.json())


# =======================
# TRADE LOGIC
# =======================

def check_and_trade(pair, equity):
    df = fetch_candles(pair)
    df = calculate_indicators(df)

    latest = df.iloc[-1]
    previous = df.iloc[-2]

    # Trend bias
    if latest["ema20"] > latest["ema50"] > latest["ema200"]:
        bias = "long"
    elif latest["ema20"] < latest["ema50"] < latest["ema200"]:
        bias = "short"
    else:
        return

    # Trend strength
    if abs(latest["ema20"] - latest["ema50"]) <= 0.25 * latest["atr"]:
        return

    # Volatility filter
    if latest["atr"] <= latest["atr20_avg"]:
        return

    # Pullback filter
    if abs(latest["close"] - latest["ema20"]) > 0.5 * latest["atr"]:
        return

    # Breakout confirmation
    if bias == "long" and latest["close"] <= previous["high"]:
        return
    if bias == "short" and latest["close"] >= previous["low"]:
        return

    stop_distance = STOP_MULTIPLIER * latest["atr"]
    units = calculate_position_size(equity, stop_distance)

    if bias == "short":
        units = -units

    if bias == "long":
        stop_price = latest["close"] - stop_distance
        take_profit_price = latest["close"] + stop_distance * TAKE_PROFIT_RR
    else:
        stop_price = latest["close"] + stop_distance
        take_profit_price = latest["close"] - stop_distance * TAKE_PROFIT_RR

    place_order(pair, units, stop_price, take_profit_price)


# =======================
# TRADE MANAGEMENT
# =======================

def manage_open_trades(equity):
    trades = get_open_trades()

    for trade in trades:
        trade_id = trade["id"]
        pair = trade["instrument"]
        units = float(trade["currentUnits"])
        entry_price = float(trade["price"])
        unrealizedPL = float(trade["unrealizedPL"])

        df = fetch_candles(pair)
        df = calculate_indicators(df)
        atr = df.iloc[-1]["atr"]

        risk_distance = STOP_MULTIPLIER * atr
        rr1 = risk_distance
        rr15 = risk_distance * 1.5

        current_stop = float(trade["stopLossOrder"]["price"])

        # LONG
        if units > 0:
            if unrealizedPL >= rr1:
                modify_stop(trade_id, max(entry_price, current_stop))

            if unrealizedPL >= rr15:
                new_stop = df.iloc[-1]["close"] - atr
                if new_stop > current_stop:
                    modify_stop(trade_id, new_stop)

        # SHORT
        elif units < 0:
            if unrealizedPL >= rr1:
                modify_stop(trade_id, min(entry_price, current_stop))

            if unrealizedPL >= rr15:
                new_stop = df.iloc[-1]["close"] + atr
                if new_stop < current_stop:
                    modify_stop(trade_id, new_stop)


# =======================
# MAIN LOOP
# =======================

def run_bot():
    equity = 1000  # Replace with live equity pull later

    while True:
        manage_open_trades(equity)

        open_trades = get_open_trades()

        if len(open_trades) < MAX_OPEN_TRADES:
            for pair in PAIRS:
                check_and_trade(pair, equity)

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_bot()
