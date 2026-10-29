"""Handelslogik: Aufträge ausführen, Gebühren, Ein-/Auszahlungen, Dividenden, Bewertung."""
import logging
import re
from datetime import date, datetime, timezone

from . import db, quotes

log = logging.getLogger("musterdepot.trading")

EPS = 1e-9
ORDER_TYPES = {"market": "Bestens/Billigst", "limit": "Limit", "stop": "Stop"}
SIDES = {"buy": "Kauf", "sell": "Verkauf"}
STATUS_LABELS = {"open": "Offen", "filled": "Ausgeführt", "cancelled": "Gelöscht",
                 "rejected": "Abgelehnt", "expired": "Abgelaufen"}
TX_LABELS = {"deposit": "Einzahlung", "withdraw": "Auszahlung", "buy": "Kauf", "sell": "Verkauf",
             "dividend": "Dividende"}


class TradeError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def money(x):
    return round(x + 0.0, 2)


def fee_for(portfolio, gross):
    if not (portfolio["fee_fixed"] or portfolio["fee_pct"] or portfolio["fee_min"]):
        return 0.0
    return money(max(portfolio["fee_min"], portfolio["fee_fixed"] + gross * portfolio["fee_pct"] / 100))


def triggered(order, price):
    """Ist die Bedingung des Auftrags beim Kurs price erfüllt?"""
    t, side = order["order_type"], order["side"]
    if t == "market":
        return True
    if t == "limit":
        return price <= order["limit_price"] if side == "buy" else price >= order["limit_price"]
    if t == "stop":
        return price >= order["stop_price"] if side == "buy" else price <= order["stop_price"]
    return False


# --------------------------------------------------------------------------- #
# Ausführung (innerhalb einer DB-Transaktion)
# --------------------------------------------------------------------------- #

