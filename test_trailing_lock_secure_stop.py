"""
test_trailing_lock_secure_stop.py – Mock/Test-DB-basierter Test für die
Trailing-Gewinnsicherung des Tag-5-Grant-Mechanismus (2026-08-13, siehe
broker.monitor_open_positions), die die alte "hälftige Sicherung vom
MOMENTAN-Gewinn"-Logik ersetzt.

KEINE echte Alpaca-Verbindung: broker._time_exit_currently_allowed() wird
fest auf True gemockt (der eigentliche Guard prüft NYSE-Handelszeiten über
einen echten Alpaca-Client, hier bewusst irrelevant für die zu testende
Stop-Berechnung), yfinance.Ticker() wird auf einen fixen current_price
gemockt (kein Netzwerk-Call).
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(SQLite scheitert an den Postgres-spezifischen "ADD COLUMN IF NOT EXISTS"-
Migrationen in database.py) – vor dem Lauf einmalig anlegen, danach wieder
droppen (analog test_loss_streak_cooldown.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER traillock_test_tmp WITH PASSWORD 'traillock_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_traillock_test OWNER traillock_test_tmp;"
    python3 test_trailing_lock_secure_stop.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_traillock_test;"
    sudo -u postgres psql -c "DROP USER traillock_test_tmp;"

DATABASE_URL kann per Env-Var überschrieben werden. NIEMALS gegen die echte
Produktions-DB oder ein echtes Live-Konto ausführen.

Deckt ab (siehe Aufgabe "Trailing-Gewinnsicherung Tag-5-Grant", 2026-08-13):
  1) Normalfall: höchster Gewinn 2%, Sicherungsfaktor 50% -> Stop bei Entry+1%
  2) Mindestabstand-Regel greift: theoretischer Stop zu nah am aktuellen Kurs
  3) Randfall: Kurs bereits unter dem theoretischen Stop-Niveau gefallen ->
     bestehender Stop bleibt unverändert, nur Schutzfrist verlängert
  4) Mindestschwelle: höchster Gewinn < TRAILING_LOCK_MIN_PROFIT_PCT -> keine
     Sonderbehandlung, regulärer sofortiger Time-Exit
  5) LEA-Regression (echter Vorfall 2026-08-12, siehe Memory
     loss-streak-diagnose-2026-08-12.md): reproduziert die dokumentierten
     Zahlen (Entry 121.076 USD, Kurs bei Tag-5-Check 123.26 USD, ~1.8% Gewinn)
     und zeigt zusätzlich anhand einer realistischen Variante mit einem
     früheren, höheren Tages-Hoch, wie die neue Randfall-Regel einen künstlich
     zu engen Stop verhindert. WICHTIG: der exakte historische
     highest_price_since_entry-Wert über den gesamten 5-tägigen Haltezeitraum
     ist NICHT dokumentiert (nur der Kurs im Moment des Tag-5-Checks selbst,
     123.26 USD) - Testfall 5b verwendet daher einen plausiblen,
     KENNTLICH GEMACHTEN Rekonstruktions-Wert, keine verifizierte Historie.
  6) Bestehende Tests dürfen nicht brechen (siehe separater Lauf der übrigen
     test_*.py in derselben Session).
"""
import os
import sys
import traceback
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://traillock_test_tmp:traillock_test_tmp_pw@localhost:5432/alpaca_traillock_test",
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

database.init_db()

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_all_state():
    with database.get_session() as session:
        session.query(database.Trade).delete()
        session.query(database.PostExitTracking).delete()
        session.commit()


def set_config(**kwargs):
    with database.get_session() as session:
        for key, value in kwargs.items():
            database.set_bot_config(session, key, str(value))
        session.commit()


