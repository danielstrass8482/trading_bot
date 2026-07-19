"""
broker.py – Abstraktion für Paper Trading und Live Trading via Alpaca.
Identische Schnittstelle für beide Modi – nur die URL ändert sich.
"""

import math
from datetime import datetime
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    TRADING_MODE, MAX_CAPITAL_PER_TRADE, MAX_CAPITAL_TOTAL,
    MAX_OPEN_POSITIONS, MAX_TRADES_PER_DAY, DAILY_LOSS_LIMIT_PCT
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
    with get_session() as session:
        # 1. Bot pausiert?
        if BotState.get(session, "bot_paused") == "true":
            raise GuardrailViolation("Bot ist manuell pausiert")

        # 2. Tageslimit Trades
        daily_count = get_daily_trade_count(session)
        if daily_count >= MAX_TRADES_PER_DAY:
            raise GuardrailViolation(f"Tageslimit erreicht ({daily_count}/{MAX_TRADES_PER_DAY} Trades)")

        # 3. Max. offene Positionen
        open_trades = get_open_trades(session)
        if len(open_trades) >= MAX_OPEN_POSITIONS:
            raise GuardrailViolation(f"Max. offene Positionen erreicht ({len(open_trades)}/{MAX_OPEN_POSITIONS})")

        # 4. Doppelter Trade auf gleichen Ticker verhindern
        open_tickers = [t.ticker for t in open_trades]
        if signal.ticker in open_tickers:
            raise GuardrailViolation(f"Position auf {signal.ticker} bereits offen")

        # 5. Max. Kapital pro Trade
        if signal.current_price > MAX_CAPITAL_PER_TRADE:
            # Mindestens 1 Aktie kaufen → prüfen ob Preis zu hoch
            raise GuardrailViolation(
                f"Aktienpreis ${signal.current_price} überschreitet Max-Kapitallimit "
                f"${MAX_CAPITAL_PER_TRADE} pro Trade"
            )

        # 6. Tägliches Verlustlimit
        daily_pnl = get_daily_pnl(session)
        daily_loss_limit = MAX_CAPITAL_TOTAL * DAILY_LOSS_LIMIT_PCT
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            BotState.set(session, "bot_paused", "true")
            session.commit()
            raise GuardrailViolation(
                f"Tägliches Verlustlimit erreicht (${abs(daily_pnl):.2f} / ${daily_loss_limit:.2f}). "
                f"Bot pausiert automatisch."
            )


def calculate_quantity(price: float, max_capital: float = MAX_CAPITAL_PER_TRADE) -> float:
    """Berechnet Anzahl der Aktien basierend auf Kapital-Limit."""
    if price <= 0:
        return 0
    qty = math.floor(max_capital / price)
    return max(1, qty)  # Mindestens 1 Aktie


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
    capital_used = round(quantity * signal.current_price, 2)

    print(f"📋 Trade-Parameter: {quantity}x {signal.ticker} @ ${signal.current_price} = ${capital_used}")

    # ── LIVE TRADING via Alpaca ─────────────────────────────────────
    if TRADING_MODE == "LIVE":
        client = _get_alpaca_client()
        if not client:
            print("❌ Live Trade abgebrochen: Alpaca nicht verfügbar")
            return None
        try:
            # Market Order mit Stop Loss und Take Profit als Bracket Order
            client.submit_order(
                symbol=signal.ticker,
                qty=quantity,
                side="buy",
                type="market",
                time_in_force="day",
                order_class="bracket",
                stop_loss={"stop_price": signal.stop_loss},
                take_profit={"limit_price": signal.take_profit}
            )
            print(f"✅ LIVE Order platziert: {quantity}x {signal.ticker}")
        except Exception as e:
            print(f"❌ Alpaca Order fehlgeschlagen: {e}")
            return None

    # ── PAPER TRADING (Simulation) ──────────────────────────────────
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
        llm_sentiment   = llm_result.get("sentiment_score"),
        llm_summary     = llm_result.get("summary"),
        llm_risks       = _json.dumps(llm_result.get("risks", []), ensure_ascii=False),
        status          = "OPEN",
        mode            = TRADING_MODE
    )

    with get_session() as session:
        session.add(trade)
        session.commit()
        session.refresh(trade)
        print(f"💾 Trade #{trade.id} in DB gespeichert")
        return trade


def monitor_open_positions():
    """
    Prüft alle offenen Positionen gegen aktuelle Preise.
    Schließt Positionen die Stop Loss oder Take Profit erreicht haben.
    Wird vom Scheduler regelmäßig aufgerufen.
    """
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

                # Stop Loss ausgelöst?
                if current_price <= trade.stop_loss:
                    close_trade(session, trade, current_price, "CLOSED_SL")
                    print(f"🔴 SL ausgelöst: {trade.ticker} @ ${current_price} (PnL: ${trade.pnl_usd:.2f})")

                # Take Profit ausgelöst?
                elif current_price >= trade.take_profit:
                    close_trade(session, trade, current_price, "CLOSED_TP")
                    print(f"🟢 TP ausgelöst: {trade.ticker} @ ${current_price} (PnL: ${trade.pnl_usd:.2f})")

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

        from config import MAX_CAPITAL_TOTAL
        return round(MAX_CAPITAL_TOTAL + realized_pnl + unrealized_pnl, 2)
