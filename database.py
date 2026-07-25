"""
database.py – Datenbankmodelle und Datenbankzugriff
Verwendet SQLAlchemy mit SQLite (serverlos, kein Setup nötig).
"""

from datetime import datetime, date
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Date, Text, Boolean, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager
import json

from config import DATABASE_URL, SCORE_WEIGHTS

Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


# ─────────────────────────────────────────────
# MODELLE
# ─────────────────────────────────────────────

class Trade(Base):
    """Jeder einzelne Trade (Paper oder Live)."""
    __tablename__ = "trades"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    ticker         = Column(String(10), nullable=False)
    direction      = Column(String(10), nullable=False)   # 'LONG' (auch für Inverse ETFs)
    instrument_type = Column(String(20), nullable=False)  # 'STOCK' oder 'INVERSE_ETF'
    entry_price    = Column(Float, nullable=False)
    stop_loss      = Column(Float, nullable=False)
    take_profit    = Column(Float, nullable=False)
    quantity       = Column(Float, nullable=False)
    capital_used   = Column(Float, nullable=False)
    rule_score     = Column(Integer, nullable=False)       # 0–100
    llm_sentiment  = Column(Integer, nullable=True)        # 1–10
    llm_summary    = Column(Text, nullable=True)
    llm_risks      = Column(Text, nullable=True)           # JSON-Array als String
    score_breakdown = Column(Text, nullable=True)          # JSON-Objekt: Kriterium -> {score, max, value}
    status         = Column(String(20), default="OPEN")   # OPEN / CLOSED_SL / CLOSED_TP / CLOSED_TRAILING_SL / CLOSED_TIME_EXIT / CLOSED_MANUAL
    exit_price     = Column(Float, nullable=True)
    closed_at      = Column(DateTime, nullable=True)
    pnl_usd        = Column(Float, nullable=True)
    pnl_pct        = Column(Float, nullable=True)
    mode           = Column(String(10), default="PAPER")   # PAPER / LIVE
    atr            = Column(Float, nullable=True)          # ATR(14) zum Einstiegszeitpunkt
    sl_pct         = Column(Float, nullable=True)          # tatsächlich verwendeter SL % (ATR- oder Fallback-basiert)
    tp_pct         = Column(Float, nullable=True)          # tatsächlich verwendeter TP %
    trailing_sl_active         = Column(Boolean, default=False)  # True sobald TP erreicht & Trailing SL statt Verkauf aktiviert
    trailing_sl_price          = Column(Float, nullable=True)    # aktueller Trailing-SL-Preis (nur wenn trailing_sl_active)
    highest_price_since_entry  = Column(Float, nullable=True)    # höchster beobachteter Kurs seit Entry (Basis für Trailing SL)

    def get_llm_risks(self) -> list:
        """Deserialisiert llm_risks JSON-String zu Liste."""
        if self.llm_risks:
            try:
                return json.loads(self.llm_risks)
            except json.JSONDecodeError:
                return []
        return []

    def set_llm_risks(self, risks: list):
        """Serialisiert Risiken-Liste zu JSON-String."""
        self.llm_risks = json.dumps(risks, ensure_ascii=False)

    def get_score_breakdown(self) -> dict:
        """Deserialisiert score_breakdown JSON-String zu Dict."""
        if self.score_breakdown:
            try:
                return json.loads(self.score_breakdown)
            except json.JSONDecodeError:
                return {}
        return {}

    def set_score_breakdown(self, breakdown: dict):
        """Serialisiert Score-Breakdown-Dict zu JSON-String (für den Backlook)."""
        self.score_breakdown = json.dumps(breakdown, ensure_ascii=False, default=str)

    def __repr__(self):
        return f"<Trade {self.ticker} {self.direction} {self.status} PnL={self.pnl_usd}>"


