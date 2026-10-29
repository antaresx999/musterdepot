"""Alle Seiten und Formulare."""
import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import config, db, quotes, trading
from .security import hash_password, login_limiter, require_csrf, verify_password
from .web import (Redirect, client_ip, current_user, flash, own_portfolio, render, require_admin,
                  require_user)

router = APIRouter()


def parse_num(value):
    """'1’234.50', "1'234,5" oder '1234.5' -> 1234.5; leer -> None."""
    if value is None:
        return None
    s = re.sub(r"[\s'’]", "", str(value)).replace(",", ".")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        raise trading.TradeError("'{}' ist keine gültige Zahl.".format(value))


def back(url):
    return RedirectResponse(url, status_code=303)


def _safe_next(url):
    return url if url and url.startswith("/") and not url.startswith("//") else "/"


# --------------------------------------------------------------------------- #
# Anmeldung
# --------------------------------------------------------------------------- #

@router.get("/login")
def login_form(request: Request, next: str = "/"):
    if current_user(request):
        return back(_safe_next(next))
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/"),
          csrf: str = Form("")):
    require_csrf(request, csrf)
    if not login_limiter.allow(client_ip(request)):
        return render(request, "login.html", {"next": next, "error": "Zu viele Versuche. Bitte später erneut."},
                      status=429)
    with db.get() as conn:
        user = db.one(conn, "SELECT * FROM users WHERE username=? AND active=1", (username.strip(),))
        if not user or not verify_password(password, user["password_hash"]):
            return render(request, "login.html", {"next": next, "error": "Benutzername oder Passwort falsch.",
                                                  "username": username}, status=401)
        conn.execute("UPDATE users SET last_login=datetime('now') WHERE id=?", (user["id"],))
    request.session.clear()
    request.session["uid"] = user["id"]
    return back(_safe_next(next))


@router.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    require_csrf(request, csrf)
    request.session.clear()
    return back("/login")


# --------------------------------------------------------------------------- #
# Übersicht
# --------------------------------------------------------------------------- #

def _watchlist(conn, user):
    return db.all_(conn, "SELECT i.* FROM watchlist w JOIN instruments i ON i.id=w.instrument_id "
                         "WHERE w.user_id=? ORDER BY i.name", (user["id"],))


@router.get("/")
def index(request: Request):
    user = require_user(request)
    with db.get() as conn:
        pfs = db.all_(conn, "SELECT * FROM portfolios WHERE user_id=? ORDER BY name", (user["id"],))
        ccys = db.all_(conn, "SELECT DISTINCT i.currency, p.currency AS target FROM holdings h "
                             "JOIN instruments i ON i.id=h.instrument_id JOIN portfolios p ON p.id=h.portfolio_id "
                             "WHERE p.user_id=?", (user["id"],))
    for c in ccys:
        trading.ensure_fx([c["currency"]], c["target"])
    with db.get() as conn:
        for pf in pfs:
            pf["v"] = trading.valuation(conn, pf)
        watch = _watchlist(conn, user)
    return render(request, "index.html", {"portfolios": pfs, "watchlist": watch})


