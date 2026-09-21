import os
import sys
import asyncio
from datetime import datetime, timezone
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional

# --- STRICT ROOT PATH RESOLUTION ---
# __file__ is inside 'backend/'
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
# ROOT_DIR is the project root 'stock_alerts/'
ROOT_DIR = os.path.dirname(BACKEND_DIR)

# Sibling directories at the root level
FRONTEND_DIR = os.path.join(ROOT_DIR, "frontend")
STATIC_DIR = os.path.join(ROOT_DIR, "static")

if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from worker import run_scanner, purge_expired_daily_alerts, get_live_price
from backend.database import Device, SessionLocal, User, Watchlist, init_db

load_dotenv()
init_db()

app = FastAPI(title="StockPulse Pro - Multi-Device Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount the static directory for images
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

scan_lock = asyncio.Lock()

async def safe_execute_scanner():
    if scan_lock.locked():
        print("Warning: Previous scan active. Dropping this trigger to stay real-time.")
        return
    async with scan_lock:
        await asyncio.to_thread(run_scanner)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- EXPLICIT ROUTING FOR FRONTEND FILES ---

@app.get("/")
def serve_home():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

@app.get("/sw.js")
def serve_sw():
    return FileResponse(os.path.join(FRONTEND_DIR, "sw.js"), media_type="application/javascript")

@app.get("/listed_instruments.json")
def serve_instruments():
    return FileResponse(os.path.join(FRONTEND_DIR, "listed_instruments.json"), media_type="application/json")

# --- REQUEST SCHEMAS ---
class UserAuthRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)

class DeviceSubscribeRequest(BaseModel):
    username: str
    endpoint: str
    keys: dict
    device_name: Optional[str] = "Web Browser"
    device_type: Optional[str] = "DESKTOP"

class AlertRequest(BaseModel):
    username: str
    symbol: str
    exchange: str = "NSE"
    target: float
    buffer: float = 1.0
    action_type: str = "BUY"
    list_type: str = "MONTHLY"

class DeviceUnlinkRequest(BaseModel):
    endpoint: str

# --- ENDPOINTS ---
@app.get("/api/vapid-public-key")
def get_public_key():
    return {"public_key": os.getenv("VAPID_PUBLIC_KEY")}

@app.post("/api/users/authenticate")
def authenticate_user(req: UserAuthRequest, db=Depends(get_db)):
    clean_username = req.username.strip().lower()
    user = db.query(User).filter(User.username == clean_username).first()
    if not user:
        user = User(username=clean_username)
        db.add(user)
        db.commit()
        db.refresh(user)

    devices = db.query(Device).filter(Device.username == clean_username, Device.notifications_enabled == True).all()
    return {
        "status": "success",
        "username": user.username,
        "device_count": len(devices),
        "devices": [
            {"id": d.id, "device_name": d.device_name, "device_type": d.device_type, "last_active": d.last_active.strftime("%Y-%m-%d %H:%M") if d.last_active else "N/A"}
            for d in devices
        ]
    }

@app.get("/api/devices")
def get_user_devices(username: str, db=Depends(get_db)):
    clean_username = username.strip().lower()
    devices = db.query(Device).filter(Device.username == clean_username, Device.notifications_enabled == True).all()
    return {
        "device_count": len(devices),
        "devices": [
            {"id": d.id, "device_name": d.device_name, "device_type": d.device_type, "last_active": d.last_active.strftime("%Y-%m-%d %H:%M") if d.last_active else "N/A"}
            for d in devices
        ]
    }

@app.post("/api/devices/subscribe")
def subscribe_device(sub: DeviceSubscribeRequest, db=Depends(get_db)):
    clean_username = sub.username.strip().lower()
    user = db.query(User).filter(User.username == clean_username).first()
    if not user:
        user = User(username=clean_username)
        db.add(user)
        db.commit()

    device = db.query(Device).filter(Device.endpoint == sub.endpoint).first()
    now = datetime.now(timezone.utc)

    if not device:
        new_device = Device(
            username=clean_username, endpoint=sub.endpoint, p256dh=sub.keys.get("p256dh", ""),
            auth=sub.keys.get("auth", ""), device_name=sub.device_name or "Web Browser",
            device_type=sub.device_type or "DESKTOP", notifications_enabled=True, last_active=now
        )
        db.add(new_device)
    else:
        device.username = clean_username
        device.device_name = sub.device_name or device.device_name
        device.device_type = sub.device_type or device.device_type
        device.notifications_enabled = True
        device.last_active = now

    db.commit()
    total_devices = db.query(Device).filter(Device.username == clean_username, Device.notifications_enabled == True).count()
    return {"status": "success", "message": "Device linked", "total_devices": total_devices}

@app.get("/api/watchlist/{list_type}")
def get_watchlist(list_type: str, username: str, db=Depends(get_db)):
    clean_username = username.strip().lower()
    purge_expired_daily_alerts(db)
    return db.query(Watchlist).filter(Watchlist.username == clean_username, Watchlist.list_type == list_type.upper()).all()

@app.post("/api/alerts")
def add_alert(alert: AlertRequest, db=Depends(get_db)):
    clean_username = alert.username.strip().lower()
    purge_expired_daily_alerts(db)
    count = db.query(Watchlist).filter(Watchlist.username == clean_username, Watchlist.list_type == alert.list_type.upper()).count()
    
    if count >= 15:
        raise HTTPException(status_code=400, detail=f"Maximum 15 targets reached for {alert.list_type.upper()} list.")

    new_item = Watchlist(
        username=clean_username, list_type=alert.list_type.upper(), exchange=alert.exchange.upper(),
        symbol=alert.symbol.upper().strip(), target=alert.target, buffer=alert.buffer,
        action_type=alert.action_type.upper(), created_at=datetime.now(timezone.utc)
    )
    db.add(new_item)
    db.commit()
    db.refresh(new_item)
    
    return {
        "status": "success", 
        "message": f"{alert.action_type} alert added for {new_item.symbol}",
        "item": {
            "id": new_item.id, "username": new_item.username, "list_type": new_item.list_type,
            "exchange": new_item.exchange, "symbol": new_item.symbol, "target": new_item.target,
            "buffer": new_item.buffer, "action_type": new_item.action_type
        }
    }

@app.delete("/api/watchlist/{item_id}")
def delete_alert(item_id: int, db=Depends(get_db)):
    item = db.query(Watchlist).filter(Watchlist.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
        return {"status": "success"}
    raise HTTPException(status_code=404, detail="Alert not found")

@app.post("/api/devices/unlink")
def unlink_device(req: DeviceUnlinkRequest, db=Depends(get_db)):
    device = db.query(Device).filter(Device.endpoint == req.endpoint).first()
    if device:
        db.delete(device)
        db.commit()
    return {"status": "success", "message": "Device unlinked successfully"}

@app.get("/api/price")
async def fetch_live_price_api(symbol: str, exchange: str = "NSE"):
    price = await asyncio.to_thread(get_live_price, symbol, exchange)
    if price is None:
        raise HTTPException(status_code=404, detail="Failed to fetch live price. Symbol may be invalid.")
    return {"status": "success", "symbol": symbol, "price": price}

@app.get("/api/trigger-scan")
async def trigger_market_scan(token: str = ""):
    if token != os.getenv("CRON_SECRET", "super_secret_ping"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    asyncio.create_task(safe_execute_scanner())
    return {"status": "Market scan dispatched efficiently"}