class BotState(Base):
    """Key-Value-Speicher für Bot-Zustand (Tageszähler, Gesamtkapital etc.)."""
    __tablename__ = "bot_state"

    key   = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)

    @staticmethod
    def get(session: Session, key: str, default=None):
        row = session.query(BotState).filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(session: Session, key: str, value):
        row = session.query(BotState).filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            session.add(BotState(key=key, value=str(value)))


class BotConfig(Base):
    """
    Konfigurierbare Bot-Parameter (Guardrails etc.) als Key-Value-Speicher.
    Liegt in der gemeinsamen Postgres-DB, damit das Dashboard (portfolio_os)
    diese Werte anzeigen/ändern kann und der Bot sie beim nächsten Zyklus liest.
    Die hardcoded Konstanten in config.py bleiben als Fail-safe-Fallback bestehen.
    """
    __tablename__ = "bot_config"

    key          = Column(String(100), primary_key=True)
    value        = Column(Text, nullable=False)
    beschreibung = Column(Text, nullable=True)
    updated_at   = Column(DateTime, default=datetime.utcnow,
                          onupdate=datetime.utcnow)


# Initiale Werte für bot_config – werden in init_db() nur gesetzt, falls der
# jeweilige Key noch nicht existiert (Format: key -> (value, beschreibung)).
DEFAULT_CONFIG = {
    "MAX_CAPITAL_TOTAL":       ("475.00", "Gesamtkapital in USD"),
    "MAX_CAPITAL_PER_TRADE":   ("50.00",  "Max. Einsatz pro Trade"),
    "MAX_OPEN_POSITIONS":      ("5",      "Max. offene Positionen"),
    "MAX_TRADES_PER_DAY":      ("3",      "Max. Trades pro Tag"),
    "STOP_LOSS_PCT":           ("0.03",   "Stop Loss %"),
    "TAKE_PROFIT_PCT":         ("0.06",   "Take Profit %"),
    "DAILY_LOSS_LIMIT_PCT":    ("0.05",   "Tagesverlust-Limit %"),
    "MIN_SIGNAL_SCORE":        ("65",     "Minimaler Score"),
    "VIX_PAUSE_THRESHOLD":     ("30",     "VIX-Limit"),
    "MONITORING_INTERVAL_MIN": ("15",     "Monitoring-Intervall Minuten"),
    "ENTRY_LEARNING_MODE":     ("false",  "Backlook-Vorschläge zu Einstiegszeitpunkten automatisch übernehmen"),
    "ATR_MULTIPLIER_SL":       ("1.5",    "ATR Multiplikator Stop Loss"),
    "ATR_MULTIPLIER_TP":       ("3.0",    "ATR Multiplikator Take Profit"),
    "ATR_MIN_SL_PCT":          ("0.01",   "Minimaler SL % (Sicherheitsnetz)"),
    "ATR_MAX_SL_PCT":          ("0.08",   "Maximaler SL % (Sicherheitsnetz)"),
    "MAX_HOLDING_DAYS":        ("5",      "Max. Haltedauer in Handelstagen"),
    "VOLATILE_SEGMENT_PCT":    ("0.33",   "Anteil volatile Titel am Portfolio (0-1)"),
    "VOLATILE_ATR_THRESHOLD":  ("0.025",  "ATR/Preis Ratio ab dem ein Titel als volatil gilt (2.5%)"),
    "EARNINGS_BUFFER_DAYS":    ("3",      "Tage vor Earnings in denen nicht gekauft wird"),
}


class CurrentWeight(Base):
    """
    Aktuell aktive Score-Gewichtung pro Kriterium.
    Startwerte kommen aus config.SCORE_WEIGHTS; der wöchentliche Backlook
    (siehe backlook.py) darf sie danach minimal anpassen. Liegt in der DB
    (statt nur in config.py), damit Bot- und Dashboard-Service auf Railway
    – getrennte Prozesse, gemeinsame Postgres-DB – denselben Stand sehen.
    """
    __tablename__ = "current_weights"

    criterion  = Column(String(50), primary_key=True)
    weight     = Column(Integer, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)


