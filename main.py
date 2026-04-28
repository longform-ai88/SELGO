from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import text, inspect as sa_inspect
from database import SessionLocal, engine, Base
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

def _add_column_if_missing(conn, table_name: str, column_name: str, ddl: str):
    cols = [c["name"] for c in sa_inspect(engine).get_columns(table_name)]
    if column_name not in cols:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"))
        conn.commit()


# Migrate existing DB: add new columns if missing
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

    _add_column_if_missing(_conn, "payment_orders", "listing_type", "VARCHAR")
    _add_column_if_missing(_conn, "payment_orders", "listing_duration_days", "INTEGER")
    _add_column_if_missing(_conn, "payment_orders", "expires_at", "DATETIME")
    _add_column_if_missing(_conn, "payment_orders", "item_price", "FLOAT")
    _add_column_if_missing(_conn, "payment_orders", "created_at", "DATETIME")

    # Users with phone or Vipps identity are treated as verified.
    _conn.execute(text("UPDATE users SET is_verified = 1 WHERE is_verified IS NULL OR (phone IS NOT NULL AND phone != '') OR (vipps_sub IS NOT NULL AND vipps_sub != '')"))
    _conn.execute(text("UPDATE users SET seller_type = 'privat' WHERE seller_type IS NULL OR seller_type = ''"))
    _conn.commit()

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SECRET = "change-this"
ALGO = "HS256"
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
VIPPS_CLIENT_ID = os.getenv("VIPPS_CLIENT_ID", "")
VIPPS_CLIENT_SECRET = os.getenv("VIPPS_CLIENT_SECRET", "")
VIPPS_SUBSCRIPTION_KEY = os.getenv("VIPPS_SUBSCRIPTION_KEY", "")
VIPPS_REDIRECT_URI = os.getenv("VIPPS_REDIRECT_URI", "http://localhost:8000/auth/vipps/callback")
VIPPS_ENV = os.getenv("VIPPS_ENVIRONMENT", "test")  # "test" or "production"
VIPPS_BASE = "https://apitest.vipps.no" if VIPPS_ENV == "test" else "https://api.vipps.no"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth = OAuth2PasswordBearer(tokenUrl="login")


def _is_car_category(category: str) -> bool:
    c = (category or "").strip().lower()
    return c.startswith("bil") or "car" in c


def _is_real_estate_category(category: str) -> bool:
    c = (category or "").strip().lower()
    return c.startswith("bolig") or "property" in c or "real estate" in c


def _normalize_listing_mode(mode: str) -> str:
    m = (mode or "").strip().lower()
    if m in {"sale", "sell", "salg", "kjop", "kjøp"}:
        return "sale"
    if m in {"rent", "rental", "leie", "utleie"}:
        return "rent"
    return ""


def calculate_listing_pricing(item_price: float, category: str, listing_mode: str = "", boost: bool = False):
    if item_price <= 0:
        raise HTTPException(status_code=400, detail="Pris må være større enn 0")

    if _is_car_category(category):
        if boost:
            return {
                "listing_type": "car_boost",
                "fee_nok": 699.0,
                "duration_days": 60,
                "featured": True,
            }
        return {
            "listing_type": "car_standard",
            "fee_nok": 499.0,
            "duration_days": 60,
            "featured": False,
        }

    if _is_real_estate_category(category):
        mode = _normalize_listing_mode(listing_mode)
        if not mode:
            mode = "sale"
        if mode == "sale":
            return {
                "listing_type": "real_estate_sale",
                "fee_nok": 1499.0,
                "duration_days": 120,
                "featured": False,
            }
        return {
            "listing_type": "real_estate_rent",
            "fee_nok": 799.0,
            "duration_days": 60,
            "featured": False,
        }

    if item_price < 2000:
        return {
            "listing_type": "free_verified_under_2000",
            "fee_nok": 0.0,
            "duration_days": 60,
            "featured": False,
        }

    if item_price <= 9999:
        return {
            "listing_type": "standard_2000_9999",
            "fee_nok": 199.0,
            "duration_days": 60,
            "featured": False,
        }

    if item_price <= 20000:
        return {
            "listing_type": "standard_10000_20000",
            "fee_nok": 299.0,
            "duration_days": 60,
            "featured": False,
        }

    return {
        "listing_type": "standard_10000_plus",
        "fee_nok": 299.0,
        "duration_days": 60,
        "featured": False,
    }


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_token(username):
    return jwt.encode({"sub": username}, SECRET, algorithm=ALGO)