def _execute(conn, order, inst, fx):
    """Führt einen Auftrag zum aktuellen Kurs aus. Wirft TradeError, wenn das nicht geht."""
    pf = db.one(conn, "SELECT * FROM portfolios WHERE id=?", (order["portfolio_id"],))
    qty, price = order["quantity"], inst["price"]
    if price is None or price <= 0:
        raise TradeError("Für {} ist kein Kurs vorhanden.".format(inst["name"]))
    gross = money(qty * price * fx)
    fee = fee_for(pf, gross)
    hold = db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?",
                  (pf["id"], inst["id"]))
    if order["side"] == "buy":
        total = money(gross + fee)
        if total > pf["cash"] + 0.005:
            raise TradeError("Zu wenig Guthaben: benötigt {:.2f} {}, vorhanden {:.2f} {}."
                             .format(total, pf["currency"], pf["cash"], pf["currency"]))
        if hold:
            conn.execute("UPDATE holdings SET quantity=quantity+?, cost=cost+? WHERE portfolio_id=? "
                         "AND instrument_id=?", (qty, total, pf["id"], inst["id"]))
        else:
            conn.execute("INSERT INTO holdings (portfolio_id, instrument_id, quantity, cost) VALUES (?,?,?,?)",
                         (pf["id"], inst["id"], qty, total))
        amount, realized = -total, None
    else:
        if not hold or hold["quantity"] + EPS < qty:
            raise TradeError("Nicht genügend Stück im Depot (vorhanden: {:g}).".format(hold["quantity"] if hold else 0))
        proceeds = money(gross - fee)
        cost_part = hold["cost"] * qty / hold["quantity"]
        realized = money(proceeds - cost_part)
        rest = hold["quantity"] - qty
        if rest <= EPS:
            conn.execute("DELETE FROM holdings WHERE portfolio_id=? AND instrument_id=?", (pf["id"], inst["id"]))
        else:
            conn.execute("UPDATE holdings SET quantity=?, cost=? WHERE portfolio_id=? AND instrument_id=?",
                         (rest, hold["cost"] - cost_part, pf["id"], inst["id"]))
        amount = proceeds
    conn.execute("UPDATE portfolios SET cash=? WHERE id=?", (money(pf["cash"] + amount), pf["id"]))
    conn.execute(
        "INSERT INTO transactions (portfolio_id, instrument_id, order_id, kind, quantity, price, fx, fee, "
        "amount, realized, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (pf["id"], inst["id"], order["id"], order["side"], qty, price, fx, fee, amount, realized, _now()))
    conn.execute("UPDATE orders SET status='filled', closed_at=?, message=? WHERE id=?",
                 (_now(), "Ausgeführt zu {:.4f} {}".format(price, inst["currency"]), order["id"]))


def _prepare(instrument_id, portfolio_currency):
    """Kurs und Wechselkurs auffrischen (Netzwerk, ausserhalb der Transaktion)."""
    inst = quotes.refresh_one(instrument_id)
    fx = quotes.fx_rate(inst["currency"], portfolio_currency)
    return inst, fx


def place_order(portfolio, instrument_id, side, order_type, quantity, limit_price=None, stop_price=None,
                valid_until=None):
    """Legt einen Auftrag an und führt ihn aus, falls möglich. Gibt den Auftrag zurück."""
    if side not in SIDES or order_type not in ORDER_TYPES:
        raise TradeError("Ungültiger Auftrag.")
    if not quantity or quantity <= 0:
        raise TradeError("Bitte eine positive Stückzahl angeben.")
    quantity = round(quantity, 8)
    if order_type == "limit" and not (limit_price and limit_price > 0):
        raise TradeError("Bitte einen Limitkurs angeben.")
    if order_type == "stop" and not (stop_price and stop_price > 0):
        raise TradeError("Bitte einen Stopkurs angeben.")
    if valid_until and valid_until < date.today().isoformat():
        raise TradeError("Das Gültigkeitsdatum liegt in der Vergangenheit.")
    with db.get() as conn:
        if side == "sell":
            hold = db.one(conn, "SELECT quantity FROM holdings WHERE portfolio_id=? AND instrument_id=?",
                          (portfolio["id"], instrument_id))
            pending = conn.execute("SELECT COALESCE(SUM(quantity),0) FROM orders WHERE portfolio_id=? AND "
                                   "instrument_id=? AND side='sell' AND status='open'",
                                   (portfolio["id"], instrument_id)).fetchone()[0]
            have = (hold["quantity"] if hold else 0) - pending
            if quantity > have + EPS:
                raise TradeError("Nicht genügend Stück verfügbar (frei: {:g}).".format(max(have, 0)))
        cur = conn.execute(
            "INSERT INTO orders (portfolio_id, instrument_id, side, order_type, quantity, limit_price, "
            "stop_price, valid_until, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (portfolio["id"], instrument_id, side, order_type, quantity,
             limit_price if order_type == "limit" else None,
             stop_price if order_type == "stop" else None, valid_until or None, _now()))
        order_id = cur.lastrowid
    try:
        inst, fx = _prepare(instrument_id, portfolio["currency"])
    except quotes.QuoteError as e:
        if order_type == "market":
            _close(order_id, "rejected", str(e))
            raise TradeError("Kein aktueller Kurs: {}".format(e))
        return _order(order_id)  # Limit/Stop wartet auf den nächsten Durchlauf
    return _try_fill(order_id, inst, fx, reject_on_error=(order_type == "market"))


def _order(order_id):
    with db.get() as conn:
        return db.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))


def _close(order_id, status, message):
    with db.get() as conn:
        conn.execute("UPDATE orders SET status=?, message=?, closed_at=? WHERE id=? AND status='open'",
                     (status, message, _now(), order_id))


def _try_fill(order_id, inst, fx, reject_on_error=True):
    error = None
    with db.get() as conn:
        order = db.one(conn, "SELECT * FROM orders WHERE id=?", (order_id,))
        if order["status"] != "open" or inst["price"] is None or not triggered(order, inst["price"]):
            return order
        try:
            _execute(conn, order, inst, fx)
        except TradeError as e:
            error = e
    if error:
        if reject_on_error:
            _close(order_id, "rejected", str(error))
            raise error
        with db.get() as conn:
            conn.execute("UPDATE orders SET message=? WHERE id=?", (str(error), order_id))
    return _order(order_id)


def cancel_order(portfolio_id, order_id):
    with db.get() as conn:
        cur = conn.execute("UPDATE orders SET status='cancelled', closed_at=?, message='Vom Benutzer gelöscht' "
                           "WHERE id=? AND portfolio_id=? AND status='open'", (_now(), order_id, portfolio_id))
        if not cur.rowcount:
            raise TradeError("Der Auftrag ist nicht mehr offen.")


