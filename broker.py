"""
broker.py – Abstraktion für Paper Trading und Live Trading via Alpaca.
Identische Schnittstelle für beide Modi – nur die URL ändert sich.
"""

from datetime import datetime
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    TRADING_MODE, get_live_config
)
from database import (
    get_session, Trade, get_open_trades,
    get_daily_trade_count, get_total_capital_in_trades,
    get_total_pnl, get_daily_pnl, close_trade, BotState
)
from rule_engine import SignalResult


class GuardrailViolation(Exception):
    """Wird geworfen wenn ein Guardrail-Limit erreicht wurde."""
    pass


def _get_alpaca_client():
    """Erstellt Alpaca-Client. Gibt None zurück wenn kein API-Key konfiguriert."""
    try:
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)
    except Exception as e:
        print(f"⚠️  Alpaca-Client nicht verfügbar: {e}")
        return None


def check_guardrails(signal: SignalResult) -> None:
    """
    Prüft ALLE Guardrails vor Trade-Ausführung.
    Wirft GuardrailViolation wenn eine Regel verletzt wird.
    Diese Funktion kann NICHT durch LLM-Output beeinflusst werden.
    """
    cfg = get_live_config()  # Guardrail-Limits aus DB (mit hardcoded Fallback)
    with get_session() as session:
        # 1. Bot pausiert?
        if BotState.get(session, "bot_paused") == "true":
            raise GuardrailViolation("Bot ist manuell pausiert")

        # 2. Tageslimit Trades
        daily_count = get_daily_trade_count(session)
        if daily_count >= cfg["MAX_TRADES_PER_DAY"]:
            raise GuardrailViolation(f"Tageslimit erreicht ({daily_count}/{cfg['MAX_TRADES_PER_DAY']} Trades)")

        # 3. Max. offene Positionen
        open_trades = get_open_trades(session)
        if len(open_trades) >= cfg["MAX_OPEN_POSITIONS"]:
            raise GuardrailViolation(f"Max. offene Positionen erreicht ({len(open_trades)}/{cfg['MAX_OPEN_POSITIONS']})")

        # 4. Doppelter Trade auf gleichen Ticker verhindern
        open_tickers = [t.ticker for t in open_trades]
        if signal.ticker in open_tickers:
            raise GuardrailViolation(f"Position auf {signal.ticker} bereits offen")

        # 5. Tägliches Verlustlimit
        daily_pnl = get_daily_pnl(session)
        daily_loss_limit = cfg["MAX_CAPITAL_TOTAL"] * cfg["DAILY_LOSS_LIMIT_PCT"]
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            BotState.set(session, "bot_paused", "true")
            session.commit()
            raise GuardrailViolation(
                f"Tägliches Verlustlimit erreicht (${abs(daily_pnl):.2f} / ${daily_loss_limit:.2f}). "
                f"Bot pausiert automatisch."
            )


MIN_ORDER_USD = 1.00  # Mindestorder bei Fractional Shares (Alpaca-Minimum)


def calculate_quantity(price: float, max_capital: float = None) -> float:
    """Berechnet Fractional-Share-Menge basierend auf Kapital-Limit.
    Alpaca akzeptiert Bruchteile (qty als float) – kein math.floor() mehr.
    max_capital=None → aktueller Wert aus der DB-Config (get_live_config)."""
    if max_capital is None:
        max_capital = get_live_config()["MAX_CAPITAL_PER_TRADE"]
    if price <= 0 or max_capital < MIN_ORDER_USD:
        return 0
    qty = max_capital / price
    return round(qty, 6)


