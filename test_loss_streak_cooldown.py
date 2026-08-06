"""
test_loss_streak_cooldown.py – Mock/Test-DB-basierter Test für den
Verlustserie-Cooldown (AUFGABE 1, 2026-08-06, siehe database.
get_loss_streak_state/_record_loss_streak_result und broker.check_guardrails
Punkt 5b).

KEINE echte Alpaca-Verbindung: get_alpaca_account_snapshot() wird auf None
gemockt (Guard 6, das echte-Kapital-Fail-Safe, wird dadurch übersprungen,
siehe broker.check_guardrails – kein Netzwerk-Call nötig, um NUR den neuen
Verlustserie-Guard zu testen).
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(SQLite scheitert an den Postgres-spezifischen "ADD COLUMN IF NOT EXISTS"-
Migrationen in database.py) – vor dem Lauf einmalig anlegen, danach wieder
droppen (analog trading_bot_saxo/test_capital_guard_idempotency.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER losstreak_test_tmp WITH PASSWORD 'losstreak_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_losstreak_test OWNER losstreak_test_tmp;"
    python3 test_loss_streak_cooldown.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_losstreak_test;"
    sudo -u postgres psql -c "DROP USER losstreak_test_tmp;"

DATABASE_URL kann per Env-Var überschrieben werden. NIEMALS gegen die echte
Produktions-DB oder ein echtes Live-Konto ausführen.

Deckt drei Szenarien ab (AUFGABE 3):
  (a) MAX_CONSECUTIVE_LOSSES Verlust-Trades in Folge -> Cooldown aktiv,
      check_guardrails() blockiert neue Entries
  (b) Gewinn-Trade zwischendrin -> Zähler setzt korrekt zurück, kein Cooldown
  (c) abgelaufener Cooldown -> automatische Freigabe, Zähler zurück auf 0,
      Bot nimmt wieder normal Kandidaten an
"""
import os
import sys
import traceback
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://losstreak_test_tmp:losstreak_test_tmp_pw@localhost:5432/alpaca_losstreak_test",
)
# Leere Broker-Credentials -> jeder versehentliche echte Netzwerk-Call
# schlägt sofort und offensichtlich fehl, statt "zufällig" zu funktionieren.
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

# Isoliert den Test von den unabhängigen Guards 2/3 (Tageslimit/offene
# Positionen, siehe broker.check_guardrails) - dieser Test erzeugt pro
# Testfall mehrere Trades "heute", was sonst zufällig mit dem Default
# MAX_TRADES_PER_DAY=3 kollidieren und Guard 2 statt des zu testenden
# Guards 5b (Verlustserie-Cooldown) auslösen würde.
with database.get_session() as session:
    database.set_bot_config(session, "MAX_TRADES_PER_DAY", "9999")
    database.set_bot_config(session, "MAX_OPEN_POSITIONS", "9999")
    session.commit()

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_all_state():
    with database.get_session() as session:
        session.query(database.Trade).delete()
        session.query(database.BotState).delete()
        session.commit()


