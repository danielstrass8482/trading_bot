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

# Geteilt mit portfolio_os (gleicher Wert) – noetig um pos_users.alpaca_*_encrypted
# zu entschluesseln (siehe database.get_alpaca_api_for_user, Feature 8 Multi-Tenant).
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

# ─────────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────────
ALERT_EMAIL   = os.getenv("ALERT_EMAIL", "")

# SMTP – E-Mail-Versand via smtplib (Standardbibliothek, kein externes Package).
# Wenn SMTP_HOST/SMTP_USER/SMTP_PASSWORD fehlen, wird nicht versendet,
# sondern nur in die Logs geschrieben (siehe send_email() in main.py).
SMTP_HOST         = os.getenv("SMTP_HOST", "")
# Railway blockiert Port 587 (STARTTLS) ausgehend. Port 465 (SMTPS/SSL) ist
# der Default; wird dieser Port ebenfalls blockiert (z.B. bei künftigem
# Wechsel auf einen eigenen Mailserver mit abweichender Policy), kann
# SMTP_FALLBACK_PORT auf einen von Railway nicht blockierten Port gesetzt
# werden (z.B. 2525, unverschlüsseltes SMTP).
SMTP_PORT         = int(os.getenv("SMTP_PORT", "465"))
SMTP_FALLBACK_PORT = int(os.getenv("SMTP_FALLBACK_PORT", "2525"))
SMTP_USER         = os.getenv("SMTP_USER", "")
SMTP_PASSWORD     = os.getenv("SMTP_PASSWORD", "")
SMTP_TIMEOUT      = 10

# ─────────────────────────────────────────────
# WATCHLISTS
# ─────────────────────────────────────────────

