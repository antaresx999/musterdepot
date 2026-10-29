"""Hintergrunddienst: Kurse holen, Limit-/Stop-Aufträge prüfen, Tageswerte speichern.
Läuft als Thread im Webserver. Eine Sperrdatei sorgt dafür, dass bei mehreren
Server-Prozessen nur einer davon arbeitet."""
import fcntl
import logging
import threading

from . import config, db, quotes, trading

log = logging.getLogger("musterdepot.worker")

_stop = threading.Event()
_lock_file = None


def run_once():
    with db.get() as conn:
        active = db.all_(conn, """
            SELECT * FROM instruments WHERE id IN (
                SELECT instrument_id FROM holdings
                UNION SELECT instrument_id FROM orders WHERE status='open'
                UNION SELECT instrument_id FROM watchlist)""")
        currencies = db.all_(conn, "SELECT DISTINCT currency FROM portfolios")
    errors = quotes.refresh(active, max_age=max(30, config.REFRESH_SECONDS - 30))
    if errors:
        log.info("Kursabfrage: %d Fehler (z. B. %s)", len(errors), next(iter(errors.values())))
    inst_ccy = {i["currency"] for i in active}
    for row in currencies:
        trading.ensure_fx(inst_ccy, row["currency"], max_age=quotes.FX_MAX_AGE)
    filled = trading.process_open_orders()
    if filled:
        log.info("%d Auftrag/Aufträge ausgeführt", filled)
    trading.snapshot_all()


def _loop():
    while not _stop.is_set():
        try:
            run_once()
        except Exception:  # der Dienst darf nie sterben
            log.exception("Fehler im Hintergrunddienst")
        _stop.wait(config.REFRESH_SECONDS)


def start():
    """Startet den Thread, falls dieser Prozess die Sperre bekommt."""
    global _lock_file
    _lock_file = open(config.DATA_DIR / "worker.lock", "w")
    try:
        fcntl.flock(_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        _lock_file.close()
        _lock_file = None
        return False
    threading.Thread(target=_loop, name="musterdepot-worker", daemon=True).start()
    log.info("Hintergrunddienst gestartet (alle %d s)", config.REFRESH_SECONDS)
    return True


def stop():
    _stop.set()
