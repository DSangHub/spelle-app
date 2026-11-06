"""Spelle API: translate, profiles, Razorpay.

Entrypoint: api.app:app
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import urllib.error
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

MODEL = os.environ.get("SPELLE_MODEL", "grok-4-fast-non-reasoning")
API_URL = os.environ.get("XAI_API_URL", "https://api.x.ai/v1/chat/completions")
MAX_CHARS = int(os.environ.get("SPELLE_MAX_CHARS", "2000"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "").strip()
KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "").strip()
CURRENCY = os.environ.get("RAZORPAY_CURRENCY", "USD")
AMOUNT = int(os.environ.get("RAZORPAY_AMOUNT", "499"))


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
          razorpay_customer text,
          created_at timestamptz default now()
        )
        """
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
    return {"ok": True, "grok": bool(os.environ.get("XAI_API_KEY")), "model": MODEL}


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


def mark_paid(email: str) -> None:
    with db() as conn:
        ensure(conn)
        conn.execute(
            "update profiles set plan = 'paid', paid_until = now() + interval '30 days' where email = %s",
            (email,),
        )
        conn.commit()


@app.post("/api/pay")
async def pay(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Could not read that."}, status_code=400)
    email = str(body.get("email") or "").strip().lower()
    if not email:
        return JSONResponse({"error": "Need an email."}, status_code=400)
    try:
        order = create_order(email)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {
        "order_id": order.get("id"),
        "amount": order.get("amount"),
        "currency": order.get("currency"),
        "key_id": KEY_ID,
    }


@app.post("/api/pay/verify")
async def verify(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Could not read that."}, status_code=400)
    order_id = str(body.get("razorpay_order_id") or "")
    payment_id = str(body.get("razorpay_payment_id") or "")
    signature = str(body.get("razorpay_signature") or "")
    email = str(body.get("email") or "").strip().lower()
    expected = hmac.new(KEY_SECRET.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
    if not KEY_SECRET or not hmac.compare_digest(expected, signature):
        return JSONResponse({"error": "Payment signature did not match."}, status_code=400)
    try:
        mark_paid(email)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return {"ok": True, "plan": "paid"}


@app.post("/api/webhook")
async def webhook(request: Request):
    raw = await request.body()
    sig = request.headers.get("x-razorpay-signature", "")
    expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not WEBHOOK_SECRET or not hmac.compare_digest(expected, sig):
        return JSONResponse({"error": "Bad signature."}, status_code=400)
    event = json.loads(raw.decode() or "{}")
    if event.get("event") == "order.paid":
        notes = event.get("payload", {}).get("order", {}).get("entity", {}).get("notes") or {}
        email = notes.get("email")
        if email:
            try:
                mark_paid(email)
            except RuntimeError:
                pass
    return {"ok": True}
