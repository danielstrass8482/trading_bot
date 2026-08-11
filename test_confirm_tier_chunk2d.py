"""
test_confirm_tier_chunk2d.py – Tests für Confirm-Tier Chunk 2d (2026-08-11):
dynamisches, ereignisgesteuertes Bestätigungsfenster statt starrem 15-
Minuten-Timeout (siehe confirm_execution.py-Moduldoc, SCOPE Chunk 2d).
NUR Alpaca (trading_bot) - Saxo ist bewusst ein Paritäts-Folgeauftrag.

KEIN Live-Test gegen Alpaca: der Confirm-Pfad platziert nie direkt eine
Order (main._execute_or_queue_entry endet für EXECUTION_MODE='confirm'
immer mit einem PENDING-Eintrag statt einem Order-Call), erst eine
tatsächliche Bestätigung würde place_trade() erreichen - dafür wird
trading_api.place_trade in test_d gemockt. notifications.send_email wird
in JEDEM Testfall gemockt, damit KEINE echte Mail verschickt wird. KEINE
Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB:

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER confirm2d_tmp WITH PASSWORD 'confirm2d_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_confirm2d_test OWNER confirm2d_tmp;"
    python3 test_confirm_tier_chunk2d.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_confirm2d_test;"
    sudo -u postgres psql -c "DROP USER confirm2d_tmp;"

Deckt die 5 Testszenarien aus der Aufgabe (Punkt 8, a-e) sowie den Pflicht-
Cross-Account-Sicherheitstest (Punkt 9) ab. NIEMALS gegen echte Produktions-
DB/echtes Alpaca-Konto ausführen.
"""
import os
import sys
import json
import traceback
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://confirm2d_tmp:confirm2d_tmp_pw@localhost:5432/alpaca_confirm2d_test",
)
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-only-for-this-test-run")
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
database.init_db()

import confirm_execution  # noqa: E402
import main as main_module  # noqa: E402
import trading_api  # noqa: E402
from rule_engine import SignalResult  # noqa: E402
from database import get_session, set_user_bot_config, PendingConfirmation  # noqa: E402

USER_A = 9201
USER_B = 9202

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_state():
    with get_session() as session:
        session.query(PendingConfirmation).filter(
            PendingConfirmation.user_id.in_([USER_A, USER_B])
        ).delete(synchronize_session=False)
        session.commit()


def set_confirm_mode(user_id: int):
    with get_session() as session:
        set_user_bot_config(session, user_id, "EXECUTION_MODE", "confirm")
        session.commit()


def make_signal(ticker="AAPL", score=75, price=150.0, approved=True):
    return SignalResult(
        ticker=ticker, score=score, direction="LONG", instrument_type="STOCK",
        approved=approved, current_price=price, stop_loss=round(price * 0.95, 2), take_profit=round(price * 1.1, 2),
    )


def get_pending(user_id, ticker):
    with get_session() as session:
        row = session.query(PendingConfirmation).filter_by(user_id=user_id, ticker=ticker).first()
        if row:
            session.expunge(row)
        return row


def push_expiry_into_future(user_id, ticker):
    """
    Testfeedback-Session (2026-08-11) - Bugfix in der Test-Infrastruktur
    selbst: create_pending_confirmation()/update_pending_confirmation()
    berechnen expires_at über compute_market_close_expiry(datetime.utcnow())
    - ein Testlauf NACH 16:00 ET (z.B. abends in Europa) erzeugt dadurch
    einen bereits abgelaufenen Eintrag, _resolve_confirmation() bricht dann
    sofort mit "Bestätigung abgelaufen" ab, BEVOR der eigentlich zu testende
    Preis-Re-Check-Pfad überhaupt erreicht wird (live beobachtet: Tests a-c
    liefen noch vor Handelsschluss durch, d schlug nach Handelsschluss fehl -
    keine Änderung an der eigentlich getesteten Logik, reine Testfragilität).
    Testfälle, die NACH create_pending_confirmation() aktiv _resolve_
    confirmation() aufrufen, müssen expires_at deshalb explizit in die
    Zukunft setzen, unabhängig von der tatsächlichen Tageszeit beim Testlauf.
    """
    with get_session() as session:
        row = session.query(PendingConfirmation).filter_by(user_id=user_id, ticker=ticker).first()
        row.expires_at = datetime.utcnow() + timedelta(hours=2)
        session.commit()


