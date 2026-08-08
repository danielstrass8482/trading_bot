"""
test_per_user_api_security.py – PFLICHT-SICHERHEITSTEST (Aufgabe Punkt 6,
"Presets/Kapitalaufteilung/Guardrails pro Nutzer", 2026-08-08).

Zwei gleichzeitig "eingeloggte" Test-Accounts (NICHT Produktionskonto,
user_id 9101/9102 – frei erfundene IDs, existieren in keiner echten
pos_users-Zeile) gegen die echte trading_api.py-App via FastAPI TestClient
(kein echter HTTP-Server nötig, aber echte ASGI-Request/Response-Pipeline
inkl. JWT-Cookie-Auth-Dependency – kein Mock von get_current_user_id).

Deckt für JEDEN der drei umgebauten Endpoint-Gruppen ab:
  (a) Account A setzt eigene Presets/Kapitalaufteilung/Guardrails, sieht sie
      korrekt (GET spiegelt PUT/POST).
  (b) Account B sieht davon unabhängige eigene (Default-)Werte.
  (c) Direkter Angriffsversuch: Account B versucht per Request-Body eine
      fremde user_id unterzubringen (Alias-Feld "user_id" im Body, wie es
      ein naiver Angreifer probieren würde) – MUSS wirkungslos bleiben, da
      jeder Endpoint user_id ausschließlich aus dem server-seitig geprüften
      JWT liest (Depends(get_current_user_id)), niemals aus dem Body.
  (d) Fehlender/ungültiger Token -> 401 für alle drei Endpoint-Gruppen.

KEINE Produktions-DB (siehe test_per_user_guardrails.py für Setup-Befehle,
identische Wegwerf-DB-Konvention). NIEMALS gegen echte Produktions-DB/Konto
ausführen.
"""
import os
import sys
import traceback
from datetime import datetime, timedelta
from unittest.mock import patch

os.environ.setdefault(
    "DATABASE_URL",
    "postgresql://peruser_test_tmp:peruser_test_tmp_pw@localhost:5432/alpaca_peruser_test",
)
for var in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY"):
    os.environ.setdefault(var, "")
os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-production-8f3a1c9d")

try:
    import trading_shared  # noqa: F401
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trading_shared"))

from jose import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import database  # noqa: E402
database.init_db()

import trading_api  # noqa: E402
import broker  # noqa: E402
from config import DEFAULT_USER_ID  # noqa: E402

client = TestClient(trading_api.app)

USER_A = 9101
USER_B = 9102


def token_for(user_id: int) -> str:
    return jwt.encode(
        {"sub": str(user_id), "exp": datetime.utcnow() + timedelta(hours=1)},
        trading_api.JWT_SECRET_KEY, algorithm=trading_api.JWT_ALGORITHM,
    )


def auth_headers(user_id: int) -> dict:
    return {"Authorization": f"Bearer {token_for(user_id)}"}


RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"{'✅ PASS' if ok else '❌ FAIL'} — {name}{(': ' + detail) if detail else ''}")


def wipe_test_users():
    with database.get_session() as session:
        session.query(database.UserBotConfig).filter(
            database.UserBotConfig.user_id.in_([USER_A, USER_B])
        ).delete(synchronize_session=False)
        session.query(database.CapitalAllocation).filter(
            database.CapitalAllocation.user_id.in_([USER_A, USER_B])
        ).delete(synchronize_session=False)
        session.commit()


