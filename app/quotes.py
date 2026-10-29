"""Kursdaten: Yahoo Finance (Aktien, ETFs, Fonds), CoinGecko (Kryptowährungen),
manuelle Kurse sowie Wechselkurse. Netzwerkzugriffe nie innerhalb einer DB-Transaktion."""
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import config, db

log = logging.getLogger("musterdepot.quotes")

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Musterdepot/1.0"
YAHOO = "https://query1.finance.yahoo.com"
YAHOO_SEARCH = "https://query2.finance.yahoo.com/v1/finance/search"
COINGECKO = "https://api.coingecko.com/api/v3"

KIND_LABELS = {"stock": "Aktie", "etf": "ETF", "fund": "Fonds", "crypto": "Krypto", "other": "Sonstiges"}
PROVIDER_LABELS = {"yahoo": "Yahoo Finance", "coingecko": "CoinGecko", "manual": "Manuell"}
YAHOO_KINDS = {"EQUITY": "stock", "ETF": "etf", "MUTUALFUND": "fund", "CRYPTOCURRENCY": "crypto"}
# Yahoo liefert manche Börsen in Hundertsteln (Pence, Cent, Agorot)
MINOR_UNITS = {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}

FX_MAX_AGE = 3600


class QuoteError(Exception):
    pass


# Nach "Too Many Requests" eine Weile Ruhe geben
_cooldown = {}
_cooldown_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ts(epoch):
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _get_json(url, params=None, headers=None, service=""):
    if config.OFFLINE:
        raise QuoteError("Offline-Modus: keine Kursabfrage im Internet")
    with _cooldown_lock:
        until = _cooldown.get(service, 0)
    if until > time.monotonic():
        raise QuoteError("{} hat zu viele Anfragen gemeldet – kurze Pause".format(service))
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept": "application/json"},
                                                   **(headers or {})))
    try:
        with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            with _cooldown_lock:
                _cooldown[service] = time.monotonic() + 300
            raise QuoteError("{}: zu viele Anfragen (429)".format(service))
        if e.code == 404:
            raise QuoteError("{}: Titel nicht gefunden".format(service))
        raise QuoteError("{}: HTTP-Fehler {}".format(service, e.code))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise QuoteError("{}: nicht erreichbar ({})".format(service, e))


def _cg_headers():
    return {"x-cg-demo-api-key": config.COINGECKO_API_KEY} if config.COINGECKO_API_KEY else {}


# --------------------------------------------------------------------------- #
# Suche
# --------------------------------------------------------------------------- #

def search(query):
    """Sucht bei Yahoo und CoinGecko. Gibt (Treffer, Fehlermeldungen) zurück."""
    results, errors = [], []
    query = query.strip()
    if not query:
        return results, errors
    try:
        data = _get_json(YAHOO_SEARCH, {"q": query, "quotesCount": 15, "newsCount": 0, "listsCount": 0},
                         service="Yahoo Finance")
        for q in data.get("quotes", []):
            kind = YAHOO_KINDS.get(q.get("quoteType", ""))
            if not kind or not q.get("symbol"):
                continue
            results.append({
                "provider": "yahoo", "symbol": q["symbol"], "ticker": q["symbol"],
                "name": q.get("longname") or q.get("shortname") or q["symbol"],
                "kind": kind, "exchange": q.get("exchDisp") or q.get("exchange", ""),
            })
    except QuoteError as e:
        errors.append(str(e))
    try:
        data = _get_json(COINGECKO + "/search", {"query": query}, _cg_headers(), service="CoinGecko")
        for c in data.get("coins", [])[:8]:
            results.append({
                "provider": "coingecko", "symbol": c["id"], "ticker": (c.get("symbol") or "").upper(),
                "name": c.get("name") or c["id"], "kind": "crypto",
                "exchange": "Rang {}".format(c["market_cap_rank"]) if c.get("market_cap_rank") else "",
            })
    except QuoteError as e:
        errors.append(str(e))
    return results, errors


# --------------------------------------------------------------------------- #
# Yahoo
# --------------------------------------------------------------------------- #

def _yahoo_chart(symbol, rng, interval="1d"):
    data = _get_json(YAHOO + "/v8/finance/chart/" + urllib.parse.quote(symbol),
                     {"range": rng, "interval": interval}, service="Yahoo Finance")
    chart = data.get("chart") or {}
    if chart.get("error") or not chart.get("result"):
        err = (chart.get("error") or {}).get("description") or "keine Daten"
        raise QuoteError("Yahoo Finance: " + err)
    return chart["result"][0]


