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
from typing import List, Optional
import os
import urllib.parse
import urllib.request
import urllib.error
import json
import secrets

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
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
    _add_column_if_missing(_conn, "users", "is_free", "INTEGER DEFAULT 0")
    _add_column_if_missing(_conn, "users", "full_name", "VARCHAR")

    _add_column_if_missing(_conn, "items", "status", "VARCHAR DEFAULT 'active'")
    _add_column_if_missing(_conn, "items", "listing_type", "VARCHAR")
    _add_column_if_missing(_conn, "items", "listing_price_paid", "FLOAT DEFAULT 0")
    _add_column_if_missing(_conn, "items", "listing_duration_days", "INTEGER DEFAULT 60")
    _add_column_if_missing(_conn, "items", "expires_at", "DATETIME")
    _add_column_if_missing(_conn, "items", "is_featured", "INTEGER DEFAULT 0")
    _add_column_if_missing(_conn, "items", "boost_selected", "INTEGER DEFAULT 0")
    _add_column_if_missing(_conn, "items", "address", "VARCHAR")
    _add_column_if_missing(_conn, "items", "seller_phone", "VARCHAR")

    _add_column_if_missing(_conn, "payment_orders", "provider", "VARCHAR")
    _add_column_if_missing(_conn, "payment_orders", "amount", "FLOAT")
    _add_column_if_missing(_conn, "payment_orders", "currency", "VARCHAR DEFAULT 'NOK'")
    _add_column_if_missing(_conn, "payment_orders", "status", "VARCHAR DEFAULT 'created'")
    _add_column_if_missing(_conn, "payment_orders", "provider_reference", "VARCHAR")
    _add_column_if_missing(_conn, "payment_orders", "listing_type", "VARCHAR")
    _add_column_if_missing(_conn, "payment_orders", "listing_duration_days", "INTEGER")
    _add_column_if_missing(_conn, "payment_orders", "expires_at", "DATETIME")
    _add_column_if_missing(_conn, "payment_orders", "item_price", "FLOAT")
    _add_column_if_missing(_conn, "payment_orders", "created_at", "DATETIME")

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

APP_BASE_URL = os.getenv("APP_BASE_URL", "https://selga.no")

VIPPS_REDIRECT_URI = os.getenv(
    "VIPPS_REDIRECT_URI",
    "https://selga.no/auth/vipps/callback"
)

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth = OAuth2PasswordBearer(tokenUrl="login")


def normalize_payment_provider(provider: Optional[str]) -> str:
    value = (provider or "").strip().lower()
    if value in {"stripe", "vipps"}:
        return value
    return "vipps"


def calculate_listing_fee_nok(price: float, category: Optional[str], listing_mode: Optional[str], is_free_user: bool) -> float:
    if is_free_user:
        return 0.0

    category_text = (category or "").strip().lower()
    mode = (listing_mode or "").strip().lower()
    amount = float(price or 0)

    if category_text.startswith("bil"):
        return 499.0
    if category_text.startswith("bolig"):
        return 799.0 if mode == "rent" else 1499.0
    if amount <= 0 or amount < 2000:
        return 0.0
    if amount <= 9999:
        return 199.0
    return 299.0

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
INVITE_CODE = os.getenv("INVITE_CODE", "")

@app.post("/register")
def register(username: str = Form(None), password: str = Form(...), phone: str = Form(None), full_name: str = Form(None), invite_code: str = Form(None), db: Session = Depends(get_db)):
    if not username and not phone:
        raise HTTPException(400, "Brukernavn eller mobilnummer er påkrevd")

    effective_username = username if username else phone

    existing = db.query(UserDB).filter(UserDB.username == effective_username).first()
    if existing:
        raise HTTPException(400, "Bruker finnes allerede")

    is_free = bool(INVITE_CODE and invite_code and invite_code.strip() == INVITE_CODE)

    user = UserDB(
        username=effective_username,
        password=pwd.hash(password),
        full_name=full_name.strip() if full_name else None,
        phone=phone,
        is_verified=bool(phone),
        is_free=is_free
    )

    db.add(user)
    db.commit()
    return {"msg": "ok", "is_free": is_free}

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

    return {"access_token": create_token(user.username)}

# === VIPPS ===
@app.get("/auth/vipps/url")
def vipps_auth_url():
    return {"url": "https://vipps.no"}  # placeholder

