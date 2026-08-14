"""
active_trading.py – Direkthandel-Feature (Konzept-Dokument 2026-08-14,
Build Chunk 1, Alpaca-only). Kunden können hierüber manuell, frei wählbar
Aktien kaufen/verkaufen – unabhängig vom Bot, siehe database.ManualTrade
für die Abgrenzung.

Läuft bewusst NICHT über broker.place_trade() (zu eng mit Bot-Guardrails,
Bracket-Order und Kapitalallokation verwoben) – eigener, deutlich
schlankerer Pfad: broker.get_broker(user_id) für den Kunden-eigenen
Alpaca-Client, einfacher Market-Buy/-Sell, synchron mit kurzem Timeout auf
Fill-Bestätigung (kein asynchroner WAITING_FILL-Mechanismus wie beim Bot).

Saxo-Seite bewusst NICHT Teil dieses Chunks – siehe ManualTrade-Docstring.
"""

import concurrent.futures
import re
import time
from datetime import datetime

import yfinance as yf

import rule_engine
from trading_shared import scoring as shared_scoring
from broker import (
    get_broker, get_alpaca_account_snapshot, get_active_trading_remaining_budget,
    _user_trade_guardrail_lock,
)
from database import (
    get_session, ManualTrade, DEFAULT_USER_ID,
    get_manual_trade_by_id_for_user, get_manual_trade_by_client_order_id,
    get_open_manual_trades_with_sltp, get_manual_trade_history,
    cache_company_name,
)
from config import LONG_WATCHLIST


class ActiveTradingError(Exception):
    """Analog broker.GuardrailViolation – trading_api.py fängt das zu
    HTTPException(400, detail=str(err)) ab."""


def _resolve_broker_or_raise(user_id: int):
    """
    broker.get_broker() ist seit dem Sicherheitsfix vom 2026-08-14 (siehe
    dortige Docstring) selbst fail-closed: gibt None zurück statt eines
    Fallback-Clients auf ein fremdes Konto, falls user_id != DEFAULT_USER_ID
    keine eigenen verbundenen Alpaca-Keys hat. Der vorherige, hier lokal
    duplizierte Vor-Check (eigener get_alpaca_api_for_user()-Aufruf VOR
    get_broker()) ist dadurch redundant geworden und wurde entfernt – dieser
    Wrapper übersetzt das None-Ergebnis nur noch in eine klare, kunden-
    freundliche Fehlermeldung statt einer zweiten (identischen) DB-Abfrage.
    """
    broker_client = get_broker(user_id)
    if broker_client is None:
        raise ActiveTradingError(
            "Kein eigenes Alpaca-Konto verbunden – bitte zuerst in den Einstellungen verbinden, "
            "bevor du im Direkthandel kaufst oder verkaufst."
        )
    return broker_client


# ── Caching (ticker-basiert, NICHT nutzerbasiert – mehrere gleichzeitige ──
# Kunden teilen sich denselben Cache-Eintrag für denselben Ticker, siehe
# Konzept-Dokument "Kosten/Rate-Limits beim generalisierten Scoring").
# Einfache Zeitstempel-Prüfung statt eines komplexen TTL-Mechanismus – ein
# einzelner Prozess (kein Multi-Worker-Uvicorn, siehe systemd-Unit), daher
# reicht ein simples In-Memory-Dict ohne Cross-Prozess-Koordination.
_QUOTE_CACHE: dict[str, tuple[float, dict]] = {}
_QUOTE_CACHE_TTL_SEC = 45

_ANALYSIS_CACHE: dict[str, tuple[float, dict]] = {}
_ANALYSIS_CACHE_TTL_SEC = 60

# Alpaca-Asset-Universe (für die Freitextsuche) ändert sich praktisch nie
# untertägig (neue IPOs/Delistings) – deutlich längere TTL als Kurse.
_ASSET_UNIVERSE_CACHE: dict = {"assets": None, "ts": 0}
_ASSET_UNIVERSE_TTL_SEC = 24 * 3600


