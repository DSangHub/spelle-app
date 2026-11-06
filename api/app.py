"""Spelle API: translate, profiles, Stripe Checkout.

Entrypoint: api.app:app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
PUBLIC = Path(__file__).resolve().parent.parent / "public"
if PUBLIC.exists():
    app.mount("/assets", StaticFiles(directory=PUBLIC), name="assets")


@app.get("/")
def home():
    index = PUBLIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"ok": True, "app": "spelle"}


@app.get("/logo.jpg")
@app.get("/assets/logo.jpg")
def logo():
    return FileResponse(PUBLIC / "logo.jpg")


@app.get("/mark.jpg")
@app.get("/assets/mark.jpg")
def mark():
    return FileResponse(PUBLIC / "mark.jpg")

MODEL = os.environ.get("SPELLE_MODEL", "grok-4-fast-non-reasoning")
API_URL = os.environ.get("XAI_API_URL", "https://api.x.ai/v1/chat/completions")
MAX_CHARS = int(os.environ.get("SPELLE_MAX_CHARS", "2000"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STRIPE_SECRET = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
SITE = os.environ.get("SPELLE_SITE", "https://spelle.app").rstrip("/")
CURRENCY = os.environ.get("STRIPE_CURRENCY", "usd")
AMOUNT = int(os.environ.get("STRIPE_AMOUNT", "499"))


def db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    import psycopg
    return psycopg.connect(url, prepare_threshold=None)


def ensure(conn) -> None:
    conn.execute(
        """
        create table if not exists profiles (
          email text primary key,
          name text,
          language text,
          plan text default 'free',
          paid_until timestamptz,
          stripe_customer text,
          created_at timestamptz default now()
        )
        """
    )
    conn.execute(
        "alter table profiles add column if not exists stripe_customer text"
    )
    conn.commit()


def grok(system: str, user: str) -> str:
    key = os.environ.get("XAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("XAI_API_KEY is not set.")
    payload = {
        "model": MODEL,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:240]
        raise RuntimeError(f"Grok {exc.code}. {detail}") from exc
    return data["choices"][0]["message"]["content"].strip()


@app.get("/api/spelle")
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "grok": bool(os.environ.get("XAI_API_KEY")),
        "stripe": bool(STRIPE_SECRET),
        "model": MODEL,
    }


@app.post("/api/spelle")
async def translate(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Could not read that."}, status_code=400)
    text = str(body.get("text") or "")[:MAX_CHARS]
    src = str(body.get("from") or "auto")[:16]
    dst = str(body.get("to") or "en")[:16]
    mode = str(body.get("mode") or "read")
    if not text.strip():
        return JSONResponse({"error": "Nothing to translate."}, status_code=400)
    system = (
        "You polish a short social reply. Keep names, tone, and length. "
        "Return only the finished reply."
        if mode == "ready"
        else "You translate a short social comment. Keep the tone. Return only the translation."
    )
    user = f"From {src} to {dst}.\n\n{text}"
    try:
        out = grok(system, user)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"text": out}


@app.get("/api/profile")
def get_profile(email: str = ""):
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "Need a valid email."}, status_code=400)
    try:
        with db() as conn:
            ensure(conn)
            row = conn.execute(
                "select email, name, language, plan, paid_until from profiles where email = %s",
                (email,),
            ).fetchone()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    if not row:
        return JSONResponse({"error": "No profile yet."}, status_code=404)
    return {
        "email": row[0],
        "name": row[1],
        "language": row[2],
        "plan": row[3],
        "paid_until": row[4].isoformat() if row[4] else None,
    }


@app.post("/api/profile")
async def save_profile(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Could not read that."}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    name = str(body.get("name") or "").strip()[:80]
    language = str(body.get("language") or "en")[:16]
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "Need a valid email."}, status_code=400)
    try:
        with db() as conn:
            ensure(conn)
            conn.execute(
                """
                insert into profiles (email, name, language)
                values (%s, %s, %s)
                on conflict (email) do update
                  set name = excluded.name, language = excluded.language
                """,
                (email, name, language),
            )
            row = conn.execute(
                "select email, name, language, plan, paid_until from profiles where email = %s",
                (email,),
            ).fetchone()
            conn.commit()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {
        "email": row[0],
        "name": row[1],
        "language": row[2],
        "plan": row[3],
        "paid_until": row[4].isoformat() if row[4] else None,
    }


def stripe_form(fields: dict) -> dict:
    if not STRIPE_SECRET:
        raise RuntimeError("Stripe key is not set.")
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=body,
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:240]
        raise RuntimeError(f"Stripe {exc.code}. {detail}") from exc


def mark_paid(email: str, customer: str = "") -> None:
    with db() as conn:
        ensure(conn)
        conn.execute(
            """
            update profiles
               set plan = 'paid',
                   paid_until = now() + interval '30 days',
                   stripe_customer = coalesce(nullif(%s, ''), stripe_customer)
             where email = %s
            """,
            (customer or "", email),
        )
        conn.commit()


def verify_stripe(raw: bytes, header: str) -> dict:
    parts = {}
    for item in (header or "").split(","):
        key, _, value = item.partition("=")
        parts.setdefault(key.strip(), []).append(value.strip())
    timestamp = (parts.get("t") or [""])[0]
    signatures = parts.get("v1") or []
    if not timestamp or not signatures or not STRIPE_WEBHOOK:
        raise RuntimeError("Bad signature.")
    if abs(time.time() - int(timestamp)) > 300:
        raise RuntimeError("Webhook is too old.")
    signed = f"{timestamp}.".encode() + raw
    expected = hmac.new(STRIPE_WEBHOOK.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise RuntimeError("Bad signature.")
    return json.loads(raw.decode() or "{}")


@app.post("/api/pay")
async def pay(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Could not read that."}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "Need a valid email."}, status_code=400)
    try:
        session = stripe_form({
            "mode": "payment",
            "customer_email": email,
            "success_url": f"{SITE}/?paid=1",
            "cancel_url": f"{SITE}/?paid=0",
            "metadata[email]": email,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": CURRENCY,
            "line_items[0][price_data][unit_amount]": str(AMOUNT),
            "line_items[0][price_data][product_data][name]": "Spelle",
        })
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {
        "url": session.get("url"),
        "session_id": session.get("id"),
        "amount": AMOUNT,
        "currency": CURRENCY,
    }


@app.post("/api/webhook")
async def webhook(request: Request):
    raw = await request.body()
    try:
        event = verify_stripe(raw, request.headers.get("stripe-signature", ""))
    except (RuntimeError, ValueError):
        return JSONResponse({"error": "Bad signature."}, status_code=400)
    if event.get("type") == "checkout.session.completed":
        session = event.get("data", {}).get("object") or {}
        email = (session.get("metadata") or {}).get("email") or session.get("customer_email") or ""
        if email:
            try:
                mark_paid(email.lower(), session.get("customer") or "")
            except RuntimeError:
                pass
    return {"ok": True}
