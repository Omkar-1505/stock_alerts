import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL or not DATABASE_URL.startswith("postgresql"):
    raise ValueError("CRITICAL: Set a valid PostgreSQL DATABASE_URL in .env")

connect_args = {}
if "neon.tech" in DATABASE_URL or "supabase.co" in DATABASE_URL:
    connect_args = {"sslmode": "require"}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(60), unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(60), index=True, nullable=False)
    endpoint = Column(String, unique=True, nullable=False)
    p256dh = Column(String, nullable=False)
    auth = Column(String, nullable=False)
    device_name = Column(String(100), default="Web Browser")
    device_type = Column(String(20), default="DESKTOP")
    notifications_enabled = Column(Boolean, default=True)
    last_active = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

class Watchlist(Base):
    __tablename__ = "watchlist"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(60), index=True, nullable=False)
    list_type = Column(String(10), default="MONTHLY")  # MONTHLY or DAILY
    exchange = Column(String(10), default="NSE")
    symbol = Column(String(30), nullable=False)
    target = Column(Float, nullable=False)
    buffer = Column(Float, default=1.0)
    action_type = Column(String(10), default="BUY")
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

def init_db():
    Base.metadata.create_all(bind=engine)
    # Safe non-destructive column additions
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW();"))
        conn.execute(text("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_notified_at TIMESTAMP WITH TIME ZONE;"))
        conn.commit()

if __name__ == "__main__":
    init_db()
    print("PostgreSQL Database tables and columns verified.")