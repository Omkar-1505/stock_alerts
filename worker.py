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
COOLDOWN_SECONDS = 3600  # 1-hour cooldown between identical stock triggers

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
        while target_date.weekday() >= 5:  # Skip Saturday (5) and Sunday (6)
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
        # Fast single SQL query delete
        db.query(Watchlist).filter(Watchlist.id.in_(purged_ids)).delete(synchronize_session=False)
        db.commit()
        print(f"🧹 [Auto-Purge @ 15:30] Flushed {len(purged_ids)} expired DAILY watchlist item(s).")

def get_live_price(ticker: str, exchange: str = "NSE") -> float:
    """Waterfall Scraper: Google Finance -> CNBC -> yfinance fallback."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    ]
    headers = {"User-Agent": random.choice(user_agents)}

    try:
        url = f"https://www.google.com/finance/quote/{ticker}:{exchange}"
        res = requests.get(url, headers=headers, timeout=5)
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, 'html.parser')
            div = soup.find(class_="YMlKec fxKbKc")
            if div:
                return float(div.text.replace('₹', '').replace(',', '').strip())
    except Exception:
        pass

    try:
        cnbc_ticker = f"{ticker}.NS" if exchange.upper() == "NSE" else f"{ticker}.BO"
        url_cnbc = f"https://www.cnbc.com/quotes/{cnbc_ticker}"
        res_cnbc = requests.get(url_cnbc, headers=headers, timeout=5)
        if res_cnbc.status_code == 200:
            soup = BeautifulSoup(res_cnbc.text, 'html.parser')
            span = soup.find("span", class_="QuoteStrip-lastPrice")
            if span:
                return float(span.text.replace('₹', '').replace(',', '').strip())
    except Exception:
        pass

    try:
        yf_ticker = f"{ticker}.NS" if exchange.upper() == "NSE" else f"{ticker}.BO"
        stock = yf.Ticker(yf_ticker)
        df = stock.history(period="1d", interval="1m")
        if not df.empty:
            return round(float(df['Close'].iloc[-1]), 2)
    except Exception as e:
        print(f"[{ticker}] All price sources failed: {e}")

    return None

def get_technical_advice(ticker: str, exchange: str, action: str) -> dict:
    """Calculates 14-period RSI and 1-hour momentum."""
    try:
        yf_symbol = f"{ticker}.BO" if exchange.upper() == "BSE" else f"{ticker}.NS"
        stock = yf.Ticker(yf_symbol)
        
        df = stock.history(period="5d", interval="15m")
        if df.empty or len(df) < 15:
            df = stock.history(period="1mo", interval="1d")

        if df.empty or len(df) < 15:
            return {"rsi": "N/A", "mom": "N/A", "bias": "Target Met", "summary": "Target reached."}

        delta = df['Close'].diff()
        gain = delta.where(delta > 0, 0.0).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.001)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_val = float(rsi_series.dropna().iloc[-1])

        curr_price = float(df['Close'].iloc[-1])
        ref_idx = -5 if len(df) >= 5 else 0
        ref_price = float(df['Close'].iloc[ref_idx])
        mom_val = ((curr_price - ref_price) / ref_price) * 100

        if action.upper() == "BUY":
            if rsi_val <= 35:
                bias, summary = "Oversold Dip", "Strong accumulation area. Downside momentum stabilizing."
            elif rsi_val >= 68:
                bias, summary = "Overbought", "Caution on buy. Extended levels, expect pullback."
            else:
                bias, summary = "Healthy Range", "Favorable accumulation zone near support."
        else:
            if rsi_val >= 65:
                bias, summary = "Profit Booking Zone", "Momentum slowing near resistance. Optimal exit window."
            elif rsi_val <= 32:
                bias, summary = "Severely Oversold", "Caution on sell. Bounce probability elevated."
            else:
                bias, summary = "Resistance Hit", "Target ceiling matched. Execute planned exit."

        return {"rsi": f"{rsi_val:.1f}", "mom": f"{mom_val:+.2f}%", "bias": bias, "summary": summary}
    except Exception:
        return {"rsi": "N/A", "mom": "N/A", "bias": "Target Met", "summary": "Target matched."}

def process_single_alert(task_data):
    """Thread-safe worker function for fetching prices and sending notifications."""
    alert = task_data['alert']
    devices = task_data['devices']
    now_utc = task_data['now_utc']
    
    price = get_live_price(alert['symbol'], alert['exchange'])
    if price is None:
        return None

    lower = alert['target'] * (1 - alert['buffer'] / 100.0)
    upper = alert['target'] * (1 + alert['buffer'] / 100.0)
    
    print(f"[{alert['symbol']}] Live: ₹{price} | Target: ₹{alert['target']} ({alert['list_type']} Band: ₹{lower:.2f} - ₹{upper:.2f})")

    if lower <= price <= upper:
        if alert['last_notified_at']:
            elapsed = (now_utc - alert['last_notified_at']).total_seconds()
            if elapsed < COOLDOWN_SECONDS:
                return None  # Cooldown active

        ta = get_technical_advice(alert['symbol'], alert['exchange'], alert['action_type'])
        action_icon = "🟢 BUY" if alert['action_type'] == "BUY" else "🔴 SELL"

        payload = json.dumps({
            "title": f"{action_icon} {alert['symbol']} ({alert['exchange']}): ₹{price}",
            "body": (
                f"Target: ₹{alert['target']} (±{alert['buffer']}%)\n"
                f"• RSI (14): {ta['rsi']} — {ta['bias']}\n"
                f"• 1h Mom: {ta['mom']}\n"
                f"• Strategy: {ta['summary']}"
            ),
            "url": f"https://in.tradingview.com/chart/?symbol={alert['symbol']}"
        })

        sent_count = 0
        dead_endpoints = []
        for dev in devices:
            try:
                webpush(
                    subscription_info={"endpoint": dev['endpoint'], "keys": {"p256dh": dev['p256dh'], "auth": dev['auth']}},
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims=VAPID_CLAIMS,
                    headers={"Urgency": "high", "TTL": "60"}
                )
                sent_count += 1
            except WebPushException as ex:
                if "410" in str(ex) or "404" in str(ex):
                    dead_endpoints.append(dev['endpoint'])
        
        if sent_count > 0:
            print(f"🚨 Target breached for {alert['symbol']}! Delivered to {sent_count} device(s).")
            return {"alert_id": alert['id'], "dead_endpoints": dead_endpoints}
            
    return None

def is_market_hours() -> bool:
    """Returns True only if current time is Mon-Fri between 09:15 and 15:30 IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close

