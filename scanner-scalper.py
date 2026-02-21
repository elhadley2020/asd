from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
import threading
import time
import oandapyV20
from oandapyV20 import API
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.trades as trades
from oandapyV20.endpoints.accounts import AccountDetails, AccountInstruments
import pandas as pd

# --------------------------
# API Setup
# --------------------------
API_KEY = "YOUR_OANDA_API_KEY"
ACCOUNT_ID = "YOUR_OANDA_ACCOUNT_ID"
api = API(access_token=API_KEY)

# --------------------------
# Parameters
# --------------------------
EMA_FAST_PERIOD = 3
EMA_SLOW_PERIOD = 8
RSI_PERIOD = 5
ATR_PERIOD = 10
ATR_STOP_MULTIPLIER = 1.5
TRAILING_STOP_PIPS = 2
RISK_PER_TRADE = 0.01
MAX_OPEN_TRADES = 5
TOP_VOLATILE_PAIRS = 3
TP_PIPS = 5
MICRO_VOLUME_THRESHOLD = 10000

# --------------------------
# Fetch tradable pairs
# --------------------------
r = AccountInstruments(accountID=ACCOUNT_ID)
instruments_data = api.request(r)
PAIRS = [inst['name'] for inst in instruments_data['instruments']]

# --------------------------
# Tracking data
# --------------------------
price_data = {pair: [] for pair in PAIRS}
ema_data = {pair: {"fast": None, "slow": None} for pair in PAIRS}
trades_open = {}
console = Console()
closed_trades = []

# --------------------------
# Helper Functions
# --------------------------
def update_ema(prev_ema, price, period):
    alpha = 2 / (period + 1)
    return prev_ema + alpha * (price - prev_ema)

def calculate_rsi(prices):
    df = pd.Series(prices)
    delta = df.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_atr(prices):
    df = pd.DataFrame(prices, columns=['close'])
    df['high'] = df['close']
    df['low'] = df['close']
    df['tr'] = df['high'] - df['low']
    return df['tr'].rolling(ATR_PERIOD).mean().iloc[-1]

def get_account_balance():
    r = AccountDetails(accountID=ACCOUNT_ID)
    account = api.request(r)
    return float(account['account']['balance'])

def get_trade_units(balance, sl_distance):
    risk_amount = balance * RISK_PER_TRADE
    units = int(risk_amount / sl_distance / 0.0001)
    return max(1000, units)

# --------------------------
# Place Trade
# --------------------------
def place_trade(pair, direction, last_price, atr):
    balance = get_account_balance()
    sl_distance = atr * ATR_STOP_MULTIPLIER
    units = get_trade_units(balance, sl_distance)
    sl_price = last_price - sl_distance if direction=="buy" else last_price + sl_distance
    tp_price = last_price + TP_PIPS*0.0001 if direction=="buy" else last_price - TP_PIPS*0.0001
    data = {
        "order": {
            "instrument": pair,
            "units": str(units if direction=="buy" else -units),
            "type": "MARKET",
            "positionFill": "DEFAULT",
            "takeProfitOnFill": {"price": str(round(tp_price,5))},
            "stopLossOnFill": {"price": str(round(sl_price,5))}
        }
    }
    try:
        r_order = orders.OrderCreate(accountID=ACCOUNT_ID, data=data)
        resp = api.request(r_order)
        trade_id = resp['orderFillTransaction']['tradeOpened']['tradeID']
        trades_open[pair] = {"tradeID": trade_id, "direction": direction, "sl_price": sl_price, "tp_price": tp_price, "units": units, "last_price": last_price}
        console.log(f"[TRADE] {direction.upper()} {pair} Price: {last_price} | SL: {sl_price} | TP: {tp_price}")
    except Exception as e:
        console.log(f"[ERROR] Trade failed {pair}: {e}")

# --------------------------
# Trailing Stop Update
# --------------------------
def update_trailing_stop(pair, last_price):
    if pair not in trades_open:
        return
    trade = trades_open[pair]
    trade_id = trade['tradeID']
    direction = trade['direction']
    sl_price = trade['sl_price']
    new_sl = None
    if direction=="buy":
        candidate_sl = last_price - TRAILING_STOP_PIPS*0.0001
        if candidate_sl > sl_price: new_sl = candidate_sl
    else:
        candidate_sl = last_price + TRAILING_STOP_PIPS*0.0001
        if candidate_sl < sl_price: new_sl = candidate_sl
    if new_sl:
        trade['sl_price'] = new_sl
        data = {"stopLoss": {"price": str(round(new_sl,5))}}
        try:
            r_mod = trades.TradeCRCDO(accountID=ACCOUNT_ID, tradeID=trade_id, data=data)
            api.request(r_mod)
        except: pass

