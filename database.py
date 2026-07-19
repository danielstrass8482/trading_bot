"""
database.py – Datenbankmodelle und Datenbankzugriff
Verwendet SQLAlchemy mit SQLite (serverlos, kein Setup nötig).
"""

from datetime import datetime, date
from sqlalchemy import (
    create_engine, Column, Integer, Float, String,
    DateTime, Date, Text, func
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from contextlib import contextmanager
import json

from config import DATABASE_URL

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
    status         = Column(String(20), default="OPEN")   # OPEN / CLOSED_SL / CLOSED_TP / CLOSED_MANUAL
    exit_price     = Column(Float, nullable=True)
    closed_at      = Column(DateTime, nullable=True)
    pnl_usd        = Column(Float, nullable=True)
    pnl_pct        = Column(Float, nullable=True)
    mode           = Column(String(10), default="PAPER")   # PAPER / LIVE

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


class DailyLog(Base):
    """Tägliche Zusammenfassung für Performance-Chart."""
    __tablename__ = "daily_log"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    log_date       = Column(Date, default=date.today, unique=True)
    portfolio_value = Column(Float, nullable=False)
    daily_pnl      = Column(Float, default=0.0)
    trades_count   = Column(Integer, default=0)
    open_positions = Column(Integer, default=0)


# ─────────────────────────────────────────────
# DATENBANKZUGRIFF
# ─────────────────────────────────────────────

def init_db():
    """Erstellt alle Tabellen (idempotent – safe to call multiple times)."""
    Base.metadata.create_all(engine)
    # Initiale Bot-State-Werte setzen falls nicht vorhanden
    with get_session() as session:
        if not BotState.get(session, "daily_trade_count"):
            BotState.set(session, "daily_trade_count", "0")
        if not BotState.get(session, "last_reset_date"):
            BotState.set(session, "last_reset_date", str(date.today()))
        if not BotState.get(session, "bot_paused"):
            BotState.set(session, "bot_paused", "false")
        session.commit()
    print("✅ Datenbank initialisiert.")


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


def get_total_pnl(session: Session) -> float:
    """Gesamter realisierter P&L aller abgeschlossenen Trades."""
    result = session.query(func.sum(Trade.pnl_usd)).filter(
        Trade.status.in_(["CLOSED_SL", "CLOSED_TP", "CLOSED_MANUAL"])
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
