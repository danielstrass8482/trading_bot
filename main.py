"""
main.py – Orchestrierung: Scheduler startet täglich den Bot-Loop.
Ablauf: VIX-Check → Watchlist scannen → Guardrails → LLM → Trade
"""

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import math
import pytz
from sqlalchemy import text

from config import (
    LONG_WATCHLIST, ACTIVE_SHORT_INSTRUMENTS, VOLATILE_WATCHLIST,
    PROFIT_ALERT_TARGET, MAX_CAPITAL_TOTAL, TRADING_MODE, DEFAULT_USER_ID,
    validate_config, get_live_config,
)
from database import (
    init_db, get_session, save_daily_snapshot, BotState, ScanLog,
    EntryTimeSlot, get_active_entry_time_slots, get_daily_trade_count,
    get_open_trades, FairValueCache, BotHeartbeat,
    save_position_snapshot, get_previous_position_snapshot,
    get_user_live_config, get_connected_alpaca_users, get_total_capital_in_trades,
)
from notifications import send_email
from rule_engine import scan_watchlist_parallel, check_vix, get_market_regime, get_benchmark_performance
from llm_analyst import analyze_with_llm, get_market_brief
from broker import (
    place_trade, monitor_open_positions, get_portfolio_value, get_bot_performance,
    check_guardrails, check_position_consistency, GuardrailViolation,
    get_alpaca_account_snapshot, get_effective_max_capital_total_bot,
    get_effective_max_capital_total_bot_costbasis,
)
from backlook import run_backlook
from fair_value import update_fair_value_cache, get_undervalued_tickers
from saxo_client import get_valid_access_token
from post_exit_tracking import update_pending_tracking


def _get_current_price_for_snapshot(ticker: str, fallback: float) -> float:
    """
    Einzelticker-Preisabruf via yf.Ticker(...).fast_info (KEIN yf.download()-
    Batch – siehe KRITISCHER INCIDENT #2 / Commit 255ed64: Cross-Contamination
    zwischen parallel gescannten Tickern bei geteiltem yfinance-Batch-State).
    Läuft hier ohnehin sequenziell (kein ThreadPoolExecutor), Fallback auf
    entry_price falls yfinance keinen Preis liefert.
    """
    try:
        import yfinance as yf
        price = yf.Ticker(ticker).fast_info.get("lastPrice")
        return float(price) if price else fallback
    except Exception:
        return fallback


def capture_daily_position_snapshot():
    """
    Speichert den Tages-Snapshot aller offenen Positionen + Gesamt-Portfoliowert
    (siehe database.DailyPositionSnapshot) – Scheduler-Job 16:05 ET, kurz NACH
    dem letzten Monitoring-Zyklus des Tages (run_monitoring_cycle läuft bis
    16:00 ET, siehe main(): CronTrigger(hour="9-16", ...)). Dient gleichzeitig
    als "Vortag-Endstand" UND als Vergleichsbasis für die Tages-Mail des
    nächsten Tages (siehe send_daily_summary_email) – ein Snapshot-Job deckt
    beides ab, kein separater Morgen-Snapshot nötig.
    """
    et_tz = pytz.timezone("America/New_York")
    today = datetime.now(et_tz).date()
    portfolio_value = get_portfolio_value()

    positions = []
    with get_session() as session:
        for trade in get_open_trades(session):
            # Fix 2026-07-31: entry_price ist None, solange die Kauf-Order
            # noch WAITING_FILL ist (siehe broker.place_trade/
            # _reconcile_pending_entry_fill) - PnL lässt sich dafür noch nicht
            # berechnen, DailyPositionSnapshot-Spalten sind dafür bereits
            # nullable. Position bleibt trotzdem im Snapshot sichtbar.
            if trade.entry_price is None:
                positions.append({
                    "ticker": trade.ticker,
                    "trade_id": trade.id,
                    "quantity": trade.quantity,
                    "entry_price": None,
                    "price": None,
                    "unrealized_pnl": None,
                    "unrealized_pnl_pct": None,
                })
                continue
            current_price = _get_current_price_for_snapshot(trade.ticker, trade.entry_price)
            positions.append({
                "ticker": trade.ticker,
                "trade_id": trade.id,
                "quantity": trade.quantity,
                "entry_price": trade.entry_price,
                "price": current_price,
                "unrealized_pnl": (current_price - trade.entry_price) * trade.quantity,
                "unrealized_pnl_pct": (current_price - trade.entry_price) / trade.entry_price * 100,
            })

        save_position_snapshot(session, today, portfolio_value, positions)
        session.commit()

    print(f"📸 Positions-Snapshot gespeichert ({today}): {len(positions)} offene Position(en), "
          f"Portfolio-Wert ${portfolio_value:.2f}")