def get_quote(ticker: str) -> dict:
    """Aktueller Kurs + Identität für einen beliebigen Ticker, gecacht."""
    ticker = ticker.upper().strip()
    now = time.time()
    cached = _QUOTE_CACHE.get(ticker)
    if cached and now - cached[0] < _QUOTE_CACHE_TTL_SEC:
        return cached[1]

    try:
        price = float(yf.Ticker(ticker).fast_info.get("lastPrice") or 0)
    except Exception:
        price = 0.0
    if not price:
        raise ActiveTradingError(f"Kein aktueller Kurs für '{ticker}' verfügbar – unbekannter oder nicht handelbarer Ticker?")

    fundamentals = rule_engine.fetch_fundamentals(ticker)
    company_name = fundamentals.get("name")
    if company_name:
        with get_session() as session:
            cache_company_name(session, ticker, company_name)

    quote = {
        "ticker": ticker,
        "company_name": company_name,
        "price": round(price, 2),
        "sector": fundamentals.get("sector"),
        "industry": fundamentals.get("industry"),
    }
    _QUOTE_CACHE[ticker] = (now, quote)
    return quote


def get_analysis(ticker: str) -> dict:
    """
    Chart (6 Monate) + Score-Breakdown + Blacklist-Flag für einen beliebigen
    Ticker – generalisiert aus rule_engine.calculate_score() statt einer
    neuen Score-Implementierung (fetch_market_data/fetch_fundamentals/
    calculate_score sind bereits ticker-agnostisch, siehe Konzept-Dokument).

    Bewusst NICHT rule_engine.analyze_ticker() (der volle Bot-Signalpfad):
    der bricht bei KO-Kriterien/Korrelation/VIX/Markt-Regime früh mit
    approved=False ab – für eine reine Informations-Anzeige an einen Kunden,
    der selbst entscheidet, sind das keine Blocker (nur der Blacklist-
    Hinweis soll als nicht-blockierende Warnung erscheinen, siehe Konzept).
    """
    ticker = ticker.upper().strip()
    now = time.time()
    cached = _ANALYSIS_CACHE.get(ticker)
    if cached and now - cached[0] < _ANALYSIS_CACHE_TTL_SEC:
        return cached[1]

    # 1 Jahr Historie für eine belastbare SMA200-Berechnung (siehe
    # shared_scoring.calculate_score_factors) - die ANGEZEIGTEN Chart-Punkte
    # werden unten auf die letzten ~6 Monate beschränkt, das Scoring selbst
    # braucht aber die volle Historie, sonst wäre limited_data faktisch immer
    # True bei einem reinen 6-Monats-Abruf.
    df = rule_engine.fetch_market_data(ticker, period="1y", min_rows=50)
    if df is None or df.empty:
        raise ActiveTradingError(
            f"Keine ausreichenden Kursdaten für '{ticker}' verfügbar (unbekannter, sehr junger oder illiquider Ticker?)."
        )
    fundamentals = rule_engine.fetch_fundamentals(ticker)

    # SMA200 braucht 200 Handelstage (siehe shared_scoring.calculate_score_
    # factors) - darunter rechnet die Score-Formel mit einem neutralen
    # Fallback-Teilkredit statt eines echten Trend-Signals. Randfall (siehe
    # Konzept "Offene Fragen"): Score bleibt trotzdem berechenbar, aber die
    # UI soll "eingeschränkte Datenbasis" statt eines vollen Scores zeigen.
    limited_data = len(df) < 200

    try:
        signal = rule_engine.calculate_score(ticker, df, fundamentals)
    except Exception as e:
        raise ActiveTradingError(f"Score-Berechnung für '{ticker}' fehlgeschlagen: {e}")

    blacklist_flag = shared_scoring.is_sector_blacklisted(
        fundamentals.get("sector") or "", fundamentals.get("industry") or ""
    )

    chart_df = df.tail(126)  # ~6 Handelsmonate für die Anzeige
    chart = [
        {"date": idx.strftime("%Y-%m-%d"), "close": round(float(close), 2)}
        for idx, close in chart_df["Close"].items()
    ]

    company_name = fundamentals.get("name")
    if company_name:
        with get_session() as session:
            cache_company_name(session, ticker, company_name)

    result = {
        "ticker": ticker,
        "company_name": company_name,
        "sector": signal.sector,
        "current_price": signal.current_price,
        "score": signal.score,
        "score_breakdown": signal.score_breakdown,
        "suggested_stop_loss": signal.stop_loss,
        "suggested_take_profit": signal.take_profit,
        "blacklist_flag": blacklist_flag,
        "limited_data": limited_data,
        "chart": chart,
    }
    _ANALYSIS_CACHE[ticker] = (now, result)
    return result


