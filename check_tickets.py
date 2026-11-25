#!/usr/bin/env python3
"""
Ticket-Wächter für das Kartenkontingent der WRedSocks'02 (wredsocks.de).

Ruft die Tabelle "Verfügbares Kartenkontingent" ab, vergleicht sie mit dem
letzten Stand (state.json) und schickt eine Push-Nachricht, wenn
  - ein neues Spiel in der Liste auftaucht oder
  - bei einem Spiel in einer Kartenkategorie die Anzahl steigt.
Rückgänge (Karten verkauft) lösen keine Nachricht aus, werden aber gespeichert,
damit ein späterer Anstieg korrekt erkannt wird.

Nur Python-Standardbibliothek, keine Installation nötig.

Aufruf:
  python3 check_tickets.py            normaler Lauf
  python3 check_tickets.py --test     aktuelle Übersicht als Testnachricht schicken
  python3 check_tickets.py --dry-run  nichts senden, Nachrichten nur ausgeben
  python3 check_tickets.py --from-file antwort.xml   Testdaten statt Webseite

Kanäle (als GitHub-Secrets hinterlegt, mindestens einer nötig):
  ntfy:      NTFY_TOPIC        geheimer Kanalname, den du in der ntfy-App abonnierst
             NTFY_SERVER       optional, Standard https://ntfy.sh
             NTFY_TOKEN        optional, nur für geschützte Kanäle/eigene Server
  WhatsApp:  CALLMEBOT_PHONE   eigene WhatsApp-Nummer, z. B. +4917612345678
             CALLMEBOT_APIKEY  API-Key vom CallMeBot
Sind mehrere Kanäle eingerichtet, wird an alle gesendet.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------

PAGE_URL = "https://www.wredsocks.de/hiv/uebersicht_spiele.php"

# Spalten, bei denen ein ANSTIEG eine Nachricht auslöst.
# "nur Bus" ist bewusst nicht dabei (Busplatz ohne Karte). Zum Mitüberwachen
# einfach "nur Bus" ergänzen.
ALERT_ON_INCREASE = ["Sitz Kat2", "Sitz Kat3", "Sitz Kat4", "Steh Kat5"]

STATE_FILE = Path(os.environ.get("STATE_FILE", Path(__file__).with_name("state.json")))

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36"
)

# CallMeBot kommt mit sehr langen Nachrichten nicht gut zurecht -> aufteilen.
MAX_MESSAGE_CHARS = 1200


# --------------------------------------------------------------------------
# Abruf
# --------------------------------------------------------------------------

def fetch_raw() -> str:
    """Holt die Tabelle genau so, wie es die Webseite selbst per xajax tut."""
    body = urllib.parse.urlencode({"xjxfun": "ansichtspiele"}).encode()
    req = urllib.request.Request(
        PAGE_URL,
        data=body,
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": PAGE_URL,
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            return decode(raw)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Webseite nicht erreichbar: {last_error}")


def decode(raw: bytes) -> str:
    # Server meldet ISO-8859-1, liefert aber evtl. UTF-8 -> beides abfangen.
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def extract_html(xml_text: str) -> str:
    """Schneidet das HTML aus der xajax-XML-Antwort (CDATA-Block) heraus."""
    blocks = re.findall(r"<!\[CDATA\[(.*?)\]\]>", xml_text, re.S)
    if not blocks:
        # Falls die Seite irgendwann direkt HTML liefert
        if "Kartenkontingent" in xml_text:
            return xml_text
        raise RuntimeError("Unerwartete Antwort: kein Tabelleninhalt gefunden.")
    content = max(blocks, key=len)
    if content.startswith("S"):  # xajax-Typkennung "S" = String
        content = content[1:]
    return content


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[list[dict]] = []
        self._in_th = False
        self._th_text = ""
        self._row: list[dict] | None = None
        self._cell: dict | None = None
        self._span_depth = 0  # Tooltip-Inhalte (Abfahrt/Info) überspringen

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag == "th":
            self._in_th, self._th_text = True, ""
        elif tag == "td" and self._row is not None:
            self._cell = {"text": "", "links": []}
        elif self._cell is not None:
            if tag == "span":
                self._span_depth += 1
            elif tag == "br" and not self._span_depth:
                self._cell["text"] += " "
            elif tag == "a" and attrs.get("href") and not self._span_depth:
                self._cell["links"].append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "th" and self._in_th:
            self.headers.append(" ".join(self._th_text.split()))
            self._in_th = False
        elif tag == "span" and self._span_depth:
            self._span_depth -= 1
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._cell["text"] = " ".join(self._cell["text"].split())
            self._row.append(self._cell)
            self._cell = None
            self._span_depth = 0
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._in_th:
            self._th_text += data
        elif self._cell is not None and not self._span_depth:
            self._cell["text"] += data


def _to_int(text: str):
    text = text.strip()
    return int(text) if re.fullmatch(r"\d+", text) else text


def parse_games(html_text: str) -> dict[str, dict]:
    if "Kartenkontingent" not in html_text and "tabelle" not in html_text:
        raise RuntimeError("Tabelle 'Verfügbares Kartenkontingent' nicht gefunden.")

    parser = _TableParser()
    parser.feed(html_text)
    headers = parser.headers

    # Keine Tabelle, aber Überschrift vorhanden -> aktuell keine Spiele
    if not headers:
        return {}

    required = ["Datum", "Liga", "Ort", "Begegnung"]
    missing = [h for h in required if h not in headers]
    if missing:
        raise RuntimeError(f"Tabellenaufbau hat sich geändert, fehlende Spalten: {missing}")

    categories = [h for h in headers if h and h not in required]
    games: dict[str, dict] = {}
    for row in parser.rows:
        cells = {h: row[i] for i, h in enumerate(headers) if i < len(row) and h}
        if not cells.get("Datum"):
            continue
        link = next((l for c in row for l in c["links"] if "kartenkauf" in l), None)
        game = {
            "datum": cells["Datum"]["text"],
            "liga": cells["Liga"]["text"],
            "ort": cells["Ort"]["text"],
            "begegnung": re.sub(r"\s*-\s*", " - ", cells["Begegnung"]["text"]).strip(" -"),
            "karten": {c: _to_int(cells[c]["text"]) for c in categories if c in cells},
            "link": link,
        }
        nr = re.search(r"nr=(\d+)", link or "")
        key = f"nr{nr.group(1)}" if nr else f"{game['datum']}|{game['begegnung']}"
        games[key] = game
    return games


# --------------------------------------------------------------------------
# Vergleich
# --------------------------------------------------------------------------

def _num(value) -> int:
    return value if isinstance(value, int) else 0


def find_alerts(old: dict[str, dict], new: dict[str, dict]) -> list[tuple]:
    alerts = []
    for key, game in new.items():
        prev = old.get(key)
        if prev is None:
            alerts.append(("neu", game, []))
            continue
        ups = []
        for cat in ALERT_ON_INCREASE:
            before = _num(prev.get("karten", {}).get(cat))
            after = _num(game["karten"].get(cat))
            if after > before:
                ups.append((cat, before, after))
        if ups:
            alerts.append(("mehr", game, ups))
    return alerts


# --------------------------------------------------------------------------
# Nachrichten
# --------------------------------------------------------------------------

def _label(cat: str) -> str:
    return cat.replace("Sitz ", "").replace("Steh Kat5", "Kat5 Steh")


def _counts_line(game: dict) -> str:
    return " | ".join(f"{_label(c)}: {v}" for c, v in game["karten"].items())


def alert_notification(kind: str, game: dict, ups: list) -> dict:
    when = f"{game['datum']} · {game['liga']} · {game['ort']}"
    if kind == "neu":
        title = f"Neues Spiel: {game['begegnung']}"
        body = f"{when}\n{_counts_line(game)}"
    else:
        title = f"Mehr Karten: {game['begegnung']}"
        body = when + "\n" + "\n".join(f"{_label(c)}: {a} → {b}" for c, a, b in ups)
    return {"title": title, "body": body, "link": game.get("link"), "priority": 4}


def overview_notification(games: dict[str, dict], title: str) -> dict:
    parts = []
    if not games:
        parts.append("Zurzeit sind keine Spiele gelistet.")
    for g in games.values():
        parts.append(f"{g['datum']} {g['liga']}: {g['begegnung']}\n{_counts_line(g)}")
    parts.append("Du bekommst eine Nachricht bei neuen Spielen und wenn Karten dazukommen.")
    return {"title": title, "body": "\n\n".join(parts), "link": None, "priority": 3}


def _sanitize(text: str) -> str:
    """Für Logs (in öffentlichen Repos sichtbar): HTML und Telefonnummern entfernen."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\+?\d[\d\s]{5,}\d", "[…]", text)
    return " ".join(text.split())[:300]


