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
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Request, Cookie
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from jose import JWTError, jwt
from typing import Optional

from database import (
    get_session, get_daily_trade_count, set_bot_config,
    get_learning_proposals, set_learning_proposals,
)
from config import get_live_config, TRADING_MODE
from broker import get_portfolio_value
from rule_engine import get_market_regime
import yfinance as yf

JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not JWT_SECRET_KEY:
    raise RuntimeError("JWT_SECRET_KEY nicht gesetzt! (gleicher Wert wie portfolio_os/.env nötig)")
JWT_ALGORITHM = "HS256"


def require_auth(request: Request, token: Optional[str] = Cookie(default=None, alias="token")):
    """
    Prüft dasselbe JWT-Cookie wie portfolio_os (siehe get_current_user dort) –
    nur Signatur + Ablaufzeit, kein DB-Lookup (dieser Service kennt pos_users
    nicht). Bearer-Header als Fallback wie im Portfolio-OS-Pendant.
    """
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")
    try:
        jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Nicht autorisiert")


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
protected = APIRouter(dependencies=[Depends(require_auth)])


@app.get("/api/health")
def health():
    return {"status": "ok"}


@protected.get("/api/overview")
def get_overview():
    config = get_live_config()
    with get_session() as session:
        open_trades = session.execute(text("""
            SELECT ticker, direction, instrument_type,
                   entry_price, stop_loss, take_profit,
                   quantity, capital_used, rule_score,
                   trailing_sl_active, trailing_sl_price,
                   created_at, mode, broker
            FROM trades WHERE status = 'OPEN'
            ORDER BY created_at DESC
        """)).fetchall()

        # Realisierter P&L
        realized_pnl = float(
            session.execute(text("""
                SELECT COALESCE(SUM(pnl_usd), 0)
                FROM trades
                WHERE status IN (
                    'CLOSED_SL','CLOSED_TP',
                    'CLOSED_TRAILING_SL','CLOSED_TIME_EXIT',
                    'CLOSED_MANUAL')
            """)).scalar() or 0)

        # Tages-Trades – über den existierenden Helper statt eigener Raw-SQL,
        # damit die Definition ("heute erstellte Trades, OPEN + CLOSED")
        # exakt mit main.py/dashboard.py übereinstimmt.
        daily_trades = get_daily_trade_count(session)

    # Portfolio-Wert & Marktregime werden live berechnet (broker.py /
    # rule_engine.py) statt aus bot_state gelesen – dort werden diese Werte
    # nirgends geschrieben, ein bot_state-Read hätte hier immer nur den
    # Fallback-Wert geliefert.
    portfolio_value = get_portfolio_value()
    market_regime = get_market_regime()

    try:
        vix = float(yf.Ticker("^VIX").fast_info.get("lastPrice", 0))
    except Exception:
        vix = 0

    # Aktueller Kurs + unrealisierter G/V pro offener Position (fürs
    # Frontend, siehe Positions-Karten in Uebersicht.tsx) – analog zu
    # broker.get_portfolio_value()s Unrealisiert-Schleife, hier zusätzlich
    # pro Ticker statt nur aggregiert zurückgegeben.
    open_trades_out = []
    for t in open_trades:
        row = dict(t._mapping)
        try:
            current_price = float(yf.Ticker(row["ticker"]).fast_info.get("lastPrice", row["entry_price"]))
        except Exception:
            current_price = row["entry_price"]
        row["current_price"] = current_price
        row["unrealized_pnl"] = round((current_price - row["entry_price"]) * row["quantity"], 2)
        row["unrealized_pnl_pct"] = round((current_price - row["entry_price"]) / row["entry_price"] * 100, 2) if row["entry_price"] else 0
        open_trades_out.append(row)

    return {
        "portfolio_value": portfolio_value,
        "realized_pnl": realized_pnl,
        "open_trades": open_trades_out,
        "daily_trades": daily_trades,
        "max_trades_per_day": int(config.get("MAX_TRADES_PER_DAY", 5)),
        "max_open_positions": int(config.get("MAX_OPEN_POSITIONS", 5)),
        "vix": vix,
        "market_regime": market_regime,
        "trading_mode": TRADING_MODE,
    }


