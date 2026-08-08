"""
test_per_user_guardrails.py – Regressionstest für Aufgabe "Presets/
Kapitalaufteilung/Guardrails pro Nutzer" (2026-08-08).

Deckt die Kern-Codeänderungen ab, die zuvor STILLSCHWEIGEND immer Daniels
globale bot_config statt der eigenen Werte eines Kunden gelesen haben,
obwohl user_id an der jeweiligen Stelle längst vorlag:
  (a) database._record_loss_streak_result() – MAX_CONSECUTIVE_LOSSES/
      COOLDOWN_HOURS_AFTER_LOSS_STREAK jetzt pro Nutzer.
  (b) database.get_user_live_config() – alle 10 neu ergänzten Klasse-A-Keys
      (siehe DEFAULT_USER_CONFIG) werden pro Nutzer unabhängig geliefert,
      inkl. Isolation zwischen zwei verschiedenen Nicht-Owner-Nutzern.
  (c) broker.get_or_seed_capital_allocations()/get_effective_max_capital_
      total_bot() – Kapitalaufteilung ist jetzt pro Nutzer isoliert UND
      wirkt für Nicht-Owner genauso wie für Daniel (Prozent vom echten
      Broker-Kapital statt starrem Flat-Wert).
  (d) Daniels (DEFAULT_USER_ID) eigene Werte/Verhalten bleiben in JEDEM
      Szenario unverändert (Regressionsschutz für den Live-Account).

KEINE echte Alpaca-Verbindung: get_alpaca_account_snapshot() wird gemockt.
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(analog test_loss_streak_cooldown.py/test_capital_guard_rounding.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER peruser_test_tmp WITH PASSWORD 'peruser_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_peruser_test OWNER peruser_test_tmp;"
    python3 test_per_user_guardrails.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_peruser_test;"
    sudo -u postgres psql -c "DROP USER peruser_test_tmp;"

DATABASE_URL kann per Env-Var überschrieben werden. NIEMALS gegen die echte
Produktions-DB oder ein echtes Live-Konto ausführen.
"""
import os
import sys
import traceback
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://peruser_test_tmp:peruser_test_tmp_pw@localhost:5432/alpaca_peruser_test",
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

OTHER_USER_A = 9001
OTHER_USER_B = 9002

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
        session.query(database.UserBotConfig).delete()
        session.query(database.CapitalAllocation).filter(
            database.CapitalAllocation.user_id != DEFAULT_USER_ID
        ).delete()
        session.commit()


