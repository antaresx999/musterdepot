"""Konfiguration über Umgebungsvariablen (siehe deploy/musterdepot.env)."""
import os
import secrets
import time
from pathlib import Path

DATA_DIR = Path(os.environ.get("MUSTERDEPOT_DATA", "/var/lib/musterdepot")).resolve()
DB_PATH = DATA_DIR / "musterdepot.db"

SITE_NAME = os.environ.get("MUSTERDEPOT_SITE_NAME", "Musterdepot")
COOKIE_SECURE = os.environ.get("MUSTERDEPOT_COOKIE_SECURE", "0") == "1"
ADMIN_USER = os.environ.get("MUSTERDEPOT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("MUSTERDEPOT_ADMIN_PASSWORD", "")
SESSION_DAYS = int(os.environ.get("MUSTERDEPOT_SESSION_DAYS", "14"))

# Wie oft (Sekunden) der Hintergrunddienst Kurse holt und Aufträge prüft
REFRESH_SECONDS = int(os.environ.get("MUSTERDEPOT_REFRESH_SECONDS", "300"))
# Kurse, die jünger sind, werden beim Handel nicht neu abgefragt
QUOTE_MAX_AGE = int(os.environ.get("MUSTERDEPOT_QUOTE_MAX_AGE", "60"))
# Kursabfrage im Internet ausschalten (nur manuelle Kurse, z. B. für Tests)
OFFLINE = os.environ.get("MUSTERDEPOT_OFFLINE", "0") == "1"
# Optionaler Demo-Schlüssel von CoinGecko (höhere Limits)
COINGECKO_API_KEY = os.environ.get("MUSTERDEPOT_COINGECKO_KEY", "")
# Hintergrunddienst ein/aus (für Tests)
WORKER = os.environ.get("MUSTERDEPOT_WORKER", "1") == "1"
HTTP_TIMEOUT = float(os.environ.get("MUSTERDEPOT_HTTP_TIMEOUT", "10"))

CURRENCIES = ["CHF", "EUR", "USD"]


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def secret_key():
    env = os.environ.get("MUSTERDEPOT_SECRET_KEY")
    if env:
        return env
    path = DATA_DIR / "secret.key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    for _ in range(50):
        key = path.read_text().strip()
        if key:
            return key
        time.sleep(0.05)
    raise RuntimeError("secret.key ist leer")
