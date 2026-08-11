"""
confirm_execution.py – Interception-Logik für den manuellen Bestätigungs-Tier
vor Entry-Trades (Confirm-Tier Chunk 2a, 2026-08-11).

ARCHITEKTUR-VORGABE (Option C, mit dem Anwalt abgestimmt, siehe
trading_shared/confirm_execution/__init__.py-Docstring aus Chunk 1):
physisch getrenntes Modul vom bestehenden Auto-Execution-Code. Dieses Modul
importiert BEWUSST NICHTS aus broker.py (kein place_trade, kein
_submit_order_idempotent, keine Alpaca-Order-Calls, kein calculate_quantity)
und auch nicht aus rule_engine.py (keine Kopplung an die Signal-Engine,
Übergabe erfolgt über primitive Werte statt eines SignalResult-Objekts) -
die strukturelle Trennung muss für einen Auditor direkt im Code sichtbar
sein, nicht nur über Konfiguration. Umgekehrt importiert broker.py nichts
von hier.

Entry-only: nur Trade-ENTRIES laufen über dieses Modul (siehe main.
run_entry_cycle, der Aufrufer). Exit-Typen (SL/TP/Trailing/Time-Exit) bleiben
vollständig unverändert im bestehenden broker.monitor_open_positions-Pfad -
dieses Modul kennt sie nicht und rührt sie nicht an.

Baut auf trading_shared.confirm_execution (Chunk 1, 2026-08-07) auf - die
Token-/Ablauf-Helfer dort waren bereits ORM-agnostisch für genau diesen
Zweck vorbereitet.

SCOPE Chunk 2a (NUR das hier, siehe Aufgabe): ein Entry-Signal landet als
PENDING in database.PendingConfirmation, es wird KEIN Broker-Call ausgelöst.
Kein Bestätigungskanal (E-Mail/Dashboard - Chunk 2b), kein Timeout-/Preis-
Re-Check-Enforcement (Chunk 2c - expires_at wird hier bereits gesetzt, aber
von NIEMANDEM ausgewertet). Das bedeutet: jedes Entry-Signal eines Nutzers
mit EXECUTION_MODE='confirm' führt bis Chunk 2b/2c fertig ist zu KEINER
tatsächlichen Order - das ist in dieser Zwischenphase gewollt (sicherer
Zustand), kein Bug.
"""
from datetime import datetime

from trading_shared.confirm_execution import generate_confirmation_token, compute_expiry

from database import get_session, PendingConfirmation, get_user_live_config

# Platzhalter bis Chunk 2c den echten Timeout/Preis-Re-Check baut - der Wert
# wird hier bereits in expires_at geschrieben (Spalte ist NOT NULL), aber
# bis dahin von keinem Code ausgewertet/durchgesetzt.
DEFAULT_CONFIRMATION_TIMEOUT_MINUTES = 15


def is_confirm_mode(user_id: int) -> bool:
    """
    True, falls dieser Nutzer EXECUTION_MODE='confirm' konfiguriert hat
    (siehe database.DEFAULT_USER_CONFIG/DEFAULT_CONFIG, Chunk 1, 2026-08-07).
    get_user_live_config() ist reine Config-/DB-Lesefunktion (database.py),
    kein Auto-Execution-Code.
    """
    return get_user_live_config(user_id).get("EXECUTION_MODE", "auto") == "confirm"


def create_pending_confirmation(
    user_id: int,
    ticker: str,
    quantity: float,
    signal_price: float,
    broker: str = "alpaca",
) -> PendingConfirmation:
    """
    Erstellt einen PENDING-Eintrag für ein Entry-Signal, OHNE irgendeine
    Order zu platzieren oder einen Broker anzusprechen (siehe Moduldoc).

    quantity/signal_price werden vom Aufrufer (main.run_entry_cycle) über
    bereits vorhandene, reine Rechenfunktionen ermittelt (Preisabruf/
    Kapital-Arithmetik, KEIN Order-Call) und hier nur noch persistiert.

    price_tolerance_pct_snapshot friert den AKTUELL konfigurierten
    PRICE_TOLERANCE_PCT-Wert zum Signalzeitpunkt ein - Chunk 2c vergleicht
    später den dann aktuellen Marktpreis gegen signal_price innerhalb dieser
    Toleranz, bevor eine Bestätigung tatsächlich zu einer Order führt.
    """
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
            status="pending",
            confirmation_token=generate_confirmation_token(),
            expires_at=compute_expiry(now, DEFAULT_CONFIRMATION_TIMEOUT_MINUTES),
            price_tolerance_pct_snapshot=price_tolerance,
        )
        session.add(pending)
        session.commit()
        session.refresh(pending)
        return pending
