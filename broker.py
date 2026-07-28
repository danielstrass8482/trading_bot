"""
broker.py – Abstraktion für Paper Trading und Live Trading via Alpaca.
Identische Schnittstelle für beide Modi – nur die URL ändert sich.
"""

import os
from datetime import datetime
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    TRADING_MODE, get_live_config
)
from database import (
    get_session, Trade, get_open_trades,
    get_daily_trade_count, get_total_capital_in_trades,
    get_total_pnl, get_daily_pnl, close_trade, BotState,
    get_alpaca_api_for_user,
)
from rule_engine import SignalResult
from broker_interface import BrokerInterface


class GuardrailViolation(Exception):
    """Wird geworfen wenn ein Guardrail-Limit erreicht wurde."""
    pass


def get_broker(user_id: int = None) -> BrokerInterface:
    """
    Broker-Factory (siehe broker_interface.py): liest bot_config.ACTIVE_BROKER
    ("alpaca"/"ibkr") und gibt die passende BrokerInterface-Implementierung
    zurück. Bewusst getrennt von place_trade()/_get_alpaca_client() oben, die
    weiterhin das bisherige, fest auf Alpaca zugeschnittene Guardrail+DB-
    Logging übernehmen – get_broker() ist der Broker-agnostische Einstieg für
    neuen Code (z.B. künftige IBKR-Order-Platzierung, Konto-/Positionsabfragen).
    """
    from broker_alpaca import AlpacaBroker
    from broker_ibkr import IBKRBroker

    config = get_live_config()
    broker_type = config.get("ACTIVE_BROKER", "alpaca")

    if broker_type == "ibkr":
        return IBKRBroker()

    # Alpaca (Standard)
    if user_id:
        client = get_alpaca_api_for_user(user_id)
        if client:
            return AlpacaBroker(client=client)
    return AlpacaBroker()


def _get_alpaca_client(user_id: int = None):
    """
    Erstellt Alpaca-Client. Mit user_id (Feature 8 Multi-Tenant) wird zuerst
    versucht, die pro Nutzer in pos_users hinterlegten Keys zu verwenden
    (siehe database.get_alpaca_api_for_user) – ohne user_id oder wenn der
    Nutzer keine eigenen Keys verbunden hat, Fallback auf die globalen
    .env-Keys (Beta-Phase: Daniel als einziger aktiver Trader, kein call site
    übergibt aktuell eine user_id – Verhalten bleibt also unverändert).
    Gibt None zurück wenn kein API-Key konfiguriert.
    """
    if user_id is not None:
        client = get_alpaca_api_for_user(user_id)
        if client:
            return client
    try:
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL)
    except Exception as e:
        print(f"⚠️  Alpaca-Client nicht verfügbar: {e}")
        return None


