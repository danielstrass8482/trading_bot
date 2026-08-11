"""
confirm_execution.py – Interception- und Bestätigungs-Logik für den
manuellen Bestätigungs-Tier vor Entry-Trades (Confirm-Tier Chunk 2a+2b+2c,
2026-08-11).

ARCHITEKTUR-VORGABE (Option C, mit dem Anwalt abgestimmt, siehe
trading_shared/confirm_execution/__init__.py-Docstring aus Chunk 1):
physisch getrenntes Modul vom bestehenden Auto-Execution-Code. Dieses Modul
importiert BEWUSST NICHTS aus broker.py (kein place_trade, kein
_submit_order_idempotent, keine Alpaca-Order-Calls, kein calculate_quantity)
und auch nicht aus rule_engine.py (keine Kopplung an die Signal-Engine,
Übergabe erfolgt über primitive Werte/Dicts statt eines SignalResult-
Objekts) - die strukturelle Trennung muss für einen Auditor direkt im Code
sichtbar sein, nicht nur über Konfiguration. Umgekehrt importiert broker.py
nichts von hier. notifications.py (SMTP-Utility, keine Order-Logik) und
database.py (reine DB-/Config-Schicht) sind dagegen unproblematisch und
werden hier verwendet.

Entry-only: nur Trade-ENTRIES laufen über dieses Modul (siehe main.
run_entry_cycle, der Aufrufer). Exit-Typen (SL/TP/Trailing/Time-Exit) bleiben
vollständig unverändert im bestehenden broker.monitor_open_positions-Pfad -
dieses Modul kennt sie nicht und rührt sie nicht an.

Baut auf trading_shared.confirm_execution (Chunk 1, 2026-08-07) auf - die
Token-/Ablauf-Helfer dort waren bereits ORM-agnostisch für genau diesen
Zweck vorbereitet.

Wer tatsächlich place_trade() aufruft: NICHT dieses Modul (s.o.) - das
übernimmt trading_api.py (Chunk 2b), der HTTP-seitige Orchestrator, der
sowohl dieses Modul als auch broker.py importieren darf (analog zu main.py,
das für die Entry-Seite in Chunk 2a genau dieselbe Doppelrolle hat). Dieses
Modul stellt dafür nur die atomare Status-Übergangs-Primitive (try_claim)
und die reinen Lese-/Schreibfunktionen bereit.

SCOPE Chunk 2c (2026-08-11, NUR das hier zusätzlich zu 2a/2b, siehe
Aufgabe): tatsächliche Timeout-Durchsetzung von expires_at (proaktiver
Hintergrundjob expire_overdue() UND ein Check bei jedem Bestätigungs-
versuch, siehe trading_api._resolve_confirmation), Preis-Re-Check gegen
price_tolerance_pct_snapshot (mit explizitem Re-Bestätigungs-Schritt bei
Abweichung, ebenfalls in trading_api._resolve_confirmation - der Preisabruf
selbst braucht yfinance, lebt daher bewusst dort, nicht hier, siehe unten),
und ein eigener FAILED-Status für eine Bestätigung, deren place_trade()-
Aufruf danach scheitert (mark_failed(), inkl. Grund in failure_reason).
Die fünf erreichbaren Status: PENDING, CONFIRMED, REJECTED, EXPIRED, FAILED.
"""
from datetime import datetime

from sqlalchemy import text

from trading_shared.confirm_execution import generate_confirmation_token, compute_expiry

from database import get_session, PendingConfirmation, get_user_live_config, get_user_email
from notifications import send_email

# Platzhalter bis Chunk 2c den echten Timeout/Preis-Re-Check baut - der Wert
# wird hier bereits in expires_at geschrieben (Spalte ist NOT NULL) und im
# Bestätigungs-Endpoint/der Mail angezeigt, aber bis dahin von keinem Code
# durchgesetzt (eine abgelaufene PENDING-Zeile lässt sich noch bestätigen).
DEFAULT_CONFIRMATION_TIMEOUT_MINUTES = 15

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"


