"""
test_company_name_display.py – Mock/Test-DB-basierter Test für die
Firmenname-Anzeige ("Firmenname (TICKER)", Beta-Feedback Dana, 2026-08-13,
siehe database.TickerCompanyName/ensure_company_name_cached/
get_company_names + trading_api._attach_company_names).

KEINE echte Alpaca-Verbindung: get_alpaca_account_snapshot() wird auf None
gemockt (analog test_confirm_tier_race_condition.py), PAPER-Modus (Default)
braucht dafür keinen echten Order-Netzwerk-Call. yfinance wird gezielt
gemockt, wo ein Firmenname-Lookup stattfindet.
KEINE Produktions-DB: DATABASE_URL zeigt auf eine eigene Wegwerf-Postgres-DB
(SQLite scheitert an den Postgres-spezifischen Migrationen in database.py) –
vor dem Lauf einmalig anlegen, danach wieder droppen (analog
test_confirm_tier_race_condition.py):

    sudo pg_ctlcluster 16 main start   # falls der lokale Cluster gestoppt ist
    sudo -u postgres psql -c "CREATE USER companyname_test_tmp WITH PASSWORD 'companyname_test_tmp_pw';"
    sudo -u postgres psql -c "CREATE DATABASE alpaca_companyname_test OWNER companyname_test_tmp;"
    python3 test_company_name_display.py
    sudo -u postgres psql -c "DROP DATABASE alpaca_companyname_test;"
    sudo -u postgres psql -c "DROP USER companyname_test_tmp;"

NIEMALS gegen die echte Produktions-DB oder ein echtes Live-Konto ausführen.

Deckt ab:
  1) cache_company_name + get_company_names: Bulk-Read korrekt, fehlender
     Ticker fehlt einfach im Dict statt eines Fehlers.
  2) ensure_company_name_cached: Cache-Treffer macht KEINEN Netzwerk-Call.
  3) ensure_company_name_cached: Cache-Miss holt via yfinance nach und
     cached das Ergebnis dauerhaft.
  4) ensure_company_name_cached: yfinance-Fehler -> None, KEIN Eintrag (damit
     ein späterer Versuch es erneut probieren kann statt dauerhaft leer zu
     bleiben).
  5) broker.place_trade() cached den Firmennamen garantiert (Integration).
  6) confirm_execution.create_pending_confirmation() cached den Firmennamen
     garantiert (Integration).
  7) API-Ebene: /api/overview und /api/trades/history liefern company_name
     korrekt mit, inkl. sauberem None-Fallback für einen (noch) nicht
     gecachten Ticker (kein Crash, kein KeyError).
  8) Bestehende Tests dürfen nicht brechen (separater Lauf der übrigen
     test_*.py in derselben Session).
"""
import os
import sys
import traceback
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://companyname_test_tmp:companyname_test_tmp_pw@localhost:5432/alpaca_companyname_test",
)
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-production-8f3a1c9d")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

import database  # noqa: E402
database.init_db()

import broker  # noqa: E402
import confirm_execution  # noqa: E402
from config import DEFAULT_USER_ID  # noqa: E402
from rule_engine import SignalResult  # noqa: E402

from jose import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import trading_api  # noqa: E402

client = TestClient(trading_api.app)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_all_state():
    with database.get_session() as session:
        session.query(database.Trade).delete()
        session.query(database.PendingConfirmation).delete()
        session.query(database.TickerCompanyName).delete()
        session.commit()


def token_for(user_id: int) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": datetime.utcnow() + timedelta(hours=1)},
        trading_api.JWT_SECRET_KEY, algorithm=trading_api.JWT_ALGORITHM,
    )


def auth_headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {token_for(user_id)}"}


def make_signal(ticker):
    return SignalResult(
        ticker=ticker, score=80, direction="LONG", instrument_type="STOCK",
        approved=True, current_price=100.0, stop_loss=95.0, take_profit=110.0,
    )


