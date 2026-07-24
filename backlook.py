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

from datetime import datetime, date, timedelta
import pytz

from database import (
    get_session, Trade, get_active_weights, set_active_weights, WeightHistory,
    EntryTimeSlot, get_bot_config, get_pending_entry_proposal, set_pending_entry_proposal,
    apply_entry_time_proposal,
)

MIN_TRADES_REQUIRED       = 5
MAX_WEIGHT_CHANGE_PER_RUN = 2
MIN_WEIGHT                = 5
MAX_WEIGHT                = 35

# ─────────────────────────────────────────────
# EINSTIEGSZEITPUNKT-OPTIMIERUNG (Feature 3)
# ─────────────────────────────────────────────

ET_TZ                    = pytz.timezone("America/New_York")
MIN_TRADES_PER_SLOT       = 5     # Mindestanzahl für eine valide Aussage pro Zeitslot
SLOT_DEVIATION_PCT        = 0.20  # >20% vom Durchschnitt = auffällig (besser oder schlechter)
MAX_SLOT_GEWICHTUNG       = 3.0


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


def _run_weekly_weight_backlook():
    """Wöchentliche Anpassung der Score-Gewichtungen (Option A, siehe Modulkommentar)."""
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


def _entry_hour_et(trade: Trade) -> int:
    """Einstiegsstunde (Eastern Time) eines Trades – created_at ist UTC (naiv gespeichert)."""
    utc_dt = pytz.utc.localize(trade.created_at)
    return utc_dt.astimezone(ET_TZ).hour


def _get_all_closed_trades(session) -> list[Trade]:
    """Alle je abgeschlossenen Trades – die Zeitpunkt-Analyse braucht die volle
    Historie (nicht nur die letzte Woche wie der Gewichtungs-Backlook), damit
    pro Stunde überhaupt genug Datenbasis zusammenkommt."""
    return session.query(Trade).filter(
        Trade.status.in_(["CLOSED_SL", "CLOSED_TP", "CLOSED_MANUAL"])
    ).all()


def _hourly_stats(trades: list[Trade]) -> dict[int, dict]:
    """Ø G/V%, Trefferquote und Anzahl Trades je Einstiegsstunde (ET)."""
    by_hour: dict[int, list[Trade]] = {}
    for t in trades:
        by_hour.setdefault(_entry_hour_et(t), []).append(t)

    stats = {}
    for hour, ts in by_hour.items():
        pnls = [t.pnl_pct for t in ts if t.pnl_pct is not None]
        if not pnls:
            continue
        stats[hour] = {
            "avg_pnl": sum(pnls) / len(pnls),
            "trefferquote": sum(1 for p in pnls if p > 0) / len(pnls) * 100,
            "anzahl_trades": len(pnls),
        }
    return stats


