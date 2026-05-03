import json
import secrets
import urllib.parse
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from sqlalchemy.orm import Session

from database import get_db
from models import ItemDB, ListingOwnerDB, PaymentOrderDB, UserDB
from services.vippsService import (
    VippsServiceError,
    createPayment,
    getPaymentStatus,
    isFailedState,
    isPaidState,
    isTimeoutState,
    verifyWebhookSignature,
)


def build_vipps_router(
    oauth_scheme: OAuth2PasswordBearer,
    secret: str,
    algo: str,
    app_base_url: str,
) -> APIRouter:
    router = APIRouter(prefix="/vipps", tags=["vipps"])

    def _mark_order_paid(order: PaymentOrderDB, db: Session) -> None:
        order.status = "paid"
        item = db.query(ItemDB).filter(ItemDB.id == order.item_id).first()
        if item:
            item.status = "active"
            item.listing_price_paid = float(order.amount or 0)
        db.commit()

    def _get_user(token: str = Depends(oauth_scheme), db: Session = Depends(get_db)) -> UserDB:
        try:
            data = jwt.decode(token, secret, algorithms=[algo])
        except Exception:
            raise HTTPException(401, "Ugyldig token")
        user = db.query(UserDB).filter(UserDB.username == data.get("sub")).first()
        if not user:
            raise HTTPException(401, "Ikke innlogget")
        return user

    def _resolve_status_response(state: str) -> dict:
        if isPaidState(state):
            return {"status": "paid", "state": state}
        if isFailedState(state):
            return {"status": "failed", "state": state}
        if isTimeoutState(state):
            return {"status": "timeout", "state": state}
        return {"status": "pending", "state": state}

    @router.post("/create-order")
    def vipps_create_order(
        order_id: int = Form(...),
        phone_number: Optional[str] = Form(None),
        reference: Optional[str] = Form(None),
        return_url: Optional[str] = Form(None),
        user: UserDB = Depends(_get_user),
        db: Session = Depends(get_db),
    ):
        order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
        if not order:
            raise HTTPException(404, "Betalingsordre ikke funnet")
        if order.buyer_username != user.username:
            raise HTTPException(403, "Ingen tilgang til betalingsordren")
        if order.status not in {"created", "initiated", "pending"}:
            raise HTTPException(409, "Betalingsordre kan ikke startes på nytt")

        amount_ore = int(round(float(order.amount or 0) * 100))
        vipps_reference = (reference or "").strip() or f"SELGA-{order.id}-{secrets.token_hex(4)}"
        checkout_return = (
            return_url
            or f"{app_base_url.rstrip('/')}/vipps/return?order_id={order.id}&reference={urllib.parse.quote(vipps_reference)}"
        )

        result = createPayment(
            reference=vipps_reference,
            amount_ore=amount_ore,
            return_url=checkout_return,
            phone_number=phone_number,
        )

        order.provider = "vipps"
        order.provider_reference = vipps_reference
        order.status = "initiated"
        db.commit()

        return {
            "order_id": order.id,
            "reference": vipps_reference,
            "checkout_url": result["checkout_url"],
        }

    @router.get("/verify-payment")
    def verify_payment(
        order_id: int,
        reference: Optional[str] = None,
        user: UserDB = Depends(_get_user),
        db: Session = Depends(get_db),
    ):
        order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
        if not order:
            raise HTTPException(404, "Betalingsordre ikke funnet")
        if order.buyer_username != user.username:
            raise HTTPException(403, "Ingen tilgang til betalingsordren")

        ref = (reference or order.provider_reference or "").strip()
        if not ref:
            raise HTTPException(400, "Mangler Vipps-referanse")

        status_data = getPaymentStatus(ref)
        state = status_data["state"]

        if isPaidState(state):
            if order.status != "paid":
                _mark_order_paid(order, db)
            return _resolve_status_response(state)

        if isFailedState(state):
            order.status = "failed"
            db.commit()
            raise HTTPException(409, "Betaling feilet eller ble avbrutt")

        if isTimeoutState(state):
            order.status = "timeout"
            db.commit()
            raise HTTPException(408, "Betaling utløpt (timeout)")

        order.status = "pending"
        db.commit()
        raise HTTPException(409, "Betaling er ikke fullført ennå")

    @router.get("/return")
    def vipps_return(order_id: int, reference: Optional[str] = None, db: Session = Depends(get_db)):
        order = db.query(PaymentOrderDB).filter(PaymentOrderDB.id == order_id).first()
        if not order:
            return RedirectResponse(f"{app_base_url}/?payment=failed&provider=vipps&reason=order_not_found")

        ref = (reference or order.provider_reference or "").strip()
        if not ref:
            return RedirectResponse(f"{app_base_url}/?payment=failed&provider=vipps&order_id={order_id}&reason=missing_reference")

        try:
            status_data = getPaymentStatus(ref)
            state = status_data["state"]
        except VippsServiceError:
            return RedirectResponse(f"{app_base_url}/?payment=pending&provider=vipps&order_id={order_id}&reference={urllib.parse.quote(ref)}")

        if isPaidState(state):
            if order.status != "paid":
                _mark_order_paid(order, db)
            return RedirectResponse(f"{app_base_url}/?payment=success&provider=vipps&order_id={order_id}&reference={urllib.parse.quote(ref)}")

        if isFailedState(state):
            order.status = "failed"
            db.commit()
            return RedirectResponse(f"{app_base_url}/?payment=failed&provider=vipps&order_id={order_id}&reference={urllib.parse.quote(ref)}")

        if isTimeoutState(state):
            order.status = "timeout"
            db.commit()
            return RedirectResponse(f"{app_base_url}/?payment=timeout&provider=vipps&order_id={order_id}&reference={urllib.parse.quote(ref)}")

        order.status = "pending"
        db.commit()
        return RedirectResponse(f"{app_base_url}/?payment=pending&provider=vipps&order_id={order_id}&reference={urllib.parse.quote(ref)}")

    @router.post("/webhook")
    async def vipps_webhook(request: Request, db: Session = Depends(get_db)):
        raw_body = await request.body()
        headers = {k.lower(): v for k, v in request.headers.items()}
        if not verifyWebhookSignature(raw_body, headers):
            raise HTTPException(401, "Ugyldig webhook-signatur")

        try:
            payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        except Exception:
            raise HTTPException(400, "Ugyldig webhook-payload")

        data = payload.get("data") if isinstance(payload, dict) else None
        source = data if isinstance(data, dict) else (payload if isinstance(payload, dict) else {})

        reference = (
            source.get("reference")
            or source.get("paymentReference")
            or payload.get("reference")
        )
        if not reference:
            return {"ok": True, "ignored": True, "reason": "missing_reference"}

        order = db.query(PaymentOrderDB).filter(PaymentOrderDB.provider_reference == str(reference)).first()
        if not order:
            return {"ok": True, "ignored": True, "reason": "order_not_found"}

        try:
            status_data = getPaymentStatus(str(reference))
        except VippsServiceError as exc:
            raise HTTPException(exc.status_code, exc.detail)

        state = status_data["state"]

        if isPaidState(state):
            if order.status != "paid":
                _mark_order_paid(order, db)
            return {"ok": True, "status": "paid", "state": state}

        if isFailedState(state):
            order.status = "failed"
            db.commit()
            return {"ok": True, "status": "failed", "state": state}

        if isTimeoutState(state):
            order.status = "timeout"
            db.commit()
            return {"ok": True, "status": "timeout", "state": state}

        order.status = "pending"
        db.commit()
        return {"ok": True, "status": "pending", "state": state}

    return router
