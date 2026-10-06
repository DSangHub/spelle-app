"""Vercel function: POST /api/spelle

Set XAI_API_KEY in the Vercel project environment.
Optional: SPELLE_MODEL (default grok-4-fast-non-reasoning).
The comment body is not stored.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler

MODEL = os.environ.get("SPELLE_MODEL", "grok-4-fast-non-reasoning")
API_URL = os.environ.get("XAI_API_URL", "https://api.x.ai/v1/chat/completions")
MAX_CHARS = int(os.environ.get("SPELLE_MAX_CHARS", "2000"))


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
            body = json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:240]
        raise RuntimeError(f"Grok returned {exc.code}. {detail}") from exc
    try:
        return body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("Grok returned an empty reply.") from exc


def translate(text: str, source: str, target: str, mode: str) -> str:
    if mode == "reply":
        system = (
            "You prepare a social-media reply for paste-back. "
            f"Translate from {source} into {target}. "
            "Then spell-check that translation in the target language. "
            "Keep the reply the same length and tone. Do not add greetings, hashtags, or explanations. "
            "Keep names, @handles, dates, and numbers unchanged. "
            "Return only the finished reply."
        )
    else:
        system = (
            "You translate a social-media comment so the reader can understand it. "
            f"Translate from {source} into {target}. "
            "Keep names, @handles, dates, and numbers unchanged. "
            "Do not answer the comment. Return only the translation."
        )
    return grok(system, text)


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
        self._json(200, {"ok": True, "grok": bool(os.environ.get("XAI_API_KEY")), "model": MODEL})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            incoming = json.loads(self.rfile.read(min(length, 20000)).decode() or "{}")
        except (ValueError, json.JSONDecodeError):
            self._json(400, {"error": "Could not read that paste."})
            return
        text = str(incoming.get("text") or "").strip()
        source = str(incoming.get("from") or "auto")[:16]
        target = str(incoming.get("to") or "en")[:16]
        mode = "reply" if incoming.get("mode") == "reply" else "read"
        if not text:
            self._json(400, {"error": "Nothing to translate."})
            return
        if len(text) > MAX_CHARS:
            self._json(400, {"error": f"Keep it under {MAX_CHARS} characters."})
            return
        if not os.environ.get("XAI_API_KEY"):
            self._json(503, {"error": "Grok key is not set on this server yet."})
            return
        try:
            out = translate(text, source, target, mode)
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, {"text": out, "engine": "grok", "mode": mode})
