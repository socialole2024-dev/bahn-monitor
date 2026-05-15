"""
Bahn-Monitor ÃÂ¢ÃÂÃÂ prueft die S4 zwischen Bissendorf, Wedemark und Hannover Hbf
auf Ausfaelle und Verspaetungen >= DELAY_THRESHOLD_MIN Minuten und schickt
eine Mail per Gmail-SMTP.

Datenquelle: db-rest /journeys (https://v6.db.transport.rest), eine offene HAFAS-Bridge.
Laeuft alle 15 Minuten via GitHub Actions.
"""

from __future__ import annotations
import json, os, smtplib, sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
import urllib.request, urllib.parse, urllib.error

try:
    from zoneinfo import ZoneInfo
    BERLIN_TZ = ZoneInfo("Europe/Berlin")
except Exception:
    BERLIN_TZ = timezone(timedelta(hours=2))

API_BASE = "https://v6.db.transport.rest"

# Korrigierte Stop-IDs (von db-rest /locations verifiziert)
STATIONS = {
    "Bissendorf, Wedemark": "8001000",
    "Hannover Hbf":         "8000152",
}

# Strecken (Start, Ziel) - beide Richtungen werden abgefragt
ROUTES = [
    ("Bissendorf, Wedemark", "Hannover Hbf"),
    ("Hannover Hbf",         "Bissendorf, Wedemark"),
]

# Linien-Filter
LINE_FILTERS = {"S4", "S 4"}

# Empfaenger der Stoerungs-Mails. Hier aendern (direkt im Code), kein Secret noetig.
RECIPIENTS = [
    "jasper.ernst@gmx.de",
    "ole.burose@gmail.com",
]

DELAY_THRESHOLD_MIN = int(os.environ.get("DELAY_THRESHOLD_MIN", "21"))
JOURNEYS_RESULTS    = int(os.environ.get("JOURNEYS_RESULTS", "8"))
STATE_FILE          = Path(os.environ.get("STATE_FILE", "state.json"))
DRY_RUN             = os.environ.get("DRY_RUN", "") == "1"
USER_AGENT          = "bahn-monitor/1.1 (github.com/socialole2024-dev/bahn-monitor)"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


@dataclass
class Issue:
    trip_id: str
    line: str
    from_name: str
    to_name: str
    planned_when: str
    actual_when: str
    delay_min: int
    cancelled: bool
    direction: str
    reason: str

    def short(self) -> str:
        # Knappe Form fuer Action-Logs
        ts = _format_iso_local(self.planned_when)
        if self.cancelled:
            return f"AUSFALL | {self.line.replace(' ','')} {ts} {self.from_name} -> {self.to_name}"
        return f"VERSPAETUNG +{self.delay_min} min | {self.line.replace(' ','')} {ts} {self.from_name} -> {self.to_name}"

    def for_mail(self) -> str:
        # Ausfuehrlicher Block fuer die Mail
        line_clean = self.line.replace(' ', '')
        planned = _format_iso_local(self.planned_when)
        out = []
        out.append(f"Verbindung: {line_clean} von {self.from_name} nach {self.to_name}")
        out.append(f"  Planmaessig:  {planned} ab {self.from_name}")
        if self.cancelled:
            out.append(f"  Status:       FAELLT AUS")
        else:
            actual = _format_iso_local(self.actual_when) if self.actual_when else "?"
            out.append(f"  Tatsaechlich: {actual} (+{self.delay_min} Minuten Verspaetung)")
        if self.direction:
            out.append(f"  Richtung:     {self.direction}")
        # Anspruchs-Hinweise
        out.append("")
        out.append("  Deine Anspruechen:")
        out.append("    - GVH-Punktlichkeitsgarantie: 5 EUR pro Vorfall  |  Frist: 14 Tage")
        out.append("      Antrag online: https://www.uestra.de/service/uestra-gvh-garantie/")
        if self.cancelled or self.delay_min >= 60:
            out.append("    - DB-Bundesfahrgastrechte: 1,50 EUR pro Vorfall (ab >=60 min oder Ausfall)")
            out.append("      Antrag: https://www.bahn.de/service/informationen-buchung/fahrgastrechte/service-center")
        if self.cancelled or self.delay_min >= 20:
            out.append("    - Hoeherklassige Zuege (RE/IC/ICE) duerfen kostenlos genutzt werden")
            out.append("      Mehrkosten anschliessend erstattbar (Ticket aufheben)")
        return "\n".join(out)


def http_get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_journeys(from_id: str, to_id: str, results: int = 8) -> list:
    """Holt Journeys zwischen zwei Stationen (db-rest /journeys)."""
    params = {
        "from": from_id,
        "to": to_id,
        "results": str(results),
        "stopovers": "false",
        "transfers": "0",  # nur direkte Verbindungen
    }
    url = f"{API_BASE}/journeys?{urllib.parse.urlencode(params)}"
    data = http_get_json(url)
    if isinstance(data, dict):
        return data.get("journeys", []) or []
    return []