class WeightHistory(Base):
    """Protokoll jeder Gewichtungsanpassung durch den wöchentlichen Backlook."""
    __tablename__ = "weight_history"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    run_at          = Column(DateTime, default=datetime.utcnow)
    criterion       = Column(String(50), nullable=False)
    old_weight      = Column(Integer, nullable=False)
    new_weight      = Column(Integer, nullable=False)
    change          = Column(Integer, nullable=False)
    trades_analyzed = Column(Integer, nullable=False)


class ScanLog(Base):
    """Vollständiges Log jedes Bot-Scans – auch Ticker ohne ausgeführten Trade."""
    __tablename__ = "scan_log"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    scan_time   = Column(DateTime, default=datetime.utcnow)
    slot_et     = Column(String(10), nullable=True)  # z.B. "09:45"
    ticker      = Column(String(10), nullable=False)
    score       = Column(Integer, nullable=False)
    approved    = Column(Boolean, default=False)
    instrument_type = Column(String(20), nullable=True)
    current_price   = Column(Float, nullable=True)

    # Score Breakdown
    rsi             = Column(Float, nullable=True)
    rsi_score       = Column(Integer, nullable=True)
    sma_score       = Column(Integer, nullable=True)
    volume_score    = Column(Integer, nullable=True)
    pe_score        = Column(Integer, nullable=True)
    de_score        = Column(Integer, nullable=True)
    rev_score       = Column(Integer, nullable=True)

    # Warum kein Trade
    ko_reason       = Column(Text, nullable=True)
    guardrail_reason = Column(Text, nullable=True)
    trade_executed  = Column(Boolean, default=False)
    trade_id        = Column(Integer, nullable=True)  # FK zu trades.id
    mode            = Column(String(10), default="LIVE")
    market_regime   = Column(Text, nullable=True)  # "bullish" / "bearish" / "neutral" (siehe rule_engine.get_market_regime)


class DailyLog(Base):
    """Tägliche Zusammenfassung für Performance-Chart."""
    __tablename__ = "daily_log"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    log_date       = Column(Date, default=date.today, unique=True)
    portfolio_value = Column(Float, nullable=False)
    daily_pnl      = Column(Float, default=0.0)
    trades_count   = Column(Integer, default=0)
    open_positions = Column(Integer, default=0)


class EntryTimeSlot(Base):
    """
    Konfigurierbarer Einstiegszeitpunkt (ET) für den Entry-Scheduler (main.py:
    schedule_entry_jobs). gewichtung steuert die Trade-Quote pro Slot; avg_pnl/
    trefferquote/anzahl_trades werden vom wöchentlichen Backlook gelernt
    (siehe backlook.py: analyze_entry_timing).
    """
    __tablename__ = "entry_time_slots"

    id                    = Column(Integer, primary_key=True, autoincrement=True)
    stunde_et             = Column(Integer, nullable=False)
    minute_et             = Column(Integer, nullable=False, default=0)
    gewichtung            = Column(Float, nullable=False, default=1.0)
    avg_pnl               = Column(Float, nullable=True)
    trefferquote          = Column(Float, nullable=True)
    anzahl_trades         = Column(Integer, default=0)
    quelle                = Column(String(20), default="initial")   # initial / backlook / manuell
    aktiv                 = Column(Boolean, default=True)
    vom_nutzer_bestaetigt = Column(Boolean, default=False)
    # Hartes Trade-Limit für diesen Slot (siehe Fix 1 "Konservatives Frühbudget"
    # in main.py: run_entry_cycle). NULL = kein Cap, Restbudget des Tages darf
    # voll ausgeschöpft werden.
    max_trades_per_slot   = Column(Integer, nullable=True, default=None)
    created_at            = Column(DateTime, default=datetime.utcnow)
    updated_at            = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Initiale Einstiegszeitpunkte (siehe Feature-2-Spec) – werden in init_db() nur
