import json
import os
import random
import time
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pywebpush import WebPushException, webpush
import requests
import yfinance as yf
from backend.database import Device, SessionLocal, Watchlist

load_dotenv()

VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY")
VAPID_CLAIMS = {"sub": os.getenv("VAPID_ADMIN_EMAIL", "mailto:admin@example.com")}

COOLDOWN_SECONDS = 3600  # 1 Hour Cooldown Between Alerts for the Same Stock

def get_live_price(ticker: str, exchange: str = "NSE") -> float:
    """Waterfall Scraper: Google Finance -> CNBC -> yfinance fallback."""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/115.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15",
    ]
    headers = {"User-Agent": random.choice(user_agents)}

    # Attempt 1: Google Finance
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

    # Attempt 2: CNBC Fallback
    try:
        cnbc_ticker = f"{ticker}.NS" if exchange == "NSE" else f"{ticker}.BO"
        url_cnbc = f"https://www.cnbc.com/quotes/{cnbc_ticker}"
        res_cnbc = requests.get(url_cnbc, headers=headers, timeout=5)
        if res_cnbc.status_code == 200:
            soup = BeautifulSoup(res_cnbc.text, 'html.parser')
            span = soup.find("span", class_="QuoteStrip-lastPrice")
            if span:
                return float(span.text.replace('₹', '').replace(',', '').strip())
    except Exception:
        pass

    # Attempt 3: yfinance API
    try:
        yf_ticker = f"{ticker}.NS" if exchange == "NSE" else f"{ticker}.BO"
        stock = yf.Ticker(yf_ticker)
        df = stock.history(period="1d", interval="1m")
        if not df.empty:
            return round(float(df['Close'].iloc[-1]), 2)
    except Exception as e:
        print(f"[{ticker}] All price sources failed: {e}")

    return None

def get_technical_advice(ticker: str, action: str) -> dict:
    try:
        stock = yf.Ticker(f"{ticker}.NS")
        df = stock.history(period="5d", interval="15m")
        if df.empty or len(df) < 15:
            return {"rsi": "N/A", "mom": "N/A", "advice": "Target hit. Momentum data pending."}

        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rsi = float((100 - (100 / (1 + (gain / loss)))).iloc[-1])

        current_close = df['Close'].iloc[-1]
        prev_close = df['Close'].iloc[-5] if len(df) >= 5 else df['Close'].iloc[0]
        mom = ((current_close - prev_close) / prev_close) * 100

        if action == "BUY":
            if rsi <= 35:
                advice = "Strong Accumulation Dip (Oversold with stabilizing momentum)."
            elif rsi >= 70:
                advice = "Caution on Buy (Overbought zone. Consider awaiting pullback)."
            else:
                advice = "Favorable Buy Zone (Momentum active)."
        else:
            if rsi >= 65:
                advice = "Optimal Profit Booking Zone (Overbought exhaustion near target)."
            elif rsi <= 35:
                advice = "Caution on Sell (Severely oversold bounce risk)."
            else:
                advice = "Target Resistance Hit (Execute planned exit)."

        return {"rsi": round(rsi, 1), "mom": round(mom, 2), "advice": advice}
    except Exception:
        return {"rsi": "N/A", "mom": "N/A", "advice": "Target matched. Check charts."}

def run_scanner():
    db = SessionLocal()
    try:
        # Fetch all configured alerts
        active_alerts = db.query(Watchlist).all()
        active_devices = db.query(Device).filter(Device.notifications_enabled == True).all()

        print("\n--- New Market Scan Cycle ---")
        print(f"Monitoring {len(active_alerts)} Alerts | {len(active_devices)} Connected Devices")

        if not active_alerts or not active_devices:
            return

        now = datetime.now(timezone.utc)

        for alert in active_alerts:
            price = get_live_price(alert.symbol, alert.exchange)
            if not price:
                print(f"[{alert.symbol}] Price unavailable.")
                continue

            lower = alert.target * (1 - alert.buffer / 100.0)
            upper = alert.target * (1 + alert.buffer / 100.0)

            print(f"[{alert.symbol}] Live: ₹{price} | Target: ₹{alert.target} (Band: ₹{lower:.2f} - ₹{upper:.2f})")

            # Check if live price sits inside the target buffer band
            if lower <= price <= upper:
                # Check 1-Hour Cooldown
                if alert.last_notified_at is not None:
                    # Normalize stored datetime to UTC aware
                    last_time = alert.last_notified_at
                    if last_time.tzinfo is None:
                        last_time = last_time.replace(tzinfo=timezone.utc)

                    elapsed = (now - last_time).total_seconds()
                    if elapsed < COOLDOWN_SECONDS:
                        remaining_mins = int((COOLDOWN_SECONDS - elapsed) // 60)
                        print(f"[{alert.symbol}] Target breached, but 1-hour cooldown active ({remaining_mins}m remaining).")
                        continue

                print(f"🚨 Target breached for {alert.symbol}! Dispatching push notification...")
                ta = get_technical_advice(alert.symbol, alert.action_type)
                action_icon = "🟢 BUY" if alert.action_type == "BUY" else "🔴 SELL"

                payload = json.dumps({
                    "title": f"🚨 {action_icon} {alert.symbol} ({alert.exchange}): ₹{price}",
                    "body": f"Target: ₹{alert.target} (±{alert.buffer}%)\nRSI: {ta['rsi']} | 1h Mom: {ta['mom']}%\nAdvice: {ta['advice']}"
                })

                sent_count = 0
                for dev in active_devices:
                    if dev.user_id == alert.user_id:
                        try:
                            webpush(
                                subscription_info={"endpoint": dev.endpoint, "keys": {"p256dh": dev.p256dh, "auth": dev.auth}},
                                data=payload,
                                vapid_private_key=VAPID_PRIVATE_KEY,
                                vapid_claims=VAPID_CLAIMS
                            )
                            sent_count += 1
                        except WebPushException as ex:
                            if "410" in str(ex) or "404" in str(ex):
                                db.delete(dev)

                if sent_count > 0:
                    print(f"--> Push notification delivered to {sent_count} device(s). Next alert available in 1 hour.")
                    alert.last_notified_at = now
                    db.commit()
    finally:
        db.close()

if __name__ == "__main__":
    print("Market Engine Active (1-Hour Throttled Mode)...")
    while True:
        run_scanner()
        time.sleep(60)