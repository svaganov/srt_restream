"""Database models for SRT Restreamer"""
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os

# Default DB is located in project_root/data, regardless of CWD
_PROJECT_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_DIR / "data" / "restreamer.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """Enable WAL mode and busy timeout for concurrent access."""
    if DATABASE_URL.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sessions = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")

class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(64), unique=True, index=True, nullable=False)
    csrf_token = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)
    last_used_at = Column(DateTime, default=datetime.utcnow)
    revoked = Column(Boolean, default=False)

    user = relationship("User", back_populates="sessions")

class InputStream(Base):
    __tablename__ = "input_streams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    srt_url = Column(String, nullable=False)  # srt://0.0.0.0:5000?mode=listener...
    mode = Column(String, default="listener")  # derived from URL: caller or listener
    passphrase_encrypted = Column(String, default="")
    status = Column(String, default="disconnected")  # connected, warning, disconnected
    status_message = Column(String, default="")
    is_active = Column(Boolean, default=False)
    desired_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    thumbnail_path = Column(String, default="")

    outputs = relationship("OutputStream", back_populates="input_stream", cascade="all, delete-orphan")

class OutputStream(Base):
    __tablename__ = "output_streams"
    id = Column(Integer, primary_key=True, index=True)
    input_stream_id = Column(Integer, ForeignKey("input_streams.id"), nullable=False)
    name = Column(String, nullable=False)
    srt_url = Column(String, nullable=False)  # srt://host:port?mode=caller...
    passphrase_encrypted = Column(String, default="")
    mode = Column(String, default="caller")  # caller or listener; deprecated, derived from URL
    status = Column(String, default="disconnected")
    status_message = Column(String, default="")
    is_active = Column(Boolean, default=False)
    desired_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    input_stream = relationship("InputStream", back_populates="outputs")

def init_db():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