def send_daily_summary_email():
    """
    Verschickt EINE Tages-Zusammenfassung nach Handelsschluss (Scheduler-Job
    16:10 ET, siehe main() – 5 Min nach capture_daily_position_snapshot, damit
    der Vorabend-Vergleich auf einem bereits geschriebenen Snapshot aufbaut).
    Zeigt pro Slot die tatsächlich ausgeführten Trades im Detail (Ticker,
    Entry, SL/TP je Preis+%, Kapital, Menge) bzw. bei tradefreien Slots einen
    kurzen Grund, sowie einen Vorabend-vs-Heute-Vergleich aller offenen
    Positionen und des Gesamt-Portfoliowerts (siehe database.
    DailyPositionSnapshot/get_previous_position_snapshot).
    """
    et_tz = pytz.timezone("America/New_York")
    today = datetime.now(et_tz).date()

    with get_session() as session:
        slots_heute = session.execute(text("""
            SELECT slot_et,
                   COUNT(*) as gescannt,
                   SUM(CASE WHEN score >= 65 THEN 1 ELSE 0 END) as ueber_65,
                   SUM(CASE WHEN trade_executed THEN 1 ELSE 0 END) as trades,
                   ROUND(AVG(score)::numeric, 1) as avg_score
            FROM scan_log
            WHERE DATE(scan_time AT TIME ZONE 'America/New_York') = :today
              AND score > 0
            GROUP BY slot_et
            ORDER BY slot_et
        """), {"today": today}).fetchall()

        trade_rows = session.execute(text("""
            SELECT sl.slot_et, sl.ticker, t.entry_price, t.stop_loss, t.take_profit,
                   t.sl_pct, t.tp_pct, t.capital_used, t.quantity
            FROM scan_log sl
            JOIN trades t ON t.id = sl.trade_id
            WHERE DATE(sl.scan_time AT TIME ZONE 'America/New_York') = :today
              AND sl.trade_executed = true
            ORDER BY sl.slot_et, sl.ticker
        """), {"today": today}).fetchall()

        trades_by_slot: dict = {}
        for row in trade_rows:
            trades_by_slot.setdefault(row.slot_et, []).append(row)

        # Grund fürs Ausbleiben eines Trades je tradefreiem Slot: entweder kein
        # Signal über Schwellwert, oder der bestbewertete freigegebene Kandidat
        # wurde von einem Guardrail geblockt (siehe run_entry_cycle).
        no_trade_reasons: dict = {}
        for slot in slots_heute:
            if slot.trades:
                continue
            if not slot.ueber_65:
                no_trade_reasons[slot.slot_et] = f"Kein Signal über Schwellwert (Ø Score {slot.avg_score})"
                continue
            reason_row = session.execute(text("""
                SELECT guardrail_reason FROM scan_log
                WHERE slot_et = :slot_et
                  AND DATE(scan_time AT TIME ZONE 'America/New_York') = :today
                  AND score >= 65
                ORDER BY score DESC LIMIT 1
            """), {"slot_et": slot.slot_et, "today": today}).fetchone()
            no_trade_reasons[slot.slot_et] = (
                reason_row.guardrail_reason if reason_row and reason_row.guardrail_reason
                else "Kein Trade ausgeführt (Grund nicht ermittelbar)"
            )

        open_trades = session.execute(text("""
            SELECT id, ticker, entry_price, quantity, capital_used
            FROM trades WHERE status = 'OPEN'
        """)).fetchall()

        prev_snapshot = get_previous_position_snapshot(session, today)

    portfolio_value = get_portfolio_value()

    subject = f"📊 Trading Bot – Tageszusammenfassung {today.strftime('%d.%m.%Y')}"

    body = f"""Trading Bot – Tageszusammenfassung {today.strftime('%d.%m.%Y')}
{'='*50}

PORTFOLIO
Portfolio-Wert heute Abend: ${portfolio_value:.2f}
"""
    if prev_snapshot and prev_snapshot["portfolio_value"] is not None:
        prev_value = prev_snapshot["portfolio_value"]
        diff = portfolio_value - prev_value
        diff_pct = (diff / prev_value * 100) if prev_value else 0.0
        body += (
            f"Portfolio-Wert Vorabend ({prev_snapshot['snapshot_date'].strftime('%d.%m.%Y')}): ${prev_value:.2f}\n"
            f"Veränderung: {diff:+.2f} $ ({diff_pct:+.2f}%)\n"
        )
    else:
        body += "Erster Tag mit Snapshot-Tracking, Vergleich ab morgen verfügbar.\n"

    body += "\nTRADES HEUTE\n"
    for slot in slots_heute:
        body += f"""
Slot {slot.slot_et} ET (gescannt: {slot.gescannt}, über Schwellwert: {slot.ueber_65}, Ø Score: {slot.avg_score}):
"""
        slot_trades = trades_by_slot.get(slot.slot_et)
        if slot_trades:
            for t in slot_trades:
                # Fix 2026-07-31: entry_price ist None, solange die Kauf-Order
                # noch WAITING_FILL ist (siehe broker.place_trade) - SL/TP-%
                # lassen sich dafür noch nicht berechnen.
                if t.entry_price is None:
                    body += (
                        f"  {t.ticker}: Kauf-Order noch nicht gefüllt (wartet auf Fill-Bestätigung) | "
                        f"Kapital reserviert ${t.capital_used:.2f} | Menge {t.quantity:.4f}\n"
                    )
                    continue
                sl_pct = t.sl_pct * 100 if t.sl_pct is not None else (t.entry_price - t.stop_loss) / t.entry_price * 100
                tp_pct = t.tp_pct * 100 if t.tp_pct is not None else (t.take_profit - t.entry_price) / t.entry_price * 100
                body += (
                    f"  {t.ticker}: Entry ${t.entry_price:.2f} | "
                    f"SL ${t.stop_loss:.2f} (-{sl_pct:.1f}%) | TP ${t.take_profit:.2f} (+{tp_pct:.1f}%) | "
                    f"Kapital ${t.capital_used:.2f} | Menge {t.quantity:.4f}\n"
                )
        else:
            body += f"  Kein Trade – {no_trade_reasons.get(slot.slot_et, 'Kein Trade ausgeführt')}\n"

    trades_heute = sum(s.trades for s in slots_heute)
    body += f"\nGESAMT HEUTE\nTrades ausgeführt: {trades_heute}\n"

    body += "\nOFFENE POSITIONEN – TAGESVERGLEICH\n"
    if open_trades:
        prev_positions = prev_snapshot["positions_by_trade_id"] if prev_snapshot else {}
        for t in open_trades:
            # Fix 2026-07-31: entry_price ist None, solange die Kauf-Order
            # noch WAITING_FILL ist (siehe broker.place_trade) - PnL lässt
            # sich dafür noch nicht berechnen.
            if t.entry_price is None:
                body += f"  {t.ticker} (Kauf-Order noch nicht gefüllt): wartet auf Fill-Bestätigung\n"
                continue

            current_price = _get_current_price_for_snapshot(t.ticker, t.entry_price)
            pnl_pct_heute = (current_price - t.entry_price) / t.entry_price * 100

            prev_pos = prev_positions.get(t.id)
            if prev_pos is not None and prev_pos.unrealized_pnl_pct is not None:
                delta_heute = pnl_pct_heute - prev_pos.unrealized_pnl_pct
                body += (
                    f"  {t.ticker}: Vorabend ${prev_pos.price:.2f} (uPnL {prev_pos.unrealized_pnl_pct:+.2f}%) "
                    f"→ Heute ${current_price:.2f} (uPnL {pnl_pct_heute:+.2f}%) | Δ heute: {delta_heute:+.2f}%\n"
                )
            else:
                body += f"  {t.ticker} (NEU heute gekauft): ${current_price:.2f} (uPnL {pnl_pct_heute:+.2f}%)\n"
    else:
        body += "  Keine offenen Positionen\n"

    # Performance-Vergleich vs. Benchmarks (siehe Feature Benchmark-Vergleich)
    bot_performance = get_bot_performance(days=30)
    benchmarks = get_benchmark_performance(days=30)
    if bot_performance is not None:
        sp500 = benchmarks.get("S&P 500")
        nasdaq = benchmarks.get("Nasdaq")
        body += f"""
PERFORMANCE-VERGLEICH (30 Tage):
Dein Bot:  {bot_performance:+.2f}%
S&P 500:   {f'{sp500:+.2f}%' if sp500 is not None else 'N/A'}
Nasdaq:    {f'{nasdaq:+.2f}%' if nasdaq is not None else 'N/A'}
"""

    send_email(subject, body)
    print(f"✅ Tages-E-Mail gesendet ({len(slots_heute)} Slots)")


def generate_morning_market_brief() -> str:
    """Erstellt das tägliche KI-Marktbriefing (siehe llm_analyst.get_market_brief)."""
    return get_market_brief()


