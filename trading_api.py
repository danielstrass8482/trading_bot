"""
trading_api.py – FastAPI-Backend für das Trading-Bot-React-Frontend
(trading_react, trading.diestraesschens.de).

Auth: teilt sich die JWT-Cookie-Session mit portfolio_os (identischer
JWT_SECRET_KEY, siehe .env-Kommentar) – Login läuft weiterhin über
portfolio_os/api.py (Port 8503, gleiche pos_users-Tabelle, gemeinsame DB).
Hier wird nur die Signatur/Ablaufzeit desselben Tokens geprüft, ohne
erneuten DB-Lookup, da dieser Service keine eigene User-Tabelle kennt.
Alle Datenendpunkte liegen hinter dieser Prüfung – ohne sie wäre eine
öffentlich erreichbare Config-API für einen live Trading-Bot erreichbar
(inkl. PUT /api/bot-config/{key}, das Guardrails ändern kann).
"""

import os
import json
from datetime import datetime
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Cookie
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from jose import JWTError, jwt
from typing import Optional

from database import (
    get_session, get_daily_trade_count, set_bot_config,
    get_learning_proposals, set_learning_proposals, get_user_live_config,
    get_trade_mode_for_user, set_capital_allocations,
    set_user_bot_config, DEFAULT_USER_CONFIG,
    USER_CONFIG_BOUNDS, USER_CONFIG_ENUM_BOUNDS, get_capital_allocations,
)
from config import get_live_config, DEFAULT_USER_ID
from broker import (
    get_effective_max_capital_total_bot, get_or_seed_capital_allocations,
    CAPITAL_ALLOCATION_CATEGORIES,
    get_portfolio_value, get_alpaca_account_snapshot, count_trading_days,
    get_pause_status,
)
from rule_engine import get_market_regime
import yfinance as yf

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY nicht gesetzt! (gleicher Wert wie portfolio_os/.env nötig)")
JWT_ALGORITHM = "HS256"


def get_current_user_id(request: Request, token: Optional[str] = Cookie(default=None, alias="token")) -> int:
    """
    KRITISCHER SICHERHEITSFIX 2026-07-31: Prüft dasselbe JWT-Cookie wie
    portfolio_os (siehe get_current_user dort), liest aber jetzt zusätzlich
    den "sub"-Claim (user_id, siehe portfolio_os/api.py create_access_token
    -> {"sub": str(user.id)}) aus und gibt ihn zurück. Vorher wurde das
    decodierte Payload komplett verworfen (nur Signatur/Ablaufzeit geprüft) –
    dadurch bekam JEDER eingeloggte Nutzer unabhängig von seiner Identität
    dieselben, ungefilterten globalen Daten (Daniels Live-Kapital/
    Positionen/Handelshistorie, siehe Diagnose vom 2026-07-31). Jeder
    Endpoint unten scopt seine Queries jetzt auf diese user_id statt
    implizit auf DEFAULT_USER_ID/die globale Tabelle zuzugreifen.
    Bearer-Header als Fallback wie im Portfolio-OS-Pendant.
    """
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    raw_sub = payload.get("sub")
    if raw_sub is None:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    try:
        return int(raw_sub)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Nicht autorisiert")


def require_owner(user_id: int) -> None:
    """
    Einige verbleibende Endpoints betreffen ABSICHTLICH globale, strukturell
    nicht pro-Nutzer scopebare Ressourcen: entry_time_slots (EIN gemeinsamer
    Einstiegszeitplan für den gemeinsamen Signal-Scan) und learning_proposals/
    ENTRY_LEARNING_MODE (Vorschläge des wöchentlichen Backlook-Lernzyklus,
    wirken auf Daniels globale bot_config, z.B. MIN_SIGNAL_SCORE) sind
    Daniels Bot-Kontrollpanel für den geteilten Scan, kein Kunden-
    Risikoparameter (siehe DEFAULT_USER_CONFIG-Docstring in database.py für
    die volle "Klasse A/B/C"-Einordnung). Presets und Kapitalaufteilung
    waren bis 2026-08-08 ebenfalls hier gated – siehe trading_api.py-Git-
    History bzw. Bericht "Presets/Kapitalaufteilung/Guardrails pro Nutzer"
    für die Begründung, warum sie das nicht mehr sind (regulatorischer
    Hintergrund: jeder Kunde muss seine eigenen Handelsparameter selbst
    festlegen können).
    """
    if user_id != DEFAULT_USER_ID:
        raise HTTPException(status_code=403, detail="Nicht verfügbar für diesen Account")


app = FastAPI(title="Trading Bot API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://trading.diestraesschens.de",
        "https://app.ai-tradingbot.de",
        "http://localhost:3001",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["Content-Type", "Authorization"],
)

# Alle Business-Endpoints hängen an diesem Router statt direkt an `app` –
# gleiches Muster wie portfolio_os/api.py (protected-Router mit
# Router-weiter Auth-Dependency). Nur /api/health bleibt öffentlich.
# get_current_user_id() ist hier als reine Enforcement-Dependency verdrahtet
# (garantiert 401 für jeden zukünftigen Endpoint, auch falls dessen Autor
# vergisst, den user_id-Parameter unten explizit zu deklarieren) – FastAPI
# cached die Dependency pro Request, ein zusätzliches user_id: int =
# Depends(get_current_user_id) an einzelnen Endpoints unten löst sie NICHT
# doppelt aus, sondern liefert nur den bereits ermittelten Wert.
protected = APIRouter(dependencies=[Depends(get_current_user_id)])


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/saxo/callback")
async def saxo_callback(code: str = None, error: str = None):
    if error:
        return {"error": error}
    if code:
        with open("/tmp/saxo_auth_code.txt", "w") as f:
            f.write(code)
        return HTMLResponse(content="""
            <html><body style="background:#161412;color:#f0ede8;
                               font-family:Inter;padding:2rem;">
            <h2 style="color:#c9a252">✅ Saxo Login erfolgreich!</h2>
            <p>Auth Code wurde gespeichert.</p>
            <p>Du kannst diesen Tab schließen.</p>
            </body></html>
        """)
    return {"error": "Kein Code erhalten"}