# Bullische Kandidaten – Bot kauft LONG wenn Score ≥ MIN_SIGNAL_SCORE
# Seit 2026-07-27: komplette S&P-500-Watchlist (vorher nur ~90 handverlesene
# Titel). Ggü. der vom User gelieferten Rohliste bereinigt:
# - Duplikate entfernt (CF/MOS/EVRG/SWK/SOFI standen je zweimal drin)
# - Delistete/umbenannte Ticker entfernt: ATVI (Activision, seit 2023 von MSFT
#   übernommen, nicht mehr handelbar), JDSU (seit 2015 als VIAV notiert),
#   ABC (AmerisourceBergen, seit 2023 als COR/Cencora notiert)
# - Weitere 6 Ticker nach HTTP-404 beim ersten Fair-Value-Cache-Lauf
#   (2026-07-27) als M&A-bedingt delistet identifiziert und entfernt:
#   ANSS (Synopsys), JNPR (HPE), PARA (Paramount-Skydance-Merger),
#   IPG (Omnicom), K/Kellanova (Mars), AMED (UnitedHealth/Optum)
# - RBLX/SNAP ergänzt (kein S&P-500-Mitglied, aber Teil von VOLATILE_WATCHLIST
#   unten – ohne sie hier würde der Bot sie nie scannen/kaufen und die
#   Portfolio-Segmentierung liefe für diese zwei Ticker leer, siehe 2026-07-25).
LONG_WATCHLIST = [
    # INFORMATION TECHNOLOGY
    "AAPL", "MSFT", "NVDA", "AVGO", "ORCL",
    "CRM", "CSCO", "ADBE", "AMD", "INTC",
    "QCOM", "TXN", "AMAT", "NOW", "INTU",
    "MU", "KLAC", "LRCX", "SNPS", "CDNS",
    "FTNT", "PANW", "CRWD", "MPWR",
    "VRSN", "ENPH", "FSLR", "GLW", "HPQ",
    "HPE", "NTAP", "STX", "WDC",
    "ZBRA", "AKAM", "CDW", "FFIV", "GDDY",
    "GEN", "GRMN", "IT", "KEYS",
    "LDOS", "NTRS", "PTC", "QRVO", "SWKS",
    "TER", "TRMB", "TYL", "VICI", "WU",

    # COMMUNICATION SERVICES
    "GOOGL", "META", "NFLX", "DIS", "CMCSA",
    "T", "VZ", "TMUS", "CHTR", "WBD",
    "FOX", "FOXA", "LYV",
    "MTCH", "NWS", "NWSA", "OMC", "TTWO",
    "EA",

    # CONSUMER DISCRETIONARY
    "AMZN", "TSLA", "HD", "MCD", "NKE",
    "SBUX", "TJX", "LOW", "BKNG", "CMG",
    "ORLY", "AZO", "ROST", "DHI", "LEN",
    "PHM", "NVR", "TOL", "KBH", "MDC",
    "EXPE", "MAR", "HLT", "MGM", "WYNN",
    "LVS", "RCL", "CCL", "NCLH", "HAS",
    "MAT", "RL", "PVH", "TPR", "VFC",
    "GPS", "ANF", "AEO", "URBN", "BBWI",
    "DRI", "YUM", "QSR", "JACK", "WEN",
    "F", "GM", "APTV", "BWA", "LEA",
    "MHK", "WHR", "POOL", "SWK", "LULU",
    "UBER", "LYFT", "ABNB", "DASH",

    # CONSUMER STAPLES
    "PG", "KO", "PEP", "WMT", "COST",
    "PM", "MO", "KHC", "GIS",
    "CAG", "CPB", "MKC", "SJM", "HRL",
    "CL", "CHD", "ENR", "SPB", "COTY",
    "EL", "ULTA", "TGT", "DG", "DLTR",
    "KR", "SYY", "USFD", "ADM", "BG",
    "MOS", "CF", "NTR",

    # HEALTH CARE (non-Pharma wo möglich)
    "UNH", "JNJ", "ABT", "MDT", "TMO",
    "DHR", "SYK", "BSX", "EW", "ISRG",
    "ZBH", "BDX", "BAX", "RMD", "DXCM",
    "HOLX", "IDXX", "IQV", "A", "MTD",
    "WAT", "PODD", "ALGN", "COO", "HSIC",
    "VTRS", "HCA", "CNC", "MOH", "HUM",
    "CVS", "CI", "ELV", "MCK", "CAH",
    "DGX", "LH", "ACAD",

    # FINANCIALS
    "BRK-B", "JPM", "BAC", "WFC", "GS",
    "MS", "C", "AXP", "BLK", "SCHW",
    "CB", "PGR", "MET", "PRU", "AIG",
    "AFL", "ALL", "TRV", "HIG", "L",
    "USB", "PNC", "TFC", "COF", "DFS",
    "SYF", "AMP", "IVZ", "BEN", "TROW",
    "NDAQ", "ICE", "CME", "CBOE", "MSCI",
    "MCO", "SPGI", "FDS", "BR", "FIS",
    "FISV", "GPN", "MA", "V", "PYPL",
    "SQ", "AFRM", "SOFI",

    # INDUSTRIALS
    "GE", "HON", "CAT", "DE", "RTX",
    "LMT", "NOC", "GD", "BA", "HII",
    "TDG", "CARR", "OTIS", "EMR", "ETN",
    "PH", "ROK", "AME", "ITW", "XYL",
    "VRSK", "IR", "JCI", "TT", "FTV",
    "GNRC", "HUBB", "NVT", "PAYC", "ROP",
    "UPS", "FDX", "JBHT", "CHRW", "EXPD",
    "GWW", "MSC", "FAST", "SNA",
    "AOS", "MAS", "ALLE", "LECO", "ATI",
    "HWM", "TXT", "WWD", "WAB", "CSX",
    "NSC", "UNP", "CP", "CNI",

    # ENERGIE (erneuerbar bevorzugt)
    "NEE", "DUK", "SO", "AEP", "EXC",
    "PCG", "ED", "XEL", "ES", "FE",
    "ETR", "PPL", "CMS", "NI", "AES",
    "EVRG", "PNW", "OGE", "NRG", "VST",
    # Fossil-Blacklist greift automatisch für XOM, CVX etc.

    # MATERIALS
    "LIN", "APD", "ECL", "SHW", "PPG",
    "NEM", "FCX", "NUE", "STLD", "RS",
    "PKG", "IP", "ALB",
    "CE", "EMN", "FMC", "IFF", "RPM",

    # REAL ESTATE
    "AMT", "PLD", "CCI", "EQIX", "PSA",
    "O", "SPG", "VNQ", "WELL", "DLR",
    "EXR", "AVB", "EQR", "MAA", "UDR",
    "CPT", "ESS", "AIV", "NNN", "REG",
    "FRT", "KIM", "BXP", "VTR", "PEAK",

    # UTILITIES
    "AWK", "WEC", "DTE", "CNP", "LNT",
    "WTRG", "SRE", "AEE", "NFG",

    # VOLATILE / WACHSTUM
    "PLTR", "HOOD", "RIVN", "LCID",
    "ARKK", "SOXL", "MSTR", "COIN",
    "RBLX", "SNAP",
]