def make_open_trade(ticker, user_id, entry_price=100.0, quantity=1.0):
    with database.get_session() as session:
        trade = database.Trade(
            ticker=ticker, direction="LONG", instrument_type="STOCK",
            entry_price=entry_price, stop_loss=entry_price * 0.95, take_profit=entry_price * 1.1,
            quantity=quantity, capital_used=entry_price * quantity, rule_score=80,
            status="OPEN", mode="PAPER", broker="alpaca", user_id=user_id,
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        return trade.id


def close(trade_id, exit_price):
    with database.get_session() as session:
        trade = session.query(database.Trade).filter_by(id=trade_id).first()
        database.close_trade(session, trade, exit_price, "CLOSED_MANUAL")
        session.commit()


def make_signal(ticker="TEST"):
    return SignalResult(
        ticker=ticker, score=80, direction="LONG", instrument_type="STOCK",
        approved=True, current_price=100.0, stop_loss=95.0, take_profit=110.0,
    )


def guardrails_pass_cleanly(user_id, ticker="TEST"):
    signal = make_signal(ticker)
    with patch.object(broker, "get_alpaca_account_snapshot", return_value=None):
        broker.check_guardrails(signal, user_id=user_id)


# ─────────────────────────────────────────────
# (a) Verlustserie-Cooldown-Schwelle jetzt pro Nutzer statt Daniels global
# ─────────────────────────────────────────────
def test_a_loss_streak_threshold_is_per_user():
    wipe_all_state()

    # Kunde konfiguriert eine SEHR sensible eigene Schwelle (1 statt Daniels
    # Default 3) - muss bereits nach EINEM Verlust-Trade in Cooldown gehen,
    # nicht erst nach Daniels globalen 3 (der historische Bug).
    with database.get_session() as session:
        database.set_user_bot_config(session, OTHER_USER_A, "MAX_CONSECUTIVE_LOSSES", "1")
        session.commit()

    trade_id = make_open_trade("SOLO_LOSS", OTHER_USER_A)
    close(trade_id, 99.0)

    with database.get_session() as session:
        state = database.get_loss_streak_state(session, OTHER_USER_A)
    ok = state["consecutive_losses"] == 1 and state["cooldown_active"]
    record(
        "a) Kunde mit eigenem MAX_CONSECUTIVE_LOSSES=1 pausiert bereits nach 1 Verlust",
        ok, f"consecutive_losses={state['consecutive_losses']} cooldown_active={state['cooldown_active']}"
    )

    try:
        guardrails_pass_cleanly(OTHER_USER_A, "SOLO_LOSS_RETRY")
        record("a) check_guardrails() blockiert diesen Kunden während seines eigenen Cooldowns", False, "keine Exception!")
    except broker.GuardrailViolation as gv:
        record("a) check_guardrails() blockiert diesen Kunden während seines eigenen Cooldowns", True, str(gv))

    # Daniel bleibt mit demselben EINEN Verlust unbeeinträchtigt (sein
    # globaler Default ist weiterhin 3, kein Bleed-Over zwischen Nutzern).
    trade_id = make_open_trade("DANLOSS", DEFAULT_USER_ID)
    close(trade_id, 99.0)
    with database.get_session() as session:
        daniel_state = database.get_loss_streak_state(session, DEFAULT_USER_ID)
    ok = daniel_state["consecutive_losses"] == 1 and not daniel_state["cooldown_active"]
    record(
        "a) Daniel (globaler Default MAX_CONSECUTIVE_LOSSES=3) bleibt nach 1 Verlust unpausiert",
        ok, f"consecutive_losses={daniel_state['consecutive_losses']} cooldown_active={daniel_state['cooldown_active']}"
    )
    try:
        guardrails_pass_cleanly(DEFAULT_USER_ID, "DANIEL_RETRY")
        record("a) check_guardrails() lässt Daniel weiterhin durch (kein Cross-Tenant-Bleed)", True)
    except broker.GuardrailViolation as gv:
        record("a) check_guardrails() lässt Daniel weiterhin durch (kein Cross-Tenant-Bleed)", False, str(gv))


# ─────────────────────────────────────────────
# (b) get_user_live_config: alle 10 neuen Klasse-A-Keys pro Nutzer isoliert
# ─────────────────────────────────────────────
def test_b_new_class_a_keys_isolated_between_users():
    wipe_all_state()

    new_keys_values = {
        "TRAILING_ACTIVATION_PCT": "0.12",
        "MAX_CONSECUTIVE_LOSSES": "7",
        "COOLDOWN_HOURS_AFTER_LOSS_STREAK": "9.5",
        "MAX_HOLDING_DAYS": "11",
        "MAX_HOLDING_DAYS_TRAILING_MULTIPLIER": "4",
        "TIME_EXIT_GRACE_DAYS": "2",
        "VOLATILE_SEGMENT_PCT": "0.77",
        "ATR_MULTIPLIER_SL": "2.5",
        "ATR_MIN_SL_PCT": "0.02",
        "ATR_MAX_SL_PCT": "0.15",
    }
    with database.get_session() as session:
        for key, value in new_keys_values.items():
            database.set_user_bot_config(session, OTHER_USER_A, key, value)
        session.commit()

    cfg_a = database.get_user_live_config(OTHER_USER_A)
    cfg_b = database.get_user_live_config(OTHER_USER_B)  # nie gesetzt -> DEFAULT_USER_CONFIG-Defaults
    cfg_daniel = database.get_user_live_config(DEFAULT_USER_ID)  # globale bot_config, unberührt

    all_ok = True
    for key, raw_value in new_keys_values.items():
        cast, default_value = database.DEFAULT_USER_CONFIG[key]
        expected_a = cast(raw_value)
        ok = (
            cfg_a[key] == expected_a
            and cfg_b[key] == default_value
            and cfg_a[key] != cfg_b[key]
        )
        all_ok = all_ok and ok
        record(
            f"b) {key}: Nutzer A={cfg_a[key]!r} (eigener Wert) != Nutzer B={cfg_b[key]!r} (Default)",
            ok,
        )
    record("b) Daniels globale bot_config unverändert erreichbar (keine KeyErrors)",
           all(k in cfg_daniel for k in new_keys_values))


# ─────────────────────────────────────────────
# (c) Kapitalaufteilung pro Nutzer isoliert + wirkt in get_effective_max_capital_total_bot
# ─────────────────────────────────────────────
def test_c_capital_allocations_per_user():
    wipe_all_state()

    # Daniels eigene Zeile zuerst seeden (in einer frischen Test-DB noch
    # nicht vorhanden) - Regressionsschutz: darf durch die Nutzer-A/B-
    # Schreibvorgänge unten NICHT verändert werden (kein Cross-Tenant-Bleed).
    with patch.object(broker, "get_bot_config", return_value="475.00"):
        broker.get_or_seed_capital_allocations(DEFAULT_USER_ID, 475.0)
    with database.get_session() as session:
        alloc_daniel_before = database.get_capital_allocations(session, DEFAULT_USER_ID)

    with database.get_session() as session:
        database.set_capital_allocations(session, OTHER_USER_A, {"bot": 40.0, "active_trading": 60.0})
        database.set_capital_allocations(session, OTHER_USER_B, {"bot": 90.0, "active_trading": 10.0})

    with database.get_session() as session:
        alloc_a = database.get_capital_allocations(session, OTHER_USER_A)
        alloc_b = database.get_capital_allocations(session, OTHER_USER_B)
        alloc_daniel_after = database.get_capital_allocations(session, DEFAULT_USER_ID)

    ok = alloc_a["bot"] == 40.0 and alloc_b["bot"] == 90.0 and alloc_a["bot"] != alloc_b["bot"]
    record("c) capital_allocations: Nutzer A (40% bot) und B (90% bot) unabhängig gespeichert", ok,
           f"A={alloc_a} B={alloc_b}")
    record("c) Daniels Zeile durch Nutzer-A/B-Schreibvorgänge unberührt (kein Cross-Tenant-Bleed)",
           alloc_daniel_after == alloc_daniel_before, f"vorher={alloc_daniel_before} nachher={alloc_daniel_after}")

    # get_effective_max_capital_total_bot: für Nutzer A mit verbundenem
    # Broker (equity=1000) muss jetzt 40% davon gelten (400.0) - VORHER
    # (Owner-only-Ära) hätte das unverändert den Flat-Wert aus
    # UserBotConfig.MAX_CAPITAL_TOTAL zurückgegeben, egal was hier steht.
    with patch.object(broker, "get_alpaca_account_snapshot", return_value={
        "cash": 1000.0, "buying_power": 1000.0, "equity": 1000.0,
        "long_market_value": 0.0, "unrealized_pl": 0.0,
    }):
        effective_a = broker.get_effective_max_capital_total_bot(OTHER_USER_A)
    ok = effective_a == 400.0
    record("c) get_effective_max_capital_total_bot(A) = equity(1000) x 40% = 400.0 (nicht mehr Flat-Wert)",
           ok, f"effective={effective_a}")

    # Ohne verbundenen Broker (real_snapshot=None): Fail-safe-Fallback bleibt
    # der Flat-Wert aus UserBotConfig.MAX_CAPITAL_TOTAL (Status quo erhalten).
    with database.get_session() as session:
        database.set_user_bot_config(session, OTHER_USER_A, "MAX_CAPITAL_TOTAL", "55.0")
        session.commit()
    with patch.object(broker, "get_alpaca_account_snapshot", return_value=None):
        effective_a_offline = broker.get_effective_max_capital_total_bot(OTHER_USER_A)
    ok = effective_a_offline == 55.0
    record("c) get_effective_max_capital_total_bot(A) ohne Broker-Snapshot faellt auf Flat-Wert 55.0 zurueck",
           ok, f"effective={effective_a_offline}")


def main():
    for fn in (test_a_loss_streak_threshold_is_per_user,
               test_b_new_class_a_keys_isolated_between_users,
               test_c_capital_allocations_per_user):
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