# angelegt, falls die Tabelle noch komplett leer ist.
INITIAL_ENTRY_TIME_SLOTS = [
    (9, 45, 1.0),   # nach Opening-Volatilität
    (10, 30, 1.5),  # stärkster historischer Zeitpunkt
    (12, 0, 0.5),   # Mittagskonsolidierung, weniger
    (14, 0, 1.0),   # vor letzter Handelsstunde
    (15, 0, 0.5),   # kurz vor Schluss, vorsichtig
]


# ─────────────────────────────────────────────
# DATENBANKZUGRIFF
# ─────────────────────────────────────────────

def init_db():
    """Erstellt alle Tabellen (idempotent – safe to call multiple times)."""
    Base.metadata.create_all(engine)
    _migrate_entry_time_slots_columns()
    _migrate_trades_atr_columns()
    _migrate_trades_trailing_sl_columns()
    _migrate_scan_log_regime_column()
    # Initiale Bot-State-Werte setzen falls nicht vorhanden
    with get_session() as session:
        if not BotState.get(session, "daily_trade_count"):
            BotState.set(session, "daily_trade_count", "0")
        if not BotState.get(session, "last_reset_date"):
            BotState.set(session, "last_reset_date", str(date.today()))
        if not BotState.get(session, "bot_paused"):
            BotState.set(session, "bot_paused", "false")
        # Gewichtungen mit config-Defaults seeden, falls noch nicht vorhanden
        if not session.query(CurrentWeight).first():
            set_active_weights(session, SCORE_WEIGHTS)
        # Bot-Konfiguration mit Defaults seeden – nur fehlende Keys, damit
        # im Dashboard geänderte Werte bei einem Neustart nicht überschrieben werden.
        for key, (value, beschreibung) in DEFAULT_CONFIG.items():
            if not session.query(BotConfig).filter_by(key=key).first():
                session.add(BotConfig(key=key, value=value, beschreibung=beschreibung))
        # Initiale Einstiegszeitpunkte seeden – nur beim allerersten Start (Tabelle leer).
        if not session.query(EntryTimeSlot).first():
            for stunde, minute, gewichtung in INITIAL_ENTRY_TIME_SLOTS:
                session.add(EntryTimeSlot(
                    stunde_et=stunde, minute_et=minute, gewichtung=gewichtung, quelle="initial"
                ))
        session.commit()
    print("✅ Datenbank initialisiert.")


def _migrate_entry_time_slots_columns():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – max_trades_per_slot (Fix 1 "Konservatives Frühbudget") kam nach-
    träglich zur entry_time_slots-Tabelle dazu, daher ein idempotentes
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Die initialen Caps (09:45/10:30
    konservativ auf 1) werden nur gesetzt, solange die Spalte noch NULL ist –
    ein späterer Backlook-Vorschlag (cap_erhoehen) oder eine manuelle Änderung
    darf beim nächsten Bot-Neustart NICHT wieder überschrieben werden.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE entry_time_slots ADD COLUMN IF NOT EXISTS max_trades_per_slot INTEGER DEFAULT NULL"
        ))
        conn.execute(text(
            "UPDATE entry_time_slots SET max_trades_per_slot = 1 "
            "WHERE stunde_et IN (9, 10) AND minute_et IN (45, 30) AND max_trades_per_slot IS NULL"
        ))


def _migrate_trades_atr_columns():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – atr/sl_pct/tp_pct (ATR-basierter SL/TP) kamen nachträglich zur
    trades-Tabelle dazu, daher ein idempotentes ALTER TABLE ... ADD COLUMN
    IF NOT EXISTS.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS atr FLOAT"))
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS sl_pct FLOAT"))
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS tp_pct FLOAT"))


def _migrate_trades_trailing_sl_columns():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – trailing_sl_active/trailing_sl_price/highest_price_since_entry
    (Trailing-SL-Feature) kamen nachträglich zur trades-Tabelle dazu, daher
    ein idempotentes ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trailing_sl_active BOOLEAN DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS trailing_sl_price FLOAT DEFAULT NULL"))
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS highest_price_since_entry FLOAT DEFAULT NULL"))