def issues_from_journeys(journeys: list, from_name: str, to_name: str) -> list:
    """Filtert auf S4-Verbindungen mit Stoerung."""
    issues = []
    for j in journeys:
        legs = j.get("legs") or []
        if len(legs) != 1:  # keine Umstiege
            continue
        leg = legs[0]
        line = ((leg.get("line") or {}).get("name") or "").strip()
        if line not in LINE_FILTERS:
            continue
        cancelled = bool(leg.get("cancelled"))
        delay_seconds = leg.get("departureDelay")
        delay_min = int(round((delay_seconds or 0) / 60))
        trip_id = leg.get("tripId") or f"{line}-{leg.get('plannedDeparture','')}"
        planned_when = leg.get("plannedDeparture") or ""
        actual_when = leg.get("departure") or ""
        direction = leg.get("direction") or ""
        if cancelled:
            issues.append(Issue(trip_id, line, from_name, to_name, planned_when, actual_when, 0, True, direction, "Ausfall"))
        elif delay_min >= DELAY_THRESHOLD_MIN:
            issues.append(Issue(trip_id, line, from_name, to_name, planned_when, actual_when, delay_min, False, direction, "Verspaetung"))
    return issues


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"notified": {}}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"notified": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def prune_state(state: dict, max_age_hours: int = 24) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    fresh = {}
    for key, ts in state.get("notified", {}).items():
        try:
            t = datetime.fromisoformat(ts)
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            if t >= cutoff:
                fresh[key] = ts
        except Exception:
            pass
    state["notified"] = fresh


def filter_new(issues: list, state: dict) -> list:
    notified = state.setdefault("notified", {})
    fresh = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for it in issues:
        bucket = "X" if it.cancelled else str((it.delay_min // 5) * 5)
        key = f"{it.trip_id}|{it.reason}|{bucket}"
        if key in notified:
            continue
        notified[key] = now_iso
        fresh.append(it)
    return fresh


def _format_iso_local(iso: str) -> str:
    if not iso:
        return "?"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(BERLIN_TZ).strftime("%a %d.%m. %H:%M")
    except Exception:
        return iso


def build_email(issues: list):
    n_cancel = sum(1 for i in issues if i.cancelled)
    n_delay = len(issues) - n_cancel
    parts = []
    if n_cancel:
        parts.append(f"{n_cancel} Ausfall" if n_cancel == 1 else f"{n_cancel} Ausfaelle")
    if n_delay:
        parts.append(f"{n_delay} Verspaetung" if n_delay == 1 else f"{n_delay} Verspaetungen")
    subject = "S4-Alarm: " + " und ".join(parts)

    sep = "\n" + ("-" * 60) + "\n"
    blocks = [it.for_mail() for it in issues]
    body_lines = [
        f"Stoerung auf der S4 (Bissendorf, Wedemark <-> Hannover Hbf):",
        "",
        sep.join(blocks),
        "",
        ("=" * 60),
        f"Schwelle Verspaetung: >= {DELAY_THRESHOLD_MIN} Minuten  |  Datenquelle: db-rest",
        f"Geprueft: {datetime.now(BERLIN_TZ).strftime('%d.%m.%Y %H:%M %Z')}",
        f"Generiert von: github.com/socialole2024-dev/bahn-monitor",
    ]
    return subject, "\n".join(body_lines)


def send_mail(subject: str, body: str) -> None:
    user = os.environ.get("EMAIL_USER", "")
    pwd  = os.environ.get("EMAIL_APP_PASSWORD", "")
    if RECIPIENTS:
        to_list = list(RECIPIENTS)
    else:
        env_to = os.environ.get("EMAIL_TO", "").strip()
        to_list = [x.strip() for x in env_to.split(",") if x.strip()]
    if DRY_RUN:
        print("[DRY_RUN] Wuerde Mail senden:")
        print("  An:", ", ".join(to_list))
        print("  Subject:", subject)
        for line in body.splitlines():
            print("   ", line)
        return
    if not (user and pwd and to_list):
        raise RuntimeError("EMAIL_USER, EMAIL_APP_PASSWORD oder Empfaenger-Liste fehlt.")
    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(to_list)
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, pwd)
        smtp.send_message(msg)
    print(f"Mail gesendet an {', '.join(to_list)}: {subject}")


def main() -> int:
    print(f"Bahn-Monitor laeuft - Schwelle {DELAY_THRESHOLD_MIN} min, Endpoint /journeys")
    all_issues = []
    for from_name, to_name in ROUTES:
        from_id = STATIONS[from_name]
        to_id   = STATIONS[to_name]
        try:
            journeys = fetch_journeys(from_id, to_id, JOURNEYS_RESULTS)
            print(f"  {from_name} -> {to_name}: {len(journeys)} Verbindungen geholt")
        except urllib.error.HTTPError as e:
            print(f"  HTTP-FEHLER {e.code} beim Abrufen {from_name} -> {to_name}: {e.reason}", file=sys.stderr)
            continue
        except urllib.error.URLError as e:
            print(f"  NETZWERK-FEHLER {from_name} -> {to_name}: {e}", file=sys.stderr)
            continue
        issues = issues_from_journeys(journeys, from_name, to_name)
        if issues:
            print(f"    {len(issues)} potentielle Stoerungen gefunden")
        all_issues.extend(issues)
    if not all_issues:
        print("Keine Stoerungen - alles ok.")
        return 0
    state = load_state()
    prune_state(state)
    new_issues = filter_new(all_issues, state)
    if not new_issues:
        print(f"Stoerungen vorhanden ({len(all_issues)}), aber alle bereits gemeldet.")
        save_state(state)
        return 0
    subject, body = build_email(new_issues)
    print(f"Sende Mail fuer {len(new_issues)} neue Stoerungen.")
    send_mail(subject, body)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
