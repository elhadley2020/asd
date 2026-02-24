import requests
import pandas as pd
import time

# -----------------------------
# OANDA Setup
# -----------------------------
OANDA_TOKEN = "YOUR_OANDA_API_TOKEN"
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BASE_URL = "https://api-fxpractice.oanda.com/v3"
HEADERS = {"Authorization": f"Bearer {OANDA_TOKEN}"}

# -----------------------------
# Get Candles
# -----------------------------
def get_candles(instrument="EUR_USD", granularity="S15", count=500):
    url = f"{BASE_URL}/instruments/{instrument}/candles"
    params = {"granularity": granularity, "count": count, "price": "M"}
    r = requests.get(url, headers=HEADERS, params=params)
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
    last = df.iloc[-1]
    prev = df.iloc[-2]

    if is_sideways(last):
        return None

    trend = "bullish" if last['close'] > last['ema200'] else "bearish"

    # LONG ENTRY
    if trend == "bullish" and position is None:
        if last['macd_hist'] > 0 and last['macd_hist'] > prev['macd_hist']:
            return "enter_long"

    # SHORT ENTRY
    if trend == "bearish" and position is None:
        if last['macd_hist'] < 0 and last['macd_hist'] < prev['macd_hist']:
            return "enter_short"

    # LONG EXIT
    if position == "long" and last['macd_hist'] < prev['macd_hist']:
        return "exit"

    # SHORT EXIT
    if position == "short" and last['macd_hist'] > prev['macd_hist']:
        return "exit"

    return None

# -----------------------------
# Live Bot Loop (Single Position)
# -----------------------------
position = None
current_instrument = "EUR_USD"  # example starting instrument
instruments_list = ["EUR_USD", "GBP_USD", "USD_JPY", "XAU_USD"]  # add more

while True:

    # Iterate over instruments but respect single-position rule
    for inst in instruments_list:

        if position is not None:
            # Already in trade; skip all new instruments
            break

        df = get_candles(instrument=inst, granularity="S15", count=500)
        df = add_indicators(df)

        signal = strategy(df, position)

        if signal == "enter_long":
            print(f"{inst}: ENTER LONG")
            position = "long"
            current_instrument = inst
            # TODO: Place OANDA market order via requests POST
            break

        elif signal == "enter_short":
            print(f"{inst}: ENTER SHORT")
            position = "short"
            current_instrument = inst
            # TODO: Place OANDA market order via requests POST
            break

    # Check for exit on current position
    if position is not None:
        df = get_candles(instrument=current_instrument, granularity="S15", count=500)
        df = add_indicators(df)
        signal = strategy(df, position)

        if signal == "exit":
            print(f"{current_instrument}: EXIT POSITION")
            position = None
            current_instrument = None
            # TODO: Close OANDA position via requests PUT

    time.sleep(15)
