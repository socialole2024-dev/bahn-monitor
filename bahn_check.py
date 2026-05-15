"""
Bahn-Monitor â prueft die S4 zwischen Bissendorf, Wedemark und Hannover Hbf
auf Ausfaelle und Verspaetungen >= 21 Minuten und schickt eine Mail per Gmail-SMTP.
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

STATIONS = {
    "Bissendorf, Wedemark": "8099382",
    "Hannover Hbf":         "8000152",
}

ROUTES = [
    ("Bissendorf, Wedemark", "Hannover Hbf"),
    ("Hannover Hbf",         "Bissendorf, Wedemark"),
]

LINE_FILTERS = {"S4", "S 4"}

# Empfaenger der Stoerungs-Mails. Hier aendern (direkt im Code), kein Secret noetig.
RECIPIENTS = [
    "jasper.ernst@gmx.de",
    "ole.burose@gmail.com",
]

DELAY_THRESHOLD_MIN = int(os.environ.get("DELAY_THRESHOLD_MIN", "0"))  # TEST: 0 = jede S4 triggert
LOOKAHEAD_MIN       = int(os.environ.get("LOOKAHEAD_MIN", "120"))
STATE_FILE          = Path(os.environ.get("STATE_FILE", "state.json"))
DRY_RUN             = os.environ.get("DRY_RUN", "") == "1"
USER_AGENT          = "bahn-monitor/1.0 (github.com/socialole2024-dev/bahn-monitor)"
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
        ts = _format_iso_local(self.planned_when)
        if self.cancelled:
            return f"AUSFALL  | {self.line} {ts} {self.from_name} -> {self.to_name} (Richtung {self.direction})"
        return f"VERSPAETUNG +{self.delay_min} min | {self.line} {ts} {self.from_name} -> {self.to_name} (Richtung {self.direction})"


def http_get_json(url: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_departures(stop_id: str, duration_min: int) -> list:
    params = {"duration": str(duration_min), "results": "60", "remarks": "false", "language": "de"}
    url = f"{API_BASE}/stops/{stop_id}/departures?{urllib.parse.urlencode(params)}"
    data = http_get_json(url)
    if isinstance(data, dict):
        return data.get("departures", []) or []
    if isinstance(data, list):
        return data
    return []


def is_target_line(dep: dict) -> bool:
    line = dep.get("line") or {}
    return (line.get("name") or "").strip() in LINE_FILTERS


def goes_toward(dep: dict, target_station_name: str) -> bool:
    direction = (dep.get("direction") or "").lower()
    target = target_station_name.lower()
    if "hannover" in target:
        return "hannover" in direction
    if "bissendorf" in target or "bennem" in target:
        return any(k in direction for k in ("bennem", "bissendorf", "mellendorf"))
    return False


def detect_issues(deps: list, from_name: str, to_name: str) -> list:
    issues = []
    for dep in deps:
        if not is_target_line(dep) or not goes_toward(dep, to_name):
            continue
        line = (dep.get("line") or {}).get("name", "?")
        cancelled = bool(dep.get("cancelled"))
        delay_min = int(round((dep.get("delay") or 0) / 60))
        trip_id = dep.get("tripId") or f"{line}-{dep.get('plannedWhen', '')}"
        planned_when = dep.get("plannedWhen") or ""
        actual_when = dep.get("when") or ""
        direction = dep.get("direction") or ""
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
    lines = ["Folgende Stoerungen auf der S4 (Bissendorf, Wedemark <-> Hannover Hbf):", ""]
    for it in issues:
        lines.append("  - " + it.short())
    lines += [
        "",
        f"Schwelle Verspaetung: >= {DELAY_THRESHOLD_MIN} Minuten",
        f"Datenquelle: db-rest ({API_BASE})",
        f"Geprueft: {datetime.now(BERLIN_TZ).strftime('%d.%m.%Y %H:%M %Z')}",
    ]
    return subject, "\n".join(lines)


def send_mail(subject: str, body: str) -> None:
    user = os.environ.get("EMAIL_USER", "")
    pwd  = os.environ.get("EMAIL_APP_PASSWORD", "")
    # RECIPIENTS hat Vorrang. EMAIL_TO env wird nur genutzt, wenn RECIPIENTS leer ist.
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
    print(f"Bahn-Monitor laeuft - Schwelle {DELAY_THRESHOLD_MIN} min, Vorschau {LOOKAHEAD_MIN} min")
    all_issues = []
    for from_name, to_name in ROUTES:
        stop_id = STATIONS[from_name]
        try:
            deps = fetch_departures(stop_id, LOOKAHEAD_MIN)
            print(f"  {from_name} -> {to_name}: {len(deps)} Abfahrten geholt")
        except urllib.error.URLError as e:
            print(f"  FEHLER beim Abrufen {from_name}: {e}", file=sys.stderr)
            continue
        issues = detect_issues(deps, from_name, to_name)
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