def get_alpaca_account_snapshot(user_id: int = None) -> dict | None:
    """
    Liest Cash/Buying-Power/Marktwert/unrealisierten G&V DIREKT von Alpaca
    (GET /v2/account + /v2/positions) – im Gegensatz zu get_portfolio_value()
    (das den Portfolio-Wert über yfinance-Kurse NACHRECHNET) ist das die
    Broker-eigene Wahrheit. Wird für die Übersicht gebraucht, um "verfügbares
    Kapital" (cash) von "gebunden in offenen Positionen" (long_market_value)
    zu trennen – die bisherige Anzeige zeigte nur die Summe beider (equity)
    und suggerierte damit mehr frei verfügbares Kapital als tatsächlich da war.
    None falls Alpaca nicht erreichbar (Aufrufer fällt dann auf
    get_portfolio_value() zurück, siehe trading_api.get_overview).
    """
    client = _get_alpaca_client(user_id)
    if not client:
        return None
    try:
        account = client.get_account()
        positions = client.list_positions()
    except Exception as e:
        print(f"⚠️  Alpaca-Account-Snapshot fehlgeschlagen: {e}")
        return None

    return {
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "equity": float(account.equity),
        "long_market_value": float(account.long_market_value),
        "unrealized_pl": round(sum(float(p.unrealized_pl) for p in positions), 2),
    }


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

    # Sicherheitsnetz: die eigentliche Order-Platzierung unten spricht aus-
    # schließlich die Alpaca-API an – IBKR-Order-Routing ist (noch) nicht
    # implementiert (broker_ibkr.py existiert nur als BrokerInterface-Client,
    # siehe broker.get_broker). Wäre ACTIVE_BROKER="ibkr" hier folgenlos, würde
    # der Trade fälschlich als "ibkr" geloggt, obwohl tatsächlich Alpaca (Live-
    # Geld!) gehandelt hat – daher lieber gar kein Trade als eine falsche
    # Broker-Zuordnung im Log.
    active_broker = get_live_config().get("ACTIVE_BROKER", "alpaca")
    if active_broker != "alpaca":
        print(f"❌ ACTIVE_BROKER='{active_broker}', aber Order-Routing ist aktuell nur für Alpaca implementiert – Trade übersprungen.")
        return None

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

    # entry_price wird per Default vom Signalzeitpunkt übernommen (PAPER-Modus,
    # oder LIVE-Fallback falls die Order nicht rechtzeitig gefüllt wird) –
    # im LIVE-Modus unten durch den tatsächlichen Alpaca-Fill-Preis ersetzt.
    entry_price = signal.current_price

    # ── LIVE TRADING via Alpaca ─────────────────────────────────────
    if TRADING_MODE == "LIVE":
        client = _get_alpaca_client()
        if not client:
            print("❌ Live Trade abgebrochen: Alpaca nicht verfügbar")
            return None
        try:
            if is_whole_share:
                order = client.submit_order(
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
                order = client.submit_order(
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

        # Echten Fill-Preis von Alpaca abfragen statt den yfinance-Kurs vom
        # Signalzeitpunkt als entry_price zu übernehmen (siehe DUK-Vorfall
        # 2026-07-27: eine veraltete/falsche yfinance-Quote landete sonst 1:1
        # als entry_price + Stop Loss/Take Profit in der DB, während Alpaca
        # tatsächlich zum echten Marktpreis gefüllt hat – der Stop Loss lag
        # dadurch weit unter dem realen Kurs und löste sofort fälschlich aus).
        # Market-Orders auf liquide Aktien füllen praktisch sofort, kurzes
        # Polling reicht.
        import time
        for _ in range(3):
            time.sleep(1)
            try:
                filled_order = client.get_order(order.id)
            except Exception as e:
                print(f"⚠️  Order-Status konnte nicht abgefragt werden: {e}")
                break
            if filled_order.filled_avg_price:
                entry_price = float(filled_order.filled_avg_price)
                break
        else:
            print(f"⚠️  {signal.ticker}: Order nach 3s noch nicht gefüllt – Signal-Kurs (${signal.current_price}) als entry_price-Fallback.")

        if entry_price != signal.current_price:
            print(f"ℹ️  {signal.ticker}: Entry-Preis aus Alpaca-Fill: ${entry_price} (Signal-Kurs war ${signal.current_price})")

    # ── PAPER TRADING (Simulation) ──────────────────────────────────
    else:
        if is_whole_share:
            print(f"📄 PAPER Bracket-Trade simuliert: {int(quantity)}x {signal.ticker} @ ${signal.current_price} SL: ${sl_price} TP: ${tp_price}")
        else:
            print(f"📄 PAPER Trade simuliert: {quantity}x {signal.ticker} @ ${signal.current_price}")

    # capital_used anhand des tatsächlichen entry_price statt des vorläufigen
    # Signal-Kurses – sonst würde z.B. bei DUK weiterhin $50 "capital_used"
    # geloggt, obwohl real nur ~$20 investiert wurden (Menge wurde ja mit dem
    # falschen Signal-Kurs berechnet).
    capital_used = round(quantity * entry_price, 2)

    # ── In Datenbank loggen (beide Modi) ───────────────────────────
    import json as _json
    trade = Trade(
        ticker          = signal.ticker,
        direction       = signal.direction,
        instrument_type = signal.instrument_type,
        entry_price     = entry_price,
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
        mode            = TRADING_MODE,
        broker          = active_broker,
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


def _sell_position_at_alpaca(ticker: str, fallback_price: float) -> float | None:
    """
    Verkauft die komplette Alpaca-Position in `ticker` tatsächlich und gibt
    den echten Fill-Preis zurück.

    Kritischer Fix (siehe DUK/NVDA-Vorfall 2026-07-27): monitor_open_positions()
    rief bisher direkt close_trade() auf, das NUR die DB aktualisiert und
    NIE eine Order bei Alpaca platziert hat – Positionen liefen dadurch live
    und komplett ungeschützt weiter, während der Bot sie für geschlossen
    hielt (kein SL/TP/Trailing mehr, da get_open_trades() nur status='OPEN'
    liefert).

    Gibt None zurück, wenn der Verkauf fehlschlägt – der Trade bleibt dann
    OPEN und der nächste Monitoring-Zyklus versucht es erneut, statt einen
    nicht tatsächlich verkauften Trade als geschlossen zu markieren.
    Ausnahme: existiert die Alpaca-Position gar nicht mehr (z.B. weil eine
    broker-seitige Bracket-Order sie bei ganzen Aktien bereits geschlossen
    hat), gilt sie als bereits geschlossen und `fallback_price` (aktueller
    Kurs) wird zurückgegeben.
    """
    if TRADING_MODE != "LIVE":
        return fallback_price  # Paper-Modus: kein echter Broker involviert

    client = _get_alpaca_client()
    if not client:
        print(f"⚠️  {ticker}: Alpaca-Client nicht verfügbar – Verkauf übersprungen, Trade bleibt OPEN.")
        return None

    try:
        position = client.get_position(ticker)
    except Exception as e:
        if "position does not exist" in str(e).lower() or "404" in str(e):
            print(f"ℹ️  {ticker}: Keine Alpaca-Position mehr vorhanden (vermutlich bereits broker-seitig geschlossen).")
            return fallback_price
        print(f"⚠️  {ticker}: Alpaca-Position konnte nicht abgefragt werden ({e}) – Trade bleibt OPEN.")
        return None

    qty = abs(float(position.qty))
    if qty <= 0:
        return fallback_price

    try:
        order = client.submit_order(
            symbol=ticker, qty=qty, side="sell", type="market", time_in_force="day",
        )
    except Exception as e:
        print(f"⚠️  {ticker}: Verkaufsorder fehlgeschlagen ({e}) – Trade bleibt OPEN.")
        return None

    import time
    for _ in range(3):
        time.sleep(1)
        try:
            filled_order = client.get_order(order.id)
        except Exception:
            continue
        if filled_order.filled_avg_price:
            print(f"✅ {ticker}: Live verkauft @ ${filled_order.filled_avg_price}")
            return float(filled_order.filled_avg_price)

    print(f"⚠️  {ticker}: Verkaufsorder platziert, aber Fill nach 3s nicht bestätigt – Fallback-Kurs (${fallback_price}) für PnL-Berechnung.")
    return fallback_price


def monitor_open_positions():
    """
    Prüft alle offenen Positionen gegen aktuelle Preise.
    - Time-based Exit: Ohne aktiven Trailing-SL wird die Position nach
      MAX_HOLDING_DAYS Handelstagen geschlossen (CLOSED_TIME_EXIT). Mit
      aktivem Trailing-SL wird der Time-Exit ausgesetzt (der Trade läuft
      bereits profitabel mit eigenem adaptiven Schutz) – als Sicherheitsnetz
      greift stattdessen eine harte Obergrenze bei MAX_HOLDING_DAYS *
      MAX_HOLDING_DAYS_TRAILING_MULTIPLIER Handelstagen (CLOSED_TIME_EXIT_
      HARD_CAP). Beide Time-Exit-Varianten legen einen post_exit_tracking-
      Eintrag an (siehe post_exit_tracking.py), der den Kursverlauf danach
      beobachtet, um die Schwellenwerte selbst zu evaluieren (Backlook).
    - Solange kein Trailing SL aktiv ist: normaler fester Stop Loss. Trailing
      SL wird aktiviert, sobald der Kurs den NIEDRIGEREN der beiden Trigger
      erreicht: den fixen TRAILING_ACTIVATION_PCT ggü. Entry, oder das
      individuelle ATR-basierte Take Profit – je nachdem was zuerst kommt
      (siehe Feature Trailing SL nach erstem TP). Es wird dabei NICHT sofort
      verkauft, sondern der Trailing SL aktiviert.
    - Ist der Trailing SL aktiv: SL wird nachgezogen, sobald ein neuer Hoch-
      punkt erreicht wird; fällt der Kurs unter den Trailing SL, wird verkauft.
      Die Trailing-Distanz ist ATR-basiert (ATR_MULTIPLIER_SL, konsistent mit
      dem Entry-SL in rule_engine.py) und auf ATR_MIN_SL_PCT/ATR_MAX_SL_PCT
      geclampt, damit bei sehr volatilen Tickern nicht unnötig viel bereits
      erreichter Gewinn wieder preisgegeben wird, bevor der Trailing-Stop greift.
    Wird vom Scheduler regelmäßig aufgerufen.
    """
    from rule_engine import calculate_atr
    from post_exit_tracking import start_tracking_if_applicable

    config = get_live_config()
    max_days = int(config.get("MAX_HOLDING_DAYS", 5))
    max_days_trailing_multiplier = int(config.get("MAX_HOLDING_DAYS_TRAILING_MULTIPLIER", 2))
    atr_multiplier_sl = config.get("ATR_MULTIPLIER_SL", 1.5)
    min_sl_pct = config.get("ATR_MIN_SL_PCT", 0.01)
    max_sl_pct = config.get("ATR_MAX_SL_PCT", 0.08)
    trailing_activation_pct = config.get("TRAILING_ACTIVATION_PCT", 0.06)

    def _clamped_trailing_distance(atr, reference_price):
        """ATR-basierte Trailing-Distanz in $, auf ATR_MIN_SL_PCT/ATR_MAX_SL_PCT
        geclampt (analog zum Entry-SL in rule_engine.py). Fallback 3% falls
        kein ATR verfügbar. `reference_price` ist der Kurs, gegen den der
        %-Anteil berechnet wird (bei Aktivierung: current_price; beim
        Nachziehen: highest_price_since_entry, da davon abgezogen wird)."""
        if atr and atr > 0:
            raw_distance = atr * atr_multiplier_sl
            pct = max(min_sl_pct, min(max_sl_pct, raw_distance / reference_price))
            return reference_price * pct
        return reference_price * 0.03

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

                # Time-based Exit: Bei aktivem Trailing-SL ausgesetzt (der Trade
                # trägt bereits seinen eigenen adaptiven Schutz und ist nachweislich
                # profitabel – ein stures Kappen nach MAX_HOLDING_DAYS würde genau
                # die laufenden Gewinner unnötig abschneiden). Stattdessen greift
                # eine harte Obergrenze bei MAX_HOLDING_DAYS * MAX_HOLDING_DAYS_
                # TRAILING_MULTIPLIER als Sicherheitsnetz gegen endlos offene
                # Positionen. Ohne aktiven Trailing-SL bleibt der normale Time-Exit
                # unverändert bei MAX_HOLDING_DAYS.
                days_held = count_trading_days(trade.created_at.date(), datetime.now().date())
                if trade.trailing_sl_active:
                    time_exit_reason = None
                    if days_held >= max_days * max_days_trailing_multiplier:
                        time_exit_reason = "CLOSED_TIME_EXIT_HARD_CAP"
                else:
                    time_exit_reason = "CLOSED_TIME_EXIT" if days_held >= max_days else None

                if time_exit_reason:
                    real_exit_price = _sell_position_at_alpaca(trade.ticker, current_price)
                    if real_exit_price is None:
                        print(f"⏭️  {trade.ticker}: Time-Exit-Verkauf fehlgeschlagen – bleibt OPEN, nächster Versuch beim nächsten Zyklus.")
                        continue
                    close_trade(session, trade, real_exit_price, time_exit_reason)
                    start_tracking_if_applicable(session, trade)
                    label = "Time-Exit (harte Obergrenze bei aktivem Trailing-SL)" if time_exit_reason == "CLOSED_TIME_EXIT_HARD_CAP" else "Time-Exit"
                    print(f"⏰ {trade.ticker}: {label} nach {days_held} Handelstagen (PnL: ${trade.pnl_usd:.2f})")
                    continue

                if not trade.trailing_sl_active:
                    # Phase 1: Normaler fester SL/TP
                    if current_price <= trade.stop_loss:
                        real_exit_price = _sell_position_at_alpaca(trade.ticker, current_price)
                        if real_exit_price is None:
                            print(f"⏭️  {trade.ticker}: SL-Verkauf fehlgeschlagen – bleibt OPEN, nächster Versuch beim nächsten Zyklus.")
                            continue
                        close_trade(session, trade, real_exit_price, "CLOSED_SL")
                        print(f"🔴 SL ausgelöst: {trade.ticker} @ ${real_exit_price} (PnL: ${trade.pnl_usd:.2f})")
                        continue

                    # Trailing SL aktiviert sich beim NIEDRIGEREN der beiden
                    # Trigger-Preise: fixer TRAILING_ACTIVATION_PCT ggü. Entry,
                    # oder individuelles ATR-TP – je nachdem was zuerst erreicht wird.
                    fixed_trigger_price = trade.entry_price * (1 + trailing_activation_pct)
                    if fixed_trigger_price <= trade.take_profit:
                        effective_trigger_price = fixed_trigger_price
                        trigger_reason = f"TRAILING_ACTIVATION_PCT ({trailing_activation_pct:.1%})"
                    else:
                        effective_trigger_price = trade.take_profit
                        tp_pct_label = f"{trade.tp_pct:.1%}" if trade.tp_pct else "?"
                        trigger_reason = f"ATR-TP ({tp_pct_label})"

                    if current_price >= effective_trigger_price:
                        # Trigger erreicht → Trailing SL aktivieren statt verkaufen
                        trade.trailing_sl_active = True
                        atr = calculate_atr(trade.ticker)
                        sl_distance = _clamped_trailing_distance(atr, current_price)
                        trade.trailing_sl_price = round(current_price - sl_distance, 2)
                        print(f"🎯 {trade.ticker}: Trailing SL aktiviert via {trigger_reason} "
                              f"(Kurs ${current_price:.2f} >= Trigger ${effective_trigger_price:.2f}) "
                              f"bei ${trade.trailing_sl_price:.2f}")

                else:
                    # Phase 2: Trailing SL aktiv – SL nach oben nachziehen
                    atr = calculate_atr(trade.ticker)
                    sl_distance = _clamped_trailing_distance(atr, trade.highest_price_since_entry)
                    new_trailing_sl = round(trade.highest_price_since_entry - sl_distance, 2)

                    if new_trailing_sl > trade.trailing_sl_price:
                        trade.trailing_sl_price = new_trailing_sl
                        print(f"📈 {trade.ticker}: Trailing SL → ${trade.trailing_sl_price:.2f}")

                    # Trailing SL ausgelöst?
                    if current_price <= trade.trailing_sl_price:
                        real_exit_price = _sell_position_at_alpaca(trade.ticker, current_price)
                        if real_exit_price is None:
                            print(f"⏭️  {trade.ticker}: Trailing-SL-Verkauf fehlgeschlagen – bleibt OPEN, nächster Versuch beim nächsten Zyklus.")
                            continue
                        close_trade(session, trade, real_exit_price, "CLOSED_TRAILING_SL")
                        print(f"🟢 Trailing SL ausgelöst: {trade.ticker} @ ${real_exit_price} (PnL: ${trade.pnl_usd:.2f})")
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
