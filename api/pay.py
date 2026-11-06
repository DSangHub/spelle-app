"""POST /api/pay — create a Razorpay order for a profile.
POST /api/pay/verify — confirm the checkout signature and mark the profile paid.
POST /api/webhook — Razorpay webhook (raw body signature).

Env: RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET, DATABASE_URL.
Amount is in the smallest unit (paise for INR, cents for USD).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
CURRENCY = os.environ.get("RAZORPAY_CURRENCY", "USD")
# 499 cents = $4.99, or 49900 paise = ₹499. Set RAZORPAY_AMOUNT to change.
AMOUNT = int(os.environ.get("RAZORPAY_AMOUNT", "499"))


def db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    import psycopg
    return psycopg.connect(url, prepare_threshold=None)


def create_order(email: str) -> dict:
    if not KEY_ID or not KEY_SECRET:
        raise RuntimeError("Razorpay keys are not set.")
    payload = {
        "amount": AMOUNT,
        "currency": CURRENCY,
        "receipt": email[:40],
        "notes": {"email": email, "app": "spelle"},
    }
    token = base64.b64encode(f"{KEY_ID}:{KEY_SECRET}".encode()).decode()
    req = urllib.request.Request(
        "https://api.razorpay.com/v1/orders",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:240]
        raise RuntimeError(f"Razorpay {exc.code}. {detail}") from exc


def verify_payment(order_id: str, payment_id: str, signature: str) -> bool:
    msg = f"{order_id}|{payment_id}".encode()
    expected = hmac.new(KEY_SECRET.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def mark_paid(email: str, payment_id: str) -> None:
    with db() as conn:
        conn.execute(
            """
            update profiles
               set plan = 'paid',
                   paid_until = now() + interval '30 days'
             where email = %s
            """,
            (email,),
        )
        conn.commit()


class handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(min(length, 20000))

        if path.endswith("/webhook"):
            sig = self.headers.get("X-Razorpay-Signature", "")
            expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            if not WEBHOOK_SECRET or not hmac.compare_digest(expected, sig):
                self._json(400, {"error": "Bad signature."})
                return
            event = json.loads(raw.decode() or "{}")
            if event.get("event") == "order.paid":
                notes = event.get("payload", {}).get("order", {}).get("entity", {}).get("notes") or {}
                email = notes.get("email")
                if email:
                    try:
                        mark_paid(email, event.get("payload", {}).get("payment", {}).get("entity", {}).get("id", ""))
                    except RuntimeError:
                        pass
            self._json(200, {"ok": True})
            return

        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Could not read that."})
            return

        if path.endswith("/verify"):
            order_id = str(body.get("razorpay_order_id") or "")
            payment_id = str(body.get("razorpay_payment_id") or "")
            signature = str(body.get("razorpay_signature") or "")
            email = str(body.get("email") or "").strip().lower()
            if not verify_payment(order_id, payment_id, signature):
                self._json(400, {"error": "Payment signature did not match."})
                return
            try:
                mark_paid(email, payment_id)
            except RuntimeError as exc:
                self._json(503, {"error": str(exc)})
                return
            self._json(200, {"ok": True, "plan": "paid"})
            return

        email = str(body.get("email") or "").strip().lower()
        if not email:
            self._json(400, {"error": "Need an email."})
            return
        try:
            order = create_order(email)
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)})
            return
        self._json(200, {
            "order_id": order.get("id"),
            "amount": order.get("amount"),
            "currency": order.get("currency"),
            "key_id": KEY_ID,
        })