def _get_asset_universe(user_id: int) -> list[dict]:
    """
    Alpaca bietet keine serverseitige Freitextsuche (/v2/assets kennt keinen
    q=-Parameter, siehe Konzept-Dokument) – EINMAL die volle handelbare
    US-Equity-Liste laden und lange cachen, Filterung passiert clientseitig
    in search_assets().
    """
    now = time.time()
    if _ASSET_UNIVERSE_CACHE["assets"] is not None and now - _ASSET_UNIVERSE_CACHE["ts"] < _ASSET_UNIVERSE_TTL_SEC:
        return _ASSET_UNIVERSE_CACHE["assets"]

    client = _resolve_broker_or_raise(user_id)
    alpaca_api = getattr(client, "api", None)
    if alpaca_api is None:
        raise ActiveTradingError("Alpaca-Client nicht verfügbar (ACTIVE_BROKER ist aktuell nicht 'alpaca'?).")

    raw_assets = alpaca_api.list_assets(status="active", asset_class="us_equity")
    assets = [
        {"symbol": a.symbol, "name": getattr(a, "name", None) or a.symbol}
        for a in raw_assets if getattr(a, "tradable", False)
    ]
    _ASSET_UNIVERSE_CACHE["assets"] = assets
    _ASSET_UNIVERSE_CACHE["ts"] = now
    return assets


def _search_rank(asset: dict, q_upper: str, q_lower: str) -> int:
    """
    Einfache Relevanz-Heuristik statt eines komplexen Rankings (Fund
    2026-08-14, Live-Test gegen den echten Paper-Account: Namenssuche nach
    "Exxon" listete die obskure Mikrocap-Aktie "Texxon Holding Limited"
    (Ticker NPT, $3,55) VOR "ExxonMobil Holdings Corporation" (Ticker XOM) -
    reines Alphabet-Sortierungsartefakt ohne Relevanz-Gewichtung, "N" kommt
    vor "X"). Je niedriger die Zahl, desto relevanter:
      0 exakte Ticker-Übereinstimmung
      1 Ticker beginnt mit dem Suchbegriff (bestehendes Verhalten für
        Ticker-Präfix-Suchen wie "AA" -> AAPL bleibt erhalten)
      2 Firmenname beginnt mit dem Suchbegriff
      3 Firmenname enthält den Suchbegriff als EIGENSTÄNDIGES Wort
        (Wortgrenzen-Regex - genau das unterscheidet "ExxonMobil" (Wort
        "exxonmobil" enthält "exxon" nicht als eigenes Wort, aber der Name
        STARTET damit -> Rang 2) von "Texxon" (enthält "exxon" nur als
        Teilstring MITTEN in einem anderen Wort, keine Wortgrenze davor -
        landet korrekt im niedrigsten Rang 4 statt fälschlich hoch).
      4 sonstiger Teilstring-Treffer
    Innerhalb jedes Rangs bleibt die bisherige alphabetische Sortierung
    (nach Ticker) als Tie-Breaker - kein Overengineering, reicht um
    offensichtliche Fälle wie das obige richtig zu priorisieren.
    """
    symbol = asset["symbol"].upper()
    name_lower = asset["name"].lower()
    if symbol == q_upper:
        return 0
    if symbol.startswith(q_upper):
        return 1
    if name_lower.startswith(q_lower):
        return 2
    if re.search(rf"\b{re.escape(q_lower)}\b", name_lower):
        return 3
    return 4


