# Trading Bot – Vollständige Projektspezifikation

**Version:** 1.0  
**Ziel:** Automatisierter Swing-Trading-Bot für Aktien (DAX / S&P 500)  
**Startkapital:** 500 € echtes Spielgeld  
**Betrieb:** Cloud-hosted, 24/7 online  

---

## 1. Projektziel & Philosophie

Der Bot handelt regelbasiert und diszipliniert – ohne Emotionen. Er ersetzt nicht menschliche Intelligenz, sondern menschliche Schwächen (Gier, Panik, Rache-Trades). Die KI-Komponente (LLM) dient ausschließlich als **erklärender Kommentator**, nicht als Entscheider. Entscheidungen trifft die **hardcodierte Rule Engine** auf Basis messbarer Kennzahlen.

---

## 2. Asset-Klasse & Handelsmodus

| Parameter | Wert |
|---|---|
| Handelbare Assets | US-Aktien (S&P 500 Large Caps) + Inverse ETFs |
| Broker | Alpaca Markets (Paper + Live, gleiche API) |
| Primärer Modus | Swing Trading (Haltedauer: 2–10 Tage) |
| Handelszeiten | NYSE: 15:30–22:00 Uhr DE-Zeit (Bot scannt morgens, ordert nachmittags) |
| Ausgeschlossen | Day Trading, Short Selling, Optionen, Penny Stocks |

**Begründung Swing Trading:** Bei 500 € Startkapital und LLM-basierter Architektur (Latenz 2–5 Sek.) ist Day Trading nicht konkurrenzfähig. Swing Trading gibt dem Bot Zeit für sorgfältige Analyse und minimiert Transaktionskosten.

### Watchlists

```python
# Bullische Kandidaten (LONG wenn Signal positiv)
LONG_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META",
    "NVDA", "JPM", "JNJ", "V", "UNH"
]

# Bärische Instrumente (LONG auf Inverse ETF wenn Signal negativ)
# Kein Margin-Konto nötig – Bot kauft diese wie normale Aktien
SHORT_WATCHLIST = [
    "SH",   # ProShares Short S&P500 (1x) – konservativ
    "PSQ",  # ProShares Short QQQ (1x) – Tech-Überbewertung
    "SDS",  # ProShares UltraShort S&P500 (2x) – Phase 2
]

# Phase 1: Nur SH und PSQ aktiv (kein Hebel)
# Phase 2: SDS nach erfolgreicher Paper-Trading-Validierung
ACTIVE_SHORT_INSTRUMENTS = ["SH", "PSQ"]
```

**Warum Inverse ETFs statt Short Selling:**
- Kein Margin-Konto erforderlich (kein 2.000 $ Minimum)
- Verlustrisiko gedeckelt (maximal der Einsatz, wie bei jeder Aktie)
- Identische Order-Logik im Code – kein separater SHORT-Codepfad
- Handelbar über Alpaca wie jede normale Aktie

---

## 3. Kapital & Risikomanagement (Die "Emotionsbremse")

Dies sind **harte Regeln im Code** – keine Empfehlungen, keine KI-Abwägungen. Sie werden NIEMALS durch LLM-Output überschrieben.

### 3.1 Harte Limits (hardcoded guardrails)

```python
MAX_CAPITAL_TOTAL        = 500.00   # Gesamtkapital in €
MAX_CAPITAL_PER_TRADE    = 50.00    # Max. Einsatz pro Trade (10% des Kapitals)
MAX_OPEN_POSITIONS       = 5        # Max. gleichzeitig offene Positionen
MAX_TRADES_PER_DAY       = 3        # Max. neue Trades pro Handelstag
STOP_LOSS_PCT            = 0.03     # Automatischer Ausstieg bei -3%
TAKE_PROFIT_PCT          = 0.06     # Gewinnmitnahme bei +6% (CRV = 2:1)
DAILY_LOSS_LIMIT_PCT     = 0.05     # Bot pausiert bei -5% Tagesverlust auf Gesamtkapital
MIN_SIGNAL_CONFIDENCE    = 65       # Minimale Rule-Score in % für Freigabe
```

### 3.2 Chancen-Risiko-Verhältnis (CRV)