@protected.get("/api/performance")
def get_performance():
    with get_session() as session:
        snapshots = session.execute(text("""
            SELECT log_date, portfolio_value, daily_pnl,
                   trades_count
            FROM daily_log
            ORDER BY log_date ASC
            LIMIT 90
        """)).fetchall()

        stats = session.execute(text("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_usd <= 0 THEN 1 ELSE 0 END) as losses,
                ROUND(AVG(pnl_usd)::numeric, 2) as avg_pnl,
                ROUND(MAX(pnl_usd)::numeric, 2) as best_trade,
                ROUND(MIN(pnl_usd)::numeric, 2) as worst_trade
            FROM trades
            WHERE status IN (
                'CLOSED_SL','CLOSED_TP',
                'CLOSED_TRAILING_SL','CLOSED_TIME_EXIT',
                'CLOSED_MANUAL')
        """)).fetchone()

        return {
            "snapshots": [dict(s._mapping) for s in snapshots],
            "stats": dict(stats._mapping) if stats else {},
        }


@protected.get("/api/benchmark")
def get_benchmark(days: int = 30):
    """30-Tage Bot-vs-Markt-Vergleich (siehe rule_engine/broker, Feature
    Benchmark-Vergleich vom 2026-07-25)."""
    from rule_engine import get_benchmark_performance
    from broker import get_bot_performance
    return {
        "bot": get_bot_performance(days=days),
        "benchmarks": get_benchmark_performance(days=days),
    }


@protected.get("/api/scan-log")
def get_scan_log(limit: int = 500, ticker: Optional[str] = None):
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
    Filter-Statistik im Scan-Historie-Tab)."""
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


@protected.get("/api/bot-config")
def get_bot_config_all():
    with get_session() as session:
        rows = session.execute(text("""
            SELECT key, value, beschreibung
            FROM bot_config ORDER BY key
        """)).fetchall()
        return [dict(r._mapping) for r in rows]


@protected.put("/api/bot-config/{key}")
def update_bot_config(key: str, body: dict):
    with get_session() as session:
        set_bot_config(session, key, body.get("value"))
        session.commit()
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
def apply_bot_config_preset(body: dict):
    """Identisches Preset-System wie portfolio_os (POST /api/bot-config/preset,
    siehe Onboarding-Feature vom 2026-07-25) – hier direkt gegen die
    gemeinsame DB statt über den trading_bot_connector (dieser Service läuft
    ja im selben Repo/venv wie trading_bot)."""
    preset = body.get("preset")
    if preset not in BOT_CONFIG_PRESETS:
        raise HTTPException(400, "Unbekanntes Preset")
    with get_session() as session:
        for key, value in BOT_CONFIG_PRESETS[preset].items():
            set_bot_config(session, key, value)
        session.commit()
    return {"message": f"Preset '{preset}' angewendet", "settings": BOT_CONFIG_PRESETS[preset]}


@protected.get("/api/learning-proposals")
def get_pending_learning_proposals():
    """Offene (status='pending') KI-Lernvorschläge aus dem wöchentlichen
    Lernzyklus (siehe backlook.py: analyze_optimal_threshold/analyze_ticker_performance).
    Jeder Eintrag bekommt seinen Index in der ungefilterten Gesamtliste zurück
    (statt des Index in dieser gefilterten Antwort) – accept/reject adressieren
    darüber den richtigen Eintrag, auch wenn dazwischen bereits andere
    Vorschläge akzeptiert/abgelehnt wurden."""
    with get_session() as session:
        all_proposals = get_learning_proposals(session)
        return [
            {**p, "index": i}
            for i, p in enumerate(all_proposals)
            if p.get("status") == "pending"
        ]


@protected.post("/api/learning-proposals/accept")
def accept_learning_proposal(body: dict):
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
def reject_learning_proposal(body: dict):
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
def get_entry_slots():
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
def update_entry_slot(slot_id: int, body: dict):
    """Aktiv-Toggle / Gewichtung ändern (siehe Einstellungen-Tab)."""
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
def get_entry_learning_mode():
    config = get_live_config()
    return {"lernmodus": str(config.get("ENTRY_LEARNING_MODE", "false")).lower() == "true"}


@protected.put("/api/settings/entry-learning-mode")
def update_entry_learning_mode(body: dict):
    with get_session() as session:
        set_bot_config(session, "ENTRY_LEARNING_MODE", "true" if body.get("lernmodus") else "false")
        session.commit()
    return {"ok": True}


app.include_router(protected)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8504)
