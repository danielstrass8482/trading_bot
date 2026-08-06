"""
test_order_verification_retry.py – Mock-Test für Code-Audit Chunk 1, Fund 2
(siehe trading_shared/docs/full-code-audit-06-08.md): _submit_order_idempotent()/
_reconcile_pending_entry_attempt() dürfen einen technischen Verifikations-
Fehlschlag (Timeout/5xx/Netzwerk beim Nachschauen, ob eine Order angekommen
ist) NICHT mit einem bestätigten "Order nie angekommen" verwechseln.

KEINE echte Alpaca-Verbindung: der Alpaca-Client wird komplett gemockt
(unittest.mock), keine Order wird je real gesendet.
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(SQLite scheitert an den Postgres-spezifischen Migrationen in database.py):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER audit_fix_tmp WITH PASSWORD 'audit_fix_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_audit_fix_test OWNER audit_fix_tmp;"
    python3 test_order_verification_retry.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_audit_fix_test;"
    sudo -u postgres psql -c "DROP USER audit_fix_tmp;"

Deckt ab (AUFGABE 6, Testfall b):
  (a) Verifikation schlägt N-1 mal technisch fehl (Timeout), dann Erfolg
      -> Order wird gefunden, KEIN Doppel-Versuch, KEIN FAILED markiert
  (b) Verifikation schlägt bei ALLEN Versuchen technisch fehl (nie ein
      bestätigtes "nicht gefunden") -> GuardrailViolation, PendingOrderAttempt
      bleibt PENDING (NICHT FAILED) - kein automatischer neuer Kauf
  (c) Verifikation liefert ein bestätigtes 404 -> weiterhin korrekt als
      FAILED markiert, neuer Versuch bleibt sicher erlaubt (Regressionscheck
      auf das bisherige, korrekte Verhalten)
  (d) _reconcile_pending_entry_attempt(): derselbe Verifikations-Fehlschlag-
      Fall für einen VOR dem aktuellen Lauf bereits PENDING stehenden Versuch
"""
import os
import sys
import traceback
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://audit_fix_tmp:audit_fix_tmp_pw@localhost:5432/alpaca_audit_fix_test",
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
        session.query(database.PendingOrderAttempt).delete()
        session.commit()


class FakeOrder:
    def __init__(self, order_id="ORD-EXISTS", status="filled"):
        self.id = order_id
        self.status = status


class FlakyThenFoundClient:
    """submit_order() schlägt fehl; get_order_by_client_order_id() schlägt
    die ersten `fail_times` Versuche technisch fehl (Timeout), dann liefert
    sie erfolgreich die (tatsächlich existierende) Order zurück."""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def submit_order(self, **kwargs):
        raise TimeoutError("simulierter Netzwerk-Timeout beim Order-Submit")

    def get_order_by_client_order_id(self, client_order_id):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("simulierter Netzwerk-Timeout bei der Verifikation")
        return FakeOrder()


class AlwaysFlakyClient:
    """submit_order() UND jede Verifikation schlagen technisch fehl - nie ein
    bestätigtes 'existiert nicht' (kein 404/'not found' im Fehlertext)."""
    def submit_order(self, **kwargs):
        raise TimeoutError("simulierter Netzwerk-Timeout beim Order-Submit")

    def get_order_by_client_order_id(self, client_order_id):
        raise ConnectionError("simulierter dauerhafter Netzwerkfehler bei der Verifikation")


class ConfirmedNotFoundClient:
    """submit_order() schlägt fehl, Verifikation liefert ein ECHTES 404 -
    Regressionscheck auf das weiterhin korrekte 'nachweislich nie angekommen'-Verhalten."""
    def submit_order(self, **kwargs):
        raise TimeoutError("simulierter Netzwerk-Timeout beim Order-Submit")

    def get_order_by_client_order_id(self, client_order_id):
        raise Exception("404 order not found")


# ─────────────────────────────────────────────
# (a) Verifikation erst nach Retries erfolgreich -> Order gefunden, kein Doppel-Versuch
# ─────────────────────────────────────────────
def test_a_verification_succeeds_after_retry():
    wipe_all_state()
    client = FlakyThenFoundClient(fail_times=2)  # 2 fehlgeschlagene Versuche, 3. klappt
    with patch("time.sleep"):  # Test soll nicht wirklich 1s+2s warten
        try:
            order, client_order_id = broker._submit_order_idempotent(client, "AAA", DEFAULT_USER_ID, symbol="AAA")
            ok = order.id == "ORD-EXISTS" and client.calls == 3
            record("a) Verifikation nach 2 fehlgeschlagenen Versuchen erfolgreich -> Order übernommen",
                   ok, f"order_id={order.id} calls={client.calls}")
        except Exception as e:
            record("a) Verifikation nach 2 fehlgeschlagenen Versuchen erfolgreich -> Order übernommen",
                   False, repr(e))

    with database.get_session() as session:
        row = session.query(database.PendingOrderAttempt).filter_by(ticker="AAA").order_by(
            database.PendingOrderAttempt.created_at.desc()).first()
    record("a) PendingOrderAttempt korrekt als FILLED markiert", row is not None and row.status == "FILLED",
           f"status={row.status if row else None}")


