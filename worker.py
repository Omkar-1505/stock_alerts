import json
import os
import random
import time
from datetime import datetime, timezone, timedelta, time as dtime
from concurrent.futures import ThreadPoolExecutor
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pywebpush import WebPushException, webpush
import requests
import yfinance as yf
from backend.database import Device, SessionLocal, Watchlist

load_dotenv()

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIMS = {"sub": os.getenv("VAPID_ADMIN_EMAIL", "mailto:admin@example.com")}

# 3-Hour Cooldown between identical stock notifications
COOLDOWN_SECONDS = 10800

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_CLOSE_TIME = dtime(15, 30)

def get_daily_alert_expiry(created_ist: datetime) -> datetime:
    """
    Determines the exact 15:30 IST session close when a DAILY alert expires.
    Rolls over to the next trading day if created after 15:30 IST or on weekends.
    """
    if created_ist.weekday() < 5 and created_ist.time() < MARKET_CLOSE_TIME:
        target_date = created_ist.date()
    else:
        target_date = created_ist.date() + timedelta(days=1)
        while target_date.weekday() >= 5:
            target_date += timedelta(days=1)
            
    return datetime.combine(target_date, MARKET_CLOSE_TIME).replace(tzinfo=IST)

def purge_expired_daily_alerts(db):
    """Drops DAILY alerts whose trading session has concluded at or past 15:30 IST."""
    now_ist = datetime.now(IST)
    daily_items = db.query(Watchlist).filter(Watchlist.list_type == "DAILY").all()
    purged_ids = []

    for item in daily_items:
        created_time = item.created_at
        if created_time is None:
            continue
        if created_time.tzinfo is None:
            created_time = created_time.replace(tzinfo=timezone.utc)
        
        created_ist = created_time.astimezone(IST)
        expiry_ist = get_daily_alert_expiry(created_ist)
        
        if now_ist >= expiry_ist:
            purged_ids.append(item.id)

    if purged_ids:
        db.query(Watchlist).filter(Watchlist.id.in_(purged_ids)).delete(synchronize_session=False)
        db.commit()
        print(f"🧹 [Auto-Purge @ 15:30] Flushed {len(purged_ids)} expired DAILY watchlist item(s).")

def get_live_price(ticker: str, exchange: str = "NSE") -> float:
    """Waterfall Scraper: Google Finance -> CNBC -> yfinance."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    ]
    headers = {"User-Agent": random.choice(user_agents)}

    live_session = is_market_hours()

    # 1. AFTER HOURS: Prioritize settled VWAP daily close
    if not live_session:
        try:
            yf_ticker = f"{ticker.upper()}.NS" if exchange.upper() == "NSE" else f"{ticker.upper()}.BO"
            df = yf.Ticker(yf_ticker).history(period="1d", interval="1d")
            if not df.empty:
                return round(float(df['Close'].iloc[-1]), 2)
        except Exception:
            pass

    # 2. LIVE HOURS PRIMARY: Google Finance
    try:
        url = f"https://www.google.com/finance/quote/{ticker.upper()}:{exchange.upper()}"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            div = soup.find(class_="YMlKec fxKbKc")
            if div:
                return float(div.text.replace('₹', '').replace(',', '').strip())
    except Exception:
        pass

    # 3. LIVE HOURS SECONDARY: CNBC
    try:
        cnbc_ticker = f"{ticker.upper()}.NS" if exchange.upper() == "NSE" else f"{ticker.upper()}.BO"
        url_cnbc = f"https://www.cnbc.com/quotes/{cnbc_ticker}"
        res_cnbc = requests.get(url_cnbc, headers=headers, timeout=5)
        if res_cnbc.status_code == 200:
            soup = BeautifulSoup(res_cnbc.text, 'html.parser')
            span = soup.find("span", class_="QuoteStrip-lastPrice")
            if span:
                return float(span.text.replace('₹', '').replace(',', '').strip())
    except Exception:
        pass

    # 4. LIVE HOURS BACKUP: yfinance fast_info
    if live_session:
        try:
            yf_ticker = f"{ticker.upper()}.NS" if exchange.upper() == "NSE" else f"{ticker.upper()}.BO"
            return round(float(yf.Ticker(yf_ticker).fast_info['lastPrice']), 2)
        except Exception as e:
            print(f"[{ticker}] All price sources failed: {e}")

    return None

def get_technical_readout(symbol: str, exchange: str, action: str) -> dict:
    """Calculates Wilder's RSI (14) and 1-hour momentum on 15m candles."""
    try:
        yf_symbol = f"{symbol}.BO" if exchange.upper() == "BSE" else f"{symbol}.NS"
        stock = yf.Ticker(yf_symbol)
        df = stock.history(period="5d", interval="15m")

        if df.empty or len(df) < 15:
            return {"rsi": "N/A", "mom": "N/A", "bias": "Target Met", "summary": "Price reached target level."}

        delta = df['Close'].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        
        rs = avg_gain / avg_loss.replace(0, 0.001)
        rsi_val = float((100 - (100 / (1 + rs))).dropna().iloc[-1])

        curr_price = float(df['Close'].iloc[-1])
        ref_price = float(df['Close'].iloc[-5])
        mom_val = ((curr_price - ref_price) / ref_price) * 100

        if action.upper() == "BUY":
            if rsi_val <= 35:
                bias, summary = "Oversold Range", "Intraday momentum oversold; watch for support."
            elif rsi_val >= 68:
                bias, summary = "Overbought", "Price extended short-term; risk of pullback."
            else:
                bias, summary = "Neutral Momentum", "Consolidating within standard range."
        else:
            if rsi_val >= 65:
                bias, summary = "Overbought Exhaustion", "Momentum stretched near resistance."
            elif rsi_val <= 32:
                bias, summary = "Oversold", "Extended selloff; risk of technical bounce."
            else:
                bias, summary = "Target Met", "Target matched under normal momentum."

        return {"rsi": f"{rsi_val:.1f}", "mom": f"{mom_val:+.2f}%", "bias": bias, "summary": summary}
    except Exception:
        return {"rsi": "N/A", "mom": "N/A", "bias": "Target Met", "summary": "Alert target reached."}

