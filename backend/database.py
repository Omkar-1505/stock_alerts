import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
    raise ValueError("CRITICAL: Set a valid PostgreSQL DATABASE_URL in .env")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    username = Column(String, default="Guest")
    endpoint = Column(String, unique=True, nullable=False)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    device_type = Column(String, default="MOBILE")
    notifications_enabled = Column(Boolean, default=True)

class Watchlist(Base):
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    username = Column(String, default="Guest")
    list_type = Column(String, default="MONTHLY")
    exchange = Column(String, default="NSE")
    symbol = Column(String, nullable=False)
    target = Column(Float, nullable=False)
    buffer = Column(Float, default=1.0)
    action_type = Column(String, default="BUY")
    last_notified_at = Column(DateTime(timezone=True), nullable=True) # 1-Hour Cooldown Tracker

def init_db():
    Base.metadata.create_all(bind=engine)

if __name__ == "__main__":
    init_db()
    print("Database schema updated with cooldown timestamps.")