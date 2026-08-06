"""
test_graceful_shutdown_entry_cycle.py – Integrationstest für den Graceful-
Shutdown-Fix (2026-08-06, siehe trading_shared/graceful_shutdown.py):
verifiziert am ECHTEN main.run_entry_cycle() – nicht nur am generischen
Decorator-Test in trading_shared – dass ein SIGTERM MITTEN im Scan den
Zyklus NICHT abbricht. Genau das war der Incident vom 06.08.: ein
`systemctl restart` mitten im Scan killte den Prozess sofort, ein später
fertiggewordener Kandidat hätte über der Kaufschwelle liegen können, ohne
dass es jemand erfährt.

Deckt NUR den bot-spezifischen Teil ab (AUFGABE 3, Punkt 8: Zyklus läuft
trotz SIGTERM mittendrin sauber zu Ende, scan_log wird vollständig
geschrieben; UND kein neuer Zyklus startet danach mehr). Die generischen
Mechanismen (Punkt 9: Grace-Period-Fallback bei einem hängenden Zyklus;
Punkt 10: SIGTERM AUSSERHALB eines Zyklus läuft sofort/ohne Verzögerung
durch) sind bot-agnostisch in graceful_shutdown.py implementiert und dort
in trading_shared/test_graceful_shutdown.py generisch abgedeckt –
@cycle_guard verhält sich identisch, unabhängig davon welche Funktion es
dekoriert (run_entry_cycle hier oder eine synthetische Testfunktion dort).
Eine Wiederholung derselben Timing-Logik hier wäre keine zusätzliche
Abdeckung, nur ein langsamerer Testlauf.

KEINE echte Alpaca-Verbindung/kein echtes yfinance: rule_engine.analyze_ticker/
check_vix, main.get_alpaca_account_snapshot und notifications.send_email sind
gemockt. os._exit ist gemockt (würde sonst den Testprozess selbst beenden).
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB:

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER graceful_shutdown_tmp WITH PASSWORD 'graceful_shutdown_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_graceful_shutdown_test OWNER graceful_shutdown_tmp;"
    python3 test_graceful_shutdown_entry_cycle.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_graceful_shutdown_test;"
    sudo -u postgres psql -c "DROP USER graceful_shutdown_tmp;"

NIEMALS gegen die echte Produktions-DB oder ein echtes Live-Konto ausführen.
"""
import os
import sys
import signal
import threading
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://graceful_shutdown_tmp:graceful_shutdown_tmp_pw@localhost:5432/alpaca_graceful_shutdown_test",
)
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
import main as bot_main  # noqa: E402
import rule_engine  # noqa: E402
from trading_shared import graceful_shutdown as gs  # noqa: E402
from database import EntryTimeSlot, ScanLog  # noqa: E402
from config import ACTIVE_SHORT_INSTRUMENTS  # noqa: E402

database.init_db()

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_all_state():
    with database.get_session() as session:
        session.query(database.ScanLog).delete()
        session.query(database.Trade).delete()
        session.query(EntryTimeSlot).delete()
        # bot_config seeded ALPACA_DRAIN_MODE default ist bewusst "true"
        # (Sicherheitsnetz für frisch aufgesetzte DBs) – für diesen Test
        # muss der Zyklus tatsächlich scannen, daher explizit deaktiviert.
        database.set_bot_config(session, "ALPACA_DRAIN_MODE", "false")
        session.commit()


def reset_shutdown_state():
    if gs._shutdown_timer is not None:
        gs._shutdown_timer.cancel()
    gs._shutdown_timer = None
    gs._shutdown_requested.clear()
    gs._bot_label = "Bot"
    gs._grace_period_sec = gs.DEFAULT_GRACE_PERIOD_SEC
    with gs._active_cycle_lock:
        gs._active_cycle_count = 0
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def seed_active_slot(session, stunde_et=9, minute_et=45, max_trades_per_slot=5):
    """entry_time_slots braucht mindestens eine aktive Zeile, damit
    get_trades_for_slot()'s eigene DB-Abfrage (remaining_slots) funktioniert.
    Das slot-Argument von run_entry_cycle() selbst ist absichtlich ein
    einfaches SimpleNamespace statt eines ORM-Objekts (siehe unten) – vermeidet
    SQLAlchemy-Detached-Instance-Fallstricke beim Aufruf aus einem eigenen
    Thread heraus."""
    session.add(EntryTimeSlot(
        stunde_et=stunde_et, minute_et=minute_et,
        max_trades_per_slot=max_trades_per_slot, aktiv=True,
    ))
    session.commit()


def make_slot(stunde_et=9, minute_et=45, max_trades_per_slot=5):
    return SimpleNamespace(stunde_et=stunde_et, minute_et=minute_et,
                            max_trades_per_slot=max_trades_per_slot, gewichtung=1.0)


def make_fake_analyze_ticker(delay_sec):
    """Simuliert einen langsamen (aber nie hängenden) Scan – lang genug, dass
    ein SIGTERM zuverlässig MITTEN im Scan ankommt, kurz genug, dass der
    Test schnell bleibt. Alle Kandidaten kommen als NICHT freigegeben zurück
    (approved=False), damit der Test nur den Scan/Log-Pfad prüft, nicht
    Guardrails/LLM/Order-Platzierung (bereits durch andere Tests abgedeckt)."""
    def fake_analyze_ticker(ticker):
        time.sleep(delay_sec)
        return rule_engine.SignalResult(
            ticker=ticker, score=10, direction="LONG", instrument_type="STOCK",
            approved=False, ko_reason="TEST_MOCK_NOT_APPROVED",
        )
    return fake_analyze_ticker