def process_cluster(cluster_data):
    """Processes unique stock clusters, applies custom bands in memory, and fans out WebPush."""
    symbol = cluster_data['symbol']
    exchange = cluster_data['exchange']
    user_alerts = cluster_data['alerts']
    now_utc = cluster_data['now_utc']

    # Fetch live price once per unique stock
    price = get_live_price(symbol, exchange)
    if price is None:
        return None

    # Evaluate all user buffer bands in memory
    triggered_alerts = []
    for ua in user_alerts:
        lower = ua['target'] * (1 - ua['buffer'] / 100.0)
        upper = ua['target'] * (1 + ua['buffer'] / 100.0)

        if lower <= price <= upper:
            if ua['last_notified_at']:
                elapsed = (now_utc - ua['last_notified_at']).total_seconds()
                if elapsed < COOLDOWN_SECONDS:
                    continue
            triggered_alerts.append(ua)

    if not triggered_alerts:
        return None

    # Calculate indicators once per action type
    has_buys = any(a['action_type'] == "BUY" for a in triggered_alerts)
    has_sells = any(a['action_type'] == "SELL" for a in triggered_alerts)

    ta_buy = get_technical_readout(symbol, exchange, "BUY") if has_buys else None
    ta_sell = get_technical_readout(symbol, exchange, "SELL") if has_sells else None

    dead_endpoints = []
    notified_alert_ids = []

    # Fan-Out to all triggered users concurrently
    for ua in triggered_alerts:
        ta = ta_buy if ua['action_type'] == "BUY" else ta_sell
        action_icon = "🟢 BUY" if ua['action_type'] == "BUY" else "🔴 SELL"

        payload = json.dumps({
            "title": f"{action_icon} {symbol} ({exchange}): ₹{price:.2f}",
            "body": (
                f"Target: ₹{ua['target']} (±{ua['buffer']}%)\n"
                f"• 15m RSI: {ta['rsi']} — {ta['bias']}\n"
                f"• Mom (1h): {ta['mom']}\n"
                f"• Strategy: {ta['summary']}\n"
                f"• Note: If my work is done delete me otherwise I will notify again"
            ),
            "url": "/"
        })

        sent = False
        for dev in ua['devices']:
            try:
                webpush(
                    subscription_info={"endpoint": dev['endpoint'], "keys": {"p256dh": dev['p256dh'], "auth": dev['auth']}},
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS,
                    headers={"Urgency": "high", "TTL": "3600"}
                )
                sent = True
            except WebPushException as ex:
                if "410" in str(ex) or "404" in str(ex):
                    dead_endpoints.append(dev['endpoint'])

        if sent:
            notified_alert_ids.append(ua['id'])

    return {"notified_alert_ids": notified_alert_ids, "dead_endpoints": dead_endpoints}

def is_market_hours() -> bool:
    """Returns True only if current time is Mon-Fri between 09:15 and 15:30 IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close

def run_scanner():
    """Main execution block with Pub/Sub Alert Clustering."""
    now_ist = datetime.now(IST)
    now_utc = datetime.now(timezone.utc)
    print(f"\n--- [Market Scan Cycle: {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST] ---")

    db = SessionLocal()
    try:
        purge_expired_daily_alerts(db)

        if not is_market_hours():
            print("Action: Cash market closed. Standing by.")
            return

        db_alerts = db.query(Watchlist).all()
        db_devices = db.query(Device).filter(Device.notifications_enabled == True).all()

        if not db_alerts or not db_devices:
            print("Action: No active alerts or devices. Standing by.")
            return

        device_map = {}
        for d in db_devices:
            device_map.setdefault(d.username, []).append({
                "endpoint": d.endpoint, "p256dh": d.p256dh, "auth": d.auth
            })

        # Cluster alerts by (symbol, exchange)
        clusters = {}
        for a in db_alerts:
            if a.username in device_map:
                key = (a.symbol, a.exchange)
                if key not in clusters:
                    clusters[key] = []
                
                clusters[key].append({
                    "id": a.id, "target": a.target, "buffer": a.buffer,
                    "action_type": a.action_type, 
                    "last_notified_at": a.last_notified_at.replace(tzinfo=timezone.utc) if a.last_notified_at else None,
                    "devices": device_map[a.username]
                })

        cluster_tasks = []
        for (symbol, exchange), alerts in clusters.items():
            cluster_tasks.append({
                "symbol": symbol,
                "exchange": exchange,
                "alerts": alerts,
                "now_utc": now_utc
            })

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(process_cluster, cluster_tasks))

        all_notified_ids = []
        all_dead_endpoints = []
        for res in results:
            if res:
                all_notified_ids.extend(res.get("notified_alert_ids", []))
                all_dead_endpoints.extend(res.get("dead_endpoints", []))

        if all_notified_ids:
            db.query(Watchlist).filter(Watchlist.id.in_(all_notified_ids)).update({"last_notified_at": now_utc}, synchronize_session=False)
        if all_dead_endpoints:
            db.query(Device).filter(Device.endpoint.in_(all_dead_endpoints)).delete(synchronize_session=False)
            
        db.commit()

    except Exception as e:
        print(f"Scanner exception: {e}")
        db.rollback()
    finally:
        db.close()