Jeder Trade wird nur ausgeführt, wenn:
- **Stop Loss** bei -3% liegt
- **Take Profit** bei min. +6% liegt
- → CRV = mindestens **2:1** (bei 50% Trefferquote mathematisch profitabel)

### 3.3 Gewinnmitnahme-Strategie

- Ziel: Sobald Gesamtportfolio 1.000 € erreicht → 500 € entnehmen (Startkapital zurück)
- Ab diesem Punkt: Restkapital ist "Haus-Geld", Totalverlust ist verschmerzbar
- Wird als **Alert** implementiert (keine automatische Entnahme – das muss der Nutzer manuell tun)

---

## 4. Signallogik (Rule Engine)

Die Rule Engine berechnet einen **Score von 0–100** für jede Aktie. Nur wenn Score ≥ 65 → Trade wird freigegeben.

### 4.1 Technische Signale (60% des Scores)

| Kriterium | Gewichtung | Signal LONG | Signal SHORT |
|---|---|---|---|
| RSI (14) | 20% | RSI 30–50 (aus Überverkauft) | RSI 50–70 (aus Überkauft) |
| SMA 50/200 Verhältnis | 20% | Kurs > SMA50 > SMA200 | Kurs < SMA50 < SMA200 |
| Volumen-Bestätigung | 20% | Volumen > 20-Tage-Ø | Volumen > 20-Tage-Ø |

### 4.2 Fundamentale Filter (40% des Scores)

| Kriterium | Gewichtung | Mindestanforderung LONG |
|---|---|---|
| KGV (Trailing P/E) | 15% | 5 < KGV < 35 (kein extremes Über-/Unterbewertung) |
| Debt-to-Equity | 15% | < 200% (kein überschuldetes Unternehmen) |
| Revenue-Wachstum (YoY) | 10% | > 0% (Unternehmen wächst) |

**Wichtig:** Fundamentaldaten filtern nur grob und langsam ändernde Kriterien. Sie verhindern Trades in strukturell kaputten Unternehmen.

### 4.3 Ausschluss-Kriterien (KO-Kriterien – Override alles)

Ein Trade wird **niemals** ausgeführt, wenn:
- Earnings-Release innerhalb der nächsten 3 Tage (zu hohe Gap-Gefahr)
- Aktie hat in den letzten 5 Tagen bereits > 15% bewegt (Momentum-Exhaustion)
- Marktbreite-Filter: VIX > 30 (extremer Fear-Modus → Bot pausiert komplett)

---

## 5. LLM-Integration (Claude als Kommentator)

Das LLM **entscheidet nicht**. Es erklärt und dokumentiert.

### 5.1 Wann wird die LLM-API aufgerufen?

Nach jeder positiven Rule-Engine-Freigabe (Score ≥ 65) wird ein Analyse-Request gesendet.

### 5.2 Prompt-Template (System Prompt)

```
Du bist ein kritischer, wissenschaftlich fundierter Trading-Analyst.
Dir werden strukturierte Kennzahlen einer Aktie übergeben.

Deine Aufgabe:
1. Erkläre in 3–4 Sätzen, WARUM die Rule Engine diesen Trade freigegeben hat.
2. Nenne 2 konkrete Risiken, die der Algorithmus NICHT sieht (qualitative Risiken).
3. Gib einen Sentiment-Score von 1–10 (10 = sehr bullish) aus.

WICHTIG: Du gibst KEINE Handelsempfehlung. Die Entscheidung liegt beim Algorithmus.
Antworte ausschließlich im folgenden JSON-Format:
{
  "summary": "...",
  "risks": ["...", "..."],
  "sentiment_score": 7
}
```

### 5.3 Verwendung des LLM-Outputs

- `sentiment_score` kann optional den Rule-Engine-Score um ±5 Punkte adjustieren
- `summary` und `risks` werden im Trade-Log gespeichert und im Dashboard angezeigt
- **Der sentiment_score KANN NICHT einen Trade unter die 65-Punkte-Grenze retten, wenn fundamentale KO-Kriterien ausgelöst wurden**

---

## 6. Broker-Anbindung