def run_scanner():
    """Main execution block managed by FastAPI BackgroundTasks."""
    now_ist = datetime.now(IST)
    now_utc = datetime.now(timezone.utc)
    print(f"\n--- [Market Scan Cycle: {now_ist.strftime('%Y-%m-%d %H:%M:%S')} IST] ---")

    db = SessionLocal()
    try:
        # 1. Flush expired items first (Handles 15:30 IST daily drop)
        purge_expired_daily_alerts(db)

        # 2. Gatekeeper: Stop execution if outside trading hours
        if not is_market_hours():
            print("Action: Cash market closed (Trading hours: 09:15 - 15:30 IST Mon-Fri). Standing by.")
            return

        # 3. Read from DB safely before multithreading
        db_alerts = db.query(Watchlist).all()
        db_devices = db.query(Device).filter(Device.notifications_enabled == True).all()

        if not db_alerts or not db_devices:
            print("Action: No active alerts or devices. Standing by.")
            return

        # 4. Package data into thread-safe standard dictionaries
        device_map = {}
        for d in db_devices:
            device_map.setdefault(d.username, []).append({
                "endpoint": d.endpoint, "p256dh": d.p256dh, "auth": d.auth
            })

        tasks = []
        for a in db_alerts:
            if a.username in device_map:
                last_notified = a.last_notified_at.replace(tzinfo=timezone.utc) if a.last_notified_at else None
                tasks.append({
                    "alert": {
                        "id": a.id, "symbol": a.symbol, "exchange": a.exchange, "target": a.target,
                        "buffer": a.buffer, "list_type": a.list_type, "action_type": a.action_type,
                        "last_notified_at": last_notified
                    },
                    "devices": device_map[a.username],
                    "now_utc": now_utc
                })

        # 5. Execute network requests concurrently
        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(process_single_alert, tasks))

        # 6. Safely write back to DB on the main thread
        for res in results:
            if res:
                # Update cooldown timestamp
                db.query(Watchlist).filter(Watchlist.id == res['alert_id']).update({"last_notified_at": now_utc})
                # Drop dead subscriptions
                if res['dead_endpoints']:
                    db.query(Device).filter(Device.endpoint.in_(res['dead_endpoints'])).delete(synchronize_session=False)
        
        db.commit()

    except Exception as e:
        print(f"Scanner exception: {e}")
        db.rollback()
    finally:
        db.close()