def get_user(token: str = Depends(oauth), db: Session = Depends(get_db)):
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    user = db.query(UserDB).filter(UserDB.username == data["sub"]).first()
    if not user:
        raise HTTPException(401)
    return user


@app.get("/")
def root():
    return {"msg": "SELGO API running"}


@app.get("/app", response_class=FileResponse)
def serve_app():
    return FileResponse("index.html")


@app.post("/register")
def register(
    username: str = Form(None),
    password: str = Form(...),
    phone: str = Form(None),
    seller_type: str = Form("privat"),
    company_name: str = Form(None),
    db: Session = Depends(get_db)
):
    # Mobile registration: phone becomes username if no username provided
    if not username and not phone:
        raise HTTPException(status_code=400, detail="Brukernavn eller mobilnummer er påkrevd")

    effective_username = username if username else phone

    existing = db.query(UserDB).filter(UserDB.username == effective_username).first()
    if existing:
        raise HTTPException(status_code=400, detail="Bruker finnes allerede")

    if phone:
        phone_existing = db.query(UserDB).filter(UserDB.phone == phone).first()
        if phone_existing:
            raise HTTPException(status_code=400, detail="Mobilnummer allerede registrert")

    normalized_seller_type = (seller_type or "privat").strip().lower()
    if normalized_seller_type not in {"privat", "naering"}:
        normalized_seller_type = "privat"

    user = UserDB(
        username=effective_username,
        password=pwd.hash(password),
        phone=phone,
        is_verified=bool(phone),
        seller_type=normalized_seller_type,
        company_name=(company_name or "").strip() or None
    )
    db.add(user)
    db.commit()
    return {"msg": "ok"}


