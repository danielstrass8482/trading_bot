"""
post_exit_tracking.py – Schwellenwert-Wirksamkeitsprüfung.

Verfolgt den Kursverlauf für 10 Handelstage NACH einem regelbasierten Exit,
um zu prüfen ob der auslösende Schwellenwert selbst zu früh/spät greift –
kein neuer Trade, keine Order, reine Beobachtung via yfinance-Historie.

Aktuell nur für die Holding-Days-Grenze angebunden (CLOSED_TIME_EXIT /
CLOSED_TIME_EXIT_HARD_CAP, siehe broker.monitor_open_positions). Die
`parameter`-Spalte in post_exit_tracking (database.py) macht Tabelle und
Auswertungslogik hier bewusst wiederverwendbar für künftige Schwellenwerte
(z.B. STOP_LOSS_PCT, ATR_MULTIPLIER_TP, TRAILING_ACTIVATION_PCT) nach
demselben Muster: neuer Exit-Grund in TRACKED_EXIT_REASONS je Parameter,
neuer start_tracking_*-Call an der jeweiligen Exit-Stelle, `parameter`-Wert
bei der Auswertung entsprechend filtern. Die eigentliche update_pending_
tracking()/analyze_threshold_effectiveness()-Logik ist bereits parameter-
agnostisch und muss dafür nicht verändert werden.
"""
import statistics
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from database import get_session, PostExitTracking, Trade, save_learning_proposal, get_learning_proposals

# Exit-Gründe der Holding-Days-Grenze, die eine post_exit_tracking-Zeile
# auslösen (siehe start_tracking_if_applicable).
TRACKED_EXIT_REASONS = {"CLOSED_TIME_EXIT", "CLOSED_TIME_EXIT_HARD_CAP"}
TRACKING_PARAMETER = "MAX_HOLDING_DAYS"

MIN_SAMPLES_FOR_HINT = 10    # Mindest-Stichprobenumfang für einen Hinweis
HINT_THRESHOLD_PCT   = 70.0  # ab diesem Anteil "hätte länger gehalten mehr gebracht" gilt das Muster als klar


def start_tracking_if_applicable(session, trade: Trade):
    """
    Legt bei einem Exit-Grund aus TRACKED_EXIT_REASONS eine post_exit_tracking-
    Zeile an. Wird direkt nach close_trade() in den Time-Exit-Zweigen von
    broker.monitor_open_positions() aufgerufen (kein genereller close_trade-
    Hook, da close_trade() für alle Exit-Gründe gleichermaßen gilt und die
    meisten davon – SL/TP/Trailing – hier nicht getrackt werden sollen).
    """
    if trade.status not in TRACKED_EXIT_REASONS:
        return
    session.add(PostExitTracking(
        trade_id=trade.id,
        ticker=trade.ticker,
        parameter=TRACKING_PARAMETER,
        exit_reason=trade.status,
        exit_price=trade.exit_price,
        exit_date=trade.closed_at,
        pnl_pct_at_exit=trade.pnl_pct,
    ))


def _nth_trading_day(start_date, n: int):
    """Datum n Handelstage (Mo-Fr, ohne Feiertage – Näherung analog zu
    broker.count_trading_days) nach start_date."""
    rng = pd.bdate_range(start=start_date, periods=n + 1)
    return rng[-1].date()


def _price_on_or_after(ticker: str, target_date) -> float | None:
    """Schlusskurs am oder ersten Handelstag nach target_date."""
    try:
        df = yf.Ticker(ticker).history(
            start=target_date, end=target_date + timedelta(days=10),
            interval="1d", auto_adjust=False,
        )
        if df.empty:
            return None
        return float(df["Close"].iloc[0])
    except Exception as e:
        print(f"⚠️  post_exit_tracking: Kein Kurs für {ticker} ab {target_date}: {e}")
        return None


def update_pending_tracking():
    """
    Täglicher Job (siehe main.py Scheduler): aktualisiert alle post_exit_
    tracking-Zeilen, bei denen 5 bzw. 10 Handelstage seit Exit vergangen
    sind und der jeweilige Preis noch nicht erfasst wurde. Funktioniert
    identisch für frische Exits (wartet auf den Fensterablauf) und für
    rückwirkendes Befüllen historischer Exits (Fenster liegt bereits
    komplett in der Vergangenheit → beide Werte werden sofort gesetzt).
    """
    today = datetime.utcnow().date()
    updated = 0

    with get_session() as session:
        pending = session.query(PostExitTracking).filter(
            (PostExitTracking.price_after_5_days.is_(None)) |
            (PostExitTracking.price_after_10_days.is_(None))
        ).all()

        for row in pending:
            exit_date = row.exit_date.date()

            if row.price_after_5_days is None:
                target_5 = _nth_trading_day(exit_date, 5)
                if today >= target_5:
                    price5 = _price_on_or_after(row.ticker, target_5)
                    if price5:
                        row.price_after_5_days = price5
                        row.pnl_pct_after_5_days = round((price5 - row.exit_price) / row.exit_price * 100, 2)
                        updated += 1

            if row.price_after_10_days is None:
                target_10 = _nth_trading_day(exit_date, 10)
                if today >= target_10:
                    price10 = _price_on_or_after(row.ticker, target_10)
                    if price10:
                        row.price_after_10_days = price10
                        row.pnl_pct_after_10_days = round((price10 - row.exit_price) / row.exit_price * 100, 2)
                        row.forgone_profit_pct = round(row.pnl_pct_after_10_days - (row.pnl_pct_at_exit or 0), 2)
                        row.would_have_more_profit = row.forgone_profit_pct > 0
                        updated += 1

        session.commit()

    if updated:
        print(f"📉 post_exit_tracking: {updated} Kurswert(e) aktualisiert.")
    return updated