def is_confirm_mode(user_id: int) -> bool:
    """
    True, falls dieser Nutzer EXECUTION_MODE='confirm' konfiguriert hat
    (siehe database.DEFAULT_USER_CONFIG/DEFAULT_CONFIG, Chunk 1, 2026-08-07).
    get_user_live_config() ist reine Config-/DB-Lesefunktion (database.py),
    kein Auto-Execution-Code.
    """
    return get_user_live_config(user_id).get("EXECUTION_MODE", "auto") == "confirm"


def find_existing_pending(user_id: int, ticker: str, broker: str = "alpaca") -> PendingConfirmation | None:
    """
    Dedup-Check (Chunk 2b, Aufgabe Punkt 10): bevor main._execute_or_queue_
    entry() einen neuen PENDING-Eintrag erzeugt, wird geprüft, ob für
    denselben Ticker+Nutzer(+Broker) bereits einer offen ist. Verhindert
    doppelte Einträge UND doppelte Mails, falls derselbe Kandidat über
    mehrere Entry-Slots/Scans hinweg erneut ein Signal auslöst, solange die
    vorherige Bestätigung noch aussteht.
    """
    with get_session() as session:
        return session.query(PendingConfirmation).filter_by(
            user_id=user_id, ticker=ticker, broker=broker, status=STATUS_PENDING
        ).first()


def create_pending_confirmation(
    user_id: int,
    ticker: str,
    quantity: float,
    signal_price: float,
    signal_payload: dict | None = None,
    llm_payload: dict | None = None,
    broker: str = "alpaca",
) -> PendingConfirmation:
    """
    Erstellt einen PENDING-Eintrag für ein Entry-Signal, OHNE irgendeine
    Order zu platzieren oder einen Broker anzusprechen (siehe Moduldoc).

    quantity/signal_price werden vom Aufrufer (main.run_entry_cycle) über
    bereits vorhandene, reine Rechenfunktionen ermittelt (Preisabruf/
    Kapital-Arithmetik, KEIN Order-Call) und hier nur noch persistiert.

    signal_payload/llm_payload (Chunk 2b, NEU): der Aufrufer übergibt die
    zur Signalerzeugung gehörenden SignalResult-/LLM-Felder als reine Dicts
    (main.py serialisiert, dieses Modul bleibt dadurch weiterhin ohne
    Kenntnis von rule_engine.SignalResult) - JSON-serialisiert gespeichert,
    damit trading_api.py bei einer Bestätigung ein SignalResult
    rekonstruieren und place_trade() unverändert aufrufen kann (siehe
    PendingConfirmation-Docstring in database.py).

    price_tolerance_pct_snapshot friert den AKTUELL konfigurierten
    PRICE_TOLERANCE_PCT-Wert zum Signalzeitpunkt ein - Chunk 2c vergleicht
    später den dann aktuellen Marktpreis gegen signal_price innerhalb dieser
    Toleranz, bevor eine Bestätigung tatsächlich zu einer Order führt.
    """
    import json as _json

    with get_session() as session:
        user_cfg = get_user_live_config(user_id)
        price_tolerance = float(user_cfg.get("PRICE_TOLERANCE_PCT", 0.02))
        now = datetime.utcnow()
        pending = PendingConfirmation(
            user_id=user_id,
            broker=broker,
            ticker=ticker,
            qty_or_amount=quantity,
            signal_price=signal_price,
            signal_timestamp=now,
            status=STATUS_PENDING,
            confirmation_token=generate_confirmation_token(),
            expires_at=compute_expiry(now, DEFAULT_CONFIRMATION_TIMEOUT_MINUTES),
            price_tolerance_pct_snapshot=price_tolerance,
            signal_payload=_json.dumps(signal_payload, ensure_ascii=False) if signal_payload is not None else None,
            llm_payload=_json.dumps(llm_payload, ensure_ascii=False) if llm_payload is not None else None,
        )
        session.add(pending)
        session.commit()
        session.refresh(pending)
        return pending


def get_pending_by_token(token: str) -> PendingConfirmation | None:
    """Lookup für den Email-Magic-Link-Kanal - der Token IST die Authentifizierung
    (kryptografisch zufällig, 32 Bytes Entropie, siehe trading_shared.confirm_execution.
    generate_confirmation_token), kein Login nötig."""
    with get_session() as session:
        return session.query(PendingConfirmation).filter_by(confirmation_token=token).first()


