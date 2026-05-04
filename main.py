# === SAME IMPORTS (unchanged) ===
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Request
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
from typing import List, Optional, Dict, Any
from routes.vippsRoutes import build_vipps_router
from services.vippsService import getPaymentStatus, isFailedState, isPaidState, isTimeoutState
import os
import urllib.parse
import urllib.request
import urllib.error
import json
import secrets
import base64
import time

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
    _add_column_if_missing(_conn, "items", "expires_at", "TIMESTAMP")
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
    _add_column_if_missing(_conn, "payment_orders", "expires_at", "TIMESTAMP")
    _add_column_if_missing(_conn, "payment_orders", "item_price", "FLOAT")
    _add_column_if_missing(_conn, "payment_orders", "created_at", "TIMESTAMP")

    _add_column_if_missing(_conn, "contact_messages", "status", "VARCHAR DEFAULT 'sent'")
    _add_column_if_missing(_conn, "contact_messages", "created_at", "TIMESTAMP")

    _conn.commit()

UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

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

VIPPS_LOGIN_DISCOVERY_URL = os.getenv(
    "VIPPS_LOGIN_DISCOVERY_URL",
    "https://apitest.vipps.no/access-management-1.0/access/.well-known/openid-configuration",
)
VIPPS_CLIENT_ID = os.getenv("VIPPS_CLIENT_ID", "")
VIPPS_CLIENT_SECRET = os.getenv("VIPPS_CLIENT_SECRET", "")
VIPPS_LOGIN_SCOPE = os.getenv("VIPPS_LOGIN_SCOPE", "openid name phoneNumber")
VIPPS_LOGIN_SUCCESS_REDIRECT = os.getenv("VIPPS_LOGIN_SUCCESS_REDIRECT", "/app")

VIPPS_MERCHANT_SERIAL_NUMBER = os.getenv("VIPPS_MERCHANT_SERIAL_NUMBER", "")
VIPPS_SUBSCRIPTION_KEY = os.getenv("VIPPS_SUBSCRIPTION_KEY", "")
VIPPS_EPAYMENT_API_BASE = os.getenv("VIPPS_EPAYMENT_API_BASE", "https://apitest.vipps.no")
VIPPS_PAYMENT_SCOPE = os.getenv("VIPPS_PAYMENT_SCOPE", "ePayment")
VIPPS_PAYMENT_RETURN_URL = os.getenv("VIPPS_PAYMENT_RETURN_URL", "")

VIPPS_SYSTEM_NAME = os.getenv("VIPPS_SYSTEM_NAME", "selga-backend")
VIPPS_SYSTEM_VERSION = os.getenv("VIPPS_SYSTEM_VERSION", "1.0.0")
VIPPS_SYSTEM_PLUGIN_NAME = os.getenv("VIPPS_SYSTEM_PLUGIN_NAME", "selga")
VIPPS_SYSTEM_PLUGIN_VERSION = os.getenv("VIPPS_SYSTEM_PLUGIN_VERSION", "1.0.0")

VIPPS_LOGIN_STATES: Dict[str, Dict[str, Any]] = {}
VIPPS_STATE_TTL_SECONDS = 600

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth = OAuth2PasswordBearer(tokenUrl="login")
app.include_router(build_vipps_router(oauth, SECRET, ALGO, APP_BASE_URL))


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


def _vipps_cleanup_old_states():
    now = int(time.time())
    stale = [k for k, v in VIPPS_LOGIN_STATES.items() if now - int(v.get("created_at", 0)) > VIPPS_STATE_TTL_SECONDS]
    for key in stale:
        VIPPS_LOGIN_STATES.pop(key, None)


def _vipps_require_login_config():
    if not VIPPS_CLIENT_ID or not VIPPS_CLIENT_SECRET:
        raise HTTPException(503, "Vipps Login er ikke konfigurert")