def send_morning_brief():
    """
    Verschickt das tägliche Marktbriefing per E-Mail vor dem ersten Entry-Slot
    (siehe main(): Scheduler-Job um 08:30 ET, vor dem 09:45-ET-Slot).
    """
    brief = generate_morning_market_brief()
    regime = get_market_regime()
    regime_emoji = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(regime, "➡️")

    subject = f"🌅 Trading Bot – Marktbriefing {datetime.now().strftime('%d.%m.%Y')}"
    body = f"""
Guten Morgen!

Markt-Regime: {regime_emoji} {regime.upper()}

{brief}

──────────────────────────
Trading Bot startet heute um 09:45 ET.
"""
    send_email(subject, body)
    print("✅ Morning Brief gesendet")


def log_scan_results(signals: list, slot_et: str, executed_trades: dict, guardrail_reasons: dict = None):
    """
    Loggt jedes Scan-Ergebnis (auch nicht ausgeführte Ticker) in scan_log,
    damit im Dashboard nachvollziehbar ist, warum ein Ticker gehandelt wurde
    oder nicht (siehe Feature Scan-Log). executed_trades: Ticker -> Trade-ID.
    guardrail_reasons: Ticker -> Grund (siehe run_entry_cycle), None falls kein
    Guardrail griff (KO'd oder unter Schwellwert liegende Ticker erreichen die
    Guardrail-Prüfung ohnehin nie).
    """
    guardrail_reasons = guardrail_reasons or {}
    scan_time = datetime.utcnow()
    active_broker = get_live_config().get("ACTIVE_BROKER", "alpaca")
    with get_session() as session:
        for signal in signals:
            trade_id = executed_trades.get(signal.ticker)
            breakdown = signal.score_breakdown or {}

            log_entry = ScanLog(
                scan_time    = scan_time,
                slot_et      = slot_et,
                ticker       = signal.ticker,
                score        = signal.score,
                approved     = signal.approved,
                instrument_type = signal.instrument_type,
                current_price   = signal.current_price,
                rsi          = signal.rsi,
                rsi_score    = breakdown.get("rsi", {}).get("score"),
                sma_score    = breakdown.get("sma_trend", {}).get("score"),
                volume_score = breakdown.get("volume", {}).get("score"),
                pe_score     = breakdown.get("pe_ratio", {}).get("score"),
                de_score     = breakdown.get("debt_equity", {}).get("score"),
                rev_score    = breakdown.get("revenue_growth", {}).get("score"),
                ko_reason    = signal.ko_reason,
                guardrail_reason = guardrail_reasons.get(signal.ticker),
                trade_executed = trade_id is not None,
                trade_id     = trade_id,
                mode         = TRADING_MODE,
                market_regime = signal.market_regime,
                fair_value_avg          = signal.fair_value_avg,
                fair_value_discount_pct = signal.fair_value_discount,
                broker       = active_broker,
            )
            session.add(log_entry)
        session.commit()


def get_connected_user_ids() -> list[int]:
    """
    Multi-Tenant-Handelsloop (2026-07-30): alle Nutzer, für die run_entry_cycle/
    run_monitoring_cycle eigenständig handeln/überwachen sollen. DEFAULT_USER_ID
    (Daniel) ist IMMER enthalten, an erster Stelle (bestehendes Verhalten bleibt
    exakt erhalten, egal ob er je eigene Keys verbindet) – zusätzlich jeder in
    get_connected_alpaca_users() gefundene Nutzer, dedupliziert gegen
    DEFAULT_USER_ID (der könnte theoretisch selbst dort auftauchen, falls Daniel
    irgendwann eigene Keys hinterlegt).
    """
    other_ids = [u["id"] for u in get_connected_alpaca_users() if u["id"] != DEFAULT_USER_ID]
    return [DEFAULT_USER_ID] + other_ids


def calculate_max_trades_today(user_id: int = DEFAULT_USER_ID) -> int:
    """
    Ersetzt die feste MAX_TRADES_PER_DAY-Grenze: berechnet täglich neu, wie
    viele neue Trades für EINEN Nutzer tatsächlich möglich sind – begrenzt durch
    das noch verfügbare KONFIGURIERTE Kapital (MAX_CAPITAL_TOTAL - bereits
    investiert), durch die Anzahl noch freier offener Positionen
    (MAX_OPEN_POSITIONS) UND (AUFGABE 4, 2026-07-30) zusätzlich hart durch das
    ECHTE, gerade jetzt bei Alpaca verfügbare Kapital dieses Nutzers (GET
    /v2/account cash) – ein zu hoch konfiguriertes Limit kann dieses reale
    Limit NICHT überschreiben. user_id=DEFAULT_USER_ID hält jeden bestehenden
    Aufrufer (z.B. dashboard.py) unverändert.

    Liegt real_cash unter dem noch offenen konfigurierten Budget, wird das klar
    geloggt ("konfiguriertes Limit übersteigt echtes Kapital") statt einfach
    stillschweigend 0 zurückzugeben – der Aufrufer (run_entry_cycle) übersetzt
    ein daraus resultierendes erlaubt=0 in einen sauberen Skip für GENAU diesen
    Nutzer (kein Guardrail-Exception-Pfad nötig, siehe dortige Doku) und handelt
    unabhängig davon für alle anderen Nutzer normal weiter.

    KRITISCHER BUGFIX 2026-08-04 (zweite Iteration, siehe broker.
    get_effective_max_capital_total_bot_costbasis-Docstring für die volle
    Begründung): max_capital_total kam bisher aus get_effective_max_capital_
    total_bot() (equity-basiert, Cash + MARKTWERT offener Positionen),
    während `invested` direkt darunter cost-basis-basiert ist
    (get_total_capital_in_trades) – bei unrealisiertem Gewinn/Verlust liefen
    beide Größen auseinander, wodurch available_capital je nach Vorzeichen
    über- oder unterschätzt wurde (hier: überschätzt, da der harte real_cash-
    Clamp direkt danach das praktisch abgefangen hat – anders als beim
    strukturell selben Bug im AUFGABE-4-Check in check_guardrails(), der
    dadurch komplett blockierte). Fix: max_capital_total kommt jetzt aus
    get_effective_max_capital_total_bot_costbasis() (cash + invested statt
    equity) – dieselbe Bemessungsgrundlage wie `invested` direkt darunter,
    real_snapshot dafür VOR statt nach der available_capital-Berechnung
    abgerufen (wird jetzt zweimal gebraucht: einmal für max_capital_total,
    einmal für den bestehenden real_cash-Clamp).
    """
    config = get_user_live_config(user_id)
    max_per_trade = float(config.get("MAX_CAPITAL_PER_TRADE", 50))
    max_open = int(config.get("MAX_OPEN_POSITIONS", 5))

    with get_session() as session:
        invested = get_total_capital_in_trades(session, user_id)
        current_open = len(get_open_trades(session, user_id))

    real_snapshot = get_alpaca_account_snapshot(user_id)
    if real_snapshot is not None:
        # Aufgabe "Kapital-Einstellungen Prozent-Umbau": für DEFAULT_USER_ID
        # (Daniel) ersetzt der Prozent-Anteil vom echten (cost-basis-
        # bemessenen) Gesamtkapital den alten statischen Wert, andere Nutzer
        # behalten unverändert ihr eigenes UserBotConfig.
        max_capital_total = get_effective_max_capital_total_bot_costbasis(user_id, real_snapshot, invested)
    else:
        # Fail-safe (unverändert wie schon immer): Alpaca gerade nicht
        # erreichbar -> rein konfigurationsbasiert weiterrechnen (equity-
        # basierte Variante als bester bekannter Fallback-Wert), statt den
        # Handel für alle Nutzer auszusetzen nur weil ein einzelner Status-
        # Abruf fehlschlug.
        max_capital_total = get_effective_max_capital_total_bot(user_id)

    available_capital = max_capital_total - invested

    if real_snapshot is not None:
        real_cash = real_snapshot["cash"]
        if real_cash < available_capital:
            print(f"⚠️  Nutzer {user_id}: konfiguriertes Limit übersteigt echtes Kapital "
                  f"(konfiguriert verfügbar: ${available_capital:.2f}, echtes Cash bei Alpaca: ${real_cash:.2f}) "
                  f"– echtes Kapital gilt als harte Obergrenze.")
        available_capital = min(available_capital, real_cash)

    max_by_capital = int(available_capital / max_per_trade) if max_per_trade > 0 else 0
    max_by_positions = max_open - current_open

    return max(0, min(max_by_capital, max_by_positions))