def get_pending_by_id_for_user(pending_id: int, user_id: int) -> PendingConfirmation | None:
    """
    Ownership-gescopte Lookup für den Dashboard-Kanal - user_id kommt vom
    Aufrufer (trading_api.py) IMMER aus dem JWT, nie aus dem Request-Body.
    None, falls die Zeile nicht existiert ODER einem anderen Nutzer gehört -
    beide Fälle sind für den Aufrufer ununterscheidbar (kein Leak, ob eine
    fremde ID existiert).
    """
    with get_session() as session:
        return session.query(PendingConfirmation).filter_by(id=pending_id, user_id=user_id).first()


def list_pending_for_user(user_id: int) -> list[PendingConfirmation]:
    """Alle offenen PENDING-Einträge EINES Nutzers für die Dashboard-Queue, neueste zuerst."""
    with get_session() as session:
        return session.query(PendingConfirmation).filter_by(
            user_id=user_id, status=STATUS_PENDING
        ).order_by(PendingConfirmation.created_at.desc()).all()


def try_claim(pending_id: int, new_status: str) -> PendingConfirmation | None:
    """
    Atomarer Status-Übergang PENDING -> new_status (Chunk 2b, Aufgabe Punkt 5:
    Race-Condition-Schutz, falls Email-Link und Dashboard-Klick fast
    gleichzeitig eintreffen). Ein reines UPDATE ... WHERE status='pending'
    ist ein Compare-and-Swap auf DB-Ebene: Postgres serialisiert
    konkurrierende UPDATEs auf dieselbe Zeile über deren Row-Lock - die
    zweite Transaktion wartet, bis die erste committet hat, sieht danach den
    bereits geänderten status und trifft die WHERE-Bedingung nicht mehr
    (0 betroffene Zeilen). Kein SELECT-dann-UPDATE nötig (das wäre eine
    klassische TOCTOU-Lücke zwischen den zwei Schritten).

    Gibt die aktualisierte Zeile zurück, falls DIESER Aufruf sie erfolgreich
    von PENDING auf new_status gesetzt hat - sonst None (bereits von einem
    anderen Request geclaimt, Zeile existiert nicht, oder war nie 'pending',
    z.B. schon abgelehnt).
    """
    with get_session() as session:
        result = session.execute(
            text("""
                UPDATE pending_confirmations
                SET status = :new_status, resolved_at = :now
                WHERE id = :id AND status = :pending_status
            """),
            {"new_status": new_status, "now": datetime.utcnow(), "id": pending_id, "pending_status": STATUS_PENDING},
        )
        session.commit()
        if result.rowcount != 1:
            return None
        return session.query(PendingConfirmation).filter_by(id=pending_id).first()


def mark_failed(pending_id: int, reason: str) -> None:
    """
    Chunk 2c: eine bereits CONFIRMED-Zeile (der atomare Claim ist zu diesem
    Zeitpunkt längst erfolgreich abgeschlossen, siehe try_claim) wechselt auf
    FAILED, falls der anschließende place_trade()-Aufruf (in trading_api.py,
    außerhalb dieses Moduls) scheitert - Exception, Guardrail-Ablehnung oder
    kein Trade-Objekt zurückgegeben. KEIN try_claim-Schutz nötig: an diesem
    Punkt hält bereits garantiert nur EIN Aufrufer die Zeile (er hat sie
    gerade selbst erfolgreich von PENDING auf CONFIRMED geclaimt), ein reines
    UPDATE genügt. reason wird auf 500 Zeichen gekappt (Exception-Texte
    können beliebig lang werden, failure_reason ist für eine kurze
    Nutzer-verständliche Erklärung gedacht, nicht für einen vollen Trace).
    """
    with get_session() as session:
        session.execute(
            text("UPDATE pending_confirmations SET status = :status, failure_reason = :reason, resolved_at = :now WHERE id = :id"),
            {"status": STATUS_FAILED, "reason": (reason or "")[:500], "now": datetime.utcnow(), "id": pending_id},
        )
        session.commit()


