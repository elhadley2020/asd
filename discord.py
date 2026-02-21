import requests
import pandas as pd
import discord
import threading
import datetime
import time
import csv
import os

# ---------- CONFIG ----------
API_KEY = "YOUR_OANDA_API_KEY"
ACCOUNT_ID = "YOUR_ACCOUNT_ID"
BASE_URL = "https://api-fxpractice.oanda.com/v3"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

DISCORD_ALERT_WEBHOOK = "YOUR_DISCORD_WEBHOOK_URL"
DISCORD_CONTROL_TOKEN = "YOUR_BOT_TOKEN"
DISCORD_CHANNEL_ID = 123456789012345678  # channel to receive commands

CANDLE_COUNT = 100
ATR_PERIOD = 14
SLOPE_LOOKBACK = 5
HIGHER_GRANULARITY = "M5"

# ---------- DYNAMIC CONFIG ----------
config = {
    "PAIRS": ["EUR_USD", "GBP_USD", "USD_JPY"],
    "RISK_PERCENT": 0.01,
    "MAX_RISK": 0.02,
    "MIN_RISK": 0.005,
    "EMA_FAST": 20,
    "EMA_SLOW": 50,
    "ATR_MULT": 1.0,
    "ATR_THRESHOLD": 0.0005,
    "CANDLE_CLOSE_DIST": 0.1,
    "RSI_PERIOD": 14,
    "RSI_UPPER": 70,
    "RSI_LOWER": 30,
    "TRAILING_MULT": 0.5,  # move stop after price moves 0.5*ATR
}

# ---------- TRADE LOGGING ----------
LOG_FILE = "trade_log.csv"
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, mode="w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "pair", "signal", "units", "entry_price",
            "stop_loss", "take_profit", "atr", "equity", "dynamic_risk"
        ])

def log_trade(timestamp, pair, signal, units, entry_price, stop_loss, take_profit, atr, equity, dynamic_risk):
    with open(LOG_FILE, mode="a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, pair, signal, round(units, 2), round(entry_price, 5),
            round(stop_loss, 5), round(take_profit, 5), round(atr, 5),
            round(equity, 2), round(dynamic_risk, 4)
        ])

# ---------- DISCORD ALERT ----------
def send_discord_message(message: str):
    requests.post(DISCORD_ALERT_WEBHOOK, json={"content": message})

# ---------- OANDA FUNCTIONS ----------
def get_equity():
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return float(r.json()['account']['balance'])

def get_candles(pair, granularity="M1", count=CANDLE_COUNT):
    url = f"{BASE_URL}/instruments/{pair}/candles"
    params = {"granularity": granularity, "count": count, "price": "M"}
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    data = r.json()['candles']
    df = pd.DataFrame([{
        "time": c["time"],
        "open": float(c["mid"]["o"]),
        "high": float(c["mid"]["h"]),
        "low": float(c["mid"]["l"]),
        "close": float(c["mid"]["c"])
    } for c in data])
    return df

def calculate_indicators(df):
    df["H-L"] = df["high"] - df["low"]
    df["H-PC"] = abs(df["high"] - df["close"].shift(1))
    df["L-PC"] = abs(df["low"] - df["close"].shift(1))
    df["TR"] = df[["H-L","H-PC","L-PC"]].max(axis=1)
    df["ATR"] = df["TR"].rolling(ATR_PERIOD).mean()
    df["EMA_FAST"] = df["close"].ewm(span=config["EMA_FAST"], adjust=False).mean()
    df["EMA_SLOW"] = df["close"].ewm(span=config["EMA_SLOW"], adjust=False).mean()
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(config["RSI_PERIOD"]).mean()
    avg_loss = loss.rolling(config["RSI_PERIOD"]).mean()
    rs = avg_gain / avg_loss
    df["RSI"] = 100 - (100 / (1 + rs))
    return df

def ema_slope_trend(df):
    slope = df["EMA_SLOW"].iloc[-1] - df["EMA_SLOW"].iloc[-1 - SLOPE_LOOKBACK]
    if slope > 0:
        return "UP"
    elif slope < 0:
        return "DOWN"
    return "FLAT"

def check_dual_ema_signal(df, higher_trend):
    prev_fast = df["EMA_FAST"].iloc[-2]
    prev_slow = df["EMA_SLOW"].iloc[-2]
    curr_fast = df["EMA_FAST"].iloc[-1]
    curr_slow = df["EMA_SLOW"].iloc[-1]
    trend = ema_slope_trend(df)
    last_close = df["close"].iloc[-1]
    atr = df["ATR"].iloc[-1]
    rsi = df["RSI"].iloc[-1]

    if trend != higher_trend or trend == "FLAT":
        return None
    if atr < config["ATR_THRESHOLD"]:
        return None
    if trend == "UP" and last_close < curr_slow + atr*config["CANDLE_CLOSE_DIST"]:
        return None
    if trend == "DOWN" and last_close > curr_slow - atr*config["CANDLE_CLOSE_DIST"]:
        return None
    if trend == "UP" and rsi > config["RSI_UPPER"]:
        return None
    if trend == "DOWN" and rsi < config["RSI_LOWER"]:
        return None
    if prev_fast < prev_slow and curr_fast > curr_slow and trend == "UP":
        return "BUY"
    elif prev_fast > prev_slow and curr_fast < curr_slow and trend == "DOWN":
        return "SELL"
    return None

def place_order(pair, units, stop_loss, take_profit):
    url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/orders"
    data = {
        "order": {
            "units": str(units),
            "instrument": pair,
            "timeInForce": "FOK",
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": str(stop_loss)},
            "takeProfitOnFill": {"price": str(take_profit)}
        }
    }
    r = requests.post(url, headers=HEADERS, json=data)
    r.raise_for_status()
    return r.json()

