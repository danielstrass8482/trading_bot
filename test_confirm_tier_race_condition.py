"""
test_confirm_tier_race_condition.py – PFLICHT-SICHERHEITSTEST für den
Guardrail-Race-Condition-Fix (2026-08-13).

Gemeldeter Vorfall: schnelles Bestätigen mehrerer unterschiedlicher
Confirm-Tier-Einträge kurz hintereinander (verschiedene Tickers, jeweils
eigene PendingConfirmation-ID) führte zu MEHR ausgeführten Trades als
MAX_TRADES_PER_DAY erlaubt - die Ablehnung ("3 von 2") kam erst NACH der
dritten erfolgreichen Order. Ursache: check_guardrails() (reine SELECTs)
und die eigentliche Trade-Anlage liefen in getrennten Transaktionen mit
einem teils sekundenlangen Fenster dazwischen - zwei parallele
broker.place_trade()-Aufrufe für DENSELBEN Nutzer konnten beide denselben,
noch nicht erhöhten Zählerstand lesen. confirm_execution.try_claim()
schützt nur die EINZELNE PendingConfirmation-Zeile (bereits vor diesem Fix
korrekt atomar, siehe test_confirm_tier_chunk2d.py/test_per_user_
guardrails.py), nicht den GLOBALEN Tageslimit-/Positionslimit-Zähler.

Fix: broker._user_trade_guardrail_lock(user_id) - Postgres Session-
Advisory-Lock, serialisiert konkurrierende place_trade()-Aufrufe für
DENSELBEN Nutzer vollständig.

Deckt zwei Szenarien ab:
  (a) MAX_TRADES_PER_DAY=2, 4 exakt gleichzeitige place_trade()-Aufrufe
      (verschiedene Tickers) -> exakt 2 erfolgreich, 2 sauber VOR der
      Order mit "Tageslimit erreicht" abgelehnt, DB-Endstand exakt 2.
  (b) MAX_OPEN_POSITIONS=2 (Tageslimit hochgesetzt, damit dieser Guard
      isoliert getestet wird), 4 gleichzeitige Aufrufe -> exakt 2 offene
      Positionen am Ende, nicht mehr.

KEINE echte Alpaca-Verbindung: broker.get_alpaca_account_snapshot wird
gemockt (analog test_loss_streak_cooldown.py::guardrails_pass_cleanly),
TRADING_MODE bleibt auf dem Default "PAPER" (kein Order-Netzwerk-Call).
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(analog test_loss_streak_cooldown.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER race_test_tmp WITH PASSWORD 'race_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_race_test OWNER race_test_tmp;"
    python3 test_confirm_tier_race_condition.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_race_test;"
    sudo -u postgres psql -c "DROP USER race_test_tmp;"

NIEMALS gegen die echte Produktions-DB oder ein echtes Live-Konto ausführen.
"""
import os
import sys
import threading
import traceback
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://race_test_tmp:race_test_tmp_pw@localhost:5432/alpaca_race_test",
)
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
import broker  # noqa: E402
from config import DEFAULT_USER_ID  # noqa: E402
from rule_engine import SignalResult  # noqa: E402

database.init_db()

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_all_state():
    with database.get_session() as session:
        session.query(database.Trade).delete()
        session.query(database.BotState).delete()
        session.commit()


def make_signal(ticker):
    return SignalResult(
        ticker=ticker, score=80, direction="LONG", instrument_type="STOCK",
        approved=True, current_price=100.0, stop_loss=95.0, take_profit=110.0,
    )


def run_concurrently(tickers, user_id=DEFAULT_USER_ID):
    """Startet für jeden Ticker einen eigenen Thread, alle warten an einer
    Barrier bis ALLE bereit sind, dann rufen sie broker.place_trade()
    so gleichzeitig wie in diesem Prozess möglich auf - maximiert die
    tatsächliche Überlappung des Guardrail-Check-Fensters."""
    results, errors = {}, {}
    barrier = threading.Barrier(len(tickers))

    def worker(ticker):
        signal = make_signal(ticker)
        barrier.wait()
        try:
            trade = broker.place_trade(signal, {}, user_id)
            results[ticker] = trade
        except broker.GuardrailViolation as gv:
            errors[ticker] = str(gv)
        except Exception as e:
            errors[ticker] = f"UNERWARTETER FEHLER: {e!r}"

    threads = [threading.Thread(target=worker, args=(t,)) for t in tickers]
    with patch.object(broker, "get_alpaca_account_snapshot", return_value=None):
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    return results, errors