def _migrate_scan_log_regime_column():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – market_regime (Regime-Detection-Feature) kam nachträglich zur
    scan_log-Tabelle dazu, daher ein idempotentes ALTER TABLE ... ADD COLUMN
    IF NOT EXISTS.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE scan_log ADD COLUMN IF NOT EXISTS market_regime TEXT"))


@contextmanager
def get_session():
    """Context Manager für sichere Datenbanksessions."""
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ─────────────────────────────────────────────
# TRADE HELPER FUNKTIONEN
# ─────────────────────────────────────────────

def get_open_trades(session: Session) -> list[Trade]:
    return session.query(Trade).filter_by(status="OPEN").all()


def get_daily_trade_count(session: Session) -> int:
    """Zählt Trades die heute eröffnet wurden."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return session.query(Trade).filter(
        Trade.created_at >= today_start,
        Trade.status != "OPEN"  # Zählt auch bereits geschlossene des Tages
    ).count() + session.query(Trade).filter(
        Trade.created_at >= today_start,
        Trade.status == "OPEN"
    ).count()


def get_total_capital_in_trades(session: Session) -> float:
    """Gesamtkapital aktuell in offenen Positionen gebunden."""
    result = session.query(func.sum(Trade.capital_used)).filter_by(status="OPEN").scalar()
    return result or 0.0


# Alle Status-Werte eines abgeschlossenen Trades – zentral gepflegt, damit
# neue Exit-Gründe (z.B. Trailing SL, Time-Exit) nicht in get_total_pnl/
# get_daily_pnl vergessen werden und so das Daily-Loss-Limit (Guardrail!)
# unterlaufen.
CLOSED_STATUSES = ["CLOSED_SL", "CLOSED_TP", "CLOSED_TRAILING_SL", "CLOSED_TIME_EXIT", "CLOSED_MANUAL"]


def get_total_pnl(session: Session) -> float:
    """Gesamter realisierter P&L aller abgeschlossenen Trades."""
    result = session.query(func.sum(Trade.pnl_usd)).filter(
        Trade.status.in_(CLOSED_STATUSES)
    ).scalar()
    return result or 0.0


def get_daily_pnl(session: Session) -> float:
    """Realisierter P&L der heute geschlossenen Trades (für das Daily-Loss-Limit)."""
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = session.query(func.sum(Trade.pnl_usd)).filter(
        Trade.status.in_(CLOSED_STATUSES),
        Trade.closed_at >= today_start
    ).scalar()
    return result or 0.0


def close_trade(session: Session, trade: Trade, exit_price: float, reason: str) -> Trade:
    """Schließt einen Trade und berechnet P&L."""
    trade.exit_price = exit_price
    trade.closed_at  = datetime.utcnow()
    trade.status     = reason  # CLOSED_SL / CLOSED_TP / CLOSED_MANUAL
    trade.pnl_usd    = (exit_price - trade.entry_price) * trade.quantity
    trade.pnl_pct    = (exit_price - trade.entry_price) / trade.entry_price * 100
    return trade


def get_bot_config(session: Session, key: str, default=None):
    """Liest einen Bot-Parameter aus der bot_config Tabelle (roher String)."""
    row = session.query(BotConfig).filter_by(key=key).first()
    return row.value if row else default


def set_bot_config(session: Session, key: str, value):
    """Schreibt/aktualisiert einen Bot-Parameter in der bot_config Tabelle."""
    row = session.query(BotConfig).filter_by(key=key).first()
    if row:
        row.value = str(value)
        row.updated_at = datetime.utcnow()
    else:
        session.add(BotConfig(key=key, value=str(value)))


def get_active_weights(session: Session) -> dict:
    """
    Gibt die aktuell aktiven Score-Gewichtungen zurück.
    Fällt auf config.SCORE_WEIGHTS zurück falls DB noch nicht geseedet ist.
    """
    rows = session.query(CurrentWeight).all()
    if not rows:
        return dict(SCORE_WEIGHTS)
    return {r.criterion: r.weight for r in rows}


def set_active_weights(session: Session, weights: dict):
    """Schreibt neue Gewichtungen in die current_weights Tabelle."""
    now = datetime.utcnow()
    for criterion, weight in weights.items():
        row = session.query(CurrentWeight).filter_by(criterion=criterion).first()
        if row:
            row.weight = weight
            row.updated_at = now
        else:
            session.add(CurrentWeight(criterion=criterion, weight=weight, updated_at=now))


def get_active_entry_time_slots(session: Session) -> list["EntryTimeSlot"]:
    """Aktive Einstiegszeitpunkte, sortiert nach Uhrzeit (für schedule_entry_jobs)."""
    return session.query(EntryTimeSlot).filter_by(aktiv=True).order_by(
        EntryTimeSlot.stunde_et, EntryTimeSlot.minute_et
    ).all()


def get_pending_entry_proposal(session: Session) -> dict | None:
    """Liest den aktuellen Backlook-Zeitpunkt-Vorschlag aus bot_state (siehe analyze_entry_timing)."""
    raw = BotState.get(session, "pending_entry_proposal")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def set_pending_entry_proposal(session: Session, proposal: dict | None):
    """Schreibt (oder löscht bei None) den pending_entry_proposal-Eintrag in bot_state."""
    if proposal is None:
        row = session.query(BotState).filter_by(key="pending_entry_proposal").first()
        if row:
            session.delete(row)
    else:
        BotState.set(session, "pending_entry_proposal", json.dumps(proposal, ensure_ascii=False))


def apply_entry_time_proposal(session: Session, vorschlaege: list[dict]):
    """
    Wendet eine Liste von Backlook-Zeitpunkt-Vorschlägen (Format siehe
    analyze_entry_timing) auf entry_time_slots an – entweder automatisch bei
    aktivem Lernmodus, oder nach manueller Nutzerbestätigung (siehe Feature 4).
    """
    for v in vorschlaege:
        stunde_str, minute_str = v["slot"].split(":")
        stunde, minute = int(stunde_str), int(minute_str)
        slot = session.query(EntryTimeSlot).filter_by(stunde_et=stunde, minute_et=minute).first()
        aktion = v["aktion"]

        if aktion == "deaktivieren":
            if slot:
                slot.aktiv = False
                slot.updated_at = datetime.utcnow()
        elif aktion == "gewichtung_reduzieren":
            if slot:
                slot.gewichtung = max(slot.gewichtung * 0.5, 0.1)
                slot.updated_at = datetime.utcnow()
        elif aktion == "gewichtung_erhoehen":
            if slot:
                slot.gewichtung = v["neu"]["gewichtung"]
                slot.updated_at = datetime.utcnow()
        elif aktion in ("cap_erhoehen", "cap_reduzieren"):
            if slot:
                slot.max_trades_per_slot = v["neu"]["max_trades_per_slot"]
                slot.updated_at = datetime.utcnow()
        elif aktion == "neuen_slot_hinzufuegen":
            if slot:
                slot.aktiv = True
                slot.gewichtung = v["neu"]["gewichtung"]
                slot.updated_at = datetime.utcnow()
            else:
                session.add(EntryTimeSlot(
                    stunde_et=stunde, minute_et=minute,
                    gewichtung=v["neu"]["gewichtung"], quelle="backlook", aktiv=True,
                ))


def save_daily_snapshot(session: Session, portfolio_value: float):
    """Speichert oder aktualisiert den täglichen Portfolio-Snapshot."""
    today = date.today()
    existing = session.query(DailyLog).filter_by(log_date=today).first()
    if existing:
        existing.portfolio_value = portfolio_value
    else:
        session.add(DailyLog(
            log_date=today,
            portfolio_value=portfolio_value,
            trades_count=get_daily_trade_count(session),
            open_positions=len(get_open_trades(session))
        ))


if __name__ == "__main__":
    init_db()
