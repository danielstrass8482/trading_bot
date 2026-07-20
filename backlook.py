"""
backlook.py – Wöchentlicher Backlook (Option A: Selbstlernendes Scoring).

Läuft jeden Montag 06:00 ET, VOR dem normalen Bot-Zyklus (siehe main.py).
Wertet die in der letzten Woche abgeschlossenen Trades pro Signal-Kriterium
aus: Kriterien die bei Gewinnern hoch und bei Verlierern niedrig gescort
haben, bekommen etwas mehr Gewicht – Kriterien mit umgekehrtem oder keinem
Muster etwas weniger. Kein LLM involviert, rein statistische Auswertung.

Harte Grenzen (nicht verhandelbar):
- Mindestens 5 abgeschlossene Trades nötig, sonst keine Anpassung.
- Max. ±2 Punkte Änderung pro Kriterium und Lauf.
- Jedes Kriterium bleibt zwischen 5 und 35 Punkten.
- SCORE_WEIGHTS-Summe bleibt exakt 100 (zero-sum Anpassung).
"""

from datetime import datetime, timedelta

from database import (
    get_session, Trade, get_active_weights, set_active_weights, WeightHistory
)

MIN_TRADES_REQUIRED       = 5
MAX_WEIGHT_CHANGE_PER_RUN = 2
MIN_WEIGHT                = 5
MAX_WEIGHT                = 35


def get_last_week_closed_trades(session) -> list[Trade]:
    """Alle Trades die in den letzten 7 Tagen geschlossen wurden."""
    week_ago = datetime.utcnow() - timedelta(days=7)
    return session.query(Trade).filter(
        Trade.status.in_(["CLOSED_SL", "CLOSED_TP", "CLOSED_MANUAL"]),
        Trade.closed_at >= week_ago
    ).all()


def _criterion_ratios(trades: list[Trade], criterion: str) -> list[float]:
    """Score-Anteil (score/max) eines Kriteriums über eine Gruppe von Trades."""
    ratios = []
    for t in trades:
        entry = t.get_score_breakdown().get(criterion)
        if entry and entry.get("max"):
            ratios.append(entry["score"] / entry["max"])
    return ratios


def _rebalance_to_zero_sum(raw_deltas: dict, current_weights: dict) -> dict:
    """
    Erzwingt Summe(deltas) == 0, damit die Gesamtgewichtung bei 100 bleibt.
    Korrigiert dazu iterativ das Kriterium mit dem kleinsten |delta|
    (am wenigsten "überzeugtes" Signal), solange Spielraum (±2 Cap,
    5–35 Grenzen) vorhanden ist.
    """
    deltas = dict(raw_deltas)
    total = sum(deltas.values())

    guard = 0
    while total != 0 and guard < 100:
        guard += 1
        step = -1 if total > 0 else 1
        candidates = [
            c for c in deltas
            if abs(deltas[c] + step) <= MAX_WEIGHT_CHANGE_PER_RUN
            and MIN_WEIGHT <= current_weights[c] + deltas[c] + step <= MAX_WEIGHT
        ]
        if not candidates:
            break  # Kein gültiger Ausgleich mehr möglich
        target = min(candidates, key=lambda c: abs(deltas[c]))
        deltas[target] += step
        total += step

    return deltas


def compute_weight_adjustments(trades: list[Trade], current_weights: dict) -> dict:
    """
    Berechnet neue Gewichtungen basierend auf der Trade-Historie.
    Gibt vollständiges neues Gewichtungs-Dict zurück (Summe garantiert 100).
    """
    winners = [t for t in trades if (t.pnl_usd or 0) > 0]
    losers  = [t for t in trades if (t.pnl_usd or 0) <= 0]

    raw_deltas = {}
    for criterion, weight in current_weights.items():
        win_ratios  = _criterion_ratios(winners, criterion)
        loss_ratios = _criterion_ratios(losers, criterion)
        if not win_ratios or not loss_ratios:
            raw_deltas[criterion] = 0
            continue
        diff = (sum(win_ratios) / len(win_ratios)) - (sum(loss_ratios) / len(loss_ratios))
        # Differenz (-1..1) auf max ±2 Punkte skalieren
        desired = max(-MAX_WEIGHT_CHANGE_PER_RUN, min(MAX_WEIGHT_CHANGE_PER_RUN, round(diff * 10)))
        # WICHTIG: bereits hier auf die 5–35 Grenze clippen (nicht erst hinterher),
        # sonst würde nachträgliches Clipping die Zero-Sum-Bilanz verfälschen und
        # könnte den Ausgleich weiter unten über das ±2-Limit eines anderen
        # Kriteriums hinaustreiben.
        lower = MIN_WEIGHT - weight
        upper = MAX_WEIGHT - weight
        raw_deltas[criterion] = max(lower, min(upper, desired))

    deltas = _rebalance_to_zero_sum(raw_deltas, current_weights)
    new_weights = {c: current_weights[c] + deltas.get(c, 0) for c in current_weights}

    # Sicherheitsnetz: sollte der Zero-Sum-Ausgleich (z. B. weil alle Kriterien
    # bereits an ihrer Grenze kleben) nicht vollständig aufgehen, lieber gar
    # keine Anpassung vornehmen als eine der harten Regeln zu verletzen.
    if sum(new_weights.values()) != 100:
        return dict(current_weights)

    return new_weights


def run_backlook():
    """Hauptfunktion: wird vom Scheduler jeden Montag 06:00 ET aufgerufen."""
    print(f"\n{'='*60}")
    print(f"📚 Wöchentlicher Backlook gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    with get_session() as session:
        trades = get_last_week_closed_trades(session)
        print(f"📊 {len(trades)} abgeschlossene Trades in den letzten 7 Tagen.")

        if len(trades) < MIN_TRADES_REQUIRED:
            print(f"⏭️  Weniger als {MIN_TRADES_REQUIRED} Trades – keine Anpassung diese Woche.")
            print(f"{'='*60}\n")
            return

        current_weights = get_active_weights(session)
        new_weights = compute_weight_adjustments(trades, current_weights)

        run_at = datetime.utcnow()
        any_change = False
        for criterion, old_w in current_weights.items():
            new_w = new_weights[criterion]
            change = new_w - old_w
            any_change = any_change or change != 0
            session.add(WeightHistory(
                run_at=run_at,
                criterion=criterion,
                old_weight=old_w,
                new_weight=new_w,
                change=change,
                trades_analyzed=len(trades)
            ))

        if any_change:
            set_active_weights(session, new_weights)
            print("⚖️  Gewichtungen angepasst:")
            for c, old_w in current_weights.items():
                if new_weights[c] != old_w:
                    print(f"   {c}: {old_w} → {new_weights[c]} ({new_weights[c]-old_w:+d})")
        else:
            print("⚖️  Keine klaren Muster gefunden – Gewichtungen unverändert.")

        session.commit()

    print(f"{'='*60}\n")


if __name__ == "__main__":
    run_backlook()