def get_trades_for_slot(slot: EntryTimeSlot, user_id: int = DEFAULT_USER_ID) -> int:
    """
    Dynamische Slot-Verteilung (ersetzt festes "Konservatives Frühbudget"-Cap):
    das verbleibende Tagesbudget EINES Nutzers wird gleichmäßig (aufgerundet)
    auf die noch verbleibenden aktiven Slots ab diesem Slot verteilt, statt
    frühen Slots das gesamte Restbudget zu überlassen. slot.max_trades_per_slot
    bleibt als optionale Obergrenze bestehen (GLOBAL, gilt für alle Nutzer
    gleich – der Zeitplan selbst ist nicht Teil dieses Auftrags pro Nutzer
    konfigurierbar).

    WICHTIG: calculate_max_trades_today(user_id) ist bereits das LIVE kapital-/
    positions-/echtkapital-bereinigte Restbudget DIESES Nutzers (berechnet aus
    SUM(capital_used) seiner aktuell offenen Trades, siehe AUFGABE 4) – heute
    bereits ausgeführte Trades sind darüber schon vollständig eingepreist. Ein
    zusätzlicher Abzug der heutigen Trade-Anzahl (früher: `daily_count`-
    Parameter) wäre ein Doppelabzug: einmal implizit über das reduzierte
    verfügbare Kapital, einmal explizit über die Trade-Anzahl – das hat real
    noch mögliche Trades blockiert.
    """
    restbudget = calculate_max_trades_today(user_id)

    if restbudget <= 0:
        return 0

    with get_session() as session:
        remaining_slots = session.query(EntryTimeSlot).filter(
            EntryTimeSlot.aktiv == True,
            EntryTimeSlot.stunde_et * 60 + EntryTimeSlot.minute_et >=
            slot.stunde_et * 60 + slot.minute_et
        ).count()

    if remaining_slots <= 0:
        return restbudget

    trades_this_slot = math.ceil(restbudget / remaining_slots)

    if slot.max_trades_per_slot is not None:
        trades_this_slot = min(trades_this_slot, slot.max_trades_per_slot)

    return trades_this_slot


