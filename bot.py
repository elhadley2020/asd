import requests
import pandas as pd
import numpy as np
import time
import logging

# =======================
# CONFIGURATION
# =======================
API_KEY = "YOUR_OANDA_API_KEY"
ACCOUNT_ID = "YOUR_OANDA_ACCOUNT_ID"
BASE_URL = "https://api-fxpractice.oanda.com/v3"

PAIRS = ["EUR_USD", "GBP_USD", "USD_JPY", "AUD_USD"]
RISK_PERCENT = 0.01
MAX_OPEN_TRADES = 2
STOP_MULTIPLIER = 1.5
TAKE_PROFIT_RR = 2
CHECK_INTERVAL = 60  # seconds

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

# =======================
# LOGGING
# =======================
logging.basicConfig(
    filename="bot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

# =======================
# HELPER FUNCTIONS
# =======================

def safe_request(method, url, **kwargs):
    try:
        r = requests.request(method, url, headers=HEADERS, timeout=10, **kwargs)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"API error for {url}: {e}")
        return None


def fetch_candles(pair, count=300, granularity="H1"):
    url = f"{BASE_URL}/instruments/{pair}/candles"
    data = safe_request("GET", url, params={"count": count, "granularity": granularity, "price": "M"})
    if not data or "candles" not in data:
        logging.warning(f"No candle data for {pair}")
        return pd.DataFrame()
    
    df = pd.DataFrame(data["candles"])
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
    data = safe_request("GET", url)
    if not data:
        return []
    return data.get("trades", [])


def calculate_position_size(equity, stop_distance):
    risk_amount = equity * RISK_PERCENT
    if stop_distance <= 0:
        return 0
    units = risk_amount / stop_distance
    return max(1, int(units))


def format_price(pair, price):
    if "JPY" in pair:
        return f"{price:.3f}"
    else:
        return f"{price:.5f}"


def place_order(pair, units, stop_price, take_profit_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": stop_price},
            "takeProfitOnFill": {"price": take_profit_price}
        }
    }
    result = safe_request("POST", url, json=data)
    logging.info(f"Placed order for {pair}: {result}")
    return result


def modify_stop(trade_id, new_stop_price):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
    data = {"stopLoss": {"price": new_stop_price}}
    result = safe_request("PATCH", url, json=data)
    logging.info(f"Modified stop for {trade_id}: {new_stop_price}, result: {result}")
    return result

# =======================
# TRADE LOGIC
# =======================

def check_and_trade(pair, equity):
    df = fetch_candles(pair)
    if df.empty:
        return

    df = calculate_indicators(df)
    latest = df.iloc[-1]
    previous = df.iloc[-2] if len(df) > 1 else latest

    # Trend bias
    if latest["ema20"] > latest["ema50"] > latest["ema200"]:
        bias = "long"
    elif latest["ema20"] < latest["ema50"] < latest["ema200"]:
        bias = "short"
    else:
        return

    # Filters
    if latest["atr"] <= 0 or abs(latest["ema20"] - latest["ema50"]) <= 0.25 * latest["atr"]:
        return
    if latest["atr"] <= latest["atr20_avg"]:
        return
    if abs(latest["close"] - latest["ema20"]) > 0.5 * latest["atr"]:
        return
    if (bias == "long" and latest["close"] <= previous["high"]) or \
       (bias == "short" and latest["close"] >= previous["low"]):
        return

    stop_distance = STOP_MULTIPLIER * latest["atr"]
    units = calculate_position_size(equity, stop_distance)
    if units == 0:
        return
    if bias == "short":
        units = -units

    if bias == "long":
        stop_price = latest["close"] - stop_distance
        take_profit_price = latest["close"] + stop_distance * TAKE_PROFIT_RR
    else:
        stop_price = latest["close"] + stop_distance
        take_profit_price = latest["close"] - stop_distance * TAKE_PROFIT_RR

    stop_price = format_price(pair, stop_price)
    take_profit_price = format_price(pair, take_profit_price)

    # Sanity check TP
    if (bias == "long" and float(take_profit_price) <= latest["close"]) or \
       (bias == "short" and float(take_profit_price) >= latest["close"]):
        logging.warning(f"TP invalid for {pair}. Skipping order.")
        return

    place_order(pair, units, stop_price, take_profit_price)

# =======================
# TRADE MANAGEMENT
# =======================

def manage_open_trades(equity):
    trades = get_open_trades()
    if not trades:
        return

    for trade in trades:
        trade_id = trade.get("id")
        pair = trade.get("instrument")
        units = float(trade.get("currentUnits", 0))
        entry_price = float(trade.get("price", 0))
        unrealizedPL = float(trade.get("unrealizedPL", 0))
        if units == 0:
            continue

        df = fetch_candles(pair)
        df = calculate_indicators(df)
        atr = df.iloc[-1]["atr"] if not df.empty else 0

        risk_distance = STOP_MULTIPLIER * atr
        rr1 = risk_distance
        rr15 = risk_distance * 1.5

        current_stop = float(trade.get("stopLossOrder", {}).get("price", entry_price))

        if units > 0:  # LONG
            if unrealizedPL >= rr1:
                modify_stop(trade_id, max(entry_price, current_stop))
            if unrealizedPL >= rr15:
                new_stop = df.iloc[-1]["close"] - atr
                if new_stop > current_stop:
                    modify_stop(trade_id, new_stop)
        elif units < 0:  # SHORT
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
    equity = 1000  # Replace with live equity fetch later
    logging.info("Bot started")

    while True:
        try:
            manage_open_trades(equity)

            open_trades = get_open_trades()
            if len(open_trades) < MAX_OPEN_TRADES:
                for pair in PAIRS:
                    check_and_trade(pair, equity)

        except Exception as e:
            logging.error(f"Main loop error: {e}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    run_bot()
