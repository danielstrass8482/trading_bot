"""
watchdog.py – Eigenständiger Heartbeat-Watchdog (Aufgabe 1, 2026-07-30).

Läuft NICHT als Teil von trading-bot.service oder trading-bot-saxo.service,
sondern als eigener systemd-Timer (trading-watchdog.timer, alle 10 Minuten,
siehe deploy/trading-watchdog.service + .timer). Das ist bewusst so: würde
dieser Check innerhalb eines der beiden Bot-Prozesse laufen, könnte er genau
dann nicht mehr alarmieren, wenn dieser Prozess selbst hängt oder abgestürzt
ist – der eigentliche Fall, den der Watchdog abdecken soll.

Prüft für beide Bots (alpaca/saxo), ob seit dem letzten geschriebenen
Heartbeat (database.BotHeartbeat, siehe dort) mehr als STALE_AFTER_MINUTES
vergangen sind – aber NUR während der jeweiligen Handelszeiten (außerhalb
davon läuft planmäßig kein Zyklus, ein "alter" Heartbeat ist dort normal statt
alarmierend). Bei Alarm: E-Mail, danach Re-Alarm frühestens nach
ALERT_RESEND_MINUTES (verhindert Mail-Spam bei einem länger andauernden
Ausfall, siehe BotHeartbeat.last_alert_at).
"""

from datetime import datetime, timedelta

import pytz

from database import get_session, BotHeartbeat, Base, engine
from notifications import send_email

STALE_AFTER_MINUTES = 20
ALERT_RESEND_MINUTES = 60

ALPACA_HOURS = {"timezone": "America/New_York", "open": (9, 30), "close": (16, 0)}

# Duplizierte Minimal-Kopie von trading_bot_saxo/config.EXCHANGES (nur
# timezone/open/close, kein Watchlist-Anteil) – bewusst dupliziert statt
# Cross-Repo-Import, da beide Bots getrennte Deployments mit eigenem venv
# sind (gleiches Muster wie llm_analyst.py/saxo_client.py, siehe deren
# Docstrings). Bei Änderungen an den Handelszeiten in trading_bot_saxo/
# config.py hier manuell mitziehen.
SAXO_EXCHANGES_HOURS = {
    "FSE": {"timezone": "Europe/Berlin", "open": (9, 0), "close": (17, 30)},
    "PAR": {"timezone": "Europe/Paris", "open": (9, 0), "close": (17, 30)},
    "AMS": {"timezone": "Europe/Amsterdam", "open": (9, 0), "close": (17, 30)},
    "LSE_SETS": {"timezone": "Europe/London", "open": (8, 0), "close": (16, 30)},
}


def _within_hours(spec: dict, now_override: datetime | None = None) -> bool:
    """now_override erlaubt synthetisches Testen ohne auf echte Handelszeit
    zu warten (siehe test-Aufruf in der Modul-Dokumentation/Deploy-Notiz)."""
    tz = pytz.timezone(spec["timezone"])
    now = now_override.astimezone(tz) if now_override else datetime.now(tz)
    if now.weekday() >= 5:
        return False
    open_h, open_m = spec["open"]
    close_h, close_m = spec["close"]
    open_t = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_t = now.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_t <= now <= close_t


def _saxo_market_open(now_override: datetime | None = None) -> bool:
    return any(_within_hours(spec, now_override) for spec in SAXO_EXCHANGES_HOURS.values())


def check_heartbeat(bot_name: str, market_open: bool) -> bool:
    """
    Prüft den Heartbeat für einen Bot. Gibt True zurück wenn eine Alarm-Mail
    verschickt wurde (für Tests/Logging), sonst False.
    """
    with get_session() as session:
        row = session.query(BotHeartbeat).filter_by(bot_name=bot_name).first()
        last_cycle_at = row.last_cycle_at if row else None
        last_alert_at = row.last_alert_at if row else None

        if not market_open:
            print(f"⏸️  Bot '{bot_name}': außerhalb der Handelszeiten, kein Check.")
            return False

        now = datetime.utcnow()
        age_minutes = None if last_cycle_at is None else (now - last_cycle_at).total_seconds() / 60

        if last_cycle_at is not None and age_minutes <= STALE_AFTER_MINUTES:
            print(f"✅ Bot '{bot_name}': letzter Zyklus vor {age_minutes:.1f} Min (ok).")
            return False

        # Stale oder noch nie gemeldet -> Alarm, aber nur wenn seit dem
        # letzten Alarm bereits ALERT_RESEND_MINUTES vergangen sind.
        if last_alert_at is not None and (now - last_alert_at).total_seconds() / 60 < ALERT_RESEND_MINUTES:
            print(f"🔇 Bot '{bot_name}': weiterhin stale, aber Re-Alarm-Fenster noch nicht erreicht (letzter Alarm "
                  f"vor {(now - last_alert_at).total_seconds() / 60:.1f} Min).")
            return False

        age_str = f"{age_minutes:.0f} Minuten" if age_minutes is not None else "Prozessstart (noch nie gemeldet)"
        msg = (
            f"Kein Lebenszeichen von Bot '{bot_name}' seit {age_str} "
            f"(Schwelle: {STALE_AFTER_MINUTES} Min, Handelszeit ist aktiv).\n\n"
            "Mögliche Ursache: Prozess abgestürzt/hängt, Scheduler-Job registriert sich nicht mehr, "
            "oder ein unbehandelter Fehler im Entry-/Monitoring-Zyklus. Bitte Service-Status prüfen."
        )
        print(f"🚨 {msg}")
        send_email(subject=f"🚨 Watchdog: Bot '{bot_name}' antwortet nicht", body=msg)

        if row:
            row.last_alert_at = now
        else:
            # last_cycle_at bewusst NICHT "now" – es gab noch NIE einen
            # echten Heartbeat, ein Platzhalter-Zeitstempel würde beim
            # nächsten Watchdog-Lauf fälschlich als "gerade gelaufen" zählen
            # und einen andauernden Ausfall nach der ersten Mail verschleiern.
            session.add(BotHeartbeat(bot_name=bot_name, cycle_type="unknown", last_cycle_at=None, last_alert_at=now))
        session.commit()
        return True


def main():
    # Nur create_all (idempotent, legt bot_heartbeat an falls die Tabelle
    # noch fehlt, z.B. Timer feuert vor dem ersten Start eines Bot-Prozesses
    # nach diesem Deploy) – bewusst NICHT database.init_db() (das seedet
    # zusätzlich BotConfig/CurrentWeight/etc., unnötiger Overhead für einen
    # alle 10 Minuten laufenden reinen Lese-Check).
    Base.metadata.create_all(engine)

    alpaca_open = _within_hours(ALPACA_HOURS)
    saxo_open = _saxo_market_open()
    check_heartbeat("alpaca", alpaca_open)
    check_heartbeat("saxo", saxo_open)


if __name__ == "__main__":
    main()
