# Musterdepot

Selbst gehostete Webanwendung zum **Üben des Wertschriftenhandels ohne echtes Geld**.
Mehrere Benutzer verwalten eigene Musterdepots und handeln **Aktien, ETFs, Fonds und Kryptowährungen**
zu echten Kursen.

*A self-hosted paper-trading web app (stocks, ETFs, funds, crypto) with multi-user support. UI in German.*

## Funktionen

- **Mehrere Benutzer** mit Anmeldung; Administratoren verwalten Benutzer, jeder sieht nur die eigenen Depots
- **Beliebig viele Depots** pro Benutzer, je mit eigener Währung (CHF, EUR, USD) und Startkapital
- **Kurse aus dem Internet**: Yahoo Finance (Aktien, ETFs, Fonds weltweit, z. B. SIX, XETRA, NYSE) und
  CoinGecko (Kryptowährungen) – ohne API-Schlüssel
- **Automatische Währungsumrechnung** (z. B. US-Aktie im CHF-Depot)
- **Aufträge**: Bestens/Billigst (sofort), **Limit** und **Stop** (Stop-Loss / Stop-Buy) mit optionalem Gültigkeitsdatum
- **Gebühren pro Depot**: fix + Prozent vom Kurswert, mit Mindestgebühr
- **Dividenden** von Hand gutschreiben (pro Stück oder gesamt, mit Quellensteuer)
- **Ein- und Auszahlungen**, die nicht als Gewinn zählen
- **Auswertung**: Depotwert, Gewinn/Verlust (offen und realisiert), Tagesveränderung, Aufteilung nach Anlageart
- **Grafiken**: Depotwert über die Zeit und Kursverlauf pro Titel (bis 5 Jahre)
- **Beobachtungsliste**, Buchungsjournal mit **CSV-Export**
- **Manuelle Titel** mit selbst gesetztem Kurs (für Übungen oder wenn keine Internet-Kurse verfügbar sind)
- Hell/Dunkel automatisch, funktioniert auf dem Handy

Ein Hintergrunddienst holt alle 5 Minuten die Kurse aller Titel, die in einem Depot liegen, einen offenen
Auftrag haben oder beobachtet werden. Dabei prüft er die Limit- und Stop-Aufträge und speichert den
Tageswert jedes Depots.

> **Hinweis:** Das ist eine Simulation. Ausgeführt wird zum zuletzt abgefragten Kurs; Börsenzeiten,
> Geld-/Briefkurse und Teilausführungen werden nicht nachgebildet. Die Kursdaten von Yahoo Finance und
> CoinGecko sind ohne Gewähr und nur für den privaten Gebrauch gedacht.

## Installation auf einem Linux-Server (Debian/Ubuntu)

Funktioniert auf einem normalen Server, einer VM oder in einem LXC-Container (z. B. Proxmox).
Benötigt wird nur Python 3.9 oder neuer – das Skript installiert alles Nötige.

```bash
apt install -y git
git clone https://github.com/antaresx999/musterdepot.git
cd musterdepot
sudo ./deploy/install.sh
```

Das Skript

1. legt den Systembenutzer `musterdepot` an,
2. kopiert das Programm nach `/opt/musterdepot` (mit eigener Python-Umgebung),
3. legt die Einstellungen in `/etc/musterdepot.env` ab,
4. richtet den **systemd-Dienst** `musterdepot` ein und startet ihn,
5. zeigt am Ende die Adresse und das erste Admin-Passwort an.

Danach im Browser `http://<server>:8080` öffnen, mit `admin` anmelden und unter *Mein Konto* das Passwort
ändern. Unter *Benutzer* legst du weitere Konten an.

### Update

```bash
cd musterdepot
git pull
sudo ./deploy/install.sh
```

Daten (`/var/lib/musterdepot`) und Einstellungen (`/etc/musterdepot.env`) bleiben dabei erhalten.

### Dienst bedienen

```bash
systemctl status musterdepot      # läuft er?
systemctl restart musterdepot     # nach Änderungen an /etc/musterdepot.env
journalctl -u musterdepot -f      # Protokoll live ansehen
```

### Einstellungen (`/etc/musterdepot.env`)

| Variable | Standard | Bedeutung |
|---|---|---|
| `MUSTERDEPOT_HOST` / `MUSTERDEPOT_PORT` | `0.0.0.0` / `8080` | Adresse und Port |
| `MUSTERDEPOT_SITE_NAME` | `Musterdepot` | Name der Seite |
| `MUSTERDEPOT_ADMIN_USER` / `MUSTERDEPOT_ADMIN_PASSWORD` | `admin` / *(zufällig)* | Erstes Admin-Konto |
| `MUSTERDEPOT_COOKIE_SECURE` | `0` | `1`, wenn die Seite nur über HTTPS läuft |
| `MUSTERDEPOT_REFRESH_SECONDS` | `300` | Abstand der automatischen Kursabfragen |
| `MUSTERDEPOT_COINGECKO_KEY` | *(leer)* | Optionaler kostenloser Demo-Schlüssel von CoinGecko |
| `MUSTERDEPOT_OFFLINE` | `0` | `1` = keine Internet-Kurse, nur manuelle Titel |

### Hinter einem Reverse Proxy (HTTPS)

Musterdepot versteht die üblichen `X-Forwarded-*`-Header. Beispiel für Caddy:

```
depot.example.ch {
    reverse_proxy 127.0.0.1:8080
}
```

Dann in `/etc/musterdepot.env` `MUSTERDEPOT_COOKIE_SECURE=1` setzen und den Dienst neu starten.

### Sicherung

Alle Daten liegen in einer einzigen SQLite-Datei. Eine konsistente Sicherung im laufenden Betrieb:

```bash
sqlite3 /var/lib/musterdepot/musterdepot.db ".backup '/root/musterdepot-$(date +%F).db'"
```

## Entwicklung

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt pytest httpx
MUSTERDEPOT_DATA=./data MUSTERDEPOT_ADMIN_PASSWORD=testadmin123 venv/bin/uvicorn app.main:app --reload --port 8081
venv/bin/python -m pytest -q
```

Aufbau: Python/FastAPI, Jinja-Vorlagen, SQLite, kein JavaScript-Framework (Grafiken als eigenes SVG).

| Datei | Inhalt |
|---|---|
| `app/main.py` | Start, Sitzungen, erstes Admin-Konto |
| `app/routes.py` | Alle Seiten und Formulare |
| `app/trading.py` | Aufträge, Gebühren, Dividenden, Bewertung |
| `app/quotes.py` | Kurse von Yahoo Finance / CoinGecko, Wechselkurse |
| `app/worker.py` | Hintergrunddienst (Kurse, Limit/Stop, Tageswerte) |
| `app/db.py` | Datenbankschema |
| `deploy/` | Installationsskript, systemd-Dienst, Beispiel-Einstellungen |

## Lizenz

[GNU General Public License v3.0](LICENSE)
