"""SRT Restreamer Web Application"""
from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pathlib import Path
import os

# Base directory is the project root (parent of backend/)
BASE_DIR = Path(__file__).resolve().parent.parent

from models import init_db, get_db
from auth import create_default_user, get_current_user
from api import router

app = FastAPI(title="SRT Restreamer", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router, prefix="/api")

# Static files
app.mount("/static", StaticFiles(directory=BASE_DIR / "frontend" / "static"), name="static")

@app.on_event("startup")
def startup():
    init_db()
    db = next(get_db())
    try:
        create_default_user(db)
    finally:
        db.close()

@app.get("/", response_class=HTMLResponse)
def read_root():
    with open(BASE_DIR / "frontend" / "templates" / "index.html", "r") as f:
        return f.read()

@app.get("/login", response_class=HTMLResponse)
def login_page():
    with open(BASE_DIR / "frontend" / "templates" / "login.html", "r") as f:
        return f.read()

if __name__ == "__main__":
    import uvicorn
    host = os.getenv("UVICORN_HOST", "0.0.0.0")
    port = int(os.getenv("UVICORN_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)