@router.post("/depot/neu")
def portfolio_create(request: Request, name: str = Form(""), currency: str = Form("CHF"),
                     start_cash: str = Form("100000"), fee_fixed: str = Form("0"), fee_pct: str = Form("0"),
                     fee_min: str = Form("0"), description: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    try:
        if not name.strip():
            raise trading.TradeError("Bitte einen Namen angeben.")
        if currency not in config.CURRENCIES:
            raise trading.TradeError("Ungültige Währung.")
        cash = parse_num(start_cash) or 0
        fees = [parse_num(fee_fixed) or 0, parse_num(fee_pct) or 0, parse_num(fee_min) or 0]
        if cash < 0 or min(fees) < 0:
            raise trading.TradeError("Beträge dürfen nicht negativ sein.")
    except trading.TradeError as e:
        flash(request, str(e), "error")
        return back("/")
    with db.get() as conn:
        cur = conn.execute("INSERT INTO portfolios (user_id, name, description, currency, fee_fixed, fee_pct, "
                           "fee_min) VALUES (?,?,?,?,?,?,?)",
                           (user["id"], name.strip()[:80], description.strip()[:500], currency, *fees))
        pid = cur.lastrowid
    if cash > 0:
        trading.cash_booking(pid, "deposit", cash, "Startkapital")
    with db.get() as conn:
        trading.snapshot(conn, db.one(conn, "SELECT * FROM portfolios WHERE id=?", (pid,)))
    flash(request, "Depot «{}» angelegt.".format(name.strip()))
    return back("/depot/{}".format(pid))


# --------------------------------------------------------------------------- #
# Depot
# --------------------------------------------------------------------------- #

def _load_depot(request, pid):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        ccys = [r[0] for r in conn.execute("SELECT DISTINCT i.currency FROM holdings h JOIN instruments i "
                                           "ON i.id=h.instrument_id WHERE h.portfolio_id=?", (pid,))]
    trading.ensure_fx(ccys, pf["currency"])
    return user, pf


@router.get("/depot/{pid}")
def portfolio_view(request: Request, pid: int):
    user, pf = _load_depot(request, pid)
    with db.get() as conn:
        v = trading.valuation(conn, pf)
        orders = db.all_(conn, "SELECT o.*, i.name, i.ticker, i.currency, i.price FROM orders o "
                               "JOIN instruments i ON i.id=o.instrument_id WHERE o.portfolio_id=? AND "
                               "(o.status='open' OR o.closed_at > datetime('now','-3 days')) "
                               "ORDER BY o.status='open' DESC, o.id DESC LIMIT 30", (pid,))
        txs = db.all_(conn, "SELECT t.*, i.name, i.ticker, i.currency AS inst_currency FROM transactions t "
                            "LEFT JOIN instruments i ON i.id=t.instrument_id WHERE t.portfolio_id=? "
                            "ORDER BY t.created_at DESC, t.id DESC LIMIT 10", (pid,))
        others = db.all_(conn, "SELECT id, name FROM portfolios WHERE user_id=? ORDER BY name", (user["id"],))
    alloc = {}
    for p in v["positions"]:
        if p["value"]:
            alloc[p["kind"]] = alloc.get(p["kind"], 0) + p["value"]
    if v["cash"] > 0:
        alloc["cash"] = v["cash"]
    total = sum(alloc.values()) or 1
    allocation = sorted(((k, a, a / total * 100) for k, a in alloc.items()), key=lambda x: -x[1])
    return render(request, "depot.html", {"pf": pf, "v": v, "orders": orders, "txs": txs,
                                          "allocation": allocation, "portfolios": others})


@router.post("/depot/{pid}/aktualisieren")
def portfolio_refresh(request: Request, pid: int, csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        insts = db.all_(conn, "SELECT * FROM instruments WHERE id IN (SELECT instrument_id FROM holdings "
                              "WHERE portfolio_id=? UNION SELECT instrument_id FROM orders WHERE portfolio_id=? "
                              "AND status='open')", (pid, pid))
    errors = quotes.refresh(insts, max_age=15)
    trading.ensure_fx({i["currency"] for i in insts}, pf["currency"], max_age=600)
    filled = trading.process_open_orders()
    with db.get() as conn:
        trading.snapshot(conn, db.one(conn, "SELECT * FROM portfolios WHERE id=?", (pid,)))
    msg = "Kurse aktualisiert."
    if filled:
        msg += " {} Auftrag/Aufträge ausgeführt.".format(filled)
    if errors:
        flash(request, msg + " Nicht erreichbar: " + "; ".join(sorted(set(errors.values()))), "error")
    else:
        flash(request, msg)
    return back("/depot/{}".format(pid))


@router.post("/depot/{pid}/geld")
def portfolio_cash(request: Request, pid: int, kind: str = Form(""), amount: str = Form(""),
                   note: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        own_portfolio(conn, user, pid)
    try:
        trading.cash_booking(pid, kind, parse_num(amount), note.strip()[:200])
        flash(request, "{} gebucht.".format(trading.TX_LABELS[kind]))
    except trading.TradeError as e:
        flash(request, str(e), "error")
    return back("/depot/{}".format(pid))


@router.get("/depot/{pid}/einstellungen")
def portfolio_settings(request: Request, pid: int):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
    return render(request, "depot_settings.html", {"pf": pf})


@router.post("/depot/{pid}/einstellungen")
def portfolio_settings_save(request: Request, pid: int, name: str = Form(""), description: str = Form(""),
                            fee_fixed: str = Form("0"), fee_pct: str = Form("0"), fee_min: str = Form("0"),
                            csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        own_portfolio(conn, user, pid)
    try:
        fees = [parse_num(fee_fixed) or 0, parse_num(fee_pct) or 0, parse_num(fee_min) or 0]
        if min(fees) < 0:
            raise trading.TradeError("Gebühren dürfen nicht negativ sein.")
        if not name.strip():
            raise trading.TradeError("Bitte einen Namen angeben.")
    except trading.TradeError as e:
        flash(request, str(e), "error")
        return back("/depot/{}/einstellungen".format(pid))
    with db.get() as conn:
        conn.execute("UPDATE portfolios SET name=?, description=?, fee_fixed=?, fee_pct=?, fee_min=? WHERE id=?",
                     (name.strip()[:80], description.strip()[:500], *fees, pid))
    flash(request, "Einstellungen gespeichert.")
    return back("/depot/{}".format(pid))


@router.post("/depot/{pid}/loeschen")
def portfolio_delete(request: Request, pid: int, confirm: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        if confirm.strip() != pf["name"]:
            flash(request, "Zum Löschen bitte den Namen des Depots genau eintippen.", "error")
            return back("/depot/{}/einstellungen".format(pid))
        conn.execute("DELETE FROM portfolios WHERE id=?", (pid,))
    flash(request, "Depot «{}» gelöscht.".format(pf["name"]))
    return back("/")


@router.get("/depot/{pid}/transaktionen")
def portfolio_transactions(request: Request, pid: int, format: str = ""):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        txs = db.all_(conn, "SELECT t.*, i.name, i.ticker, i.currency AS inst_currency FROM transactions t "
                            "LEFT JOIN instruments i ON i.id=t.instrument_id WHERE t.portfolio_id=? "
                            "ORDER BY t.created_at DESC, t.id DESC", (pid,))
        orders = db.all_(conn, "SELECT o.*, i.name, i.ticker, i.currency FROM orders o JOIN instruments i "
                               "ON i.id=o.instrument_id WHERE o.portfolio_id=? ORDER BY o.id DESC", (pid,))
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Datum (UTC)", "Art", "Titel", "Symbol", "Stück", "Kurs", "Titelwährung", "Wechselkurs",
                    "Gebühr", "Steuer", "Betrag " + pf["currency"], "Realisiert", "Notiz"])
        for t in reversed(txs):
            w.writerow([t["created_at"], trading.TX_LABELS[t["kind"]], t["name"] or "", t["ticker"] or "",
                        t["quantity"] if t["quantity"] is not None else "", t["price"] or "",
                        t["inst_currency"] or "", t["fx"] or "", t["fee"], t["tax"], t["amount"],
                        t["realized"] if t["realized"] is not None else "", t["note"]])
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", pf["name"]) or "depot"
        return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="{}-{}.csv"'.format(
                            name, date.today().isoformat())})
    return render(request, "transactions.html", {"pf": pf, "txs": txs, "orders": orders})