def get_dynamic_risk(equity, starting_equity):
    growth = (equity - starting_equity)/starting_equity
    risk = config["RISK_PERCENT"]*(1+growth)
    return max(min(risk, config["MAX_RISK"]), config["MIN_RISK"])

# ---------- TRAILING STOP ----------
def update_trailing_stop(pair, trade_id, entry_price, units, atr, equity, dynamic_risk, take_profit):
    try:
        df = get_candles(pair, "M1", 1)
        price = df["close"].iloc[-1]
        if units > 0:  # BUY
            new_stop = max(entry_price, price - atr*config["TRAILING_MULT"])
        else:          # SELL
            new_stop = min(entry_price, price + atr*config["TRAILING_MULT"])
        url = f"{BASE_URL}/accounts/{ACCOUNT_ID}/trades/{trade_id}/orders"
        data = {"stopLoss": {"price": f"{new_stop:.5f}"}}
        r = requests.patch(url, headers=HEADERS, json=data)
        if r.status_code == 200:
            print(f"Trailing stop updated {pair} {new_stop:.5f}")
            # Log trailing stop update
            timestamp = datetime.datetime.utcnow().isoformat()
            log_trade(timestamp, pair, "TRAILING_UPDATE", units, price, new_stop, take_profit, atr, equity, dynamic_risk)
    except Exception as e:
        print(f"Trailing stop error {pair}: {e}")

# ---------- DISCORD CONTROL BOT ----------
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
@client.event
async def on_ready():
    print(f"Discord control connected as {client.user}")
@client.event
async def on_message(message):
    if message.channel.id != DISCORD_CHANNEL_ID or message.author.bot:
        return
    content = message.content.lower()
    try:
        if content.startswith("!risk"):
            config["RISK_PERCENT"] = float(content.split()[1])
        elif content.startswith("!ema_fast"):
            config["EMA_FAST"] = int(content.split()[1])
        elif content.startswith("!ema_slow"):
            config["EMA_SLOW"] = int(content.split()[1])
        elif content.startswith("!atr"):
            config["ATR_MULT"] = float(content.split()[1])
        elif content.startswith("!pairs"):
            config["PAIRS"] = content.split()[1:]
        elif content.startswith("!atr_thresh"):
            config["ATR_THRESHOLD"] = float(content.split()[1])
        elif content.startswith("!candle_dist"):
            config["CANDLE_CLOSE_DIST"] = float(content.split()[1])
        elif content.startswith("!rsi_upper"):
            config["RSI_UPPER"] = float(content.split()[1])
        elif content.startswith("!rsi_lower"):
            config["RSI_LOWER"] = float(content.split()[1])
        await message.channel.send("Config updated")
    except:
        await message.channel.send("Invalid command")

def run_discord_bot():
    client.run(DISCORD_CONTROL_TOKEN)

# ---------- MAIN TRADING LOOP ----------
def trading_loop():
    starting_equity = get_equity()
    while True:
        try:
            utc_hour = datetime.datetime.utcnow().hour
            in_session = (8 <= utc_hour < 17) or (13 <= utc_hour < 22)
            if not in_session:
                print(f"Market inactive (UTC {utc_hour})")
                time.sleep(60)
                continue

            for pair in config["PAIRS"]:
                df_1m = calculate_indicators(get_candles(pair,"M1"))
                df_5m = calculate_indicators(get_candles(pair,"M5"))
                higher_trend = ema_slope_trend(df_5m)

                signal = check_dual_ema_signal(df_1m, higher_trend)
                last_close = df_1m["close"].iloc[-1]
                atr = df_1m["ATR"].iloc[-1]*config["ATR_MULT"]
                equity = get_equity()
                dynamic_risk = get_dynamic_risk(equity, starting_equity)
                risk_amount = equity * dynamic_risk
                units = risk_amount / atr

                timestamp = datetime.datetime.utcnow().isoformat()

                if signal == "BUY":
                    stop_loss = last_close - atr
                    take_profit = last_close + atr*2
                    order = place_order(pair, units, stop_loss, take_profit)
                    trade_id = order["orderFillTransaction"]["tradeOpened"]["tradeID"]
                    # Log trade
                    log_trade(timestamp, pair, signal, units, last_close, stop_loss, take_profit, atr, equity, dynamic_risk)
                    update_trailing_stop(pair, trade_id, last_close, units, atr, equity, dynamic_risk, take_profit)
                    msg = f"{pair} BUY | Units: {units:.0f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}"
                    print(msg)
                    send_discord_message(msg)
                elif signal == "SELL":
                    stop_loss = last_close + atr
                    take_profit = last_close - atr*2
                    order = place_order(pair, -units, stop_loss, take_profit)
                    trade_id = order["orderFillTransaction"]["tradeOpened"]["tradeID"]
                    log_trade(timestamp, pair, signal, -units, last_close, stop_loss, take_profit, atr, equity, dynamic_risk)
                    update_trailing_stop(pair, trade_id, last_close, -units, atr, equity, dynamic_risk, take_profit)
                    msg = f"{pair} SELL | Units: {units:.0f} | SL: {stop_loss:.5f} | TP: {take_profit:.5f}"
                    print(msg)
                    send_discord_message(msg)
                else:
                    print(f"{pair} | No signal | Close: {last_close:.5f}")

        except Exception as e:
            print("Trading loop error:", e)
        time.sleep(5)

# ---------- START BOTH THREADS ----------
threading.Thread(target=run_discord_bot, daemon=True).start()
trading_loop()