# Blacklist greift automatisch:
# XOM, CVX, BP (Fossil)
# MO, PM, BTI (Tobacco)
# LMT, NOC, GD, RTX (Weapons) - bleiben aber drin
#   weil manche Kunden keine Rüstungs-Blacklist wollen
#   → individuell konfigurierbar

# Bärische Instrumente – Bot kauft LONG auf Inverse ETF wenn Markt bärisch
# Kein Short Selling, kein Margin-Konto nötig. SDS/SQQQ/SPXS sind gehebelte
# Inverse-ETFs (2x/3x) – entsprechend höhere Volatilität pro eingesetztem
# Dollar als SH/PSQ (1x), bewusst mit dem User abgestimmt (2026-07-24).
ACTIVE_SHORT_INSTRUMENTS = ["SH", "PSQ", "SDS", "SQQQ", "SPXS"]

# Teilmenge von LONG_WATCHLIST, die als besonders volatil gilt (Portfolio-
# Segmentierung, siehe main.run_entry_cycle) – günstige/gehebelte/spekulative
# Titel, die der Bot bewusst nur bis zu einem Zielanteil (VOLATILE_SEGMENT_PCT)
# am offenen Portfolio zulässt.
VOLATILE_WATCHLIST = [
    "PLTR", "SOFI", "RIVN",  # Günstig + volatil
    "SOXL",                   # 3x Halbleiter ETF
    "ARKK",                   # Innovations ETF
    "MSTR",                   # Bitcoin-Proxy
    "COIN",                   # Coinbase
    "RBLX", "SNAP", "UBER",  # Wachstum + volatil
    "HOOD",                   # Robinhood
]

# Europäische Titel für IBKR (siehe broker_ibkr.py) – nur relevant wenn
# ACTIVE_BROKER=ibkr, da Alpaca keine EU-Börsen unterstützt. Ticker-Suffix
# (.DE/.L/.PA) steuert in broker_ibkr._get_contract() die Exchange-Wahl
# (XETRA/LSE/EURONEXT). Die Branchen-Blacklist (siehe rule_engine.
# BLACKLIST_MAPPING) greift unverändert auch hier – BP.L, TTE.PA (fossil),
# GSK.L, SAN.PA (Pharma) werden also automatisch ausgeschlossen, bleiben
# aber bewusst in der Liste, damit der Filter sie auch tatsächlich zu sehen
# bekommt (analog zu VOLATILE_WATCHLIST in LONG_WATCHLIST).
IBKR_EU_WATCHLIST = [
    # Deutschland (XETRA)
    "SAP.DE",    # SAP
    "SIE.DE",    # Siemens
    "ALV.DE",    # Allianz
    "DTE.DE",    # Deutsche Telekom
    "BMW.DE",    # BMW
    "MBG.DE",    # Mercedes-Benz
    "BAS.DE",    # BASF
    "BAYN.DE",   # Bayer
    "VOW3.DE",   # Volkswagen VZ
    "MUV2.DE",   # Munich Re
    "ADS.DE",    # Adidas
    "DBK.DE",    # Deutsche Bank
    "RWE.DE",    # RWE
    "HEN3.DE",   # Henkel VZ
    "EOAN.DE",   # E.ON

    # UK (LSE)
    "SHEL.L",    # Shell (Fossil-Blacklist)
    "AZN.L",     # AstraZeneca
    "HSBA.L",    # HSBC
    "BP.L",      # BP (Fossil-Blacklist)
    "RIO.L",     # Rio Tinto
    "ULVR.L",    # Unilever
    "GSK.L",     # GSK (Pharma-Blacklist)

    # Frankreich (Euronext)
    "MC.PA",     # LVMH
    "OR.PA",     # L'Oréal
    "TTE.PA",    # TotalEnergies (Fossil-Blacklist)
    "SAN.PA",    # Sanofi (Pharma-Blacklist)
    "AIR.PA",    # Airbus
    "BNP.PA",    # BNP Paribas
    "KER.PA",    # Kering
]