def search_assets(query: str, user_id: int = DEFAULT_USER_ID, limit: int = 20) -> list[dict]:
    """Freitextsuche (Ticker-Präfix ODER Namens-Teilstring) gegen Alpaca-
    handelbare US-Assets, nach Relevanz sortiert (siehe _search_rank).
    Kurse werden NUR für die zurückgegebene Seite live geholt (gecacht,
    siehe get_quote) – nicht für das gesamte Universe."""
    query = (query or "").strip()
    if len(query) < 1:
        return []

    universe = _get_asset_universe(user_id)
    q_upper = query.upper()
    q_lower = query.lower()

    matches = [
        a for a in universe
        if a["symbol"].upper().startswith(q_upper) or q_lower in a["name"].lower()
    ]
    matches.sort(key=lambda a: (_search_rank(a, q_upper, q_lower), a["symbol"]))
    matches = matches[:limit]

    results = []
    for asset in matches:
        try:
            quote = get_quote(asset["symbol"])
        except ActiveTradingError:
            # Kurs gerade nicht abrufbar (z.B. sehr illiquide/frisch delistet)
            # - Treffer fällt aus der Liste statt einer kaputten Zeile.
            continue
        results.append({
            "ticker": asset["symbol"],
            "company_name": quote["company_name"] or asset["name"],
            "price": quote["price"],
            "broker": "alpaca",
        })
    return results


# KRITISCHER FUND (2026-08-14, Live-Test gegen den echten Paper-Account):
# sector_recommendation() lief bis hierhin strikt SEQUENZIELL durch alle
# 383 LONG_WATCHLIST-Ticker - live gemessen 327,9s (~5,5 Min.) für eine
# einzelne Anfrage, obwohl get_analysis() bereits denselben _ANALYSIS_CACHE
# nutzt wie /api/active/analysis (die Caching-Schicht war also schon
# geteilt, half aber bei einem KALTEN Cache kaum, da bei der ersten Anfrage
# fast alle 383 Ticker ohnehin frisch abgerufen werden müssen). Kein
# Timeout einer echten HTTP-Kette (Browser/nginx/axios) überlebt das.
#
# ERSTE Version dieses Fixes gab jedem sector_recommendation()-Aufruf einen
# EIGENEN ThreadPoolExecutor(max_workers=10) - live mit 3 GLEICHZEITIGEN
# Sektor-Anfragen getestet (Aufgabe Punkt "Rate-Limit-Risiko"): effektiv
# 30 parallele yfinance-Calls, KEIN einzelner Rate-Limit-Fehler, aber keine
# der drei Anfragen kam innerhalb von 150s durch - reine Ressourcen-
# Überlastung statt eines sauberen Fehlers, für mehrere gleichzeitige
# Nutzer damit faktisch unbenutzbar. Fix: EIN geteilter, prozessweiter
# Executor (unten, außerhalb der Funktion angelegt) statt eines neuen Pools
# pro Aufruf - bindet die GESAMTE gleichzeitige yfinance-Last über ALLE
# parallelen Sektor-Anfragen hinweg auf denselben Wert, weitere Anfragen
# reihen sich in dieselbe begrenzte Warteschlange ein statt die Last pro
# zusätzlichem Nutzer zu vervielfachen (langsamer bei echter Nebenläufigkeit,
# aber sicher begrenzt statt eskalierend).
#
# Wert selbst bewusst NIEDRIGER als scan_watchlist_parallel (15, siehe
# rule_engine.py): der Bot-Scan läuft kontrolliert im festen 5-Minuten-
# Zyklus mit genau einem laufenden Prozess, dieser geteilte Pool läuft
# dagegen dauerhaft parallel zum Bot-Scan und ist nutzergetrieben - 10
# zusätzliche Worker on top vom Bot-Scan bleiben in derselben
# Größenordnung wie dessen eigene 15, kein neues Rate-Limit-Risiko
# gegenüber dem bereits produktiv laufenden Muster.
_SECTOR_SCAN_MAX_WORKERS = 10
# Modul-weit EINMAL angelegt (nicht pro Request) - siehe Docstring oben,
# das ist der eigentliche Fix für die Mehrnutzer-Überlastung.
_sector_scan_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_SECTOR_SCAN_MAX_WORKERS)


