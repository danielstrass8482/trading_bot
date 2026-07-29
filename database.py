"""
database.py – Datenbankmodelle und Datenbankzugriff
Verwendet SQLAlchemy mit SQLite (serverlos, kein Setup nötig).
"""

from datetime import datetime, date, timedelta
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Date, Text, Boolean, UniqueConstraint, func, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager
import json

from cryptography.fernet import Fernet, InvalidToken

from config import (
    DATABASE_URL, SCORE_WEIGHTS, ENCRYPTION_KEY, SAXO_TOKEN_ENCRYPTION_KEY,
    SAXO_ACCESS_TOKEN_INITIAL, SAXO_REFRESH_TOKEN_INITIAL,
    SAXO_EXPIRES_IN_INITIAL, SAXO_REFRESH_EXPIRES_IN_INITIAL,
)

Base = declarative_base()
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)

# Gleicher Fernet-Key wie portfolio_os/database.py – nur zum ENTSCHLÜSSELN der
# dort verschlüsselten pos_users.alpaca_*_encrypted-Spalten nötig (siehe
# get_alpaca_api_for_user unten, Feature 8 Multi-Tenant).
_fernet = Fernet(ENCRYPTION_KEY.encode()) if ENCRYPTION_KEY else None


def _decrypt_field(value: str) -> str:
    if not _fernet or not value:
        return value
    try:
        return _fernet.decrypt(value.encode()).decode()
    except Exception:
        return value


# Verschlüsselung von saxo_tokens.access_token/refresh_token (seit
# 2026-07-29, siehe config.SAXO_TOKEN_ENCRYPTION_KEY-Docstring) – EIGENER Key,
# nicht ENCRYPTION_KEY (andere Sicherheitsdomäne: Live-Broker-Zugang statt
# Alpaca-Keys). Bewusst KEIN stiller No-Op-Fallback wie _decrypt_field oben:
# Live-Broker-Zugangsdaten sollen bei fehlendem Key laut fehlschlagen statt
# unbemerkt im Klartext gespeichert zu werden. MUSS identisch mit dem Key in
# trading_bot_saxo/.env sein (siehe dortiges database.py – beide Prozesse
# lesen/schreiben dieselbe saxo_tokens-Zeile).
_saxo_token_fernet = Fernet(SAXO_TOKEN_ENCRYPTION_KEY.encode()) if SAXO_TOKEN_ENCRYPTION_KEY else None


def _require_saxo_token_fernet() -> Fernet:
    if _saxo_token_fernet is None:
        raise RuntimeError(
            "SAXO_TOKEN_ENCRYPTION_KEY fehlt – Saxo-Token-Verschlüsselung nicht möglich. "
            "Kein Klartext-Fallback (siehe database.py-Modul-Docstring)."
        )
    return _saxo_token_fernet


def _encrypt_saxo_token(plaintext: str) -> str:
    return _require_saxo_token_fernet().encrypt(plaintext.encode()).decode()


def _looks_like_fernet_token(value: str) -> bool:
    """
    Fernet-Tokens beginnen IMMER mit "gAAAAA" (siehe trading_bot_saxo/
    database.py für die ausführliche Begründung) – dient in
    _decrypt_or_migrate_saxo_token zur Unterscheidung "echtes Klartext-Alt-
    Token" vs. "Ciphertext, das nur mit dem FALSCHEN Key nicht entschlüsselt
    werden konnte" (sonst würde ein falsch konfigurierter Key den echten
    Token fälschlich für Klartext halten und unwiederbringlich überschreiben).
    """
    return value.startswith("gAAAAA")