def run_entry_cycle(slot: EntryTimeSlot):
    """
    Entry-Zyklus für einen einzelnen Zeitslot (siehe entry_time_slots /
    schedule_entry_jobs). Wie der frühere run_bot_cycle, aber nur Signal-Scan +
    Trade-Platzierung – kein SL/TP-Monitoring (läuft separat, siehe
    run_monitoring_cycle alle MONITORING_INTERVAL_MIN Minuten).

    Multi-Tenant-Handelsloop (2026-07-30): der Markt-Scan (Schritt 4) läuft
    EINMAL zentral – Kandidaten/Scores sind für alle Nutzer identisch (siehe
    config.DEFAULT_USER_ID-Docstring, warum SL/TP/Score-Berechnung bewusst
    nicht pro Nutzer divergieren). Ab Schritt 5 (Guardrail-Prüfung + Order-
    Platzierung) läuft die Kandidatenliste dagegen für JEDEN verbundenen
    Nutzer (get_connected_user_ids()) unabhängig durch – eigene Guardrails,
    eigener Alpaca-Client, eigene trades-Zeilen (siehe broker.check_guardrails/
    place_trade). Ein Fehler oder ein zu knappes Kapital bei EINEM Nutzer darf
    NIE die anderen Nutzer im selben Zyklus beeinträchtigen (AUFGABE 4) – siehe
    die try/except-Grenze pro Nutzer unten. VIX-Check, Fair-Value-Vorfilter und
    das Scan-Log/Snapshot/Heartbeat am Ende bleiben bewusst wie bisher an
    DEFAULT_USER_ID/global verankert (Dashboard/Scan-Historie-UI ist nicht Teil
    dieses Auftrags – nur der Handelsloop selbst wurde multi-tenant-fähig gemacht).
    """
    print(f"\n{'='*60}")
    print(f"🤖 Entry-Zyklus {slot.stunde_et:02d}:{slot.minute_et:02d} ET gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 0. Bot pausiert?
    with get_session() as session:
        if BotState.get(session, "bot_paused") == "true":
            print("⏸️  Bot ist pausiert. Kein Handel in diesem Slot.")
            return

    # 0.5 Alpaca Drain Mode: solange Alpaca der aktive Broker ist UND Drain
    # Mode an ist, werden keine neuen Positionen mehr eröffnet – bestehende
    # laufen unverändert weiter (Monitoring/SL/TP/Trailing/Time-Exit läuft
    # IMMER, siehe run_monitoring_cycle/broker.monitor_open_positions, das ist
    # bewusst NICHT an dieses Gate gekoppelt). Ist IBKR der aktive Broker,
    # greift das Gate nicht – neue Trades laufen dann über IBKR.
    cfg = get_live_config()
    alpaca_drain = cfg.get("ALPACA_DRAIN_MODE", "false").lower() == "true"
    active_broker = cfg.get("ACTIVE_BROKER", "alpaca")
    if alpaca_drain and active_broker == "alpaca":
        print("⏸️  Alpaca Drain Mode: Keine neuen Trades. Bestehende Positionen werden weiter gemanagt.")
        return

    # 1. VIX-Check (Marktangst-Filter)
    vix, vix_ok = check_vix()
    print(f"\n📊 VIX: {vix:.1f}", end=" ")
    if not vix_ok:
        print(f"🚨 ÜBER LIMIT – Slot übersprungen (VIX > Schwellwert)")
        return
    print(f"✅ Im grünen Bereich")

    # 2. Portfolio-Status
    portfolio_value = get_portfolio_value()
    print(f"💼 Portfolio-Wert: ${portfolio_value:.2f} (Start: ${MAX_CAPITAL_TOTAL:.2f})")

    # Profit-Alert prüfen
    if portfolio_value >= PROFIT_ALERT_TARGET:
        print(f"\n🎯 PROFIT-ALERT: Portfolio hat ${PROFIT_ALERT_TARGET:.2f} erreicht!")
        print(f"   → Empfehlung: ${MAX_CAPITAL_TOTAL:.2f} entnehmen (Startkapital zurück)")
        send_email(
            subject="🎯 Trading Bot – Profit-Alert",
            body=(
                f"Portfolio-Wert: ${portfolio_value:.2f}\n"
                f"Ziel erreicht: ${PROFIT_ALERT_TARGET:.2f}\n\n"
                f"Empfehlung: ${MAX_CAPITAL_TOTAL:.2f} entnehmen (Startkapital zurück)."
            )
        )

    # 3. Budget für diesen Slot bestimmen (DEFAULT_USER_ID, für die
    # unveränderten Scan-Log/Dashboard-Variablen unten) – jeder andere
    # verbundene Nutzer bekommt sein eigenes Budget weiter unten in der
    # Multi-Tenant-Schleife (Schritt 5) berechnet, nicht hier.
    with get_session() as session:
        max_trades_today = calculate_max_trades_today(DEFAULT_USER_ID)
        trades_heute = get_daily_trade_count(session, DEFAULT_USER_ID)

    erlaubt = get_trades_for_slot(slot, DEFAULT_USER_ID)

    print(f"🎯 Slot-Budget: {erlaubt} Trade(s) (Cap {slot.max_trades_per_slot}, "
          f"Restbudget {max_trades_today} weitere(r) Trade(s) laut Kapital/Positionen, heute bereits {trades_heute} ausgeführt)")
    budget_exhausted = erlaubt <= 0
    if budget_exhausted:
        print(f"⏭️  Slot {slot.stunde_et}:{slot.minute_et:02d} – kein Budget "
              f"(Scan läuft trotzdem weiter und wird geloggt, nur der Kauf entfällt)")

    # 4. Watchlists scannen
    # Fair-Value-Vorfilter (Stufe 1) VOR dem teuren Marktdaten-Scan anwenden:
    # nur Ticker, die im wöchentlichen Fair-Value-Cache bereits als unterbewertet
    # markiert sind (siehe fair_value.py), plus die Short-Instrumente (die haben
    # kein KGV/Cashflow und werden vom Fair-Value-Gatekeeper nicht erfasst).
    # Ticker ohne Cache-Eintrag (noch nicht berechnet) werden hier ausgeschlossen,
    # bekommen ihre Chance aber automatisch nach dem nächsten Montags-Lauf von
    # update_fair_value_cache().
    print(f"\n--- Signal-Scan ---")
    fv_undervalued = {t[0] for t in get_undervalued_tickers()}
    all_tickers = LONG_WATCHLIST + ACTIVE_SHORT_INSTRUMENTS
    scan_list = [
        t for t in all_tickers
        if t in fv_undervalued or t in ACTIVE_SHORT_INSTRUMENTS
    ]
    print(f"📊 {len(all_tickers)} Titel analysiert, {len(scan_list)} im aktiven Scan (Fair Value Vorfilter)")
    signals = scan_watchlist_parallel(scan_list, max_workers=15)

    # Beste Kandidaten zuerst (höchster Score) – ein Trade-Slot soll dem
    # stärksten Signal zugutekommen, nicht einfach dem ersten in
    # Watchlist-Reihenfolge (siehe FIX Slot-Cap).
    approved = sorted((s for s in signals if s.approved), key=lambda s: s.score, reverse=True)
    print(f"\n✅ {len(approved)} Trade-Signale über Schwellwert:")

    # 5. Für jeden verbundenen Nutzer unabhängig: für jedes freigegebene Signal
    # (bis zum jeweils EIGENEN Slot-Kontingent) Guardrail zuerst prüfen (spart
    # LLM-Aufrufe für ohnehin geblockte Kandidaten), dann LLM + Trade. WICHTIG
    # (FIX Slot-Cap): nur ein tatsächlich AUSGEFÜHRTER Trade verbraucht das
    # Slot-Kontingent – ein von einem Guardrail geblockter Kandidat (z.B.
    # "Position auf TICKER bereits offen", was nur DIESEN einen Ticker
    # betrifft) darf keinen Slot verbrauchen und nicht verhindern, dass der
    # nächste Kandidat noch versucht wird.
    #
    # llm_cache: LLM-Analyse eines Kandidaten ist nutzerunabhängig (derselbe
    # Ticker, derselbe Marktkontext) – wird beim ersten Nutzer, der ihn kauft,
    # einmalig berechnet und für alle weiteren Nutzer wiederverwendet statt pro
    # Nutzer erneut abgerufen zu werden (spart LLM-Kosten/Latenz).
    llm_cache: dict = {}
    results_by_user: dict = {}
    connected_user_ids = get_connected_user_ids()
    if len(connected_user_ids) > 1:
        print(f"\n👥 Multi-Tenant: {len(connected_user_ids)} verbundene Nutzer in diesem Zyklus "
              f"({', '.join(str(u) for u in connected_user_ids)})")

    for user_id in connected_user_ids:
        # Jeder Nutzer läuft in seiner eigenen try/except-Grenze: ein
        # unerwarteter Fehler bei EINEM Nutzer (z.B. Netzwerkproblem mit
        # dessen individuellem Alpaca-Client) darf niemals die Verarbeitung
        # der ANDEREN Nutzer im selben Zyklus verhindern (AUFGABE 4-Prinzip).
        try:
            user_erlaubt = erlaubt if user_id == DEFAULT_USER_ID else get_trades_for_slot(slot, user_id)
            user_executed_trades = []
            user_guardrail_reasons = {}
            user_trades_in_slot = 0
            verlustlimit_alert_gesendet = False

            for signal in approved:
                if user_trades_in_slot >= user_erlaubt:
                    break

                # check_guardrails() fragt open_trades/daily_trade_count/daily_pnl
                # bei jedem Aufruf frisch aus der DB ab (pro Nutzer gefiltert) –
                # reflektiert also automatisch bereits in diesem Zyklus
                # ausgeführte Trades DIESES Nutzers, keine veralteten Werte.
                try:
                    check_guardrails(signal, user_id)
                except GuardrailViolation as gv:
                    user_guardrail_reasons[signal.ticker] = str(gv)
                    print(f"   🛡️  Nutzer {user_id}, {signal.ticker}: Guardrail – {gv}")
                    if "Verlustlimit" in str(gv) and not verlustlimit_alert_gesendet:
                        send_email(
                            subject="🛑 Trading Bot – Daily Loss Limit erreicht",
                            body=(
                                f"{gv}\n\n"
                                f"Nutzer: {user_id}\n"
                                f"Der Bot wurde automatisch pausiert und handelt erst nach "
                                f"manueller Freigabe wieder."
                            )
                        )
                        verlustlimit_alert_gesendet = True
                    continue  # NICHT trades_in_slot erhöhen – geblockter Kandidat verbraucht keinen Slot

                # Portfolio-Segmentierung: Anteil volatiler Titel (VOLATILE_WATCHLIST)
                # an den offenen Positionen DIESES Nutzers begrenzen bzw. gezielt
                # auffüllen (siehe Feature Portfolio-Segmentierung). Frisch pro
                # Kandidat berechnet, damit bereits in diesem Zyklus ausgeführte
                # Trades berücksichtigt sind. VOLATILE_SEGMENT_PCT bleibt bewusst
                # global (nicht Teil der Pro-Nutzer-Guardrails in AUFGABE 1).
                seg_config = get_live_config()
                volatile_target = float(seg_config.get("VOLATILE_SEGMENT_PCT", 0.33))

                with get_session() as seg_session:
                    open_trades_all = get_open_trades(seg_session, user_id)
                total_open = len(open_trades_all)
                volatile_open = sum(1 for t in open_trades_all if t.ticker in VOLATILE_WATCHLIST)
                volatile_ratio = (volatile_open / total_open) if total_open > 0 else 0

                if signal.ticker in VOLATILE_WATCHLIST:
                    if volatile_ratio > volatile_target + 0.15:
                        # Zu viele volatile Titel offen: diesen Kandidaten blockieren
                        user_guardrail_reasons[signal.ticker] = f"Volatile Segment voll ({volatile_ratio*100:.0f}%)"
                        print(f"   🛡️  Nutzer {user_id}, {signal.ticker}: Volatile Segment voll ({volatile_ratio*100:.0f}%)")
                        continue  # NICHT trades_in_slot erhöhen
                    elif volatile_ratio < volatile_target:
                        # Volatiles Segment unterrepräsentiert: Score-Bonus
                        signal.score += 5
                        print(f"   📊 Nutzer {user_id}, {signal.ticker}: Volatile Segment unterrepräsentiert ({volatile_ratio*100:.0f}%) – Score +5")

                print(f"\n--- Nutzer {user_id}, Trade-Kandidat: {signal.ticker} (Score: {signal.score}/100) ---")

                # LLM-Analyse (non-blocking – Bot läuft weiter bei Fehler),
                # gecacht pro Ticker über alle Nutzer hinweg (siehe llm_cache oben).
                if signal.ticker not in llm_cache:
                    print(f"🧠 LLM-Analyse für {signal.ticker}...")
                    llm_cache[signal.ticker] = analyze_with_llm(signal)
                llm_result = llm_cache[signal.ticker]

                if llm_result.get("summary"):
                    print(f"   Summary: {llm_result['summary'][:100]}...")
                if llm_result.get("risks"):
                    for r in llm_result["risks"]:
                        print(f"   ⚠️  {r}")

                # Trade platzieren (Guardrails werden intern nochmal geprüft – Sicherheitsnetz
                # falls sich der Zustand zwischen Vor-Check und Order-Platzierung ändert).
                try:
                    trade = place_trade(signal, llm_result, user_id)
                    if trade:
                        user_executed_trades.append(trade)
                        user_trades_in_slot += 1  # Nur hier erhöhen – ein echter Trade wurde ausgeführt
                        print(f"   ✅ Nutzer {user_id}: Trade #{trade.id} ausgeführt ({user_trades_in_slot}/{user_erlaubt})")
                except GuardrailViolation as gv:
                    user_guardrail_reasons[signal.ticker] = str(gv)
                    print(f"   🛡️  Nutzer {user_id}: Guardrail – {gv}")
                    if "Verlustlimit" in str(gv) and not verlustlimit_alert_gesendet:
                        send_email(
                            subject="🛑 Trading Bot – Daily Loss Limit erreicht",
                            body=(
                                f"{gv}\n\n"
                                f"Nutzer: {user_id}\n"
                                f"Der Bot wurde automatisch pausiert und handelt erst nach "
                                f"manueller Freigabe wieder."
                            )
                        )
                        verlustlimit_alert_gesendet = True

            results_by_user[user_id] = {
                "executed_trades": user_executed_trades,
                "guardrail_reasons": user_guardrail_reasons,
                "trades_in_slot": user_trades_in_slot,
                "erlaubt": user_erlaubt,
                "budget_exhausted": user_erlaubt <= 0,
            }

        except Exception as e:
            # Nicht als GuardrailViolation gefangene, echte Fehler (z.B. ein
            # unerwarteter API-Fehler) dürfen NIE den gesamten Entry-Zyklus für
            # ALLE Nutzer abbrechen (AUFGABE 4) – klare Log-/Alarm-Meldung,
            # dann normal weiter zum nächsten Nutzer.
            print(f"🚨 Nutzer {user_id}: unerwarteter Fehler im Entry-Zyklus ({e}) – "
                  f"dieser Nutzer wird für diesen Zyklus übersprungen, andere Nutzer nicht betroffen.")
            send_email(
                subject=f"🚨 Trading Bot – Fehler im Entry-Zyklus (Nutzer {user_id})",
                body=f"{e}\n\nNutzer {user_id} wurde in diesem Zyklus übersprungen. Andere Nutzer liefen normal weiter."
            )
            results_by_user[user_id] = {
                "executed_trades": [], "guardrail_reasons": {}, "trades_in_slot": 0,
                "erlaubt": 0, "budget_exhausted": True,
            }

    # Bestehende Variablen für Scan-Log/Dashboard (siehe Schritt 6 unten)
    # bleiben exakt an DEFAULT_USER_IDs Ergebnis gebunden – keine Änderung an
    # der (nicht Teil dieses Auftrags) Single-Tenant-Scan-Historie-UI.
    default_result = results_by_user.get(DEFAULT_USER_ID, {
        "executed_trades": [], "guardrail_reasons": {}, "trades_in_slot": 0, "erlaubt": erlaubt, "budget_exhausted": budget_exhausted,
    })
    executed_trades = default_result["executed_trades"]
    guardrail_reasons = default_result["guardrail_reasons"]
    trades_in_slot = default_result["trades_in_slot"]

    total_executed = sum(len(r["executed_trades"]) for r in results_by_user.values())
    if len(connected_user_ids) > 1:
        summary = ", ".join(f"Nutzer {uid}: {len(r['executed_trades'])}" for uid, r in results_by_user.items())
        print(f"\n👥 Multi-Tenant-Zusammenfassung dieses Slots – {total_executed} Trade(s) insgesamt ({summary})")

    # Kandidaten, die wegen erreichtem Slot-Kontingent gar nicht mehr geprüft
    # wurden (Schleife oben per break beendet), bekommen fürs Scan-Log trotzdem
    # einen nachvollziehbaren Grund statt eines leeren guardrail_reason.
    # BUDGET_EXHAUSTED (kein Restbudget/keine freie Position laut
    # calculate_max_trades_today) wird dabei vom normalen Slot-Cap
    # unterschieden, damit im Dashboard erkennbar bleibt, ob überhaupt kein
    # Kapital mehr verfügbar war oder nur dieser einzelne Slot voll ist.
    executed_tickers = {t.ticker for t in executed_trades}
    for signal in approved:
        if signal.ticker not in guardrail_reasons and signal.ticker not in executed_tickers:
            guardrail_reasons[signal.ticker] = (
                "BUDGET_EXHAUSTED" if budget_exhausted
                else f"Slot-Cap erreicht ({trades_in_slot}/{erlaubt} für diesen Slot)"
            )

    # 6. Scan-Ergebnisse loggen (auch nicht ausgeführte Ticker, siehe Feature Scan-Log)
    executed_trades_by_ticker = {t.ticker: t.id for t in executed_trades}
    slot_label = f"{slot.stunde_et:02d}:{slot.minute_et:02d}"
    log_scan_results(signals, slot_label, executed_trades_by_ticker, guardrail_reasons)

    # 7. Tages-Snapshot speichern – EIN Snapshot PRO verbundenem Nutzer (Fix
    # 2026-08-04, Multi-Tenant-Performance/-Benchmark, siehe database.DailyLog/
    # save_daily_snapshot-Docstring; vorher nur Daniels globaler Snapshot).
    # Daniels bereits oben berechneter portfolio_value wird wiederverwendet
    # (kein doppelter Alpaca-Call), für alle anderen verbundenen Nutzer wird
    # er separat abgerufen. Jeder Nutzer in eigener try/except-Grenze
    # (AUFGABE-4-Prinzip, siehe oben) – ein Fehler bei einem Nutzer darf die
    # Snapshots der anderen nicht verhindern.
    for uid in connected_user_ids:
        try:
            user_portfolio_value = portfolio_value if uid == DEFAULT_USER_ID else get_portfolio_value(uid)
            with get_session() as session:
                save_daily_snapshot(session, uid, user_portfolio_value)
                session.commit()
        except Exception as e:
            print(f"🚨 Nutzer {uid}: Tages-Snapshot fehlgeschlagen ({e}) – andere Nutzer nicht betroffen.")

    # 8. Heartbeat (Aufgabe 1, 2026-07-30, siehe database.BotHeartbeat) – der
    # externe Watchdog erkennt daran, dass dieser Zyklus tatsächlich
    # durchgelaufen ist, unabhängig davon ob dabei Trades ausgeführt wurden.
    with get_session() as session:
        BotHeartbeat.touch(session, "alpaca", "entry")
        session.commit()

    print(f"\n{'='*60}")
    print(f"✅ Entry-Zyklus abgeschlossen. Trades in diesem Slot: {len(executed_trades)}")
    print(f"{'='*60}\n")


def schedule_entry_jobs(scheduler: BlockingScheduler, et_tz):
    """
    Registriert für jeden aktiven entry_time_slot einen eigenen Scheduler-Job
    (ersetzt die frühere feste Scan-Zeit 09:00 ET, siehe Feature 6).
    """
    with get_session() as session:
        slots = get_active_entry_time_slots(session)

    if not slots:
        print("⚠️  Keine aktiven Entry-Zeitslots gefunden – kein Entry-Job registriert.")
        return

    for slot in slots:
        scheduler.add_job(
            lambda s=slot: run_entry_cycle(s),
            CronTrigger(
                hour=slot.stunde_et,
                minute=slot.minute_et,
                day_of_week="mon-fri",
                timezone=et_tz,
            ),
            id=f"entry_{slot.id}",
            name=f"Entry-Zyklus {slot.stunde_et:02d}:{slot.minute_et:02d} ET",
            replace_existing=True,
        )
        print(f"   📍 Entry-Job registriert: {slot.stunde_et:02d}:{slot.minute_et:02d} ET (Gewichtung {slot.gewichtung})")


def init_fair_value_if_empty():
    """
    Stufe-1-Gatekeeper (siehe fair_value.py) braucht einen gefüllten Cache
    bevor der erste Entry-Zyklus läuft – ansonsten würde jeder Ticker beim
    allerersten Start mangels fair_value_cache-Eintrag ungefiltert
    durchgereicht. Läuft daher einmalig beim Bot-Start, falls die Tabelle
    noch komplett leer ist (der reguläre Betrieb aktualisiert wöchentlich
    montags, siehe schedule_entry_jobs/main).
    """
    with get_session() as session:
        count = session.query(FairValueCache).count()
    if count == 0:
        print("Fair Value Cache leer → initialer Update...")
        update_fair_value_cache(LONG_WATCHLIST + ACTIVE_SHORT_INSTRUMENTS)


def run_monitoring_cycle():
    """
    Leichtgewichtiger Zyklus: Nur SL/TP überwachen (alle 30 Min während Handelszeit).
    Seit 2026-07-30 zusätzlich: Positions-Konsistenz-Watchdog (Aufgabe 3,
    eigenständige Funktion in broker.py, siehe dort) und Heartbeat (Aufgabe 1).

    Multi-Tenant (2026-07-30): läuft für JEDEN verbundenen Nutzer einzeln
    (eigene Positionen, eigener Alpaca-Client) – ein Fehler bei einem Nutzer
    darf die anderen nicht vom Monitoring/Watchdog ausschließen (analog zum
    Prinzip in run_entry_cycle).
    """
    for user_id in get_connected_user_ids():
        try:
            monitor_open_positions(user_id)
            check_position_consistency(user_id)
        except Exception as e:
            print(f"🚨 Nutzer {user_id}: unerwarteter Fehler im Monitoring-Zyklus ({e}) – "
                  f"andere Nutzer nicht betroffen.")
            send_email(
                subject=f"🚨 Trading Bot – Fehler im Monitoring-Zyklus (Nutzer {user_id})",
                body=f"{e}"
            )
    with get_session() as session:
        BotHeartbeat.touch(session, "alpaca", "monitoring")
        session.commit()


def saxo_token_refresh_job():
    """
    Proaktiver Saxo-Token-Refresh alle 10 Minuten (siehe saxo_client.py) –
    Access Token ist nur ~19,5 Min gültig, Refresh Token ~59,5 Min. Der
    10-Minuten-Puffer erlaubt mehrere fehlgeschlagene Versuche, bevor der
    Refresh Token selbst abläuft und ein manueller OAuth-Login nötig wird.
    Fehler werden hier nur geloggt (die Warn-E-Mail bei einem echten
    Refresh-Fehlschlag verschickt bereits saxo_client.refresh_saxo_token selbst).
    """
    try:
        get_valid_access_token()
    except Exception as e:
        print(f"⚠️  Saxo Token-Refresh-Job fehlgeschlagen: {e}")


def main():
    """Startet den Scheduler."""
    print("🚀 Trading Bot startet...")

    # Konfiguration validieren
    warnings = validate_config()
    for w in warnings:
        print(f"⚠️  Config-Warnung: {w}")

    # Datenbank initialisieren
    init_db()

    # Monitoring-Intervall dynamisch aus DB lesen (nach init_db, damit
    # bot_config sicher existiert; Fallback 15 Min via get_live_config).
    monitoring_interval = get_live_config().get("MONITORING_INTERVAL_MIN", 15)

    # Scheduler konfigurieren (Eastern Time)
    et_tz = pytz.timezone("America/New_York")
    scheduler = BlockingScheduler(timezone=et_tz)

    # Entry-Zyklen: ein Job pro aktivem entry_time_slot (siehe Feature 2/6),
    # ersetzt die frühere feste Scan-Zeit 09:00 ET.
    print("📍 Entry-Zeitslots registrieren...")
    schedule_entry_jobs(scheduler, et_tz)

    # Morning Brief: täglich 08:30 ET, vor dem ersten Entry-Slot (09:45 ET).
    scheduler.add_job(
        send_morning_brief,
        CronTrigger(
            hour=8, minute=30,
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="morning_brief",
        name="Morning Market Brief",
        replace_existing=True
    )

    # Monitoring: alle N Minuten während Handelszeit (09:30–16:00 ET),
    # Intervall konfigurierbar via bot_config (MONITORING_INTERVAL_MIN).
    scheduler.add_job(
        run_monitoring_cycle,
        CronTrigger(
            hour="9-16",
            minute=f"*/{monitoring_interval}",
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="monitor_cycle",
        name="SL/TP Monitoring"
    )

    # Post-Exit-Tracking Update: täglich 05:00 ET (vor Handelsbeginn und vor
    # dem montäglichen Backlook 06:00 ET), füllt price_after_5/10_days für
    # post_exit_tracking-Zeilen deren Beobachtungsfenster abgelaufen ist
    # (siehe post_exit_tracking.py – Schwellenwert-Wirksamkeitsprüfung).
    scheduler.add_job(
        update_pending_tracking,
        CronTrigger(
            hour=5, minute=0,
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="post_exit_tracking_update",
        name="Post-Exit-Tracking Update",
        replace_existing=True
    )

    # Positions-Snapshot: 16:05 ET, kurz NACH dem letzten Monitoring-Zyklus des
    # Tages (der bis 16:00 ET läuft, siehe run_monitoring_cycle oben) – dient
    # als Vorabend-Vergleichsbasis für die Tages-Mail des nächsten Tages
    # (siehe database.DailyPositionSnapshot/capture_daily_position_snapshot).
    scheduler.add_job(
        capture_daily_position_snapshot,
        CronTrigger(
            hour=16, minute=5,
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="daily_position_snapshot",
        name="Tages-Positions-Snapshot (Vergleichsbasis)",
        replace_existing=True
    )

    # Tages-Zusammenfassung per E-Mail: 16:10 ET, NACH dem Positions-Snapshot
    # oben (ersetzt das frühere Feuern am Ende des letzten Entry-Slots, das vor
    # Handelsschluss lag und noch keinen "heute Abend"-Snapshot hatte).
    scheduler.add_job(
        send_daily_summary_email,
        CronTrigger(
            hour=16, minute=10,
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="daily_summary_email",
        name="Tages-Zusammenfassung E-Mail",
        replace_existing=True
    )

    # Wöchentlicher Backlook: Montags 06:00 ET, vor dem Haupt-Zyklus (Option A Selbstlern)
    scheduler.add_job(
        run_backlook,
        CronTrigger(
            hour=6,
            minute=0,
            day_of_week="mon",
            timezone=et_tz
        ),
        id="weekly_backlook",
        name="Wöchentlicher Backlook"
    )

    # Fair Value Update: montags 08:00 ET, vor dem ersten Entry-Slot (09:45 ET)
    # – Stufe 1 (WAS kaufen?) muss vor Stufe 2 (WANN kaufen?) aktuell sein.
    scheduler.add_job(
        lambda: update_fair_value_cache(LONG_WATCHLIST + ACTIVE_SHORT_INSTRUMENTS),
        CronTrigger(
            day_of_week="mon",
            hour=8,
            minute=0,
            timezone=et_tz
        ),
        id="fair_value_update",
        name="Wöchentliches Fair Value Update",
        replace_existing=True
    )

    # Initialer Fair-Value-Update falls Cache noch leer (z.B. erster Start).
    init_fair_value_if_empty()

    # Saxo Token Refresh: alle 10 Minuten, unabhängig von Handelszeiten (Access
    # Token ~19,5 Min gültig, Refresh Token ~59,5 Min – siehe saxo_client.py).
    scheduler.add_job(
        saxo_token_refresh_job,
        CronTrigger(minute="*/10", timezone=et_tz),
        id="saxo_token_refresh",
        name="Saxo Token Refresh (proaktiv)",
        replace_existing=True
    )

    print(f"⏰ Scheduler aktiv. Entry-Zyklen laufen zu den registrierten Zeitslots (Mo–Fr)")
    print(f"🌅 Morning Brief: täglich 08:30 ET")
    print(f"📡 Monitoring: alle {monitoring_interval} Min von 09:30–16:00 ET")
    print(f"📉 Post-Exit-Tracking Update: täglich 05:00 ET")
    print(f"📸 Positions-Snapshot: 16:05 ET")
    print(f"📊 Tages-Zusammenfassung E-Mail: 16:10 ET")
    print(f"📚 Backlook: montags 06:00 ET")
    print(f"💰 Fair Value Update: montags 08:00 ET")
    print(f"🔑 Saxo Token Refresh: alle 10 Minuten")
    print(f"🛑 Zum Beenden: Ctrl+C\n")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n🛑 Bot gestoppt.")


if __name__ == "__main__":
    main()
