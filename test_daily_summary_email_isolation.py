"""
test_daily_summary_email_isolation.py – PFLICHT-SICHERHEITSTEST (Tages-Mail-
Datenleck-Fix, 2026-08-12).

Vorfall (gefunden bei der Multi-Tenant-Readiness-Diagnose vor Aufnahme eines
dritten externen Nutzers): _send_daily_summary_email_for_user() bekam
no_trade_reasons bisher als EINEN global in send_daily_summary_email()
berechneten Parameter übergeben – der guardrail_reason des bestbewerteten
Kandidaten je Slot, ÜBER ALLE verbundenen Nutzer hinweg, unabhängig davon
wessen Guardrail-Auswertung das war. JEDER Nutzer bekam denselben Text unter
"Kein eigener Trade – ..." in seiner eigenen Mail. Exakt dieselbe Bug-Klasse
wie der bereits gefixte scan_log-Leak vom 2026-08-11 (siehe a6e32b9/e7dcd3a,
test_per_user_api_security.py::test_scan_log_isolation), nur ein anderer
Code-Pfad (Mail-Versand statt API-Endpoint), der beim damaligen Fix übersehen
wurde.

Fix: no_trade_reasons wird jetzt INNERHALB von
_send_daily_summary_email_for_user() lokal, mit `WHERE user_id = :uid` auf
scan_log, berechnet - kein geteilter Parameter mehr.

Zwei unabhängige Test-Accounts (9201/9202, frei erfundene IDs, keine
Beziehung zu echten pos_users-Zeilen) - send_email wird gemockt (kein realer
Versand), get_user_email wird gemockt (pos_users existiert nicht in der
Wegwerf-Test-DB, siehe test_per_user_api_security.py-Konvention).

KEINE Produktions-DB (identische Wegwerf-DB-Konvention wie test_per_user_
api_security.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER dailymail_test_tmp WITH PASSWORD 'dailymail_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_dailymail_test OWNER dailymail_test_tmp;"
    python3 test_daily_summary_email_isolation.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_dailymail_test;"
    sudo -u postgres psql -c "DROP USER dailymail_test_tmp;"

NIEMALS gegen echte Produktions-DB/Konto ausführen.
"""
import os
import sys
import traceback
from datetime import date, datetime, timedelta
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://dailymail_test_tmp:dailymail_test_tmp_pw@localhost:5432/alpaca_dailymail_test",
)
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-production-daily-mail")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
database.init_db()

import main  # noqa: E402

USER_A = 9201
USER_B = 9202
TODAY = date(2026, 8, 11)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_scan_log_test_users():
    with database.get_session() as session:
        session.query(database.ScanLog).filter(
            database.ScanLog.user_id.in_([USER_A, USER_B])
        ).delete(synchronize_session=False)
        session.query(database.Trade).filter(
            database.Trade.user_id.in_([USER_A, USER_B])
        ).delete(synchronize_session=False)
        session.commit()


def seed_scan_log_row(user_id: int, ticker: str, score: int, guardrail_reason, trade_executed: bool = False):
    with database.get_session() as session:
        session.add(database.ScanLog(
            scan_time=datetime(2026, 8, 11, 19, 0, 0),
            slot_et="15:00", ticker=ticker, score=score, approved=True,
            guardrail_reason=guardrail_reason, trade_executed=trade_executed,
            broker="alpaca", mode="LIVE", user_id=user_id,
        ))
        session.commit()