def sector_recommendation(sector_query: str) -> dict:
    """
    Top 5 aus LONG_WATCHLIST (383 Ticker, siehe Konzept-Dokument-Korrektur)
    nach Sektor gefiltert + gescort. Gleicher Blacklist-Mechanismus wie
    Feature 1, aber bereits auf die SEKTOR-ANFRAGE SELBST angewendet -
    "Pharma" zeigt den Warnhinweis schon bevor überhaupt Kandidaten
    zurückkommen.

    Parallelisiert über get_analysis() + den geteilten _sector_scan_executor
    (siehe _SECTOR_SCAN_MAX_WORKERS-Docstring oben) - jeder Aufruf nutzt
    weiterhin zuerst den bestehenden _ANALYSIS_CACHE, nur tatsächlich
    nötige Live-Abrufe laufen parallel statt sequenziell, UND teilen sich
    den Pool mit etwaigen gleichzeitigen anderen Sektor-Anfragen statt
    jeweils eigene zusätzliche Worker aufzumachen.
    """
    sector_query = (sector_query or "").strip()
    if not sector_query:
        raise ActiveTradingError("Sektor/Thema angeben.")

    query_blacklist_flag = shared_scoring.is_sector_blacklisted(sector_query, sector_query)

    def _safe_analysis(ticker: str) -> dict | None:
        try:
            return get_analysis(ticker)
        except ActiveTradingError:
            return None

    q_lower = sector_query.lower()
    candidates = []
    futures = {_sector_scan_executor.submit(_safe_analysis, ticker): ticker for ticker in LONG_WATCHLIST}
    for future in concurrent.futures.as_completed(futures):
        ticker = futures[future]
        try:
            analysis = future.result(timeout=60)
        except Exception as e:
            print(f"⚠️  Sektor-Empfehlung: {ticker} übersprungen ({e}).")
            continue
        if analysis is None:
            continue
        sector = (analysis.get("sector") or "")
        if q_lower not in sector.lower():
            continue
        candidates.append(analysis)

    candidates.sort(key=lambda a: a["score"], reverse=True)
    top5 = candidates[:5]

    return {
        "sector_query": sector_query,
        "blacklist_flag": query_blacklist_flag,
        "candidates": [
            {
                "ticker": c["ticker"],
                "company_name": c["company_name"],
                "sector": c["sector"],
                "score": c["score"],
                "current_price": c["current_price"],
                "blacklist_flag": c["blacklist_flag"],
                "limited_data": c["limited_data"],
            }
            for c in top5
        ],
    }


def _poll_fill(alpaca_api, order_id: str, tries: int = 5, interval_sec: float = 1.0) -> float | None:
    """Synchrones Warten auf Fill-Bestätigung, analog broker.place_trade()s
    3x1s-Polling (Market-Orders auf liquide Aktien füllen praktisch sofort)
    – hier 5x1s, da dies ein Vordergrund-Klick eines Kunden ist statt eines
    Hintergrund-Scan-Zyklus, etwas mehr Geduld ist vertretbar. Bewusst KEIN
    asynchroner WAITING_FILL-Reconciliation-Mechanismus wie beim Bot (siehe
    Modul-Docstring) - liefert None nach dem Timeout, Aufrufer entscheidet."""
    for _ in range(tries):
        time.sleep(interval_sec)
        try:
            order = alpaca_api.get_order(order_id)
        except Exception:
            continue
        if order.filled_avg_price:
            return float(order.filled_avg_price)
    return None