# ─────────────────────────────────────────────
# HARTKODIERTE GUARDRAILS (Emotionsbremse)
# Diese Werte sind bewusst NICHT in .env ausgelagert.
# Änderungen erfordern Code-Änderung + Deployment.
# ─────────────────────────────────────────────

MAX_CAPITAL_TOTAL     = 475.00   # Gesamtkapital in USD (Alpaca arbeitet in USD)
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


# ─────────────────────────────────────────────
# LIVE-KONFIGURATION (aus DB, mit hardcoded Fallback)
# ─────────────────────────────────────────────
# Diese Parameter dürfen über das Dashboard (portfolio_os) angepasst werden und
# liegen in der gemeinsamen Postgres-Tabelle bot_config. Der Bot liest sie zu
# Beginn jedes Zyklus. Die obigen hartkodierten Konstanten bleiben Fail-safe:
# Ist die DB nicht erreichbar oder ein Wert ungültig, gilt weiterhin der sichere
# Default – ein DB-Ausfall kann also niemals die Guardrails aushebeln.

# Erwartete Typen der (als Text gespeicherten) DB-Werte + Fallback-Konstante.
_LIVE_CONFIG_SPEC = {
    "MAX_CAPITAL_TOTAL":       (float, MAX_CAPITAL_TOTAL),
    "MAX_CAPITAL_PER_TRADE":   (float, MAX_CAPITAL_PER_TRADE),
    "MAX_OPEN_POSITIONS":      (int,   MAX_OPEN_POSITIONS),
    "MAX_TRADES_PER_DAY":      (int,   MAX_TRADES_PER_DAY),
    "STOP_LOSS_PCT":           (float, STOP_LOSS_PCT),
    "TAKE_PROFIT_PCT":         (float, TAKE_PROFIT_PCT),
    "DAILY_LOSS_LIMIT_PCT":    (float, DAILY_LOSS_LIMIT_PCT),
    "MIN_SIGNAL_SCORE":        (int,   MIN_SIGNAL_SCORE),
    "VIX_PAUSE_THRESHOLD":     (float, VIX_PAUSE_THRESHOLD),
    "MONITORING_INTERVAL_MIN": (int,   15),
    "ATR_MULTIPLIER_SL":       (float, 1.5),
    "ATR_MULTIPLIER_TP":       (float, 3.0),
    "ATR_MIN_SL_PCT":          (float, 0.01),
    "ATR_MAX_SL_PCT":          (float, 0.08),
    "MAX_HOLDING_DAYS":        (int,   5),
    "VOLATILE_SEGMENT_PCT":    (float, 0.33),
    "VOLATILE_ATR_THRESHOLD":  (float, 0.025),
    "EARNINGS_BUFFER_DAYS":    (int,   EARNINGS_BUFFER_DAYS),
    "ACTIVE_BROKER":           (str,   "alpaca"),  # "alpaca" oder "ibkr" (siehe broker.get_broker)
    "ALPACA_DRAIN_MODE":       (str,   "false"),   # "true" = keine neuen Alpaca-Käufe, nur bestehende Positionen managen
}


def get_live_config() -> dict:
    """
    Lädt alle konfigurierbaren Bot-Parameter aus der DB (Tabelle bot_config)
    mit hardcoded Fallback. Rückgabe: dict key -> typisierter Wert.

    Import von database erfolgt bewusst lazy (innerhalb der Funktion), da
    database.py seinerseits config.py importiert (Zirkelimport-Vermeidung).
    """
    cfg = {key: fallback for key, (_cast, fallback) in _LIVE_CONFIG_SPEC.items()}
    try:
        from database import get_session, get_bot_config
        with get_session() as session:
            for key, (cast, _fallback) in _LIVE_CONFIG_SPEC.items():
                raw = get_bot_config(session, key)
                if raw is not None:
                    try:
                        cfg[key] = cast(raw)
                    except (ValueError, TypeError):
                        pass  # ungültiger DB-Wert → Fallback behalten
    except Exception:
        pass  # DB nicht erreichbar → komplett Fallback (Fail-safe)
    return cfg


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