def fake_yf_ticker(name: str | None, raise_error: bool = False):
    """Baut ein yf.Ticker-Ersatz-Objekt mit .info={'longName': name} bzw.
    wirft bei Zugriff auf .info, falls raise_error."""
    m = MagicMock()
    if raise_error:
        type(m).info = property(lambda self: (_ for _ in ()).throw(Exception("simulierter yfinance-Ausfall")))
    else:
        m.info = {"longName": name}
    m.fast_info.get.return_value = 100.0
    return m


# ─────────────────────────────────────────────
# 1) cache_company_name + get_company_names
# ─────────────────────────────────────────────
def test_1_cache_and_bulk_read():
    wipe_all_state()
    with database.get_session() as session:
        database.cache_company_name(session, "UPS", "United Parcel Service, Inc.")
        database.cache_company_name(session, "AAPL", "Apple Inc.")
        names = database.get_company_names(session, ["UPS", "AAPL", "MISSING"])
    ok = (
        names.get("UPS") == "United Parcel Service, Inc."
        and names.get("AAPL") == "Apple Inc."
        and "MISSING" not in names
    )
    record("1) cache_company_name + get_company_names: Bulk-Read korrekt, fehlender Ticker fehlt sauber", ok, str(names))


# ─────────────────────────────────────────────
# 2) ensure_company_name_cached: Cache-Treffer -> kein Netzwerk-Call
# ─────────────────────────────────────────────
def test_2_ensure_cached_hit_no_network():
    wipe_all_state()
    with database.get_session() as session:
        database.cache_company_name(session, "UPS", "United Parcel Service, Inc.")

    with patch("yfinance.Ticker") as mock_ticker:
        name = database.ensure_company_name_cached("UPS")

    record(
        "2) ensure_company_name_cached: Cache-Treffer liefert Namen ohne yfinance-Aufruf",
        name == "United Parcel Service, Inc." and not mock_ticker.called,
        f"name={name} yf_called={mock_ticker.called}"
    )


# ─────────────────────────────────────────────
# 3) ensure_company_name_cached: Cache-Miss -> Live-Fetch + dauerhaftes Cachen
# ─────────────────────────────────────────────
def test_3_ensure_cached_miss_fetches_and_caches():
    wipe_all_state()
    with patch("yfinance.Ticker", return_value=fake_yf_ticker("Apple Inc.")):
        name = database.ensure_company_name_cached("AAPL")

    with database.get_session() as session:
        cached = database.get_company_names(session, ["AAPL"])

    record(
        "3) ensure_company_name_cached: Cache-Miss holt via yfinance nach und cached dauerhaft",
        name == "Apple Inc." and cached.get("AAPL") == "Apple Inc.",
        f"name={name} cached={cached}"
    )


# ─────────────────────────────────────────────
# 4) ensure_company_name_cached: yfinance-Fehler -> None, kein Eintrag
# ─────────────────────────────────────────────
def test_4_ensure_cached_failure_no_pollution():
    wipe_all_state()
    with patch("yfinance.Ticker", return_value=fake_yf_ticker(None, raise_error=True)):
        name = database.ensure_company_name_cached("BROKEN")

    with database.get_session() as session:
        cached = database.get_company_names(session, ["BROKEN"])

    record(
        "4) ensure_company_name_cached: yfinance-Fehler liefert None, KEIN Eintrag (Retry beim nächsten Mal möglich)",
        name is None and "BROKEN" not in cached,
        f"name={name} cached={cached}"
    )


# ─────────────────────────────────────────────
# 5) broker.place_trade() cached den Firmennamen garantiert
# ─────────────────────────────────────────────
def test_5_place_trade_caches_company_name():
    wipe_all_state()
    signal = make_signal("MSFT")
    with patch.object(broker, "get_alpaca_account_snapshot", return_value=None), \
         patch("yfinance.Ticker", return_value=fake_yf_ticker("Microsoft Corporation")):
        trade = broker.place_trade(signal, {}, DEFAULT_USER_ID)

    with database.get_session() as session:
        cached = database.get_company_names(session, ["MSFT"])

    record(
        "5) place_trade() cached den Firmennamen garantiert",
        trade is not None and cached.get("MSFT") == "Microsoft Corporation",
        f"trade={trade.id if trade else None} cached={cached}"
    )