### Phase 1: Paper Trading (Wochen 1–4)
- Kein echtes Geld
- Trades werden in lokaler SQLite-Datenbank simuliert
- Marktpreise werden real via yfinance abgerufen
- Ziel: Strategie validieren, Bugs finden, emotionale Reaktion des Nutzers beobachten

### Phase 2: Live Trading mit echtem Geld
- Empfohlener Broker: **Alpaca Markets** (US-Aktien, kostenlose API, Paper + Live im gleichen Interface)
- Alternative für DE/EU-Aktien: **Interactive Brokers** (IBKR) via `ib_insync` Python-Library
- Authentifizierung: API-Key + Secret in `.env`-Datei (niemals im Code hardcoded)

### Broker: Alpaca Markets (einziger Broker für alle Phasen)

| Kriterium | Wert |
|---|---|
| Mindesteinlage | 0 $ |
| Handelbare Assets | US-Aktien + ETFs (inkl. alle Inverse ETFs) |
| Gebühren | 0 $ / Trade |
| Paper Trading | Identische API – nur Endpoint-URL wechseln |
| Margin-Konto | Nicht erforderlich (kein Short Selling) |
| API-Komplexität | Gering – fertige Python-Library `alpaca-trade-api` |

---

## 7. Technische Architektur

```
┌─────────────────────────────────────────────────────┐
│                   CLOUD SERVER                       │
│                (Railway / Render.com)                │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │  Scheduler   │───▶│     Rule Engine          │   │
│  │  (täglich    │    │  - Technische Signale    │   │
│  │   09:00 Uhr) │    │  - Fundamentaldaten      │   │
│  └──────────────┘    │  - KO-Kriterien          │   │
│                      └────────────┬─────────────┘   │
│                                   │ Score ≥ 65?      │
│                      ┌────────────▼─────────────┐   │
│                      │  Guardrails Check        │   │
│                      │  - Max Trades/Tag?       │   │
│                      │  - Max Kapital?          │   │
│                      │  - VIX > 30?             │   │
│                      └────────────┬─────────────┘   │
│                                   │ Alles OK?        │
│                      ┌────────────▼─────────────┐   │
│                      │  LLM-Analyse (Claude API)│   │
│                      │  → summary, risks,       │   │
│                      │    sentiment_score       │   │
│                      └────────────┬─────────────┘   │
│                                   │                  │
│                      ┌────────────▼─────────────┐   │
│                      │  Order Execution          │   │
│                      │  (Paper Trade / Alpaca)  │   │
│                      └────────────┬─────────────┘   │
│                                   │                  │
│                      ┌────────────▼─────────────┐   │
│                      │  SQLite Trade Log         │   │
│                      │  + Streamlit Dashboard   │   │
│                      └──────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

---

## 8. Tech Stack

| Komponente | Technologie | Begründung |
|---|---|---|
| Sprache | Python 3.11+ | Standard im Finanz/Data-Bereich |
| Scheduling | APScheduler | Leichtgewichtig, keine externe Queue nötig |
| Marktdaten (Live) | yfinance | Kostenlos, einfach, ausreichend für Swing Trading |
| Technische Indikatoren | pandas-ta | Fertige RSI/SMA/Volumen-Berechnung |
| Fundamentaldaten | yfinance (info dict) | KGV, D/E, Revenue via Yahoo Finance |
| LLM | Anthropic Claude API (claude-sonnet-4-6) | Günstig, schnell, JSON-Mode zuverlässig |
| Datenbank | SQLite (via SQLAlchemy) | Serverlos, kein Setup, ausreichend |
| Dashboard | Streamlit | Schnell, in Python, einfach zu deployen |
| Broker Phase 1 | Paper Trading (intern) | Kein Risiko, volle Kontrolle |
| Broker Phase 2 | Alpaca API | Kostenlos, einfache Python-Library |
| Hosting | Railway.app | Günstig (~5 €/Monat), GitHub-Integration, immer online |
| VIX-Daten | yfinance (Ticker: ^VIX) | Kostenlos verfügbar |

---

## 9. Datenbankschema (SQLite)

### Tabelle: `trades`
```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    ticker          TEXT NOT NULL,
    direction       TEXT NOT NULL,        -- 'LONG' oder 'SHORT'
    entry_price     REAL NOT NULL,
    stop_loss       REAL NOT NULL,
    take_profit     REAL NOT NULL,
    quantity        REAL NOT NULL,
    capital_used    REAL NOT NULL,
    rule_score      INTEGER NOT NULL,     -- 0–100
    llm_sentiment   INTEGER,              -- 1–10
    llm_summary     TEXT,
    llm_risks       TEXT,                 -- JSON array als String
    status          TEXT DEFAULT 'OPEN',  -- 'OPEN', 'CLOSED_SL', 'CLOSED_TP', 'CLOSED_MANUAL'
    exit_price      REAL,
    pnl_eur         REAL,
    mode            TEXT DEFAULT 'PAPER'  -- 'PAPER' oder 'LIVE'
);
```

### Tabelle: `bot_state`
```sql
CREATE TABLE bot_state (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
-- Speichert: daily_trade_count, last_run, total_pnl, etc.
```

---

## 10. Dashboard (Streamlit)

### Seiten / Tabs

1. **Overview:** Gesamtkapital, offene Positionen, heutige Trades, Gesamt-P&L
2. **Trade Log:** Tabelle aller Trades mit LLM-Summary und Status
3. **Rule Engine Live:** Eingabe beliebiger Ticker → sofortige Score-Berechnung + LLM-Analyse
4. **Bot-Einstellungen:** Alle Guardrail-Parameter anpassbar (mit Warnung bei Änderung)
5. **Performance:** Chart – Kapitalentwicklung über Zeit

### Alerts

- **E-Mail oder Telegram-Nachricht** bei jedem ausgeführten Trade
- **Alert wenn Gesamtkapital ≥ 1.000 €** (Hinweis: "Jetzt 500 € entnehmen")
- **Alert wenn Daily Loss Limit erreicht** (Bot pausiert automatisch)

---

## 11. Deployment (Railway.app)

```bash
# Minimale Projektstruktur
trading_bot/
├── main.py              # Entry point, Scheduler
├── rule_engine.py       # Score-Berechnung
├── llm_analyst.py       # Claude API Integration
├── broker.py            # Paper / Alpaca Abstraction
├── database.py          # SQLite via SQLAlchemy
├── dashboard.py         # Streamlit App
├── config.py            # Alle Parameter (aus .env geladen)
├── requirements.txt
└── .env.example         # Template (echte .env NICHT ins Git!)
```

```
# .env.example
ANTHROPIC_API_KEY=sk-ant-...
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
TRADING_MODE=PAPER          # 'PAPER' oder 'LIVE'
ALERT_EMAIL=deine@email.de
```

---

## 12. Risiken & Disclaimer

| Risiko | Mitigierung |
|---|---|
| LLM halluziniert | LLM entscheidet nicht – nur Rule Engine entscheidet |
| yfinance-Daten fehlerhaft/verzögert | Validierung: Preis-Sanity-Check (±20% Tagesbereich = Fehler) |
| API-Ausfall (Anthropic) | Bot handelt weiter ohne LLM-Analyse (degraded mode), Trade wird als "no LLM" geloggt |
| Totalverlust 500 € | Bewusst akzeptiert. Stop-Loss und Daily-Limit begrenzen Drawdown strukturell |
| Steuerliche Pflichten | Gewinne aus Aktienhandel sind in DE steuerpflichtig (Abgeltungssteuer). Eigenverantwortung. |

---

## 13. Entwicklungsreihenfolge (für Claude Code)

**Schritt 1:** `database.py` + `config.py` → Datenbankschema + Konfiguration  
**Schritt 2:** `rule_engine.py` → Score-Berechnung mit yfinance + pandas-ta  
**Schritt 3:** `broker.py` → Paper-Trading-Logik  
**Schritt 4:** `llm_analyst.py` → Claude API Integration mit JSON-Output  
**Schritt 5:** `main.py` → Scheduler + Orchestrierung aller Module  
**Schritt 6:** `dashboard.py` → Streamlit Dashboard  
**Schritt 7:** Deployment auf Railway.app  
**Schritt 8:** 4 Wochen Paper Trading → Evaluation → Entscheidung Live  