# ─────────────────────────────────────────────
# a) Ticker bleibt über mehrere Zyklen über der Schwelle -> Update, kein
#    Duplikat, keine Mehrfach-Mail
# ─────────────────────────────────────────────
def test_a_multiple_cycles_update_no_duplicate_no_extra_mail():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email") as email_mock, \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(score=70, price=100.0), {}, USER_A)
        main_module._execute_or_queue_entry(make_signal(score=75, price=101.5), {}, USER_A)
        main_module._execute_or_queue_entry(make_signal(score=80, price=103.0), {}, USER_A)

    with get_session() as session:
        rows = session.query(PendingConfirmation).filter_by(user_id=USER_A, ticker="AAPL", status="pending").all()
    record("a) genau EIN PENDING-Eintrag nach 3 Zyklen (kein Duplikat)", len(rows) == 1, f"count={len(rows)}")

    if rows:
        row = rows[0]
        record("a) signal_price wurde auf den LETZTEN Zyklus aktualisiert (103.0, nicht 100.0)",
               row.signal_price == 103.0, f"got={row.signal_price}")
        score = json.loads(row.signal_payload).get("score")
        record("a) Score im Payload wurde auf den LETZTEN Zyklus aktualisiert (80, nicht 70)",
               score == 80, f"got={score}")

    record("a) GENAU EINE Bestätigungs-Mail verschickt (nicht 3x)", email_mock.call_count == 1,
           f"calls={email_mock.call_count}")


# ─────────────────────────────────────────────
# b) Ticker fällt unter die Schwelle -> sofort proaktiv EXPIRED
# ─────────────────────────────────────────────
def test_b_proactive_expire_when_dropped_below_threshold():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="MSFT", score=70, price=300.0), {}, USER_A)

    row = get_pending(USER_A, "MSFT")
    record("b) PENDING-Eintrag angelegt (Testvoraussetzung)",
           row is not None and row.status == confirm_execution.STATUS_PENDING)

    # Re-Scan: MSFT liegt jetzt nicht mehr über der Schwelle (leeres
    # still_qualifying_tickers-Set simuliert "kein einziger Ticker qualifiziert
    # sich mehr", MSFT eingeschlossen).
    expired = confirm_execution.expire_dropped_below_threshold(USER_A, still_qualifying_tickers=set())
    record("b) MSFT wird von expire_dropped_below_threshold() als abgelaufen gemeldet",
           "MSFT" in expired, f"expired={expired}")

    row = get_pending(USER_A, "MSFT")
    record("b) Status ist SOFORT EXPIRED (nicht erst beim Timeout)",
           row.status == confirm_execution.STATUS_EXPIRED, f"status={row.status}")
    record("b) resolved_at wurde gesetzt", row.resolved_at is not None)

    # Negativ-Kontrolle: ein Ticker, der WEITERHIN qualifiziert, bleibt PENDING.
    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="GOOG", score=70, price=140.0), {}, USER_A)
    confirm_execution.expire_dropped_below_threshold(USER_A, still_qualifying_tickers={"GOOG"})
    row = get_pending(USER_A, "GOOG")
    record("b) Negativ-Kontrolle: GOOG bleibt PENDING (weiterhin über der Schwelle)",
           row.status == confirm_execution.STATUS_PENDING, f"status={row.status}")


# ─────────────────────────────────────────────
# c) Handelsschluss erreicht -> EXPIRED via expire_overdue(); zusätzlich
#    compute_market_close_expiry() direkt geprüft (16:00 ET, gleicher Tag)
# ─────────────────────────────────────────────
def test_c_market_close_expires_via_expire_overdue():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="TSLA", score=70, price=200.0), {}, USER_A)

    # Handelsschluss simulieren, statt real bis 16:00 ET zu warten: expires_at
    # manuell in die Vergangenheit setzen (identisch zu dem, was
    # compute_market_close_expiry() für einen bereits vergangenen Handelstag
    # tatsächlich berechnet hätte).
    with get_session() as session:
        row = session.query(PendingConfirmation).filter_by(user_id=USER_A, ticker="TSLA").first()
        row.expires_at = datetime.utcnow() - timedelta(minutes=1)
        session.commit()

    count = confirm_execution.expire_overdue()
    record("c) expire_overdue() erfasst mindestens die abgelaufene Zeile", count >= 1, f"count={count}")

    row = get_pending(USER_A, "TSLA")
    record("c) Status ist EXPIRED nach (simuliertem) Handelsschluss",
           row.status == confirm_execution.STATUS_EXPIRED, f"status={row.status}")