@protected.get("/api/overview")
def get_overview(user_id: int = Depends(get_current_user_id)):
    # get_user_live_config() statt get_live_config(): liefert Daniels globale
    # bot_config 1:1 für DEFAULT_USER_ID, für jeden anderen Nutzer aber dessen
    # EIGENE Guardrails aus user_bot_config (siehe database.py) – vorher
    # bekam jeder eingeloggte Nutzer Daniels max_trades_per_day/
    # max_open_positions angezeigt (Sicherheitsvorfall 2026-07-31).
    config = get_user_live_config(user_id)
    with get_session() as session:
        open_trades = session.execute(text("""
            SELECT ticker, direction, instrument_type,
                   entry_price, stop_loss, take_profit,
                   quantity, capital_used, rule_score,
                   trailing_sl_active, trailing_sl_price,
                   time_exit_grace_used, time_exit_grace_deadline,
                   created_at, mode, broker, status_detail, sector
            FROM trades WHERE status = 'OPEN' AND user_id = :user_id
            ORDER BY created_at DESC
        """), {"user_id": user_id}).fetchall()

        # Realisierter P&L – NUR dieses Nutzers eigene geschlossene Trades
        # (Fix 2026-07-31: vorher ungefiltert über ALLE Nutzer/Daniels Konto).
        realized_pnl = float(
            session.execute(text("""
                SELECT COALESCE(SUM(pnl_usd), 0)
                FROM trades
                WHERE user_id = :user_id AND status IN (
                    'CLOSED_SL','CLOSED_TP',
                    'CLOSED_TRAILING_SL','CLOSED_TIME_EXIT',
                    'CLOSED_MANUAL')
            """), {"user_id": user_id}).scalar() or 0)

        # Tages-Trades – über den existierenden Helper statt eigener Raw-SQL,
        # damit die Definition ("heute erstellte Trades, OPEN + CLOSED")
        # exakt mit main.py/dashboard.py übereinstimmt. user_id durchreichen
        # (Fix 2026-07-31: Helper unterstützt es längst, wurde hier aber nie
        # übergeben – zählte bisher Daniels Trades für jeden Nutzer mit).
        daily_trades = get_daily_trade_count(session, user_id)

    # Cash/Marktwert/unrealisierter G&V kommen direkt von Alpaca (Broker-
    # Wahrheit, siehe get_alpaca_account_snapshot) statt wie bisher nur den
    # blendeten equity-Wert über yfinance nachzurechnen – Frontend trennt
    # damit "verfügbares Kapital" von "gebunden in Positionen" (siehe
    # Uebersicht.tsx). Fallback auf get_portfolio_value() falls Alpaca gerade
    # nicht erreichbar ist (z.B. Wartungsfenster) – lieber ein etwas
    # ungenauerer Wert als ein kompletter Ausfall der Übersicht.
    # Fix 2026-07-31: beide OHNE user_id fielen auf die globalen .env-Keys
    # zurück = IMMER Daniels echtes Live-Konto, unabhängig davon wer
    # eingeloggt war (siehe get_alpaca_account_snapshot/get_portfolio_value
    # in broker.py – unterstützen user_id längst, wurden hier aber nie damit
    # aufgerufen).
    alpaca_account = get_alpaca_account_snapshot(user_id)
    portfolio_value = alpaca_account["equity"] if alpaca_account else get_portfolio_value(user_id)
    cash = alpaca_account["cash"] if alpaca_account else None
    long_market_value = alpaca_account["long_market_value"] if alpaca_account else None
    unrealized_pnl_total = alpaca_account["unrealized_pl"] if alpaca_account else None

    market_regime = get_market_regime()

    try:
        vix = float(yf.Ticker("^VIX").fast_info.get("lastPrice", 0))
    except Exception:
        vix = 0

    # Time-Exit-Countdown (siehe Feature "Time-Exit-Anzeige Uebersicht"):
    # dieselbe count_trading_days()-Funktion und dieselben Schwellenwerte wie
    # broker.monitor_open_positions() – ohne aktiven Trailing-SL greift
    # MAX_HOLDING_DAYS, mit aktivem Trailing-SL die harte Obergrenze
    # MAX_HOLDING_DAYS * MAX_HOLDING_DAYS_TRAILING_MULTIPLIER (Time-Exit dort
    # ausgesetzt, siehe broker.py). Nur Alpaca – Saxo kennt kein Time-Exit.
    # Fix 2026-08-05 (UPS-Befund): berücksichtigt jetzt auch die Time-Exit-
    # Schutzfrist (time_exit_grace_used/-deadline, siehe broker.py Zeilen
    # 1289-1300) – vorher fehlte dieser dritte Zustand hier komplett, wodurch
    # Positionen in einer noch laufenden, gültigen Schutzfrist fälschlich als
    # "überfällig" (negative time_exit_days_remaining) auftauchten.
    max_holding_days = int(config.get("MAX_HOLDING_DAYS", 5))
    max_holding_days_trailing_multiplier = int(config.get("MAX_HOLDING_DAYS_TRAILING_MULTIPLIER", 2))

    # Aktueller Kurs + unrealisierter G/V pro offener Position (fürs
    # Frontend, siehe Positions-Karten in Uebersicht.tsx) – analog zu
    # broker.get_portfolio_value()s Unrealisiert-Schleife, hier zusätzlich
    # pro Ticker statt nur aggregiert zurückgegeben.
    open_trades_out = []
    for t in open_trades:
        row = dict(t._mapping)
        # Fix 2026-07-31: entry_price ist None, solange die Kauf-Order noch
        # WAITING_FILL ist (siehe broker.place_trade) - unrealized_pnl lässt
        # sich dafür noch nicht berechnen, Frontend zeigt stattdessen "wartet
        # auf Fill" an (siehe status_detail, ebenfalls in row enthalten).
        if row["entry_price"] is None:
            row["current_price"] = None
            row["unrealized_pnl"] = None
            row["unrealized_pnl_pct"] = None
        else:
            try:
                current_price = float(yf.Ticker(row["ticker"]).fast_info.get("lastPrice", row["entry_price"]))
            except Exception:
                current_price = row["entry_price"]
            row["current_price"] = current_price
            row["unrealized_pnl"] = round((current_price - row["entry_price"]) * row["quantity"], 2)
            row["unrealized_pnl_pct"] = round((current_price - row["entry_price"]) / row["entry_price"] * 100, 2) if row["entry_price"] else 0

        today = datetime.now().date()
        days_held = count_trading_days(row["created_at"].date(), today)

        # Schutzfrist gilt nur, solange kein Trailing-SL aktiv ist (der
        # übernimmt in broker.py Vorrang vor der Grace-Prüfung) UND die
        # Deadline noch nicht erreicht ist (an/nach der Deadline greift der
        # reguläre Time-Exit, siehe broker.py Zeile 1295).
        grace_active = bool(
            not row["trailing_sl_active"]
            and row["time_exit_grace_used"]
            and row["time_exit_grace_deadline"] is not None
            and today < row["time_exit_grace_deadline"]
        )

        if row["trailing_sl_active"]:
            limit = max_holding_days * max_holding_days_trailing_multiplier
            row["time_exit_days_remaining"] = limit - days_held
        elif grace_active:
            # Tage bis zur Schutzfrist-Deadline statt bis zur ursprünglichen
            # MAX_HOLDING_DAYS-Grenze (die ist für diese Position bereits
            # überholt, siehe broker.py Zeile 1289 elif-Zweig).
            row["time_exit_days_remaining"] = count_trading_days(today, row["time_exit_grace_deadline"]) - 1
        else:
            row["time_exit_days_remaining"] = max_holding_days - days_held

        row["time_exit_grace_active"] = grace_active
        row["time_exit_grace_deadline"] = (
            row["time_exit_grace_deadline"].isoformat() if row["time_exit_grace_deadline"] else None
        )

        open_trades_out.append(row)

    return {
        "portfolio_value": portfolio_value,
        "cash": cash,
        "long_market_value": long_market_value,
        "unrealized_pnl": unrealized_pnl_total,
        "realized_pnl": realized_pnl,
        "open_trades": open_trades_out,
        "daily_trades": daily_trades,
        "max_trades_per_day": int(config.get("MAX_TRADES_PER_DAY", 5)),
        "max_open_positions": int(config.get("MAX_OPEN_POSITIONS", 5)),
        "vix": vix,
        "market_regime": market_regime,
        # get_trade_mode_for_user() statt des globalen TRADING_MODE-Konstante
        # (Fix 2026-07-31): ein per Connect-Flow im Paper-Modus verbundener
        # Nutzer soll "PAPER" sehen, nicht Daniels globales LIVE (siehe
        # database.get_trade_mode_for_user-Docstring).
        "trading_mode": get_trade_mode_for_user(user_id),
        # Pause-Sichtbarkeit (AUFGABE 2, 2026-08-06): fasst Tagesverlustlimit
        # UND Verlustserie-Cooldown zusammen (siehe broker.get_pause_status) –
        # vorher hatte das Frontend KEINE Möglichkeit, einen aktiven
        # bot_paused-Zustand überhaupt anzuzeigen.
        "pause_status": get_pause_status(user_id),
    }


