import base64
import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from django.conf import settings


RAZORPAY_API_URL = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    pass


def _api_request(path, payload=None):
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RazorpayError("Razorpay is not configured.")

    credentials = f"{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}"
    authorization = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{RAZORPAY_API_URL}/{path}",
        data=data,
        headers={
            "Authorization": f"Basic {authorization}",
            "Content-Type": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        # Razorpay's JSON error body contains the useful reason (for example,
        # authentication failure or an invalid order parameter). Keep it in
        # server logs so a failed checkout can be diagnosed without exposing
        # credentials or raw gateway details to the customer.
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            error = error_body.get("error", {}) if isinstance(error_body, dict) else {}
            code = str(error.get("code") or "")[:100]
            description = str(error.get("description") or "")[:500]
        except (UnicodeDecodeError, json.JSONDecodeError):
            code, description = "", ""
        detail = ": ".join(part for part in (code, description) if part)
        suffix = f" ({detail})" if detail else ""
        raise RazorpayError(f"Razorpay returned HTTP {exc.code}{suffix}.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RazorpayError("Could not reach Razorpay.") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RazorpayError("Razorpay returned an invalid response.") from exc

    if not isinstance(result, dict):
        raise RazorpayError("Razorpay returned an invalid response.")
    return result


def create_order(amount_paise, receipt):
    result = _api_request(
        "orders",
        {
            "amount": amount_paise,
            "currency": "INR",
            "receipt": receipt,
        },
    )
    if (
        not isinstance(result.get("id"), str)
        or result.get("amount") != amount_paise
        or result.get("currency") != "INR"
    ):
        raise RazorpayError("Razorpay returned an order with unexpected details.")
    return result


def fetch_payment(payment_id):
    return _api_request(f"payments/{quote(payment_id, safe='')}")


def fetch_order_payments(order_id):
    return _api_request(f"orders/{quote(order_id, safe='')}/payments")


def verify_signature(order_id, payment_id, signature):
    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected_signature = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature)