def place_trade(signal: SignalResult, llm_result: dict) -> Trade | None:
    """
    Führt Trade aus (Paper oder Live).
    1. Guardrails prüfen
    2. Order bei Alpaca platzieren (oder Paper-Simulation)
    3. Trade in DB loggen
    Gibt Trade-Objekt zurück oder None bei Fehler.
    """
    # Guardrails zuerst – keine Ausnahmen
    check_guardrails(signal)  # Wirft GuardrailViolation bei Verstoß

    quantity = calculate_quantity(signal.current_price)

    # Ganze Aktie möglich → broker-seitige Bracket-Order (echter SL/TP-Schutz
    # auch über Nacht/Wochenende). Bruchteil → weiterhin Simple Order, da
    # Alpaca bei Fractional Shares KEINE Bracket-/Stop-Orders erlaubt
    # ("fractional orders must be simple orders") – SL/TP dafür weiterhin nur
    # softwareseitig via monitor_open_positions() (alle 30 Min, siehe unten).
    is_whole_share = quantity >= 1.0
    if is_whole_share:
        quantity = float(int(quantity))

    capital_used = round(quantity * signal.current_price, 2)

    print(f"📋 Trade-Parameter: {quantity}x {signal.ticker} @ ${signal.current_price} = ${capital_used}")

    # signal.stop_loss/take_profit sind bereits mit den (DB-konfigurierbaren,
    # siehe get_live_config) STOP_LOSS_PCT/TAKE_PROFIT_PCT berechnet (siehe
    # rule_engine.py) – für die Bracket-Order dieselben Werte verwenden statt
    # sie hier erneut aus config.py zu berechnen, sonst könnten broker-seitiger
    # SL/TP und der in der DB geloggte SL/TP (den z.B. das Dashboard anzeigt)
    # auseinanderlaufen, falls STOP_LOSS_PCT/TAKE_PROFIT_PCT per bot_config
    # überschrieben wurden.
    sl_price = signal.stop_loss
    tp_price = signal.take_profit

    # ── LIVE TRADING via Alpaca ─────────────────────────────────────
    if TRADING_MODE == "LIVE":
        client = _get_alpaca_client()
        if not client:
            print("❌ Live Trade abgebrochen: Alpaca nicht verfügbar")
            return None
        try:
            if is_whole_share:
                client.submit_order(
                    symbol=signal.ticker,
                    qty=int(quantity),
                    side="buy",
                    type="market",
                    time_in_force="day",
                    order_class="bracket",
                    stop_loss={"stop_price": sl_price},
                    take_profit={"limit_price": tp_price},
                )
                print(f"✅ LIVE Bracket-Order platziert: {int(quantity)}x {signal.ticker} SL: ${sl_price} TP: ${tp_price}")
            else:
                client.submit_order(
                    symbol=signal.ticker,
                    qty=quantity,
                    side="buy",
                    type="market",
                    time_in_force="day",
                )
                print(f"✅ LIVE Order platziert: {quantity}x {signal.ticker} (Software-Monitor SL/TP)")
        except Exception as e:
            print(f"❌ Alpaca Order fehlgeschlagen: {e}")
            return None

    # ── PAPER TRADING (Simulation) ──────────────────────────────────
    else:
        if is_whole_share:
            print(f"📄 PAPER Bracket-Trade simuliert: {int(quantity)}x {signal.ticker} @ ${signal.current_price} SL: ${sl_price} TP: ${tp_price}")
        else:
            print(f"📄 PAPER Trade simuliert: {quantity}x {signal.ticker} @ ${signal.current_price}")

    # ── In Datenbank loggen (beide Modi) ───────────────────────────
    import json as _json
    trade = Trade(
        ticker          = signal.ticker,
        direction       = signal.direction,
        instrument_type = signal.instrument_type,
        entry_price     = signal.current_price,
        stop_loss       = signal.stop_loss,
        take_profit     = signal.take_profit,
        quantity        = quantity,
        capital_used    = capital_used,
        rule_score      = signal.score,
        atr             = signal.atr,
        sl_pct          = signal.sl_pct,
        tp_pct          = signal.tp_pct,
        llm_sentiment   = llm_result.get("sentiment_score"),
        llm_summary     = llm_result.get("summary"),
        llm_risks       = _json.dumps(llm_result.get("risks", []), ensure_ascii=False),
        status          = "OPEN",
        mode            = TRADING_MODE
    )
    trade.set_score_breakdown(signal.score_breakdown)

    with get_session() as session:
        session.add(trade)
        session.commit()
        session.refresh(trade)
        print(f"💾 Trade #{trade.id} in DB gespeichert")
        return trade


def count_trading_days(start_date, end_date) -> int:
    """
    Zählt Handelstage (Mo-Fr) zwischen start_date und end_date (inklusive) –
    Feiertage werden bewusst nicht berücksichtigt (Näherung, siehe Feature
    Time-based Exit).
    """
    import pandas as pd
    return len(pd.bdate_range(start_date, end_date))


