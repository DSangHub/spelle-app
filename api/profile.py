"""POST /api/profile — create or update a Spelle profile.
GET  /api/profile?email= — read a profile.

Requires DATABASE_URL (Postgres). Paid status is set by the Razorpay webhook.
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def db():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not set.")
    import psycopg
    return psycopg.connect(url)


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


def upsert(email: str, name: str, language: str) -> dict:
    with db() as conn:
        ensure(conn)
        conn.execute(
            """
            insert into profiles (email, name, language)
            values (%s, %s, %s)
            on conflict (email) do update
              set name = excluded.name,
                  language = excluded.language
            """,
            (email, name, language),
        )
        row = conn.execute(
            "select email, name, language, plan, paid_until from profiles where email = %s",
            (email,),
        ).fetchone()
        conn.commit()
    return {
        "email": row[0],
        "name": row[1],
        "language": row[2],
        "plan": row[3],
        "paid_until": row[4].isoformat() if row[4] else None,
    }


def fetch(email: str) -> dict | None:
    with db() as conn:
        ensure(conn)
        row = conn.execute(
            "select email, name, language, plan, paid_until from profiles where email = %s",
            (email,),
        ).fetchone()
    if not row:
        return None
    return {
        "email": row[0],
        "name": row[1],
        "language": row[2],
        "plan": row[3],
        "paid_until": row[4].isoformat() if row[4] else None,
    }


class handler(BaseHTTPRequestHandler):
    def _json(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        from urllib.parse import parse_qs, urlparse
        email = (parse_qs(urlparse(self.path).query).get("email") or [""])[0].strip().lower()
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "Need a valid email."})
            return
        try:
            profile = fetch(email)
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)})
            return
        if not profile:
            self._json(404, {"error": "No profile yet."})
            return
        self._json(200, profile)

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(length, 8000)).decode() or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Could not read that."})
            return
        email = str(body.get("email") or "").strip().lower()
        name = str(body.get("name") or "").strip()[:80]
        language = str(body.get("language") or "en")[:16]
        if not EMAIL_RE.match(email):
            self._json(400, {"error": "Need a valid email."})
            return
        try:
            profile = upsert(email, name, language)
        except RuntimeError as exc:
            self._json(503, {"error": str(exc)})
            return
        self._json(200, profile)
