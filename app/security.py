"""Passwort-Hashing (scrypt/PBKDF2), CSRF-Schutz und Login-Drosselung."""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_N, _R, _P = 2 ** 14, 8, 1
_PBKDF2_ROUNDS = 600_000
_HAS_SCRYPT = hasattr(hashlib, "scrypt")  # fehlt z. B. bei Python mit LibreSSL (macOS)


def _b64(b):
    return base64.b64encode(b).decode()


def hash_password(password):
    salt = secrets.token_bytes(16)
    if _HAS_SCRYPT:
        dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
        return "scrypt${}${}${}${}${}".format(_N, _R, _P, _b64(salt), _b64(dk))
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return "pbkdf2${}${}${}".format(_PBKDF2_ROUNDS, _b64(salt), _b64(dk))


def verify_password(password, stored):
    try:
        parts = stored.split("$")
        if parts[0] == "scrypt":
            n, r, p, salt, dk = parts[1:]
            expected = base64.b64decode(dk)
            got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                                 n=int(n), r=int(r), p=int(p), dklen=len(expected))
        elif parts[0] == "pbkdf2":
            rounds, salt, dk = parts[1:]
            expected = base64.b64decode(dk)
            got = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt), int(rounds))
        else:
            return False
        return hmac.compare_digest(got, expected)
    except (ValueError, TypeError, AttributeError):
        return False


def csrf_token(request: Request):
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


def require_csrf(request: Request, token: str = ""):
    token = token or request.headers.get("x-csrf-token", "")
    expected = request.session.get("csrf", "")
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(400, "Ungültiges Formular-Token. Bitte Seite neu laden.")


class RateLimiter:
    """Einfache In-Memory-Drosselung pro Schlüssel (z. B. IP-Adresse)."""

    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = defaultdict(deque)

    def allow(self, key):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


login_limiter = RateLimiter(limit=10, window=600)
