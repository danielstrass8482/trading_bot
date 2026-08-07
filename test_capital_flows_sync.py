"""
test_capital_flows_sync.py – Tests für die Kapitalfluss-Erfassung Chunk 1
(2026-08-07, siehe broker.sync_capital_flows/database.CapitalFlow).

KEIN Live-Alpaca-Zugriff: der Alpaca-Client (client.get_activities) wird in
jedem Testfall komplett gemockt - die echte, empirisch verifizierte API-
Antwort (siehe Commit-Message/Doku) wurde bereits einmalig separat gegen
das echte LIVE-Konto geprüft (nur lesend), nicht Teil dieser Testsuite.

KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(analog test_confirm_tier_chunk1_migration.py):

    sudo pg_ctlcluster 16 main start
    sudo -u postgres psql -c "CREATE USER capflow_tmp WITH PASSWORD 'capflow_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_capflow_test OWNER capflow_tmp;"
    python3 test_capital_flows_sync.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_capflow_test;"
    sudo -u postgres psql -c "DROP USER capflow_tmp;"
"""
import os
import sys
import traceback
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://capflow_tmp:capflow_tmp_pw@localhost:5432/alpaca_capflow_test",
)
for var in ("ANTHROPIC_API_KEY",):
    os.environ.setdefault(var, "")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
import broker  # noqa: E402

database.init_db()

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_capital_flows():
    with database.get_session() as session:
        session.query(database.CapitalFlow).delete()
        session.commit()


class FakeActivity:
    def __init__(self, id, activity_type, date, net_amount, currency="USD"):
        self.id = id
        self.activity_type = activity_type
        self.date = date
        self.net_amount = net_amount
        self.currency = currency


class FakeClient:
    """Simuliert tradeapi.REST – get_activities() gibt vordefinierte Seiten zurück."""
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get_activities(self, activity_types=None, direction=None, page_size=None, page_token=None):
        self.calls.append({"page_token": page_token})
        if not self.pages:
            return []
        return self.pages.pop(0)


def test_a_initial_sync_inserts_correctly():
    wipe_capital_flows()
    client = FakeClient([[
        FakeActivity("act-1", "CSD", "2026-07-22", "475"),
        FakeActivity("act-2", "CSW", "2026-08-01", "-50.5"),
    ]])

    with patch.object(broker, "_get_alpaca_client", return_value=client):
        n = broker.sync_capital_flows(user_id=1)

    record("a) 2 neue Datensätze eingefügt", n == 2, f"n={n}")

    with database.get_session() as session:
        dep = session.query(database.CapitalFlow).filter_by(broker_reference_id="act-1").first()
        wd = session.query(database.CapitalFlow).filter_by(broker_reference_id="act-2").first()

    record("a) CSD korrekt als 'deposit', amount=475.0 (float, nicht String)",
           dep is not None and dep.flow_type == "deposit" and dep.amount == 475.0 and isinstance(dep.amount, float))
    record("a) CSW korrekt als 'withdrawal', amount=-50.5", wd is not None and wd.flow_type == "withdrawal" and wd.amount == -50.5)
    record("a) currency korrekt übernommen", dep.currency == "USD")
    record("a) user_id korrekt gesetzt", dep.user_id == 1 and wd.user_id == 1)


def test_b_repeated_sync_is_idempotent():
    wipe_capital_flows()
    activities = [FakeActivity("act-1", "CSD", "2026-07-22", "475")]

    client1 = FakeClient([activities])
    with patch.object(broker, "_get_alpaca_client", return_value=client1):
        n1 = broker.sync_capital_flows(user_id=1)

    client2 = FakeClient([activities])  # identische Activity, wie bei einem erneuten Sync-Lauf
    with patch.object(broker, "_get_alpaca_client", return_value=client2):
        n2 = broker.sync_capital_flows(user_id=1)

    record("b) erster Sync fügt 1 Datensatz ein", n1 == 1, f"n1={n1}")
    record("b) zweiter Sync mit identischer Activity fügt 0 NEUE Datensätze ein (Dedup)", n2 == 0, f"n2={n2}")

    with database.get_session() as session:
        count = session.query(database.CapitalFlow).filter_by(broker_reference_id="act-1").count()
    record("b) trotzdem nur GENAU EINE Zeile in der DB (kein Doppel-Eintrag)", count == 1, f"count={count}")


def test_c_pagination_collects_all_pages():
    wipe_capital_flows()
    page_1 = [FakeActivity(f"p1-{i}", "CSD", "2026-01-01", "10") for i in range(100)]
    page_2 = [FakeActivity(f"p2-{i}", "CSD", "2026-02-01", "20") for i in range(3)]
    client = FakeClient([page_1, page_2])

    with patch.object(broker, "_get_alpaca_client", return_value=client):
        n = broker.sync_capital_flows(user_id=1)

    record("c) beide Seiten (100 + 3) wurden vollständig eingesammelt", n == 103, f"n={n}")
    record("c) zweiter Aufruf nutzte den page_token der letzten Activity aus Seite 1",
           client.calls[1]["page_token"] == "p1-99", f"calls={client.calls}")


def test_d_malformed_record_skipped_without_crash():
    wipe_capital_flows()
    client = FakeClient([[
        FakeActivity("good-1", "CSD", "2026-07-22", "475"),
        FakeActivity("bad-1", "CSD", "2026-07-22", "not-a-number"),
    ]])

    with patch.object(broker, "_get_alpaca_client", return_value=client):
        try:
            n = broker.sync_capital_flows(user_id=1)
            crashed = False
        except Exception:
            n = 0
            crashed = True

    record("d) kein Crash bei fehlerhaftem net_amount", not crashed)
    record("d) der gültige Datensatz wurde trotzdem eingefügt", n == 1, f"n={n}")


def test_e_no_client_available_returns_zero():
    wipe_capital_flows()
    with patch.object(broker, "_get_alpaca_client", return_value=None):
        n = broker.sync_capital_flows(user_id=999)
    record("e) kein Alpaca-Client verfügbar -> 0, kein Crash", n == 0, f"n={n}")


def main():
    for fn in (test_a_initial_sync_inserts_correctly, test_b_repeated_sync_is_idempotent,
               test_c_pagination_collects_all_pages, test_d_malformed_record_skipped_without_crash,
               test_e_no_client_available_returns_zero):
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