@app.post("/login")
def login(
    username: str = Form(None),
    phone: str = Form(None),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    user = None
    if username:
        user = db.query(UserDB).filter(UserDB.username == username).first()
    if not user and phone:
        user = db.query(UserDB).filter(UserDB.phone == phone).first()
    if not user or not pwd.verify(password, user.password):
        raise HTTPException(status_code=401, detail="Feil brukernavn/mobilnummer eller passord")
    return {"access_token": create_token(user.username), "token_type": "bearer"}


@app.get("/auth/vipps/url")
def vipps_auth_url(state: str = ""):
    if not VIPPS_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Vipps Login er ikke konfigurert ennå. Sett VIPPS_CLIENT_ID i miljøvariabler.")
    if not state:
        state = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode({
        "client_id": VIPPS_CLIENT_ID,
        "response_type": "code",
        "scope": "openid phoneNumber name",
        "redirect_uri": VIPPS_REDIRECT_URI,
        "state": state,
    })
    url = f"{VIPPS_BASE}/access-management-1.0/access/oauth2/auth?{params}"
    return {"url": url, "state": state}


@app.get("/auth/vipps/callback")
def vipps_callback(code: str = None, state: str = None, error: str = None, db: Session = Depends(get_db)):
    frontend_url = "http://localhost:8000/app"

    if error or not code:
        return RedirectResponse(f"{frontend_url}?vipps_error=true")

    # Exchange code for access token
    token_url = f"{VIPPS_BASE}/access-management-1.0/access/oauth2/token"
    token_data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": VIPPS_REDIRECT_URI,
    }).encode()
    credentials = urllib.parse.quote(VIPPS_CLIENT_ID) + ":" + urllib.parse.quote(VIPPS_CLIENT_SECRET)
    import base64
    basic_auth = base64.b64encode(credentials.encode()).decode()

    try:
        req = urllib.request.Request(
            token_url,
            data=token_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic_auth}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            token_resp = json.loads(resp.read())
    except Exception:
        return RedirectResponse(f"{frontend_url}?vipps_error=true")

    vipps_access_token = token_resp.get("access_token")
    if not vipps_access_token:
        return RedirectResponse(f"{frontend_url}?vipps_error=true")

    # Get user info from Vipps
    userinfo_url = f"{VIPPS_BASE}/vipps-userinfo-api/userinfo"
    try:
        req2 = urllib.request.Request(
            userinfo_url,
            headers={"Authorization": f"Bearer {vipps_access_token}"}
        )
        with urllib.request.urlopen(req2, timeout=10) as resp2:
            userinfo = json.loads(resp2.read())
    except Exception:
        return RedirectResponse(f"{frontend_url}?vipps_error=true")

    vipps_sub = userinfo.get("sub")
    phone = userinfo.get("phone_number", "")
    name = userinfo.get("name", "")
    if not vipps_sub:
        return RedirectResponse(f"{frontend_url}?vipps_error=true")

    # Find or create user
    user = db.query(UserDB).filter(UserDB.vipps_sub == vipps_sub).first()
    if not user and phone:
        user = db.query(UserDB).filter(UserDB.phone == phone).first()
    if not user:
        # Create new user from Vipps info
        base_username = name.lower().replace(" ", ".") if name else phone or vipps_sub[:12]
        username = base_username
        counter = 1
        while db.query(UserDB).filter(UserDB.username == username).first():
            username = f"{base_username}{counter}"
            counter += 1
        user = UserDB(
            username=username,
            password=pwd.hash(secrets.token_urlsafe(32)),  # random password, login only via Vipps
            phone=phone or None,
            vipps_sub=vipps_sub,
            is_verified=True,
            seller_type="privat"
        )
        db.add(user)
    else:
        user.vipps_sub = vipps_sub
        user.is_verified = True
    db.commit()

    our_token = create_token(user.username)
    return RedirectResponse(f"{frontend_url}?token={our_token}&vipps_login=true")