@protected.get("/api/performance")
def get_performance(user_id: int = Depends(get_current_user_id)):
    """
    Echte Pro-Nutzer-Isolation (Fix 2026-08-04) – daily_log hat jetzt eine
    user_id-Spalte (siehe database.DailyLog-Docstring/
    _migrate_daily_log_user_id_column). Vorher (Fix 2026-07-31) war dieser
    Endpoint für alle außer DEFAULT_USER_ID mit 403 gesperrt, weil log_date
    global eindeutig war (ein Snapshot/Tag für alle Nutzer) – jeder Nutzer
    sieht jetzt ausschließlich seine eigenen Snapshots/Stats.
    """
    with get_session() as session:
        snapshots = session.execute(text("""
            SELECT log_date, portfolio_value, daily_pnl,
                   trades_count
            FROM daily_log
            WHERE user_id = :user_id
            ORDER BY log_date ASC
            LIMIT 90
        """), {"user_id": user_id}).fetchall()

        stats = session.execute(text("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                ROUND(AVG(pnl_usd)::numeric, 2) as avg_pnl,
                ROUND(MAX(pnl_usd)::numeric, 2) as best_trade,
                ROUND(MIN(pnl_usd)::numeric, 2) as worst_trade
            FROM trades
            WHERE user_id = :user_id AND status IN (
                'CLOSED_SL','CLOSED_TP',
                'CLOSED_TRAILING_SL','CLOSED_TIME_EXIT',
                'CLOSED_MANUAL')
        """), {"user_id": user_id}).fetchone()

        return {
            "snapshots": [dict(s._mapping) for s in snapshots],
            "stats": dict(stats._mapping) if stats else {},
        }


def _parse_json_field(raw, default):
    """Deserialisiert ein Text-Column (llm_risks/score_breakdown, siehe database.py
    Trade.get_llm_risks/get_score_breakdown), damit die API sie als echtes
    JSON-Array/-Objekt statt als doppelt-kodierten String zurückgibt."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


@protected.get("/api/trades/history")
def get_trade_history(limit: int = 50, user_id: int = Depends(get_current_user_id)):
    """
    Alle Positionen (offen + geschlossen) für die Handelshistorie-Ansicht,
    NUR des eingeloggten Nutzers (Fix 2026-07-31 – vorher komplett
    ungefiltert, jeder Nutzer sah ALLE Trades aller Nutzer/Daniels
    Live-Konto). Bis 2026-07-28 wurden hier nur geschlossene Trades
    zurückgegeben (WHERE status NOT IN ('OPEN', ...)) – offene Positionen
    fehlten trotz längst bekanntem Kaufdatum/-preis komplett. Sortierung
    jetzt nach created_at (Kaufzeitpunkt) statt closed_at, damit gerade
    gekaufte offene Positionen sofort oben erscheinen, unabhängig vom Status.

    pnl_usd/pnl_pct bleiben unverändert NULL für OPEN-Trades (das ist die
    Definition "realisierter P&L", siehe database.get_total_pnl/get_daily_pnl
    – hier NICHT angefasst). Für OPEN-Trades kommen current_price/
    unrealized_pnl/unrealized_pnl_pct als zusätzliche, separate Felder dazu
    (analog zu get_overview()'s open_trades-Anreicherung).
    """
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                ticker, direction, quantity,
                entry_price, exit_price,
                stop_loss, take_profit,
                capital_used, pnl_usd, pnl_pct,
                rule_score, status, status_detail,
                broker, mode, sector,
                created_at, closed_at,
                llm_sentiment, llm_summary, llm_risks, score_breakdown,
                CASE
                    WHEN status = 'OPEN' AND status_detail = 'WAITING_FILL' THEN 'Kauf wartet auf Fill'
                    WHEN status = 'OPEN' THEN 'Offen'
                    WHEN status = 'CLOSED_SL' THEN 'Stop Loss'
                    WHEN status = 'CLOSED_TP' THEN 'Take Profit'
                    WHEN status = 'CLOSED_TRAILING_SL' THEN 'Trailing Stop'
                    WHEN status = 'CLOSED_TIME_EXIT' THEN 'Time Exit (5 Tage)'
                    WHEN status = 'CLOSED_MANUAL' THEN 'Manuell'
                    WHEN status = 'FAILED_ENTRY' THEN 'Kauf nie gefüllt'
                    ELSE status
                END as exit_grund
            FROM trades
            WHERE status != 'PENDING' AND user_id = :user_id
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit, "user_id": user_id}).fetchall()

        result = []
        for r in rows:
            row = dict(r._mapping)
            row["llm_risks"] = _parse_json_field(row.get("llm_risks"), default=[])
            row["score_breakdown"] = _parse_json_field(row.get("score_breakdown"), default={})
            if row["status"] == "OPEN" and row["entry_price"] is not None:
                try:
                    current_price = float(yf.Ticker(row["ticker"]).fast_info.get("lastPrice", row["entry_price"]))
                except Exception:
                    current_price = row["entry_price"]
                row["current_price"] = current_price
                row["unrealized_pnl"] = round((current_price - row["entry_price"]) * row["quantity"], 2)
                row["unrealized_pnl_pct"] = (
                    round((current_price - row["entry_price"]) / row["entry_price"] * 100, 2)
                    if row["entry_price"] else 0
                )
            else:
                # entry_price is None (Fix 2026-07-31): Kauf-Order noch
                # WAITING_FILL, siehe broker.place_trade - kein current_price/
                # unrealized_pnl berechenbar, bis der echte Fill nachgetragen ist.
                row["current_price"] = None
                row["unrealized_pnl"] = None
                row["unrealized_pnl_pct"] = None
            result.append(row)

        return result