def _decrypt_or_migrate_saxo_token(session: Session, row: "SaxoToken") -> None:
    """
    Entschlüsselt row.access_token/refresh_token IN-MEMORY (mutiert das ORM-
    Objekt, aber committet nichts von sich aus – kein Autoflush-Risiko, siehe
    trading_bot_saxo/database.py für die identische Implementierung/
    Begründung). InvalidToken kann ZWEI Ursachen haben – echtes Klartext aus
    der Zeit VOR diesem Fix (sicher migrierbar) oder Ciphertext mit einem
    ANDEREN/falschen SAXO_TOKEN_ENCRYPTION_KEY (nicht migrierbar, siehe
    _looks_like_fernet_token) – Fall 2 bricht laut ab statt zu überschreiben.
    """
    fernet = _require_saxo_token_fernet()
    try:
        row.access_token = fernet.decrypt(row.access_token.encode()).decode()
        row.refresh_token = fernet.decrypt(row.refresh_token.encode()).decode()
    except InvalidToken:
        if _looks_like_fernet_token(row.access_token) or _looks_like_fernet_token(row.refresh_token):
            raise RuntimeError(
                "Saxo-Token sieht wie Fernet-Ciphertext aus, lässt sich mit dem aktuell "
                "konfigurierten SAXO_TOKEN_ENCRYPTION_KEY aber nicht entschlüsseln – vermutlich "
                "ein falscher/abweichender Key. ABBRUCH statt Migration, um den echten Token "
                "nicht durch erneute Verschlüsselung des bereits verschlüsselten Werts zu zerstören."
            )
        plaintext_access, plaintext_refresh = row.access_token, row.refresh_token
        row.access_token = fernet.encrypt(plaintext_access.encode()).decode()
        row.refresh_token = fernet.encrypt(plaintext_refresh.encode()).decode()
        session.commit()
        print("🔐 Saxo-Tokens waren im Klartext gespeichert – einmalig verschlüsselt und migriert.")
        row.access_token = plaintext_access
        row.refresh_token = plaintext_refresh


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
    status         = Column(String(20), default="OPEN")   # OPEN / CLOSED_SL / CLOSED_TP / CLOSED_TRAILING_SL / CLOSED_TIME_EXIT / CLOSED_TIME_EXIT_HARD_CAP / CLOSED_MANUAL
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
    broker                     = Column(String(20), default="alpaca")  # "alpaca" / "ibkr" (siehe broker.place_trade)

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


class PostExitTracking(Base):
    """
    Schwellenwert-Wirksamkeitsprüfung (siehe post_exit_tracking.py): verfolgt
    den Kursverlauf eines Tickers für 10 Handelstage NACH einem regelbasierten
    Exit, um zu prüfen ob der auslösende Schwellenwert selbst zu früh/spät
    greift. Aktuell nur für die Holding-Days-Grenze befüllt (CLOSED_TIME_EXIT/
    CLOSED_TIME_EXIT_HARD_CAP) – die `parameter`-Spalte macht die Tabelle
    bewusst wiederverwendbar für künftige Schwellenwerte nach demselben Muster
    (z.B. STOP_LOSS_PCT, ATR_MULTIPLIER_TP, TRAILING_ACTIVATION_PCT), ohne
    dass dafür eine neue Tabelle/neuer Code-Pfad nötig wäre.
    """
    __tablename__ = "post_exit_tracking"

    id                     = Column(Integer, primary_key=True, autoincrement=True)
    trade_id               = Column(Integer, nullable=False)
    ticker                 = Column(String(10), nullable=False)
    parameter              = Column(String(50), nullable=False, default="MAX_HOLDING_DAYS")
    exit_reason            = Column(String(30), nullable=False)
    exit_price             = Column(Float, nullable=False)
    exit_date              = Column(DateTime, nullable=False)
    pnl_pct_at_exit        = Column(Float, nullable=True)
    price_after_5_days     = Column(Float, nullable=True)
    pnl_pct_after_5_days   = Column(Float, nullable=True)
    price_after_10_days    = Column(Float, nullable=True)
    pnl_pct_after_10_days  = Column(Float, nullable=True)
    # None = 10-Tage-Fenster noch nicht ausgewertet; True/False danach fix.
    would_have_more_profit = Column(Boolean, nullable=True)
    # pnl_pct_after_10_days - pnl_pct_at_exit: positiv = entgangener Gewinn
    # durch den Exit, negativ = durch den Exit vermiedener weiterer Verlust.
    forgone_profit_pct     = Column(Float, nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)
    updated_at             = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<PostExitTracking trade={self.trade_id} {self.ticker} {self.parameter} {self.exit_reason}>"


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
    "TRAILING_ACTIVATION_PCT": ("0.06",   "Fixer Trailing-Aktivierungs-Trigger % ggü. Entry (niedrigerer von diesem und ATR-TP löst aus)"),
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
    "MAX_HOLDING_DAYS_TRAILING_MULTIPLIER": ("2", "Harte Obergrenze bei aktivem Trailing-SL = MAX_HOLDING_DAYS x dieser Wert"),
    "VOLATILE_SEGMENT_PCT":    ("0.33",   "Anteil volatile Titel am Portfolio (0-1)"),
    "VOLATILE_ATR_THRESHOLD":  ("0.025",  "ATR/Preis Ratio ab dem ein Titel als volatil gilt (2.5%)"),
    "EARNINGS_BUFFER_DAYS":    ("3",      "Tage vor Earnings in denen nicht gekauft wird"),
    "ACTIVE_BROKER":           ("alpaca", "Aktiver Broker für neue Trades: alpaca / ibkr"),
    "ALPACA_DRAIN_MODE":       ("true",   "Alpaca: Keine neuen Käufe, nur bestehende Positionen managen"),
}


