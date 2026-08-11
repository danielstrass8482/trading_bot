"""
notifications.py – E-Mail-Versand fuer den Alpaca-Bot, in ein eigenes Modul
ausgelagert (aus main.py extrahiert, analog zum bereits vorhandenen Fix im
trading_bot_saxo-Repo, siehe dortiges notifications.py).

Grund: broker.py braucht fuer den Positions-Konsistenz-Watchdog (Aufgabe 3,
2026-07-30) send_email(), main.py importiert aber bereits (transitiv)
broker.py - ein direkter Top-Level-Import von main.py dort waere zirkulaer.
Ein Lazy-Import ("from main import send_email" zur Aufrufzeit) waere zudem
gefaehrlich: main.py wird vom systemd-Service als __main__ ausgefuehrt, ist
also NIE unter dem Namen "main" in sys.modules zwischengespeichert. Jeder
Lazy-Import wuerde main.py deshalb bei jedem Aufruf ein zweites Mal frisch
von der Platte laden - inklusive aller Top-Level-Imports - mit dem Risiko,
dass ein frisch gepullter main.py/config.py-Stand (ohne Service-Neustart)
auf ein noch altes gecachtes Modul trifft und mit ImportError abbricht
(exakt so 2026-07-29 im Saxo-Bot beobachtet).

Fix: send_email() lebt jetzt in einem eigenen Modul ohne jede Abhaengigkeit
zu main.py/broker.py/watchdog.py - kann von allen normal am Modulanfang
importiert werden, kein Zirkel, kein Re-Import-Risiko.
"""

import base64
import smtplib
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from config import ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_FALLBACK_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_TIMEOUT


def _smtp_login_utf8(server, user, password):
    """
    AUTH LOGIN von Hand, da smtplib.auth()/login() den Base64-Payload intern
    mit .encode("ascii") kodiert und damit bei Nicht-ASCII-Zeichen (Umlaute)
    im Passwort mit UnicodeEncodeError abstuerzt.
    """
    server.ehlo()
    code, resp = server.docmd("AUTH", "LOGIN")
    if code != 334:
        raise smtplib.SMTPAuthenticationError(code, resp)
    code, resp = server.docmd(base64.b64encode(user.encode("utf-8")).decode("ascii"))
    if code != 334:
        raise smtplib.SMTPAuthenticationError(code, resp)
    code, resp = server.docmd(base64.b64encode(password.encode("utf-8")).decode("ascii"))
    if code not in (235, 503):
        raise smtplib.SMTPAuthenticationError(code, resp)


def send_email(subject: str, body: str, to: str | None = None):
    """
    Verschickt eine E-Mail via smtplib (Standardbibliothek, kein externes Package).
    Fallback: Ohne ALERT_EMAIL oder SMTP-Zugangsdaten wird nur in die Logs
    geschrieben – der Bot darf dadurch nie abstürzen.

    Railway blockiert ausgehenden Port 587 (STARTTLS). Primär wird daher
    Port 465 (SMTPS/SSL) verwendet. Falls auch dieser Port blockiert wird
    (Timeout), greift ein Fallback auf SMTP_FALLBACK_PORT (Standard: 2525),
    der von Railway nicht blockiert wird. SMTP_HOST ist konfigurierbar,
    sodass später auf einen eigenen Mailserver umgestellt werden kann.

    to (Multi-Tenant-Snapshot/-Mail-Fix, 2026-08-11): optionaler Empfänger,
    Default weiterhin ALERT_EMAIL (Daniel) - alle bisherigen Aufrufer
    (Fehler-Alerts, Marktbriefing, Watchdog etc.) bleiben dadurch unverändert
    an ihn adressiert. Nur main.send_daily_summary_email() übergibt hier
    künftig die individuelle pos_users.email eines verbundenen Nutzers (siehe
    database.get_user_email), damit jeder Nutzer seine eigene Tages-Mail an
    seine eigene Adresse bekommt statt an Daniels.
    """
    recipient = to or ALERT_EMAIL
    if not recipient or not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"📧 [E-Mail nicht konfiguriert – nur Log] {subject}\n{body}")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            _smtp_login_utf8(server, SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [recipient], msg.as_string())
        print(f"📧 E-Mail versendet: {subject} (Port {SMTP_PORT})")
    except (TimeoutError, OSError) as e:
        print(f"⚠️  SMTP Port {SMTP_PORT} nicht erreichbar ({e}) – Fallback auf Port {SMTP_FALLBACK_PORT}")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_FALLBACK_PORT, timeout=SMTP_TIMEOUT) as server:
                _smtp_login_utf8(server, SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [recipient], msg.as_string())
            print(f"📧 E-Mail versendet: {subject} (Port {SMTP_FALLBACK_PORT})")
        except Exception as fallback_e:
            print(f"⚠️  E-Mail-Versand fehlgeschlagen (Fallback Port {SMTP_FALLBACK_PORT}): {fallback_e}")
    except Exception as e:
        print(f"⚠️  E-Mail-Versand fehlgeschlagen: {e}")