# ─────────────────────────────────────────────
# (a)+(b)+(c) Guardrails: PUT /api/bot-config/{key}, GET /api/bot-config
# ─────────────────────────────────────────────
def test_guardrails_isolation_and_attack():
    wipe_test_users()

    r = client.put("/api/bot-config/MAX_TRADES_PER_DAY", json={"value": "17"}, headers=auth_headers(USER_A))
    record("a) Account A: PUT MAX_TRADES_PER_DAY=17 -> 200", r.status_code == 200, f"status={r.status_code} body={r.text}")

    r = client.get("/api/bot-config", headers=auth_headers(USER_A))
    cfg_a = {row["key"]: row["value"] for row in r.json()}
    record("a) Account A: GET /api/bot-config spiegelt eigenen Wert (17)", cfg_a.get("MAX_TRADES_PER_DAY") == "17",
           f"got={cfg_a.get('MAX_TRADES_PER_DAY')}")
    record("a) Account A: GET /api/bot-config crasht NICHT auf EXECUTION_MODE/PRICE_TOLERANCE_PCT (Bugfix)",
           "EXECUTION_MODE" in cfg_a and "PRICE_TOLERANCE_PCT" in cfg_a, str(list(cfg_a.keys())))

    r = client.get("/api/bot-config", headers=auth_headers(USER_B))
    cfg_b = {row["key"]: row["value"] for row in r.json()}
    record("b) Account B: eigener Default-Wert (2), unabhängig von Account A", cfg_b.get("MAX_TRADES_PER_DAY") == "2",
           f"got={cfg_b.get('MAX_TRADES_PER_DAY')}")

    # (c) Angriff: B versucht per Body-Feld "user_id" Account A zu adressieren.
    # Wert "42" bewusst innerhalb der gültigen Grenzen (1-50), damit dieser
    # Test wirklich die Cross-Tenant-Frage prüft statt zufällig an der
    # Bounds-Validierung zu scheitern.
    r = client.put(
        "/api/bot-config/MAX_TRADES_PER_DAY",
        json={"value": "42", "user_id": USER_A},
        headers=auth_headers(USER_B),
    )
    record("c) Angriff: B schickt user_id=A im Body -> Server ignoriert es (200, aber nur B's eigene Zeile geändert)",
           r.status_code == 200, f"status={r.status_code}")
    r = client.get("/api/bot-config", headers=auth_headers(USER_A))
    cfg_a_after = {row["key"]: row["value"] for row in r.json()}
    record("c) Account A's MAX_TRADES_PER_DAY bleibt 17 (NICHT von B's Angriff auf 42 verändert)",
           cfg_a_after.get("MAX_TRADES_PER_DAY") == "17", f"got={cfg_a_after.get('MAX_TRADES_PER_DAY')}")

    # Validierungsgrenzen (Punkt 4): DAILY_LOSS_LIMIT_PCT darf nicht 0/1.0 sein.
    r = client.put("/api/bot-config/DAILY_LOSS_LIMIT_PCT", json={"value": "0"}, headers=auth_headers(USER_A))
    record("Validierung: DAILY_LOSS_LIMIT_PCT=0 -> 400 (nicht 0% erlaubt)", r.status_code == 400, f"status={r.status_code}")
    r = client.put("/api/bot-config/DAILY_LOSS_LIMIT_PCT", json={"value": "1.0"}, headers=auth_headers(USER_A))
    record("Validierung: DAILY_LOSS_LIMIT_PCT=1.0 -> 400 (nicht 100% erlaubt)", r.status_code == 400, f"status={r.status_code}")
    r = client.put("/api/bot-config/MAX_OPEN_POSITIONS", json={"value": "-3"}, headers=auth_headers(USER_A))
    record("Validierung: MAX_OPEN_POSITIONS=-3 -> 400 (negativ nicht erlaubt)", r.status_code == 400, f"status={r.status_code}")
    r = client.put("/api/bot-config/EXECUTION_MODE", json={"value": "yolo"}, headers=auth_headers(USER_A))
    record("Validierung: EXECUTION_MODE='yolo' -> 400 (nur auto/confirm erlaubt)", r.status_code == 400, f"status={r.status_code}")
    r = client.put("/api/bot-config/ATR_MIN_SL_PCT", json={"value": "0.5"}, headers=auth_headers(USER_A))
    record("Validierung: ATR_MIN_SL_PCT=0.5 > Default ATR_MAX_SL_PCT=0.08 -> 400 (Kreuzvalidierung)",
           r.status_code == 400, f"status={r.status_code} body={r.text}")

    # Owner-only globale Keys bleiben für Nicht-Owner gesperrt.
    r = client.put("/api/bot-config/ALPACA_DRAIN_MODE", json={"value": "true"}, headers=auth_headers(USER_A))
    record("Regression: Nicht-Owner darf globalen Key ALPACA_DRAIN_MODE weiterhin NICHT setzen (403)",
           r.status_code == 403, f"status={r.status_code}")