class SaxoToken(Base):
    """
    OAuth Access-/Refresh-Token für die Saxo Bank OpenAPI (LIVE). Es gibt
    genau einen Saxo-Account, daher pflegt upsert_saxo_token() immer nur
    EINE Zeile per UPDATE – es wird nie eine zweite Zeile angelegt. Saxo
    rotiert bei jedem Refresh sowohl Access- als auch Refresh Token (der alte
    Refresh Token wird dabei sofort ungültig), siehe saxo_client.refresh_saxo_token.
    """
    __tablename__ = "saxo_tokens"

    id                       = Column(Integer, primary_key=True, autoincrement=True)
    access_token             = Column(Text, nullable=False)
    refresh_token            = Column(Text, nullable=False)
    access_token_expires_at  = Column(DateTime, nullable=False)
    refresh_token_expires_at = Column(DateTime, nullable=False)
    updated_at               = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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

    # Fair Value (Stufe-1-Gatekeeper, siehe fair_value.py)
    fair_value_avg          = Column(Float, nullable=True)
    fair_value_discount_pct = Column(Float, nullable=True)

    # Aktiver Broker zum Scan-Zeitpunkt (siehe broker.get_broker/Feature Alpaca
    # Drain Mode) – nicht zwingend derselbe wie trades.broker, falls sich
    # ACTIVE_BROKER zwischen Scan und tatsächlicher Order geändert hat.
    broker          = Column(String(20), default="alpaca")


class FairValueCache(Base):
    """
    Wöchentlich (siehe main.py: fair_value_update Job) neu berechneter Fair
    Value je Ticker – Stufe-1-Gatekeeper vor dem täglichen 8-Faktoren-Score
    (siehe fair_value.py, rule_engine.analyze_ticker). Nur Ticker mit
    is_undervalued=True dürfen die Stufe-2-Technikprüfung überhaupt erreichen.
    """
    __tablename__ = "fair_value_cache"

    id     = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False)

    # Aktuelle Kennzahlen
    current_price       = Column(Float)
    pe_ratio             = Column(Float)
    eps                  = Column(Float)
    revenue_per_share    = Column(Float)
    cashflow_per_share   = Column(Float)
    dividend_yield       = Column(Float)

    # Sektor
    sector = Column(String(50))

    # Fair Value Berechnungen
    fair_value_kgv = Column(Float)
    fair_value_kcv = Column(Float)
    fair_value_div = Column(Float)
    fair_value_avg = Column(Float)

    # Bewertung
    discount_pct     = Column(Float)   # % unter Fair Value (positiv = günstig)
    is_undervalued   = Column(Boolean, default=False)
    value_trap_risk  = Column(String(20), default="low")   # low/medium/high

    # Metadata
    updated_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('ticker', name='uq_fair_value_ticker'),
    )


class DailyLog(Base):
    """Tägliche Zusammenfassung für Performance-Chart."""
    __tablename__ = "daily_log"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    log_date       = Column(Date, default=date.today, unique=True)
    portfolio_value = Column(Float, nullable=False)
    daily_pnl      = Column(Float, default=0.0)
    trades_count   = Column(Integer, default=0)
    open_positions = Column(Integer, default=0)


