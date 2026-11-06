"""POST /api/webhook — Razorpay webhook. Verifies the raw-body signature.

Set the webhook URL in the Razorpay dashboard to https://spelle.vercel.app/api/webhook
Env: RAZORPAY_WEBHOOK_SECRET, DATABASE_URL
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler

WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()


def mark_paid(email: str) -> None:
    import psycopg
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return
    with psycopg.connect(url, prepare_threshold=None) as conn:
        conn.execute(
            "update profiles set plan = 'paid', paid_until = now() + interval '30 days' where email = %s",
            (email,),
        )
        conn.commit()


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(min(length, 50000))
        sig = self.headers.get("X-Razorpay-Signature", "")
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not WEBHOOK_SECRET or not hmac.compare_digest(expected, sig):
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error":"Bad signature."}')
            return
        event = json.loads(raw.decode() or "{}")
        if event.get("event") == "order.paid":
            notes = event.get("payload", {}).get("order", {}).get("entity", {}).get("notes") or {}
            email = notes.get("email")
            if email:
                mark_paid(email)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')
