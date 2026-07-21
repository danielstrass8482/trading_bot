"""
main.py – Orchestrierung: Scheduler startet täglich den Bot-Loop.
Ablauf: VIX-Check → Watchlist scannen → Guardrails → LLM → Trade
"""

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime
import smtplib
from email.mime.text import MIMEText
import pytz

from config import (
    LONG_WATCHLIST, ACTIVE_SHORT_INSTRUMENTS,
    PROFIT_ALERT_TARGET, MAX_CAPITAL_TOTAL,
    SCAN_HOUR_ET, SCAN_MINUTE_ET, validate_config,
    ALERT_EMAIL, SMTP_HOST, SMTP_PORT, SMTP_FALLBACK_PORT,
    SMTP_USER, SMTP_PASSWORD, SMTP_TIMEOUT
)
from database import init_db, get_session, save_daily_snapshot, BotState
from rule_engine import scan_all_watchlists, check_vix
from llm_analyst import analyze_with_llm
from broker import place_trade, monitor_open_positions, get_portfolio_value, GuardrailViolation
from backlook import run_backlook


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
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
        print(f"📧 E-Mail versendet: {subject} (Port {SMTP_PORT})")
    except (TimeoutError, OSError) as e:
        print(f"⚠️  SMTP Port {SMTP_PORT} nicht erreichbar ({e}) – Fallback auf Port {SMTP_FALLBACK_PORT}")
        try:
            with smtplib.SMTP(SMTP_HOST, SMTP_FALLBACK_PORT, timeout=SMTP_TIMEOUT) as server:
                server.login(SMTP_USER, SMTP_PASSWORD)
                server.sendmail(SMTP_USER, [ALERT_EMAIL], msg.as_string())
            print(f"📧 E-Mail versendet: {subject} (Port {SMTP_FALLBACK_PORT})")
        except Exception as fallback_e:
            print(f"⚠️  E-Mail-Versand fehlgeschlagen (Fallback Port {SMTP_FALLBACK_PORT}): {fallback_e}")
    except Exception as e:
        print(f"⚠️  E-Mail-Versand fehlgeschlagen: {e}")


def send_daily_summary(scanned_count: int, executed_trades: list, portfolio_value: float, vix: float):
    """Verschickt die tägliche Zusammenfassung nach Abschluss eines Bot-Zyklus."""
    lines = [
        f"Gescannte Ticker: {scanned_count}",
        f"Portfolio-Wert: ${portfolio_value:.2f}",
        f"VIX: {vix:.1f}",
        "",
        f"Ausgeführte Trades heute: {len(executed_trades)}",
    ]
    for trade in executed_trades:
        lines.append(
            f"  - {trade.ticker} | Score: {trade.rule_score}/100 | "
            f"Entry: ${trade.entry_price:.2f} | SL: ${trade.stop_loss:.2f} | TP: ${trade.take_profit:.2f}"
        )

    body = "\n".join(lines)
    subject = f"📊 Trading Bot – Tageszusammenfassung {datetime.now().strftime('%Y-%m-%d')}"
    send_email(subject, body)