def buy(
    user_id: int,
    ticker: str,
    client_order_id: str,
    quantity: float | None = None,
    notional: float | None = None,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    origin: str = "SEARCH",
) -> ManualTrade:
    """
    Einfacher Market-Buy, KEIN place_trade() (siehe Modul-Docstring).
    Budget-Check läuft VOR jeder Order-Platzierung (Aufgabe: Kauf-Request
    schlägt fehl BEVOR eine Order rausgeht, kein Order-Rückabwickeln nötig).
    """
    if not client_order_id:
        raise ActiveTradingError("client_order_id ist Pflicht.")
    ticker = ticker.upper().strip()

    # Idempotenz-Vorprüfung AUSSERHALB des Locks (schneller Pfad für den
    # Normalfall: derselbe client_order_id wurde schon einmal erfolgreich
    # verarbeitet, kein Grund den Lock dafür zu belegen).
    with get_session() as session:
        existing = get_manual_trade_by_client_order_id(session, user_id, client_order_id)
        if existing:
            return existing

    with _user_trade_guardrail_lock(user_id):
        # Erneute Prüfung INNERHALB des Locks - Verteidigung gegen die exakte
        # Race, die dieses Projekt beim Confirm-Tier bereits live hatte
        # (zwei fast gleichzeitige Requests mit identischem client_order_id
        # könnten sonst beide die Vorprüfung oben passieren, bevor einer von
        # beiden fertig committet hat).
        with get_session() as session:
            existing = get_manual_trade_by_client_order_id(session, user_id, client_order_id)
            if existing:
                return existing

        broker_client = _resolve_broker_or_raise(user_id)
        quote = get_quote(ticker)

        if quantity is None:
            if not notional or notional <= 0:
                raise ActiveTradingError("quantity oder notional (Betrag) angeben.")
            quantity = round(notional / quote["price"], 6)
        if quantity <= 0:
            raise ActiveTradingError("quantity muss größer als 0 sein.")

        capital_used_estimate = round(quantity * quote["price"], 2)

        # ── Budget-Check (VOR der Order!) ──────────────────────────────
        budget = get_active_trading_remaining_budget(user_id)
        if budget is None:
            raise ActiveTradingError("Kontostand aktuell nicht abrufbar – bitte in Kürze erneut versuchen.")
        _effective_budget, remaining_budget = budget
        if capital_used_estimate > remaining_budget:
            raise ActiveTradingError(
                f"Nicht genug Direkthandel-Budget verfügbar (frei: ${remaining_budget:.2f}, "
                f"benötigt: ${capital_used_estimate:.2f}). Anteil in den Kapital-Einstellungen anpassen "
                f"oder eine kleinere Position wählen."
            )

        # ── Order platzieren ────────────────────────────────────────────
        try:
            result = broker_client.place_market_order(ticker, quantity, "buy")
        except Exception as e:
            raise ActiveTradingError(f"Order fehlgeschlagen: {e}")

        entry_price = result.filled_price or None
        if not entry_price:
            alpaca_api = getattr(broker_client, "api", None)
            entry_price = _poll_fill(alpaca_api, result.order_id) if alpaca_api else None

        # Fill-Preis nach Timeout weiterhin unbekannt: Trade wird trotzdem
        # angelegt (Order ist bereits raus, echtes Geld bewegt sich) mit
        # entry_price=None statt eines geratenen Preises - exakt dieselbe
        # Konvention wie bei Trade.entry_price (kein Fallback-Kurs, siehe
        # DUK-Vorfall 2026-07-27). Anders als beim Bot sind SL/TP hier
        # ABSOLUTE, vom Kunden gewählte Preise statt entry-relative Prozent-
        # werte - ein noch unbestätigter entry_price verfälscht die SL/TP-
        # Logik in monitor_manual_positions() daher NICHT, nur die Anzeige
        # von capital_used/pnl bleibt vorübergehend eine Schätzung.
        capital_used = round(quantity * (entry_price if entry_price is not None else quote["price"]), 2)

        sector = quote.get("sector")
        blacklist_flag = shared_scoring.is_sector_blacklisted(sector or "", quote.get("industry") or "")
        rule_score_at_purchase = None
        try:
            rule_score_at_purchase = get_analysis(ticker)["score"]
        except ActiveTradingError:
            pass  # rein informativ, darf den Kauf nicht verhindern

        with get_session() as session:
            trade = ManualTrade(
                user_id=user_id, ticker=ticker, company_name=quote.get("company_name"),
                quantity=quantity, entry_price=entry_price, capital_used=capital_used,
                status="OPEN", broker="alpaca", sector=sector,
                rule_score_at_purchase=rule_score_at_purchase,
                blacklist_flag_at_purchase=blacklist_flag, origin=origin,
                client_order_id=client_order_id,
                stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            )
            session.add(trade)
            session.commit()
            session.refresh(trade)
            return trade