class DailyPositionSnapshot(Base):
    """
    Tages-Snapshot aller offenen Positionen + Gesamt-Portfoliowert, geschrieben
    einmal täglich nach Handelsschluss (siehe main.py: capture_daily_position_snapshot,
    Scheduler-Job 16:05 ET, kurz nach dem letzten Monitoring-Zyklus). Dient als
    "Vortag-Endstand" UND direkt als Vergleichsbasis für die Tages-Mail des
    FOLGENDEN Tages (ein Job reicht, kein separater Morgen-Snapshot nötig).
    Pro Tag immer mindestens eine Zeile mit ticker=NULL (Portfolio-Total, auch
    an Tagen ganz ohne offene Positionen), plus eine Zeile je offener Position.
    """
    __tablename__ = "daily_position_snapshot"

    id                 = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date       = Column(Date, nullable=False)   # ET-Handelstag
    snapshot_time       = Column(DateTime, default=datetime.utcnow)
    ticker              = Column(String(10), nullable=True)  # NULL = Portfolio-Total-Zeile
    trade_id            = Column(Integer, nullable=True)     # FK trades.id, nur bei Ticker-Zeilen
    quantity            = Column(Float, nullable=True)
    entry_price         = Column(Float, nullable=True)
    price               = Column(Float, nullable=True)       # Kurs zum Snapshot-Zeitpunkt
    unrealized_pnl      = Column(Float, nullable=True)
    unrealized_pnl_pct  = Column(Float, nullable=True)
    portfolio_value     = Column(Float, nullable=False)   # Gesamtwert, auf jeder Zeile eines Snapshots identisch


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
    _migrate_trades_broker_column()
    _migrate_scan_log_regime_column()
    _migrate_scan_log_fair_value_columns()
    _seed_saxo_token_from_env()
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


def _migrate_trades_broker_column():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – broker (IBKR-Integration, siehe broker.get_broker/place_trade)
    kam nachträglich zur trades-Tabelle dazu, daher ein idempotentes
    ALTER TABLE ... ADD COLUMN IF NOT EXISTS. Bestehende Trades (alle vor
    diesem Feature) sind ausnahmslos über Alpaca gelaufen, daher DEFAULT 'alpaca'.
    """
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE trades ADD COLUMN IF NOT EXISTS broker VARCHAR(20) DEFAULT 'alpaca'"))
        conn.execute(text("UPDATE trades SET broker = 'alpaca' WHERE broker IS NULL"))
        conn.execute(text("ALTER TABLE scan_log ADD COLUMN IF NOT EXISTS broker VARCHAR(20) DEFAULT 'alpaca'"))
        conn.execute(text("UPDATE scan_log SET broker = 'alpaca' WHERE broker IS NULL"))


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


def _migrate_scan_log_fair_value_columns():
    """
    Base.metadata.create_all() ändert keine Spalten einer bereits bestehenden
    Tabelle – fair_value_avg/fair_value_discount_pct (Fair-Value-Gatekeeper,
    siehe fair_value.py) kamen nachträglich zur scan_log-Tabelle dazu, daher
    ein idempotentes ALTER TABLE ... ADD COLUMN IF NOT EXISTS.
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE scan_log ADD COLUMN IF NOT EXISTS fair_value_avg FLOAT"))
        conn.execute(text("ALTER TABLE scan_log ADD COLUMN IF NOT EXISTS fair_value_discount_pct FLOAT"))