@protected.get("/api/benchmark")
def get_benchmark(days: int = 30, user_id: int = Depends(get_current_user_id)):
    """30-Tage Bot-vs-Markt-Vergleich (siehe rule_engine/broker, Feature
    Benchmark-Vergleich vom 2026-07-25). Echte Pro-Nutzer-Isolation (Fix
    2026-08-04): get_bot_performance() ist jetzt user_id-scoped (siehe
    /api/performance-Fix, database.DailyLog hat eine user_id-Spalte) – jeder
    Nutzer sieht seine EIGENE Bot-Performance. Die Markt-Benchmarks (S&P 500/
    Nasdaq, get_benchmark_performance) bleiben bewusst global/marktweit,
    keine Nutzerbindung nötig oder sinnvoll."""
    from rule_engine import get_benchmark_performance
    from broker import get_bot_performance
    return {
        "bot": get_bot_performance(days=days, user_id=user_id),
        "benchmarks": get_benchmark_performance(days=days),
    }


@protected.get("/api/scan-log")
def get_scan_log(limit: int = 500, ticker: Optional[str] = None):
    """
    Bewusst NICHT auf user_id gescopt (Security-Review 2026-07-31): der
    Signal-Scan läuft zentral EINMAL pro Ticker/Slot für die gesamte
    Watchlist – scan_log enthält keine Kapital-/Positions-/Kontodaten
    irgendeines Nutzers, nur öffentlich am Markt beobachtbare Indikatoren
    (RSI/SMA/Volumen/Score) plus ob der GLOBALE Bot bei diesem Scan gekauft
    hat. Für jeden Nutzer identisch und nicht sensibel – im Unterschied zu
    /api/overview, /api/trades/history etc. war das nie Teil der Sicherheits-
    lücke, daher hier absichtlich keine Änderung.
    """
    query = """
        SELECT
            id,
            DATE(scan_time AT TIME ZONE 'America/New_York')
                as scan_date,
            slot_et, scan_time, ticker, score, approved,
            rsi_score, sma_score, volume_score,
            pe_score, de_score, rev_score,
            ko_reason, guardrail_reason,
            trade_executed, mode, market_regime, broker
        FROM scan_log
    """
    params = {"limit": min(limit, 5000)}
    if ticker:
        query += " WHERE ticker = :ticker"
        params["ticker"] = ticker.upper()
    query += " ORDER BY scan_time DESC LIMIT :limit"

    with get_session() as session:
        rows = session.execute(text(query), params).fetchall()

        days = {}
        for row in rows:
            d = str(row.scan_date)
            s = row.slot_et or "?"
            if d not in days:
                days[d] = {"date": d, "slots": {}}
            if s not in days[d]["slots"]:
                days[d]["slots"][s] = {
                    "slot": s, "tickers": [],
                    "total": 0, "above_threshold": 0,
                    "trades": 0, "avg_score": 0
                }
            slot = days[d]["slots"][s]
            slot["tickers"].append(dict(row._mapping))
            slot["total"] += 1
            if row.score and row.score >= 65:
                slot["above_threshold"] += 1
            if row.trade_executed:
                slot["trades"] += 1

        for day in days.values():
            for slot in day["slots"].values():
                scores = [t["score"] for t in slot["tickers"]
                         if t["score"] and t["score"] > 0]
                slot["avg_score"] = round(
                    sum(scores)/len(scores), 1) if scores else 0
            day["slots"] = list(day["slots"].values())

        return list(days.values())