def test_a_sigterm_mid_scan_completes_cycle_fully():
    reset_shutdown_state()
    wipe_all_state()
    with database.get_session() as session:
        seed_active_slot(session)
    slot = make_slot()

    with patch("rule_engine.analyze_ticker", side_effect=make_fake_analyze_ticker(0.3)), \
         patch("rule_engine.check_vix", return_value=(15.0, True)), \
         patch("main.get_alpaca_account_snapshot", return_value=None), \
         patch("broker.get_alpaca_account_snapshot", return_value=None), \
         patch("main.get_connected_alpaca_users", return_value=[]), \
         patch("notifications.send_email"), \
         patch("os._exit") as mock_exit:

        gs.install_sigterm_handler(bot_label="TestAlpaca", grace_period_sec=10)

        cycle_thread = threading.Thread(target=bot_main.run_entry_cycle, args=(slot,))
        cycle_thread.start()

        time.sleep(0.05)  # Zyklus ist jetzt sicher mitten im Scan (0.3s Mock-Delay pro Ticker)
        ok_active = gs._any_cycle_active()
        record("a) Zyklus ist zum Zeitpunkt des SIGTERM tatsächlich aktiv (Testvoraussetzung)", ok_active)

        os.kill(os.getpid(), signal.SIGTERM)

        cycle_thread.join(timeout=5)
        ok_finished = not cycle_thread.is_alive()
        record("a) Entry-Zyklus lief trotz SIGTERM mittendrin bis zum Ende durch (kein Hänger)",
               ok_finished)

        ok_exit = mock_exit.called and mock_exit.call_args[0][0] == 0
        record("a) Prozess-Exit war sauber (os._exit(0)), NICHT der 1-Fallback",
               ok_exit, f"called={mock_exit.called} args={mock_exit.call_args}")

    # ACTIVE_SHORT_INSTRUMENTS ist der einzige Teil der Watchlist, der ohne
    # Fair-Value-Cache-Eintrag gescannt wird (frische Test-DB -> leerer
    # fair_value_cache -> Long-Watchlist komplett rausgefiltert, siehe
    # run_entry_cycle Schritt 4) – dadurch bleibt der Scan klein und schnell,
    # ohne die Watchlist im Test künstlich mocken zu müssen.
    expected_tickers = set(ACTIVE_SHORT_INSTRUMENTS)

    with database.get_session() as session:
        logged_tickers = {row.ticker for row in session.query(ScanLog).all()}

    ok_complete = logged_tickers == expected_tickers
    record("a) scan_log enthält ALLE gescannten Kandidaten (Scan wurde nicht abgeschnitten)",
           ok_complete, f"erwartet={expected_tickers} geloggt={logged_tickers}")

    reset_shutdown_state()


def test_b_no_new_cycle_starts_after_shutdown_requested():
    # Baut auf dem Zustand von Test A NICHT auf (eigener, isolierter Lauf) –
    # provoziert das SIGTERM zuerst im Leerlauf (kein aktiver Zyklus), dann
    # wird geprüft, dass ein DANACH aufgerufener run_entry_cycle() sofort
    # überspringt, ohne zu scannen/zu loggen.
    reset_shutdown_state()
    wipe_all_state()
    with database.get_session() as session:
        seed_active_slot(session)
    slot = make_slot()

    with patch("rule_engine.analyze_ticker", side_effect=make_fake_analyze_ticker(0.01)), \
         patch("rule_engine.check_vix", return_value=(15.0, True)), \
         patch("main.get_alpaca_account_snapshot", return_value=None), \
         patch("broker.get_alpaca_account_snapshot", return_value=None), \
         patch("main.get_connected_alpaca_users", return_value=[]), \
         patch("notifications.send_email"), \
         patch("os._exit") as mock_exit:

        gs.install_sigterm_handler(bot_label="TestAlpaca", grace_period_sec=10)
        gs._shutdown_requested.set()  # simuliert: SIGTERM ist bereits eingegangen

        bot_main.run_entry_cycle(slot)

        with database.get_session() as session:
            row_count = session.query(ScanLog).count()
        ok = row_count == 0
        record("b) run_entry_cycle() nach bereits angefordertem Shutdown scannt NICHTS mehr (0 scan_log-Zeilen)",
               ok, f"scan_log-Zeilen={row_count}")

    reset_shutdown_state()


def main():
    tests = (
        test_a_sigterm_mid_scan_completes_cycle_fully,
        test_b_no_new_cycle_starts_after_shutdown_requested,
    )
    for fn in tests:
        print(f"\n--- {fn.__name__} ---")
        try:
            fn()
        except Exception:
            record(fn.__name__, False, "Testfall selbst abgestürzt:\n" + traceback.format_exc())
            reset_shutdown_state()

    print("\n=== ZUSAMMENFASSUNG ===")
    failed = [n for n, ok, _ in RESULTS if not ok]
    for name, ok, _ in RESULTS:
        print(f"{'✅' if ok else '❌'} {name}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} Checks bestanden.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
