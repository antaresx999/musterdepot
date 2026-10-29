"""Ablauf-Tests über die Weboberfläche mit manuellen Titeln (ohne Internet)."""
import re
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, trading
from app.main import app

from .conftest import csrf_of


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def portfolio(pid):
    with db.get() as conn:
        return db.one(conn, "SELECT * FROM portfolios WHERE id=?", (pid,))


def holding(pid, iid):
    with db.get() as conn:
        return db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?", (pid, iid))


def order_status(oid):
    with db.get() as conn:
        return db.one(conn, "SELECT * FROM orders WHERE id=?", (oid,))["status"]


def new_depot(c, name, cash="10000", ccy="CHF", fees=("0", "0", "0")):
    r = c.post("/depot/neu", data={"name": name, "currency": ccy, "start_cash": cash, "fee_fixed": fees[0],
                                   "fee_pct": fees[1], "fee_min": fees[2], "csrf": c.csrf})
    assert r.status_code == 200, r.text
    return int(re.search(r"/depot/(\d+)/einstellungen", r.text).group(1))


def new_manual(c, ticker, price, ccy="CHF"):
    c.post("/admin/titel/neu", data={"name": "Test " + ticker, "ticker": ticker, "kind": "stock",
                                     "currency": ccy, "price": price, "csrf": c.csrf})
    with db.get() as conn:
        return db.one(conn, "SELECT id FROM instruments WHERE provider='manual' AND symbol=?", (ticker,))["id"]


def set_price(c, iid, price):
    c.post("/admin/titel/{}".format(iid), data={"action": "price", "price": price, "csrf": c.csrf})


def trade(c, pid, iid, side, qty, order_type="market", limit="", stop=""):
    return c.post("/depot/{}/handel/{}".format(pid, iid), data={
        "side": side, "order_type": order_type, "quantity": qty, "limit_price": limit, "stop_price": stop,
        "valid_until": "", "csrf": c.csrf})


def test_fee_and_trigger_rules():
    pf = {"fee_fixed": 5, "fee_pct": 0.5, "fee_min": 9}
    assert trading.fee_for(pf, 1000) == 10.0
    assert trading.fee_for(pf, 100) == 9.0
    assert trading.fee_for({"fee_fixed": 0, "fee_pct": 0, "fee_min": 0}, 1000) == 0
    assert trading.triggered({"order_type": "limit", "side": "buy", "limit_price": 10}, 9.9)
    assert not trading.triggered({"order_type": "limit", "side": "buy", "limit_price": 10}, 10.1)
    assert trading.triggered({"order_type": "limit", "side": "sell", "limit_price": 10}, 10)
    assert trading.triggered({"order_type": "stop", "side": "sell", "stop_price": 10}, 9)
    assert not trading.triggered({"order_type": "stop", "side": "sell", "stop_price": 10}, 11)
    assert trading.triggered({"order_type": "stop", "side": "buy", "stop_price": 10}, 11)


def test_buy_sell_with_fees(admin):
    pid = new_depot(admin, "Gebühren", "10000", fees=("0", "0.5", "5"))
    iid = new_manual(admin, "AAA", "100")
    trade(admin, pid, iid, "buy", "20")             # 2000 + 10 Gebühr
    assert portfolio(pid)["cash"] == pytest.approx(7990)
    assert holding(pid, iid)["cost"] == pytest.approx(2010)
    set_price(admin, iid, "120")
    trade(admin, pid, iid, "sell", "10")            # 1200 - 6 Gebühr; Einstand Hälfte = 1005
    assert portfolio(pid)["cash"] == pytest.approx(9184)
    h = holding(pid, iid)
    assert h["quantity"] == pytest.approx(10) and h["cost"] == pytest.approx(1005)
    pf = portfolio(pid)
    with db.get() as conn:
        realized = conn.execute("SELECT realized FROM transactions WHERE portfolio_id=? AND kind='sell'",
                                (pid,)).fetchone()[0]
        v = trading.valuation(conn, pf)
    assert realized == pytest.approx(189)
    assert v["value"] == pytest.approx(9184 + 1200)
    assert v["gain"] == pytest.approx(384)
    trade(admin, pid, iid, "sell", "10")
    assert holding(pid, iid) is None