def make_open_trade(ticker, entry_price, highest_price_since_entry, stop_loss, take_profit=None, quantity=1.0, days_ago=12):
    """created_at 12 Kalendertage in der Vergangenheit -> garantiert >= 5
    Handelstage (Mo-Fr), unabhängig vom aktuellen Wochentag."""
    with database.get_session() as session:
        trade = database.Trade(
            created_at=datetime.utcnow() - timedelta(days=days_ago),
            ticker=ticker, direction="LONG", instrument_type="STOCK",
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit if take_profit is not None else entry_price * 1.2,
            quantity=quantity, capital_used=entry_price * quantity, rule_score=80,
            status="OPEN", mode="PAPER", broker="alpaca", user_id=DEFAULT_USER_ID,
            highest_price_since_entry=highest_price_since_entry,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade.id


def get_trade(trade_id):
    with database.get_session() as session:
        return session.query(database.Trade).filter_by(id=trade_id).first()


def run_monitor_cycle(current_price):
    """Führt genau einen monitor_open_positions()-Zyklus aus, mit gemocktem
    Alpaca-Time-Exit-Guard (immer erlaubt) und gemocktem yfinance-Kurs."""
    fake_ticker = MagicMock()
    fake_ticker.fast_info.get.return_value = current_price
    with patch.object(broker, "_time_exit_currently_allowed", return_value=True), \
         patch("yfinance.Ticker", return_value=fake_ticker):
        broker.monitor_open_positions(user_id=DEFAULT_USER_ID)


def run_monitor_cycle_with_sell(current_price, fill_price=None):
    """Wie run_monitor_cycle(), aber zusätzlich _sell_position_at_alpaca()
    gemockt (simuliert eine sofort erfolgreiche Verkaufs-Order) - für Fälle,
    in denen ein sofortiger Time-Exit erwartet wird."""
    fake_ticker = MagicMock()
    fake_ticker.fast_info.get.return_value = current_price
    with patch.object(broker, "_time_exit_currently_allowed", return_value=True), \
         patch("yfinance.Ticker", return_value=fake_ticker), \
         patch.object(broker, "_sell_position_at_alpaca", return_value=fill_price if fill_price is not None else current_price):
        broker.monitor_open_positions(user_id=DEFAULT_USER_ID)


# ─────────────────────────────────────────────
# 1) Normalfall: höchster Gewinn 2%, Sicherungsfaktor 50% -> Stop bei Entry+1%
# ─────────────────────────────────────────────
def test_1_normalfall():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 100.0
    highest = 102.0  # +2%
    trade_id = make_open_trade("NORM", entry_price=entry, highest_price_since_entry=highest, stop_loss=95.0)

    run_monitor_cycle(current_price=highest)

    trade = get_trade(trade_id)
    ok = (
        trade.status == "OPEN"
        and trade.time_exit_grace_used is True
        and trade.trailing_lock_rule_applied == "NORMAL"
        and abs(trade.stop_loss - 101.0) < 1e-6
        and abs(trade.trailing_lock_calculated_stop - 101.0) < 1e-6
        and abs(trade.trailing_lock_highest_profit_pct_at_grant - 0.02) < 1e-4
    )
    record(
        "1) Normalfall: Stop bei Entry+1% (halber 2%-Peak-Gewinn)", ok,
        f"stop_loss={trade.stop_loss} rule={trade.trailing_lock_rule_applied} "
        f"calc={trade.trailing_lock_calculated_stop} peak_pct={trade.trailing_lock_highest_profit_pct_at_grant}"
    )


# ─────────────────────────────────────────────
# 2) Mindestabstand-Regel: theoretischer Stop zu nah am aktuellen Kurs
# ─────────────────────────────────────────────
def test_2_mindestabstand():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 100.0
    highest = 100.6  # +0.6% (über der 0.3%-Mindestschwelle)
    current = 100.5  # leicht unter dem Peak, aber der theoretische Stop (100.3)
                      # liegt dadurch nur noch 0.2% unter current -> zu knapp
    trade_id = make_open_trade("BUFF", entry_price=entry, highest_price_since_entry=highest, stop_loss=95.0)

    run_monitor_cycle(current_price=current)

    trade = get_trade(trade_id)
    theoretical_stop = round(entry + (highest - entry) * 0.5, 2)
    expected_min_buffer_stop = round(current * (1 - 0.005), 2)
    ok = (
        trade.status == "OPEN"
        and trade.trailing_lock_rule_applied == "MIN_BUFFER"
        and abs(trade.stop_loss - expected_min_buffer_stop) < 1e-6
        and trade.stop_loss != theoretical_stop
        and abs(trade.trailing_lock_calculated_stop - theoretical_stop) < 1e-6
    )
    record(
        "2) Mindestabstand: Stop auf Mindestabstand statt zu nahem theoretischem Wert", ok,
        f"stop_loss={trade.stop_loss} expected_min_buffer={expected_min_buffer_stop} "
        f"theoretical={theoretical_stop} rule={trade.trailing_lock_rule_applied}"
    )


# ─────────────────────────────────────────────
# 3) Randfall: Kurs bereits unter dem theoretischen Stop-Niveau gefallen ->
#    bestehender Stop bleibt unverändert, nur Schutzfrist verlängert, kein
#    Zwangsverkauf.
# ─────────────────────────────────────────────
def test_3_randfall():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 100.0
    highest = 105.0  # +5% Peak (bewusst UNTER der 6%-Trailing-Aktivierungsschwelle,
                      # sonst wäre trailing_sl_active längst aktiv und der
                      # Tag-5-Grant-Zweig würde nie erreicht)
    current = 101.0  # deutlich vom Peak zurückgefallen, theoretischer Stop
                      # (102.5 = 100 + 5*0.5) liegt jetzt ÜBER dem aktuellen Kurs
    old_stop = 94.0   # bestehender (z.B. ATR-basierter) Stop
    trade_id = make_open_trade("EDGE", entry_price=entry, highest_price_since_entry=highest, stop_loss=old_stop)

    run_monitor_cycle(current_price=current)

    trade = get_trade(trade_id)
    theoretical_stop = round(entry + (highest - entry) * 0.5, 2)
    ok = (
        trade.status == "OPEN"  # KEIN Zwangsverkauf
        and trade.trailing_lock_rule_applied == "EDGE_CASE_UNCHANGED"
        and abs(trade.stop_loss - old_stop) < 1e-6  # unverändert
        and trade.time_exit_grace_used is True  # Schutzfrist trotzdem gewährt
        and trade.time_exit_grace_deadline is not None
        and abs(trade.trailing_lock_calculated_stop - theoretical_stop) < 1e-6
    )
    record(
        "3) Randfall: alter Stop bleibt unverändert, kein Zwangsverkauf, Schutzfrist verlängert", ok,
        f"status={trade.status} stop_loss={trade.stop_loss} old_stop={old_stop} "
        f"rule={trade.trailing_lock_rule_applied} grace_deadline={trade.time_exit_grace_deadline}"
    )


# ─────────────────────────────────────────────
# 4) Mindestschwelle: höchster Gewinn < TRAILING_LOCK_MIN_PROFIT_PCT -> keine
#    Sonderbehandlung, regulärer sofortiger Time-Exit (auch wenn die
#    Position aktuell noch minimal im Plus steht).
# ─────────────────────────────────────────────
def test_4_mindestschwelle():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 100.0
    highest = 100.2  # +0.2%, UNTER der 0.3%-Mindestschwelle
    current = 100.1  # aktuell noch minimal im Plus
    trade_id = make_open_trade("TINY", entry_price=entry, highest_price_since_entry=highest, stop_loss=95.0)

    run_monitor_cycle_with_sell(current_price=current, fill_price=current)

    trade = get_trade(trade_id)
    ok = (
        trade.status == "CLOSED_TIME_EXIT"
        and trade.time_exit_grace_used is False  # keine Schutzfrist gewährt
        and trade.trailing_lock_rule_applied == "MIN_PROFIT_THRESHOLD"
        and trade.trailing_lock_calculated_stop is None
        and abs(trade.trailing_lock_highest_profit_pct_at_grant - 0.002) < 1e-4
    )
    record(
        "4) Mindestschwelle: winziger Peak-Gewinn -> sofortiger Time-Exit, keine Schutzfrist", ok,
        f"status={trade.status} grace_used={trade.time_exit_grace_used} rule={trade.trailing_lock_rule_applied}"
    )


# ─────────────────────────────────────────────
# 5a) LEA-Regression (dokumentierte Zahlen, siehe Memory
#     loss-streak-diagnose-2026-08-12.md): peak == current-at-grant (einziger
#     dokumentierter Wert) -> neue Logik darf NICHT schlechter sein als die
#     alte hälftige Momentan-Gewinn-Logik (Ergebnis hier identisch, da beide
#     Formeln bei peak==current mathematisch zusammenfallen).
# ─────────────────────────────────────────────
def test_5a_lea_regression_documented_numbers():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 121.076
    current_at_grant = 123.26  # dokumentierter Kurs beim Tag-5-Check
    old_half_gain_stop = round(entry + (current_at_grant - entry) / 2, 2)  # alte Formel, zum Vergleich

    trade_id = make_open_trade("LEA", entry_price=entry, highest_price_since_entry=current_at_grant, stop_loss=entry * 0.94)
    run_monitor_cycle(current_price=current_at_grant)

    trade = get_trade(trade_id)
    ok = (
        trade.status == "OPEN"
        and trade.trailing_lock_rule_applied == "NORMAL"
        and trade.stop_loss <= current_at_grant  # kein sofortiger Zwangsverkauf
        and abs(trade.stop_loss - old_half_gain_stop) < 0.02  # nicht schlechter als vorher (bei peak==current identisch)
    )
    record(
        "5a) LEA (dokumentierte Zahlen, peak==current): neue Logik nicht schlechter als alte", ok,
        f"new_stop={trade.stop_loss} old_half_gain_stop={old_half_gain_stop} rule={trade.trailing_lock_rule_applied}"
    )


# ─────────────────────────────────────────────
# 5b) LEA-artige Variante MIT rekonstruiertem früherem Tages-Hoch (NICHT Teil
#     der verifizierten Historie - der reale highest_price_since_entry-Verlauf
#     vor dem exakten Tag-5-Check-Zeitpunkt ist nicht dokumentiert, nur der
#     Kurs in genau diesem Moment). Realistische Annahme: LEA hatte an einem
#     der 5 Haltetage bereits ein höheres Hoch (124.50 statt 123.26) erreicht,
#     bevor der Kurs auf 123.26 zurückkam. Zeigt konkret, dass die neue
#     Randfall-Logik in diesem Fall NICHT den (jetzt schon wieder gefallenen)
#     Kurs für einen künstlich engen Stop heranzieht.
# ─────────────────────────────────────────────
def test_5b_lea_variant_with_reconstructed_earlier_peak():
    wipe_all_state()
    set_config(TRAILING_LOCK_SECURE_PCT="0.5", TRAILING_LOCK_MIN_BUFFER_PCT="0.005", TRAILING_LOCK_MIN_PROFIT_PCT="0.003")

    entry = 121.076
    reconstructed_peak = 130.0  # deutlich höheres, rekonstruiertes Tages-Hoch
    current_at_grant = 123.26   # dokumentierter Kurs beim Tag-5-Check (bereits zurückgefallen)
    old_stop = round(entry * 0.94, 2)  # bestehender ATR-Stop vor dem Grant

    theoretical_stop = round(entry + (reconstructed_peak - entry) * 0.5, 2)
    assert current_at_grant < theoretical_stop, "Testvoraussetzung: current muss unter theoretical_stop liegen (Randfall)"

    trade_id = make_open_trade("LEA2", entry_price=entry, highest_price_since_entry=reconstructed_peak, stop_loss=old_stop)
    run_monitor_cycle(current_price=current_at_grant)

    trade = get_trade(trade_id)
    ok = (
        trade.status == "OPEN"
        and trade.trailing_lock_rule_applied == "EDGE_CASE_UNCHANGED"
        and abs(trade.stop_loss - old_stop) < 1e-6  # NICHT auf einen künstlich engen Wert nachgezogen
        and trade.time_exit_grace_used is True
    )
    record(
        "5b) LEA-Variante (rekonstruierter höherer Peak): Randfall verhindert künstlich engen Stop", ok,
        f"stop_loss={trade.stop_loss} old_stop={old_stop} theoretical={theoretical_stop} rule={trade.trailing_lock_rule_applied}"
    )


def main():
    for fn in (
        test_1_normalfall, test_2_mindestabstand, test_3_randfall,
        test_4_mindestschwelle, test_5a_lea_regression_documented_numbers,
        test_5b_lea_variant_with_reconstructed_earlier_peak,
    ):
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