def _http_json_request(url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None, body: Optional[bytes] = None, timeout: int = 20) -> Dict[str, Any]:
    req = urllib.request.Request(url=url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if hasattr(e, "read") else ""
        detail = raw
        try:
            parsed = json.loads(raw) if raw else {}
            detail = parsed.get("message") or parsed.get("detail") or raw
        except Exception:
            pass
        raise HTTPException(e.code, f"Vipps API-feil: {detail}")
    except urllib.error.URLError as e:
        raise HTTPException(502, f"Nettverksfeil mot Vipps: {e.reason}")


def _vipps_get_discovery() -> Dict[str, Any]:
    return _http_json_request(VIPPS_LOGIN_DISCOVERY_URL)


def _vipps_basic_auth_header() -> str:
    value = f"{VIPPS_CLIENT_ID}:{VIPPS_CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(value).decode("ascii")


def _vipps_exchange_auth_code(code: str, token_endpoint: str) -> Dict[str, Any]:
    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": VIPPS_REDIRECT_URI,
        }
    ).encode("utf-8")
    return _http_json_request(
        token_endpoint,
        method="POST",
        headers={
            "Authorization": _vipps_basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )


def _vipps_get_userinfo(userinfo_endpoint: str, access_token: str) -> Dict[str, Any]:
    return _http_json_request(
        userinfo_endpoint,
        method="GET",
        headers={"Authorization": f"Bearer {access_token}"},
    )

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

# === ADMIN: set user as free ===
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "") or os.getenv("ADMIN_PASSWORD", "") or os.getenv("ADMIN-PASSWORD", "")
INVITE_CODE = os.getenv("INVITE_CODE", "")

def _is_valid_admin_or_invite_secret(raw_secret: Optional[str]) -> bool:
    entered = (raw_secret or "").strip()
    admin_secret = (ADMIN_SECRET or "").strip()
    invite_secret = (INVITE_CODE or "").strip()
    if not entered:
        return False
    return (admin_secret and entered == admin_secret) or (invite_secret and entered == invite_secret)