# ─────────────────────────────────────────────
# (a)+(b)+(c) Kapitalaufteilung: PUT/GET /api/capital-allocations
# ─────────────────────────────────────────────
def test_capital_allocations_isolation_and_attack():
    wipe_test_users()

    # Test-Accounts haben keinen echten pos_users-Eintrag/Alpaca-Connect
    # (siehe Moduldocstring) - get_alpaca_account_snapshot() gemockt auf
    # None, damit dieser API-Test den Fail-safe-Pfad (Flat-Wert-Fallback)
    # nutzt statt an der fehlenden pos_users-Tabelle in der Wegwerf-Test-DB
    # zu scheitern. BEIDE Referenzen patchen: trading_api importiert die
    # Funktion direkt (eigener Modul-Namespace), broker.
    # get_effective_max_capital_total_bot() ruft intern aber SEINE EIGENE,
    # unabhängig gebundene Modul-Referenz auf - ein reines
    # patch.object(trading_api, ...) hätte diesen zweiten Aufruf verfehlt.
    # Der Prozent-von-Equity-Pfad ist bereits in
    # test_per_user_guardrails.py::test_c_capital_allocations_per_user
    # direkt gegen broker.get_effective_max_capital_total_bot abgedeckt.
    with patch.object(trading_api, "get_alpaca_account_snapshot", return_value=None), \
         patch.object(broker, "get_alpaca_account_snapshot", return_value=None):
        r = client.put("/api/capital-allocations", json={"allocations": {"bot": 30.0, "active_trading": 70.0}},
                        headers=auth_headers(USER_A))
        record("a) Account A: PUT eigene Kapitalaufteilung (30/70) -> 200", r.status_code == 200, f"status={r.status_code} body={r.text}")

        r = client.get("/api/capital-allocations", headers=auth_headers(USER_A))
        body_a = r.json()
        record("a) Account A: GET spiegelt eigenen Wert (bot=30.0)", body_a["allocations"].get("bot") == 30.0, str(body_a))

        r = client.get("/api/capital-allocations", headers=auth_headers(USER_B))
        body_b = r.json()
        record("b) Account B: eigener, unabhängiger Default (bot=100.0, nie von A berührt)",
               body_b["allocations"].get("bot") == 100.0, str(body_b))

        # (c) Angriff: B versucht per Body user_id=A zu adressieren.
        r = client.put(
            "/api/capital-allocations",
            json={"allocations": {"bot": 1.0, "active_trading": 99.0}, "user_id": USER_A},
            headers=auth_headers(USER_B),
        )
        record("c) Angriff: B schickt user_id=A im Body -> nur B's eigene Zeile geändert (200)", r.status_code == 200,
               f"status={r.status_code}")
        r = client.get("/api/capital-allocations", headers=auth_headers(USER_A))
        body_a_after = r.json()
        record("c) Account A's Kapitalaufteilung bleibt bot=30.0 (NICHT von B's Angriff auf 1.0 verändert)",
               body_a_after["allocations"].get("bot") == 30.0, str(body_a_after))

        r = client.put("/api/capital-allocations", json={"allocations": {"bot": -5.0, "active_trading": 105.0}},
                        headers=auth_headers(USER_A))
        record("Validierung: negativer Prozentwert -> 400", r.status_code == 400, f"status={r.status_code}")

        # Regression: Daniel (Owner) unverändert erreichbar.
        r = client.get("/api/capital-allocations", headers=auth_headers(DEFAULT_USER_ID))
        record("Regression: Owner (Daniel) kann /api/capital-allocations weiterhin lesen (200)",
               r.status_code == 200, f"status={r.status_code}")


