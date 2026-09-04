import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
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

def purge_expired_daily_alerts(db):
    """Drops any DAILY alert whose calendar day in IST has passed or concluded after 15:30 IST."""
    now_ist = datetime.now(IST)
    today_date = now_ist.date()
    
    daily_items = db.query(Watchlist).filter(Watchlist.list_type == "DAILY").all()
    purged_count = 0

    for item in daily_items:
        created_time = item.created_at
        if created_time is None:
            continue
        if created_time.tzinfo is None:
            created_time = created_time.replace(tzinfo=timezone.utc)
        
        created_ist = created_time.astimezone(IST)
        
        # Condition A: Created on a previous calendar date
        # Condition B: Created today, but trading session ended (after 15:30 IST)
        if created_ist.date() < today_date or (created_ist.date() == today_date and now_ist.time() >= datetime.strptime("15:30", "%H:%M").time()):
            db.delete(item)
            purged_count += 1

    if purged_count > 0:
        db.commit()
        print(f"🧹 [Auto-Purge] Dropped {purged_count} expired DAILY watchlist item(s).")

def get_live_price(ticker: str, exchange: str = "NSE") -> float:
    """Waterfall Scraper: Google Finance -> CNBC -> yfinance fallback."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    ]
    headers = {"User-Agent": random.choice(user_agents)}

    # 1. Google Finance
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

    # 2. CNBC Fallback
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

    # 3. yfinance Fallback
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
            return {
                "rsi": "N/A",
                "mom": "N/A",
                "bias": "Target Met",
                "summary": "Target reached. Indicator data synchronizing."
            }

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
                bias = "Oversold Dip"
                summary = "Strong accumulation area. Downside momentum stabilizing."
            elif rsi_val >= 68:
                bias = "Overbought"
                summary = "Caution on buy. Extended levels, expect pullback."
            else:
                bias = "Healthy Range"
                summary = "Favorable accumulation zone near support."
        else:
            if rsi_val >= 65:
                bias = "Profit Booking Zone"
                summary = "Momentum slowing near resistance. Optimal exit window."
            elif rsi_val <= 32:
                bias = "Severely Oversold"
                summary = "Caution on sell. Bounce probability elevated."
            else:
                bias = "Resistance Hit"
                summary = "Target ceiling matched. Execute planned exit."

        return {
            "rsi": f"{rsi_val:.1f}",
            "mom": f"{mom_val:+.2f}%",
            "bias": bias,
            "summary": summary
        }
    except Exception as e:
        print(f"[{ticker}] Indicator computation notice: {e}")
        return {
            "rsi": "N/A",
            "mom": "N/A",
            "bias": "Target Met",
            "summary": "Target matched. Review active chart."
        }

def is_market_hours() -> bool:
    """Returns True if current time is Monday-Friday between 09:15 and 15:30 IST."""
    now_ist = datetime.now(IST)
    if now_ist.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    return market_open <= now_ist <= market_close

def run_scanner():
    """Performs one scan cycle across all watchlist targets and drops expired daily alerts."""
    now_ist = datetime.now(IST)
    ist_str = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
    print(f"\n--- [Market Scan Cycle: {ist_str}] ---")

    db = SessionLocal()
    try:
        purge_expired_daily_alerts(db)

        active_alerts = db.query(Watchlist).all()
        active_devices = db.query(Device).filter(Device.notifications_enabled == True).all()

        print(f"Status: {len(active_alerts)} Alerts Active | {len(active_devices)} Devices Connected")

        if not active_alerts:
            print("Action: No alert targets configured. Waiting for user input.")
            return

        if not active_devices:
            print("Action: Targets exist, but no devices are connected to receive alerts.")
            return

        now_utc = datetime.now(timezone.utc)

        for alert in active_alerts:
            price = get_live_price(alert.symbol, alert.exchange)
            if price is None:
                print(f"[{alert.symbol}] Price fetch returned empty. Skipping this tick.")
                continue

            lower = alert.target * (1 - alert.buffer / 100.0)
            upper = alert.target * (1 + alert.buffer / 100.0)

            print(f"[{alert.symbol}] Live: ₹{price} | Target: ₹{alert.target} ({alert.list_type} Band: ₹{lower:.2f} - ₹{upper:.2f})")

            if lower <= price <= upper:
                if alert.last_notified_at is not None:
                    last_time = alert.last_notified_at
                    if last_time.tzinfo is None:
                        last_time = last_time.replace(tzinfo=timezone.utc)
                    elapsed = (now_utc - last_time).total_seconds()
                    if elapsed < COOLDOWN_SECONDS:
                        mins_left = int((COOLDOWN_SECONDS - elapsed) // 60)
                        print(f"[{alert.symbol}] Target in zone, but 1-hour cooldown active ({mins_left}m remaining).")
                        continue

                ta = get_technical_advice(alert.symbol, alert.exchange, alert.action_type)
                action_icon = "🟢 BUY" if alert.action_type.upper() == "BUY" else "🔴 SELL"

                # Notification formatted with RSI & Momentum
                payload = json.dumps({
                    "title": f"{action_icon} {alert.symbol} ({alert.exchange}): ₹{price}",
                    "body": (
                        f"Target: ₹{alert.target} (±{alert.buffer}%)\n"
                        f"• RSI (14): {ta['rsi']} — {ta['bias']}\n"
                        f"• 1h Momentum: {ta['mom']}\n"
                        f"• Strategy: {ta['summary']}"
                    )
                })

                user_devices = db.query(Device).filter(
                    Device.username == alert.username,
                    Device.notifications_enabled == True
                ).all()

                sent_count = 0
                for dev in user_devices:
                    try:
                        webpush(
                            subscription_info={
                                "endpoint": dev.endpoint,
                                "keys": {"p256dh": dev.p256dh, "auth": dev.auth}
                            },
                            data=payload,
                            vapid_private_key=VAPID_PRIVATE_KEY,
                            vapid_claims=VAPID_CLAIMS
                        )
                        sent_count += 1
                    except WebPushException as ex:
                        print(f"Push dispatch error on device {dev.id}: {ex}")
                        if "410" in str(ex) or "404" in str(ex):
                            db.delete(dev)

                if sent_count > 0:
                    print(f"🚨 Target breached for {alert.symbol}! Delivered push to {sent_count} device(s) of '{alert.username}'.")
                    alert.last_notified_at = now_utc
                    db.commit()
    finally:
        db.close()

if __name__ == "__main__":
    print("==================================================")
    print("🚀 StockPulse Market Engine Worker Online (Loop Mode)")
    print("==================================================")
    while True:
        try:
            run_scanner()
        except Exception as e:
            print(f"Unexpected cycle error: {e}")
        time.sleep(60)