# ─────────────────────────────────────────────
# (b) Verifikation dauerhaft technisch unklar -> GuardrailViolation, PENDING bleibt PENDING
# ─────────────────────────────────────────────
def test_b_verification_always_inconclusive():
    wipe_all_state()
    client = AlwaysFlakyClient()
    with patch("time.sleep"):
        try:
            broker._submit_order_idempotent(client, "BBB", DEFAULT_USER_ID, symbol="BBB")
            record("b) dauerhaft unklare Verifikation -> GuardrailViolation erwartet", False, "keine Exception geworfen!")
        except broker.GuardrailViolation as gv:
            record("b) dauerhaft unklare Verifikation -> GuardrailViolation (kein Doppel-Kauf-Freibrief)", True, str(gv))
        except Exception as e:
            record("b) dauerhaft unklare Verifikation -> GuardrailViolation (kein Doppel-Kauf-Freibrief)",
                   False, f"falscher Exception-Typ: {repr(e)}")

    with database.get_session() as session:
        row = session.query(database.PendingOrderAttempt).filter_by(ticker="BBB").order_by(
            database.PendingOrderAttempt.created_at.desc()).first()
    ok = row is not None and row.status == "PENDING"
    record("b) PendingOrderAttempt bleibt PENDING (NICHT fälschlich FAILED)", ok,
           f"status={row.status if row else None}")


# ─────────────────────────────────────────────
# (c) Regressionscheck: bestätigtes 404 -> weiterhin korrekt FAILED + neuer Versuch erlaubt
# ─────────────────────────────────────────────
def test_c_confirmed_not_found_still_works():
    wipe_all_state()
    client = ConfirmedNotFoundClient()
    with patch("time.sleep"):
        try:
            broker._submit_order_idempotent(client, "CCC", DEFAULT_USER_ID, symbol="CCC")
            record("c) bestätigtes 404 -> ursprüngliche Exception erwartet", False, "keine Exception geworfen!")
        except broker.GuardrailViolation as gv:
            record("c) bestätigtes 404 -> ursprüngliche Exception erwartet", False, f"fälschlich GuardrailViolation: {gv}")
        except TimeoutError:
            record("c) bestätigtes 404 -> ursprüngliche TimeoutError durchgereicht (Regression OK)", True)
        except Exception as e:
            record("c) bestätigtes 404 -> ursprüngliche Exception erwartet", False, f"unerwarteter Typ: {repr(e)}")

    with database.get_session() as session:
        row = session.query(database.PendingOrderAttempt).filter_by(ticker="CCC").order_by(
            database.PendingOrderAttempt.created_at.desc()).first()
    ok = row is not None and row.status == "FAILED"
    record("c) PendingOrderAttempt korrekt als FAILED markiert (Regression OK)", ok,
           f"status={row.status if row else None}")


# ─────────────────────────────────────────────
# (d) _reconcile_pending_entry_attempt(): derselbe Fall für einen bereits PENDING stehenden Versuch
# ─────────────────────────────────────────────
def test_d_reconcile_pending_entry_attempt_inconclusive():
    wipe_all_state()
    with database.get_session() as session:
        session.add(database.PendingOrderAttempt(ticker="DDD", client_order_id="old-attempt-ddd", user_id=DEFAULT_USER_ID))
        session.commit()

    client = AlwaysFlakyClient()
    with patch("time.sleep"):
        try:
            broker._reconcile_pending_entry_attempt(client, "DDD", DEFAULT_USER_ID)
            record("d) _reconcile_pending_entry_attempt bei dauerhaft unklarer Verifikation -> GuardrailViolation",
                   False, "keine Exception geworfen (hätte einen neuen Kauf erlaubt)!")
        except broker.GuardrailViolation as gv:
            record("d) _reconcile_pending_entry_attempt bei dauerhaft unklarer Verifikation -> GuardrailViolation",
                   True, str(gv))
        except Exception as e:
            record("d) _reconcile_pending_entry_attempt bei dauerhaft unklarer Verifikation -> GuardrailViolation",
                   False, f"falscher Exception-Typ: {repr(e)}")

    with database.get_session() as session:
        row = session.query(database.PendingOrderAttempt).filter_by(client_order_id="old-attempt-ddd").first()
    ok = row is not None and row.status == "PENDING"
    record("d) alter PendingOrderAttempt bleibt PENDING (kein automatischer neuer Kauf freigegeben)", ok,
           f"status={row.status if row else None}")


def main():
    for fn in (test_a_verification_succeeds_after_retry, test_b_verification_always_inconclusive,
               test_c_confirmed_not_found_still_works, test_d_reconcile_pending_entry_attempt_inconclusive):
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