def expire_overdue() -> int:
    """
    Chunk 2c: proaktiver Hintergrundjob (siehe main.py-Scheduler-Registrierung)
    setzt ALLE PENDING-Zeilen, deren expires_at bereits vergangen ist, atomar
    auf EXPIRED - Timeout muss unabhängig davon greifen, ob der Nutzer je auf
    den Link klickt oder die Dashboard-Queue öffnet. Reines bulk UPDATE...
    WHERE (kein SELECT-dann-UPDATE, identisches Prinzip wie try_claim): läuft
    dieser Job zeitgleich mit einem echten Bestätigungsversuch für dieselbe
    Zeile, gewinnt schlicht, wer zuerst committet - der jeweils andere sieht
    danach status != 'pending' und greift korrekt nicht mehr (der explizite
    Expiry-Check in trading_api._resolve_confirmation ist daher kein
    Duplikat, sondern deckt die Lücke zwischen zwei Job-Läufen ab).

    Gibt die Anzahl abgelaufener Zeilen zurück (für Logging).
    """
    with get_session() as session:
        result = session.execute(
            text("UPDATE pending_confirmations SET status = :expired, resolved_at = :now "
                 "WHERE status = :pending_status AND expires_at < :now"),
            {"expired": STATUS_EXPIRED, "pending_status": STATUS_PENDING, "now": datetime.utcnow()},
        )
        session.commit()
        return result.rowcount


def list_recent_for_user(user_id: int, limit: int = 20) -> list[PendingConfirmation]:
    """
    Chunk 2c: Verlauf ALLER Status (PENDING/CONFIRMED/REJECTED/EXPIRED/
    FAILED) für die Dashboard-Historie - im Gegensatz zu list_pending_for_
    user() oben, das bewusst NUR die aktuell noch handlungsfähigen PENDING-
    Zeilen liefert (für die Bestätigen/Ablehnen-Buttons).
    """
    with get_session() as session:
        return session.query(PendingConfirmation).filter_by(user_id=user_id).order_by(
            PendingConfirmation.created_at.desc()
        ).limit(limit).all()


def send_confirmation_email(pending: PendingConfirmation) -> None:
    """
    Verschickt die Bestätigungs-Mail mit Magic-Link (Chunk 2b). Plain-Text,
    stilkonsistent zu jeder anderen Mail in diesem Repo (notifications.
    send_email/portfolio_os.notifier.send_email - beide ausschließlich
    MIMEText(..., "plain")) statt eines neuen HTML-Templates.

    Fehlt eine hinterlegte Adresse (get_user_email liefert None), wird NUR
    geloggt statt eine Exception zu werfen - der Dashboard-Kanal bleibt für
    diesen Nutzer trotzdem nutzbar, ein fehlender Email-Kanal darf den
    gesamten Entry-Zyklus nicht zum Absturz bringen (identisches Prinzip wie
    überall sonst in main.py: Fehler-Isolierung pro Nutzer).
    """
    from config import APP_BASE_URL

    recipient = get_user_email(pending.user_id)
    if not recipient:
        print(f"⚠️  Nutzer {pending.user_id}: keine E-Mail-Adresse hinterlegt – Bestätigungs-Mail für "
              f"{pending.ticker} (PENDING #{pending.id}) übersprungen (Dashboard-Kanal bleibt nutzbar).")
        return

    link = f"{APP_BASE_URL}/confirm/{pending.confirmation_token}"
    subject = f"⏳ Trading Bot – Bestätigung nötig: {pending.ticker}"
    body = f"""Trading Bot – Bestätigung nötig
{'=' * 50}

Ein Entry-Signal wartet auf deine Bestätigung:

Ticker:      {pending.ticker}
Menge:       {pending.qty_or_amount}
Preis:       ${pending.signal_price:.2f}
Zeitpunkt:   {pending.signal_timestamp.strftime('%d.%m.%Y %H:%M')} UTC
Läuft ab:    {pending.expires_at.strftime('%d.%m.%Y %H:%M')} UTC

Bestätigen oder ablehnen (kein Login nötig):
{link}

Der Trade wird NICHT ausgeführt, bis du ihn bestätigst. Du kannst
Bestätigungen auch im Dashboard unter "Bestätigungen" sehen und bearbeiten.
"""
    send_email(subject, body, to=recipient)
    print(f"📧 Nutzer {pending.user_id}: Bestätigungs-Mail für {pending.ticker} (PENDING #{pending.id}) an {recipient} gesendet.")