def analyze_entry_timing():
    """
    Analysiert alle abgeschlossenen Trades nach Einstiegsstunde (ET) und
    schlägt Anpassungen an den entry_time_slots vor (siehe Feature-3-Spec).
    Läuft direkt im Anschluss an den Gewichtungs-Backlook (siehe run_backlook
    unten) – jeden Montag, unabhängig davon ob dort genug Trades für eine
    Gewichtungsanpassung vorlagen.

    Vorschläge werden NIE automatisch übernommen, außer der Nutzer hat den
    Lernmodus aktiviert (bot_config ENTRY_LEARNING_MODE=true, siehe Feature 4).
    """
    print(f"\n{'='*60}")
    print(f"🕒 Einstiegszeitpunkt-Analyse gestartet: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    with get_session() as session:
        trades = _get_all_closed_trades(session)
        stats = _hourly_stats(trades)
        slots = session.query(EntryTimeSlot).all()

        # Gelernte Performance je konfiguriertem Slot aktualisieren – auch
        # unterhalb der Mindestanzahl, rein zur Anzeige in der Zeitslot-Tabelle.
        for slot in slots:
            s = stats.get(slot.stunde_et)
            if not s:
                continue
            slot.avg_pnl = s["avg_pnl"]
            slot.trefferquote = s["trefferquote"]
            slot.anzahl_trades = s["anzahl_trades"]
            if slot.quelle == "initial" and s["anzahl_trades"] >= MIN_TRADES_PER_SLOT:
                slot.quelle = "backlook"
            slot.updated_at = datetime.utcnow()

        valide_stats = {h: s for h, s in stats.items() if s["anzahl_trades"] >= MIN_TRADES_PER_SLOT}
        if not valide_stats:
            print(f"⏭️  Kein Zeitslot mit ≥{MIN_TRADES_PER_SLOT} abgeschlossenen Trades – noch keine Vorschläge möglich.")
            session.commit()
            print(f"{'='*60}\n")
            return

        gesamt_avg = sum(s["avg_pnl"] for s in valide_stats.values()) / len(valide_stats)

        vorschlaege = []
        for slot in slots:
            s = stats.get(slot.stunde_et)
            if not s or s["anzahl_trades"] < MIN_TRADES_PER_SLOT:
                continue

            schwelle = abs(gesamt_avg) * SLOT_DEVIATION_PCT if gesamt_avg != 0 else 0.5
            begruendung = (
                f"Ø G/V {s['avg_pnl']:+.1f}%, Trefferquote {s['trefferquote']:.0f}% "
                f"({s['anzahl_trades']} Trades)"
            )
            if s["avg_pnl"] < gesamt_avg - schwelle:
                aktion = "deaktivieren" if s["trefferquote"] < 50 else "gewichtung_reduzieren"
                vorschlaege.append({
                    "slot": f"{slot.stunde_et:02d}:{slot.minute_et:02d}",
                    "aktion": aktion,
                    "begruendung": begruendung,
                    "aktuell": {"gewichtung": slot.gewichtung, "avg_pnl": round(s["avg_pnl"], 1)},
                })
            elif s["avg_pnl"] > gesamt_avg + schwelle and s["trefferquote"] >= 60:
                neue_gewichtung = round(min(slot.gewichtung * 1.5, MAX_SLOT_GEWICHTUNG), 1)
                if neue_gewichtung > slot.gewichtung:
                    vorschlaege.append({
                        "slot": f"{slot.stunde_et:02d}:{slot.minute_et:02d}",
                        "aktion": "gewichtung_erhoehen",
                        "begruendung": begruendung,
                        "neu": {"gewichtung": neue_gewichtung},
                    })

        # Unbekannte (nicht konfigurierte) Stunden, die besser performen als
        # der aktuell schwächste konfigurierte Slot.
        configured_hours = {slot.stunde_et for slot in slots}
        konfigurierte_avgs = [slot.avg_pnl for slot in slots if slot.avg_pnl is not None]
        schwaechster = min(konfigurierte_avgs) if konfigurierte_avgs else None
        for hour, s in stats.items():
            if hour in configured_hours or s["anzahl_trades"] < MIN_TRADES_PER_SLOT:
                continue
            if schwaechster is None or s["avg_pnl"] > schwaechster:
                vorschlaege.append({
                    "slot": f"{hour:02d}:00",
                    "aktion": "neuen_slot_hinzufuegen",
                    "begruendung": (
                        f"Ø G/V {s['avg_pnl']:+.1f}%, Trefferquote {s['trefferquote']:.0f}% "
                        f"({s['anzahl_trades']} Trades) – bisher kein Slot um {hour:02d} Uhr"
                    ),
                    "neu": {"gewichtung": 1.0},
                })

        if vorschlaege:
            lernmodus = get_bot_config(session, "ENTRY_LEARNING_MODE", "false") == "true"
            if lernmodus:
                apply_entry_time_proposal(session, vorschlaege)
                set_pending_entry_proposal(session, None)
                print(f"🤖 Lernmodus aktiv – {len(vorschlaege)} Vorschlag/Vorschläge automatisch übernommen.")
            else:
                set_pending_entry_proposal(session, {
                    "typ": "entry_time_optimization",
                    "erstellt": str(date.today()),
                    "vorschlaege": vorschlaege,
                    "lernmodus": False,
                })
                print(f"📋 {len(vorschlaege)} Vorschlag/Vorschläge gespeichert (pending_entry_proposal).")
        else:
            print("✅ Keine Optimierungsvorschläge – aktuelle Zeitpunkte performen gleichmäßig.")

        session.commit()

    print(f"{'='*60}\n")


def run_backlook():
    """
    Hauptfunktion: wird vom Scheduler jeden Montag 06:00 ET aufgerufen.
    Führt zuerst den Gewichtungs-Backlook aus, danach direkt im Anschluss die
    Einstiegszeitpunkt-Analyse (siehe Feature 3 – unabhängig vom Ergebnis des
    Gewichtungs-Backlooks, da beide auf unterschiedlichen Datenbasen laufen).
    """
    _run_weekly_weight_backlook()
    analyze_entry_timing()


if __name__ == "__main__":
    run_backlook()
