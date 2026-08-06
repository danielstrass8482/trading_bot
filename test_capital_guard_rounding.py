"""
test_capital_guard_rounding.py – Regressionstest für den Float-Rundungs-Bug
in broker.check_guardrails() Punkt 6 (2026-08-06, siehe broker.py:395-427
Kommentar "BUGFIX 2026-08-06").

Live beobachtet 2026-08-06: bei Bot-Anteil-Prozent = 100% ist
effective_max_capital_total (GERUNDET via round(..., 2)) mathematisch
identisch zu real_total_capital (bisher UNGERUNDET verglichen) – Binär-
fließkomma konnte beim Runden minimal nach oben kippen und den Guard
fälschlich auslösen, obwohl beide Werte auf 2 Dezimalstellen identisch
angezeigt wurden ("$467.17 vs $467.17"). 110 Kandidaten in 2 von 3
Scan-Zyklen wurden dadurch an diesem Tag fälschlich übersprungen (u.a.
Score 98). Fix: real_total_capital wird für den Vergleich auf dieselbe
Cent-Genauigkeit gerundet wie effective_max_capital_total.

KEINE echte Alpaca-Verbindung: get_alpaca_account_snapshot() wird gemockt,
nie ein echter Netzwerk-Call. KEINE Produktions-DB: DATABASE_URL zeigt auf
eine eigene Wegwerf-Postgres-DB (SQLite scheitert an den Postgres-
spezifischen "ADD COLUMN IF NOT EXISTS"-Migrationen in database.py) – vor
dem Lauf einmalig anlegen, danach wieder droppen (analog
test_loss_streak_cooldown.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER capguard_test_tmp WITH PASSWORD 'capguard_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_capguard_test OWNER capguard_test_tmp;"
    python3 test_capital_guard_rounding.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_capguard_test;"
    sudo -u postgres psql -c "DROP USER capguard_test_tmp;"

DATABASE_URL kann per Env-Var überschrieben werden. NIEMALS gegen die echte
Produktions-DB oder ein echtes Live-Konto ausführen.

Deckt ab:
  (a/b/c) Grenzfall-Regression: die drei heute live beobachteten Werte-Paare
      (467.17/467.17, 467.80/467.80, plus 467.65/467.65 aus der Live-
      Diagnose) als exakte Float-Konstruktion (math.nextafter direkt unter
      dem gerundeten Wert) -> Guard darf NICHT mehr auslösen.
  (d) Kontrolltest: bot_pct fehlerhaft auf 150% konfiguriert (deutlich mehr
      als Rundungsrauschen) -> Guard MUSS weiterhin zuverlässig auslösen.
"""
import math
import os
import sys
import traceback
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://capguard_test_tmp:capguard_test_tmp_pw@localhost:5432/alpaca_capguard_test",
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

# Isoliert den Test von den unabhängigen Guards 2/3/5/5b (Tageslimit/offene
# Positionen/Verlustlimit/Verlustserie-Cooldown) – dieser Test prüft NUR
# Guard 6 (Kapital-Limit vs. echtes Kapital).
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
        session.commit()


def make_signal(ticker="TEST", score=90):
    return SignalResult(
        ticker=ticker, score=score, direction="LONG", instrument_type="STOCK",
        approved=True, current_price=100.0, stop_loss=95.0, take_profit=110.0,
    )


def one_ulp_below(rounded_target: float) -> float:
    """Liefert den größten Float direkt UNTER rounded_target, der bei
    round(x, 2) trotzdem wieder auf rounded_target rundet – reproduziert
    exakt den live beobachteten Grenzfall (Summe zweier Floats landet
    hauchdünn unterhalb des 'glatten' Cent-Werts)."""
    x = math.nextafter(rounded_target, -math.inf)
    assert round(x, 2) == rounded_target and x < rounded_target, \
        f"Testkonstruktion fehlgeschlagen für {rounded_target}: x={x!r} round(x,2)={round(x, 2)!r}"
    return x


def run_rounding_boundary_case(name, rounded_value):
    wipe_all_state()
    cash = one_ulp_below(rounded_value)
    with patch.object(broker, "get_alpaca_account_snapshot",
                       return_value={"cash": cash, "buying_power": cash, "equity": cash,
                                     "long_market_value": 0.0, "unrealized_pl": 0.0}), \
         patch.object(broker, "get_total_capital_in_trades", return_value=0.0), \
         patch.object(broker, "get_or_seed_capital_allocations",
                       return_value={"bot": 100.0, "active_trading": 0.0}):
        try:
            broker.check_guardrails(make_signal(), DEFAULT_USER_ID)
            record(name, True)
        except broker.GuardrailViolation as gv:
            record(name, False, f"fälschlich ausgelöst trotz cash==configured (auf 2 Dez.): {gv}")
        except Exception as e:
            record(name, False, f"unerwarteter Exception-Typ: {repr(e)}")


# ─────────────────────────────────────────────
# (a/b/c) Grenzfall-Regression: heute live beobachtete Werte
# ─────────────────────────────────────────────
def test_a_boundary_46717():
    run_rounding_boundary_case("a) Grenzfall $467.17/$467.17 (16:01-Scan) -> Guard darf NICHT auslösen", 467.17)


def test_b_boundary_46780():
    run_rounding_boundary_case("b) Grenzfall $467.80/$467.80 (14:31-Scan) -> Guard darf NICHT auslösen", 467.80)


def test_c_boundary_46765():
    run_rounding_boundary_case("c) Grenzfall $467.65/$467.65 (Live-Diagnose-Snapshot) -> Guard darf NICHT auslösen", 467.65)


# ─────────────────────────────────────────────
# (d) Kontrolltest: ECHTER Kapitalüberschuss muss weiterhin blockiert werden
# ─────────────────────────────────────────────
def test_d_real_violation_still_blocks():
    wipe_all_state()
    cash = 100.0
    with patch.object(broker, "get_alpaca_account_snapshot",
                       return_value={"cash": cash, "buying_power": cash, "equity": cash,
                                     "long_market_value": 0.0, "unrealized_pl": 0.0}), \
         patch.object(broker, "get_total_capital_in_trades", return_value=0.0), \
         patch.object(broker, "get_or_seed_capital_allocations",
                       return_value={"bot": 150.0, "active_trading": -50.0}):
        # effective_max = round(100.0 * 150/100, 2) = 150.00, real = 100.00
        # -> Differenz $50, weit über jedem Cent-Rundungsrauschen.
        try:
            broker.check_guardrails(make_signal(), DEFAULT_USER_ID)
            record("d) bot_pct=150% (echter Konfigurationsfehler) -> GuardrailViolation erwartet",
                   False, "keine Exception geworfen!")
        except broker.GuardrailViolation as gv:
            ok = "150.00" in str(gv) and "100.00" in str(gv)
            record("d) bot_pct=150% (echter Konfigurationsfehler) -> GuardrailViolation erwartet", ok, str(gv))
        except Exception as e:
            record("d) bot_pct=150% (echter Konfigurationsfehler) -> GuardrailViolation erwartet",
                   False, f"falscher Exception-Typ: {repr(e)}")


def main():
    for fn in (test_a_boundary_46717, test_b_boundary_46780, test_c_boundary_46765,
               test_d_real_violation_still_blocks):
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