# --------------------------------------------------------------------------- #
# Handel
# --------------------------------------------------------------------------- #

def _instrument(conn, iid):
    inst = db.one(conn, "SELECT * FROM instruments WHERE id=?", (iid,))
    if not inst:
        raise HTTPException(404, "Titel nicht gefunden.")
    return inst


@router.get("/depot/{pid}/handel/{iid}")
def trade_form(request: Request, pid: int, iid: int, side: str = "buy"):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        _instrument(conn, iid)
    error = None
    try:
        quotes.refresh_one(iid)
    except quotes.QuoteError as e:
        error = str(e)
    with db.get() as conn:
        inst = _instrument(conn, iid)
    try:
        fx = quotes.fx_rate(inst["currency"], pf["currency"])
    except quotes.QuoteError as e:
        fx, error = None, str(e)
    with db.get() as conn:
        hold = db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?", (pid, iid))
        pending = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE portfolio_id=? AND "
                               "instrument_id=? AND side='sell' AND status='open'", (pid, iid)).fetchone()[0]
    return render(request, "trade.html", {
        "pf": pf, "inst": inst, "fx": fx, "hold": hold, "free_qty": (hold["quantity"] if hold else 0) - pending,
        "side": side if side in trading.SIDES else "buy", "error": error or (inst["last_error"] or None),
    })