def _yahoo_closes(result):
    """[(tag, schlusskurs)] aus einer Chart-Antwort."""
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    offset = (result.get("meta") or {}).get("gmtoffset") or 0
    out = {}
    for t, c in zip(stamps, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(int(t) + offset, timezone.utc).strftime("%Y-%m-%d")
        out[day] = float(c)
    return sorted(out.items())


def parse_yahoo_quote(result):
    meta = result.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        raise QuoteError("Yahoo Finance: kein Kurs")
    currency = meta.get("currency") or "USD"
    factor = 1.0
    if currency in MINOR_UNITS:
        currency, factor = MINOR_UNITS[currency], 0.01
    closes = _yahoo_closes(result)
    market_time = meta.get("regularMarketTime")
    prev = None
    if market_time:
        offset = meta.get("gmtoffset") or 0
        today = datetime.fromtimestamp(int(market_time) + offset, timezone.utc).strftime("%Y-%m-%d")
        earlier = [c for d, c in closes if d < today]
        if earlier:
            prev = earlier[-1]
    if prev is None:
        prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    return {
        "price": float(price) * factor,
        "prev_close": float(prev) * factor if prev else None,
        "price_time": _ts(market_time) if market_time else _now(),
        "currency": currency,
        "name": meta.get("longName") or meta.get("shortName") or "",
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName") or "",
        "kind": YAHOO_KINDS.get(meta.get("instrumentType", "")),
        "factor": factor,
    }


def parse_yahoo_history(result):
    meta = result.get("meta") or {}
    factor = 0.01 if meta.get("currency") in MINOR_UNITS else 1.0
    return [(d, c * factor) for d, c in _yahoo_closes(result)]


# --------------------------------------------------------------------------- #
# CoinGecko (Preise immer in USD)
# --------------------------------------------------------------------------- #

def _coingecko_quotes(ids):
    data = _get_json(COINGECKO + "/simple/price", {
        "ids": ",".join(ids), "vs_currencies": "usd",
        "include_24hr_change": "true", "include_last_updated_at": "true",
    }, _cg_headers(), service="CoinGecko")
    out = {}
    for cid, v in data.items():
        if "usd" not in v:
            continue
        price = float(v["usd"])
        chg = v.get("usd_24h_change")
        out[cid] = {
            "price": price,
            "prev_close": price / (1 + chg / 100) if chg not in (None, -100) else None,
            "price_time": _ts(v["last_updated_at"]) if v.get("last_updated_at") else _now(),
            "currency": "USD",
        }
    return out


def _coingecko_history(cid):
    data = _get_json(COINGECKO + "/coins/{}/market_chart".format(urllib.parse.quote(cid)),
                     {"vs_currency": "usd", "days": 365, "interval": "daily"}, _cg_headers(),
                     service="CoinGecko")
    out = {}
    for ms, price in data.get("prices", []):
        out[datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")] = float(price)
    return sorted(out.items())


# --------------------------------------------------------------------------- #
# Titel anlegen und aktualisieren
# --------------------------------------------------------------------------- #

def get_or_create(provider, symbol, ticker="", name="", kind="stock", exchange=""):
    """Legt einen Titel an (mit erster Kursabfrage) oder gibt den vorhandenen zurück."""
    with db.get() as conn:
        row = db.one(conn, "SELECT * FROM instruments WHERE provider=? AND symbol=?", (provider, symbol))
    if row:
        return row
    if provider == "yahoo":
        q = parse_yahoo_quote(_yahoo_chart(symbol, "5d"))
        currency = q["currency"]
        name = q["name"] or name or symbol
        kind = q["kind"] or kind
        exchange = q["exchange"] or exchange
    elif provider == "coingecko":
        q = _coingecko_quotes([symbol]).get(symbol)
        if not q:
            raise QuoteError("CoinGecko: kein Kurs für " + symbol)
        currency, kind = "USD", "crypto"
    else:
        raise QuoteError("Unbekannte Kursquelle")
    with db.get() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO instruments (provider, symbol, ticker, name, kind, currency, exchange, "
            "price, prev_close, price_time, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (provider, symbol, ticker or symbol, name or symbol, kind, currency, exchange,
             q["price"], q["prev_close"], q["price_time"], _now()))
        return db.one(conn, "SELECT * FROM instruments WHERE provider=? AND symbol=?", (provider, symbol))


def refresh(instruments, max_age=None):
    """Aktualisiert die Kurse der übergebenen Titel (Liste von dicts). Gibt Fehler pro ID zurück."""
    max_age = config.QUOTE_MAX_AGE if max_age is None else max_age
    now = datetime.now(timezone.utc)

    def stale(i):
        if not i.get("updated_at"):
            return True
        t = datetime.strptime(i["updated_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (now - t).total_seconds() >= max_age

    todo = [i for i in instruments if i["provider"] != "manual" and stale(i)]
    results, errors = {}, {}
    for i in [i for i in todo if i["provider"] == "yahoo"]:
        try:
            results[i["id"]] = parse_yahoo_quote(_yahoo_chart(i["symbol"], "5d"))
        except QuoteError as e:
            errors[i["id"]] = str(e)
    crypto = [i for i in todo if i["provider"] == "coingecko"]
    for start in range(0, len(crypto), 100):
        chunk = crypto[start:start + 100]
        try:
            got = _coingecko_quotes([i["symbol"] for i in chunk])
            for i in chunk:
                if i["symbol"] in got:
                    results[i["id"]] = got[i["symbol"]]
                else:
                    errors[i["id"]] = "CoinGecko: kein Kurs"
        except QuoteError as e:
            for i in chunk:
                errors[i["id"]] = str(e)
    if results or errors:
        with db.get() as conn:
            for iid, q in results.items():
                conn.execute("UPDATE instruments SET price=?, prev_close=?, price_time=?, updated_at=?, "
                             "last_error='' WHERE id=?",
                             (q["price"], q["prev_close"], q["price_time"], _now(), iid))
                # Tageskurs auch in den Verlauf schreiben
                conn.execute("INSERT OR REPLACE INTO price_history (instrument_id, day, close) VALUES (?,?,?)",
                             (iid, q["price_time"][:10], q["price"]))
            for iid, err in errors.items():
                conn.execute("UPDATE instruments SET last_error=? WHERE id=?", (err, iid))
    return errors


def refresh_one(instrument_id, max_age=None):
    with db.get() as conn:
        inst = db.one(conn, "SELECT * FROM instruments WHERE id=?", (instrument_id,))
    if not inst:
        raise QuoteError("Titel nicht gefunden")
    refresh([inst], max_age)
    with db.get() as conn:
        return db.one(conn, "SELECT * FROM instruments WHERE id=?", (instrument_id,))


def load_history(instrument_id, force=False):
    """Holt den Kursverlauf (höchstens einmal pro Tag)."""
    with db.get() as conn:
        inst = db.one(conn, "SELECT * FROM instruments WHERE id=?", (instrument_id,))
    if not inst or inst["provider"] == "manual":
        return None
    if not force and inst.get("history_at") and inst["history_at"][:10] == _now()[:10]:
        return None
    try:
        if inst["provider"] == "yahoo":
            rows = parse_yahoo_history(_yahoo_chart(inst["symbol"], "5y"))
        else:
            rows = _coingecko_history(inst["symbol"])
    except QuoteError as e:
        return str(e)
    with db.get() as conn:
        conn.executemany("INSERT OR REPLACE INTO price_history (instrument_id, day, close) VALUES (?,?,?)",
                         [(instrument_id, d, c) for d, c in rows])
        conn.execute("UPDATE instruments SET history_at=? WHERE id=?", (_now(), instrument_id))
    return None


# --------------------------------------------------------------------------- #
# Wechselkurse
# --------------------------------------------------------------------------- #

def _fetch_fx(src, dst):
    try:
        q = parse_yahoo_quote(_yahoo_chart("{}{}=X".format(src, dst), "1d"))
        return q["price"]
    except QuoteError as yahoo_err:
        try:
            # Ersatz: CoinGecko liefert Kurse relativ zu Bitcoin -> Kreuzkurs
            rates = _get_json(COINGECKO + "/exchange_rates", headers=_cg_headers(), service="CoinGecko")["rates"]
            a, b = rates[src.lower()]["value"], rates[dst.lower()]["value"]
            return float(b) / float(a)
        except (QuoteError, KeyError, TypeError, ZeroDivisionError):
            raise QuoteError("Wechselkurs {}/{} nicht verfügbar ({})".format(src, dst, yahoo_err))


def fx_rate(src, dst, max_age=FX_MAX_AGE):
    """Umrechnungskurs von src nach dst (1 src = x dst). Nutzt den Zwischenspeicher."""
    if src == dst:
        return 1.0
    pair = src + dst
    with db.get() as conn:
        row = db.one(conn, "SELECT * FROM fx_rates WHERE pair=?", (pair,))
    if row:
        age = (datetime.now(timezone.utc) - datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S")
               .replace(tzinfo=timezone.utc)).total_seconds()
        if age < max_age:
            return row["rate"]
    try:
        rate = _fetch_fx(src, dst)
    except QuoteError:
        if row:  # lieber ein etwas älterer Kurs als gar keiner
            return row["rate"]
        raise
    with db.get() as conn:
        conn.execute("INSERT OR REPLACE INTO fx_rates (pair, rate, updated_at) VALUES (?,?,?)",
                     (pair, rate, _now()))
    return rate


def cached_fx(conn, src, dst):
    """Nur aus dem Zwischenspeicher (für Anzeigen ohne Netzwerkzugriff). None, wenn unbekannt."""
    if src == dst:
        return 1.0
    row = conn.execute("SELECT rate FROM fx_rates WHERE pair=?", (src + dst,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT rate FROM fx_rates WHERE pair=?", (dst + src,)).fetchone()
    return 1 / row[0] if row and row[0] else None