@protected.get("/api/scan-log/stats")
def get_scan_log_stats():
    """Welche Filter haben in den letzten 30 Tagen wie oft geblockt (siehe
    Filter-Statistik im Scan-Historie-Tab). Bewusst nicht user_id-gescopt,
    gleicher Grund wie /api/scan-log oben (zentraler, nutzerunabhängiger
    Signal-Scan – keine Konto-/Positionsdaten)."""
    with get_session() as session:
        rows = session.execute(text("""
            SELECT
                CASE
                    WHEN ko_reason LIKE '%Blacklist%' THEN 'Blacklist'
                    WHEN ko_reason LIKE '%Fair Value%' THEN 'Fair Value'
                    WHEN ko_reason LIKE '%Earnings%' THEN 'Earnings'
                    WHEN guardrail_reason IS NOT NULL THEN 'Guardrail'
                    WHEN score < 65 THEN 'Score zu niedrig'
                    ELSE 'Sonstiges'
                END as grund,
                COUNT(*) as anzahl
            FROM scan_log
            WHERE scan_time >= NOW() - INTERVAL '30 days'
            GROUP BY grund
            ORDER BY anzahl DESC
        """)).fetchall()
        return [dict(r._mapping) for r in rows]


# Deutsche Beschreibungstexte für ALLE Pro-Nutzer-Guardrail-Keys aus
# DEFAULT_USER_CONFIG – identisch zu den entsprechenden Einträgen in
# database.DEFAULT_CONFIG (Daniels globale bot_config), damit ein Nicht-
# Owner dieselbe Beschriftung sieht wie Daniel für denselben Guardrail.
#
# BUGFIX 2026-08-08: hatte bis dahin nur 5 Einträge, DEFAULT_USER_CONFIG
# aber bereits 7 (seit Confirm-Tier Chunk 1, 2026-08-07) – jeder Nicht-Owner-
# Aufruf von GET /api/bot-config löste unten in der Dict-Comprehension einen
# unabgefangenen KeyError (-> 500) aus, sobald EXECUTION_MODE/PRICE_
# TOLERANCE_PCT durchlaufen wurden. Jetzt vollständig für alle 16 Keys aus
# DEFAULT_USER_CONFIG (siehe database.py für die volle Begründung, welche
# Keys das sind und warum).
USER_GUARDRAIL_DESCRIPTIONS = {
    "MAX_CAPITAL_TOTAL":                    "Gesamtkapital in USD",
    "MAX_CAPITAL_PER_TRADE":                "Max. Einsatz pro Trade",
    "MAX_OPEN_POSITIONS":                   "Max. offene Positionen",
    "MAX_TRADES_PER_DAY":                   "Max. Trades pro Tag",
    "DAILY_LOSS_LIMIT_PCT":                 "Tagesverlust-Limit %",
    "EXECUTION_MODE":                       "Trade-Ausführung (auto/confirm)",
    "PRICE_TOLERANCE_PCT":                  "Preistoleranz bei manueller Bestätigung %",
    "TRAILING_ACTIVATION_PCT":              "Trailing-Stop-Aktivierungsschwelle %",
    "MAX_CONSECUTIVE_LOSSES":               "Verlustserie-Cooldown: Anzahl Verluste in Folge",
    "COOLDOWN_HOURS_AFTER_LOSS_STREAK":     "Verlustserie-Cooldown: Pausendauer in Stunden",
    "MAX_HOLDING_DAYS":                     "Max. Haltedauer in Handelstagen",
    "MAX_HOLDING_DAYS_TRAILING_MULTIPLIER": "Harte Obergrenze bei aktivem Trailing-SL (× Max. Haltedauer)",
    "TIME_EXIT_GRACE_DAYS":                 "Schutzfrist für Gewinner ohne aktives Trailing (Handelstage)",
    "VOLATILE_SEGMENT_PCT":                 "Ziel-Anteil volatile Titel am Portfolio",
    "ATR_MULTIPLIER_SL":                    "ATR-Multiplikator für Trailing-Stop-Distanz",
    "ATR_MIN_SL_PCT":                       "Minimale Trailing-Stop-Distanz %",
    "ATR_MAX_SL_PCT":                       "Maximale Trailing-Stop-Distanz %",
}