def process_open_orders():
    """Prüft alle offenen Limit-/Stop-Aufträge (vom Hintergrunddienst aufgerufen)."""
    today = date.today().isoformat()
    with db.get() as conn:
        conn.execute("UPDATE orders SET status='expired', closed_at=?, message='Gültigkeit abgelaufen' "
                     "WHERE status='open' AND valid_until IS NOT NULL AND valid_until < ?", (_now(), today))
        orders = db.all_(conn, "SELECT o.*, p.currency AS pf_currency FROM orders o JOIN portfolios p "
                               "ON p.id=o.portfolio_id WHERE o.status='open' ORDER BY o.id")
    filled = 0
    for o in orders:
        try:
            inst, fx = _prepare(o["instrument_id"], o["pf_currency"])
        except quotes.QuoteError:
            continue
        if _try_fill(o["id"], inst, fx, reject_on_error=False)["status"] == "filled":
            filled += 1
    return filled


# --------------------------------------------------------------------------- #
# Geldbewegungen
# --------------------------------------------------------------------------- #

def cash_booking(portfolio_id, kind, amount, note=""):
    if kind not in ("deposit", "withdraw"):
        raise TradeError("Ungültige Buchung.")
    amount = money(amount or 0)
    if amount <= 0:
        raise TradeError("Bitte einen positiven Betrag angeben.")
    with db.get() as conn:
        pf = db.one(conn, "SELECT * FROM portfolios WHERE id=?", (portfolio_id,))
        signed = amount if kind == "deposit" else -amount
        if pf["cash"] + signed < -0.005:
            raise TradeError("So viel Guthaben ist nicht vorhanden.")
        conn.execute("UPDATE portfolios SET cash=? WHERE id=?", (money(pf["cash"] + signed), portfolio_id))
        conn.execute("INSERT INTO transactions (portfolio_id, kind, amount, note, created_at) VALUES (?,?,?,?,?)",
                     (portfolio_id, kind, signed, note, _now()))


def book_dividend(portfolio_id, instrument_id, gross, tax=0.0, note="", day=None):
    gross, tax = money(gross or 0), money(tax or 0)
    if gross <= 0:
        raise TradeError("Bitte einen positiven Betrag angeben.")
    if tax < 0 or tax > gross:
        raise TradeError("Die Steuer muss zwischen 0 und dem Bruttobetrag liegen.")
    with db.get() as conn:
        hold = db.one(conn, "SELECT quantity FROM holdings WHERE portfolio_id=? AND instrument_id=?",
                      (portfolio_id, instrument_id))
        known = conn.execute("SELECT 1 FROM transactions WHERE portfolio_id=? AND instrument_id=? LIMIT 1",
                             (portfolio_id, instrument_id)).fetchone()
        if not hold and not known:
            raise TradeError("Dieser Titel war nie im Depot.")
        net = money(gross - tax)
        when = (day + " 12:00:00") if day else _now()
        conn.execute("UPDATE portfolios SET cash=ROUND(cash+?, 2) WHERE id=?", (net, portfolio_id))
        conn.execute("INSERT INTO transactions (portfolio_id, instrument_id, kind, quantity, tax, amount, note, "
                     "created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (portfolio_id, instrument_id, "dividend", hold["quantity"] if hold else None, tax, net,
                      note, when))


# --------------------------------------------------------------------------- #
# Bewertung
# --------------------------------------------------------------------------- #

def ensure_fx(currencies, target, max_age=None):
    """Wechselkurse holen – ohne max_age nur, wenn sie noch ganz fehlen.
    Fehler werden ignoriert (die Anzeige zeigt dann '–')."""
    for c in set(currencies):
        if c != target:
            try:
                quotes.fx_rate(c, target, max_age=10 ** 9 if max_age is None else max_age)
            except quotes.QuoteError:
                pass