def _seed_saxo_token_from_env():
    """
    Einmaliger initialer Seed der saxo_tokens-Tabelle aus den .env-Werten
    SAXO_ACCESS_TOKEN_INITIAL/SAXO_REFRESH_TOKEN_INITIAL (direkt nach dem
    manuellen OAuth Authorization Code Flow). Läuft nur, solange noch KEINE
    Zeile existiert UND ein initialer Access Token in .env gesetzt ist –
    SAXO_ACCESS_TOKEN_INITIAL kann/soll danach wieder aus .env entfernt werden,
    da der Bot Access/Refresh Token ab dann ausschließlich per Refresh in der
    DB fortschreibt (siehe saxo_client.refresh_saxo_token).
    """
    if not SAXO_ACCESS_TOKEN_INITIAL or not SAXO_REFRESH_TOKEN_INITIAL:
        return
    with get_session() as session:
        if get_saxo_token(session):
            return  # bereits geseedet (oder längst per Refresh überschrieben)
        now = datetime.utcnow()
        # upsert_saxo_token() statt direktem SaxoToken(...)-Insert, damit der
        # Seed-Pfad dieselbe Verschlüsselung durchläuft wie jeder reguläre
        # Refresh (siehe _encrypt_saxo_token) – vorher schrieb dieser Pfad
        # Klartext direkt in die Zeile.
        upsert_saxo_token(
            session,
            access_token=SAXO_ACCESS_TOKEN_INITIAL,
            refresh_token=SAXO_REFRESH_TOKEN_INITIAL,
            access_token_expires_at=now + timedelta(seconds=SAXO_EXPIRES_IN_INITIAL or 1170),
            refresh_token_expires_at=now + timedelta(seconds=SAXO_REFRESH_EXPIRES_IN_INITIAL or 3570),
        )
        session.commit()
    print("✅ Saxo-Token initial aus .env in DB geseedet.")


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
CLOSED_STATUSES = ["CLOSED_SL", "CLOSED_TP", "CLOSED_TRAILING_SL", "CLOSED_TIME_EXIT", "CLOSED_TIME_EXIT_HARD_CAP", "CLOSED_MANUAL"]


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
    trade.status     = reason  # z.B. CLOSED_SL / CLOSED_TP / CLOSED_TRAILING_SL / CLOSED_TIME_EXIT / CLOSED_TIME_EXIT_HARD_CAP / CLOSED_MANUAL
    trade.pnl_usd    = (exit_price - trade.entry_price) * trade.quantity
    trade.pnl_pct    = (exit_price - trade.entry_price) / trade.entry_price * 100
    return trade


def get_saxo_token(session: Session):
    """
    Liest die einzige gepflegte saxo_tokens-Zeile (None falls noch nicht
    geseedet). access_token/refresh_token kommen entschlüsselt zurück (siehe
    _decrypt_or_migrate_saxo_token – migriert bestehende Klartext-Zeilen
    transparent beim ersten Lesen nach diesem Fix).
    """
    row = session.query(SaxoToken).order_by(SaxoToken.id).first()
    if row is None:
        return None
    _decrypt_or_migrate_saxo_token(session, row)
    return row


def upsert_saxo_token(session: Session, access_token: str, refresh_token: str,
                       access_token_expires_at: datetime, refresh_token_expires_at: datetime):
    """
    Schreibt/aktualisiert die einzige saxo_tokens-Zeile (ein Saxo-Account,
    daher immer UPDATE statt einer neuen Zeile pro Refresh). access_token/
    refresh_token werden hier verschlüsselt (siehe _encrypt_saxo_token) –
    Aufrufer übergeben immer Klartext.
    """
    encrypted_access = _encrypt_saxo_token(access_token)
    encrypted_refresh = _encrypt_saxo_token(refresh_token)
    row = session.query(SaxoToken).order_by(SaxoToken.id).first()
    if row:
        row.access_token = encrypted_access
        row.refresh_token = encrypted_refresh
        row.access_token_expires_at = access_token_expires_at
        row.refresh_token_expires_at = refresh_token_expires_at
        row.updated_at = datetime.utcnow()
    else:
        session.add(SaxoToken(
            access_token=encrypted_access,
            refresh_token=encrypted_refresh,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
        ))


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


def get_learning_proposals(session: Session) -> list[dict]:
    """Liest alle KI-Lernvorschläge (Intelligenter Lernzyklus, siehe backlook.py) aus bot_state."""
    raw = BotState.get(session, "learning_proposals")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def set_learning_proposals(session: Session, proposals: list[dict]):
    """Schreibt die vollständige Liste der Lernvorschläge in bot_state."""
    BotState.set(session, "learning_proposals", json.dumps(proposals, ensure_ascii=False))


def save_learning_proposal(session: Session, typ: str, data: dict):
    """
    Hängt einen neuen Lernvorschlag an die pending-Liste in bot_state an.
    Ursprünglich in backlook.py definiert, hierher verschoben (liegt näher an
    get_learning_proposals/set_learning_proposals) damit sowohl backlook.py
    als auch post_exit_tracking.py sie ohne Circular-Import nutzen können.
    """
    proposals = get_learning_proposals(session)
    proposals.append({
        "typ": typ,
        "erstellt": datetime.utcnow().isoformat(),
        "data": data,
        "status": "pending",
    })
    set_learning_proposals(session, proposals)


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