def test_c2_compute_market_close_expiry_targets_16_00_et_same_day():
    import pytz
    et_tz = pytz.timezone("America/New_York")
    # 14:00 UTC an einem August-Tag = 10:00 ET (EDT, UTC-4, Sommerzeit aktiv).
    now_utc = datetime(2026, 8, 11, 14, 0, 0)
    expiry = confirm_execution.compute_market_close_expiry(now_utc)
    expiry_et = pytz.utc.localize(expiry).astimezone(et_tz)
    now_et = pytz.utc.localize(now_utc).astimezone(et_tz)
    record("c2) compute_market_close_expiry() liefert exakt 16:00 ET",
           (expiry_et.hour, expiry_et.minute) == (16, 0), f"got={expiry_et}")
    record("c2) derselbe Kalendertag wie der Eingabezeitpunkt (in ET)",
           expiry_et.date() == now_et.date(), f"expiry_date={expiry_et.date()} now_date={now_et.date()}")


# ─────────────────────────────────────────────
# d) Nutzer bestätigt einen über Stunden aktualisierten Eintrag -> Preis-
#    Re-Check vergleicht gegen die ZULETZT aktualisierte Basis
# ─────────────────────────────────────────────
def test_d_price_recheck_against_updated_basis_not_original():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        # Zyklus 1 (ursprüngliches Signal): 100.0
        main_module._execute_or_queue_entry(make_signal(ticker="NVDA", score=70, price=100.0), {}, USER_A)
        # Zyklus 2 (Stunden später simuliert, Kandidat weiterhin über der
        # Schwelle): Preis auf 110.0 aktualisiert - GENAU das Szenario aus
        # der Aufgabe ("über Stunden aktualisierter Eintrag").
        main_module._execute_or_queue_entry(make_signal(ticker="NVDA", score=72, price=110.0), {}, USER_A)

    push_expiry_into_future(USER_A, "NVDA")
    row = get_pending(USER_A, "NVDA")
    record("d) signal_price steht auf der aktualisierten Basis (110.0), NICHT dem Ursprungspreis (100.0)",
           row.signal_price == 110.0, f"got={row.signal_price}")

    # Live-Preis nah an der AKTUALISIERTEN Basis (110): 0.9% Abweichung,
    # innerhalb der Standard-Toleranz (2%) - dürfte OHNE Re-Bestätigungs-
    # Schritt direkt durchgehen. Gegen den URSPRÜNGLICHEN Preis (100) wäre
    # das eine 10%-Abweichung gewesen (weit über der Toleranz) - würde der
    # Re-Check fälschlich noch gegen 100 vergleichen, müsste hier
    # needs_reconfirmation=True zurückkommen. Tut es nicht -> Beweis, dass
    # tatsächlich gegen die aktualisierte Basis verglichen wird.
    live_price_near_updated_basis = 111.0
    with patch.object(trading_api, "_fetch_live_price", return_value=live_price_near_updated_basis), \
         patch.object(trading_api, "place_trade") as place_trade_mock:
        fake_trade = type("FakeTrade", (), {"id": 999})()
        place_trade_mock.return_value = fake_trade
        result = trading_api._resolve_confirmation(row, "confirm")

    record("d) Bestätigung geht OHNE Re-Bestätigungs-Schritt durch (Vergleich gegen aktualisierte Basis, nicht Ursprungspreis)",
           result["ok"] is True and not result.get("needs_reconfirmation"), str(result))
    record("d) place_trade() wurde mit dem aktuellen Live-Preis aufgerufen (111.0)",
           place_trade_mock.call_args is not None
           and abs(place_trade_mock.call_args[0][0].current_price - live_price_near_updated_basis) < 0.001,
           str(place_trade_mock.call_args))

    # Negativ-Kontrolle (derselbe Mechanismus, umgekehrt): frisches Signal,
    # Live-Preis weicht stark vom AKTUELLEN signal_price ab -> MUSS
    # needs_reconfirmation auslösen (Sicherheitsschicht bleibt intakt).
    wipe_state()
    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="AMD", score=70, price=50.0), {}, USER_A)
    push_expiry_into_future(USER_A, "AMD")
    row2 = get_pending(USER_A, "AMD")
    with patch.object(trading_api, "_fetch_live_price", return_value=60.0), \
         patch.object(trading_api, "place_trade") as place_trade_mock2:
        result2 = trading_api._resolve_confirmation(row2, "confirm")
    record("d) Negativ-Kontrolle: starke Preisabweichung löst weiterhin needs_reconfirmation aus (Sicherheitsschicht intakt)",
           result2.get("needs_reconfirmation") is True and place_trade_mock2.call_count == 0, str(result2))