def valuation(conn, portfolio):
    """Depot bewerten: Positionen, Werte, Gewinne. Nutzt nur zwischengespeicherte Kurse."""
    pid, ccy = portfolio["id"], portfolio["currency"]
    rows = db.all_(conn, "SELECT h.quantity, h.cost, i.* FROM holdings h JOIN instruments i "
                         "ON i.id=h.instrument_id WHERE h.portfolio_id=? ORDER BY i.name", (pid,))
    positions, total, complete = [], 0.0, True
    for r in rows:
        fx = quotes.cached_fx(conn, r["currency"], ccy)
        value = r["quantity"] * r["price"] * fx if (fx is not None and r["price"] is not None) else None
        pos = dict(r, fx=fx, value=value, avg_price=r["cost"] / r["quantity"] if r["quantity"] else 0)
        if value is None:
            complete = False
            pos.update(gain=None, gain_pct=None, day_change=None, day_pct=None)
        else:
            total += value
            pos["gain"] = value - r["cost"]
            pos["gain_pct"] = pos["gain"] / r["cost"] * 100 if r["cost"] else None
            if r["prev_close"]:
                pos["day_change"] = r["quantity"] * (r["price"] - r["prev_close"]) * fx
                pos["day_pct"] = (r["price"] / r["prev_close"] - 1) * 100
            else:
                pos["day_change"] = pos["day_pct"] = None
        positions.append(pos)
    for p in positions:
        p["weight"] = p["value"] / (total + portfolio["cash"]) * 100 if p["value"] and (total + portfolio["cash"]) else None
    sums = conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN kind IN ('deposit','withdraw') THEN amount END),0), "
        "COALESCE(SUM(realized),0), COALESCE(SUM(CASE WHEN kind='dividend' THEN amount END),0), "
        "COALESCE(SUM(fee),0) FROM transactions WHERE portfolio_id=?", (pid,)).fetchone()
    invested = sums[0]
    value = total + portfolio["cash"]
    cost = sum(p["cost"] for p in positions)
    # Tagesveränderung: Depotwert gestern (letzter Tageswert) + heutige Ein-/Auszahlungen als Basis.
    # So zählen heute gekaufte Titel erst ab dem Kaufkurs.
    midnight = datetime.combine(date.today(), datetime.min.time()).astimezone().astimezone(timezone.utc)
    prev = conn.execute("SELECT value FROM snapshots WHERE portfolio_id=? AND day<? ORDER BY day DESC LIMIT 1",
                        (pid, date.today().isoformat())).fetchone()
    flows = conn.execute("SELECT COALESCE(SUM(amount),0) FROM transactions WHERE portfolio_id=? AND "
                         "kind IN ('deposit','withdraw') AND created_at>=?",
                         (pid, midnight.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()[0]
    if prev:
        day_base = prev[0] + flows
    else:
        # Noch kein Tageswert von gestern (Depot heute angelegt): Basis sind die Einzahlungen, wobei vor
        # heute datierte (nachgetragene) Käufe mit dem Vortageskurs statt dem Einstand zählen.
        day_base = invested
        old = db.all_(conn, "SELECT instrument_id, SUM(quantity) AS qty, -SUM(amount) AS cost FROM transactions "
                            "WHERE portfolio_id=? AND kind='buy' AND created_at<? GROUP BY instrument_id",
                      (pid, midnight.strftime("%Y-%m-%d %H:%M:%S")))
        by_id = {p["id"]: p for p in positions}
        for o in old:
            p = by_id.get(o["instrument_id"])
            if p and p["fx"] is not None and p["price"] is not None:
                ref = p["prev_close"] if p["prev_close"] else p["price"]
                day_base += o["qty"] * ref * p["fx"] - o["cost"]
    day_change = value - day_base if day_base > 0 else 0.0
    return {
        "positions": positions,
        "securities": total,
        "cash": portfolio["cash"],
        "value": value,
        "invested": invested,
        "gain": value - invested,
        "gain_pct": (value - invested) / invested * 100 if invested > 0 else None,
        "unrealized": total - cost,
        "realized": sums[1],
        "dividends": sums[2],
        "fees": sums[3],
        "day_change": day_change if complete else None,
        "day_pct": day_change / day_base * 100 if complete and day_base > 0 else None,
        "complete": complete,
    }


def book_past_buy(portfolio_id, instrument_id, day, quantity, price, fx, fee, funding="deposit", note=""):
    """Einen früheren (z. B. realen) Kauf nachträglich erfassen.
    funding='deposit': Kaufbetrag wird gleichzeitig als Einzahlung gebucht (Bargeld bleibt gleich).
    funding='cash':    Kaufbetrag wird vom vorhandenen Bargeld abgezogen."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", day or ""):
        raise TradeError("Bitte ein gültiges Kaufdatum angeben.")
    if day > date.today().isoformat():
        raise TradeError("Das Kaufdatum liegt in der Zukunft.")
    if day < "1970-01-01":
        raise TradeError("Das Kaufdatum ist zu weit in der Vergangenheit.")
    if not quantity or quantity <= 0:
        raise TradeError("Bitte eine positive Stückzahl angeben.")
    if not price or price <= 0:
        raise TradeError("Bitte den Kaufkurs angeben.")
    if not fx or fx <= 0:
        raise TradeError("Bitte einen gültigen Wechselkurs angeben.")
    if fee is not None and fee < 0:
        raise TradeError("Die Gebühr darf nicht negativ sein.")
    if funding not in ("deposit", "cash"):
        raise TradeError("Ungültige Finanzierung.")
    quantity = round(quantity, 8)
    gross = money(quantity * price * fx)
    when = day + " 12:00:00"
    with db.get() as conn:
        pf = db.one(conn, "SELECT * FROM portfolios WHERE id=?", (portfolio_id,))
        fee = fee_for(pf, gross) if fee is None else money(fee)  # leer = Gebührenregel des Depots
        total = money(gross + fee)
        if funding == "cash" and total > pf["cash"] + 0.005:
            raise TradeError("Zu wenig Bargeld: benötigt {:.2f} {}, vorhanden {:.2f} {}. Wähle «Als Einzahlung "
                             "verbuchen» oder zahle zuerst Geld ein.".format(total, pf["currency"], pf["cash"],
                                                                            pf["currency"]))
        inst = db.one(conn, "SELECT * FROM instruments WHERE id=?", (instrument_id,))
        if funding == "deposit":
            conn.execute("INSERT INTO transactions (portfolio_id, kind, amount, note, created_at) VALUES (?,?,?,?,?)",
                         (portfolio_id, "deposit", total, "Einbuchung für Kauf {}".format(inst["name"]), when))
        else:
            conn.execute("UPDATE portfolios SET cash=? WHERE id=?", (money(pf["cash"] - total), portfolio_id))
        hold = db.one(conn, "SELECT * FROM holdings WHERE portfolio_id=? AND instrument_id=?",
                      (portfolio_id, instrument_id))
        if hold:
            conn.execute("UPDATE holdings SET quantity=quantity+?, cost=cost+? WHERE portfolio_id=? "
                         "AND instrument_id=?", (quantity, total, portfolio_id, instrument_id))
        else:
            conn.execute("INSERT INTO holdings (portfolio_id, instrument_id, quantity, cost) VALUES (?,?,?,?)",
                         (portfolio_id, instrument_id, quantity, total))
        conn.execute(
            "INSERT INTO transactions (portfolio_id, instrument_id, kind, quantity, price, fx, fee, amount, note, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (portfolio_id, instrument_id, "buy", quantity, price, fx, fee, -total,
             ("Nachgetragen. " + note).strip() if note else "Nachgetragen", when))
        conn.execute("INSERT OR IGNORE INTO price_history (instrument_id, day, close) VALUES (?,?,?)",
                     (instrument_id, day, price))
        # Bereits gespeicherte Tageswerte ab dem Kaufdatum nachführen (Grafik, Tagesveränderung)
        for snap in db.all_(conn, "SELECT day FROM snapshots WHERE portfolio_id=? AND day>=?", (portfolio_id, day)):
            row = conn.execute("SELECT close FROM price_history WHERE instrument_id=? AND day<=? "
                               "ORDER BY day DESC LIMIT 1", (instrument_id, snap["day"])).fetchone()
            value = quantity * (row[0] if row else price) * fx
            if funding == "deposit":
                conn.execute("UPDATE snapshots SET value=ROUND(value+?,2), invested=ROUND(invested+?,2) "
                             "WHERE portfolio_id=? AND day=?", (value, total, portfolio_id, snap["day"]))
            else:
                conn.execute("UPDATE snapshots SET value=ROUND(value+?,2) WHERE portfolio_id=? AND day=?",
                             (value - total, portfolio_id, snap["day"]))


def snapshot(conn, portfolio):
    """Heutigen Depotwert für die Grafik speichern."""
    v = valuation(conn, portfolio)
    if not v["complete"]:
        return
    conn.execute("INSERT OR REPLACE INTO snapshots (portfolio_id, day, value, invested) VALUES (?,?,?,?)",
                 (portfolio["id"], date.today().isoformat(), money(v["value"]), money(v["invested"])))


def snapshot_all():
    with db.get() as conn:
        for pf in db.all_(conn, "SELECT * FROM portfolios"):
            snapshot(conn, pf)