@protected.get("/api/bot-config")
def get_bot_config_all(user_id: int = Depends(get_current_user_id)):
    """
    Echte Pro-Nutzer-Isolation (Fix 2026-08-04, erweitert 2026-08-08): Daniel
    (DEFAULT_USER_ID) sieht weiterhin unverändert sein komplettes globales
    Kontrollpanel (alle ~40 bot_config-Keys). Andere Nutzer sehen NUR ihre
    eigenen Guardrail-Keys aus user_bot_config (siehe DEFAULT_USER_CONFIG/
    UserBotConfig-Docstring in database.py – lazy geseedet über
    get_user_live_config). Die übrigen bot_config-Keys (MIN_SIGNAL_SCORE,
    SL/TP-Fallback%, ATR-Multiplikator TP, EARNINGS_BUFFER_DAYS,
    VIX_PAUSE_THRESHOLD, sowie System-Schalter wie MONITORING_INTERVAL_MIN/
    ACTIVE_BROKER) bleiben bewusst global und für Nicht-Owner unsichtbar/
    nicht editierbar, da sie entweder den gemeinsamen Signal-Scan betreffen
    (ein Score/SL/TP-Preis pro Ticker für alle Nutzer, siehe DEFAULT_USER_
    CONFIG-Docstring) oder reine Betreiber-Schalter ohne Kunden-Risikobezug
    sind – kein 403 mehr für die pro-Nutzer-fähigen Guardrails, aber auch
    kein Zugriff auf Daniels übrige globale Konfiguration.
    """
    if user_id == DEFAULT_USER_ID:
        with get_session() as session:
            rows = session.execute(text("""
                SELECT key, value, beschreibung
                FROM bot_config ORDER BY key
            """)).fetchall()
            return [dict(r._mapping) for r in rows]

    cfg = get_user_live_config(user_id)
    return [
        {"key": key, "value": str(cfg.get(key, default)), "beschreibung": USER_GUARDRAIL_DESCRIPTIONS[key]}
        for key, (_cast, default) in DEFAULT_USER_CONFIG.items()
    ]


@protected.put("/api/bot-config/{key}")
def update_bot_config(key: str, body: dict, user_id: int = Depends(get_current_user_id)):
    """
    Echte Pro-Nutzer-Isolation (Fix 2026-08-04): Daniel (DEFAULT_USER_ID)
    schreibt weiterhin unverändert in die globale bot_config-Zeile, OHNE
    Grenzprüfung (Owner behält volle, ungeprüfte Kontrolle wie schon vor
    dieser Aufgabe). Andere Nutzer dürfen NUR die Guardrail-Keys aus
    DEFAULT_USER_CONFIG setzen (die einzigen, die pro Nutzer in
    user_bot_config überhaupt existieren, siehe get_bot_config_all oben) –
    jeder andere Key bleibt mit 403 gesperrt, da er eine globale, geteilte
    Einstellung adressiert und ein Schreibzugriff dort exakt die
    Sicherheitslücke aus dem a19605f-Vorfall (2026-07-31) wiederherstellen
    würde.

    Validierungsgrenzen (Aufgabe Punkt 4, 2026-08-08): NUR für den Nicht-
    Owner-Selbstbedienungs-Pfad, siehe USER_CONFIG_BOUNDS/USER_CONFIG_ENUM_
    BOUNDS in database.py für die Begründung der einzelnen Grenzen. Verhindert
    u.a., dass ein Kunde sein eigenes Tagesverlustlimit auf 0%/100% oder
    MAX_OPEN_POSITIONS auf 0/negativ setzt – vorher gab es hier gar keine
    Prüfung (weder Typ noch Wertebereich).
    """
    if user_id == DEFAULT_USER_ID:
        with get_session() as session:
            set_bot_config(session, key, body.get("value"))
            session.commit()
        return {"ok": True}

    if key not in DEFAULT_USER_CONFIG:
        raise HTTPException(status_code=403, detail="Dieser Parameter ist nicht pro Nutzer einstellbar")

    raw_value = body.get("value")
    if key in USER_CONFIG_ENUM_BOUNDS:
        if str(raw_value) not in USER_CONFIG_ENUM_BOUNDS[key]:
            raise HTTPException(
                status_code=400,
                detail=f"Ungültiger Wert für {key} – erlaubt: {', '.join(sorted(USER_CONFIG_ENUM_BOUNDS[key]))}",
            )
    elif key in USER_CONFIG_BOUNDS:
        cast, _default = DEFAULT_USER_CONFIG[key]
        try:
            numeric_value = cast(raw_value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} muss eine Zahl sein")
        min_value, max_value = USER_CONFIG_BOUNDS[key]
        if not (min_value <= numeric_value <= max_value):
            raise HTTPException(
                status_code=400,
                detail=f"{key} muss zwischen {min_value} und {max_value} liegen (übergeben: {numeric_value})",
            )
        # ATR_MIN_SL_PCT/ATR_MAX_SL_PCT: Kreuzvalidierung gegen den jeweils
        # ANDEREN, bereits gespeicherten Wert dieses Nutzers – ein einzelner
        # PUT sieht immer nur EINEN Key, ein isoliert plausibler Wert könnte
        # trotzdem min > max ergeben (z.B. ATR_MIN_SL_PCT=0.2 während
        # ATR_MAX_SL_PCT beim selben Nutzer noch auf dem Default 0.08 steht).
        if key in ("ATR_MIN_SL_PCT", "ATR_MAX_SL_PCT"):
            other_key = "ATR_MAX_SL_PCT" if key == "ATR_MIN_SL_PCT" else "ATR_MIN_SL_PCT"
            other_value = float(get_user_live_config(user_id).get(other_key))
            new_min = numeric_value if key == "ATR_MIN_SL_PCT" else other_value
            new_max = other_value if key == "ATR_MIN_SL_PCT" else numeric_value
            if new_min >= new_max:
                raise HTTPException(
                    status_code=400,
                    detail=f"ATR_MIN_SL_PCT ({new_min}) muss kleiner als ATR_MAX_SL_PCT ({new_max}) sein",
                )

    with get_session() as session:
        set_user_bot_config(session, user_id, key, raw_value)
        session.commit()
    return {"ok": True}


