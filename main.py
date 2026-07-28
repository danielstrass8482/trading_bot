"""
main.py – Orchestrierung: Scheduler startet täglich den Bot-Loop.
Ablauf: VIX-Check → Watchlist scannen → Guardrails → LLM → Trade
"""

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import math
import smtplib
import base64
from email.mime.text import MIMEText
import pytz
from sqlalchemy import text

from config import (
    LONG_WATCHLIST, ACTIVE_SHORT_INSTRUMENTS, VOLATILE_WATCHLIST,
    PROFIT_ALERT_TARGET, MAX_CAPITAL_TOTAL, TRADING_MODE,
    validate_config, get_live_config,
    ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_FALLBACK_PORT,
    SMTP_USER, SMTP_PASSWORD, SMTP_TIMEOUT
)
from database import (
    init_db, get_session, save_daily_snapshot, BotState, ScanLog,
    EntryTimeSlot, get_active_entry_time_slots, get_daily_trade_count,
    get_open_trades, FairValueCache,
)
from rule_engine import scan_watchlist_parallel, check_vix, get_market_regime, get_benchmark_performance
from llm_analyst import analyze_with_llm, get_market_brief
from broker import place_trade, monitor_open_positions, get_portfolio_value, get_bot_performance, check_guardrails, GuardrailViolation
from backlook import run_backlook
from fair_value import update_fair_value_cache, get_undervalued_tickers
from saxo_client import get_valid_access_token


def _smtp_login_utf8(server, user, password):
    """
    AUTH LOGIN von Hand, da smtplib.auth()/login() den Base64-Payload intern
    mit .encode("ascii") kodiert und damit bei Nicht-ASCII-Zeichen (Umlaute)
    im Passwort mit UnicodeEncodeError abstuerzt.
    """
    server.ehlo()
    code, resp = server.docmd("AUTH", "LOGIN")
    if code != 334:
        raise smtplib.SMTPAuthenticationError(code, resp)
    code, resp = server.docmd(base64.b64encode(user.encode("utf-8")).decode("ascii"))
    if code != 334:
        raise smtplib.SMTPAuthenticationError(code, resp)
    code, resp = server.docmd(base64.b64encode(password.encode("utf-8")).decode("ascii"))
    if code not in (235, 503):
        raise smtplib.SMTPAuthenticationError(code, resp)