@app.get("/listings")
def listings(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    items = db.query(ItemDB).all()
    owners = db.query(ListingOwnerDB).all()
    users = db.query(UserDB).all()
    owner_by_item = {o.item_id: o.username for o in owners}
    users_by_username = {u.username: u for u in users}

    visible = []
    for i in items:
        if i.status != "active":
            continue
        if i.expires_at and i.expires_at <= now:
            continue
        visible.append(i)

    # Boosted listings are sorted first for increased visibility.
    visible.sort(key=lambda x: (not bool(x.is_featured), -(x.id or 0)))

    payload = []
    for i in visible:
        seller_username = owner_by_item.get(i.id, "unknown")
        seller_user = users_by_username.get(seller_username)
        seller_type = (seller_user.seller_type if seller_user and seller_user.seller_type else "privat").lower()
        seller_id_num = seller_user.id if seller_user and seller_user.id is not None else 0
        seller_public_id = f"NARING-{seller_id_num}" if seller_type == "naering" else f"PRIVAT-{seller_id_num}"

        payload.append({
            "id": i.id,
            "title": i.title,
            "description": i.description,
            "price": i.price,
            "location": i.location,
            "category": i.category,
            "image_url": i.image_url,
            "seller_username": seller_username,
            "seller_type": seller_type,
            "seller_company_name": seller_user.company_name if seller_user else None,
            "seller_public_id": seller_public_id,
            "listing_type": i.listing_type,
            "listing_price_paid": i.listing_price_paid,
            "listing_duration_days": i.listing_duration_days,
            "expires_at": i.expires_at.isoformat() if i.expires_at else None,
            "is_featured": bool(i.is_featured),
        })
    return payload


@app.post("/listings")
def create_listing(
    title: str = Form(...),
    description: str = Form(...),
    price: float = Form(...),
    city: str = Form(...),
    category: str = Form(...),
    listing_mode: str = Form(""),
    boost: bool = Form(False),
    payment_provider: str = Form("vipps"),
    image: UploadFile = File(None),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    pricing = calculate_listing_pricing(
        item_price=price,
        category=category,
        listing_mode=listing_mode,
        boost=boost,
    )
    fee = float(pricing["fee_nok"])
    duration_days = int(pricing["duration_days"])
    expires_at = datetime.utcnow() + timedelta(days=duration_days)

    if fee == 0 and not user.is_verified:
        raise HTTPException(status_code=403, detail="Gratis annonser under 2000 NOK krever verifisert bruker")

    img_url = None
    if image:
        name = str(uuid4()) + image.filename
        path = UPLOAD_DIR / name
        with open(path, "wb") as f:
            f.write(image.file.read())
        img_url = str(path)

    provider = (payment_provider or "vipps").strip().lower()
    if provider not in {"vipps", "stripe"}:
        raise HTTPException(status_code=400, detail="payment_provider må være 'vipps' eller 'stripe'")

    initial_status = "active" if fee == 0 else "pending_payment"

    item = ItemDB(
        title=title,
        description=description,
        price=price,
        location=city,
        category=category,
        image_url=img_url,
        status=initial_status,
        listing_type=pricing["listing_type"],
        listing_price_paid=fee if fee == 0 else 0,
        listing_duration_days=duration_days,
        expires_at=expires_at,
        is_featured=bool(pricing["featured"]),
        boost_selected=bool(boost),
    )

    db.add(item)
    db.commit()
    db.refresh(item)

    owner = ListingOwnerDB(item_id=item.id, username=user.username)
    db.add(owner)
    db.commit()

    if fee == 0:
        return {
            "listing_id": item.id,
            "status": "active",
            "payment_required": False,
            "listing_type": item.listing_type,
            "fee_nok": fee,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            "is_featured": bool(item.is_featured),
        }

    order = PaymentOrderDB(
        item_id=item.id,
        buyer_username=user.username,
        provider=provider,
        amount=fee,
        currency="NOK",
        status="created",
        provider_reference=f"{provider}_listing_{uuid4()}",
        listing_type=item.listing_type,
        listing_duration_days=item.listing_duration_days,
        expires_at=item.expires_at,
        item_price=item.price,
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    return {
        "listing_id": item.id,
        "status": item.status,
        "payment_required": True,
        "payment_order_id": order.id,
        "payment_provider": provider,
        "fee_nok": fee,
        "listing_type": item.listing_type,
        "duration_days": item.listing_duration_days,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "next_step": "Complete payment and call /payments/orders/{order_id}/confirm to activate listing.",
    }


@app.delete("/items/{id}")
def delete(id: int, user: UserDB = Depends(get_user), db: Session = Depends(get_db)):
    item = db.query(ItemDB).get(id)
    db.delete(item)
    db.commit()
    return {"msg": "deleted"}


@app.post("/contact-seller")
def contact_seller(
    item_id: int = Form(...),
    message: str = Form(...),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Listing not found")

    owner = db.query(ListingOwnerDB).filter(ListingOwnerDB.item_id == item_id).first()
    if not owner:
        raise HTTPException(status_code=404, detail="Seller not found")

    if owner.username == user.username:
        raise HTTPException(status_code=400, detail="You cannot contact yourself")

    msg = ContactMessageDB(
        item_id=item_id,
        buyer_username=user.username,
        seller_username=owner.username,
        message=message.strip(),
        status="sent"
    )
    db.add(msg)
    db.commit()

    return {"msg": "Message sent", "seller": owner.username}


@app.post("/ai/assist")
def ai_assist(message: str = Form(...)):
    if not message.strip():
        raise HTTPException(status_code=400, detail="Melding er tom")

    if OPENAI_API_KEY:
        try:
            payload = json.dumps({
                "model": "gpt-4o-mini",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Du er en hjelpsom assistent for SELGO, en norsk markedsplass. "
                            "Hjelp brukere med å lage gode annonser, prissette varer, "
                            "skrive beskrivelser og gi råd om trygt kjøp og salg. "
                            "Svar alltid på norsk og hold svarene korte og konkrete."
                        )
                    },
                    {"role": "user", "content": message.strip()}
                ],
                "max_tokens": 400,
                "temperature": 0.7
            }).encode()

            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {OPENAI_API_KEY}"
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            return {"reply": data["choices"][0]["message"]["content"].strip()}
        except Exception:
            pass

    # Fallback: rule-based Norwegian assistant
    msg = message.lower()
    tips = []

    if any(w in msg for w in ["bil", "auto", "kjøretøy"]):
        tips.append("For bil: Legg til årsmodell, kilometerstand, servicehistorikk og EU-godkjent dato.")
    if any(w in msg for w in ["bolig", "leilighet", "hus", "hytte"]):
        tips.append("For bolig: Oppgi m², antall rom, beliggenhet og fellesutgifter.")
    if any(w in msg for w in ["klær", "sko", "jakke", "dress"]):
        tips.append("For klær: Legg til størrelse, merke, materiale og tilstand (ny/brukt).")
    if any(w in msg for w in ["sykkel", "elsykkel"]):
        tips.append("For sykkel: Oppgi merke, ramme størrelse, antall gir og eventuelle oppgraderinger.")
    if any(w in msg for w in ["pris", "verdi", "selge raskt", "fort"]):
        tips.append("Sjekk hva tilsvarende varer selges for på SELGO og sett en konkurransedyktig pris.")
    if any(w in msg for w in ["bilde", "foto", "bilder"]):
        tips.append("Ta bilder i dagslys mot nøytral bakgrunn. Vis eventuelle skader tydelig.")
    if any(w in msg for w in ["trygg", "svindel", "sikker", "betaling"]):
        tips.append("Møt på offentlig sted. Bruk Vipps eller Stripe for sikker betaling — unngå kontanter ved større beløp.")
    if any(w in msg for w in ["tittel", "overskrift"]):
        tips.append("God tittel: Merke + modell + år + nøkkelord, f.eks. «Trek Marlin 7 sykkel 2022 lite brukt».")
    if any(w in msg for w in ["beskrivelse", "tekst"]):
        tips.append("Beskriv: tilstand, brukstid, årsak til salg, eventuelle feil/mangler, og om levering er mulig.")

    if not tips:
        tips.append("Tips: Bruk tydelig tittel, gode bilder og full beskrivelse med tilstand og pris for raskere salg.")

    return {"reply": "\n\n".join(tips)}


@app.get("/payments/providers")
def payment_providers():
    return {
        "stripe_ready": bool(STRIPE_SECRET_KEY),
        "vipps_ready": bool(VIPPS_CLIENT_ID and VIPPS_CLIENT_SECRET and VIPPS_SUBSCRIPTION_KEY),
        "currency": "NOK"
    }


@app.get("/payments/orders/{order_id}")
def get_payment_order(
    order_id: int,
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.buyer_username != user.username:
        raise HTTPException(status_code=403, detail="Not allowed")

    return {
        "order_id": order.id,
        "listing_id": order.item_id,
        "provider": order.provider,
        "amount": order.amount,
        "currency": order.currency,
        "status": order.status,
        "listing_type": order.listing_type,
        "duration_days": order.listing_duration_days,
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
        "provider_reference": order.provider_reference,
    }


@app.post("/payments/orders/{order_id}/confirm")
def confirm_payment_order(
    order_id: int,
    provider_reference: str = Form(""),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.buyer_username != user.username:
        raise HTTPException(status_code=403, detail="Not allowed")
    if order.status == "paid":
        return {
            "order_id": order.id,
            "status": "paid",
            "listing_id": order.item_id,
            "listing_status": "active"
        }

    item = db.query(ItemDB).filter(ItemDB.id == order.item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Listing not found")

    order.status = "paid"
    if provider_reference:
        order.provider_reference = provider_reference

    item.status = "active"
    item.listing_price_paid = order.amount
    item.listing_duration_days = order.listing_duration_days or item.listing_duration_days
    item.expires_at = order.expires_at or item.expires_at
    item.listing_type = order.listing_type or item.listing_type

    db.commit()

    return {
        "order_id": order.id,
        "status": order.status,
        "listing_id": item.id,
        "listing_status": item.status,
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
    }


def _create_or_refresh_payment_order(db: Session, user: UserDB, item: ItemDB, provider: str):
    if item.listing_price_paid and item.listing_price_paid > 0:
        raise HTTPException(status_code=400, detail="Listing fee is already paid")
    if item.status not in {"pending_payment", "active"}:
        raise HTTPException(status_code=400, detail="Listing is not payable in current state")

    order = db.query(PaymentOrderDB).filter(
        PaymentOrderDB.item_id == item.id,
        PaymentOrderDB.buyer_username == user.username,
        PaymentOrderDB.provider == provider,
        PaymentOrderDB.status == "created"
    ).first()

    if not order:
        order = PaymentOrderDB(
            item_id=item.id,
            buyer_username=user.username,
            provider=provider,
            amount=item.listing_price_paid if item.listing_price_paid else 0,
            currency="NOK",
            status="created",
            provider_reference=f"{provider}_listing_{uuid4()}",
            listing_type=item.listing_type,
            listing_duration_days=item.listing_duration_days,
            expires_at=item.expires_at,
            item_price=item.price,
        )

    if order.amount <= 0:
        pricing = calculate_listing_pricing(
            item_price=item.price,
            category=item.category,
            listing_mode="sale" if item.listing_type == "real_estate_sale" else "rent" if item.listing_type == "real_estate_rent" else "",
            boost=bool(item.boost_selected),
        )
        order.amount = float(pricing["fee_nok"])
        order.listing_type = pricing["listing_type"]
        order.listing_duration_days = int(pricing["duration_days"])
        order.expires_at = item.expires_at

    if order.amount <= 0:
        raise HTTPException(status_code=400, detail="Denne annonsen krever ikke betaling")

    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@app.post("/payments/stripe/create-checkout-session")
def create_stripe_checkout_session(
    item_id: int = Form(...),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Listing not found")
    owner = db.query(ListingOwnerDB).filter(ListingOwnerDB.item_id == item_id).first()
    if not owner or owner.username != user.username:
        raise HTTPException(status_code=403, detail="Only listing owner can pay listing fee")

    order = _create_or_refresh_payment_order(db=db, user=user, item=item, provider="stripe")

    return {
        "order_id": order.id,
        "listing_id": item.id,
        "provider": "stripe",
        "status": "created",
        "amount": order.amount,
        "integration_ready": bool(STRIPE_SECRET_KEY),
        "next_step": "Set STRIPE_SECRET_KEY and wire Stripe Checkout Session API call, then confirm via /payments/orders/{order_id}/confirm."
    }


@app.post("/payments/vipps/create-order")
def create_vipps_order(
    item_id: int = Form(...),
    user: UserDB = Depends(get_user),
    db: Session = Depends(get_db)
):
    item = db.query(ItemDB).filter(ItemDB.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Listing not found")
    owner = db.query(ListingOwnerDB).filter(ListingOwnerDB.item_id == item_id).first()
    if not owner or owner.username != user.username:
        raise HTTPException(status_code=403, detail="Only listing owner can pay listing fee")

    order = _create_or_refresh_payment_order(db=db, user=user, item=item, provider="vipps")

    return {
        "order_id": order.id,
        "listing_id": item.id,
        "provider": "vipps",
        "status": "created",
        "amount": order.amount,
        "integration_ready": bool(VIPPS_CLIENT_ID and VIPPS_CLIENT_SECRET and VIPPS_SUBSCRIPTION_KEY),
        "next_step": "Set VIPPS_CLIENT_ID, VIPPS_CLIENT_SECRET and VIPPS_SUBSCRIPTION_KEY, then call Vipps ePayment create-payment API and confirm via /payments/orders/{order_id}/confirm."
    }