@protected.get("/api/capital-allocations")
def get_capital_allocations_endpoint(user_id: int = Depends(get_current_user_id)):
    """
    Prozent-Aufteilung des echten Gesamtkapitals EINES Nutzers (Aufgabe
    "Kapital-Einstellungen Prozent-Umbau", ab 2026-08-08 pro Nutzer statt
    Owner-only, siehe broker.get_effective_max_capital_total_bot-Docstring
    für die volle Begründung). Liest/schreibt ausschließlich die eigene
    user_id (get_current_user_id kommt aus dem JWT-Cookie, nicht aus dem
    Request-Body – ein Nutzer kann hier strukturell nicht die Kapital-
    aufteilung eines anderen Nutzers lesen/ändern). Liefert zusätzlich das
    bereits berechnete effective_max_capital_total_bot (Gesamtkapital ×
    Bot-Anteil), damit das Frontend nicht selbst nachrechnen muss.
    """
    real_snapshot = get_alpaca_account_snapshot(user_id)
    real_total = real_snapshot["equity"] if real_snapshot else None
    allocations = get_or_seed_capital_allocations(user_id, real_total)
    return {
        "allocations": allocations,
        "effective_max_capital_total_bot": get_effective_max_capital_total_bot(user_id),
        "real_total_capital": real_total,
    }


@protected.put("/api/capital-allocations")
def update_capital_allocations(body: dict, user_id: int = Depends(get_current_user_id)):
    """Schreibt ausschließlich die Kapitalaufteilung des eingeloggten Nutzers
    (user_id aus dem JWT-Cookie, siehe get_capital_allocations_endpoint oben).
    Erwartet {"allocations": {"bot": X, "active_trading": Y}} – alle
    bekannten Kategorien (CAPITAL_ALLOCATION_CATEGORIES), Summe=100."""
    allocations = body.get("allocations")
    if not isinstance(allocations, dict) or not allocations:
        raise HTTPException(status_code=400, detail="allocations fehlt")
    unknown = set(allocations.keys()) - set(CAPITAL_ALLOCATION_CATEGORIES)
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unbekannte Kategorie(n): {unknown}")
    try:
        values = [float(v) for v in allocations.values()]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Prozentwerte müssen Zahlen sein")
    if any(v < 0 for v in values):
        raise HTTPException(status_code=400, detail="Prozentwerte dürfen nicht negativ sein")
    if abs(sum(values) - 100) > 0.5:
        raise HTTPException(status_code=400, detail=f"Summe muss 100 ergeben (ist {sum(values):.1f})")
    with get_session() as session:
        set_capital_allocations(session, user_id, {k: float(v) for k, v in allocations.items()})
    return {"ok": True}


BOT_CONFIG_PRESETS = {
    "konservativ": {
        "MAX_CAPITAL_PER_TRADE": "30",
        "MAX_OPEN_POSITIONS": "3",
        "ATR_MULTIPLIER_SL": "1.0",
        "ATR_MULTIPLIER_TP": "2.0",
        "MAX_HOLDING_DAYS": "3",
        "VOLATILE_SEGMENT_PCT": "0.0",
    },
    "ausgewogen": {
        "MAX_CAPITAL_PER_TRADE": "50",
        "MAX_OPEN_POSITIONS": "5",
        "ATR_MULTIPLIER_SL": "1.5",
        "ATR_MULTIPLIER_TP": "3.0",
        "MAX_HOLDING_DAYS": "5",
        "VOLATILE_SEGMENT_PCT": "0.33",
    },
    "aggressiv": {
        "MAX_CAPITAL_PER_TRADE": "100",
        "MAX_OPEN_POSITIONS": "8",
        "ATR_MULTIPLIER_SL": "2.0",
        "ATR_MULTIPLIER_TP": "4.0",
        "MAX_HOLDING_DAYS": "7",
        "VOLATILE_SEGMENT_PCT": "0.5",
    },
}


@protected.post("/api/bot-config/preset")
def apply_bot_config_preset(body: dict, user_id: int = Depends(get_current_user_id)):
    """Identisches Preset-System wie portfolio_os (POST /api/bot-config/preset,
    siehe Onboarding-Feature vom 2026-07-25) – hier direkt gegen die
    gemeinsame DB statt über den trading_bot_connector (dieser Service läuft
    ja im selben Repo/venv wie trading_bot).

    Pro Nutzer statt Owner-only (2026-08-08, Aufgabe "Presets/Kapital-
    aufteilung/Guardrails pro Nutzer"): Daniel (DEFAULT_USER_ID) schreibt
    unverändert in die globale bot_config (identisches Verhalten wie vor
    dieser Aufgabe). Für jeden anderen Nutzer wird NUR die Teilmenge der
    Preset-Werte übernommen, die tatsächlich pro Nutzer existiert (siehe
    DEFAULT_USER_CONFIG in database.py) – ATR_MULTIPLIER_TP ist bewusst
    GLOBAL (gemeinsamer Signal-Scan, siehe dortige Docstring) und wird für
    Nicht-Owner ausgelassen statt fälschlich in die globale bot_config zu
    schreiben (das würde Daniels/aller anderen Nutzer Entry-Take-Profit
    verändern – exakt die Art von Cross-Tenant-Schreibzugriff, die diese
    Aufgabe verhindern soll)."""
    preset = body.get("preset")
    if preset not in BOT_CONFIG_PRESETS:
        raise HTTPException(400, "Unbekanntes Preset")

    if user_id == DEFAULT_USER_ID:
        with get_session() as session:
            for key, value in BOT_CONFIG_PRESETS[preset].items():
                set_bot_config(session, key, value)
            session.commit()
        return {"message": f"Preset '{preset}' angewendet", "settings": BOT_CONFIG_PRESETS[preset]}

    applied = {k: v for k, v in BOT_CONFIG_PRESETS[preset].items() if k in DEFAULT_USER_CONFIG}
    skipped = [k for k in BOT_CONFIG_PRESETS[preset] if k not in DEFAULT_USER_CONFIG]
    with get_session() as session:
        for key, value in applied.items():
            set_user_bot_config(session, user_id, key, value)
        session.commit()
    return {
        "message": f"Preset '{preset}' angewendet" + (f" ({', '.join(skipped)} zentral verwaltet, unverändert)" if skipped else ""),
        "settings": applied,
    }