def test_rejections(admin):
    pid = new_depot(admin, "Abgelehnt", "1000")
    iid = new_manual(admin, "BBB", "100")
    r = trade(admin, pid, iid, "buy", "11")
    assert "Zu wenig Guthaben" in r.text
    assert portfolio(pid)["cash"] == 1000
    r = trade(admin, pid, iid, "sell", "1")
    assert "Nicht genügend Stück" in r.text
    r = trade(admin, pid, iid, "buy", "-3")
    assert "positive Stückzahl" in r.text


def test_limit_and_stop_orders(admin):
    pid = new_depot(admin, "Limit", "10000")
    iid = new_manual(admin, "CCC", "50")
    trade(admin, pid, iid, "buy", "10", "limit", limit="45")
    with db.get() as conn:
        oid = conn.execute("SELECT MAX(id) FROM orders WHERE portfolio_id=?", (pid,)).fetchone()[0]
    assert order_status(oid) == "open" and holding(pid, iid) is None
    set_price(admin, iid, "44")                       # Limit erreicht -> ausgeführt zu 44
    assert order_status(oid) == "filled"
    assert portfolio(pid)["cash"] == pytest.approx(10000 - 440)
    trade(admin, pid, iid, "sell", "10", "stop", stop="40")
    with db.get() as conn:
        sid = conn.execute("SELECT MAX(id) FROM orders WHERE portfolio_id=?", (pid,)).fetchone()[0]
    r = trade(admin, pid, iid, "sell", "1")           # alle Stück sind durch den Stop reserviert
    assert "Nicht genügend Stück verfügbar" in r.text
    set_price(admin, iid, "41")
    assert order_status(sid) == "open"
    set_price(admin, iid, "39")
    assert order_status(sid) == "filled"
    assert portfolio(pid)["cash"] == pytest.approx(10000 - 440 + 390)


def test_foreign_currency_and_dividend(admin):
    pid = new_depot(admin, "Fremdwährung", "10000")
    iid = new_manual(admin, "USD1", "100", ccy="USD")
    with db.get() as conn:
        conn.execute("INSERT OR REPLACE INTO fx_rates VALUES ('USDCHF', 0.9, ?)", (_now(),))
    trade(admin, pid, iid, "buy", "10")
    assert portfolio(pid)["cash"] == pytest.approx(10000 - 900)
    r = admin.post("/depot/{}/dividende/{}".format(pid, iid),
                   data={"per_share": "2", "gross": "", "tax": "7", "day": "", "note": "", "csrf": admin.csrf})
    assert r.status_code == 200
    assert portfolio(pid)["cash"] == pytest.approx(9100 + 13)
    csv = admin.get("/depot/{}/transaktionen?format=csv".format(pid))
    assert csv.headers["content-type"].startswith("text/csv") and "Dividende" in csv.text


def test_cash_and_pages(admin):
    pid = new_depot(admin, "Seiten", "500", ccy="EUR")
    admin.post("/depot/{}/geld".format(pid), data={"kind": "deposit", "amount": "1'500.50", "csrf": admin.csrf})
    assert portfolio(pid)["cash"] == pytest.approx(2000.5)
    r = admin.post("/depot/{}/geld".format(pid), data={"kind": "withdraw", "amount": "5000", "csrf": admin.csrf})
    assert "nicht vorhanden" in r.text
    iid = new_manual(admin, "PAGE", "10", ccy="EUR")
    for url in ["/", "/depot/{}".format(pid), "/depot/{}/transaktionen".format(pid),
                "/depot/{}/einstellungen".format(pid), "/depot/{}/handel/{}".format(pid, iid),
                "/depot/{}/dividende/{}".format(pid, iid), "/titel/{}".format(iid), "/suche?q=PAGE",
                "/konto", "/admin/benutzer", "/admin/titel"]:
        r = admin.get(url)
        assert r.status_code == 200, url
    data = admin.get("/api/titel/{}/verlauf?range=1y".format(iid)).json()
    assert data["points"]
    assert admin.get("/api/depot/{}/verlauf".format(pid)).json()["points"]


def test_other_users_cannot_see_depot(admin):
    pid = new_depot(admin, "Privat")
    admin.post("/admin/benutzer/neu", data={"username": "erika", "display_name": "Erika", "password": "erika-pw-123",
                                           "role": "user", "csrf": admin.csrf})
    with TestClient(app) as c:
        tok = csrf_of(c, "/login")
        c.post("/login", data={"username": "erika", "password": "erika-pw-123", "csrf": tok, "next": "/"})
        assert c.get("/").status_code == 200
        assert c.get("/depot/{}".format(pid)).status_code == 404
        assert c.get("/admin/benutzer").status_code == 403
        assert c.get("/api/depot/{}/verlauf".format(pid)).status_code == 404