# --------------------------
# Adaptive Dashboard
# --------------------------
def dashboard():
    with Live(console=console, refresh_per_second=1) as live:
        while True:
            table = Table(title="Adaptive High-Volatility Forex Scalper")
            table.add_column("Pair")
            table.add_column("Volatility")
            table.add_column("Signal")
            table.add_column("Direction")
            table.add_column("SL")
            table.add_column("TP")
            table.add_column("Units")
            
            # Compute volatility
            volatilities = {pair: calculate_atr(price_data[pair]) if len(price_data[pair])>=ATR_PERIOD else 0 for pair in PAIRS}
            top_pairs = [pair for pair,_ in sorted(volatilities.items(), key=lambda x: x[1], reverse=True)[:TOP_VOLATILE_PAIRS]]
            
            # Close trades for pairs no longer top volatile
            for pair in list(trades_open.keys()):
                if pair not in top_pairs:
                    last_price = price_data[pair][-1] if price_data[pair] else trades_open[pair]["last_price"]
                    trades_open.pop(pair)
                    console.log(f"[ADAPTIVE] Dropped {pair}, exiting trade at price {last_price}")
            
            for pair in top_pairs:
                trade = trades_open.get(pair,{})
                last_price = price_data[pair][-1] if price_data[pair] else 0
                ema_f = ema_data[pair]['fast'] if ema_data[pair]['fast'] else 0
                ema_s = ema_data[pair]['slow'] if ema_data[pair]['slow'] else 0
                rsi = calculate_rsi(price_data[pair]) if len(price_data[pair])>=RSI_PERIOD else 0
                signal = "BUY" if ema_f>ema_s and rsi<70 else "SELL" if ema_f<ema_s and rsi>30 else "HOLD"
                table.add_row(
                    pair,
                    str(round(volatilities[pair],5)),
                    signal,
                    trade.get("direction",""),
                    str(round(trade.get("sl_price",0),5)),
                    str(round(trade.get("tp_price",0),5)),
                    str(trade.get("units",""))
                )
            live.update(table)
            time.sleep(0.5)

# --------------------------
# Tick Stream
# --------------------------
def tick_stream():
    params = {"instruments": ",".join(PAIRS)}
    r = pricing.PricingStream(accountID=ACCOUNT_ID, params=params)
    for msg in api.request(r):
        if "bids" in msg and "asks" in msg:
            pair = msg['instrument']
            last_price = (float(msg['bids'][0]['price']) + float(msg['asks'][0]['price']))/2
            volume = float(msg.get('volume',0))
            if volume < MICRO_VOLUME_THRESHOLD: continue
            price_data[pair].append(last_price)
            if len(price_data[pair])>EMA_SLOW_PERIOD: price_data[pair].pop(0)
            if ema_data[pair]['fast'] is None:
                ema_data[pair]['fast']=last_price
                ema_data[pair]['slow']=last_price
                continue
            ema_data[pair]['fast']=update_ema(ema_data[pair]['fast'],last_price,EMA_FAST_PERIOD)
            ema_data[pair]['slow']=update_ema(ema_data[pair]['slow'],last_price,EMA_SLOW_PERIOD)
            
            signal="hold"
            if len(price_data[pair])>=RSI_PERIOD:
                rsi = calculate_rsi(price_data[pair])
                if ema_data[pair]['fast']>ema_data[pair]['slow'] and rsi<70: signal="buy"
                elif ema_data[pair]['fast']<ema_data[pair]['slow'] and rsi>30: signal="sell"
            
            atr = calculate_atr(price_data[pair]) if len(price_data[pair])>=ATR_PERIOD else 0.0002
            
            # Place trade if pair is in top volatility and not already traded
            volatilities = {p: calculate_atr(price_data[p]) if len(price_data[p])>=ATR_PERIOD else 0 for p in PAIRS}
            top_pairs = [p for p,_ in sorted(volatilities.items(), key=lambda x:x[1], reverse=True)[:TOP_VOLATILE_PAIRS]]
            
            if signal!="hold" and len(trades_open)<MAX_OPEN_TRADES and pair in top_pairs and pair not in trades_open:
                place_trade(pair, signal, last_price, atr)
            
            for open_pair in list(trades_open.keys()):
                update_trailing_stop(open_pair, last_price)

# --------------------------
# Run Threads
# --------------------------
threading.Thread(target=dashboard,daemon=True).start()
threading.Thread(target=tick_stream,daemon=True).start()

while True:
    time.sleep(1)