@app.post("/admin/set-free")
def admin_set_free(username: str = Form(...), secret: str = Form(...), db: Session = Depends(get_db)):
    if not _is_valid_admin_or_invite_secret(secret):
        raise HTTPException(status_code=403, detail="Ikke autorisert")
    user = db.query(UserDB).filter(
        (UserDB.username == username) | (UserDB.phone == username)
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Bruker ikke funnet")
    user.is_free = 1
    db.commit()
    return {"msg": f"Bruker '{username}' er nå gratis"}

@app.post("/account/activate-free")
def activate_free_account(
    admin_password: str = Form(...),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    if not _is_valid_admin_or_invite_secret(admin_password):
        raise HTTPException(status_code=403, detail="Feil passord")

    if not bool(user.is_free):
        user.is_free = 1
        db.commit()

    return {"msg": "Gratis tilgang er aktiv", "is_free": True}

# === REGISTER ===
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
    username: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    password: str = Form(...),
    invite_code: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    identifier = (username or phone or "").strip()
    if not identifier:
        raise HTTPException(400, "Brukernavn eller mobilnummer er påkrevd")

    user = db.query(UserDB).filter(
        (UserDB.username == identifier) | (UserDB.phone == identifier)
    ).first()

    if not user:
        raise HTTPException(401, "Feil login")

    if not pwd.verify(password, user.password):
        raise HTTPException(401, "Feil login")

    # Allow existing accounts to be upgraded to free access with invite code.
    if INVITE_CODE and invite_code and invite_code.strip() == INVITE_CODE and not bool(user.is_free):
        user.is_free = 1
        db.commit()

    return {"access_token": create_token(user.username), "is_free": bool(user.is_free)}

# === VIPPS LOGIN (OIDC) ===
@app.get("/auth/vipps/url")
def vipps_auth_url(state: Optional[str] = None, return_to: Optional[str] = None):
    _vipps_require_login_config()
    _vipps_cleanup_old_states()

    discovery = _vipps_get_discovery()
    auth_endpoint = discovery.get("authorization_endpoint")
    if not auth_endpoint:
        raise HTTPException(502, "Mangler authorization_endpoint i Vipps discovery")

    server_state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    VIPPS_LOGIN_STATES[server_state] = {
        "created_at": int(time.time()),
        "client_state": state,
        "return_to": return_to,
        "nonce": nonce,
    }

    query = {
        "client_id": VIPPS_CLIENT_ID,
        "response_type": "code",
        "scope": VIPPS_LOGIN_SCOPE,
        "redirect_uri": VIPPS_REDIRECT_URI,
        "state": server_state,
        "nonce": nonce,
    }

    url = auth_endpoint + "?" + urllib.parse.urlencode(query)
    return {"url": url}


@app.get("/auth/vipps/callback")
def vipps_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None, db: Session = Depends(get_db)):
    target = VIPPS_LOGIN_SUCCESS_REDIRECT or "/app"

    if error:
        sep = "&" if "?" in target else "?"
        return RedirectResponse(f"{target}{sep}vipps_error=1")

    if not code or not state:
        raise HTTPException(400, "Mangler code/state fra Vipps")

    state_data = VIPPS_LOGIN_STATES.pop(state, None)
    if not state_data:
        raise HTTPException(400, "Ugyldig eller utløpt Vipps state")

    discovery = _vipps_get_discovery()
    token_endpoint = discovery.get("token_endpoint")
    userinfo_endpoint = discovery.get("userinfo_endpoint")
    if not token_endpoint or not userinfo_endpoint:
        raise HTTPException(502, "Ufullstendig Vipps OIDC discovery")

    token_data = _vipps_exchange_auth_code(code, token_endpoint)
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(502, "Fikk ikke access token fra Vipps Login")

    profile = _vipps_get_userinfo(userinfo_endpoint, access_token)
    vipps_sub = profile.get("sub")
    full_name = profile.get("name")
    phone = profile.get("phone_number") or profile.get("phoneNumber")
    if not vipps_sub:
        raise HTTPException(502, "Mangler sub i Vipps userinfo")

    user = db.query(UserDB).filter(UserDB.vipps_sub == vipps_sub).first()
    if not user and phone:
        user = db.query(UserDB).filter(UserDB.phone == phone).first()

    if not user:
        phone_tail = (phone or "000000")[-6:]
        generated_username = f"vipps_{phone_tail}_{secrets.token_hex(2)}"
        user = UserDB(
            username=generated_username,
            password=pwd.hash(secrets.token_urlsafe(24)),
            full_name=full_name,
            phone=phone,
            vipps_sub=vipps_sub,
            is_verified=bool(phone),
        )
        db.add(user)
    else:
        user.vipps_sub = vipps_sub
        if phone and not user.phone:
            user.phone = phone
        if full_name and not user.full_name:
            user.full_name = full_name
        if phone:
            user.is_verified = True

    db.commit()
    auth_token = create_token(user.username)

    redirect_base = state_data.get("return_to") or target
    sep = "&" if "?" in redirect_base else "?"
    return RedirectResponse(f"{redirect_base}{sep}token={urllib.parse.quote(auth_token)}")

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
        content = await f.read()
        mime = f.content_type or "image/jpeg"
        b64 = base64.b64encode(content).decode("utf-8")
        image_urls.append(f"data:{mime};base64,{b64}")

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
            PaymentOrderDB.status.in_(["created", "initiated"]),
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

    if order.provider == "vipps":
        reference = (provider_reference or order.provider_reference or "").strip()
        if not reference:
            raise HTTPException(400, "Mangler Vipps-referanse for verifisering")

        status_data = getPaymentStatus(reference)
        state = status_data["state"]

        if isPaidState(state):
            order.status = "paid"
            order.provider_reference = reference[:120]
        elif isFailedState(state):
            order.status = "failed"
            db.commit()
            raise HTTPException(409, "Vipps-betaling feilet eller ble avbrutt")
        elif isTimeoutState(state):
            order.status = "timeout"
            db.commit()
            raise HTTPException(408, "Vipps-betaling utløpt")
        else:
            order.status = "pending"
            db.commit()
            raise HTTPException(409, "Vipps-betaling er ikke fullført ennå")
    elif order.status != "paid":
        order.status = "paid"
        if provider_reference:
            order.provider_reference = provider_reference[:120]

    item.status = "active"
    item.listing_price_paid = float(order.amount or 0)

    db.commit()
    confirmation_number = f"SELGA-{order.id:05d}"
    return {
        "msg": "confirmed",
        "order_id": order.id,
        "confirmation_number": confirmation_number,
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
    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not stripe_secret_key:
        raise HTTPException(503, "Stripe checkout is not configured. Set STRIPE_SECRET_KEY in environment variables.")

    import stripe as stripe_lib
    stripe_lib.api_key = stripe_secret_key

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

# === CONTACT SELLER ===
@app.post("/contact-seller")
def contact_seller(
    item_id: int = Form(...),
    message: str = Form(...),
    token: str = Depends(oauth),
    db: Session = Depends(get_db)
):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    buyer = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not buyer:
        raise HTTPException(401, "Ikke innlogget")

    owner = db.query(ListingOwnerDB).filter(ListingOwnerDB.item_id == item_id).first()
    seller_username = owner.username if owner else None
    if not seller_username:
        raise HTTPException(404, "Selger ikke funnet for annonsen")

    msg = ContactMessageDB(
        item_id=item_id,
        buyer_username=buyer.username,
        seller_username=seller_username,
        message=message.strip(),
        status="sent",
        created_at=datetime.utcnow(),
    )
    db.add(msg)
    db.commit()
    return {"msg": "Melding sendt"}


@app.get("/messages/inbox")
def get_inbox_messages(limit: int = 30, token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    safe_limit = max(1, min(int(limit or 30), 100))
    rows = (
        db.query(ContactMessageDB)
        .filter(ContactMessageDB.seller_username == user.username)
        .order_by(ContactMessageDB.id.desc())
        .limit(safe_limit)
        .all()
    )

    item_ids = [r.item_id for r in rows if r.item_id]
    items = db.query(ItemDB).filter(ItemDB.id.in_(item_ids)).all() if item_ids else []
    item_map = {i.id: i for i in items}

    unread_count = 0
    messages = []
    for r in rows:
        is_read = (r.status or "").lower() == "read"
        if not is_read:
            unread_count += 1
        item = item_map.get(r.item_id)
        messages.append(
            {
                "id": r.id,
                "item_id": r.item_id,
                "item_title": item.title if item else None,
                "buyer_username": r.buyer_username,
                "seller_username": r.seller_username,
                "message": r.message,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )

    return {"messages": messages, "unread_count": unread_count}


@app.post("/messages/{message_id}/read")
def mark_inbox_message_read(message_id: int, token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401, "Ikke innlogget")

    row = db.query(ContactMessageDB).filter(ContactMessageDB.id == message_id).first()
    if not row:
        raise HTTPException(404, "Melding ikke funnet")
    if row.seller_username != user.username:
        raise HTTPException(403, "Ingen tilgang til denne meldingen")

    row.status = "read"
    db.commit()
    return {"msg": "ok"}


# === AI ASSISTANT ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

@app.post("/ai/assist")
async def ai_assist(message: str = Form(...)):
    if not OPENAI_API_KEY:
        return {"reply": "AI-assistenten er ikke konfigurert ennå. Ta kontakt med selger direkte."}

    import urllib.request, urllib.error, json as _json
    payload = _json.dumps({
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "Du er en hjelpsom kundeservice-assistent for SELGA, en norsk markedsplass. Svar kort og på norsk."},
            {"role": "user", "content": message}
        ],
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            reply = data["choices"][0]["message"]["content"].strip()
            return {"reply": reply}
    except Exception as e:
        return {"reply": f"Kunne ikke nå AI-assistenten akkurat nå. Prøv igjen senere."}