"""
llm_analyst.py – Claude API Integration für Trade-Kommentare.
Das LLM ENTSCHEIDET NICHT. Es erklärt und dokumentiert.
Bei API-Ausfall läuft der Bot im degraded mode weiter.
"""

import anthropic
from trading_shared.llm_commentary import analyze_with_llm as _shared_analyze_with_llm
from config import ANTHROPIC_API_KEY, LLM_MODEL, LLM_MAX_TOKENS
from rule_engine import SignalResult

SYSTEM_PROMPT = """Du bist ein kritischer, wissenschaftlich fundierter Trading-Analyst.
Dir werden strukturierte Kennzahlen einer Aktie übergeben.

Deine Aufgabe:
1. Erkläre in 2–3 Sätzen WARUM der Algorithmus diesen Trade freigegeben hat.
2. Nenne 2 konkrete Risiken, die der Algorithmus NICHT sieht (qualitative Risiken).
3. Gib einen Sentiment-Score von 1–10 aus (10 = sehr bullish für STOCK, sehr bearish für INVERSE_ETF).

WICHTIG: Du gibst KEINE Handelsempfehlung. Die Entscheidung liegt beim Algorithmus.
Antworte AUSSCHLIESSLICH im folgenden JSON-Format, ohne Markdown-Backticks:
{
  "summary": "...",
  "risks": ["Risiko 1", "Risiko 2"],
  "sentiment_score": 7
}"""


def analyze_with_llm(signal: SignalResult) -> dict:
    """
    Sendet Signal-Daten an Claude und erhält strukturierte Analyse zurück.
    Gibt dict mit summary, risks, sentiment_score zurück.
    Bei Fehler: leeres dict (Bot läuft weiter ohne LLM-Analyse).
    Call/Parse/Fallback-Mechanik seit Audit Chunk 1 (2026-08-05) in
    trading_shared.llm_commentary (identisch zur Saxo-Version, siehe dort) –
    hier bleibt nur der Alpaca-spezifische Prompt-Aufbau.
    """
    # Kontext für das LLM aufbauen
    user_content = f"""Analysiere diesen Swing-Trade-Kandidaten:

Ticker: {signal.ticker}
Instrument-Typ: {signal.instrument_type}
Aktueller Preis: ${signal.current_price}
Rule-Engine-Score: {signal.score}/100

Technische Kennzahlen:
- RSI (14): {signal.rsi}
- SMA 50: {signal.sma50}
- SMA 200: {signal.sma200}
- Volumen-Ratio (vs. 20-Tage-Ø): {signal.volume_ratio}x

Fundamentale Kennzahlen:
- KGV (Trailing P/E): {signal.pe_ratio}
- Verschuldungsgrad (D/E): {signal.debt_to_equity}%
- Umsatzwachstum (YoY): {signal.revenue_growth}

Stop Loss: ${signal.stop_loss} (-3%)
Take Profit: ${signal.take_profit} (+6%)
CRV: 2:1"""

    return _shared_analyze_with_llm(ANTHROPIC_API_KEY, LLM_MODEL, LLM_MAX_TOKENS, SYSTEM_PROMPT, user_content)


def get_market_brief() -> str:
    """
    Erstellt ein kurzes KI-Marktbriefing (max. 150 Wörter, Deutsch) anhand
    aktueller Indexstände. Bei fehlendem API-Key oder Fehler: Fallback-Text
    statt Absturz (siehe Modul-Docstring: Bot läuft im degraded mode weiter).
    """
    import yfinance as yf
    from datetime import datetime

    indices = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
        "VIX": "^VIX",
        "10Y Treasury": "^TNX",
        "EUR/USD": "EURUSD=X",
    }

    market_data = {}
    for name, ticker in indices.items():
        try:
            info = yf.Ticker(ticker).fast_info
            price = info.get("lastPrice", 0)
            prev = info.get("previousClose", price)
            change_pct = ((price - prev) / prev * 100) if prev else 0
            market_data[name] = {"price": price, "change_pct": change_pct}
        except Exception:
            pass

    market_summary = "\n".join([
        f"{name}: {d['price']:.2f} ({d['change_pct']:+.2f}%)"
        for name, d in market_data.items()
    ])

    if not ANTHROPIC_API_KEY:
        print("⚠️  Marktbriefing nicht verfügbar: Kein API-Key konfiguriert (degraded mode)")
        return f"Marktdaten (KI-Briefing nicht verfügbar):\n{market_summary}" if market_summary else "Marktdaten aktuell nicht verfügbar."

    prompt = f"""Heute ist {datetime.now().strftime('%d.%m.%Y')}.

Aktuelle Marktdaten:
{market_summary}

Erstelle eine kurze Marktbriefing (max 150 Wörter) auf Deutsch:
1. Wie ist die aktuelle Marktstimmung?
2. Worauf sollten Anleger heute achten?
3. Welche Sektoren sind besonders im Fokus?

Ton: sachlich, informativ, keine Anlageberatung.
Hinweis am Ende: "Dies ist kein Anlageberatung."
"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text.strip()
    except Exception as e:
        print(f"⚠️  Marktbriefing-Generierung fehlgeschlagen: {e} (degraded mode)")
        return f"Marktdaten (KI-Briefing fehlgeschlagen):\n{market_summary}" if market_summary else "Marktdaten aktuell nicht verfügbar."