@router.post("/depot/{pid}/handel/{iid}")
def trade_submit(request: Request, pid: int, iid: int, side: str = Form("buy"), order_type: str = Form("market"),
                 quantity: str = Form(""), limit_price: str = Form(""), stop_price: str = Form(""),
                 valid_until: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        inst = _instrument(conn, iid)
    url = "/depot/{}/handel/{}?side={}".format(pid, iid, side)
    try:
        order = trading.place_order(pf, iid, side, order_type, parse_num(quantity), parse_num(limit_price),
                                    parse_num(stop_price), valid_until.strip() or None)
    except trading.TradeError as e:
        flash(request, str(e), "error")
        return back(url)
    what = "{} {} {}".format(trading.SIDES[side], _q(order["quantity"]), inst["name"])
    if order["status"] == "filled":
        flash(request, "{}: ausgeführt.".format(what))
    else:
        flash(request, "{}: Auftrag ist offen und wird ausgeführt, sobald der Kurs passt.{}".format(
            what, " Hinweis: " + order["message"] if order["message"] else ""))
    return back("/depot/{}".format(pid))


def _q(x):
    return "{:,.8f}".format(x).rstrip("0").rstrip(".").replace(",", "’")


@router.post("/depot/{pid}/auftrag/{oid}/loeschen")
def order_cancel(request: Request, pid: int, oid: int, csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        own_portfolio(conn, user, pid)
    try:
        trading.cancel_order(pid, oid)
        flash(request, "Auftrag gelöscht.")
    except trading.TradeError as e:
        flash(request, str(e), "error")
    return back("/depot/{}".format(pid))


@router.get("/depot/{pid}/nachtragen/{iid}")
def past_buy_form(request: Request, pid: int, iid: int):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        inst = _instrument(conn, iid)
    return render(request, "past_buy.html", {"pf": pf, "inst": inst, "today": date.today().isoformat(),
                                             "form": request.session.pop("past_buy_form", {})})


@router.post("/depot/{pid}/nachtragen/{iid}")
def past_buy_submit(request: Request, pid: int, iid: int, day: str = Form(""), quantity: str = Form(""),
                    price: str = Form(""), fx: str = Form(""), fee: str = Form(""), funding: str = Form("deposit"),
                    note: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        inst = _instrument(conn, iid)
    try:
        rate = 1.0 if inst["currency"] == pf["currency"] else parse_num(fx)
        trading.book_past_buy(pid, iid, day.strip(), parse_num(quantity), parse_num(price), rate,
                              parse_num(fee), funding, note.strip()[:200])
    except trading.TradeError as e:
        flash(request, str(e), "error")
        request.session["past_buy_form"] = {"day": day, "quantity": quantity, "price": price, "fx": fx,
                                            "fee": fee, "funding": funding, "note": note[:200]}
        return back("/depot/{}/nachtragen/{}".format(pid, iid))
    with db.get() as conn:
        trading.snapshot(conn, db.one(conn, "SELECT * FROM portfolios WHERE id=?", (pid,)))
    flash(request, "Kauf von {} {} vom {} nachgetragen.".format(_q(parse_num(quantity)), inst["name"],
                                                               datetime.strptime(day.strip(), "%Y-%m-%d")
                                                               .strftime("%d.%m.%Y")))
    return back("/depot/{}".format(pid))


@router.get("/api/titel/{iid}/kurs")
def api_price_on(request: Request, iid: int, tag: str = "", depot: int = 0):
    """Vorschlag für Kurs und Wechselkurs an einem Tag (für nachgetragene Käufe)."""
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", tag) or tag > date.today().isoformat():
        return JSONResponse({"error": "Ungültiges Datum"}, status_code=400)
    with db.get() as conn:
        pf = own_portfolio(conn, user, depot)
        inst = _instrument(conn, iid)
    price, price_day = quotes.price_on(iid, tag)
    fx, fx_hist = None, False
    try:
        fx, fx_hist = quotes.fx_on(inst["currency"], pf["currency"], tag)
    except quotes.QuoteError:
        pass
    return {"price": price, "price_day": price_day, "fx": fx, "fx_historic": fx_hist}


@router.get("/depot/{pid}/dividende/{iid}")
def dividend_form(request: Request, pid: int, iid: int):
    user = require_user(request)
    with db.get() as conn:
        pf = own_portfolio(conn, user, pid)
        inst = _instrument(conn, iid)
        hold = db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?", (pid, iid))
    return render(request, "dividend.html", {"pf": pf, "inst": inst, "hold": hold,
                                             "today": date.today().isoformat()})


@router.post("/depot/{pid}/dividende/{iid}")
def dividend_submit(request: Request, pid: int, iid: int, per_share: str = Form(""), gross: str = Form(""),
                    tax: str = Form(""), day: str = Form(""), note: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        own_portfolio(conn, user, pid)
        hold = db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?", (pid, iid))
    try:
        amount = parse_num(gross)
        ps = parse_num(per_share)
        if amount is None and ps is not None and hold:
            amount = ps * hold["quantity"]
        if day and not re.match(r"^\d{4}-\d{2}-\d{2}$", day):
            raise trading.TradeError("Ungültiges Datum.")
        if day and day > date.today().isoformat():
            raise trading.TradeError("Das Datum liegt in der Zukunft.")
        trading.book_dividend(pid, iid, amount, parse_num(tax) or 0, note.strip()[:200], day or None)
    except trading.TradeError as e:
        flash(request, str(e), "error")
        return back("/depot/{}/dividende/{}".format(pid, iid))
    flash(request, "Dividende gebucht.")
    return back("/depot/{}".format(pid))


# --------------------------------------------------------------------------- #
# Suche und Titel
# --------------------------------------------------------------------------- #

@router.get("/suche")
def search(request: Request, q: str = "", depot: int = 0, nachtrag: int = 0):
    require_user(request)
    q = q.strip()[:80]
    results, errors, known = [], [], []
    if q:
        like = "%{}%".format(q)
        with db.get() as conn:
            known = db.all_(conn, "SELECT * FROM instruments WHERE name LIKE ? OR symbol LIKE ? OR ticker LIKE ? "
                                  "ORDER BY name LIMIT 20", (like, like, like))
        results, errors = quotes.search(q)
        seen = {(k["provider"], k["symbol"]) for k in known}
        results = [r for r in results if (r["provider"], r["symbol"]) not in seen]
    return render(request, "search.html", {"q": q, "results": results, "errors": errors, "known": known,
                                           "depot": depot, "nachtrag": 1 if nachtrag and depot else 0})


@router.post("/titel/aufnehmen")
def instrument_add(request: Request, provider: str = Form(""), symbol: str = Form(""), ticker: str = Form(""),
                   name: str = Form(""), kind: str = Form("stock"), exchange: str = Form(""), depot: int = Form(0),
                   nachtrag: int = Form(0), csrf: str = Form("")):
    require_user(request)
    require_csrf(request, csrf)
    if provider not in ("yahoo", "coingecko") or not symbol:
        raise HTTPException(400, "Ungültiger Titel.")
    if kind not in quotes.KIND_LABELS:
        kind = "other"
    try:
        inst = quotes.get_or_create(provider, symbol.strip(), ticker.strip(), name.strip(), kind, exchange.strip())
    except quotes.QuoteError as e:
        flash(request, "Titel konnte nicht aufgenommen werden: {}".format(e), "error")
        return back("/suche?q={}".format(symbol))
    if depot and nachtrag:
        return back("/depot/{}/nachtragen/{}".format(depot, inst["id"]))
    return back("/titel/{}{}".format(inst["id"], "?depot={}".format(depot) if depot else ""))


@router.get("/titel/{iid}")
def instrument_view(request: Request, iid: int, depot: int = 0):
    user = require_user(request)
    with db.get() as conn:
        _instrument(conn, iid)
    try:
        quotes.refresh_one(iid, max_age=config.REFRESH_SECONDS)
    except quotes.QuoteError:
        pass
    with db.get() as conn:
        inst = _instrument(conn, iid)
        pfs = db.all_(conn, "SELECT p.id, p.name, p.currency, p.cash, h.quantity, h.cost FROM portfolios p "
                            "LEFT JOIN holdings h ON h.portfolio_id=p.id AND h.instrument_id=? "
                            "WHERE p.user_id=? ORDER BY p.name", (iid, user["id"]))
        watched = conn.execute("SELECT 1 FROM watchlist WHERE user_id=? AND instrument_id=?",
                               (user["id"], iid)).fetchone() is not None
    return render(request, "instrument.html", {"inst": inst, "portfolios": pfs, "watched": watched,
                                               "depot": depot})


@router.post("/titel/{iid}/beobachten")
def instrument_watch(request: Request, iid: int, csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        _instrument(conn, iid)
        cur = conn.execute("DELETE FROM watchlist WHERE user_id=? AND instrument_id=?", (user["id"], iid))
        if not cur.rowcount:
            conn.execute("INSERT INTO watchlist (user_id, instrument_id) VALUES (?,?)", (user["id"], iid))
    return back("/titel/{}".format(iid))


RANGES = {"1m": 31, "3m": 92, "6m": 183, "1y": 366, "3y": 1096, "5y": 1827, "max": 100000}


@router.get("/api/titel/{iid}/verlauf")
def api_instrument_history(request: Request, iid: int, range: str = "1y"):
    if not current_user(request):
        return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
    error = quotes.load_history(iid)
    since = (date.today() - timedelta(days=RANGES.get(range, 366))).isoformat()
    with db.get() as conn:
        rows = conn.execute("SELECT day, close FROM price_history WHERE instrument_id=? AND day>=? ORDER BY day",
                            (iid, since)).fetchall()
    return {"points": [[r[0], r[1]] for r in rows], "error": error}


@router.get("/api/depot/{pid}/verlauf")
def api_portfolio_history(request: Request, pid: int, range: str = "max"):
    user = current_user(request)
    if not user:
        return JSONResponse({"error": "Nicht angemeldet"}, status_code=401)
    since = (date.today() - timedelta(days=RANGES.get(range, 100000))).isoformat()
    with db.get() as conn:
        own_portfolio(conn, user, pid)
        rows = conn.execute("SELECT day, value, invested FROM snapshots WHERE portfolio_id=? AND day>=? "
                            "ORDER BY day", (pid, since)).fetchall()
    return {"points": [[r[0], r[1]] for r in rows], "invested": [[r[0], r[2]] for r in rows]}


# --------------------------------------------------------------------------- #
# Konto
# --------------------------------------------------------------------------- #

@router.get("/konto")
def account(request: Request):
    require_user(request)
    return render(request, "account.html")


@router.post("/konto")
def account_save(request: Request, display_name: str = Form(""), old_password: str = Form(""),
                 new_password: str = Form(""), new_password2: str = Form(""), csrf: str = Form("")):
    user = require_user(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name.strip()[:80], user["id"]))
        if new_password:
            row = db.one(conn, "SELECT password_hash FROM users WHERE id=?", (user["id"],))
            if not verify_password(old_password, row["password_hash"]):
                flash(request, "Das bisherige Passwort stimmt nicht.", "error")
                return back("/konto")
            if len(new_password) < 8 or new_password != new_password2:
                flash(request, "Das neue Passwort muss mindestens 8 Zeichen haben und zweimal gleich sein.", "error")
                return back("/konto")
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user["id"]))
    flash(request, "Gespeichert.")
    return back("/konto")


# --------------------------------------------------------------------------- #
# Verwaltung
# --------------------------------------------------------------------------- #

@router.get("/admin/benutzer")
def admin_users(request: Request):
    require_admin(request)
    with db.get() as conn:
        users = db.all_(conn, "SELECT u.*, (SELECT COUNT(*) FROM portfolios p WHERE p.user_id=u.id) AS depots "
                              "FROM users u ORDER BY u.username")
    return render(request, "admin_users.html", {"users": users})


@router.post("/admin/benutzer/neu")
def admin_user_create(request: Request, username: str = Form(""), display_name: str = Form(""),
                      password: str = Form(""), role: str = Form("user"), csrf: str = Form("")):
    require_admin(request)
    require_csrf(request, csrf)
    username = username.strip()
    if not re.match(r"^[A-Za-z0-9._@-]{2,40}$", username):
        flash(request, "Benutzername: 2–40 Zeichen, nur Buchstaben, Ziffern und . _ @ -", "error")
        return back("/admin/benutzer")
    if len(password) < 8:
        flash(request, "Das Passwort muss mindestens 8 Zeichen haben.", "error")
        return back("/admin/benutzer")
    with db.get() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            flash(request, "Diesen Benutzernamen gibt es schon.", "error")
            return back("/admin/benutzer")
        conn.execute("INSERT INTO users (username, display_name, password_hash, role) VALUES (?,?,?,?)",
                     (username, display_name.strip()[:80], hash_password(password),
                      "admin" if role == "admin" else "user"))
    flash(request, "Benutzer «{}» angelegt.".format(username))
    return back("/admin/benutzer")


@router.post("/admin/benutzer/{uid}")
def admin_user_action(request: Request, uid: int, action: str = Form(""), password: str = Form(""),
                      csrf: str = Form("")):
    me = require_admin(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        u = db.one(conn, "SELECT * FROM users WHERE id=?", (uid,))
        if not u:
            raise HTTPException(404, "Benutzer nicht gefunden.")
        if action == "password":
            if len(password) < 8:
                flash(request, "Das Passwort muss mindestens 8 Zeichen haben.", "error")
                return back("/admin/benutzer")
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), uid))
            flash(request, "Passwort für «{}» gesetzt.".format(u["username"]))
        elif uid == me["id"]:
            flash(request, "Das eigene Konto kann hier nicht gesperrt oder gelöscht werden.", "error")
        elif action == "toggle":
            conn.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
            flash(request, "«{}» {}.".format(u["username"], "gesperrt" if u["active"] else "entsperrt"))
        elif action == "role":
            conn.execute("UPDATE users SET role=? WHERE id=?", ("user" if u["role"] == "admin" else "admin", uid))
            flash(request, "Rolle von «{}» geändert.".format(u["username"]))
        elif action == "delete":
            conn.execute("DELETE FROM users WHERE id=?", (uid,))
            flash(request, "«{}» und alle Depots gelöscht.".format(u["username"]))
    return back("/admin/benutzer")