def test_csrf_required(admin):
    r = admin.post("/depot/neu", data={"name": "X", "currency": "CHF", "start_cash": "1", "csrf": "falsch"})
    assert r.status_code == 400


def test_day_change_counts_from_purchase(admin):
    pid = new_depot(admin, "Tag", "10000")
    iid = new_manual(admin, "DAY", "100")
    set_price(admin, iid, "100")
    trade(admin, pid, iid, "buy", "10")
    set_price(admin, iid, "90")   # Vortageskurs bleibt 100, gekauft wurde aber heute zu 100
    pf = portfolio(pid)
    with db.get() as conn:
        v = trading.valuation(conn, pf)
    assert v["day_change"] == pytest.approx(-100)
    assert v["gain"] == pytest.approx(-100)


def past_buy(c, pid, iid, **kw):
    data = {"day": "2024-03-15", "quantity": "10", "price": "80", "fx": "", "fee": "", "funding": "deposit",
            "note": "", "csrf": c.csrf}
    data.update(kw)
    return c.post("/depot/{}/nachtragen/{}".format(pid, iid), data=data)


def test_past_buy_as_deposit(admin):
    pid = new_depot(admin, "Nachtrag", "1000", fees=("0", "1", "5"))
    iid = new_manual(admin, "PAST", "100")
    r = past_buy(admin, pid, iid)                     # 800 + 1 % (8, mind. 5) = 808
    assert "nachgetragen" in r.text
    pf = portfolio(pid)
    assert pf["cash"] == pytest.approx(1000)          # Bargeld unverändert
    h = holding(pid, iid)
    assert h["quantity"] == pytest.approx(10) and h["cost"] == pytest.approx(808)
    with db.get() as conn:
        txs = db.all_(conn, "SELECT kind, amount, created_at FROM transactions WHERE portfolio_id=? "
                            "AND created_at LIKE '2024-03-15%' ORDER BY id", (pid,))
        v = trading.valuation(conn, pf)
        snap = db.one(conn, "SELECT * FROM snapshots WHERE portfolio_id=?", (pid,))
    assert [t["kind"] for t in txs] == ["deposit", "buy"] and txs[0]["amount"] == pytest.approx(808)
    assert v["invested"] == pytest.approx(1808)
    assert v["gain"] == pytest.approx(1000 - 808)     # Wert 1000 + 1000 Bargeld - 1808 eingezahlt
    assert v["day_change"] == pytest.approx(0)        # Tageswert wurde nachgeführt
    assert snap["value"] == pytest.approx(2000) and snap["invested"] == pytest.approx(1808)


def test_past_buy_from_cash_and_validation(admin):
    pid = new_depot(admin, "Nachtrag Cash", "1000")
    iid = new_manual(admin, "PAST2", "50", ccy="USD")
    r = past_buy(admin, pid, iid, funding="cash", fx="0.9", fee="0")  # 10 * 80 * 0.9 = 720
    assert "nachgetragen" in r.text
    assert portfolio(pid)["cash"] == pytest.approx(280)
    r = past_buy(admin, pid, iid, funding="cash", fx="0.9", fee="0")
    assert "Zu wenig Bargeld" in r.text
    assert 'value="2024-03-15"' in r.text               # Formular behält die Eingaben
    r = past_buy(admin, pid, iid, day="2999-01-01", fx="0.9")
    assert "Zukunft" in r.text
    r = past_buy(admin, pid, iid, fx="")
    assert "Wechselkurs" in r.text
    assert portfolio(pid)["cash"] == pytest.approx(280)


def test_price_suggestion_api(admin):
    pid = new_depot(admin, "API", "1000")
    iid = new_manual(admin, "SUGG", "42")
    with db.get() as conn:
        conn.execute("INSERT OR REPLACE INTO price_history VALUES (?, '2024-05-10', 39.5)", (iid,))
    s = admin.get("/api/titel/{}/kurs?tag=2024-05-12&depot={}".format(iid, pid)).json()
    assert s["price"] == 39.5 and s["price_day"] == "2024-05-10" and s["fx"] == 1.0
    assert admin.get("/depot/{}/nachtragen/{}".format(pid, iid)).status_code == 200
    assert "Nachtragen" in admin.get("/suche?q=SUGG&depot={}&nachtrag=1".format(pid)).text