def analyze_threshold_effectiveness(session, parameter: str = TRACKING_PARAMETER, lookback_days: int = 30) -> dict | None:
    """
    Statistische Auswertung: von allen bereits vollständig ausgewerteten
    (would_have_more_profit is not None) post_exit_tracking-Zeilen eines
    Parameters im Lookback-Fenster – wie viele hätten bei längerem Halten
    mehr Gewinn gebracht vs. weniger, mit Ø/Median der jeweiligen %-Werte.
    None falls keine ausgewerteten Zeilen im Fenster vorhanden sind.
    """
    cutoff = datetime.utcnow() - timedelta(days=lookback_days)
    rows = session.query(PostExitTracking).filter(
        PostExitTracking.parameter == parameter,
        PostExitTracking.exit_date >= cutoff,
        PostExitTracking.would_have_more_profit.isnot(None),
    ).all()

    if not rows:
        return None

    better = [r for r in rows if r.would_have_more_profit]
    worse = [r for r in rows if not r.would_have_more_profit]

    forgone_values = [r.forgone_profit_pct for r in better]           # entgangener Gewinn (positiv)
    avoided_values = [-r.forgone_profit_pct for r in worse]           # vermiedener Verlust (positiv)

    return {
        "parameter": parameter,
        "lookback_days": lookback_days,
        "n": len(rows),
        "better_count": len(better),
        "worse_count": len(worse),
        "better_pct": round(len(better) / len(rows) * 100, 1),
        "avg_forgone_pct": round(sum(forgone_values) / len(forgone_values), 2) if forgone_values else None,
        "median_forgone_pct": round(statistics.median(forgone_values), 2) if forgone_values else None,
        "avg_avoided_pct": round(sum(avoided_values) / len(avoided_values), 2) if avoided_values else None,
        "median_avoided_pct": round(statistics.median(avoided_values), 2) if avoided_values else None,
    }


def maybe_create_effectiveness_hint(session, stats: dict | None):
    """
    Legt bei einem klaren Muster (Mindest-Stichprobenumfang + Anteil über
    HINT_THRESHOLD_PCT) einen Lernvorschlag an (bestehende learning_proposals-
    Infrastruktur, siehe backlook.py/trading_api.py) – rein informativ, KEINE
    automatische Config-Änderung (accept_learning_proposal in trading_api.py
    hat für den typ "schwellenwert_wirksamkeit" keinen Auto-Apply-Zweig).
    """
    if not stats or stats["n"] < MIN_SAMPLES_FOR_HINT:
        return
    if stats["better_pct"] < HINT_THRESHOLD_PCT:
        return

    # Kein Duplikat anlegen, solange bereits ein offener (pending) Hinweis
    # für denselben Parameter existiert – sonst würde jeder Wochenlauf mit
    # weiterhin klarem Muster einen neuen Eintrag anhäufen.
    existing = get_learning_proposals(session)
    already_pending = any(
        p.get("status") == "pending"
        and p.get("typ") == "schwellenwert_wirksamkeit"
        and p.get("data", {}).get("parameter") == stats["parameter"]
        for p in existing
    )
    if already_pending:
        return

    begruendung = (
        f"Von {stats['n']} Time-Exits der letzten {stats['lookback_days']} Tage ({stats['parameter']}) "
        f"hätten {stats['better_count']} ({stats['better_pct']:.0f}%) bei 10 weiteren Handelstagen mehr "
        f"Gewinn gebracht – im Schnitt {stats['avg_forgone_pct']:+.1f}% entgangener Gewinn "
        f"(Median {stats['median_forgone_pct']:+.1f}%). Die {stats['parameter']}-Grenze schneidet "
        f"Gewinner ggf. systematisch zu früh ab. Keine automatische Änderung – Entscheidung liegt bei dir."
    )
    save_learning_proposal(session, "schwellenwert_wirksamkeit", {
        "typ": "schwellenwert_wirksamkeit",
        "parameter": stats["parameter"],
        "begruendung": begruendung,
        "stats": stats,
    })
    print(f"💡 Hinweis erzeugt: {begruendung}")