@router.get("/admin/titel")
def admin_instruments(request: Request):
    require_admin(request)
    with db.get() as conn:
        insts = db.all_(conn, "SELECT i.*, (SELECT COUNT(*) FROM holdings h WHERE h.instrument_id=i.id) AS held, "
                              "(SELECT COUNT(*) FROM transactions t WHERE t.instrument_id=i.id) AS used "
                              "FROM instruments i ORDER BY i.name")
        fx = db.all_(conn, "SELECT * FROM fx_rates ORDER BY pair")
    return render(request, "admin_instruments.html", {"insts": insts, "fx": fx})


@router.post("/admin/titel/neu")
def admin_instrument_create(request: Request, name: str = Form(""), ticker: str = Form(""),
                            kind: str = Form("stock"), currency: str = Form("CHF"), price: str = Form(""),
                            csrf: str = Form("")):
    require_admin(request)
    require_csrf(request, csrf)
    try:
        p = parse_num(price)
        if not name.strip() or not ticker.strip() or not p or p <= 0:
            raise trading.TradeError("Bitte Name, Kürzel und einen positiven Kurs angeben.")
        currency = currency.strip().upper()
        if not re.match(r"^[A-Z]{3}$", currency):
            raise trading.TradeError("Währung als dreistelliger Code, z. B. CHF.")
    except trading.TradeError as e:
        flash(request, str(e), "error")
        return back("/admin/titel")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with db.get() as conn:
        if conn.execute("SELECT 1 FROM instruments WHERE provider='manual' AND symbol=?",
                        (ticker.strip().upper(),)).fetchone():
            flash(request, "Dieses Kürzel gibt es schon.", "error")
            return back("/admin/titel")
        cur = conn.execute("INSERT INTO instruments (provider, symbol, ticker, name, kind, currency, price, "
                           "prev_close, price_time, updated_at) VALUES ('manual',?,?,?,?,?,?,?,?,?)",
                           (ticker.strip().upper(), ticker.strip().upper(), name.strip(),
                            kind if kind in quotes.KIND_LABELS else "other", currency, p, p, now, now))
        conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)", (cur.lastrowid, now[:10], p))
    flash(request, "Titel «{}» angelegt.".format(name.strip()))
    return back("/admin/titel")


