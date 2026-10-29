"""Gemeinsame Helfer: Templates, Formatierung, Sitzung, Zugriffsrechte."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from . import config, db, quotes, trading
from .security import csrf_token

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")

THIN = "’"  # Schweizer Tausendertrennzeichen ’


# --------------------------------------------------------------------------- #
# Formatierung (Schweizer Schreibweise: 1’234.56)
# --------------------------------------------------------------------------- #

def _fmt(value, decimals):
    s = "{:,.{}f}".format(abs(value), decimals).replace(",", THIN)
    return ("-" if value < 0 and float(s.replace(THIN, "")) != 0 else "") + s


def fmt_money(value, currency=None, decimals=2):
    if value is None:
        return "–"
    s = _fmt(value, decimals)
    return "{} {}".format(currency, s) if currency else s


def fmt_signed(value, currency=None):
    if value is None:
        return "–"
    s = fmt_money(value, currency)
    return ("+" + s) if value > 0.004 else s


def fmt_price(value, currency=None):
    """Kurse: grosse Werte mit 2, kleine (Krypto) mit mehr Nachkommastellen."""
    if value is None:
        return "–"
    a = abs(value)
    if a >= 1 or a == 0:
        decimals = 2
    elif a >= 0.01:
        decimals = 4
    else:  # z. B. 0.00001234 -> 4 gültige Stellen
        decimals = min(12, 3 - int(math.floor(math.log10(a))))
    return fmt_money(value, currency, decimals)


def fmt_qty(value):
    if value is None:
        return "–"
    s = "{:,.8f}".format(value).rstrip("0").rstrip(".").replace(",", THIN)
    return s


def fmt_pct(value, signed=True):
    if value is None:
        return "–"
    s = "{:.2f} %".format(value)
    return ("+" + s) if signed and value > 0.004 else s


def sign_class(value):
    if value is None:
        return ""
    return "pos" if value > 0.004 else "neg" if value < -0.004 else ""


def local_dt(value, with_time=True):
    """SQLite speichert UTC – Anzeige in der Zeitzone des Servers."""
    if not value:
        return ""
    try:
        dt = datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()
        return dt.strftime("%d.%m.%Y %H:%M" if with_time else "%d.%m.%Y")
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            return value


def to_json(value):
    return Markup(json.dumps(value).replace("<", "\\u003c"))


templates.env.filters.update(
    money=fmt_money, signed=fmt_signed, price=fmt_price, qty=fmt_qty, pct=fmt_pct,
    signclass=sign_class, dt=local_dt, tojs=to_json,
)
templates.env.globals.update(
    KIND_LABELS=quotes.KIND_LABELS, PROVIDER_LABELS=quotes.PROVIDER_LABELS,
    ORDER_TYPES=trading.ORDER_TYPES, SIDES=trading.SIDES, STATUS_LABELS=trading.STATUS_LABELS,
    TX_LABELS=trading.TX_LABELS, CURRENCIES=config.CURRENCIES, site_name=config.SITE_NAME,
)


# --------------------------------------------------------------------------- #
# Sitzung und Rechte
# --------------------------------------------------------------------------- #

class Redirect(Exception):
    def __init__(self, url):
        self.url = url


def flash(request, msg, kind="ok"):
    request.session["flash"] = {"msg": msg, "kind": kind}


def current_user(request: Request):
    if "_user" in request.scope:
        return request.scope["_user"]
    user = None
    uid = request.session.get("uid")
    if uid:
        with db.get() as conn:
            user = db.one(conn, "SELECT id, username, display_name, role FROM users WHERE id=? AND active=1",
                          (uid,))
        if not user:
            request.session.pop("uid", None)
    request.scope["_user"] = user
    return user


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise Redirect("/login?next=" + request.url.path)
    return user


def require_admin(request: Request):
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Nur für Administratoren.")
    return user


def own_portfolio(conn, user, portfolio_id):
    pf = db.one(conn, "SELECT * FROM portfolios WHERE id=? AND user_id=?", (portfolio_id, user["id"]))
    if not pf:
        raise HTTPException(404, "Depot nicht gefunden.")
    return pf


def client_ip(request):
    return request.client.host if request.client else "?"


def render(request, name, ctx=None, status=200):
    ctx = dict(ctx or {})
    ctx.setdefault("user", current_user(request))
    ctx["csrf"] = csrf_token(request)
    ctx["flash"] = request.session.pop("flash", None)
    ctx["offline"] = config.OFFLINE
    return templates.TemplateResponse(request, name, ctx, status_code=status)
