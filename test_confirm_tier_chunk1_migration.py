"""
test_confirm_tier_chunk1_migration.py – Migrations-/Settings-Test für den
Confirm-Tier Chunk 1 (Datenmodell + Settings, 2026-08-07):

  1. pending_confirmations-Tabelle wird von init_db() korrekt angelegt
     (Spalten/Typen), ohne bestehende Tabellen/Daten anzufassen.
  2. EXECUTION_MODE/PRICE_TOLERANCE_PCT werden mit Default 'auto'/0.02
     geseedet – auch auf einer DB, die bereits vor diesem Chunk existierte
     (bestehende Keys/Werte bleiben dabei unverändert – reiner Insert-if-
     missing, kein Overwrite).
  3. get_live_config() (Daniel/DEFAULT_USER_ID) und get_user_live_config()
     (Multi-Tenant-Nutzer) liefern die neuen Keys korrekt typisiert.

KEINE Produktions-DB, KEIN Live-Broker-Zugriff: DATABASE_URL zeigt auf eine
eigene Wegwerf-Postgres-DB (analog test_capital_guard_rounding.py):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER confirm_tmp WITH PASSWORD 'confirm_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_confirm_test OWNER confirm_tmp;"
    python3 test_confirm_tier_chunk1_migration.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_confirm_test;"
    sudo -u postgres psql -c "DROP USER confirm_tmp;"
"""
import os
import sys
import traceback

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://confirm_tmp:confirm_tmp_pw@localhost:5432/alpaca_confirm_test",
)
for var in ("ANTHROPIC_API_KEY",):
    os.environ.setdefault(var, "")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
from sqlalchemy import text, inspect  # noqa: E402

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def test_pending_confirmations_table_created():
    database.init_db()
    insp = inspect(database.engine)
    record("pending_confirmations-Tabelle existiert nach init_db()",
           insp.has_table("pending_confirmations"))

    cols = {c["name"]: c for c in insp.get_columns("pending_confirmations")}
    expected = [
        "id", "user_id", "broker", "ticker", "qty_or_amount", "signal_price",
        "signal_timestamp", "status", "confirmation_token", "expires_at",
        "price_tolerance_pct_snapshot", "created_at", "resolved_at",
    ]
    missing = [c for c in expected if c not in cols]
    record("alle geforderten Spalten vorhanden", not missing, f"missing={missing}")

    not_null_expected = {"user_id", "broker", "ticker", "qty_or_amount", "signal_price",
                          "signal_timestamp", "status", "confirmation_token", "expires_at",
                          "price_tolerance_pct_snapshot"}
    nullable_violations = [c for c in not_null_expected if cols.get(c, {}).get("nullable", True)]
    record("Pflichtfelder sind NOT NULL", not nullable_violations, f"violations={nullable_violations}")

    with database.engine.begin() as conn:
        uniq = conn.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE tablename='pending_confirmations' "
            "AND indexdef LIKE '%UNIQUE%confirmation_token%'"
        )).fetchall()
    record("confirmation_token ist UNIQUE indiziert", len(uniq) >= 1, f"uniq={uniq}")


def test_existing_db_upgrade_seeds_new_keys_without_overwriting_old():
    # Simuliert eine VOR diesem Chunk existierende, bereits laufende DB:
    # bestehender Key wird auf einen vom Default abweichenden, "echten"
    # Nutzerwert gesetzt; neue Confirm-Tier-Keys werden explizit entfernt.
    with database.get_session() as session:
        row = session.query(database.BotConfig).filter_by(key="MAX_CAPITAL_TOTAL").first()
        row.value = "999.00"
        session.query(database.BotConfig).filter_by(key="EXECUTION_MODE").delete()
        session.query(database.BotConfig).filter_by(key="PRICE_TOLERANCE_PCT").delete()
        session.commit()

    database.init_db()  # "Neustart nach Deploy"

    with database.get_session() as session:
        max_cap = session.query(database.BotConfig).filter_by(key="MAX_CAPITAL_TOTAL").first()
        exec_mode = session.query(database.BotConfig).filter_by(key="EXECUTION_MODE").first()
        tol = session.query(database.BotConfig).filter_by(key="PRICE_TOLERANCE_PCT").first()

    record("bestehender Wert (MAX_CAPITAL_TOTAL=999.00) bleibt beim Re-Seed unverändert",
           max_cap is not None and max_cap.value == "999.00", f"value={max_cap.value if max_cap else None}")
    record("EXECUTION_MODE wird beim Upgrade nachträglich mit Default 'auto' geseedet",
           exec_mode is not None and exec_mode.value == "auto", f"value={exec_mode.value if exec_mode else None}")
    record("PRICE_TOLERANCE_PCT wird beim Upgrade nachträglich mit Default 0.02 geseedet",
           tol is not None and tol.value == "0.02", f"value={tol.value if tol else None}")

    # Aufräumen für den nächsten Testfall (Daniels echten Wert zurücksetzen).
    with database.get_session() as session:
        session.query(database.BotConfig).filter_by(key="MAX_CAPITAL_TOTAL").first().value = "475.00"
        session.commit()


def test_get_live_config_returns_new_keys_typed():
    from config import get_live_config
    cfg = get_live_config()
    record("get_live_config()['EXECUTION_MODE'] == 'auto' (str)",
           cfg.get("EXECUTION_MODE") == "auto", f"value={cfg.get('EXECUTION_MODE')!r}")
    record("get_live_config()['PRICE_TOLERANCE_PCT'] == 0.02 (float)",
           cfg.get("PRICE_TOLERANCE_PCT") == 0.02 and isinstance(cfg.get("PRICE_TOLERANCE_PCT"), float),
           f"value={cfg.get('PRICE_TOLERANCE_PCT')!r}")


def test_get_user_live_config_lazy_seeds_new_user():
    other_user_id = 4242
    with database.get_session() as session:
        session.query(database.UserBotConfig).filter_by(user_id=other_user_id).delete()
        session.commit()

    cfg = database.get_user_live_config(other_user_id)
    record("neuer Multi-Tenant-Nutzer bekommt EXECUTION_MODE='auto'",
           cfg.get("EXECUTION_MODE") == "auto", f"value={cfg.get('EXECUTION_MODE')!r}")
    record("neuer Multi-Tenant-Nutzer bekommt PRICE_TOLERANCE_PCT=0.02",
           cfg.get("PRICE_TOLERANCE_PCT") == 0.02, f"value={cfg.get('PRICE_TOLERANCE_PCT')!r}")

    with database.get_session() as session:
        rows = {r.key: r.value for r in
                session.query(database.UserBotConfig).filter_by(user_id=other_user_id).all()}
    record("EXECUTION_MODE wurde für den neuen Nutzer tatsächlich persistiert (nicht nur im Rückgabewert)",
           rows.get("EXECUTION_MODE") == "auto", f"rows={rows}")


def main():
    for fn in (test_pending_confirmations_table_created,
               test_existing_db_upgrade_seeds_new_keys_without_overwriting_old,
               test_get_live_config_returns_new_keys_typed,
               test_get_user_live_config_lazy_seeds_new_user):
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