def sell(user_id: int, trade_id: int, client_order_id: str, exit_reason: str = "CLOSED_MANUAL") -> ManualTrade:
    """
    Market-Sell für eine eigene manuelle Position. Ownership-Check ist
    PFLICHT (get_manual_trade_by_id_for_user), nicht optional – dieses
    Projekt hatte bereits mehrfach echte Multi-Tenant-Leaks dieser Form.

    exit_reason: "CLOSED_MANUAL" (Kunde verkauft aktiv) oder "CLOSED_SL"/
    "CLOSED_TP" (broker.monitor_manual_positions() ruft dies automatisiert
    mit einem synthetischen client_order_id auf, siehe dort).
    """
    if not client_order_id:
        raise ActiveTradingError("client_order_id ist Pflicht.")

    with get_session() as session:
        trade = get_manual_trade_by_id_for_user(session, trade_id, user_id)
        if trade is None:
            raise ActiveTradingError("Position nicht gefunden oder gehört nicht zu diesem Konto.")
        if trade.status != "OPEN":
            if trade.sell_client_order_id == client_order_id:
                return trade  # idempotenter Replay eines bereits erfolgreichen Verkaufs
            raise ActiveTradingError(f"Position ist bereits geschlossen (Status: {trade.status}).")

    with _user_trade_guardrail_lock(user_id):
        with get_session() as session:
            trade = get_manual_trade_by_id_for_user(session, trade_id, user_id)
            if trade is None:
                raise ActiveTradingError("Position nicht gefunden oder gehört nicht zu diesem Konto.")
            if trade.status != "OPEN":
                if trade.sell_client_order_id == client_order_id:
                    return trade
                raise ActiveTradingError(f"Position ist bereits geschlossen (Status: {trade.status}).")
            ticker, quantity, entry_price = trade.ticker, trade.quantity, trade.entry_price

        broker_client = _resolve_broker_or_raise(user_id)
        try:
            result = broker_client.place_market_order(ticker, quantity, "sell")
        except Exception as e:
            raise ActiveTradingError(f"Verkaufs-Order fehlgeschlagen: {e}")

        exit_price = result.filled_price or None
        if not exit_price:
            alpaca_api = getattr(broker_client, "api", None)
            exit_price = _poll_fill(alpaca_api, result.order_id) if alpaca_api else None
        if not exit_price:
            try:
                exit_price = get_quote(ticker)["price"]
            except ActiveTradingError:
                exit_price = entry_price  # letzter Fallback, nur für die Anzeige

        pnl_usd = None
        pnl_pct = None
        if entry_price:
            pnl_usd = round((exit_price - entry_price) * quantity, 2)
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 2)

        with get_session() as session:
            trade = get_manual_trade_by_id_for_user(session, trade_id, user_id)
            trade.status = exit_reason
            trade.exit_price = exit_price
            trade.closed_at = datetime.utcnow()
            trade.pnl_usd = pnl_usd
            trade.pnl_pct = pnl_pct
            trade.sell_client_order_id = client_order_id
            session.commit()
            session.refresh(trade)
            return trade


def monitor_manual_positions(user_id: int = DEFAULT_USER_ID):
    """
    Einfache SL/TP-Prüfung für manuelle Positionen mit vom Kunden gesetzten,
    ABSOLUTEN Verkaufsgrenzen (kein ATR, kein Trailing, kein Time-Exit -
    bewusst viel einfacher als broker.monitor_open_positions()). Von
    main.run_monitoring_cycle() im bestehenden 5-Minuten-Loop aufgerufen,
    direkt neben monitor_open_positions(user_id).
    """
    with get_session() as session:
        trades = get_open_manual_trades_with_sltp(session, user_id)
        trade_snapshots = [(t.id, t.ticker, t.stop_loss_price, t.take_profit_price) for t in trades]

    for trade_id, ticker, stop_loss_price, take_profit_price in trade_snapshots:
        try:
            current_price = get_quote(ticker)["price"]
        except ActiveTradingError as e:
            print(f"⚠️  Direkthandel-Monitoring: Kurs für {ticker} (manual_trades#{trade_id}) nicht abrufbar: {e}")
            continue

        exit_reason = None
        if stop_loss_price is not None and current_price <= stop_loss_price:
            exit_reason = "CLOSED_SL"
        elif take_profit_price is not None and current_price >= take_profit_price:
            exit_reason = "CLOSED_TP"
        if not exit_reason:
            continue

        synthetic_client_order_id = f"auto-{exit_reason.lower()}-{trade_id}-{int(time.time())}"
        try:
            sell(user_id, trade_id, synthetic_client_order_id, exit_reason=exit_reason)
            print(f"✅ Direkthandel-Monitoring: manual_trades#{trade_id} ({ticker}) automatisch verkauft ({exit_reason} @ ${current_price}).")
        except ActiveTradingError as e:
            print(f"⚠️  Direkthandel-Monitoring: Auto-Verkauf für manual_trades#{trade_id} ({ticker}) fehlgeschlagen: {e}")