@protected.get("/api/learning-proposals")
def get_pending_learning_proposals(user_id: int = Depends(get_current_user_id)):
    """Offene (status='pending') KI-Lernvorschläge aus dem wöchentlichen
    Lernzyklus (siehe backlook.py: analyze_optimal_threshold/analyze_ticker_performance).
    Jeder Eintrag bekommt seinen Index in der ungefilterten Gesamtliste zurück
    (statt des Index in dieser gefilterten Antwort) – accept/reject adressieren
    darüber den richtigen Eintrag, auch wenn dazwischen bereits andere
    Vorschläge akzeptiert/abgelehnt wurden. Owner-only (Fix 2026-07-31, siehe
    require_owner-Docstring) – betrifft ausschließlich Daniels globale
    bot_config (MIN_SIGNAL_SCORE etc.)."""
    require_owner(user_id)
    with get_session() as session:
        all_proposals = get_learning_proposals(session)
        return [
            {**p, "index": i}
            for i, p in enumerate(all_proposals)
            if p.get("status") == "pending"
        ]


@protected.post("/api/learning-proposals/accept")
def accept_learning_proposal(body: dict, user_id: int = Depends(get_current_user_id)):
    """Owner-only (Fix 2026-07-31, siehe require_owner-Docstring) – setzt
    ggf. MIN_SIGNAL_SCORE in Daniels globaler bot_config."""
    require_owner(user_id)
    idx = body.get("index")
    with get_session() as session:
        proposals = get_learning_proposals(session)
        if idx is None or not (0 <= idx < len(proposals)):
            raise HTTPException(400, "Ungültiger Index")
        proposal = proposals[idx]
        proposal["status"] = "accepted"
        if proposal["typ"] == "threshold":
            set_bot_config(session, "MIN_SIGNAL_SCORE", str(proposal["data"]["empfohlen"]))
        set_learning_proposals(session, proposals)
        session.commit()
    return {"ok": True}


@protected.post("/api/learning-proposals/reject")
def reject_learning_proposal(body: dict, user_id: int = Depends(get_current_user_id)):
    """Owner-only (Fix 2026-07-31, siehe require_owner-Docstring)."""
    require_owner(user_id)
    idx = body.get("index")
    with get_session() as session:
        proposals = get_learning_proposals(session)
        if idx is None or not (0 <= idx < len(proposals)):
            raise HTTPException(400, "Ungültiger Index")
        proposals[idx]["status"] = "rejected"
        set_learning_proposals(session, proposals)
        session.commit()
    return {"ok": True}


@protected.get("/api/entry-slots")
def get_entry_slots(user_id: int = Depends(get_current_user_id)):
    """Owner-only (Fix 2026-07-31, siehe require_owner-Docstring) –
    entry_time_slots ist Daniels globaler Zeitplan, kein Multi-Tenant-Konzept."""
    require_owner(user_id)
    with get_session() as session:
        rows = session.execute(text("""
            SELECT id, stunde_et, minute_et, gewichtung,
                   max_trades_per_slot, aktiv,
                   avg_pnl, trefferquote, anzahl_trades, quelle
            FROM entry_time_slots
            ORDER BY stunde_et, minute_et
        """)).fetchall()
        return [dict(r._mapping) for r in rows]


@protected.put("/api/entry-slots/{slot_id}")
def update_entry_slot(slot_id: int, body: dict, user_id: int = Depends(get_current_user_id)):
    """Aktiv-Toggle / Gewichtung ändern (siehe Einstellungen-Tab). Owner-only
    (Fix 2026-07-31, siehe require_owner-Docstring)."""
    require_owner(user_id)
    fields = []
    params = {"id": slot_id}
    if "aktiv" in body:
        fields.append("aktiv = :aktiv")
        params["aktiv"] = bool(body["aktiv"])
    if "gewichtung" in body:
        fields.append("gewichtung = :gewichtung")
        params["gewichtung"] = float(body["gewichtung"])
    if not fields:
        raise HTTPException(400, "Keine Felder zum Aktualisieren übergeben")
    with get_session() as session:
        session.execute(
            text(f"UPDATE entry_time_slots SET {', '.join(fields)} WHERE id = :id"),
            params,
        )
        session.commit()
    return {"ok": True}


@protected.get("/api/settings/entry-learning-mode")
def get_entry_learning_mode(user_id: int = Depends(get_current_user_id)):
    """Owner-only (Fix 2026-07-31, siehe require_owner-Docstring) –
    ENTRY_LEARNING_MODE ist ein globaler bot_config-Key."""
    require_owner(user_id)
    config = get_live_config()
    return {"lernmodus": str(config.get("ENTRY_LEARNING_MODE", "false")).lower() == "true"}


@protected.put("/api/settings/entry-learning-mode")
def update_entry_learning_mode(body: dict, user_id: int = Depends(get_current_user_id)):
    """Owner-only (Fix 2026-07-31, siehe require_owner-Docstring)."""
    require_owner(user_id)
    with get_session() as session:
        set_bot_config(session, "ENTRY_LEARNING_MODE", "true" if body.get("lernmodus") else "false")
        session.commit()
    return {"ok": True}


app.include_router(protected)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8504)