# ─────────────────────────────────────────────
# (a) MAX_TRADES_PER_DAY-Race: 4 gleichzeitige Bestätigungen, Limit=2
# ─────────────────────────────────────────────
def test_a_concurrent_confirms_respect_max_trades_per_day():
    wipe_all_state()
    with database.get_session() as session:
        database.set_bot_config(session, "MAX_TRADES_PER_DAY", "2")
        database.set_bot_config(session, "MAX_OPEN_POSITIONS", "9999")
        session.commit()

    tickers = ["RACE1", "RACE2", "RACE3", "RACE4"]
    results, errors = run_concurrently(tickers)

    succeeded = [t for t, trade in results.items() if trade is not None]
    record(
        "a) exakt MAX_TRADES_PER_DAY (2) von 4 gleichzeitigen Bestätigungen erfolgreich",
        len(succeeded) == 2, f"erfolgreich={succeeded}, abgelehnt={list(errors.keys())}"
    )

    with database.get_session() as session:
        actual_count = database.get_daily_trade_count(session, DEFAULT_USER_ID)
    record(
        "a) tatsächliche DB-Trade-Anzahl entspricht exakt dem Limit (kein Overcommit über 2 hinaus)",
        actual_count == 2, f"actual_count={actual_count}"
    )

    record(
        "a) alle 2 abgelehnten Requests bekamen sauber 'Tageslimit erreicht' VOR jeder Order",
        len(errors) == 2 and all("Tageslimit erreicht" in msg for msg in errors.values()),
        str(errors)
    )
    # Der historische Bug zeigte "3 von 2" (Ablehnung NACH der 3. Order) -
    # nach dem Fix darf die im Fehlertext genannte Zahl niemals über dem
    # Limit liegen (jeder abgelehnte Request musste durch den Lock warten,
    # bis der Zähler final war, sieht daher nie einen bereits-zu-hohen Wert).
    counts_seen = [int(msg.split("(")[1].split("/")[0]) for msg in errors.values()]
    record(
        "a) keine Ablehnung nennt einen Zählerstand > 2 (der historische '3 von 2'-Fall tritt nicht mehr auf)",
        all(c <= 2 for c in counts_seen), f"counts_seen={counts_seen}, errors={errors}"
    )


# ─────────────────────────────────────────────
# (b) MAX_OPEN_POSITIONS-Race: 4 gleichzeitige Bestätigungen, Limit=2
# ─────────────────────────────────────────────
def test_b_concurrent_confirms_respect_max_open_positions():
    wipe_all_state()
    with database.get_session() as session:
        database.set_bot_config(session, "MAX_TRADES_PER_DAY", "9999")
        database.set_bot_config(session, "MAX_OPEN_POSITIONS", "2")
        session.commit()

    tickers = ["POS1", "POS2", "POS3", "POS4"]
    results, errors = run_concurrently(tickers)

    succeeded = [t for t, trade in results.items() if trade is not None]
    record(
        "b) exakt MAX_OPEN_POSITIONS (2) von 4 gleichzeitigen Bestätigungen erfolgreich",
        len(succeeded) == 2, f"erfolgreich={succeeded}, abgelehnt={list(errors.keys())}"
    )

    with database.get_session() as session:
        open_count = len(database.get_open_trades(session, DEFAULT_USER_ID))
    record(
        "b) tatsächliche Anzahl offener Positionen entspricht exakt dem Limit",
        open_count == 2, f"open_count={open_count}"
    )
    record(
        "b) alle 2 abgelehnten Requests bekamen sauber 'Max. offene Positionen erreicht'",
        len(errors) == 2 and all("Max. offene Positionen erreicht" in msg for msg in errors.values()),
        str(errors)
    )


def main():
    for fn in (test_a_concurrent_confirms_respect_max_trades_per_day, test_b_concurrent_confirms_respect_max_open_positions):
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception:
            record(fn.__name__, False, "Testfall selbst abgestürzt:\n" + traceback.format_exc())

    print("\n=== ZUSAMMENFASSUNG ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    for name, ok, detail in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} Checks bestanden.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