# ─────────────────────────────────────────────
# e) Sortierung: mehrere Pending-Einträge, unterschiedliche Scores
# ─────────────────────────────────────────────
def test_e_sorting_by_score_descending():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="AAA", score=60, price=10.0), {}, USER_A)
        main_module._execute_or_queue_entry(make_signal(ticker="BBB", score=90, price=20.0), {}, USER_A)
        main_module._execute_or_queue_entry(make_signal(ticker="CCC", score=75, price=30.0), {}, USER_A)

    result = trading_api.list_pending_confirmations(user_id=USER_A)
    tickers_in_order = [r["ticker"] for r in result]
    scores_in_order = [r["score"] for r in result]
    record("e) Liste absteigend nach Score sortiert (BBB=90, CCC=75, AAA=60)",
           tickers_in_order == ["BBB", "CCC", "AAA"], f"tickers={tickers_in_order} scores={scores_in_order}")


# ─────────────────────────────────────────────
# PFLICHT-SICHERHEITSTEST (Aufgabe Punkt 9): Cross-Account-Check
# ─────────────────────────────────────────────
def test_security_cross_account_no_access_to_updated_pending():
    wipe_state()
    set_confirm_mode(USER_A)

    with patch.object(confirm_execution, "send_email"), \
         patch.object(confirm_execution, "get_user_email", return_value="test@example.com"):
        main_module._execute_or_queue_entry(make_signal(ticker="AMZN", score=70, price=200.0), {}, USER_A)
        # Zweiter Zyklus: derselbe Eintrag wird aktualisiert (genau das neue
        # Chunk-2d-Verhalten) - der Sicherheitstest muss GEGEN DIESEN
        # aktualisierten Eintrag laufen, nicht gegen einen frischen.
        main_module._execute_or_queue_entry(make_signal(ticker="AMZN", score=78, price=205.0), {}, USER_A)

    with get_session() as session:
        row = session.query(PendingConfirmation).filter_by(user_id=USER_A, ticker="AMZN").first()
        pending_id = row.id
        session.expunge(row)
    record("Security-Testvoraussetzung: Eintrag wurde tatsächlich aktualisiert (205.0)",
           row.signal_price == 205.0, f"got={row.signal_price}")

    from fastapi.testclient import TestClient
    from jose import jwt
    client = TestClient(trading_api.app)
    token_b = jwt.encode({"sub": str(USER_B)}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")
    token_a = jwt.encode({"sub": str(USER_A)}, os.environ["JWT_SECRET_KEY"], algorithm="HS256")

    r = client.get("/api/pending-confirmations", headers={"Authorization": f"Bearer {token_b}"})
    record("a) User B sieht KEINE Zeile von User A in seiner eigenen Liste",
           all(row["ticker"] != "AMZN" for row in r.json()), str(r.json()))

    r = client.post(f"/api/pending-confirmations/{pending_id}/confirm", headers={"Authorization": f"Bearer {token_b}"})
    record("b) User B kann As aktualisierten Eintrag NICHT bestätigen (404, kein Leak ob er existiert)",
           r.status_code == 404, f"status={r.status_code} body={r.text}")

    r = client.post(f"/api/pending-confirmations/{pending_id}/reject", headers={"Authorization": f"Bearer {token_b}"})
    record("c) User B kann As aktualisierten Eintrag NICHT ablehnen (404)",
           r.status_code == 404, f"status={r.status_code}")

    r = client.get("/api/pending-confirmations", headers={"Authorization": f"Bearer {token_a}"})
    record("d) Regression: User A sieht weiterhin seinen eigenen, aktualisierten Eintrag (205.0)",
           any(row["ticker"] == "AMZN" and row["signal_price"] == 205.0 for row in r.json()), str(r.json()))

    r = client.get("/api/pending-confirmations")
    record("e) Kein Token -> 401", r.status_code == 401, f"status={r.status_code}")


def main():
    for fn in (
        test_a_multiple_cycles_update_no_duplicate_no_extra_mail,
        test_b_proactive_expire_when_dropped_below_threshold,
        test_c_market_close_expires_via_expire_overdue,
        test_c2_compute_market_close_expiry_targets_16_00_et_same_day,
        test_d_price_recheck_against_updated_basis_not_original,
        test_e_sorting_by_score_descending,
        test_security_cross_account_no_access_to_updated_pending,
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
