"""Auswertung von Yahoo-Antworten (Beispieldaten, ohne Internet)."""
from app import quotes


def chart(currency="CHF", price=101.5, closes=(99.0, 100.0, 101.5)):
    day = 86400
    start = 1761782400  # 30.10.2025 00:00 UTC
    return {
        "meta": {"currency": currency, "symbol": "TEST.SW", "regularMarketPrice": price,
                 "regularMarketTime": start + 2 * day + 15 * 3600, "chartPreviousClose": 98.0,
                 "gmtoffset": 3600, "longName": "Test AG", "fullExchangeName": "SIX Swiss",
                 "instrumentType": "EQUITY"},
        "timestamp": [start + i * day + 8 * 3600 for i in range(len(closes))],
        "indicators": {"quote": [{"close": list(closes)}]},
    }


def test_parse_quote():
    q = quotes.parse_yahoo_quote(chart())
    assert q["price"] == 101.5
    assert q["prev_close"] == 100.0          # Schluss des Vortags, nicht chartPreviousClose
    assert q["currency"] == "CHF" and q["kind"] == "stock" and q["name"] == "Test AG"


def test_parse_pence():
    q = quotes.parse_yahoo_quote(chart("GBp", 2500.0, (2400.0, 2450.0, 2500.0)))
    assert q["currency"] == "GBP" and q["price"] == 25.0 and q["prev_close"] == 24.5
    hist = quotes.parse_yahoo_history(chart("GBp", 2500.0, (2400.0, None, 2500.0)))
    assert hist == [("2025-10-30", 24.0), ("2025-11-01", 25.0)]
