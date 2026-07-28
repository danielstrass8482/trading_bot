"""
saxo_client.py – Saxo Bank OpenAPI Client (OAuth-Token-Handling + API-Helper)

Access Token: ~19,5 Min gültig. Refresh Token: ~59,5 Min gültig, wird bei
JEDEM Refresh zusammen mit dem Access Token rotiert – der alte Refresh Token
wird dabei sofort ungültig. Beide Werte liegen ausschließlich in der DB
(Tabelle saxo_tokens, siehe database.py), niemals in .env (bis auf den
initialen One-Time-Seed nach dem manuellen OAuth-Login).
"""

from datetime import datetime, timedelta
import socket

import requests
import urllib3.util.connection as _urllib3_connection

from config import SAXO_CLIENT_ID, SAXO_CLIENT_SECRET, SAXO_TOKEN_URL, SAXO_API_BASE_URL
from database import get_session, get_saxo_token, upsert_saxo_token

# Saxos Akamai-Edge liefert auf diesem VPS (Hoster-ASN dogado GmbH) über IPv6
# ein hartes "403 Access Denied" (WAF-Block, kein Saxo-Auth-Fehler) – bestätigt
# per `curl -4` (funktioniert, liefert korrektes 401 bei Test-Credentials) vs.
# Standard-`curl` (IPv6, 403). Globaler Fix: urllib3 zwingen, bei DNS-Resolution
# nur IPv4-Adressen zu berücksichtigen. Betrifft den ganzen Prozess (auch
# Alpaca/yfinance-Requests), das ist aber unkritisch, da alle genutzten APIs
# IPv4 unterstützen.
_urllib3_connection.allowed_gai_family = lambda: socket.AF_INET

# Access Token gilt als "gleich ablaufend" wenn weniger als 2 Minuten Restlaufzeit
# bleiben – proaktiver Refresh statt erst beim tatsächlichen Ablauf.
TOKEN_REFRESH_BUFFER_SECONDS = 120

REQUEST_TIMEOUT_SECONDS = 15


def get_valid_access_token() -> str:
    """
    Liefert einen garantiert (>2 Min) gültigen Saxo Access Token. Liest den
    aktuellen Stand aus der DB; ist der Access Token abgelaufen oder läuft
    innerhalb der nächsten 2 Minuten ab, wird automatisch refresht.
    """
    with get_session() as session:
        token = get_saxo_token(session)
        if token is None:
            raise RuntimeError(
                "Kein Saxo-Token in der DB (Tabelle saxo_tokens) gefunden – "
                "initialer OAuth-Login-Flow muss zuerst durchgeführt werden."
            )
        access_token = token.access_token
        expires_at = token.access_token_expires_at

    if datetime.utcnow() >= expires_at - timedelta(seconds=TOKEN_REFRESH_BUFFER_SECONDS):
        return refresh_saxo_token()
    return access_token


def refresh_saxo_token() -> str:
    """
    Tauscht den aktuellen Refresh Token gegen ein neues Access-/Refresh-Token-
    Paar (Saxo rotiert bei jedem Refresh BEIDE Werte, der alte Refresh Token
    wird sofort ungültig – daher müssen beide neuen Werte die DB überschreiben).

    Schlägt der Refresh fehl, führt das spätestens nach Ablauf des aktuellen
    Refresh Tokens (~1h) zum kompletten Verbindungsverlust und erfordert einen
    manuellen Wiederholungslauf des kompletten OAuth-Login-Flows – daher wird
    bei einem Fehler zusätzlich eine Warn-E-Mail verschickt (analog zur
    Daily-Summary-E-Mail in main.py).
    """
    with get_session() as session:
        token = get_saxo_token(session)
        if token is None:
            raise RuntimeError(
                "Kein Saxo-Token in der DB (Tabelle saxo_tokens) – Refresh nicht möglich, "
                "initialer OAuth-Login-Flow muss zuerst durchgeführt werden."
            )
        current_refresh_token = token.refresh_token

    try:
        response = requests.post(
            SAXO_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": current_refresh_token,
                "client_id": SAXO_CLIENT_ID,
                "client_secret": SAXO_CLIENT_SECRET,
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        print(f"⚠️  Saxo Token-Refresh fehlgeschlagen: {e}")
        _send_refresh_failure_email(e)
        raise

    now = datetime.utcnow()
    new_access_token = payload["access_token"]
    new_refresh_token = payload["refresh_token"]
    access_expires_at = now + timedelta(seconds=payload.get("expires_in", 1170))
    refresh_expires_at = now + timedelta(seconds=payload.get("refresh_token_expires_in", 3570))

    with get_session() as session:
        upsert_saxo_token(
            session,
            access_token=new_access_token,
            refresh_token=new_refresh_token,
            access_token_expires_at=access_expires_at,
            refresh_token_expires_at=refresh_expires_at,
        )
        session.commit()

    print("✅ Saxo Token erfolgreich erneuert.")
    return new_access_token


def _send_refresh_failure_email(error: Exception):
    """
    Lazy Import von main.send_email (main.py importiert seinerseits Module,
    die auf saxo_client zugreifen könnten – Import erst zur Laufzeit statt auf
    Modulebene vermeidet einen Zirkelimport, analog zu config.get_live_config).
    """
    try:
        from main import send_email
        send_email(
            subject="🛑 Saxo Bank – Token-Refresh fehlgeschlagen",
            body=(
                f"Der automatische Refresh des Saxo OAuth-Tokens ist fehlgeschlagen:\n{error}\n\n"
                "Der aktuelle Refresh Token läuft ~1h nach dem letzten erfolgreichen Refresh ab. "
                "Ohne rechtzeitige manuelle Korrektur geht die Saxo-Verbindung komplett verloren "
                "und der komplette OAuth-Login-Flow muss von Hand wiederholt werden."
            ),
        )
    except Exception as email_error:
        print(f"⚠️  Zusätzlich: Warn-E-Mail konnte nicht versendet werden: {email_error}")


def saxo_api_get(endpoint: str) -> dict:
    """
    GET-Request gegen die Saxo OpenAPI (https://gateway.saxobank.com/openapi/),
    holt sich automatisch einen gültigen Access Token.

    Beispiel: saxo_api_get("port/v1/accounts/me")
    """
    access_token = get_valid_access_token()
    url = f"{SAXO_API_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()