# ─────────────────────────────────────────────
# (a)+(b)+(c) Presets: POST /api/bot-config/preset
# ─────────────────────────────────────────────
def test_presets_isolation_and_attack():
    wipe_test_users()

    r = client.post("/api/bot-config/preset", json={"preset": "konservativ"}, headers=auth_headers(USER_A))
    record("a) Account A: POST Preset 'konservativ' -> 200", r.status_code == 200, f"status={r.status_code} body={r.text}")
    settings = r.json().get("settings", {})
    record("a) Account A: Antwort enthält NUR pro-Nutzer-fähige Keys (kein ATR_MULTIPLIER_TP)",
           "ATR_MULTIPLIER_TP" not in settings and "MAX_CAPITAL_PER_TRADE" in settings, str(settings))

    r = client.get("/api/bot-config", headers=auth_headers(USER_A))
    cfg_a = {row["key"]: row["value"] for row in r.json()}
    record("a) Account A: eigenes MAX_CAPITAL_PER_TRADE jetzt 30 (Preset-Wert)", cfg_a.get("MAX_CAPITAL_PER_TRADE") == "30.0",
           f"got={cfg_a.get('MAX_CAPITAL_PER_TRADE')}")

    r = client.get("/api/bot-config", headers=auth_headers(USER_B))
    cfg_b = {row["key"]: row["value"] for row in r.json()}
    record("b) Account B: unveränderter eigener Default (20.0), kein Preset angewendet",
           cfg_b.get("MAX_CAPITAL_PER_TRADE") == "20.0", f"got={cfg_b.get('MAX_CAPITAL_PER_TRADE')}")

    # (c) Angriff: B versucht per Body user_id=A ein Preset für A zu triggern.
    r = client.post("/api/bot-config/preset", json={"preset": "aggressiv", "user_id": USER_A}, headers=auth_headers(USER_B))
    record("c) Angriff: B schickt user_id=A im Body -> nur B's eigene Config geändert (200)", r.status_code == 200,
           f"status={r.status_code}")
    r = client.get("/api/bot-config", headers=auth_headers(USER_A))
    cfg_a_after = {row["key"]: row["value"] for row in r.json()}
    record("c) Account A's Preset-Werte bleiben 'konservativ' (NICHT von B's Angriff auf 'aggressiv' verändert)",
           cfg_a_after.get("MAX_CAPITAL_PER_TRADE") == "30.0", f"got={cfg_a_after.get('MAX_CAPITAL_PER_TRADE')}")

    r = client.post("/api/bot-config/preset", json={"preset": "nicht-existent"}, headers=auth_headers(USER_A))
    record("Validierung: unbekanntes Preset -> 400", r.status_code == 400, f"status={r.status_code}")


# ─────────────────────────────────────────────
# (d) Kein/ungültiger Token -> 401 für alle drei Endpoint-Gruppen
# ─────────────────────────────────────────────
def test_unauthenticated_rejected():
    for method, path, kwargs in [
        ("get", "/api/bot-config", {}),
        ("put", "/api/bot-config/MAX_TRADES_PER_DAY", {"json": {"value": "5"}}),
        ("get", "/api/capital-allocations", {}),
        ("put", "/api/capital-allocations", {"json": {"allocations": {"bot": 50.0, "active_trading": 50.0}}}),
        ("post", "/api/bot-config/preset", {"json": {"preset": "konservativ"}}),
    ]:
        r = getattr(client, method)(path, **kwargs)  # kein Authorization-Header
        record(f"d) {method.upper()} {path} ohne Token -> 401", r.status_code == 401, f"status={r.status_code}")

        r = getattr(client, method)(path, headers={"Authorization": "Bearer offensichtlich-kein-jwt"}, **kwargs)
        record(f"d) {method.upper()} {path} mit kaputtem Token -> 401", r.status_code == 401, f"status={r.status_code}")


def main():
    for fn in (test_guardrails_isolation_and_attack, test_capital_allocations_isolation_and_attack,
               test_presets_isolation_and_attack, test_unauthenticated_rejected):
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
