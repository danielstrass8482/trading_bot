"""
config.py – Zentrale Konfiguration des Trading Bots
Alle Parameter werden aus .env geladen. Guardrails sind hardcoded
und können NICHT durch LLM-Output überschrieben werden.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────────────
# BROKER (Alpaca)
# ─────────────────────────────────────────────
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# "PAPER" oder "LIVE" – normalisiert, damit z.B. "paper" oder ein Tippfehler
# nicht versehentlich als LIVE durchgeht.
TRADING_MODE      = os.getenv("TRADING_MODE", "PAPER").strip().upper()

# Fail-safe: NUR ein exaktes "LIVE" schaltet auf den Live-Endpoint um.
# Jeder andere Wert (Tippfehler, leerer String, etc.) bleibt bewusst PAPER –
# ein Fehler soll nie versehentlich zu echten Live-Orders führen.
ALPACA_BASE_URL = (
    "https://api.alpaca.markets"
    if TRADING_MODE == "LIVE"
    else "https://paper-api.alpaca.markets"
)

# ─────────────────────────────────────────────
# LLM (Anthropic Claude)
# ─────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
LLM_MODEL         = "claude-sonnet-4-6"
LLM_MAX_TOKENS    = 512

# ─────────────────────────────────────────────
# DATENBANK
# ─────────────────────────────────────────────
# Railway (und Heroku) liefern DATABASE_URL im Format "postgres://...".
# SQLAlchemy 1.4+/2.0 akzeptiert nur "postgresql://" – daher hier korrigieren.
# Ohne gesetzte DATABASE_URL läuft lokal weiterhin SQLite als Fallback.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///trading_bot.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────
ALERT_EMAIL = os.getenv("ALERT_EMAIL", "")

# ─────────────────────────────────────────────
# WATCHLISTS
# ─────────────────────────────────────────────

# Bullische Kandidaten – Bot kauft LONG wenn Score ≥ MIN_SIGNAL_SCORE
LONG_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "NVDA", "JPM", "JNJ", "V", "UNH",
    "HD", "PG", "MA", "XOM", "BAC",
]

# Bärische Instrumente – Bot kauft LONG auf Inverse ETF wenn Markt bärisch
# Kein Short Selling, kein Margin-Konto nötig
ACTIVE_SHORT_INSTRUMENTS = ["SH", "PSQ"]   # Phase 1: 1x ohne Hebel
# Phase 2 (nach Validierung): ["SH", "PSQ", "SDS"]

# ─────────────────────────────────────────────
# HARTKODIERTE GUARDRAILS (Emotionsbremse)
# Diese Werte sind bewusst NICHT in .env ausgelagert.
# Änderungen erfordern Code-Änderung + Deployment.
# ─────────────────────────────────────────────

MAX_CAPITAL_TOTAL     = 500.00   # Gesamtkapital in USD (Alpaca arbeitet in USD)
MAX_CAPITAL_PER_TRADE = 50.00    # Max. Einsatz pro Trade (10% des Kapitals)
MAX_OPEN_POSITIONS    = 5        # Max. gleichzeitig offene Positionen
MAX_TRADES_PER_DAY    = 3        # Max. neue Trades pro Handelstag
STOP_LOSS_PCT         = 0.03     # Automatischer Ausstieg bei -3%
TAKE_PROFIT_PCT       = 0.06     # Gewinnmitnahme bei +6% (CRV = 2:1)
DAILY_LOSS_LIMIT_PCT  = 0.05     # Bot pausiert bei -5% Tagesverlust auf Gesamtkapital
MIN_SIGNAL_SCORE      = 65       # Minimaler Rule-Engine-Score (0–100) für Trade-Freigabe

# Markt-Kontext-Filter (KO-Kriterien)
VIX_PAUSE_THRESHOLD   = 30       # Bot pausiert komplett wenn VIX > 30
EARNINGS_BUFFER_DAYS  = 3        # Kein Trade wenn Earnings innerhalb N Tage
MAX_5DAY_MOVE_PCT     = 0.15     # Ausschluss wenn Aktie >15% in 5 Tagen bewegt

# Profit-Alert (manuelle Entnahme durch Nutzer)
PROFIT_ALERT_TARGET   = 1000.00  # Alert wenn Portfolio diesen Wert erreicht

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
# Bot scannt morgens (09:00 ET = 15:00 DE) und platziert Orders zur NYSE-Öffnung
SCAN_HOUR_ET   = 9    # 09:00 Eastern Time
SCAN_MINUTE_ET = 0

# ─────────────────────────────────────────────
# TECHNISCHE INDIKATOREN (Gewichtungen für Score)
# ─────────────────────────────────────────────
# Gewichtung muss in Summe 100 ergeben
SCORE_WEIGHTS = {
    "rsi":         20,   # RSI(14) – Momentum
    "sma_trend":   20,   # SMA50/200 Verhältnis
    "volume":      20,   # Volumen vs. 20-Tage-Durchschnitt
    "pe_ratio":    15,   # KGV – Bewertung
    "debt_equity": 15,   # Verschuldungsgrad
    "revenue_growth": 10, # Umsatzwachstum YoY
}

# Technische Schwellwerte
RSI_OVERSOLD    = 35   # RSI < 35 → bullisches Signal
RSI_OVERBOUGHT  = 65   # RSI > 65 → bärisches Signal (für Inverse ETF)
VOLUME_FACTOR   = 1.2  # Volumen muss min. 20% über 20-Tage-Ø liegen

# Fundamentale Schwellwerte
PE_MIN     = 5.0    # Unter 5 → verdächtig (Datenfehler oder strukturelles Problem)
PE_MAX     = 40.0   # Über 40 → zu teuer für Long
DE_MAX     = 200.0  # Debt-to-Equity über 200% → ausgeschlossen


def validate_config() -> list[str]:
    """Prüft ob kritische Konfiguration vorhanden ist. Gibt Liste mit Warnings zurück."""
    warnings = []
    if not ANTHROPIC_API_KEY:
        warnings.append("ANTHROPIC_API_KEY fehlt – LLM-Analyse deaktiviert (degraded mode)")
    if TRADING_MODE not in ("PAPER", "LIVE"):
        warnings.append(f"TRADING_MODE='{TRADING_MODE}' unbekannt – Bot läuft sicherheitshalber im PAPER-Modus")
    if TRADING_MODE == "LIVE" and (not ALPACA_API_KEY or not ALPACA_SECRET_KEY):
        warnings.append("ALPACA Credentials fehlen – Live Trading nicht möglich")
    if sum(SCORE_WEIGHTS.values()) != 100:
        warnings.append(f"SCORE_WEIGHTS summieren nicht auf 100 (aktuell: {sum(SCORE_WEIGHTS.values())})")
    return warnings
