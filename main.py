# === SAME IMPORTS (unchanged) ===
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, inspect as sa_inspect
from database import SessionLocal, engine, Base, get_db
from models import ItemDB, UserDB, ListingOwnerDB, ContactMessageDB, PaymentOrderDB
from jose import jwt
from passlib.context import CryptContext
from uuid import uuid4
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
import os
import urllib.parse
import urllib.request
import urllib.error
import json
import secrets

app = FastAPI()
Base.metadata.create_all(bind=engine)

# === FIX: indent bug ===
def _add_column_if_missing(conn, table_name: str, column_name: str, ddl: str):
    cols = [c["name"] for c in sa_inspect(engine).get_columns(table_name)]
    if column_name not in cols:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
        conn.commit()

# === DB MIGRATION (same) ===
with engine.connect() as _conn:
    _add_column_if_missing(_conn, "users", "phone", "VARCHAR")
    _add_column_if_missing(_conn, "users", "vipps_sub", "VARCHAR")
    _add_column_if_missing(_conn, "users", "is_verified", "INTEGER DEFAULT 0")
    _add_column_if_missing(_conn, "users", "seller_type", "VARCHAR DEFAULT 'privat'")
    _add_column_if_missing(_conn, "users", "company_name", "VARCHAR")

    _add_column_if_missing(_conn, "items", "status", "VARCHAR DEFAULT 'active'")
    _add_column_if_missing(_conn, "items", "listing_type", "VARCHAR")
    _add_column_if_missing(_conn, "items", "listing_price_paid", "FLOAT DEFAULT 0")
    _add_column_if_missing(_conn, "items", "listing_duration_days", "INTEGER DEFAULT 60")
    _add_column_if_missing(_conn, "items", "expires_at", "DATETIME")
    _add_column_if_missing(_conn, "items", "is_featured", "INTEGER DEFAULT 0")
    _add_column_if_missing(_conn, "items", "boost_selected", "INTEGER DEFAULT 0")

    _conn.commit()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# === CORS ===
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# === CONFIG FIXED ===
SECRET = "change-this"
ALGO = "HS256"

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://selgo.onrender.com")

VIPPS_REDIRECT_URI = os.getenv(
    "VIPPS_REDIRECT_URI",
    "https://sego-qmo1.onrender.com/auth/vipps/callback"
)

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth = OAuth2PasswordBearer(tokenUrl="login")

# === DB ===
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# === AUTH ===
def create_token(username):
    return jwt.encode({"sub": username}, SECRET, algorithm=ALGO)

def get_user(token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401)
    return user

# === ROOT ===
@app.get("/")
def root():
    return FileResponse("index.html")

@app.get("/app")
def serve_app():
    return FileResponse("index.html")

# === REGISTER ===
@app.post("/register")
def register(username: str = Form(None), password: str = Form(...), phone: str = Form(None), db: Session = Depends(get_db)):
    if not username and not phone:
        raise HTTPException(400, "Brukernavn eller mobilnummer er påkrevd")

    effective_username = username if username else phone

    existing = db.query(UserDB).filter(UserDB.username == effective_username).first()
    if existing:
        raise HTTPException(400, "Bruker finnes allerede")

    user = UserDB(
        username=effective_username,
        password=pwd.hash(password),
        phone=phone,
        is_verified=bool(phone)
    )

    db.add(user)
    db.commit()
    return {"msg": "ok"}

# === LOGIN ===
@app.post("/login")
def login(
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = db.query(UserDB).filter(UserDB.username == username).first()

    if not user:
        raise HTTPException(401, "Feil login")

    if not pwd.verify(password, user.password):
        raise HTTPException(401, "Feil login")

    return {"access_token": user.username}

# === VIPPS ===
@app.get("/auth/vipps/url")
def vipps_auth_url():
    return {"url": "https://vipps.no"}  # placeholder

@app.get("/auth/vipps/callback")
def vipps_callback():
    frontend_url = "https://selgo.onrender.com/app"
    return RedirectResponse(frontend_url)

# === LISTINGS (MINIMAL SAFE) ===
@app.get("/listings")
def listings(db: Session = Depends(get_db)):
    return db.query(ItemDB).all()

# === ADD LISTING ===
@app.post("/listings")
def create_listing(title: str = Form(...), price: float = Form(...), db: Session = Depends(get_db)):
    item = ItemDB(title=title, price=price)
    db.add(item)
    db.commit()
    return {"msg": "created"}

# === TEST ===
@app.get("/ping")
def ping():
    return {"status": "ok"}