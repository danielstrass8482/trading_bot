"""
broker.py – Abstraktion für Paper Trading und Live Trading via Alpaca.
Identische Schnittstelle für beide Modi – nur die URL ändert sich.
"""

import os
import uuid
import pytz
from contextlib import contextmanager
from datetime import datetime, timedelta
from sqlalchemy import text
from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL,
    TRADING_MODE, get_live_config, DEFAULT_USER_ID, MAX_CAPITAL_TOTAL,
)
from database import (
    get_session, Trade, get_open_trades, engine,
    get_daily_trade_count, get_total_capital_in_trades,
    get_total_pnl, get_daily_pnl, close_trade, BotState,
    get_alpaca_api_for_user, PendingOrderAttempt,
    get_active_entry_time_slots, get_user_live_config,
    get_trade_mode_for_user, get_bot_config,
    get_capital_allocations, set_capital_allocations,
    get_loss_streak_state, CapitalFlow,
)
from rule_engine import SignalResult
from broker_interface import BrokerInterface
from notifications import send_email
from trading_shared.graceful_shutdown import is_shutdown_requested


class GuardrailViolation(Exception):
    """Wird geworfen wenn ein Guardrail-Limit erreicht wurde.

    reason_code (2026-08-06, Fund 13): strukturierter Ersatz für das bisherige
    String-Matching auf den Fehlertext ("Verlustlimit" in str(gv)) an den
    Alarm-Mail-Stellen in main.py – None für alle Guardrails, die keine
    Sofort-Mail auslösen sollen.
    """
    def __init__(self, message: str, reason_code: str | None = None):
        super().__init__(message)
        self.reason_code = reason_code


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
    (siehe database.get_alpaca_api_for_user). Ohne user_id ODER für
    user_id == DEFAULT_USER_ID (Daniel, der nie eigene Keys über den
    Connect-Flow hinterlegt hat) Fallback auf die globalen .env-Keys.

    KRITISCHER SICHERHEITSFIX 2026-07-31: Für JEDEN ANDEREN user_id ohne
    eigene verbundene Keys wird jetzt None zurückgegeben statt (wie vorher)
    stillschweigend auf die globalen .env-Keys zurückzufallen – das waren
    Daniels echte Live-Kontodaten! Die ursprüngliche Docstring-Annahme "kein
    call site übergibt aktuell eine user_id" stimmte, als main.py/broker.py
    multi-tenant-fähig gemacht wurden (713e497) – dort iteriert
    get_connected_alpaca_users() ohnehin nur bereits verbundene Nutzer, der
    Fallback griff nie fälschlich. Seit dem trading_api.py-Sicherheitsfix
    (a19605f) reicht die API aber JEDE eingeloggte user_id durch, auch die
    eines Nutzers ohne eigene Keys (Account B, siehe Sicherheitsvorfall) –
    genau dafür war der globale Fallback nie gedacht und leakte Daniels
    Kontostand/Positionswert/unrealisierten G&V in dessen Übersicht.
    """
    if user_id is not None:
        client = get_alpaca_api_for_user(user_id)
        if client:
            return client
        if user_id != DEFAULT_USER_ID:
            return None
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


CAPITAL_ALLOCATION_CATEGORIES = ["bot", "active_trading"]


def get_or_seed_capital_allocations(user_id: int, real_total_capital: float | None) -> dict:
    """
    Liest die Prozent-Aufteilung des Gesamtkapitals EINES Nutzers (Aufgabe
    "Kapital-Einstellungen Prozent-Umbau"); beim allerersten Aufruf (noch
    keine Zeilen für diesen Nutzer) wird sie EINMALIG geseedet.

    Für DEFAULT_USER_ID (Daniel) unverändert wie vor der Multi-Tenant-
    Erweiterung (2026-08-08): Herleitung aus dem alten statischen
    MAX_CAPITAL_TOTAL-Wert (Migrations-Punkt 6): bot_pct = altes
    MAX_CAPITAL_TOTAL / echtes Gesamtkapital × 100, Rest an
    "active_trading". Ist real_total_capital gerade nicht bekannt (Alpaca
    nicht erreichbar), wird Fail-safe mit bot_pct=100 geseedet (kompletter
    Umbau erst beim nächsten erfolgreichen Aufruf mit echtem Kapital –
    verhindert einen falschen, zu niedrigen Startwert allein wegen eines
    vorübergehenden API-Ausfalls).

    Für jeden anderen Nutzer (2026-08-08 neu, vorher require_owner() in
    trading_api.py – kein eigenes UI): kein Legacy-Wert vorhanden, daher
    einfacher Seed bot_pct=100 ("mein ganzes verbundenes Konto ist
    Bot-Kapital", identisch zur bisherigen impliziten Bedeutung von
    UserBotConfig.MAX_CAPITAL_TOTAL als alleinigem Kapitallimit) – der
    Kunde kann den Anteil danach selbst über /api/capital-allocations
    verschieben, genau wie Daniel.
    """
    with get_session() as session:
        existing = get_capital_allocations(session, user_id)
        if existing:
            return existing
        if user_id == DEFAULT_USER_ID:
            legacy_max_capital_total = float(get_bot_config(session, "MAX_CAPITAL_TOTAL") or MAX_CAPITAL_TOTAL)
            if real_total_capital and real_total_capital > 0:
                bot_pct = max(0.0, min(100.0, round(legacy_max_capital_total / real_total_capital * 100, 1)))
            else:
                bot_pct = 100.0
        else:
            bot_pct = 100.0
        allocations = {"bot": bot_pct, "active_trading": round(100 - bot_pct, 1)}
        set_capital_allocations(session, user_id, allocations)
        return allocations


def get_effective_max_capital_total_bot(user_id: int = DEFAULT_USER_ID) -> float:
    """
    Ersetzt die alte statische MAX_CAPITAL_TOTAL-Grenze in den Guardrails
    (Aufgabe "Kapital-Einstellungen Prozent-Umbau"): effective = echtes
    Gesamtkapital DIESES Nutzers (cash + gebundenes Kapital, siehe
    get_alpaca_account_snapshot "equity") × dessen eigener Bot-Anteil-
    Prozent (siehe CapitalAllocation, Kategorie "bot").

    Bis 2026-08-08 galt diese Prozent-Rechnung NUR für DEFAULT_USER_ID
    (Daniel) – andere Nutzer hatten kein eigenes Einstellungen-UI (siehe
    require_owner in trading_api.py) und blieben bei ihrem statischen
    UserBotConfig.MAX_CAPITAL_TOTAL. Aufgabe "Presets/Kapitalaufteilung/
    Guardrails pro Nutzer" (regulatorischer Hintergrund: jeder Kunde muss
    seine eigene Kapitalaufteilung selbst festlegen können) macht diese
    Rechnung jetzt für JEDEN Nutzer identisch – get_or_seed_capital_
    allocations() seedet einen neuen Kunden mit bot_pct=100 (siehe dortige
    Docstring), was ohne weiteres Zutun exakt dem alten Verhalten
    entspricht ("mein ganzes verbundenes Konto ist Bot-Kapital"). Daniels
    eigener Wert bleibt unverändert, da get_or_seed_capital_allocations()
    für ihn weiterhin dieselbe Legacy-Herleitung nutzt und seine
    bestehenden DB-Zeilen von der Migration unangetastet übernommen wurden.

    Broker gerade nicht erreichbar/nicht verbunden (real_total is None):
    Fail-safe-Fallback auf den statischen, weiterhin gepflegten
    MAX_CAPITAL_TOTAL-Wert dieses Nutzers (bot_config für Daniel,
    UserBotConfig sonst) statt 0 – 0 würde jeden Trade blockieren und wäre
    strenger als der bisherige Fail-safe-Pfad dieser Guardrails.
    """
    real_snapshot = get_alpaca_account_snapshot(user_id)
    real_total = real_snapshot["equity"] if real_snapshot else None
    allocations = get_or_seed_capital_allocations(user_id, real_total)
    bot_pct = allocations.get("bot", 100.0)

    if real_total is None:
        fallback_cfg = get_live_config() if user_id == DEFAULT_USER_ID else get_user_live_config(user_id)
        fallback_default = MAX_CAPITAL_TOTAL if user_id == DEFAULT_USER_ID else 100
        return float(fallback_cfg.get("MAX_CAPITAL_TOTAL", fallback_default))
    return round(real_total * bot_pct / 100, 2)


def get_effective_max_capital_total_bot_costbasis(user_id: int, real_snapshot: dict, capital_used_sum: float) -> float:
    """
    Cost-basis-konsistente Variante von get_effective_max_capital_total_bot()
    – NUR für Guard-/Budget-Berechnungen (check_guardrails() AUFGABE-4-Check,
    main.calculate_max_trades_today()), NICHT für die Anzeige-Kachel
    "Gesamtkapital" (/api/capital-allocations), die weiterhin bewusst die
    equity-basierte get_effective_max_capital_total_bot() nutzt (echter
    Marktwert zur Transparenz gewünscht, siehe dortige Docstring).

    KRITISCHER BUGFIX 2026-08-04, zweite Iteration (nach Rücksprache): die
    erste Version dieses Fixes (Commit 0321cc3) glich real_total_capital im
    Guard auf equity (Cash + MARKTWERT offener Positionen) ab – numerisch
    korrekt für den beobachteten Fall, aber konzeptionell nur zufällig
    passend (equity als Obergrenze funktioniert nur, weil
    effective_max_capital_total_bot ebenfalls equity-basiert ist). Sauberer:
    BEIDE Seiten des Vergleichs konsequent auf EINSTANDSPREIS-Basis
    (capital_used, "bereits gebunden") rechnen – das beseitigt die
    Diskrepanz strukturell, unabhängig davon ob/wie equity und Cost-Basis
    gerade auseinanderlaufen (unrealisierter Gewinn/Verlust), statt nur eine
    andere, ebenfalls in sich konsistente Bemessungsgrundlage zu wählen.

    real_snapshot/capital_used_sum werden vom Aufrufer übergeben (nicht hier
    selbst neu abgerufen) – beide liegen an den beiden Aufrufstellen ohnehin
    bereits vor, ein zweiter Alpaca-Call wäre unnötig und könnte durch
    Kurs-Ticks zwischen zwei separaten Calls sogar eine neue, kleine
    Rennbedingung einführen (die erste, equity-basierte Fix-Version hatte
    dieses Risiko in kleinerem Maß, weil get_effective_max_capital_total_bot
    intern einen eigenen, zweiten get_alpaca_account_snapshot()-Call machte).

    Seit 2026-08-08 (Aufgabe "Guardrails pro Nutzer", siehe get_effective_
    max_capital_total_bot()-Docstring): (cash + capital_used_sum) × dessen
    eigener bot_pct, für JEDEN Nutzer identisch berechnet – beide Aufrufer
    (check_guardrails, main.calculate_max_trades_today) rufen diese Funktion
    ohnehin nur auf, wenn real_snapshot bereits bekannt ist (Broker
    verbunden), ein None-Fallback ist hier also nie nötig.
    """
    allocations = get_or_seed_capital_allocations(user_id, real_snapshot["equity"])
    bot_pct = allocations.get("bot", 100.0)
    real_capital_costbasis = real_snapshot["cash"] + capital_used_sum
    return round(real_capital_costbasis * bot_pct / 100, 2)


def _user_pause_key(user_id: int) -> str:
    """
    DEFAULT_USER_ID nutzt bewusst den EXISTIERENDEN globalen "bot_paused"-Key
    (100% Verhaltens-Kompatibilität – ein Daily-Loss-Limit-Hit von Daniel
    pausiert wie schon immer "den Bot"). Jeder andere Nutzer bekommt einen
    eigenen Key, damit ein Verlustlimit-Hit bei EINEM Nutzer nicht alle
    anderen mit-pausiert (siehe check_guardrails Punkt 5, AUFGABE 4-Prinzip
    "ein Nutzer darf andere nicht beeinträchtigen" – gilt hier analog).
    """
    return "bot_paused" if user_id == DEFAULT_USER_ID else f"bot_paused_user_{user_id}"


def get_pause_status(user_id: int = DEFAULT_USER_ID) -> dict:
    """
    Aktueller Pause-Zustand für Frontend/Mail-Sichtbarkeit (AUFGABE 2,
    2026-08-06) – fasst BEIDE unabhängigen Pause-Mechanismen zusammen, die
    check_guardrails() prüft: das Tagesverlustlimit (Guard 5, Key
    _user_pause_key) und den Verlustserie-Cooldown (Guard 5b, siehe
    get_loss_streak_state). Beide können gleichzeitig aktiv sein.

    Tagesverlustlimit hat bewusst KEIN "bis"-Zeitpunkt (until=None): der
    bot_paused-Key wird nirgends automatisch zurückgesetzt (siehe
    _user_pause_key-Docstring, dashboard.py-Toggle) – die Freigabe erfordert
    einen manuellen Reset im Dashboard. Der Verlustserie-Cooldown dagegen
    läuft automatisch nach COOLDOWN_HOURS_AFTER_LOSS_STREAK Stunden ab.
    """
    with get_session() as session:
        global_paused = BotState.get(session, "bot_paused") == "true"
        user_paused = (
            user_id != DEFAULT_USER_ID
            and BotState.get(session, _user_pause_key(user_id)) == "true"
        )
        loss_streak = get_loss_streak_state(session, user_id)

    reasons = []
    if global_paused or user_paused:
        reasons.append({"reason": "daily_loss_limit", "until": None})
    if loss_streak["cooldown_active"]:
        reasons.append({
            "reason": "loss_streak_cooldown",
            "until": loss_streak["cooldown_until"].isoformat(),
            "consecutive_losses": loss_streak["consecutive_losses"],
        })

    return {"paused": bool(reasons), "reasons": reasons}


def check_guardrails(signal: SignalResult, user_id: int = DEFAULT_USER_ID) -> None:
    """
    Prüft ALLE Guardrails vor Trade-Ausführung, für EINEN Nutzer (Multi-Tenant-
    Handelsloop, 2026-07-30). user_id=DEFAULT_USER_ID hält jeden bestehenden
    Aufrufer unverändert (siehe config.DEFAULT_USER_ID-Docstring).
    Wirft GuardrailViolation wenn eine Regel verletzt wird.
    Diese Funktion kann NICHT durch LLM-Output beeinflusst werden.
    """
    # Für DEFAULT_USER_ID identisch zu vorher (globale bot_config-Tabelle);
    # für andere Nutzer aus user_bot_config (siehe database.get_user_live_config).
    cfg = get_user_live_config(user_id)
    with get_session() as session:
        # 0. Globaler Not-Aus (immer geprüft, unabhängig von user_id) UND
        #    ggf. dieser Nutzer eigens pausiert (z.B. eigenes Tagesverlustlimit).
        if BotState.get(session, "bot_paused") == "true":
            raise GuardrailViolation("Bot ist manuell pausiert")
        if user_id != DEFAULT_USER_ID and BotState.get(session, _user_pause_key(user_id)) == "true":
            raise GuardrailViolation(f"Nutzer {user_id} ist pausiert (eigenes Tagesverlustlimit erreicht)")

        # 2. Tageslimit Trades
        daily_count = get_daily_trade_count(session, user_id)
        if daily_count >= cfg["MAX_TRADES_PER_DAY"]:
            raise GuardrailViolation(f"Tageslimit erreicht ({daily_count}/{cfg['MAX_TRADES_PER_DAY']} Trades)")

        # 3. Max. offene Positionen
        open_trades = get_open_trades(session, user_id)
        if len(open_trades) >= cfg["MAX_OPEN_POSITIONS"]:
            raise GuardrailViolation(f"Max. offene Positionen erreicht ({len(open_trades)}/{cfg['MAX_OPEN_POSITIONS']})")

        # 4. Doppelter Trade auf gleichen Ticker verhindern
        open_tickers = [t.ticker for t in open_trades]
        if signal.ticker in open_tickers:
            raise GuardrailViolation(f"Position auf {signal.ticker} bereits offen")

        # 5. Tägliches Verlustlimit
        daily_pnl = get_daily_pnl(session, user_id)
        daily_loss_limit = get_effective_max_capital_total_bot(user_id) * cfg["DAILY_LOSS_LIMIT_PCT"]
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            BotState.set(session, _user_pause_key(user_id), "true")
            session.commit()
            raise GuardrailViolation(
                f"Tägliches Verlustlimit erreicht (${abs(daily_pnl):.2f} / ${daily_loss_limit:.2f}). "
                f"{'Bot' if user_id == DEFAULT_USER_ID else f'Nutzer {user_id}'} pausiert automatisch.",
                reason_code="daily_loss_limit",
            )

        # 5b. Verlustserie-Cooldown (AUFGABE 1, 2026-08-06): EIGENSTÄNDIGER
        # Guardrail, unabhängig vom Tagesverlustlimit oben – beide können
        # gleichzeitig aktiv sein. Zählt aufeinanderfolgende Verlust-Trades
        # unabhängig von deren Höhe (siehe database.close_trade/
        # _record_loss_streak_result), sperrt hier nur NEUE Entries; offene
        # Positionen laufen unverändert per SL/TP/Trailing weiter (siehe
        # monitor_open_positions, das diesen Guard nicht aufruft). Läuft
        # automatisch nach COOLDOWN_HOURS_AFTER_LOSS_STREAK Stunden ab (siehe
        # get_loss_streak_state) – kein manueller Reset nötig.
        loss_streak = get_loss_streak_state(session, user_id)
        if loss_streak["cooldown_active"]:
            raise GuardrailViolation(
                f"Verlustserie-Cooldown aktiv ({loss_streak['consecutive_losses']} Verluste in Folge) – "
                f"pausiert bis {loss_streak['cooldown_until'].isoformat()}.",
                reason_code="loss_streak_cooldown",
            )

        # 6. AUFGABE 4 (2026-07-30): konfiguriertes Kapital-Limit vs. echtes
        # Broker-Kapital. Redundant zur (primären) Prüfung in
        # main.calculate_max_trades_today()/get_trades_for_slot() – die
        # verhindert im Normalfall schon, dass place_trade() für einen so
        # fehlkonfigurierten Nutzer überhaupt aufgerufen wird (erlaubt=0). Diese
        # zweite, unabhängige Prüfung HIER (jeder place_trade()-Aufruf ruft
        # check_guardrails() als allerersten Schritt) ist Verteidigung in der
        # Tiefe: selbst falls place_trade() je direkt/ohne den Slot-Budget-Gate
        # aufgerufen würde, darf ein Nutzer mit unrealistischem Limit trotzdem
        # NIE einen Trade auslösen – KEINE Exception, die andere Nutzer
        # beeinträchtigt (GuardrailViolation wird vom Aufrufer immer lokal pro
        # Kandidat/Nutzer gefangen, siehe main.run_entry_cycle).
        #
        # KRITISCHER BUGFIX 2026-08-04 (zweite Iteration, nach Rücksprache):
        # real_total_capital verglich ursprünglich cash + invested(COST-BASIS,
        # get_total_capital_in_trades – Summe der ursprünglichen capital_used-
        # Werte bei Entry) gegen effective_max_capital_total_bot, das für
        # DEFAULT_USER_ID aber als equity(= cash + MARKTWERT offener
        # Positionen) × bot_pct definiert ist (siehe
        # get_effective_max_capital_total_bot). Cost-Basis und Marktwert
        # fallen bei jedem unrealisierten Gewinn/Verlust auseinander – die
        # Differenz entsprach exakt dem unrealisierten P&L. Bei JEDEM Tag mit
        # positivem unrealisiertem Ergebnis (dem Normalfall bei einem
        # funktionierenden Bot) feuerte dieser Guard dadurch für JEDEN
        # Kandidaten unabhängig vom Score – live beobachtet am 2026-08-04:
        # 33/33 Kandidaten über Score 65 blockiert trotz freier Slots
        # (unrealisierter Gewinn +$9.31).
        # Ein erster Fix (Commit 0321cc3) glich real_total_capital stattdessen
        # auf equity ab (numerisch korrekt, aber nur weil beide Seiten dann
        # zufällig dieselbe equity-Bemessungsgrundlage teilten). Sauberer
        # (diese Version): BEIDE Seiten konsequent auf EINSTANDSPREIS-Basis
        # rechnen (get_effective_max_capital_total_bot_costbasis, siehe
        # dortige Docstring) – strukturell unabhängig von unrealisiertem
        # Gewinn/Verlust, nicht nur eine andere, ebenfalls konsistente
        # Bemessungsgrundlage. Cash bleibt bewusst Teil beider Seiten – ohne
        # Cash würde die Prüfung genau den Fall nicht mehr erkennen, den sie
        # verhindern soll (ein Nutzer mit $0 investiert aber unrealistisch
        # hoch konfiguriertem Limit würde sonst nie anschlagen). Dieselbe
        # cost-basis-konsistente Größe treibt jetzt auch main.
        # calculate_max_trades_today() (vorher: equity-basiertes Limit minus
        # cost-basis-basiertem invested – dieselbe Kategorie Inkonsistenz,
        # dort aber nur zu überhöhtem statt blockiertem Budget führend).
        real_snapshot = get_alpaca_account_snapshot(user_id)
        if real_snapshot is not None:
            capital_used_sum = get_total_capital_in_trades(session, user_id)
            real_total_capital = real_snapshot["cash"] + capital_used_sum
            # BUGFIX 2026-08-06 (live Fehlalarm, Score-98-Kandidaten trotz
            # freier Slots übersprungen): für DEFAULT_USER_ID bei bot_pct=100
            # ist effective_max_capital_total MATHEMATISCH identisch zu
            # real_total_capital (get_effective_max_capital_total_bot_
            # costbasis() ist exakt als Prozentsatz dieser (cash +
            # capital_used_sum)-Größe definiert) – eine frühere Version
            # dieses Kommentars hielt den Fall deshalb für "strukturell nicht
            # erreichbar". Das ignorierte, dass effective_max_capital_total
            # GERUNDET wird (round(..., 2)), real_total_capital hier aber
            # UNGERUNDET blieb: Binärfließkomma kann beim Runden minimal nach
            # oben kippen (z.B. 467.649999999999977... -> gerundet
            # 467.65000000000003...), sodass die gerundete Seite die
            # ungerundete Seite um einen Bruchteil eines Cents überstieg und
            # ">" fälschlich auslöste, obwohl beide Werte auf 2 Dezimalstellen
            # identisch angezeigt wurden ("$467.17 vs $467.17"). Live
            # beobachtet 2026-08-06: 110 Kandidaten in 2 von 3 Scan-Zyklen
            # fälschlich übersprungen. Fix: real_total_capital für den
            # Vergleich auf dieselbe Cent-Genauigkeit runden wie
            # effective_max_capital_total – macht den Vergleich für den
            # bot_pct=100-Fall exakt (keine Cent-Rundungsartefakte mehr),
            # erkennt einen ECHTEN Kapitalüberschuss (z.B. fehlerhaft
            # konfiguriertes bot_pct > 100, oder für andere Nutzer ein
            # UserBotConfig.MAX_CAPITAL_TOTAL deutlich über deren eigenem
            # Cash/Cost-Basis) weiterhin zuverlässig, da ein solcher
            # Unterschied um Größenordnungen über dem Cent-Rundungsrauschen
            # liegt.
            effective_max_capital_total = get_effective_max_capital_total_bot_costbasis(
                user_id, real_snapshot, capital_used_sum
            )
            if effective_max_capital_total > round(real_total_capital, 2):
                raise GuardrailViolation(
                    f"Nutzer {user_id}: konfiguriertes Limit übersteigt echtes Kapital "
                    f"(konfiguriert: ${effective_max_capital_total:.2f}, echt: ${real_total_capital:.2f}), "
                    f"Kandidat übersprungen"
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


def _is_confirmed_not_found(exc: Exception) -> bool:
    """
    Erkennt eine NACHWEISLICH bestätigte "Order/Position existiert nicht"-
    Antwort (z.B. echtes 404) anhand der Fehlermeldung – analog zum bereits
    korrekten Muster in _sell_position_at_alpaca() ("position does not
    exist"/"404"). Alles andere (Timeout, Rate-Limit, 5xx, Netzwerkfehler)
    ist KEINE Bestätigung, sondern lediglich ein fehlgeschlagener
    Verifikationsversuch – siehe _verify_order_with_retry.
    """
    msg = str(exc).lower()
    return "404" in msg or "not found" in msg or "does not exist" in msg


def _verify_order_with_retry(client, client_order_id: str, ticker: str, max_attempts: int = 3):
    """
    BUGFIX 2026-08-06 (Code-Audit Chunk 1, Fund 2): fragt eine Order per
    client_order_id ab und unterscheidet dabei sauber zwei völlig
    unterschiedliche Fälle, die vorher beide identisch als "Order existiert
    nicht" behandelt wurden:
      (a) NACHWEISLICH bestätigt nicht vorhanden (echtes 404/"not found")
          -> sicher, ein neuer Versuch mit neuer client_order_id ist erlaubt.
      (b) die Verifikationsabfrage SELBST schlägt technisch fehl (Timeout,
          Rate-Limit, 5xx, Netzwerkfehler) -> das ist KEINE Bestätigung,
          dass die ursprüngliche Order nie ankam! Ein Retry mit Backoff wird
          versucht, bevor aufgegeben wird.

    Gibt (order, "found") zurück falls die Order gefunden wurde,
    (None, "not_found") falls nachweislich bestätigt nicht vorhanden, oder
    (None, "inconclusive") falls nach allen Versuchen weiterhin unklar ist,
    ob die Order angekommen ist – der Aufrufer darf im letzten Fall NIEMALS
    automatisch einen neuen Kauf-/Verkaufsversuch auslösen (Doppel-Order-
    Risiko), sondern muss den PendingOrderAttempt unresolved (PENDING)
    lassen, damit der nächste reguläre Zyklus erneut nachschaut.
    """
    import time
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return client.get_order_by_client_order_id(client_order_id), "found"
        except Exception as e:
            last_exc = e
            if _is_confirmed_not_found(e):
                return None, "not_found"
            if attempt < max_attempts - 1:
                wait_s = 2 ** attempt  # 1s, 2s, 4s, ...
                print(f"⚠️  {ticker}: Verifikation von Order {client_order_id} (Versuch {attempt + 1}/"
                      f"{max_attempts}) technisch fehlgeschlagen ({e}) – kein bestätigtes 'existiert nicht', "
                      f"erneuter Versuch in {wait_s}s...")
                time.sleep(wait_s)
    print(f"🚨 {ticker}: Verifikation von Order {client_order_id} nach {max_attempts} Versuchen weiterhin "
          f"unklar (letzter Fehler: {last_exc}) – Status bleibt PENDING, KEIN automatischer neuer Versuch "
          f"(Doppel-Order-Risiko), nächster regulärer Zyklus prüft erneut nach.")
    return None, "inconclusive"


def _submit_order_idempotent(client, ticker: str, user_id: int = DEFAULT_USER_ID, **submit_kwargs):
    """
    Idempotenz-Schutz (Aufgabe 1, 2026-07-30): platziert eine Alpaca-Order mit
    einer selbst generierten client_order_id (NICHT vom Broker vergeben,
    sondern VOR dem Request erzeugt) – Alpaca garantiert, dass eine Order mit
    bereits verwendeter client_order_id nur EINMAL angenommen wird.

    Schlägt submit_order() durch Timeout/Netzwerkfehler unklar fehl (kam die
    Order an oder nicht?), wird NICHT blind als Fehlschlag behandelt: per
    get_order_by_client_order_id() wird nachgeprüft, ob Alpaca die Order
    trotzdem angenommen hat. Existiert sie bereits, wird DIESE Order
    zurückgegeben statt eines zweiten Versuchs – verhindert Doppelkauf/
    -verkauf bei einem Retry über einen unklaren Fehler hinweg.

    Gibt (order, client_order_id) zurück oder wirft die ursprüngliche
    Exception weiter, falls die Order nachweislich nie angekommen ist
    (dann ist ein neuer Versuch mit neuer ID sicher). user_id (Multi-Tenant,
    2026-07-30) wird auf den PendingOrderAttempt-Eintrag geschrieben, damit
    zwei Nutzer, die im selben Zyklus denselben Ticker kaufen, sich nicht
    gegenseitig über pending_order_attempts blockieren (siehe
    _reconcile_pending_entry_attempt).
    """
    client_order_id = str(uuid.uuid4())
    with get_session() as session:
        session.add(PendingOrderAttempt(ticker=ticker, client_order_id=client_order_id, user_id=user_id))
        session.commit()

    def _resolve(status: str):
        with get_session() as session:
            row = session.query(PendingOrderAttempt).filter_by(client_order_id=client_order_id).first()
            if row:
                row.status = status
                row.resolved_at = datetime.utcnow()
                session.commit()

    try:
        order = client.submit_order(client_order_id=client_order_id, **submit_kwargs)
        _resolve("FILLED")
        return order, client_order_id
    except Exception as e:
        print(f"⚠️  {ticker}: Order-Submit unklar fehlgeschlagen ({e}) – prüfe bei Alpaca nach, "
              f"ob sie trotzdem angenommen wurde, bevor ein zweiter Versuch startet...")
        existing, verdict = _verify_order_with_retry(client, client_order_id, ticker)
        if verdict == "found":
            print(f"ℹ️  {ticker}: Order {client_order_id} EXISTIERT bei Alpaca trotz Fehler "
                  f"(Status: {existing.status}) – kein Doppel-Versuch, nutze diese Order.")
            _resolve("FILLED")
            return existing, client_order_id
        if verdict == "not_found":
            print(f"✅ {ticker}: Order {client_order_id} existiert NICHT bei Alpaca – Request ist "
                  f"nachweislich nie angekommen, ein neuer Versuch ist sicher.")
            _resolve("FAILED")
            raise e
        # verdict == "inconclusive": bewusst NICHT als FAILED markieren (bleibt
        # PENDING für die nächste reguläre Reconciliation) und bewusst KEINE
        # neue Order erlauben – GuardrailViolation wird von run_entry_cycle()
        # bereits sauber pro Kandidat abgefangen (nur dieser Kandidat wird
        # übersprungen, siehe check_guardrails-Aufrufer).
        raise GuardrailViolation(
            f"{ticker}: Order-Status nach Submit-Fehler UND fehlgeschlagener Verifikation weiterhin "
            f"unklar – Kandidat sicherheitshalber übersprungen (Doppel-Order-Risiko), nächster Zyklus "
            f"prüft den offenen Versuch erneut."
        )


def _reconcile_pending_entry_attempt(client, ticker: str, user_id: int = DEFAULT_USER_ID):
    """
    Wird VOR jedem neuen Entry-Versuch für `ticker` aufgerufen (Aufgabe 1):
    prüft, ob ein vorheriger, durch einen Prozess-Absturz o.ä. unterbrochener
    Order-Versuch für denselben Ticker noch als PENDING in der DB steht, und
    klärt ihn zuerst, statt blind einen neuen Kauf zu starten.

    Nach (ticker, user_id) gefiltert (Multi-Tenant, 2026-07-30) – sonst würde
    ein PENDING-Eintrag von Nutzer A hier fälschlich Nutzer Bs (komplett
    unabhängiger Alpaca-Account!) Kaufversuch für denselben Ticker blockieren.

    Gibt die existierende Alpaca-Order zurück, falls der alte Versuch
    tatsächlich durchging (Aufrufer darf dann KEINEN neuen Kauf platzieren),
    oder None (alter Versuch war nachweislich fehlgeschlagen oder es gab
    keinen offenen Versuch – sicher, normal fortzufahren). Wirft
    GuardrailViolation, falls sich auch nach mehreren Versuchen mit Backoff
    NICHT klären lässt, ob der alte Versuch durchging (BUGFIX 2026-08-06,
    Code-Audit Chunk 1, Fund 2, analog _submit_order_idempotent) – ein
    technischer Verifikations-Fehlschlag (Timeout/5xx/Netzwerk) ist KEINE
    Bestätigung, dass der alte Versuch nie ankam, und darf NIE automatisch
    einen neuen Kauf freigeben (Doppel-Order-Risiko).
    """
    with get_session() as session:
        pending = (
            session.query(PendingOrderAttempt)
            .filter_by(ticker=ticker, status="PENDING", user_id=user_id)
            .order_by(PendingOrderAttempt.created_at.desc())
            .first()
        )
        if not pending:
            return None
        client_order_id = pending.client_order_id

    print(f"🔎 {ticker}: Ein vorheriger Entry-Order-Versuch ({client_order_id}) war noch ungeklärt "
          f"(PENDING) – prüfe bei Alpaca nach, bevor ein neuer Versuch startet.")
    existing, verdict = _verify_order_with_retry(client, client_order_id, ticker)

    if verdict == "not_found":
        print(f"✅ {ticker}: Alter Versuch {client_order_id} existiert NICHT bei Alpaca – "
              f"nie angekommen, sicher für einen neuen Versuch.")
        with get_session() as session:
            row = session.query(PendingOrderAttempt).filter_by(client_order_id=client_order_id).first()
            if row:
                row.status = "FAILED"
                row.resolved_at = datetime.utcnow()
                session.commit()
        return None

    if verdict == "inconclusive":
        # Bewusst NICHT als FAILED markieren (bleibt PENDING für die nächste
        # reguläre Reconciliation) und bewusst KEIN neuer Kauf – siehe Docstring.
        raise GuardrailViolation(
            f"{ticker}: Status des vorherigen Entry-Versuchs ({client_order_id}) auch nach mehreren "
            f"Verifikationsversuchen weiterhin unklar – Kandidat sicherheitshalber übersprungen "
            f"(Doppel-Order-Risiko), nächster Zyklus prüft erneut nach."
        )

    print(f"ℹ️  {ticker}: Alter Versuch {client_order_id} EXISTIERT bei Alpaca (Status: {existing.status}) "
          f"– kein neuer Kauf, alter Versuch wird stattdessen übernommen.")
    with get_session() as session:
        row = session.query(PendingOrderAttempt).filter_by(client_order_id=client_order_id).first()
        if row:
            row.status = "FILLED"
            row.resolved_at = datetime.utcnow()
            session.commit()
    return existing


# Namespace-Konstante für den Guardrail-Lock unten (siehe _user_trade_guardrail_
# lock) – beliebig gewählt, aber fest, damit dieser Lock-Key-Raum nicht mit
# einem etwaigen künftigen anderen pg_advisory_lock-Nutzer in diesem Prozess
# kollidiert (Postgres-Advisory-Locks sind global pro DB, nicht pro Tabelle/
# Feature namensraumgetrennt).
_TRADE_GUARDRAIL_LOCK_NAMESPACE = 894613


@contextmanager
def _user_trade_guardrail_lock(user_id: int):
    """
    Race-Condition-Fix (2026-08-13): check_guardrails() (Tageslimit/max.
    offene Positionen – reine SELECTs, kein Lock) und die eigentliche
    Trade-Anlage (session.add(Trade)/commit ganz am Ende von place_trade())
    liefen bisher in ZWEI GETRENNTEN Transaktionen mit einem teils
    sekundenlangen Fenster dazwischen (Alpaca-Order-Platzierung + bis zu
    3x 1s Fill-Polling). Zwei parallele place_trade()-Aufrufe für DENSELBEN
    Nutzer (z.B. schnelles Bestätigen mehrerer unterschiedlicher Confirm-
    Tier-Einträge kurz hintereinander – confirm_execution.try_claim()
    schützt dort nur die EINZELNE PendingConfirmation-Zeile vor Doppel-
    Ausführung, nicht den GLOBALEN Tageslimit-/Positionslimit-Zähler)
    konnten beide denselben, vom jeweils anderen noch nicht erhöhten
    Zählerstand lesen und beide durchkommen. Gemeldeter Vorfall: 3
    ausgeführte Trades bei MAX_TRADES_PER_DAY=2, die Ablehnung ("3 von 2")
    kam erst nach der dritten Order.

    Postgres SESSION-Advisory-Lock (bewusst NICHT pg_advisory_xact_lock,
    da check_guardrails() und der finale INSERT in unterschiedlichen
    get_session()-Blöcken/Transaktionen laufen, also keine einzelne
    Transaktion die gesamte place_trade()-Dauer umspannt) auf
    (Namespace, user_id) – serialisiert konkurrierende place_trade()-
    Aufrufe für DENSELBEN Nutzer vollständig (auch über den Alpaca-
    Netzwerk-Call hinweg: der zweite Aufrufer wartet, bis der erste seinen
    Trade committet oder mit einer GuardrailViolation/None abbricht, sieht
    danach den korrekt erhöhten Zählerstand). Verschiedene Nutzer blockieren
    sich gegenseitig NICHT (unterschiedlicher Lock-Key) – kein Cross-Tenant-
    Delay. Lock/Unlock laufen explizit auf derselben rohen Connection
    (nicht über den ORM-Session-Pool, dessen Connections zwischen den
    einzelnen get_session()-Blöcken innerhalb von place_trade() wechseln
    können).
    """
    conn = engine.connect()
    try:
        conn.execute(text("SELECT pg_advisory_lock(:ns, :uid)"),
                     {"ns": _TRADE_GUARDRAIL_LOCK_NAMESPACE, "uid": user_id})
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :uid)"),
                         {"ns": _TRADE_GUARDRAIL_LOCK_NAMESPACE, "uid": user_id})
        finally:
            conn.close()


def place_trade(signal: SignalResult, llm_result: dict, user_id: int = DEFAULT_USER_ID) -> Trade | None:
    """
    Führt Trade aus (Paper oder Live), für EINEN Nutzer (Multi-Tenant-
    Handelsloop, 2026-07-30). user_id=DEFAULT_USER_ID hält jeden bestehenden
    Aufrufer unverändert (siehe config.DEFAULT_USER_ID-Docstring).
    1. Guardrails prüfen (inkl. echtem Kapital-Check, siehe unten)
    2. Order bei Alpaca platzieren (oder Paper-Simulation)
    3. Trade in DB loggen (mit user_id)
    Gibt Trade-Objekt zurück oder None bei Fehler.

    Race-Condition-Fix (2026-08-13): die gesamte Funktion läuft jetzt hinter
    _user_trade_guardrail_lock(user_id) – siehe dortige Docstring für den
    genauen Vorfall/die Begründung. Dünner Wrapper statt Inline-`with`, um
    den bestehenden Funktionskörper nicht komplett neu einrücken zu müssen.
    """
    with _user_trade_guardrail_lock(user_id):
        return _place_trade_locked(signal, llm_result, user_id)


def _place_trade_locked(signal: SignalResult, llm_result: dict, user_id: int = DEFAULT_USER_ID) -> Trade | None:
    """Eigentliche place_trade()-Implementierung – läuft IMMER innerhalb von
    _user_trade_guardrail_lock, niemals direkt aufrufen."""
    # Guardrails zuerst – keine Ausnahmen
    check_guardrails(signal, user_id)  # Wirft GuardrailViolation bei Verstoß

    # Sicherheitsnetz: die eigentliche Order-Platzierung unten spricht aus-
    # schließlich die Alpaca-API an – IBKR-Order-Routing ist (noch) nicht
    # implementiert (broker_ibkr.py existiert nur als BrokerInterface-Client,
    # siehe broker.get_broker). Wäre ACTIVE_BROKER="ibkr" hier folgenlos, würde
    # der Trade fälschlich als "ibkr" geloggt, obwohl tatsächlich Alpaca (Live-
    # Geld!) gehandelt hat – daher lieber gar kein Trade als eine falsche
    # Broker-Zuordnung im Log. ACTIVE_BROKER bleibt bewusst GLOBAL (Broker-Wahl
    # fürs gesamte Deployment, kein Teil dieses Auftrags als Pro-Nutzer-Einstellung).
    active_broker = get_live_config().get("ACTIVE_BROKER", "alpaca")
    if active_broker != "alpaca":
        print(f"❌ ACTIVE_BROKER='{active_broker}', aber Order-Routing ist aktuell nur für Alpaca implementiert – Trade übersprungen.")
        return None

    # AUFGABE 4 (2026-07-30): echtes, gerade jetzt verfügbares Kapital dieses
    # Nutzers als harte Obergrenze für die Positionsgröße – zusätzlich zum
    # bereits in calculate_max_trades_today() eingerechneten Cash-Limit (das
    # bestimmt nur OB dieser Slot für diesen Nutzer noch Budget hat, nicht wie
    # viel EXAKT für DIESEN einen Kandidaten noch übrig ist, falls der Nutzer
    # innerhalb desselben Zyklus bereits andere Kandidaten gekauft hat).
    user_cfg = get_user_live_config(user_id)
    max_capital_per_trade = user_cfg["MAX_CAPITAL_PER_TRADE"]
    real_snapshot = get_alpaca_account_snapshot(user_id)
    if real_snapshot is not None:
        max_capital_per_trade = min(max_capital_per_trade, real_snapshot["cash"])

    quantity = calculate_quantity(signal.current_price, max_capital_per_trade)
    if quantity <= 0:
        real_cash_label = "unbekannt" if real_snapshot is None else f"${real_snapshot['cash']:.2f}"
        print(f"❌ Nutzer {user_id}: {signal.ticker} übersprungen – kein Kapital für auch nur eine Bruchteil-Aktie "
              f"(echtes verfügbares Kapital: {real_cash_label}).")
        return None

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

    # entry_price wird per Default vom Signalzeitpunkt übernommen (PAPER-Modus) –
    # im LIVE-Modus unten durch den tatsächlichen Alpaca-Fill-Preis ersetzt,
    # ODER (Fix 2026-07-31, Kauf-Fill-Pendant zu _sell_position_at_alpaca(),
    # siehe dort/DUK-Vorfall 2026-07-27) auf None gesetzt, falls die Order
    # nicht innerhalb des Polls gefüllt wurde – KEIN geratener Preis mehr wird
    # je als "der" Einstiegspreis übernommen, siehe unten.
    entry_price = signal.current_price
    entry_status_detail = None
    entry_pending_client_order_id = None

    # ── LIVE TRADING via Alpaca ─────────────────────────────────────
    # TRADING_MODE bleibt global (steuert nur OB überhaupt echte API-Calls
    # versucht werden) – OB dabei echtes Geld bewegt wird, entscheidet einzig
    # die base_url DIESES Nutzer-Clients (get_alpaca_api_for_user() baut sie
    # aus pos_users.alpaca_mode: "paper" -> paper-api.alpaca.markets, "live"
    # -> api.alpaca.markets). Ein per Connect-Flow im Paper-Modus verbundener
    # Nutzer landet also selbst bei globalem TRADING_MODE=LIVE sicher im
    # Alpaca-Sandbox, nicht auf echtem Geld – nur DEFAULT_USER_IDs Fallback
    # auf die .env-Keys spricht tatsächlich das reale Live-Konto an.
    if TRADING_MODE == "LIVE":
        client = _get_alpaca_client(user_id)
        if not client:
            print(f"❌ Nutzer {user_id}: Live Trade abgebrochen: Alpaca nicht verfügbar")
            return None

        # Idempotenz-Schutz (Aufgabe 1, 2026-07-30): bevor ein NEUER Kauf
        # gestartet wird, erst prüfen ob ein früherer, durch einen Absturz
        # o.ä. unterbrochener Entry-Versuch für denselben Ticker noch offen
        # ist – sonst könnte ein Retry nach einem unklaren Timeout versehent-
        # lich zu einer zweiten echten Position führen.
        existing_order = _reconcile_pending_entry_attempt(client, signal.ticker, user_id)
        if existing_order is not None:
            order = existing_order
            client_order_id = existing_order.client_order_id
            print(f"ℹ️  {signal.ticker}: nutze bereits bestehende Order aus vorherigem Versuch statt neu zu kaufen.")
        else:
            try:
                if is_whole_share:
                    order, client_order_id = _submit_order_idempotent(
                        client, signal.ticker, user_id,
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
                    order, client_order_id = _submit_order_idempotent(
                        client, signal.ticker, user_id,
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
        filled = False
        for _ in range(3):
            time.sleep(1)
            try:
                filled_order = client.get_order(order.id)
            except Exception as e:
                print(f"⚠️  Order-Status konnte nicht abgefragt werden: {e}")
                break
            if filled_order.filled_avg_price:
                entry_price = float(filled_order.filled_avg_price)
                filled = True
                print(f"ℹ️  {signal.ticker}: Entry-Preis aus Alpaca-Fill: ${entry_price} (Signal-Kurs war ${signal.current_price})")
                break
        if not filled:
            # Kein Fallback-Kurs mehr (Fix 2026-07-31, analog zu
            # _sell_position_at_alpaca()/Incident 2026-07-30 UNH/AMZN/PSQ,
            # hier auf die Entry-Seite übertragen): entry_price bleibt auf
            # dem Signal-Kurs NUR als grobe Kapital-Reservierungs-Schätzung
            # (siehe capital_used unten) – status_detail=WAITING_FILL markiert
            # ihn explizit als NICHT bestätigt, damit SL/TP/Trailing (die auf
            # trade.entry_price rechnen) ihn nicht fälschlich als echten Fill
            # behandeln. _reconcile_pending_entry_fill() (broker.py) trägt den
            # echten Fill-Preis nach, sobald die Order tatsächlich gefüllt ist.
            entry_price = None
            entry_status_detail = "WAITING_FILL"
            entry_pending_client_order_id = client_order_id
            print(f"⏳ {signal.ticker}: Order {client_order_id} nach 3s noch nicht gefüllt – KEIN Preis-Fallback, "
                  f"Trade wird mit status_detail=WAITING_FILL angelegt. Nächster Monitoring-Zyklus prüft per "
                  f"Reconciliation nach und trägt den echten Fill-Preis nach.")

    # ── PAPER TRADING (Simulation) ──────────────────────────────────
    else:
        if is_whole_share:
            print(f"📄 PAPER Bracket-Trade simuliert: {int(quantity)}x {signal.ticker} @ ${signal.current_price} SL: ${sl_price} TP: ${tp_price}")
        else:
            print(f"📄 PAPER Trade simuliert: {quantity}x {signal.ticker} @ ${signal.current_price}")

    # capital_used anhand des tatsächlichen entry_price statt des vorläufigen
    # Signal-Kurses – sonst würde z.B. bei DUK weiterhin $50 "capital_used"
    # geloggt, obwohl real nur ~$20 investiert wurden (Menge wurde ja mit dem
    # falschen Signal-Kurs berechnet). entry_price ist None, solange der Kauf
    # noch WAITING_FILL ist (siehe oben) – capital_used ist dann bewusst nur
    # eine grobe, auf dem Signal-Kurs basierende Schätzung für die Guardrail-
    # Kapitalreservierung; _reconcile_pending_entry_fill() ersetzt sie durch
    # den exakten Wert, sobald der echte Fill-Preis feststeht.
    capital_used = round(quantity * (entry_price if entry_price is not None else signal.current_price), 2)

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
        status_detail   = entry_status_detail,
        pending_client_order_id = entry_pending_client_order_id,
        mode            = get_trade_mode_for_user(user_id),
        broker          = active_broker,
        sector          = signal.sector,
        user_id         = user_id,
    )
    trade.set_score_breakdown(signal.score_breakdown)

    with get_session() as session:
        session.add(trade)
        session.commit()
        session.refresh(trade)
        print(f"💾 Trade #{trade.id} in DB gespeichert")
        return trade


def _reconcile_pending_entry_fill(session, trade: Trade):
    """
    Kauf-Fill-Pendant zu _reconcile_pending_exit() (Fix 2026-07-31, gleiche
    Fehlerklasse wie der DUK-Vorfall 2026-07-27 / UNH-AMZN-PSQ-Incident
    2026-07-30, hier auf die Entry-Seite übertragen). Wird von
    monitor_open_positions() für Trades aufgerufen, deren status_detail
    bereits "WAITING_FILL" ist UND deren pending_exit_reason None ist – dieser
    zweite Teil ist der Diskriminator gegen das Exit-Pendant, das denselben
    status_detail-Wert nutzt, aber immer einen pending_exit_reason gesetzt hat
    (siehe Trade.status_detail-Docstring in database.py). Trifft KEINE neue
    Kauf-Entscheidung (die trifft ausschließlich run_entry_cycle für neue
    Kandidaten) – klärt ausschließlich, was aus DIESER bereits abgeschickten
    Order geworden ist.

    place_trade() legt einen solchen Trade mit entry_price=None an, wenn die
    Kauf-Order innerhalb des 3s-Polls nicht gefüllt war – KEIN geratener Preis
    fließt dadurch je als "der" Einstiegspreis in SL/TP/Trailing/PnL ein.
    Diese Funktion trägt entry_price/capital_used JETZT ERST mit dem echten
    Fill-Preis nach, sobald er feststeht; ab dann läuft der Trade regulär wie
    jeder sofort gefüllte.
    """
    ticker = trade.ticker
    user_id = trade.user_id if trade.user_id is not None else DEFAULT_USER_ID
    client_order_id = trade.pending_client_order_id

    client = _get_alpaca_client(user_id)
    if not client:
        print(f"⚠️  {ticker}: Kauf-Fill-Reconciliation (Trade #{trade.id}) übersprungen – "
              f"Alpaca-Client nicht verfügbar.")
        return

    try:
        order = client.get_order_by_client_order_id(client_order_id)
    except Exception as e:
        # Anders als beim Exit-Pendant NICHT zurücksetzen: für einen Exit kann
        # der nächste Zyklus einen frischen Verkaufsversuch starten, wenn der
        # alte nachweislich nie ankam - für einen Entry gibt es keine
        # äquivalente automatische Retry-Kauf-Logik, die diesen Trade sonst
        # ersetzen würde. Bleibt daher in WAITING_FILL, bis entweder ein
        # nachfolgender Zyklus die Order doch findet, oder der
        # Staleness-Alarm unten manuelles Eingreifen anstößt.
        print(f"⚠️  {ticker}: Kauf-Order {client_order_id} (Trade #{trade.id}) bei Alpaca nicht "
              f"auffindbar ({e}) – bleibt in status_detail=WAITING_FILL, nächster Zyklus prüft erneut nach.")
        return

    if order.filled_avg_price:
        entry_price = float(order.filled_avg_price)
        trade.entry_price = entry_price
        trade.capital_used = round(trade.quantity * entry_price, 2)
        trade.status_detail = None
        trade.pending_client_order_id = None
        session.commit()
        print(f"✅ {ticker}: Kauf-Order {client_order_id} war tatsächlich gefüllt (@ ${entry_price}) – "
              f"Trade #{trade.id} ab jetzt regulär überwacht (SL ${trade.stop_loss}, TP ${trade.take_profit}).")
        return

    if order.status in ("canceled", "rejected", "expired"):
        print(f"ℹ️  {ticker}: Kauf-Order {client_order_id} (Trade #{trade.id}) wurde storniert/abgelehnt/"
              f"abgelaufen (Status: {order.status}) – war nie real gekauft, wird als FAILED_ENTRY "
              f"geschlossen (kein Kapital bleibt dafür reserviert).")
        trade.status = "FAILED_ENTRY"
        trade.status_detail = None
        trade.pending_client_order_id = None
        trade.closed_at = datetime.utcnow()
        session.commit()
        send_email(
            subject=f"⚠️ Trading Bot – Kauf-Order nie gefüllt ({ticker})",
            body=(
                f"Trade #{trade.id} ({ticker}, Nutzer {user_id}): Kauf-Order {client_order_id} wurde "
                f"storniert/abgelehnt/abgelaufen (Status: {order.status}), bevor sie gefüllt wurde.\n\n"
                f"Der Trade wurde als FAILED_ENTRY markiert, kein Kapital bleibt dafür reserviert."
            )
        )
        return

    # Order existiert weiterhin, ist aber weder gefüllt noch final (z.B.
    # weiterhin "accepted"/"new", weil der Markt seit Order-Aufgabe noch nicht
    # offen war) – keine Aktion außer ggf. einem Staleness-Alarm. Sendet bei
    # jedem weiteren Zyklus erneut, solange der Zustand anhält (keine
    # Dedupe-Logik, analog zu check_position_consistency()).
    #
    # BEWUSST NICHT count_trading_days() (zählt INKLUSIVE - der Anlage-Tag
    # selbst zählt dort schon als Tag 1, siehe dessen Docstring/Verwendung
    # beim Time-Exit) - das würde hier sofort am Anlage-Tag selbst alarmieren,
    # nicht erst "bis Handelsschluss desselben Tages nicht gefüllt". Stattdessen
    # simpler ET-Kalendertag-Vergleich: erst wenn der ET-Kalendertag seit der
    # Order-Anlage weitergerückt ist, ist deren Handelsschluss sicher vorbei.
    et_tz = pytz.timezone("America/New_York")
    created_et_date = trade.created_at.replace(tzinfo=pytz.UTC).astimezone(et_tz).date()
    now_et_date = datetime.now(et_tz).date()
    if now_et_date > created_et_date:
        print(f"🚨 {ticker}: Kauf-Order {client_order_id} (Trade #{trade.id}) seit {trade.created_at.date()} "
              f"weiterhin nicht final (Status: {order.status}) – mindestens ein Handelstag ohne Fill vergangen.")
        send_email(
            subject=f"🚨 Trading Bot – Kauf-Order seit über einem Handelstag nicht gefüllt ({ticker})",
            body=(
                f"Trade #{trade.id} ({ticker}, Nutzer {user_id}): Kauf-Order {client_order_id}, "
                f"angelegt am {trade.created_at}, ist bei Alpaca weiterhin im Status '{order.status}' "
                f"(weder gefüllt noch storniert/abgelehnt/abgelaufen). Bitte manuell bei Alpaca prüfen."
            )
        )
    else:
        print(f"⏳ {ticker}: Kauf-Order {client_order_id} (Trade #{trade.id}) noch nicht final "
              f"(Status: {order.status}) – warte weiter, keine neue Aktion in diesem Zyklus.")


def count_trading_days(start_date, end_date) -> int:
    """
    Zählt Handelstage (Mo-Fr) zwischen start_date und end_date (inklusive) –
    Feiertage werden bewusst nicht berücksichtigt (Näherung, siehe Feature
    Time-based Exit).
    """
    import pandas as pd
    return len(pd.bdate_range(start_date, end_date))


def add_trading_days(start_date, n: int):
    """
    Addiert n Handelstage (Mo-Fr) zu start_date (Umkehrung von
    count_trading_days) – Feiertage bewusst nicht berücksichtigt, gleiche
    Näherung wie dort. Für die Time-Exit-Schutzfrist (2026-07-31, siehe
    monitor_open_positions): das Ergebnis-Datum ist der Tag, ab dem die
    Schutzfrist als abgelaufen gilt.
    """
    import pandas as pd
    return (pd.bdate_range(start=start_date + timedelta(days=1), periods=n)[-1]).date()


def _sell_position_at_alpaca(session, trade: Trade, exit_reason: str, fallback_price: float) -> float | None:
    """
    Verkauft die komplette Alpaca-Position in `trade.ticker` tatsächlich und
    gibt den echten Fill-Preis zurück.

    Kritischer Fix (siehe DUK/NVDA-Vorfall 2026-07-27): monitor_open_positions()
    rief bisher direkt close_trade() auf, das NUR die DB aktualisiert und
    NIE eine Order bei Alpaca platziert hat – Positionen liefen dadurch live
    und komplett ungeschützt weiter, während der Bot sie für geschlossen
    hielt (kein SL/TP/Trailing mehr, da get_open_trades() nur status='OPEN'
    liefert).

    State Machine + Idempotenz (Aufgabe 1+2, 2026-07-30): setzt trade.status_detail
    VOR dem eigentlichen Order-Request auf EXIT_REQUESTED (Entscheidung
    getroffen) und danach auf WAITING_FILL (Order raus, Ausgang unbekannt) –
    jeweils SOFORT committet, bevor der nächste Schritt versucht wird. Stürzt
    der Prozess irgendwo dazwischen ab, erkennt der nächste Monitoring-Zyklus
    über status_detail, dass hier bereits ein Exit läuft, und ruft
    _reconcile_pending_exit() statt einer neuen Exit-Entscheidung auf – so
    kann dieselbe Position nie zweimal verkauft werden. Die eigentliche Order
    nutzt zusätzlich _submit_order_idempotent() (client_order_id), das einen
    unklaren Timeout von einem echten Fehlschlag unterscheidet.

    Gibt None zurück, wenn der Verkauf fehlschlägt – der Trade bleibt dann
    OPEN (status_detail wird zurückgesetzt) und der nächste Monitoring-Zyklus
    versucht es erneut, statt einen nicht tatsächlich verkauften Trade als
    geschlossen zu markieren. Ausnahme: existiert die Alpaca-Position gar
    nicht mehr (z.B. weil eine broker-seitige Bracket-Order sie bei ganzen
    Aktien bereits geschlossen hat), gilt sie als bereits geschlossen und
    `fallback_price` (aktueller Kurs) wird zurückgegeben.

    Multi-Tenant (2026-07-30): nutzt trade.user_id (statt eines eigenen
    Parameters) für den passenden Alpaca-Client UND für die
    PendingOrderAttempt-Zuordnung – jede Trade-Zeile trägt bereits ihren
    Besitzer, ein zusätzlicher Parameter wäre eine redundante zweite Quelle
    der Wahrheit, die auseinanderlaufen könnte.
    """
    ticker = trade.ticker
    user_id = trade.user_id if trade.user_id is not None else DEFAULT_USER_ID

    def _reset_pending():
        trade.status_detail = None
        trade.pending_client_order_id = None
        trade.pending_exit_reason = None
        session.commit()

    # Schritt 1: Entscheidung dokumentieren, BEVOR irgendein Broker-Call passiert.
    trade.status_detail = "EXIT_REQUESTED"
    trade.pending_exit_reason = exit_reason
    session.commit()

    if TRADING_MODE != "LIVE":
        _reset_pending()  # Paper-Modus: kein echter Broker involviert, State Machine nicht nötig
        return fallback_price

    client = _get_alpaca_client(user_id)
    if not client:
        print(f"⚠️  {ticker}: Alpaca-Client nicht verfügbar – Verkauf übersprungen, Trade bleibt OPEN.")
        _reset_pending()
        return None

    try:
        position = client.get_position(ticker)
    except Exception as e:
        if "position does not exist" in str(e).lower() or "404" in str(e):
            print(f"ℹ️  {ticker}: Keine Alpaca-Position mehr vorhanden (vermutlich bereits broker-seitig geschlossen).")
            _reset_pending()
            return fallback_price
        print(f"⚠️  {ticker}: Alpaca-Position konnte nicht abgefragt werden ({e}) – Trade bleibt OPEN.")
        _reset_pending()
        return None

    qty = abs(float(position.qty))
    if qty <= 0:
        _reset_pending()
        return fallback_price

    # Schritt 2: Order wird jetzt WIRKLICH abgeschickt – ab hier ist der
    # Ausgang bis zur Bestätigung unbekannt (Netzwerk-Timeout möglich).
    trade.status_detail = "WAITING_FILL"
    session.commit()

    try:
        order, client_order_id = _submit_order_idempotent(
            client, ticker, user_id, symbol=ticker, qty=qty, side="sell", type="market", time_in_force="day",
        )
        trade.pending_client_order_id = client_order_id
        session.commit()
    except Exception as e:
        print(f"⚠️  {ticker}: Verkaufsorder fehlgeschlagen ({e}) – Trade bleibt OPEN.")
        _reset_pending()
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

    # Kein Fallback-Kurs mehr (Incident 2026-07-30: UNH/AMZN/PSQ wurden mit
    # diesem Platzhalterkurs fälschlich als CLOSED_TIME_EXIT geschlossen,
    # während die Order bei Alpaca noch unfilled war – z.B. weil außerhalb
    # der Handelszeiten platziert). status_detail bleibt bewusst auf
    # WAITING_FILL (pending_client_order_id/pending_exit_reason sind bereits
    # gesetzt) – KEIN close_trade() hier. Der nächste Monitoring-Zyklus
    # erkennt über status_detail, dass diese Position noch aussteht, und
    # ruft _reconcile_pending_exit() auf, das erst bei nachweislich
    # bestätigtem Fill (order.filled_avg_price) close_trade() mit dem
    # ECHTEN Preis aufruft.
    print(f"⏳ {ticker}: Verkaufsorder platziert (Order {client_order_id}), aber innerhalb von 3s nicht gefüllt "
          f"– kein Fallback-Kurs, Trade bleibt in status_detail=WAITING_FILL. Nächster Zyklus prüft per "
          f"Reconciliation nach, ob die Order inzwischen gefüllt wurde.")
    return None


def _reconcile_pending_exit(session, trade: Trade):
    """
    Wird für Positionen aufgerufen, deren status_detail bereits EXIT_REQUESTED
    oder WAITING_FILL ist (Aufgabe 2) – d.h. ein vorheriger Monitoring-Zyklus
    hat einen Exit entschieden/abgeschickt, aber der Prozess wurde davor
    beendet/abgestürzt, bevor close_trade() die Position final schließen
    konnte. Trifft hier bewusst KEINE neue Exit-Entscheidung (kein erneutes
    SL/TP/Trailing/Time-Exit-Check) – sonst könnte ein zweiter, unabhängiger
    Verkauf ausgelöst werden, während der erste ggf. längst gefüllt ist.
    Multi-Tenant (2026-07-30): nutzt trade.user_id für den passenden Client
    (siehe _sell_position_at_alpaca).
    """
    ticker = trade.ticker
    user_id = trade.user_id if trade.user_id is not None else DEFAULT_USER_ID

    if trade.status_detail == "EXIT_REQUESTED" and not trade.pending_client_order_id:
        # Entscheidung wurde dokumentiert, aber der Order-Request selbst kam
        # nie zustande (Absturz zwischen den beiden Schritten) – nichts wurde
        # bei Alpaca ausgelöst, sicher zurückzusetzen und im nächsten Zyklus
        # normal neu zu entscheiden.
        print(f"🔎 {ticker}: EXIT_REQUESTED ohne abgeschickte Order – wurde nie an Alpaca gesendet, setze zurück.")
        trade.status_detail = None
        trade.pending_exit_reason = None
        session.commit()
        return

    client_order_id = trade.pending_client_order_id
    client = _get_alpaca_client(user_id)
    if not client:
        print(f"⚠️  {ticker}: Reconciliation übersprungen – Alpaca-Client nicht verfügbar.")
        return

    print(f"🔎 {ticker}: Position hängt in status_detail={trade.status_detail} (Order {client_order_id}) – "
          f"prüfe bei Alpaca nach, bevor irgendetwas Neues ausgelöst wird.")
    try:
        order = client.get_order_by_client_order_id(client_order_id)
    except Exception as e:
        print(f"⚠️  {ticker}: Order {client_order_id} bei Alpaca nicht auffindbar ({e}) – vermutlich nie "
              f"angekommen, setze zurück für einen frischen Versuch im nächsten Zyklus.")
        trade.status_detail = None
        trade.pending_client_order_id = None
        trade.pending_exit_reason = None
        session.commit()
        return

    if order.filled_avg_price:
        exit_price = float(order.filled_avg_price)
        print(f"✅ {ticker}: Order {client_order_id} war tatsächlich gefüllt (@ ${exit_price}) – schließe Trade final ab.")
        close_trade(session, trade, exit_price, trade.pending_exit_reason or "CLOSED_MANUAL")
        session.commit()
    elif order.status in ("canceled", "rejected", "expired"):
        print(f"ℹ️  {ticker}: Order {client_order_id} wurde storniert/abgelehnt (Status: {order.status}) – "
              f"setze zurück für einen frischen Versuch im nächsten Zyklus.")
        trade.status_detail = None
        trade.pending_client_order_id = None
        trade.pending_exit_reason = None
        session.commit()
    else:
        print(f"⏳ {ticker}: Order {client_order_id} noch nicht final (Status: {order.status}) – warte weiter, "
              f"keine neue Aktion in diesem Zyklus.")


def check_position_consistency(user_id: int = DEFAULT_USER_ID):
    """
    Positions-Konsistenz-Watchdog (Aufgabe 3, 2026-07-30): vergleicht JEDE
    DB-Position mit status=OPEN gegen die tatsächlich bei Alpaca offenen
    Positionen (GET /v2/positions via client.list_positions()) – unabhängig
    vom sonstigen SL/TP-Software-Monitoring in monitor_open_positions().
    Multi-Tenant (2026-07-30): pro Nutzer aufzurufen (main.run_entry_cycle
    ruft sie einmal je verbundenem Nutzer), da sowohl der Alpaca-Client als
    auch die DB-Vergleichsbasis (get_open_trades/Trade-Query) strikt an
    user_id gebunden sind – sonst würde z.B. Nutzer As Live-Position gegen
    Nutzer Bs DB-Trade-Historie für denselben Ticker verglichen.
    Bewusst eine eigenständige Funktion (nicht mit monitor_open_positions()
    vermischt), analog zu check_stop_order_health() im Saxo-Bot, aber
    allgemeiner: hier geht es um reine Positions-EXISTENZ, nicht um
    Stop-Order-Schutz (das Alpaca-Pendant zu Stop-Order-Schutz ist ohnehin
    broker-seitig irrelevant, da Alpaca-Trades rein software-überwacht sind).

    Fehlt eine DB-OPEN-Position bei Alpaca (z.B. durch einen nicht sauber
    verarbeiteten Exit, eine manuelle Aktion im Alpaca-Dashboard, oder eine
    Broker-seitige Zwangsliquidation): Alarm-Mail, da das auf eine Daten-
    inkonsistenz zwischen Bot-DB und Broker-Wahrheit hindeutet, die sonst
    unbemerkt bliebe (der Bot würde weiter versuchen, eine Position zu
    "verwalten", die es beim Broker gar nicht mehr gibt).

    Rückrichtung (Aufgabe 3, 2026-07-30, Incident UNH/AMZN/PSQ): prüft
    zusätzlich JEDE bei Alpaca tatsächlich offene Position gegen den
    zuletzt angelegten DB-Trade für denselben Ticker – ist dessen Status
    NICHT OPEN, hält der Broker eine Position, die laut DB gar nicht (mehr)
    existieren dürfte. Genau dieses Muster trat beim Incident auf: der
    3s-Fill-Timeout in _sell_position_at_alpaca() übernahm vor dem Fix einen
    Platzhalterkurs, obwohl die Verkaufsorder bei Alpaca noch unfilled war –
    ohne diesen Check wäre das nur durch zufälliges manuelles Nachschauen
    aufgefallen. Ein 2-Minuten-Puffer ab closed_at vermeidet False Positives
    durch normale Settlement-Verzögerung zwischen Fill-Bestätigung und
    Alpacas list_positions()-Antwort.
    """
    from datetime import timedelta

    client = _get_alpaca_client(user_id)
    if not client:
        print(f"⚠️  Positions-Konsistenz-Check (Nutzer {user_id}): Alpaca-Client nicht verfügbar, übersprungen.")
        return

    try:
        live_positions = client.list_positions()
    except Exception as e:
        print(f"⚠️  Positions-Konsistenz-Check (Nutzer {user_id}): Alpaca-Positionsabfrage fehlgeschlagen: {e} – Check übersprungen.")
        return

    problems = []

    with get_session() as session:
        open_trades = get_open_trades(session, user_id)
        live_tickers = {p.symbol for p in live_positions}
        # Trades, deren Kauf-Order noch WAITING_FILL ist (Fix 2026-07-31, siehe
        # place_trade()/_reconcile_pending_entry_fill()), haben per Definition
        # noch KEINE Alpaca-Position - würden hier sonst fälschlich als
        # "verschwundene Position" alarmiert, obwohl der Kauf schlicht noch
        # nicht gefüllt ist.
        missing = [
            t for t in open_trades
            if t.ticker not in live_tickers
            and not (t.status_detail == "WAITING_FILL" and t.pending_exit_reason is None)
        ]
        if missing:
            tickers_str = ", ".join(f"{t.ticker} (Trade #{t.id})" for t in missing)
            problems.append(
                f"{len(missing)} DB-Position(en) als OPEN markiert, aber bei Alpaca nicht mehr auffindbar: "
                f"{tickers_str}.\n"
                "Mögliche Ursache: nicht sauber verarbeiteter Exit, manuelle Aktion im Alpaca-Dashboard, "
                "oder Broker-seitige Zwangsliquidation. Der Bot verwaltet diese Position(en) in der DB "
                "weiter als offen, obwohl sie beim Broker nicht mehr existieren."
            )

        stale_cutoff = datetime.utcnow() - timedelta(minutes=2)
        phantom = []
        for pos in live_positions:
            ticker = pos.symbol
            latest_trade = (
                session.query(Trade)
                .filter_by(ticker=ticker, broker="alpaca", user_id=user_id)
                .order_by(Trade.id.desc())
                .first()
            )
            if latest_trade is None or latest_trade.status == "OPEN":
                continue
            if latest_trade.closed_at and latest_trade.closed_at > stale_cutoff:
                continue  # frisch geschlossen – vermutlich nur Settlement-Verzögerung
            phantom.append(latest_trade)
        if phantom:
            tickers_str = ", ".join(f"{t.ticker} (Trade #{t.id}, Status: {t.status})" for t in phantom)
            problems.append(
                f"{len(phantom)} Position(en) bei Alpaca noch offen, obwohl der zuletzt zugehörige "
                f"DB-Trade als geschlossen markiert ist: {tickers_str}.\n"
                "Mögliche Ursache: Exit-Order wurde bei Broker platziert, aber nie tatsächlich gefüllt "
                "(z.B. außerhalb der Handelszeiten), während die DB bereits einen Platzhalterkurs als "
                "Fill übernommen hat. Bitte manuell prüfen und ggf. Order-Status bei Alpaca nachschlagen."
            )

    user_label = "Alpaca" if user_id == DEFAULT_USER_ID else f"Alpaca, Nutzer {user_id}"
    if problems:
        msg = "\n\n".join(problems)
        print(f"🚨 Positions-Konsistenz-Watchdog ({user_label}): {msg}")
        send_email(subject=f"🚨 Positions-Konsistenz-Warnung ({user_label})", body=msg)
    else:
        print(f"✅ Positions-Konsistenz-Check ({user_label}): alle {len(open_trades)} offene(n) DB-Position(en) "
              f"und {len(live_positions)} Alpaca-Live-Position(en) konsistent.")


def _time_exit_currently_allowed(session, user_id: int = DEFAULT_USER_ID) -> bool:
    """
    Guard für Time-Exit-Verkäufe (Aufgabe 2026-07-30, Incident UNH/AMZN/PSQ):
    days_held >= max_days sagt nichts darüber aus, ob der Markt gerade offen
    ist – monitor_open_positions() kann auch außerhalb der Handelszeiten
    laufen (z.B. manueller Testlauf). Eine dann via _sell_position_at_alpaca()
    platzierte Market-Order wird von Alpaca zwar angenommen, bleibt aber bis
    Markteröffnung ungefüllt; ohne diesen Guard hätte close_trade() (vor dem
    Fix in _sell_position_at_alpaca()) einen Platzhalterkurs als echten Fill
    übernommen. Zusätzlich zu is_open ein Puffer bis zum ersten aktiven
    Entry-Zeitslot (typischerweise 09:45 ET) – analog zum Eröffnungs-
    Volatilitäts-Puffer, den Entries bereits nutzen –, damit ein Time-Exit
    nicht direkt in der volatilen ersten Handelsminute feuert. SL/TP/Trailing
    sind von diesem Guard NICHT betroffen und bleiben sofort-reagierend.

    user_id (Multi-Tenant, 2026-07-30) bestimmt nur, WESSEN Client für den
    Markt-Uhr-Abruf genutzt wird (NYSE-Handelszeiten sind objektiv identisch
    für alle Nutzer) – schlägt genau dieser Client fehl, wird der Time-Exit
    für DIESEN Nutzer in diesem Zyklus ausgesetzt, statt einen anderen
    Nutzer-Client als Fallback zu missbrauchen.
    """
    client = _get_alpaca_client(user_id)
    if not client:
        return False
    try:
        clock = client.get_clock()
    except Exception as e:
        print(f"⚠️  Time-Exit-Guard: Alpaca-Clock nicht abrufbar ({e}) – Time-Exit für diesen Zyklus ausgesetzt.")
        return False
    if not clock.is_open:
        return False

    slots = get_active_entry_time_slots(session)
    if slots:
        buffer_hour, buffer_minute = slots[0].stunde_et, slots[0].minute_et
    else:
        buffer_hour, buffer_minute = 9, 45  # Fallback, falls keine Slots konfiguriert

    now_et = datetime.now(pytz.timezone("America/New_York"))
    return (now_et.hour, now_et.minute) >= (buffer_hour, buffer_minute)


def monitor_open_positions(user_id: int = DEFAULT_USER_ID):
    """
    Prüft alle offenen Positionen EINES Nutzers gegen aktuelle Preise (Multi-
    Tenant, 2026-07-30 – main.run_monitoring_cycle ruft dies einmal je
    verbundenem Nutzer auf; user_id=DEFAULT_USER_ID hält den bisherigen
    Single-User-Aufrufer unverändert).

    BUGFIX 2026-08-08 (Aufgabe "Guardrails pro Nutzer"): SL/TP-Management-
    Parameter (MAX_HOLDING_DAYS/-_TRAILING_MULTIPLIER/TIME_EXIT_GRACE_DAYS,
    ATR_MULTIPLIER_SL/ATR_MIN_SL_PCT/ATR_MAX_SL_PCT, TRAILING_ACTIVATION_PCT)
    lasen bisher IMMER get_live_config() (Daniels globale bot_config), obwohl
    user_id hier längst als Parameter vorliegt und diese Funktion ohnehin nur
    die eigenen, bereits offenen Positionen GENAU DIESES Nutzers anfasst –
    jeder Kunde bekam sein Time-Exit-/Trailing-Verhalten also nach Daniels
    Werten statt nach seinen eigenen (siehe DEFAULT_USER_CONFIG in
    database.py). get_user_live_config(user_id) liefert für DEFAULT_USER_ID
    unverändert dieselbe globale bot_config wie vorher (kein Verhaltens-
    unterschied für Daniel). Der Entry-seitige SL/TP-Preis (rule_engine.
    analyze_ticker, EINMAL pro Ticker pro Scan für alle Nutzer berechnet)
    bleibt bewusst unverändert global – nur wie eine bereits offene Position
    danach verwaltet wird, ist jetzt pro Nutzer konfigurierbar.
    - Time-based Exit: Ohne aktiven Trailing-SL wird die Position nach
      MAX_HOLDING_DAYS Handelstagen geschlossen (CLOSED_TIME_EXIT) – AUSSER
      (Schutzfrist-Feature, 2026-07-31) sie steht zu diesem Zeitpunkt im Plus:
      dann bekommt sie statt des sofortigen harten Verkaufs einen nachgezogenen
      Stop auf hälftige Gewinnsicherung (trade.stop_loss = entry_price +
      halber bisheriger Kursgewinn, Korrektur 2026-07-31 - ursprünglich
      Break-Even, siehe Commit c6a0df1) und bis zu TIME_EXIT_GRACE_DAYS
      weitere Handelstage Zeit, entweder die Trailing-Aktivierungsschwelle
      noch zu erreichen (dann übernimmt das normale Trailing-Verhalten
      vollständig, inkl. dessen eigenem Hard-Cap) oder den nachgezogenen Stop
      auszulösen (normaler CLOSED_SL-Exit, hälftiger Gewinn ggü. Entry bleibt
      gesichert) – läuft
      die Schutzfrist dagegen ab, ohne dass eines von beidem passiert ist,
      wird hart verkauft (weiterhin CLOSED_TIME_EXIT, aber
      trade.time_exit_grace_used=True markiert diesen Fall als "nach
      abgelaufener Schutzfrist" für Backlook/post_exit_tracking). Nur EINMAL
      pro Position gewährt (time_exit_grace_used-Flag). Mit aktivem
      Trailing-SL wird der reguläre Time-Exit ausgesetzt (der Trade läuft
      bereits profitabel mit eigenem adaptiven Schutz) – als Sicherheitsnetz
      greift stattdessen eine harte Obergrenze bei MAX_HOLDING_DAYS *
      MAX_HOLDING_DAYS_TRAILING_MULTIPLIER Handelstagen (CLOSED_TIME_EXIT_
      HARD_CAP). Alle Time-Exit-Varianten legen einen post_exit_tracking-
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
    from trading_shared.atr import clamped_trailing_distance

    config = get_user_live_config(user_id)
    max_days = int(config.get("MAX_HOLDING_DAYS", 5))
    max_days_trailing_multiplier = int(config.get("MAX_HOLDING_DAYS_TRAILING_MULTIPLIER", 2))
    grace_days = int(config.get("TIME_EXIT_GRACE_DAYS", 3))
    atr_multiplier_sl = config.get("ATR_MULTIPLIER_SL", 1.5)
    min_sl_pct = config.get("ATR_MIN_SL_PCT", 0.01)
    max_sl_pct = config.get("ATR_MAX_SL_PCT", 0.08)
    trailing_activation_pct = config.get("TRAILING_ACTIVATION_PCT", 0.06)

    def _clamped_trailing_distance(atr, reference_price):
        """ATR-basierte Trailing-Distanz – seit Audit Chunk 1 (2026-08-05) in
        trading_shared.atr (identisch zur Saxo-Version, siehe dort)."""
        return clamped_trailing_distance(atr, reference_price, atr_multiplier_sl, min_sl_pct, max_sl_pct)

    with get_session() as session:
        open_trades = get_open_trades(session, user_id)
        if not open_trades:
            return

        user_label = "" if user_id == DEFAULT_USER_ID else f" (Nutzer {user_id})"
        print(f"👁️  Monitoring {len(open_trades)} offene Position(en){user_label}...")

        # Einmal pro Zyklus geprüft (nicht pro Trade) – Guard siehe
        # _time_exit_currently_allowed(). Betrifft NUR Time-Exit, SL/TP/
        # Trailing unten bleiben unverändert sofort-reagierend.
        time_exit_allowed = _time_exit_currently_allowed(session, user_id)

        # Graceful Shutdown (Bugfix 2026-08-06): rein informativ (einmaliges
        # Log) – der Zyklus läuft bewusst zu Ende, ein Abbruch mitten in
        # dieser Schleife könnte eine bereits vorbereitete Sell-Order
        # (SL/TP/Trailing/Time-Exit) unvollständig lassen, siehe
        # graceful_shutdown.py.
        shutdown_notice_logged = False

        for trade in open_trades:
            if is_shutdown_requested() and not shutdown_notice_logged:
                print(f"   ℹ️  Shutdown angefordert (aktuelle Position: {trade.ticker}) – Monitoring "
                      f"läuft trotzdem normal zu Ende (kein Abbruch mitten in SL/TP/Trailing-Check).")
                shutdown_notice_logged = True
            try:
                # State-Machine-Gate (Aufgabe 2, 2026-07-30, erweitert Fix
                # 2026-07-31 um die Entry-Seite): steckt diese Position bereits
                # mitten in einem Kauf- ODER Verkaufs-Versuch (z.B. weil der
                # Prozess zwischen Order-Platzierung und Bestätigung
                # abgestürzt ist, oder die Kauf-Order beim Anlegen des Trades
                # noch nicht gefüllt war, siehe place_trade()), wird HIER keine
                # neue Exit-Entscheidung getroffen (kein SL/TP/Trailing/Time-
                # Exit-Check gegen einen unbestätigten entry_price) – stattdessen
                # nur der jeweils offene Versuch aufgelöst. WAITING_FILL wird auf
                # beiden Seiten genutzt; pending_exit_reason (nur auf der Exit-
                # Seite gesetzt) unterscheidet, welches Pendant zuständig ist.
                if trade.status_detail == "WAITING_FILL" and trade.pending_exit_reason is None:
                    _reconcile_pending_entry_fill(session, trade)
                    continue
                if trade.status_detail:
                    _reconcile_pending_exit(session, trade)
                    continue

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
                    if time_exit_allowed and days_held >= max_days * max_days_trailing_multiplier:
                        time_exit_reason = "CLOSED_TIME_EXIT_HARD_CAP"
                elif trade.time_exit_grace_used:
                    # Schutzfrist wurde in einem früheren Zyklus bereits
                    # gewährt (Fix 2026-07-31) - der reguläre MAX_HOLDING_DAYS-
                    # Trigger unten ist für diese Position hinfällig, jetzt
                    # zählt nur noch, ob die Schutzfrist selbst abgelaufen ist
                    # (Trailing wäre sonst schon oben abgefangen worden).
                    if time_exit_allowed and datetime.now().date() >= trade.time_exit_grace_deadline:
                        time_exit_reason = "CLOSED_TIME_EXIT"
                        print(f"⏰ {trade.ticker}: Schutzfrist abgelaufen ({trade.time_exit_grace_deadline}) "
                              f"ohne Trailing-Aktivierung – harter Verkauf nach Schutzfrist.")
                    else:
                        time_exit_reason = None
                elif time_exit_allowed and days_held >= max_days:
                    # Regulärer Time-Exit-Trigger erreicht - Schutzfrist-
                    # Unterscheidung (Fix 2026-07-31): nur Gewinner ohne
                    # Trailing-Aktivierung bekommen statt des sofortigen
                    # harten Verkaufs einen nachgezogenen Stop (hälftige
                    # Gewinnsicherung, Korrektur 2026-07-31 - ursprünglich
                    # Break-Even, siehe c6a0df1) + Aufschub.
                    unrealized_pnl = (current_price - trade.entry_price) * trade.quantity
                    if unrealized_pnl > 0:
                        # Hälftige Gewinnsicherung statt Break-Even: die
                        # Haelfte des bisher erreichten Kursgewinns bleibt bei
                        # einem Stop-Treffer gesichert, statt Gewinn komplett
                        # gegen Null abzugeben. Bei z.B. +4% liegt der neue
                        # Stop bei ca. +2% ueber Entry, nicht bei 0%.
                        half_gain_stop = round(trade.entry_price + (current_price - trade.entry_price) / 2, 2)
                        trade.time_exit_grace_deadline = add_trading_days(datetime.now().date(), grace_days)
                        trade.time_exit_grace_used = True
                        trade.stop_loss = half_gain_stop
                        time_exit_reason = None
                        print(f"🛡️  {trade.ticker}: Time-Exit fällig (Tag {days_held}), aber im Plus "
                              f"(${unrealized_pnl:.2f}) und Trailing noch nicht aktiv – Schutzfrist bis "
                              f"{trade.time_exit_grace_deadline} gewährt, Stop-Loss auf hälftige "
                              f"Gewinnsicherung (${half_gain_stop}, Entry war ${trade.entry_price}) nachgezogen.")
                    else:
                        # Break-Even oder Verlust: unverändertes Verhalten,
                        # sofortiger harter Verkauf wie vor diesem Fix.
                        time_exit_reason = "CLOSED_TIME_EXIT"
                else:
                    time_exit_reason = None

                if time_exit_reason:
                    real_exit_price = _sell_position_at_alpaca(session, trade, time_exit_reason, current_price)
                    if real_exit_price is None:
                        print(f"⏭️  {trade.ticker}: Time-Exit-Verkauf nicht final abgeschlossen (fehlgeschlagen ODER noch unfilled) – bleibt OPEN, nächster Zyklus prüft nach.")
                        continue
                    close_trade(session, trade, real_exit_price, time_exit_reason)
                    start_tracking_if_applicable(session, trade)
                    if time_exit_reason == "CLOSED_TIME_EXIT_HARD_CAP":
                        label = "Time-Exit (harte Obergrenze bei aktivem Trailing-SL)"
                    elif trade.time_exit_grace_used:
                        # Fix 2026-07-31: unterscheidet im Log klar einen
                        # regulären Time-Exit (Gewinn <= 0, sofort bei
                        # MAX_HOLDING_DAYS) von einem nach abgelaufener
                        # Schutzfrist (siehe trade.time_exit_grace_used/
                        # -deadline, auch für Backlook/post_exit_tracking
                        # unterscheidbar).
                        label = "Time-Exit (nach abgelaufener Schutzfrist)"
                    else:
                        label = "Time-Exit (regulär)"
                    print(f"⏰ {trade.ticker}: {label} nach {days_held} Handelstagen (PnL: ${trade.pnl_usd:.2f})")
                    continue

                if not trade.trailing_sl_active:
                    # Phase 1: Normaler fester SL/TP
                    if current_price <= trade.stop_loss:
                        real_exit_price = _sell_position_at_alpaca(session, trade, "CLOSED_SL", current_price)
                        if real_exit_price is None:
                            print(f"⏭️  {trade.ticker}: SL-Verkauf nicht final abgeschlossen (fehlgeschlagen ODER noch unfilled) – bleibt OPEN, nächster Zyklus prüft nach.")
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
                        real_exit_price = _sell_position_at_alpaca(session, trade, "CLOSED_TRAILING_SL", current_price)
                        if real_exit_price is None:
                            print(f"⏭️  {trade.ticker}: Trailing-SL-Verkauf nicht final abgeschlossen (fehlgeschlagen ODER noch unfilled) – bleibt OPEN, nächster Zyklus prüft nach.")
                            continue
                        close_trade(session, trade, real_exit_price, "CLOSED_TRAILING_SL")
                        print(f"🟢 Trailing SL ausgelöst: {trade.ticker} @ ${real_exit_price} (PnL: ${trade.pnl_usd:.2f})")
                        continue

            except Exception as e:
                print(f"⚠️  Fehler beim Monitoring von {trade.ticker}: {e}")

        session.commit()


def get_portfolio_value(user_id: int = DEFAULT_USER_ID) -> float:
    """
    Berechnet aktuellen Portfolio-Wert für EINEN Nutzer:
    Startkapital + realisierter P&L + unrealisierter P&L offener Positionen.
    user_id=DEFAULT_USER_ID hält jeden bestehenden Aufrufer (Dashboard/API)
    unverändert (siehe config.DEFAULT_USER_ID-Docstring).
    """
    with get_session() as session:
        realized_pnl = get_total_pnl(session, user_id)
        open_trades = get_open_trades(session, user_id)

        unrealized_pnl = 0.0
        for trade in open_trades:
            try:
                import yfinance as yf
                current_price = yf.Ticker(trade.ticker).fast_info.get("lastPrice", trade.entry_price)
                unrealized_pnl += (float(current_price) - trade.entry_price) * trade.quantity
            except Exception as e:
                # Fund 14 (Code-Audit 2026-08-06): Sichtbarkeit statt stillem
                # Verschlucken – ein systematisches yfinance-Problem für eine
                # Ticker-Untergruppe soll auffallen, auch wenn der Fallback
                # (Unrealisiert ≈ 0) selbst unverändert bleibt.
                print(f"⚠️  get_portfolio_value: Preis für {trade.ticker} nicht abrufbar ({e}) – unrealisiert ≈ 0 für diese Position.")

        max_capital_total = get_user_live_config(user_id)["MAX_CAPITAL_TOTAL"]
        return round(max_capital_total + realized_pnl + unrealized_pnl, 2)


def get_bot_performance(days: int = 30, user_id: int = DEFAULT_USER_ID) -> float | None:
    """
    Prozentuale Bot-Performance über die letzten `days` Tage EINES Nutzers
    (Fix 2026-08-04: daily_log hat jetzt eine user_id-Spalte, siehe
    database.DailyLog-Docstring – vorher implizit nur Daniels globaler
    Snapshot) – Vergleichsbasis für rule_engine.get_benchmark_performance()
    (S&P 500 / Nasdaq, bewusst weiterhin global/marktweit, keine
    Nutzerbindung). Nutzt den ältesten daily_log-Snapshot DIESES Nutzers
    innerhalb des Zeitraums als Startwert; None falls noch kein Snapshot in
    diesem Zeitraum existiert (z.B. Nutzer erst seit kurzem verbunden).

    Kapitalfluss-Verzerrungs-Bugfix Chunk 2 (2026-08-11): verwendet jetzt
    trading_shared.performance.compute_twr_performance_pct statt des
    simplen (current-start)/start*100 - die alte Formel zählte jede
    Einzahlung fälschlich als Trading-Gewinn mit (siehe
    trading_shared.performance-Docstring für die volle Begründung/den live
    bestätigten Saxo-Fall). net_deposits summiert alle capital_flows dieses
    Nutzers, die NACH dem Start-Snapshot-Tag erfasst wurden - Flüsse AM
    Snapshot-Tag selbst gelten als bereits im Startwert enthalten (daily_log
    hat keine Uhrzeit, gröbere Granularität als eine untertägige Zuordnung
    hergeben würde). Läuft rückwirkend korrekt für JEDES Zeitfenster, auch
    eines, das vor dem Formel-Deploy beginnt - Chunk 1 hat die komplette
    Kapitalfluss-Historie bereits synchronisiert (kein Teilzeitraum-Problem,
    siehe Moduldoc).
    """
    from datetime import date, timedelta, datetime
    from sqlalchemy import func
    from database import DailyLog
    from trading_shared.performance import compute_twr_performance_pct

    cutoff = date.today() - timedelta(days=days)
    with get_session() as session:
        start_snapshot = session.query(DailyLog).filter(
            DailyLog.log_date >= cutoff, DailyLog.user_id == user_id
        ).order_by(DailyLog.log_date.asc()).first()

        if not start_snapshot or start_snapshot.portfolio_value <= 0:
            return None

        flows_since = datetime.combine(start_snapshot.log_date + timedelta(days=1), datetime.min.time())
        net_deposits = session.query(func.coalesce(func.sum(CapitalFlow.amount), 0.0)).filter(
            CapitalFlow.broker == "alpaca",
            CapitalFlow.user_id == user_id,
            CapitalFlow.occurred_at >= flows_since,
        ).scalar()

    current_value = get_portfolio_value(user_id)
    return compute_twr_performance_pct(start_snapshot.portfolio_value, current_value, net_deposits)


# Kapitalfluss-Erfassung Chunk 1 (2026-08-07): Alpacas Account-Activities-
# Endpoint liefert Ein-/Auszahlungen als eigene "Non-Trade-Activity"-Typen
# CSD (Cash Deposit) und CSW (Cash Withdrawal) - siehe
# https://docs.alpaca.markets/reference/getaccountactivitiesbyactivitytype-1
# (GET /v2/account/activities/{activity_type}, api.alpaca.markets bzw.
# paper-api.alpaca.markets, Header-Auth wie überall sonst in diesem Modul).
# Empirisch verifiziert 2026-08-07 gegen Daniels echtes LIVE-Konto
# (nur lesend, GET, kein Order-relevanter Call): api.get_activities(
# activity_types=["CSD","CSW"]) liefert 1 CSD-Eintrag über $475 vom
# 2026-07-22 zurück (deckt sich exakt mit der bekannten Startkapital-
# Einzahlung) - Feldformat: {"id": "20260722000000000::<uuid>",
# "activity_type": "CSD", "date": "2026-07-22", "net_amount": "475"
# (STRING, kein float!), "currency": "USD", "status": "executed"}.
# Kein dokumentiertes Historientiefen-Limit; die zusammengesetzte "id"
# (Datum + UUID) ist bereits von Alpaca selbst global eindeutig sortierbar
# und dient direkt als broker_reference_id.
CAPITAL_FLOW_ACTIVITY_TYPES = ["CSD", "CSW"]


def _fetch_all_alpaca_capital_activities(client) -> list:
    """
    Holt ALLE CSD/CSW-Activities eines Kontos, seitenweise (page_token/
    page_size, siehe get_activities-Doku) - kein Datums-Cutoff, damit ein
    einmaliger Aufruf sowohl für den initialen Komplett-Backfill als auch
    den täglichen Sync-Job reicht (Dedup passiert ohnehin erst beim Insert
    in capital_flows, ein wiederholter voller Abruf ist nur unnötiger
    Netzwerk-Overhead, keine Korrektheitsgefahr).
    """
    all_activities = []
    page_token = None
    while True:
        batch = client.get_activities(
            activity_types=CAPITAL_FLOW_ACTIVITY_TYPES,
            direction="asc",
            page_size=100,
            page_token=page_token,
        )
        if not batch:
            break
        all_activities.extend(batch)
        if len(batch) < 100:
            break
        page_token = batch[-1].id
    return all_activities


def sync_capital_flows(user_id: int = DEFAULT_USER_ID) -> int:
    """
    Ruft Alpacas CSD/CSW-Activities ab und pflegt neue Datensätze idempotent
    in capital_flows ein (Dedup über die UniqueConstraint (broker,
    broker_reference_id) - ein erneuter INSERT-Versuch für eine bereits
    bekannte Activity wird einfach übersprungen, kein Fehler). Dient sowohl
    für den initialen historischen Backfill als auch den täglichen Sync-Job
    (siehe main.py-Scheduler) - beide rufen dieselbe Funktion auf.

    NUR Datenerfassung - fließt in diesem Chunk NOCH NICHT in
    get_bot_performance() oder irgendeine andere Performance-Berechnung ein
    (das ist Chunk 2).

    Returns: Anzahl NEU eingefügter Datensätze (0 bei bereits vollständig
    synchronisiertem Zustand oder falls kein Alpaca-Client verfügbar ist).
    """
    client = _get_alpaca_client(user_id)
    if not client:
        print(f"⚠️  sync_capital_flows: kein Alpaca-Client für user_id={user_id} verfügbar.")
        return 0

    try:
        activities = _fetch_all_alpaca_capital_activities(client)
    except Exception as e:
        print(f"⚠️  sync_capital_flows: Alpaca-Activities-Abruf fehlgeschlagen (user_id={user_id}): {e}")
        return 0

    inserted = 0
    with get_session() as session:
        existing_ids = {
            r[0] for r in session.query(CapitalFlow.broker_reference_id).filter_by(broker="alpaca").all()
        }
        for a in activities:
            if a.id in existing_ids:
                continue
            try:
                amount = float(a.net_amount)
                occurred_at = datetime.fromisoformat(a.date)
            except (TypeError, ValueError) as e:
                print(f"⚠️  sync_capital_flows: Activity {a.id} übersprungen, unerwartetes Format: {e}")
                continue
            flow_type = "deposit" if a.activity_type == "CSD" else "withdrawal"
            session.add(CapitalFlow(
                user_id=user_id,
                broker="alpaca",
                amount=amount,
                currency=getattr(a, "currency", None) or "USD",
                flow_type=flow_type,
                broker_reference_id=a.id,
                occurred_at=occurred_at,
            ))
            existing_ids.add(a.id)
            inserted += 1
        if inserted:
            session.commit()

    if inserted:
        print(f"💰 sync_capital_flows: {inserted} neue Kapitalfluss-Einträge für user_id={user_id} gespeichert.")
    return inserted