@router.post("/admin/titel/{iid}")
def admin_instrument_action(request: Request, iid: int, action: str = Form(""), price: str = Form(""),
                            csrf: str = Form("")):
    require_admin(request)
    require_csrf(request, csrf)
    with db.get() as conn:
        inst = _instrument(conn, iid)
    if action == "price":
        try:
            p = parse_num(price)
            if not p or p <= 0:
                raise trading.TradeError("Bitte einen positiven Kurs angeben.")
        except trading.TradeError as e:
            flash(request, str(e), "error")
            return back("/admin/titel")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with db.get() as conn:
            # Vortageskurs nur beim ersten Kurs eines neuen Tages nachziehen
            prev = inst["price"] if (inst["price_time"] or "")[:10] != now[:10] else inst["prev_close"]
            conn.execute("UPDATE instruments SET price=?, prev_close=?, price_time=?, updated_at=? WHERE id=?",
                         (p, prev, now, now, iid))
            conn.execute("INSERT OR REPLACE INTO price_history VALUES (?,?,?)", (iid, now[:10], p))
        trading.process_open_orders()
        flash(request, "Kurs von «{}» gesetzt.".format(inst["name"]))
    elif action == "refresh":
        err = quotes.refresh([inst], max_age=0).get(iid) or quotes.load_history(iid, force=True)
        flash(request, err or "Kurs und Verlauf neu geladen.", "error" if err else "ok")
    elif action == "delete":
        with db.get() as conn:
            used = conn.execute("SELECT 1 FROM transactions WHERE instrument_id=? UNION SELECT 1 FROM holdings "
                                "WHERE instrument_id=? UNION SELECT 1 FROM orders WHERE instrument_id=?",
                                (iid, iid, iid)).fetchone()
            if used:
                flash(request, "Der Titel wird in Depots verwendet und kann nicht gelöscht werden.", "error")
                return back("/admin/titel")
            conn.execute("DELETE FROM instruments WHERE id=?", (iid,))
        flash(request, "Titel gelöscht.")
    return back("/admin/titel")