# --------------------------------------------------------------------------
# Kanal: ntfy (Push-App)
# --------------------------------------------------------------------------

def ntfy_configured() -> bool:
    return bool(os.environ.get("NTFY_TOPIC", "").strip())


def send_ntfy(notes: list[dict]) -> None:
    server = (os.environ.get("NTFY_SERVER", "").strip() or "https://ntfy.sh").rstrip("/")
    topic = os.environ["NTFY_TOPIC"].strip()
    token = os.environ.get("NTFY_TOKEN", "").strip()
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    for i, note in enumerate(notes):
        if i:
            time.sleep(1)
        payload = {
            "topic": topic,
            "title": note["title"],
            "message": note["body"],
            "priority": note["priority"],
            "tags": ["ticket"],
            "click": note["link"] or PAGE_URL,
        }
        if note["link"]:
            payload["actions"] = [{"action": "view", "label": "Bestellen", "url": note["link"]}]
        req = urllib.request.Request(
            server, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as exc:
            answer = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ntfy-Fehler {exc.code}: {_sanitize(answer)}") from None
        except urllib.error.URLError as exc:
            raise RuntimeError(f"ntfy nicht erreichbar: {exc.reason}") from None
    print(f"ntfy: {len(notes)} Push-Nachricht(en) gesendet.")


# --------------------------------------------------------------------------
# Kanal: WhatsApp über CallMeBot (optional)
# --------------------------------------------------------------------------

def callmebot_configured() -> bool:
    return bool(
        os.environ.get("CALLMEBOT_PHONE", "").strip()
        and os.environ.get("CALLMEBOT_APIKEY", "").strip()
    )


def chunk_messages(blocks: list[str]) -> list[str]:
    footer = f"\n\nÜbersicht: {PAGE_URL}"
    messages, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if current and len(candidate) > MAX_MESSAGE_CHARS:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    if messages:
        messages[-1] += footer
    return messages


def _callmebot_request(text: str) -> None:
    query = urllib.parse.urlencode({
        "phone": os.environ["CALLMEBOT_PHONE"].strip(),
        "text": text,
        "apikey": os.environ["CALLMEBOT_APIKEY"].strip(),
    })
    req = urllib.request.Request(
        f"https://api.callmebot.com/whatsapp.php?{query}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            answer = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        answer = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CallMeBot-Fehler {exc.code}: {_sanitize(answer)}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CallMeBot nicht erreichbar: {exc.reason}") from None

    lowered = answer.lower()
    if "queued" in lowered:
        print("WhatsApp an CallMeBot übergeben.")
    elif "apikey" in lowered and ("invalid" in lowered or "not valid" in lowered):
        raise RuntimeError(f"CallMeBot: API-Key ungültig: {_sanitize(answer)}")
    else:
        print(f"Hinweis: unbekannte CallMeBot-Antwort: {_sanitize(answer)}")


def send_callmebot(notes: list[dict]) -> None:
    blocks = [
        f"*{n['title']}*\n{n['body']}" + (f"\nBestellung: {n['link']}" if n["link"] else "")
        for n in notes
    ]
    for i, msg in enumerate(chunk_messages(blocks)):
        if i:
            time.sleep(5)  # CallMeBot mag keine schnellen Serien
        _callmebot_request(msg)


# --------------------------------------------------------------------------
# Versand an alle eingerichteten Kanäle
# --------------------------------------------------------------------------

CHANNELS = [
    ("ntfy", ntfy_configured, send_ntfy),
    ("WhatsApp (CallMeBot)", callmebot_configured, send_callmebot),
]


def deliver(notes: list[dict], dry_run: bool) -> bool:
    """Sendet an alle Kanäle. True = alle ok, False = mindestens einer fehlgeschlagen.

    Schlagen ALLE Kanäle fehl, wird abgebrochen (Stand wird dann nicht
    gespeichert, der nächste Lauf versucht es erneut).
    """
    if dry_run:
        for n in notes:
            print(f"----- {n['title']} -----\n{n['body']}")
            if n["link"]:
                print(f"[Bestellen] {n['link']}")
        return True

    active = [(name, send) for name, configured, send in CHANNELS if configured()]
    if not active:
        raise RuntimeError(
            "Kein Kanal eingerichtet: Secret NTFY_TOPIC (oder CALLMEBOT_PHONE + "
            "CALLMEBOT_APIKEY) fehlt."
        )
    failed = []
    for name, send in active:
        try:
            send(notes)
        except RuntimeError as exc:
            print(f"WARNUNG – {name}: {exc}", file=sys.stderr)
            failed.append(name)
    if len(failed) == len(active):
        raise RuntimeError("Versand über alle Kanäle fehlgeschlagen.")
    return not failed


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict[str, dict] | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text(encoding="utf-8")).get("spiele", {})