def run_bot_cycle():
    """
    Haupt-Bot-Zyklus. Wird täglich zur Marktöffnung ausgeführt.
    """
    print(f"\n{'='*60}")
    print(f"🤖 Bot-Zyklus gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 0. Bot pausiert?
    with get_session() as session:
        if BotState.get(session, "bot_paused") == "true":
            print("⏸️  Bot ist pausiert. Kein Handel heute.")
            return

    # 1. VIX-Check (Marktangst-Filter)
    vix, vix_ok = check_vix()
    print(f"\n📊 VIX: {vix:.1f}", end=" ")
    if not vix_ok:
        print(f"🚨 ÜBER LIMIT – Bot pausiert heute (VIX > Schwellwert)")
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

    # 3. Positionen überwachen (SL/TP prüfen)
    print(f"\n--- Positions-Check ---")
    monitor_open_positions()

    # 4. Watchlists scannen
    print(f"\n--- Signal-Scan ---")
    signals = scan_all_watchlists(LONG_WATCHLIST, ACTIVE_SHORT_INSTRUMENTS)

    approved = [s for s in signals if s.approved]
    print(f"\n✅ {len(approved)} Trade-Signale über Schwellwert:")

    # 5. Für jedes freigegebene Signal: LLM + Trade
    executed_trades = []
    for signal in approved:
        print(f"\n--- Trade-Kandidat: {signal.ticker} (Score: {signal.score}/100) ---")

        # LLM-Analyse (non-blocking – Bot läuft weiter bei Fehler)
        print(f"🧠 LLM-Analyse für {signal.ticker}...")
        llm_result = analyze_with_llm(signal)

        if llm_result.get("summary"):
            print(f"   Summary: {llm_result['summary'][:100]}...")
        if llm_result.get("risks"):
            for r in llm_result["risks"]:
                print(f"   ⚠️  {r}")

        # Trade platzieren (Guardrails werden intern geprüft)
        try:
            trade = place_trade(signal, llm_result)
            if trade:
                executed_trades.append(trade)
                print(f"   ✅ Trade #{trade.id} ausgeführt")
        except GuardrailViolation as gv:
            print(f"   🛡️  Guardrail: {gv}")
            if "Verlustlimit" in str(gv):
                send_email(
                    subject="🛑 Trading Bot – Daily Loss Limit erreicht",
                    body=(
                        f"{gv}\n\n"
                        f"Portfolio-Wert: ${portfolio_value:.2f}\n"
                        f"Der Bot wurde automatisch pausiert und handelt erst nach "
                        f"manueller Freigabe wieder."
                    )
                )
            break  # Wenn Tageslimit, weitere Trades sinnlos

    # 6. Tages-Snapshot speichern
    with get_session() as session:
        save_daily_snapshot(session, portfolio_value)
        session.commit()

    print(f"\n{'='*60}")
    print(f"✅ Zyklus abgeschlossen. Heute ausgeführte Trades: {len(executed_trades)}")
    print(f"{'='*60}\n")

    # 7. Tägliche Zusammenfassung per E-Mail
    send_daily_summary(len(signals), executed_trades, portfolio_value, vix)


def run_monitoring_cycle():
    """
    Leichtgewichtiger Zyklus: Nur SL/TP überwachen (alle 30 Min während Handelszeit).
    """
    monitor_open_positions()


def main():
    """Startet den Scheduler."""
    print("🚀 Trading Bot startet...")

    # Konfiguration validieren
    warnings = validate_config()
    for w in warnings:
        print(f"⚠️  Config-Warnung: {w}")

    # Datenbank initialisieren
    init_db()

    # Scheduler konfigurieren (Eastern Time)
    et_tz = pytz.timezone("America/New_York")
    scheduler = BlockingScheduler(timezone=et_tz)

    # Haupt-Zyklus: täglich zur Marktöffnung (09:00 ET)
    scheduler.add_job(
        run_bot_cycle,
        CronTrigger(
            hour=SCAN_HOUR_ET,
            minute=SCAN_MINUTE_ET,
            day_of_week="mon-fri",
            timezone=et_tz
        ),
        id="main_cycle",
        name="Täglicher Bot-Zyklus"
    )

    # Monitoring: alle 30 Minuten während Handelszeit (09:30–16:00 ET)
    scheduler.add_job(
        run_monitoring_cycle,
        CronTrigger(
            hour="9-16",
            minute="*/30",
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

    print(f"⏰ Scheduler aktiv. Bot läuft täglich um {SCAN_HOUR_ET:02d}:{SCAN_MINUTE_ET:02d} ET (Mo–Fr)")
    print(f"📡 Monitoring: alle 30 Min von 09:30–16:00 ET")
    print(f"📚 Backlook: montags 06:00 ET")
    print(f"🛑 Zum Beenden: Ctrl+C\n")

    # Einmalig sofort ausführen beim Start (zum Testen)
    # run_bot_cycle()  # ← Auskommentiert für Production; einkommentieren zum Testen

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\n🛑 Bot gestoppt.")


if __name__ == "__main__":
    main()