def make_open_trade(ticker, entry_price=100.0, quantity=1.0):
    with database.get_session() as session:
        trade = database.Trade(
            ticker=ticker, direction="LONG", instrument_type="STOCK",
            entry_price=entry_price, stop_loss=entry_price * 0.95, take_profit=entry_price * 1.1,
            quantity=quantity, capital_used=entry_price * quantity, rule_score=80,
            status="OPEN", mode="PAPER", broker="alpaca", user_id=DEFAULT_USER_ID,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade.id


def close(trade_id, exit_price):
    """Simuliert einen abgeschlossenen Trade über denselben Codepfad, den
    broker.py an allen 4 Call-Sites nutzt (database.close_trade), damit der
    Test exakt die Produktionslogik trifft."""
    with database.get_session() as session:
        trade = session.query(database.Trade).filter_by(id=trade_id).first()
        database.close_trade(session, trade, exit_price, "CLOSED_MANUAL")
        session.commit()


def make_signal(ticker="TEST"):
    return SignalResult(
        ticker=ticker, score=80, direction="LONG", instrument_type="STOCK",
        approved=True, current_price=100.0, stop_loss=95.0, take_profit=110.0,
    )


def guardrails_pass_cleanly(ticker="TEST"):
    """check_guardrails() ohne die neue Verlustserie-Sperre: get_alpaca_
    account_snapshot() auf None gemockt, damit Guard 6 (echtes Kapital,
    Netzwerk-Call) übersprungen wird - isoliert den Test auf Guard 5b."""
    signal = make_signal(ticker)
    with patch.object(broker, "get_alpaca_account_snapshot", return_value=None):
        broker.check_guardrails(signal, user_id=DEFAULT_USER_ID)


# ─────────────────────────────────────────────
# (a) MAX_CONSECUTIVE_LOSSES Verlust-Trades in Folge -> Cooldown aktiv
# ─────────────────────────────────────────────
def test_a_loss_streak_triggers_cooldown():
    wipe_all_state()

    with database.get_session() as session:
        cfg = database.get_user_live_config(DEFAULT_USER_ID)
    max_losses = cfg["MAX_CONSECUTIVE_LOSSES"]

    # Vor der Verlustserie: Entry unproblematisch möglich.
    try:
        guardrails_pass_cleanly("PRE")
        record("a) vor Verlustserie: check_guardrails() lässt Entry durch", True)
    except Exception as e:
        record("a) vor Verlustserie: check_guardrails() lässt Entry durch", False, repr(e))

    # MAX_CONSECUTIVE_LOSSES Verlust-Trades hintereinander schließen (Entry @
    # 100, Exit @ 99 -> jeweils ein klarer, aber bewusst KLEINER Verlust:
    # der Zähler ist von der Höhe unabhängig, siehe AUFGABE 1 Punkt 2 - ein
    # großer Verlust würde stattdessen zusätzlich das unabhängige, bereits
    # bestehende Tagesverlustlimit auslösen (Guard 5) und den hier zu
    # testenden Guard 5b maskieren).
    for i in range(max_losses):
        trade_id = make_open_trade(f"LOSS{i}")
        close(trade_id, 99.0)

    with database.get_session() as session:
        state = database.get_loss_streak_state(session, DEFAULT_USER_ID)
    ok = state["consecutive_losses"] >= max_losses and state["cooldown_active"]
    record(
        f"a) nach {max_losses} Verlust-Trades: Cooldown aktiv",
        ok, f"consecutive_losses={state['consecutive_losses']} cooldown_active={state['cooldown_active']}"
    )

    try:
        guardrails_pass_cleanly("BLOCKED")
        record("a) check_guardrails() blockiert neuen Entry während Cooldown", False, "keine Exception geworfen!")
    except broker.GuardrailViolation as gv:
        ok = "Verlustserie-Cooldown" in str(gv)
        record("a) check_guardrails() blockiert neuen Entry während Cooldown", ok, str(gv))
    except Exception as e:
        record("a) check_guardrails() blockiert neuen Entry während Cooldown", False, f"falscher Exception-Typ: {repr(e)}")


# ─────────────────────────────────────────────
# (b) Gewinn-Trade zwischendrin -> Zähler setzt korrekt zurück
# ─────────────────────────────────────────────
def test_b_win_resets_counter():
    wipe_all_state()

    # 2 Verluste, dann 1 Gewinn -> Zähler muss auf 0 zurückfallen (kein
    # Cooldown, unabhängig davon wie viele Verluste VOR dem Gewinn kamen).
    # Bewusst kleine Beträge, siehe Kommentar in test_a (Tagesverlustlimit
    # nicht mit-triggern).
    close(make_open_trade("L1"), 99.0)
    close(make_open_trade("L2"), 99.0)
    close(make_open_trade("W1"), 101.0)  # Gewinn (Entry 100 -> Exit 101)

    with database.get_session() as session:
        state = database.get_loss_streak_state(session, DEFAULT_USER_ID)
    ok = state["consecutive_losses"] == 0 and not state["cooldown_active"]
    record(
        "b) Gewinn-Trade setzt Verlustserie-Zähler auf 0 zurück",
        ok, f"consecutive_losses={state['consecutive_losses']} cooldown_active={state['cooldown_active']}"
    )

    try:
        guardrails_pass_cleanly("AFTER_WIN")
        record("b) check_guardrails() lässt Entry nach Reset wieder durch", True)
    except Exception as e:
        record("b) check_guardrails() lässt Entry nach Reset wieder durch", False, repr(e))


# ─────────────────────────────────────────────
# (c) abgelaufener Cooldown -> automatische Freigabe, Zähler auf 0
# ─────────────────────────────────────────────
def test_c_cooldown_expiry_resumes_trading():
    wipe_all_state()

    with database.get_session() as session:
        cfg = database.get_user_live_config(DEFAULT_USER_ID)
    max_losses = cfg["MAX_CONSECUTIVE_LOSSES"]

    for i in range(max_losses):
        close(make_open_trade(f"EXP{i}"), 99.0)

    with database.get_session() as session:
        state_before = database.get_loss_streak_state(session, DEFAULT_USER_ID)
    if not state_before["cooldown_active"]:
        record("c) Vorbedingung (Cooldown aktiv) erfüllt", False, "Cooldown wurde nicht ausgelöst - Test ungültig")
        return

    # Cooldown-Ende künstlich in die Vergangenheit setzen (simuliert Ablauf
    # der COOLDOWN_HOURS_AFTER_LOSS_STREAK Stunden, ohne echte Wartezeit).
    with database.get_session() as session:
        database.BotState.set(session, "loss_streak_cooldown_until", (datetime.utcnow() - timedelta(minutes=1)).isoformat())
        session.commit()

    with database.get_session() as session:
        state_after = database.get_loss_streak_state(session, DEFAULT_USER_ID)
    ok = not state_after["cooldown_active"] and state_after["consecutive_losses"] == 0
    record(
        "c) abgelaufener Cooldown wird automatisch aufgeräumt (Zähler auf 0)",
        ok, f"consecutive_losses={state_after['consecutive_losses']} cooldown_active={state_after['cooldown_active']}"
    )

    try:
        guardrails_pass_cleanly("AFTER_EXPIRY")
        record("c) check_guardrails() lässt Entry nach Cooldown-Ablauf wieder durch", True)
    except Exception as e:
        record("c) check_guardrails() lässt Entry nach Cooldown-Ablauf wieder durch", False, repr(e))


def main():
    for fn in (test_a_loss_streak_triggers_cooldown, test_b_win_resets_counter, test_c_cooldown_expiry_resumes_trading):
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