def test_no_trade_reasons_isolated_per_user():
    """
    Reproduziert den gemeldeten Fall: Account A hat an diesem Slot den
    höchsten Score UND ein Konto-spezifisches Guardrail-Ereignis
    ("Max. offene Position erreicht 5/5"). Account B ist ein komplett
    unabhängiger Account mit einem eigenen, anderen Guardrail-Grund
    ("Tagesverlustlimit erreicht (B's eigenes Limit)") und niedrigerem Score.

    VOR dem Fix: die globale "ORDER BY score DESC LIMIT 1"-Query hätte für
    diesen Slot IMMER As Text gewählt (höherer Score) - und genau dieser
    Text wäre in BEIDER Nutzer Mails gelandet, weil no_trade_reasons ein
    einziges geteiltes Dict war. B hätte fälschlich As Grund als "seinen
    eigenen" gesehen.
    """
    wipe_scan_log_test_users()
    seed_scan_log_row(USER_A, "AAPL", score=85, guardrail_reason="Max. offene Position erreicht 5/5")
    seed_scan_log_row(USER_B, "MSFT", score=70, guardrail_reason="Tagesverlustlimit erreicht (B's eigenes Limit)")

    with database.get_session() as session:
        from sqlalchemy import text
        slots_heute = session.execute(text("""
            SELECT slot_et,
                   COUNT(*) as gescannt,
                   SUM(CASE WHEN score >= 65 THEN 1 ELSE 0 END) as ueber_65,
                   SUM(CASE WHEN trade_executed THEN 1 ELSE 0 END) as trades,
                   ROUND(AVG(score)::numeric, 1) as avg_score
            FROM scan_log
            WHERE DATE(scan_time AT TIME ZONE 'America/New_York') = :today
              AND score > 0
            GROUP BY slot_et
            ORDER BY slot_et
        """), {"today": TODAY}).fetchall()

    sent = {}

    def fake_send_email(subject, body, to=None):
        sent[to] = body

    def fake_get_user_email(uid):
        return {USER_A: "a@example.com", USER_B: "b@example.com"}.get(uid)

    with patch.object(main, "send_email", side_effect=fake_send_email), \
         patch.object(main, "get_user_email", side_effect=fake_get_user_email), \
         patch.object(main, "get_portfolio_value", return_value=1000.0), \
         patch.object(main, "get_bot_performance", return_value=None), \
         patch.object(main, "get_pause_status", return_value={"paused": False, "reasons": []}):
        main._send_daily_summary_email_for_user(USER_A, TODAY, slots_heute)
        main._send_daily_summary_email_for_user(USER_B, TODAY, slots_heute)

    record("a) Beide Mails wurden 'versendet' (gemockt)",
           "a@example.com" in sent and "b@example.com" in sent,
           f"empfänger={list(sent.keys())}")

    body_a = sent.get("a@example.com", "")
    body_b = sent.get("b@example.com", "")

    record("b) Account A sieht seinen EIGENEN Grund ('Max. offene Position erreicht 5/5')",
           "Max. offene Position erreicht 5/5" in body_a, body_a)
    record("c) Account B sieht NICHT As Text (der gemeldete Vorfall) - stattdessen seinen EIGENEN Grund",
           "Max. offene Position erreicht 5/5" not in body_b
           and "Tagesverlustlimit erreicht (B's eigenes Limit)" in body_b,
           body_b)
    record("d) Account A sieht NICHT Bs Text",
           "Tagesverlustlimit erreicht (B's eigenes Limit)" not in body_a, body_a)


def test_no_qualifying_candidate_shows_generic_reason_not_crash():
    """Nutzer ohne eigene score>=65-Zeile an diesem Tag (z.B. Dana in der
    Live-Diagnose, die noch keinen Alpaca-Scan hatte) darf keinen fremden
    Text sehen und nicht abstürzen - fällt auf den generischen Fallback-Text
    zurück."""
    wipe_scan_log_test_users()
    seed_scan_log_row(USER_A, "AAPL", score=85, guardrail_reason="Max. offene Position erreicht 5/5")
    # USER_B seedet bewusst NICHTS

    with database.get_session() as session:
        from sqlalchemy import text
        slots_heute = session.execute(text("""
            SELECT slot_et,
                   COUNT(*) as gescannt,
                   SUM(CASE WHEN score >= 65 THEN 1 ELSE 0 END) as ueber_65,
                   SUM(CASE WHEN trade_executed THEN 1 ELSE 0 END) as trades,
                   ROUND(AVG(score)::numeric, 1) as avg_score
            FROM scan_log
            WHERE DATE(scan_time AT TIME ZONE 'America/New_York') = :today
              AND score > 0
            GROUP BY slot_et
            ORDER BY slot_et
        """), {"today": TODAY}).fetchall()

    sent = {}

    def fake_send_email(subject, body, to=None):
        sent[to] = body

    with patch.object(main, "send_email", side_effect=fake_send_email), \
         patch.object(main, "get_user_email", side_effect=lambda uid: "b@example.com"), \
         patch.object(main, "get_portfolio_value", return_value=1000.0), \
         patch.object(main, "get_bot_performance", return_value=None), \
         patch.object(main, "get_pause_status", return_value={"paused": False, "reasons": []}):
        main._send_daily_summary_email_for_user(USER_B, TODAY, slots_heute)

    body_b = sent.get("b@example.com", "")
    record("e) Nutzer ohne eigene Kandidaten sieht NICHT As Text",
           "Max. offene Position erreicht 5/5" not in body_b, body_b)
    record("f) ... sondern den generischen Fallback",
           "Kein Trade ausgeführt (Grund nicht ermittelbar)" in body_b, body_b)


def main_test_runner():
    for fn in (test_no_trade_reasons_isolated_per_user, test_no_qualifying_candidate_shows_generic_reason_not_crash):
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
    main_test_runner()
