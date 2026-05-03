import base64
import hashlib
import hmac
import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from fastapi import HTTPException


class VippsServiceError(HTTPException):
    def __init__(self, status_code: int, detail: str):
        super().__init__(status_code=status_code, detail=detail)


def _env_name() -> str:
    return (os.getenv("VIPPS_ENV", "sandbox") or "sandbox").strip().lower()


def _api_base() -> str:
    configured = (os.getenv("VIPPS_EPAYMENT_API_BASE", "") or "").strip()
    if configured:
        return configured
    return "https://api.vipps.no" if _env_name() == "production" else "https://apitest.vipps.no"


def _discovery_url() -> str:
    configured = (os.getenv("VIPPS_LOGIN_DISCOVERY_URL", "") or "").strip()
    if configured:
        return configured
    if _env_name() == "production":
        return "https://api.vipps.no/access-management-1.0/access/.well-known/openid-configuration"
    return "https://apitest.vipps.no/access-management-1.0/access/.well-known/openid-configuration"


def _require_https(url: str, env_key: str) -> None:
    if not url.lower().startswith("https://"):
        raise VippsServiceError(503, f"{env_key} må bruke HTTPS")


def _require_config() -> None:
    if not os.getenv("VIPPS_CLIENT_ID") or not os.getenv("VIPPS_CLIENT_SECRET"):
        raise VippsServiceError(503, "Vipps er ikke konfigurert")
    if not os.getenv("VIPPS_MERCHANT_SERIAL_NUMBER"):
        raise VippsServiceError(503, "VIPPS_MERCHANT_SERIAL_NUMBER mangler")

    _require_https(_api_base(), "VIPPS_EPAYMENT_API_BASE")
    _require_https(_discovery_url(), "VIPPS_LOGIN_DISCOVERY_URL")


def _basic_auth_header() -> str:
    client_id = os.getenv("VIPPS_CLIENT_ID", "")
    client_secret = os.getenv("VIPPS_CLIENT_SECRET", "")
    value = f"{client_id}:{client_secret}".encode("utf-8")
    return "Basic " + base64.b64encode(value).decode("ascii")


def _http_json_request(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
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
        raise VippsServiceError(e.code, f"Vipps API-feil: {detail}")
    except urllib.error.URLError as e:
        raise VippsServiceError(502, f"Nettverksfeil mot Vipps: {e.reason}")


def _discovery() -> Dict[str, Any]:
    return _http_json_request(_discovery_url())


def _epayment_headers(access_token: str, idempotency_key: Optional[str] = None) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Merchant-Serial-Number": os.getenv("VIPPS_MERCHANT_SERIAL_NUMBER", ""),
        "Vipps-System-Name": os.getenv("VIPPS_SYSTEM_NAME", "selga-backend"),
        "Vipps-System-Version": os.getenv("VIPPS_SYSTEM_VERSION", "1.0.0"),
        "Vipps-System-Plugin-Name": os.getenv("VIPPS_SYSTEM_PLUGIN_NAME", "selga"),
        "Vipps-System-Plugin-Version": os.getenv("VIPPS_SYSTEM_PLUGIN_VERSION", "1.0.0"),
    }
    subscription_key = (os.getenv("VIPPS_SUBSCRIPTION_KEY", "") or "").strip()
    if subscription_key:
        headers["Ocp-Apim-Subscription-Key"] = subscription_key
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def getAccessToken(scope: str = "ePayment") -> str:
    _require_config()
    discovery = _discovery()
    token_endpoint = discovery.get("token_endpoint")
    if not token_endpoint:
        raise VippsServiceError(502, "Mangler token_endpoint i Vipps discovery")

    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": scope,
        }
    ).encode("utf-8")

    token_data = _http_json_request(
        token_endpoint,
        method="POST",
        headers={
            "Authorization": _basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=body,
    )
    access_token = token_data.get("access_token")
    if not access_token:
        raise VippsServiceError(502, "Fikk ikke access token fra Vipps")
    return access_token


def createPayment(
    reference: str,
    amount_ore: int,
    return_url: str,
    phone_number: Optional[str] = None,
) -> Dict[str, Any]:
    _require_config()

    if amount_ore <= 0:
        raise VippsServiceError(400, "Ugyldig beløp")
    if not reference or len(reference) > 120:
        raise VippsServiceError(400, "Ugyldig referanse")
    _require_https(return_url, "return_url")

    payload: Dict[str, Any] = {
        "amount": {"currency": "NOK", "value": amount_ore},
        "paymentMethod": {"type": "WALLET"},
        "reference": reference,
        "returnUrl": return_url,
        "userFlow": "WEB_REDIRECT",
    }
    if phone_number:
        payload["customer"] = {"phoneNumber": phone_number}

    access_token = getAccessToken()
    create_url = _api_base().rstrip("/") + "/epayment/v1/payments"
    response_payload = _http_json_request(
        create_url,
        method="POST",
        headers=_epayment_headers(access_token, idempotency_key=secrets.token_urlsafe(24)),
        body=json.dumps(payload).encode("utf-8"),
    )

    checkout_url = (
        response_payload.get("redirectUrl")
        or response_payload.get("url")
        or response_payload.get("hostedPaymentPageUrl")
        or response_payload.get("links", {}).get("redirect", {}).get("href")
    )
    if not checkout_url:
        raise VippsServiceError(502, "Vipps returnerte ikke checkout-url")

    return {
        "checkout_url": checkout_url,
        "raw": response_payload,
    }


def getPaymentStatus(reference: str) -> Dict[str, Any]:
    _require_config()
    if not reference:
        raise VippsServiceError(400, "Mangler referanse")

    access_token = getAccessToken()
    status_url = _api_base().rstrip("/") + f"/epayment/v1/payments/{urllib.parse.quote(str(reference))}"
    payload = _http_json_request(
        status_url,
        method="GET",
        headers=_epayment_headers(access_token),
    )

    state = (
        str(payload.get("state") or "")
        or str(payload.get("status") or "")
        or str(payload.get("transactionInfo", {}).get("status") or "")
        or str(payload.get("aggregate", {}).get("state") or "")
    ).upper()

    return {
        "state": state,
        "raw": payload,
    }


def verifyWebhookSignature(raw_body: bytes, headers: Dict[str, str]) -> bool:
    webhook_secret = (os.getenv("VIPPS_WEBHOOK_SECRET", "") or "").strip()
    if not webhook_secret:
        return True

    signature = (
        headers.get("x-vipps-signature")
        or headers.get("x-signature")
        or headers.get("vipps-signature")
        or ""
    ).strip()
    if not signature:
        return False

    expected = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def isPaidState(state: str) -> bool:
    return (state or "").upper() in {"AUTHORIZED", "CAPTURED", "SUCCESS", "COMPLETED"}


def isFailedState(state: str) -> bool:
    return (state or "").upper() in {"FAILED", "CANCELLED", "REJECTED", "ABORTED", "TERMINATED"}


def isTimeoutState(state: str) -> bool:
    return (state or "").upper() in {"EXPIRED", "TIMED_OUT", "TIMEOUT"}
