import requests
import pandas as pd
import time
import json

# -----------------------------
# OANDA Setup
# -----------------------------
OANDA_TOKEN = "YOUR_OANDA_API_TOKEN"
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BASE_URL = "https://api-fxpractice.oanda.com/v3"
HEADERS = {"Authorization": f"Bearer {OANDA_TOKEN}", "Content-Type": "application/json"}

# -----------------------------
# Get Candles
# -----------------------------
def get_candles(instrument="EUR_USD", granularity="S15", count=500):
    url = f"{BASE_URL}/instruments/{instrument}/candles"
    params = {"granularity": granularity, "count": count, "price": "M"}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()['candles']
        df = pd.DataFrame([{
            "time": c['time'],
            "open": float(c['mid']['o']),
            "high": float(c['mid']['h']),
            "low": float(c['mid']['l']),
            "close": float(c['mid']['c'])
        } for c in data])
        df['time'] = pd.to_datetime(df['time'])
        return df
    except Exception as e:
        print(f"Error fetching candles for {instrument}: {e}")
        return pd.DataFrame()

# -----------------------------
# Indicators
# -----------------------------
def add_indicators(df):
    df['ema200'] = df['close'].ewm(span=200, adjust=False).mean()
    df['ema_slope_pct'] = df['ema200'].pct_change()
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema12 - ema26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    return df

# -----------------------------
# Sideways Detection
# -----------------------------
def is_sideways(row, threshold=0.00001):
    return abs(row['ema_slope_pct']) < threshold

# -----------------------------
# Strategy Logic
# -----------------------------
def strategy(df, position):
    if df.empty or len(df) < 2:
        return None
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if is_sideways(last):
        return None

    trend = "bullish" if last['close'] > last['ema200'] else "bearish"

    if trend == "bullish" and position is None:
        if last['macd_hist'] > 0 and last['macd_hist'] > prev['macd_hist']:
            return "enter_long"
    if trend == "bearish" and position is None:
        if last['macd_hist'] < 0 and last['macd_hist'] < prev['macd_hist']:
            return "enter_short"
    if position == "long" and last['macd_hist'] < prev['macd_hist']:
        return "exit"
    if position == "short" and last['macd_hist'] > prev['macd_hist']:
        return "exit"
    return None

# -----------------------------
# OANDA Market Order Functions
# -----------------------------
def place_order(instrument, units):
    """Place a market order. Positive units = buy, negative = sell"""
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    data = {
        "order": {
            "units": str(units),
            "instrument": instrument,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT"
        }
    }
    try:
        r = requests.post(url, headers=HEADERS, data=json.dumps(data))
        r.raise_for_status()
        resp = r.json()
        print(f"Order placed: {resp}")
        return True
    except Exception as e:
        print(f"Error placing order for {instrument}: {e}")
        return False

def close_position(instrument):
    """Close all positions for the given instrument"""
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/positions/{instrument}/close"
    data = {"longUnits": "ALL", "shortUnits": "ALL"}
    try:
        r = requests.put(url, headers=HEADERS, data=json.dumps(data))
        r.raise_for_status()
        resp = r.json()
        print(f"Position closed: {resp}")
        return True
    except Exception as e:
        print(f"Error closing position for {instrument}: {e}")
        return False

# -----------------------------
# Live Bot Loop (Single Position)
# -----------------------------
position = None
current_instrument = None
units_size = 1000  # 0.01 standard lot

instruments_list = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]

while True:
    try:
        # Iterate over instruments only if no open position
        for inst in instruments_list:
            if position is not None:
                break

            df = get_candles(instrument=inst, granularity="S15", count=500)
            df = add_indicators(df)
            signal = strategy(df, position)

            if signal == "enter_long":
                print(f"{inst}: ENTER LONG")
                if place_order(inst, units_size):
                    position = "long"
                    current_instrument = inst
                break

            elif signal == "enter_short":
                print(f"{inst}: ENTER SHORT")
                if place_order(inst, -units_size):
                    position = "short"
                    current_instrument = inst
                break

        # Check exit for current position
        if position is not None and current_instrument is not None:
            df = get_candles(instrument=current_instrument, granularity="S15", count=500)
            df = add_indicators(df)
            signal = strategy(df, position)

            if signal == "exit":
                print(f"{current_instrument}: EXIT POSITION")
                if close_position(current_instrument):
                    position = None
                    current_instrument = None

    except Exception as e:
        print(f"Unexpected error in bot loop: {e}")

    time.sleep(15)
