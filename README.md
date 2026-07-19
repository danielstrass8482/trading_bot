# 🤖 Trading Bot

Automatisierter Swing-Trading-Bot für US-Aktien (S&P 500) mit KI-Kommentator.

**Stack:** Python · Alpaca Markets · Claude API · Streamlit · SQLite · Railway.app

---

## Projektstruktur

```
trading_bot/
├── config.py          # Alle Parameter & hardcodierte Guardrails
├── database.py        # SQLite-Modelle (Trades, Bot-Status, Daily Log)
├── rule_engine.py     # Signal-Score Berechnung (RSI, SMA, Volumen, Fundamentals)
├── llm_analyst.py     # Claude API Integration (Kommentator, kein Entscheider)
├── broker.py          # Alpaca Paper/Live Abstraktion + Guardrail-Enforcement
├── main.py            # Scheduler + Orchestrierung
├── dashboard.py       # Streamlit Dashboard
├── requirements.txt
├── railway.toml       # Railway Deployment Config
├── Procfile           # Prozess-Definitionen
└── .env.example       # Umgebungsvariablen Template
```

---

## Setup (lokal)

### 1. Repository klonen & Dependencies installieren

```bash
git clone <dein-repo>
cd trading_bot
pip install -r requirements.txt
```

### 2. Umgebungsvariablen konfigurieren

```bash
cp .env.example .env
# .env öffnen und ausfüllen:
```

```env
ANTHROPIC_API_KEY=sk-ant-...        # https://console.anthropic.com
ALPACA_API_KEY=...                   # https://alpaca.markets → Paper Trading
ALPACA_SECRET_KEY=...
TRADING_MODE=PAPER                   # PAPER zuerst, LIVE erst nach Validierung
ALERT_EMAIL=deine@email.de
```

### 3. Datenbank initialisieren & Bot starten

```bash
# Nur Datenbank initialisieren (Test)
python database.py

# Dashboard starten (separates Terminal)
streamlit run dashboard.py

# Bot starten (Scheduler)
python main.py
```

---

## Deployment auf Railway.app

### Schritt 1: GitHub Repository erstellen

```bash
git init
git add .
git commit -m "Initial commit: Trading Bot"
git remote add origin https://github.com/DEIN-USERNAME/trading-bot.git
git push -u origin main
```

**WICHTIG:** `.env` darf NICHT in Git! Prüfe `.gitignore`:
```
.env
*.db
__pycache__/
```

### Schritt 2: Railway Projekt erstellen

1. Gehe zu [railway.app](https://railway.app) → Einloggen mit GitHub
2. "New Project" → "Deploy from GitHub repo"
3. Dein `trading-bot` Repository auswählen

### Schritt 3: Umgebungsvariablen in Railway setzen

Railway Dashboard → Dein Projekt → "Variables" Tab:

| Variable | Wert |
|---|---|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `ALPACA_API_KEY` | dein Alpaca Key |
| `ALPACA_SECRET_KEY` | dein Alpaca Secret |
| `TRADING_MODE` | `PAPER` |
| `ALERT_EMAIL` | deine E-Mail |

### Schritt 4: Zwei Services einrichten

Railway unterstützt mehrere Prozesse aus einem Repo:

**Service 1 – Bot:**
- Start Command: `python main.py`

**Service 2 – Dashboard:**
- Start Command: `streamlit run dashboard.py --server.port $PORT --server.address 0.0.0.0 --server.headless true`
- Railway gibt automatisch eine öffentliche URL für das Dashboard

### Schritt 5: Deployment prüfen

```
Railway Logs → "✅ Datenbank initialisiert."
Railway Logs → "⏰ Scheduler aktiv..."
Dashboard URL → Portfolio-Übersicht sichtbar
```

---

## Alpaca Account einrichten

1. Gehe zu [alpaca.markets](https://alpaca.markets)
2. "Sign Up" → kostenloses Konto erstellen
3. "Paper Trading" aktivieren (kein echtes Geld nötig)
4. API Keys generieren: Dashboard → "API Keys" → "Generate New Key"
5. Key und Secret in `.env` / Railway Variables eintragen

**Phase 2 (Live Trading):**
- Alpaca Dashboard → "Go Live" → Identitätsverifizierung (ca. 1 Werktag)
- 500 $ einzahlen
- `.env` ändern: `TRADING_MODE=LIVE`
- Neu deployen

---

## Architektur & Philosophie

```
Scheduler (09:00 ET)
    │
    ├─ VIX Check → Über 30? → Bot pausiert
    │
    ├─ Watchlist scannen (15 Aktien + 2 Inverse ETFs)
    │      RSI · SMA50/200 · Volumen · KGV · D/E · Revenue
    │      → Score 0–100
    │
    ├─ Score ≥ 65? → Guardrails prüfen (hardcoded, kein LLM-Override)
    │      Max 3 Trades/Tag · Max $50/Trade · Max 5 Positionen
    │
    ├─ LLM-Analyse (Claude) → Summary + Risiken + Sentiment
    │      ⚠️ LLM ENTSCHEIDET NICHT – nur Kommentator
    │
    └─ Trade ausführen (Paper oder Live via Alpaca)
           + In SQLite loggen
           + Dashboard aktualisieren
```

**Bearish-Strategie:** Statt Short Selling (Margin-Konto nötig) kauft der Bot
Long-Positionen auf Inverse ETFs (SH, PSQ). Gleiches Risikoprofil, keine
Margin-Anforderungen, identische Order-Logik im Code.

---

## Guardrails (unveränderlich im Code)

| Parameter | Wert | Zweck |
|---|---|---|
| Max. Kapital/Trade | $50 | 10% des Startkapitals |
| Max. Trades/Tag | 3 | Kein Overtrading |
| Stop Loss | -3% | Verlust begrenzen |
| Take Profit | +6% | CRV = 2:1 |
| VIX-Limit | 30 | Kein Handel bei Panik |
| Min. Score | 65/100 | Nur starke Signale |

---

## Profit-Ziel

Sobald das Portfolio **$1.000** erreicht (2x Startkapital):
- Dashboard zeigt Alert
- $500 entnehmen (Startkapital zurück)
- Restkapital = "Haus-Geld" (Totalverlust verschmerzbar)
