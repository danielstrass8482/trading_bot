"""
llm_analyst.py – Claude API Integration für Trade-Kommentare.
Das LLM ENTSCHEIDET NICHT. Es erklärt und dokumentiert.
Bei API-Ausfall läuft der Bot im degraded mode weiter.
"""

import json
import anthropic
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
    """
    if not ANTHROPIC_API_KEY:
        return _fallback_response("Kein API-Key konfiguriert")

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

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=LLM_MODEL,
            max_tokens=LLM_MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}]
        )

        raw_text = response.content[0].text.strip()
        # JSON parsen (Backticks entfernen falls doch vorhanden)
        clean_text = raw_text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(clean_text)

        # Validierung
        return {
            "summary":         str(parsed.get("summary", "")),
            "risks":           list(parsed.get("risks", [])),
            "sentiment_score": int(parsed.get("sentiment_score", 5))
        }

    except json.JSONDecodeError as e:
        return _fallback_response(f"JSON-Parse-Fehler: {e}")
    except Exception as e:
        return _fallback_response(f"API-Fehler: {e}")


def _fallback_response(reason: str) -> dict:
    """Leere Antwort wenn LLM nicht verfügbar – Bot läuft weiter."""
    print(f"⚠️  LLM-Analyse nicht verfügbar: {reason} (degraded mode)")
    return {
        "summary":         None,
        "risks":           [],
        "sentiment_score": None
    }