def monitor_open_positions():
    """
    Prüft alle offenen Positionen gegen aktuelle Preise.
    - Time-based Exit: Position wird nach MAX_HOLDING_DAYS Handelstagen
      unabhängig von SL/TP geschlossen.
    - Solange kein Trailing SL aktiv ist: normaler fester Stop Loss / Take
      Profit. Erreicht der Kurs den Take Profit, wird NICHT sofort verkauft,
      sondern ein ATR-basierter Trailing SL aktiviert (siehe Feature
      Trailing SL nach erstem TP).
    - Ist der Trailing SL aktiv: SL wird nachgezogen, sobald ein neuer Hoch-
      punkt erreicht wird; fällt der Kurs unter den Trailing SL, wird verkauft.
    Wird vom Scheduler regelmäßig aufgerufen.
    """
    from rule_engine import calculate_atr

    config = get_live_config()
    max_days = int(config.get("MAX_HOLDING_DAYS", 5))

    with get_session() as session:
        open_trades = get_open_trades(session)
        if not open_trades:
            return

        print(f"👁️  Monitoring {len(open_trades)} offene Position(en)...")

        for trade in open_trades:
            try:
                # Aktuellen Preis via yfinance holen
                import yfinance as yf
                ticker_data = yf.Ticker(trade.ticker)
                current_price = ticker_data.fast_info.get("lastPrice")

                if not current_price:
                    continue

                current_price = float(current_price)

                # Höchsten Kurs seit Entry tracken (Basis für Trailing SL)
                if (trade.highest_price_since_entry is None or
                        current_price > trade.highest_price_since_entry):
                    trade.highest_price_since_entry = current_price

                # Time-based Exit: unabhängig von SL/TP/Trailing.
                days_held = count_trading_days(trade.created_at.date(), datetime.now().date())
                if days_held >= max_days:
                    close_trade(session, trade, current_price, "CLOSED_TIME_EXIT")
                    print(f"⏰ {trade.ticker}: Time-Exit nach {days_held} Handelstagen (PnL: ${trade.pnl_usd:.2f})")
                    continue

                if not trade.trailing_sl_active:
                    # Phase 1: Normaler fester SL/TP
                    if current_price <= trade.stop_loss:
                        close_trade(session, trade, current_price, "CLOSED_SL")
                        print(f"🔴 SL ausgelöst: {trade.ticker} @ ${current_price} (PnL: ${trade.pnl_usd:.2f})")
                        continue

                    if current_price >= trade.take_profit:
                        # TP erreicht → Trailing SL aktivieren statt verkaufen
                        trade.trailing_sl_active = True
                        atr = calculate_atr(trade.ticker)
                        sl_distance = atr * 1.5 if atr else current_price * 0.03
                        trade.trailing_sl_price = round(current_price - sl_distance, 2)
                        print(f"🎯 {trade.ticker}: TP erreicht! Trailing SL aktiviert bei ${trade.trailing_sl_price:.2f}")

                else:
                    # Phase 2: Trailing SL aktiv – SL nach oben nachziehen
                    atr = calculate_atr(trade.ticker)
                    sl_distance = atr * 1.5 if atr else current_price * 0.03
                    new_trailing_sl = round(trade.highest_price_since_entry - sl_distance, 2)

                    if new_trailing_sl > trade.trailing_sl_price:
                        trade.trailing_sl_price = new_trailing_sl
                        print(f"📈 {trade.ticker}: Trailing SL → ${trade.trailing_sl_price:.2f}")

                    # Trailing SL ausgelöst?
                    if current_price <= trade.trailing_sl_price:
                        close_trade(session, trade, current_price, "CLOSED_TRAILING_SL")
                        print(f"🟢 Trailing SL ausgelöst: {trade.ticker} @ ${current_price} (PnL: ${trade.pnl_usd:.2f})")
                        continue

            except Exception as e:
                print(f"⚠️  Fehler beim Monitoring von {trade.ticker}: {e}")

        session.commit()


def get_portfolio_value() -> float:
    """
    Berechnet aktuellen Portfolio-Wert:
    Startkapital + realisierter P&L + unrealisierter P&L offener Positionen.
    """
    with get_session() as session:
        realized_pnl = get_total_pnl(session)
        open_trades = get_open_trades(session)

        unrealized_pnl = 0.0
        for trade in open_trades:
            try:
                import yfinance as yf
                current_price = yf.Ticker(trade.ticker).fast_info.get("lastPrice", trade.entry_price)
                unrealized_pnl += (float(current_price) - trade.entry_price) * trade.quantity
            except Exception:
                pass  # Unrealisiert ≈ 0 wenn Preis nicht abrufbar

        max_capital_total = get_live_config()["MAX_CAPITAL_TOTAL"]
        return round(max_capital_total + realized_pnl + unrealized_pnl, 2)


def get_bot_performance(days: int = 30) -> float | None:
    """
    Prozentuale Bot-Performance über die letzten `days` Tage – Vergleichsbasis
    für rule_engine.get_benchmark_performance() (S&P 500 / Nasdaq). Nutzt den
    ältesten daily_log-Snapshot innerhalb des Zeitraums als Startwert; None
    falls noch kein Snapshot in diesem Zeitraum existiert (z.B. Bot läuft
    noch keine `days` Tage).
    """
    from datetime import date, timedelta
    from database import DailyLog

    cutoff = date.today() - timedelta(days=days)
    with get_session() as session:
        start_snapshot = session.query(DailyLog).filter(
            DailyLog.log_date >= cutoff
        ).order_by(DailyLog.log_date.asc()).first()

    if not start_snapshot or start_snapshot.portfolio_value <= 0:
        return None

    current_value = get_portfolio_value()
    return round((current_value - start_snapshot.portfolio_value) / start_snapshot.portfolio_value * 100, 2)