def merge_state(old: dict[str, dict], new: dict[str, dict]) -> dict[str, dict]:
    """Aktueller Stand + kurz verschwundene, noch nicht gespielte Spiele.

    So löst ein Spiel, das nur vorübergehend aus der Liste fällt, beim
    Wiederauftauchen nicht fälschlich "Neues Spiel" aus.
    """
    today = datetime.date.today()
    merged = dict(new)
    for key, game in old.items():
        if key in merged:
            continue
        try:
            played = datetime.datetime.strptime(game["datum"], "%d.%m.%Y").date() < today
        except (KeyError, ValueError):
            played = True
        if not played:
            merged[key] = game
    return merged


def save_state(games: dict[str, dict]) -> None:
    STATE_FILE.write_text(
        json.dumps({"spiele": games}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true", help="aktuelle Übersicht als Testnachricht senden")
    ap.add_argument("--dry-run", action="store_true", help="nichts senden, nur ausgeben")
    ap.add_argument("--from-file", type=Path, help="xajax-Antwort aus Datei statt von der Webseite")
    args = ap.parse_args()

    raw = decode(args.from_file.read_bytes()) if args.from_file else fetch_raw()
    games = parse_games(extract_html(raw))

    print(f"{len(games)} Spiel(e) gefunden:")
    for g in games.values():
        print(f"  {g['datum']} {g['liga']:<4} {g['begegnung']:<35} {_counts_line(g)}")

    old = load_state()

    if args.test or old is None:
        title = "Ticket-Wächter: Test" if args.test else "Ticket-Wächter ist aktiv"
        all_ok = deliver([overview_notification(games, title)], args.dry_run)
        if old is None and not args.dry_run:
            save_state(games)
            print("Erster Lauf: Ausgangsstand gespeichert.")
    else:
        alerts = find_alerts(old, games)
        all_ok = True
        if alerts:
            print(f"{len(alerts)} Meldung(en) – sende Benachrichtigung …")
            all_ok = deliver([alert_notification(*a) for a in alerts], args.dry_run)
        else:
            print("Keine neuen Spiele, keine zusätzlichen Karten.")

        # Erst NACH dem Versand speichern -> scheitern alle Kanäle, nächster Versuch.
        merged = merge_state(old, games)
        if merged != old and not args.dry_run:
            save_state(merged)
            print("Neuer Stand gespeichert.")

    if not all_ok:
        # Stand ist gespeichert (keine Doppelmeldungen), aber der Lauf wird rot,
        # damit GitHub dich per Mail auf den kaputten Kanal hinweist.
        print("Mindestens ein Kanal ist fehlgeschlagen, siehe WARNUNG oben.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        sys.exit(1)
