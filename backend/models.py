"""Database models for SRT Restreamer"""
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
from pathlib import Path
import os

# Default DB is located in project_root/data, regardless of CWD
_PROJECT_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_DIR / "data" / "restreamer.db"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{_DEFAULT_DB_PATH}")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class InputStream(Base):
    __tablename__ = "input_streams"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    srt_url = Column(String, nullable=False)  # srt://0.0.0.0:5000?mode=listener...
    status = Column(String, default="disconnected")  # connected, warning, disconnected
    status_message = Column(String, default="")
    is_active = Column(Boolean, default=False)
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
    mode = Column(String, default="caller")  # caller or listener
    status = Column(String, default="disconnected")
    status_message = Column(String, default="")
    is_active = Column(Boolean, default=False)
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