@app.get("/auth/vipps/callback")
def vipps_callback():
    frontend_url = "https://selga.no/app"
    return RedirectResponse(frontend_url)

# === LISTINGS (MINIMAL SAFE) ===
@app.get("/listings")
def listings(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    items = (
        db.query(ItemDB)
        .filter((ItemDB.status == "active") | (ItemDB.status.is_(None)))
        .order_by(ItemDB.id.desc())
        .all()
    )
    result = []
    for item in items:
        if item.expires_at and item.expires_at < now:
            continue
        owner = db.query(ListingOwnerDB).filter(ListingOwnerDB.item_id == item.id).first()
        seller_username = owner.username if owner else None
        seller_user = db.query(UserDB).filter(UserDB.username == seller_username).first() if seller_username else None
        d = {c.name: getattr(item, c.name) for c in item.__table__.columns}
        d["seller_username"] = seller_username
        d["seller_name"] = seller_user.full_name if seller_user and seller_user.full_name else seller_username
        d["seller_type"] = seller_user.seller_type if seller_user else "privat"
        d["seller_company_name"] = seller_user.company_name if seller_user else None
        result.append(d)
    return result

# === ADD LISTING ===
@app.post("/listings")
async def create_listing(
    title: str = Form(...),
    price: float = Form(...),
    description: str = Form(None),
    city: str = Form(None),
    category: str = Form(None),
    listing_mode: str = Form(None),
    boost: str = Form("false"),
    payment_provider: str = Form(None),
    address: str = Form(None),
    seller_phone: str = Form(None),
    image: UploadFile = File(None),
    images: Optional[List[UploadFile]] = File(None),
    token: str = Depends(oauth),
    db: Session = Depends(get_db)
):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    # Collect all uploaded files (support both single 'image' and multi 'images')
    all_files = []
    if images:
        all_files = [f for f in images if f and f.filename]
    if not all_files and image and image.filename:
        all_files = [image]

    image_urls = []
    for f in all_files:
        ext = Path(f.filename).suffix
        filename = f"{uuid4().hex}{ext}"
        dest = UPLOAD_DIR / filename
        content = await f.read()
        dest.write_bytes(content)
        image_urls.append(f"uploads/{filename}")

    image_url = json.dumps(image_urls) if image_urls else None

    boost_flag = boost.lower() == "true"
    payment_provider_value = normalize_payment_provider(payment_provider)
    fee_nok = calculate_listing_fee_nok(price, category, listing_mode, bool(user.is_free))
    payment_required = fee_nok > 0

    listing_type = listing_mode if listing_mode else "standard"
    duration = 120 if listing_type == "sale" else 60
    expires = datetime.utcnow() + timedelta(days=duration)

    listing_status = "pending_payment" if payment_required else "active"

    item = ItemDB(
        title=title,
        description=description,
        price=price,
        location=city,
        category=category,
        image_url=image_url,
        listing_type=listing_type,
        boost_selected=boost_flag,
        is_featured=boost_flag,
        listing_price_paid=0 if payment_required else fee_nok,
        listing_duration_days=duration,
        expires_at=expires,
        status=listing_status,
        address=address,
        seller_phone=seller_phone,
    )
    db.add(item)
    db.flush()

    owner = ListingOwnerDB(item_id=item.id, username=user.username)
    db.add(owner)

    payment_order_id = None
    if payment_required:
        order = PaymentOrderDB(
            item_id=item.id,
            buyer_username=user.username,
            provider=payment_provider_value,
            amount=fee_nok,
            currency="NOK",
            status="created",
            listing_type=listing_type,
            listing_duration_days=duration,
            expires_at=expires,
            item_price=price,
        )
        db.add(order)
        db.flush()
        payment_order_id = order.id

    db.commit()
    db.refresh(item)
    return {
        "msg": "created",
        "listing_id": item.id,
        "payment_required": payment_required,
        "payment_order_id": payment_order_id,
        "fee_nok": fee_nok,
        "provider": payment_provider_value,
    }

# === DELETE LISTING ===
@app.delete("/listings/{item_id}")
def delete_listing(item_id: int, token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    owner = db.query(ListingOwnerDB).filter(
        ListingOwnerDB.item_id == item_id,
        ListingOwnerDB.username == user.username
    ).first()
    if not owner:
        raise HTTPException(403, "Du eier ikke denne annonsen")

    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(404, "Annonse ikke funnet")

    item.status = "deleted"
    db.commit()
    return {"msg": "deleted"}

# === ACTIVATE LISTING AFTER PAYMENT ===
@app.post("/listings/{item_id}/activate")
def activate_listing(item_id: int, token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    owner = db.query(ListingOwnerDB).filter(
        ListingOwnerDB.item_id == item_id,
        ListingOwnerDB.username == user.username
    ).first()
    if not owner:
        raise HTTPException(403, "Du eier ikke denne annonsen")

    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(404, "Annonse ikke funnet")

    if item.status == "pending_payment":
        paid_order = (
            db.query(PaymentOrderDB)
            .filter(
                PaymentOrderDB.item_id == item_id,
                PaymentOrderDB.buyer_username == user.username,
                PaymentOrderDB.status == "paid",
            )
            .order_by(PaymentOrderDB.id.desc())
            .first()
        )
        if not paid_order:
            raise HTTPException(403, "Betaling mangler for denne annonsen")

    item.status = "active"
    db.commit()
    return {"msg": "activated"}


@app.get("/payments/orders/latest-pending")
def latest_pending_payment_order(token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    order = (
        db.query(PaymentOrderDB)
        .filter(
            PaymentOrderDB.buyer_username == user.username,
            PaymentOrderDB.status == "created",
        )
        .order_by(PaymentOrderDB.id.desc())
        .first()
    )
    if not order:
        return {"order": None}

    return {
        "order": {
            "id": order.id,
            "item_id": order.item_id,
            "provider": order.provider,
            "amount": order.amount,
            "currency": order.currency,
            "status": order.status,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
    }


@app.post("/payments/orders/{order_id}/confirm")
def confirm_payment_order(
    order_id: int,
    provider_reference: str = Form(None),
    token: str = Depends(oauth),
    db: Session = Depends(get_db),
):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
    if not order:
        raise HTTPException(404, "Betalingsordre ikke funnet")
    if order.buyer_username != user.username:
        raise HTTPException(403, "Du har ikke tilgang til denne betalingsordren")

    item = db.query(ItemDB).filter(ItemDB.id == order.item_id).first()
    if not item:
        raise HTTPException(404, "Annonse ikke funnet")

    owner = db.query(ListingOwnerDB).filter(
        ListingOwnerDB.item_id == item.id,
        ListingOwnerDB.username == user.username,
    ).first()
    if not owner:
        raise HTTPException(403, "Du eier ikke denne annonsen")

    if order.status != "paid":
        order.status = "paid"
        if provider_reference:
            order.provider_reference = provider_reference[:120]

    item.status = "active"
    item.listing_price_paid = float(order.amount or 0)

    db.commit()
    return {
        "msg": "confirmed",
        "order_id": order.id,
        "listing_id": item.id,
        "status": "active",
    }

# === STRIPE CHECKOUT SESSION ===
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")

@app.post("/create-checkout-session")
async def create_checkout_session(
    item_id: int = Form(...),
    amount: float = Form(...),
    title: str = Form(None),
    token: str = Depends(oauth),
    db: Session = Depends(get_db)
):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(503, "Stripe er ikke konfigurert")

    import stripe as stripe_lib
    stripe_lib.api_key = STRIPE_SECRET_KEY

    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(404, "Annonse ikke funnet")

    owner = db.query(ListingOwnerDB).filter(
        ListingOwnerDB.item_id == item_id,
        ListingOwnerDB.username == user.username,
    ).first()
    if not owner:
        raise HTTPException(403, "Du eier ikke denne annonsen")

    order = (
        db.query(PaymentOrderDB)
        .filter(
            PaymentOrderDB.item_id == item_id,
            PaymentOrderDB.buyer_username == user.username,
            PaymentOrderDB.provider == "stripe",
            PaymentOrderDB.status == "created",
        )
        .order_by(PaymentOrderDB.id.desc())
        .first()
    )

    amount_oere = int(round(amount * 100))
    order_q = f"&order_id={order.id}" if order else ""
    success_url = f"{APP_BASE_URL}/?payment=success&listing_id={item_id}{order_q}&session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{APP_BASE_URL}/?payment=cancel"

    session = stripe_lib.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "nok",
                "product_data": {"name": title or item.title or "Annonsering"},
                "unit_amount": amount_oere,
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
    )

    if order:
        order.provider_reference = session.id
        db.commit()

    return {"checkout_url": session.url}

# === TEST ===
@app.get("/ping")
def ping():
    return {"status": "ok"}