# ─────────────────────────────────────────────
# 6) confirm_execution.create_pending_confirmation() cached den Firmennamen
# ─────────────────────────────────────────────
def test_6_create_pending_confirmation_caches_company_name():
    wipe_all_state()
    with patch("yfinance.Ticker", return_value=fake_yf_ticker("NVIDIA Corporation")):
        pending = confirm_execution.create_pending_confirmation(
            user_id=DEFAULT_USER_ID, ticker="NVDA", quantity=1.0, signal_price=100.0,
        )

    with database.get_session() as session:
        cached = database.get_company_names(session, ["NVDA"])

    record(
        "6) create_pending_confirmation() cached den Firmennamen garantiert",
        pending is not None and cached.get("NVDA") == "NVIDIA Corporation",
        f"pending={pending.id if pending else None} cached={cached}"
    )


# ─────────────────────────────────────────────
# 7) API-Ebene: /api/overview + /api/trades/history liefern company_name
# ─────────────────────────────────────────────
def test_7_api_endpoints_include_company_name():
    wipe_all_state()
    with database.get_session() as session:
        database.cache_company_name(session, "UPS", "United Parcel Service, Inc.")
        trade = database.Trade(
            ticker="UPS", direction="LONG", instrument_type="STOCK",
            entry_price=100.0, stop_loss=95.0, take_profit=110.0,
            quantity=1.0, capital_used=100.0, rule_score=80,
            status="OPEN", mode="PAPER", broker="alpaca", user_id=DEFAULT_USER_ID,
        )
        session.add(trade)
        # Zweiter Trade mit einem NICHT gecachten Ticker - Fallback-Pfad.
        trade2 = database.Trade(
            ticker="NONAME", direction="LONG", instrument_type="STOCK",
            entry_price=50.0, stop_loss=45.0, take_profit=60.0,
            quantity=1.0, capital_used=50.0, rule_score=80,
            status="CLOSED_SL", exit_price=48.0, pnl_usd=-2.0, pnl_pct=-4.0,
            mode="PAPER", broker="alpaca", user_id=DEFAULT_USER_ID,
        )
        session.add(trade2)
        session.commit()

    fake_ticker = MagicMock()
    fake_ticker.fast_info.get.return_value = 100.0
    with patch("yfinance.Ticker", return_value=fake_ticker), \
         patch.object(trading_api, "get_alpaca_account_snapshot", return_value=None), \
         patch.object(trading_api, "get_portfolio_value", return_value=1000.0):
        overview_resp = client.get("/api/overview", headers=auth_headers(DEFAULT_USER_ID))
        history_resp = client.get("/api/trades/history", headers=auth_headers(DEFAULT_USER_ID))

    overview_ok = overview_resp.status_code == 200
    overview_names = {t["ticker"]: t.get("company_name") for t in overview_resp.json().get("open_trades", [])} if overview_ok else {}
    record(
        "7a) /api/overview liefert company_name für offene Position",
        overview_ok and overview_names.get("UPS") == "United Parcel Service, Inc.",
        f"status={overview_resp.status_code} names={overview_names}"
    )

    history_ok = history_resp.status_code == 200
    history_names = {t["ticker"]: t.get("company_name") for t in history_resp.json()} if history_ok else {}
    record(
        "7b) /api/trades/history liefert company_name für gecachten Ticker",
        history_ok and history_names.get("UPS") == "United Parcel Service, Inc.",
        f"status={history_resp.status_code} names={history_names}"
    )
    record(
        "7c) /api/trades/history: nicht gecachter Ticker liefert company_name=None statt Crash/fehlendem Key",
        history_ok and "NONAME" in history_names and history_names.get("NONAME") is None,
        f"names={history_names}"
    )


def main():
    for fn in (
        test_1_cache_and_bulk_read, test_2_ensure_cached_hit_no_network,
        test_3_ensure_cached_miss_fetches_and_caches, test_4_ensure_cached_failure_no_pollution,
        test_5_place_trade_caches_company_name, test_6_create_pending_confirmation_caches_company_name,
        test_7_api_endpoints_include_company_name,
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