def save_position_snapshot(session: Session, snapshot_date, portfolio_value: float, positions: list[dict]):
    """
    Schreibt den Tages-Positions-Snapshot (siehe DailyPositionSnapshot).
    Idempotent: löscht zuerst evtl. bereits vorhandene Zeilen für
    snapshot_date (z.B. bei einem manuell wiederholten Testlauf am selben
    Tag), bevor neu geschrieben wird. positions: Liste von Dicts mit
    ticker/trade_id/quantity/entry_price/price/unrealized_pnl/
    unrealized_pnl_pct (siehe main.capture_daily_position_snapshot).
    """
    session.query(DailyPositionSnapshot).filter_by(snapshot_date=snapshot_date).delete()
    now = datetime.utcnow()
    session.add(DailyPositionSnapshot(
        snapshot_date=snapshot_date, snapshot_time=now,
        ticker=None, trade_id=None, portfolio_value=portfolio_value,
    ))
    for p in positions:
        session.add(DailyPositionSnapshot(
            snapshot_date=snapshot_date, snapshot_time=now, portfolio_value=portfolio_value,
            ticker=p["ticker"], trade_id=p["trade_id"], quantity=p["quantity"],
            entry_price=p["entry_price"], price=p["price"],
            unrealized_pnl=p["unrealized_pnl"], unrealized_pnl_pct=p["unrealized_pnl_pct"],
        ))


def get_previous_position_snapshot(session: Session, before_date) -> dict | None:
    """
    Liest den letzten gespeicherten Positions-Snapshot VOR before_date
    (typischerweise "heute", ET-Datum) – dient als Vorabend-Vergleichsbasis
    für die Tages-Mail (siehe main.send_daily_summary_email). Gibt None
    zurück, wenn noch nie ein Snapshot gespeichert wurde (allererster Lauf
    seit Einführung des Features – Mail zeigt dann einen Hinweis statt
    eines Vergleichs).
    """
    last_date = session.query(func.max(DailyPositionSnapshot.snapshot_date)).filter(
        DailyPositionSnapshot.snapshot_date < before_date
    ).scalar()
    if last_date is None:
        return None

    rows = session.query(DailyPositionSnapshot).filter_by(snapshot_date=last_date).all()
    total_row = next((r for r in rows if r.ticker is None), None)
    return {
        "snapshot_date": last_date,
        "portfolio_value": total_row.portfolio_value if total_row else None,
        "positions_by_trade_id": {r.trade_id: r for r in rows if r.ticker is not None},
    }


def get_alpaca_api_for_user(user_id: int):
    """
    Baut einen Alpaca-Client mit den pro Nutzer hinterlegten Keys (siehe
    portfolio_os/database.py PosUser.alpaca_*_encrypted, verschlüsselt via
    Fernet mit demselben ENCRYPTION_KEY). trading_bot hat kein eigenes
    SQLAlchemy-Modell für pos_users (die Tabelle "gehört" portfolio_os) –
    daher ein schlankes Raw-SQL-SELECT statt eines Duplikat-Modells.

    Gibt None zurück, wenn kein Nutzer/keine Keys gefunden wurden – der
    Aufrufer (siehe broker._get_alpaca_client) fällt dann auf die globalen
    .env-Keys zurück (Beta-Phase: Daniel als einziger aktiver Trader,
    Multi-User-Trading kommt erst in Phase 2).
    """
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT alpaca_api_key_encrypted, alpaca_secret_key_encrypted, alpaca_mode "
            "FROM pos_users WHERE id = :uid"
        ), {"uid": user_id}).fetchone()

    if not row or not row[0] or not row[1]:
        return None

    api_key = _decrypt_field(row[0])
    secret_key = _decrypt_field(row[1])
    mode = row[2] or "paper"
    base_url = "https://api.alpaca.markets" if mode == "live" else "https://paper-api.alpaca.markets"

    try:
        import alpaca_trade_api as tradeapi
        return tradeapi.REST(api_key, secret_key, base_url)
    except Exception as e:
        print(f"⚠️  Alpaca-Client für Nutzer {user_id} nicht verfügbar: {e}")
        return None


if __name__ == "__main__":
    init_db()