def send_email(subject: str, body: str):
    """
    Verschickt eine E-Mail via smtplib (Standardbibliothek, kein externes Package).
    Fallback: Ohne ALERT_EMAIL oder SMTP-Zugangsdaten wird nur in die Logs
    geschrieben – der Bot darf dadurch nie abstürzen.

    Railway blockiert ausgehenden Port 587 (STARTTLS). Primär wird daher
    Port 465 (SMTPS/SSL) verwendet. Falls auch dieser Port blockiert wird
    (Timeout), greift ein Fallback auf SMTP_FALLBACK_PORT (Standard: 2525),
    der von Railway nicht blockiert wird. SMTP_HOST ist konfigurierbar,
    sodass später auf einen eigenen Mailserver umgestellt werden kann.
    """
    if not ALERT_EMAIL or not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        print(f"📧 [E-Mail nicht konfiguriert – nur Log] {subject}\n{body}")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ALERT_EMAIL

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            _smtp_login_utf8(server, SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
        print(f"📧 E-Mail versendet: {subject} (Port {SMTP_PORT})")
    except (TimeoutError, OSError) as e:
        print(f"⚠️  SMTP Port {SMTP_PORT} nicht erreichbar ({e}) – Fallback auf Port {SMTP_FALLBACK_PORT}")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_FALLBACK_PORT, timeout=SMTP_TIMEOUT) as server:
                _smtp_login_utf8(server, SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
            print(f"📧 E-Mail versendet: {subject} (Port {SMTP_FALLBACK_PORT})")
        except Exception as fallback_e:
            print(f"⚠️  E-Mail-Versand fehlgeschlagen (Fallback Port {SMTP_FALLBACK_PORT}): {fallback_e}")
    except Exception as e:
        print(f"⚠️  E-Mail-Versand fehlgeschlagen: {e}")


def is_last_slot_of_day(current_slot: EntryTimeSlot) -> bool:
    """
    Prüft ob current_slot der zeitlich letzte AKTIVE Einstiegszeitpunkt des
    Tages ist – steuert, wann send_daily_summary_email() feuert (siehe FIX 7:
    Tages-Mail nach dem letzten Slot statt zu einer festen Uhrzeit).
    """
    with get_session() as session:
        active_slots = session.query(EntryTimeSlot).filter_by(aktiv=True).order_by(
            EntryTimeSlot.stunde_et.desc(), EntryTimeSlot.minute_et.desc()
        ).all()
    if not active_slots:
        return True
    last = active_slots[0]
    return current_slot.stunde_et == last.stunde_et and current_slot.minute_et == last.minute_et


def send_daily_summary_email():
    """
    Verschickt EINE Tages-Zusammenfassung nach dem letzten aktiven Entry-Slot
    des Tages (siehe FIX 7) – ersetzt die frühere Pro-Slot-Mail (die nur bei
    tatsächlich ausgeführten Trades verschickt wurde), damit auch tradefreie
    Tage einen vollständigen Scan-Überblick per E-Mail bekommen.
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

        open_trades = session.execute(text("""
            SELECT ticker, entry_price, quantity, capital_used
            FROM trades WHERE status = 'OPEN'
        """)).fetchall()

    portfolio_value = get_portfolio_value()

    subject = f"📊 Trading Bot – Tageszusammenfassung {today.strftime('%d.%m.%Y')}"

    body = f"""Trading Bot – Tageszusammenfassung {today.strftime('%d.%m.%Y')}
{'='*50}

PORTFOLIO
Portfolio-Wert: ${portfolio_value:.2f}

SCAN-ÜBERSICHT
"""
    for slot in slots_heute:
        body += f"""
Slot {slot.slot_et} ET:
  Gescannt: {slot.gescannt} Ticker
  Über Schwellwert (≥65): {slot.ueber_65}
  Trades ausgeführt: {slot.trades}
  Ø Score: {slot.avg_score}
"""

    trades_heute = sum(s.trades for s in slots_heute)
    body += f"""
GESAMT HEUTE
Trades ausgeführt: {trades_heute}

OFFENE POSITIONEN
"""
    if open_trades:
        for t in open_trades:
            body += f"  {t.ticker}: {t.quantity:.4f} Stück @ ${t.entry_price:.2f}\n"
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


def calculate_max_trades_today() -> int:
    """
    Ersetzt die feste MAX_TRADES_PER_DAY-Grenze: berechnet täglich neu, wie
    viele neue Trades tatsächlich möglich sind – begrenzt durch das noch
    verfügbare Kapital (MAX_CAPITAL_TOTAL - bereits investiert) UND durch
    die Anzahl noch freier offener Positionen (MAX_OPEN_POSITIONS).
    """
    config = get_live_config()
    max_capital_total = float(config.get("MAX_CAPITAL_TOTAL", 475))
    max_per_trade = float(config.get("MAX_CAPITAL_PER_TRADE", 50))
    max_open = int(config.get("MAX_OPEN_POSITIONS", 5))

    with get_session() as session:
        invested = session.execute(text("""
            SELECT COALESCE(SUM(capital_used), 0) as invested
            FROM trades WHERE status = 'OPEN'
        """)).scalar()
        invested = float(invested or 0)
        available_capital = max_capital_total - invested

        current_open = session.execute(text("""
            SELECT COUNT(*) FROM trades WHERE status = 'OPEN'
        """)).scalar()

        max_by_capital = int(available_capital / max_per_trade) if max_per_trade > 0 else 0
        max_by_positions = max_open - int(current_open)

        return max(0, min(max_by_capital, max_by_positions))


def get_trades_for_slot(slot: EntryTimeSlot, daily_count: int) -> int:
    """
    Dynamische Slot-Verteilung (ersetzt festes "Konservatives Frühbudget"-Cap):
    das verbleibende Tagesbudget wird gleichmäßig (aufgerundet) auf die noch
    verbleibenden aktiven Slots ab diesem Slot verteilt, statt frühen Slots
    das gesamte Restbudget zu überlassen. slot.max_trades_per_slot bleibt als
    optionale Obergrenze bestehen.
    """
    max_trades_today = calculate_max_trades_today()
    restbudget = max_trades_today - daily_count

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

    # 3. Budget für diesen Slot bestimmen: das Restbudget des Tages wird
    # dynamisch auf die verbleibenden aktiven Slots verteilt statt frühen
    # Slots das gesamte Restbudget zu überlassen (siehe get_trades_for_slot).
    with get_session() as session:
        max_trades_today = calculate_max_trades_today()
        trades_heute = get_daily_trade_count(session)
        restbudget = max_trades_today - trades_heute

    erlaubt = get_trades_for_slot(slot, trades_heute)

    print(f"🎯 Slot-Budget: {erlaubt} Trade(s) (Cap {slot.max_trades_per_slot}, Restbudget {restbudget}/{max_trades_today})")
    if erlaubt <= 0:
        print(f"⏭️  Slot {slot.stunde_et}:{slot.minute_et:02d} – kein Budget")
        return

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

    # 5. Für jedes freigegebene Signal (bis zum Slot-Kontingent): Guardrail
    # zuerst prüfen (spart LLM-Aufrufe für ohnehin geblockte Kandidaten), dann
    # LLM + Trade. WICHTIG (FIX Slot-Cap): nur ein tatsächlich AUSGEFÜHRTER
    # Trade verbraucht das Slot-Kontingent – ein von einem Guardrail
    # geblockter Kandidat (z.B. "Position auf TICKER bereits offen", was nur
    # DIESEN einen Ticker betrifft) darf keinen Slot verbrauchen und nicht
    # verhindern, dass der nächste Kandidat noch versucht wird.
    executed_trades = []
    guardrail_reasons = {}
    trades_in_slot = 0
    verlustlimit_alert_gesendet = False

    for signal in approved:
        if trades_in_slot >= erlaubt:
            break

        # check_guardrails() fragt open_trades/daily_trade_count/daily_pnl bei
        # jedem Aufruf frisch aus der DB ab – reflektiert also automatisch
        # bereits in diesem Zyklus ausgeführte Trades, keine veralteten Werte.
        try:
            check_guardrails(signal)
        except GuardrailViolation as gv:
            guardrail_reasons[signal.ticker] = str(gv)
            print(f"   🛡️  {signal.ticker}: Guardrail – {gv}")
            if "Verlustlimit" in str(gv) and not verlustlimit_alert_gesendet:
                send_email(
                    subject="🛑 Trading Bot – Daily Loss Limit erreicht",
                    body=(
                        f"{gv}\n\n"
                        f"Portfolio-Wert: ${portfolio_value:.2f}\n"
                        f"Der Bot wurde automatisch pausiert und handelt erst nach "
                        f"manueller Freigabe wieder."
                    )
                )
                verlustlimit_alert_gesendet = True
            continue  # NICHT trades_in_slot erhöhen – geblockter Kandidat verbraucht keinen Slot

        # Portfolio-Segmentierung: Anteil volatiler Titel (VOLATILE_WATCHLIST)
        # an den offenen Positionen begrenzen bzw. gezielt auffüllen (siehe
        # Feature Portfolio-Segmentierung). Frisch pro Kandidat berechnet,
        # damit bereits in diesem Zyklus ausgeführte Trades berücksichtigt sind.
        seg_config = get_live_config()
        volatile_target = float(seg_config.get("VOLATILE_SEGMENT_PCT", 0.33))

        with get_session() as seg_session:
            open_trades_all = get_open_trades(seg_session)
        total_open = len(open_trades_all)
        volatile_open = sum(1 for t in open_trades_all if t.ticker in VOLATILE_WATCHLIST)
        volatile_ratio = (volatile_open / total_open) if total_open > 0 else 0

        if signal.ticker in VOLATILE_WATCHLIST:
            if volatile_ratio > volatile_target + 0.15:
                # Zu viele volatile Titel offen: diesen Kandidaten blockieren
                guardrail_reasons[signal.ticker] = f"Volatile Segment voll ({volatile_ratio*100:.0f}%)"
                print(f"   🛡️  {signal.ticker}: Volatile Segment voll ({volatile_ratio*100:.0f}%)")
                continue  # NICHT trades_in_slot erhöhen
            elif volatile_ratio < volatile_target:
                # Volatiles Segment unterrepräsentiert: Score-Bonus
                signal.score += 5
                print(f"   📊 {signal.ticker}: Volatile Segment unterrepräsentiert ({volatile_ratio*100:.0f}%) – Score +5")

        print(f"\n--- Trade-Kandidat: {signal.ticker} (Score: {signal.score}/100) ---")

        # LLM-Analyse (non-blocking – Bot läuft weiter bei Fehler)
        print(f"🧠 LLM-Analyse für {signal.ticker}...")
        llm_result = analyze_with_llm(signal)

        if llm_result.get("summary"):
            print(f"   Summary: {llm_result['summary'][:100]}...")
        if llm_result.get("risks"):
            for r in llm_result["risks"]:
                print(f"   ⚠️  {r}")

        # Trade platzieren (Guardrails werden intern nochmal geprüft – Sicherheitsnetz
        # falls sich der Zustand zwischen Vor-Check und Order-Platzierung ändert).
        try:
            trade = place_trade(signal, llm_result)
            if trade:
                executed_trades.append(trade)
                trades_in_slot += 1  # Nur hier erhöhen – ein echter Trade wurde ausgeführt
                print(f"   ✅ Trade #{trade.id} ausgeführt ({trades_in_slot}/{erlaubt})")
        except GuardrailViolation as gv:
            guardrail_reasons[signal.ticker] = str(gv)
            print(f"   🛡️  Guardrail: {gv}")
            if "Verlustlimit" in str(gv) and not verlustlimit_alert_gesendet:
                send_email(
                    subject="🛑 Trading Bot – Daily Loss Limit erreicht",
                    body=(
                        f"{gv}\n\n"
                        f"Portfolio-Wert: ${portfolio_value:.2f}\n"
                        f"Der Bot wurde automatisch pausiert und handelt erst nach "
                        f"manueller Freigabe wieder."
                    )
                )
                verlustlimit_alert_gesendet = True

    # Kandidaten, die wegen erreichtem Slot-Kontingent gar nicht mehr geprüft
    # wurden (Schleife oben per break beendet), bekommen fürs Scan-Log trotzdem
    # einen nachvollziehbaren Grund statt eines leeren guardrail_reason.
    executed_tickers = {t.ticker for t in executed_trades}
    for signal in approved:
        if signal.ticker not in guardrail_reasons and signal.ticker not in executed_tickers:
            guardrail_reasons[signal.ticker] = f"Slot-Cap erreicht ({trades_in_slot}/{erlaubt} für diesen Slot)"

    # 6. Scan-Ergebnisse loggen (auch nicht ausgeführte Ticker, siehe Feature Scan-Log)
    executed_trades_by_ticker = {t.ticker: t.id for t in executed_trades}
    slot_label = f"{slot.stunde_et:02d}:{slot.minute_et:02d}"
    log_scan_results(signals, slot_label, executed_trades_by_ticker, guardrail_reasons)

    # 7. Tages-Snapshot speichern
    with get_session() as session:
        save_daily_snapshot(session, portfolio_value)
        session.commit()

    print(f"\n{'='*60}")
    print(f"✅ Entry-Zyklus abgeschlossen. Trades in diesem Slot: {len(executed_trades)}")
    print(f"{'='*60}\n")

    # 8. Tages-Zusammenfassung per E-Mail nach dem letzten aktiven Slot des Tages
    # (nicht mehr pro Slot bei Trades, siehe FIX 7) – deckt den ganzen Handelstag ab.
    if is_last_slot_of_day(slot):
        send_daily_summary_email()


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
    """
    monitor_open_positions()


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
