"""
rule_engine.py – Berechnet den Signal-Score (0–100) für jeden Ticker.
Entscheidet ob ein Trade freigegeben wird. Kein LLM involviert.
"""

import yfinance as yf
import pandas as pd
import pandas_ta as ta
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import warnings
warnings.filterwarnings("ignore")

from config import (
    RSI_OVERSOLD, RSI_OVERBOUGHT,
    VOLUME_FACTOR, PE_MIN, PE_MAX, DE_MAX,
    MIN_SIGNAL_SCORE, VIX_PAUSE_THRESHOLD,
    EARNINGS_BUFFER_DAYS, MAX_5DAY_MOVE_PCT,
    ACTIVE_SHORT_INSTRUMENTS, STOP_LOSS_PCT, TAKE_PROFIT_PCT
)
from database import get_session, get_active_weights


@dataclass
class SignalResult:
    """Ergebnis der Rule-Engine-Analyse für einen Ticker."""
    ticker:           str
    score:            int                    # 0–100
    direction:        str                    # 'LONG' oder 'BLOCKED'
    instrument_type:  str                    # 'STOCK' oder 'INVERSE_ETF'
    approved:         bool                   # True wenn Score ≥ MIN_SIGNAL_SCORE
    current_price:    float = 0.0
    stop_loss:        float = 0.0
    take_profit:      float = 0.0
    score_breakdown:  dict  = field(default_factory=dict)
    ko_reason:        Optional[str] = None   # Gesetzt wenn KO-Kriterium ausgelöst
    # Rohdaten für LLM-Analyse
    rsi:              Optional[float] = None
    pe_ratio:         Optional[float] = None
    debt_to_equity:   Optional[float] = None
    revenue_growth:   Optional[float] = None
    volume_ratio:     Optional[float] = None
    sma50:            Optional[float] = None
    sma200:           Optional[float] = None


def fetch_market_data(ticker: str, period: str = "1y", min_rows: int = 50) -> Optional[pd.DataFrame]:
    """Lädt historische OHLCV-Daten via yfinance."""
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        if df.empty or len(df) < min_rows:
            return None
        # Spaltennamen normalisieren (yfinance gibt MultiIndex zurück bei manchen Versionen)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
    except Exception as e:
        print(f"⚠️  Fehler beim Laden von {ticker}: {e}")
        return None


def fetch_fundamentals(ticker: str) -> dict:
    """Lädt Fundamentaldaten via yfinance info-Dict."""
    try:
        info = yf.Ticker(ticker).info
        return {
            "pe_ratio":       info.get("trailingPE"),
            "debt_to_equity": info.get("debtToEquity"),
            "revenue_growth": info.get("revenueGrowth"),   # YoY als Dezimalzahl (0.12 = 12%)
            "earnings_date":  info.get("earningsTimestamp"),
        }
    except Exception:
        return {}


def check_vix() -> tuple[float, bool]:
    """Prüft ob VIX unter dem Pausenschwellwert liegt."""
    df = fetch_market_data("^VIX", period="5d", min_rows=1)
    if df is None:
        return 0.0, True  # Im Zweifel: nicht pausieren
    vix = float(df["Close"].iloc[-1])
    return vix, vix <= VIX_PAUSE_THRESHOLD


def check_ko_criteria(ticker: str, df: pd.DataFrame, fundamentals: dict) -> Optional[str]:
    """
    Prüft alle KO-Kriterien. Gibt Grund zurück wenn KO ausgelöst, sonst None.
    KO-Kriterien überschreiben alle anderen Signale.
    """
    # 1. Earnings innerhalb der nächsten N Tage
    earnings_ts = fundamentals.get("earnings_date")
    if earnings_ts:
        earnings_date = datetime.fromtimestamp(earnings_ts)
        days_to_earnings = (earnings_date - datetime.now()).days
        if 0 <= days_to_earnings <= EARNINGS_BUFFER_DAYS:
            return f"Earnings in {days_to_earnings} Tagen – zu hohes Gap-Risiko"

    # 2. Aktie hat sich in 5 Tagen zu stark bewegt
    if len(df) >= 5:
        price_5d_ago = float(df["Close"].iloc[-5])
        price_now    = float(df["Close"].iloc[-1])
        move_5d      = abs(price_now - price_5d_ago) / price_5d_ago
        if move_5d > MAX_5DAY_MOVE_PCT:
            return f"5-Tage-Bewegung von {move_5d:.1%} überschreitet Limit ({MAX_5DAY_MOVE_PCT:.0%})"

    return None  # Kein KO-Kriterium ausgelöst


def calculate_score(ticker: str, df: pd.DataFrame, fundamentals: dict, is_inverse_etf: bool = False) -> SignalResult:
    """
    Berechnet den Signal-Score (0–100) anhand technischer und fundamentaler Kriterien.
    Gibt SignalResult zurück.
    """
    with get_session() as session:
        weights = get_active_weights(session)

    breakdown = {}
    current_price = float(df["Close"].iloc[-1])

    # ── RSI (20 Punkte) ──────────────────────────────────────────────
    rsi_series = ta.rsi(df["Close"], length=14)
    rsi = float(rsi_series.iloc[-1]) if rsi_series is not None and not rsi_series.empty else 50.0

    if is_inverse_etf:
        # Für Inverse ETFs: überKAUFTER Markt ist POSITIV (wir wollen fallen sehen)
        rsi_score = weights["rsi"] if rsi > RSI_OVERBOUGHT else int(weights["rsi"] * (rsi / RSI_OVERBOUGHT))
    else:
        # Für normale Aktien: überVERKAUFT ist bullisch
        rsi_score = weights["rsi"] if rsi < RSI_OVERSOLD else int(weights["rsi"] * max(0, (RSI_OVERSOLD - rsi + 20) / 20))

    breakdown["rsi"] = {"score": rsi_score, "max": weights["rsi"], "value": round(rsi, 1)}

    # ── SMA 50/200 Trend (20 Punkte) ─────────────────────────────────
    sma50  = float(df["Close"].rolling(50).mean().iloc[-1])  if len(df) >= 50  else None
    sma200 = float(df["Close"].rolling(200).mean().iloc[-1]) if len(df) >= 200 else None

    if sma50 and sma200:
        if is_inverse_etf:
            # Inverse ETF profitiert wenn Markt unter SMA50/200 fällt
            sma_score = weights["sma_trend"] if current_price < sma50 < sma200 else int(weights["sma_trend"] * 0.3)
        else:
            sma_score = weights["sma_trend"] if current_price > sma50 > sma200 else int(weights["sma_trend"] * 0.3)
    else:
        sma_score = int(weights["sma_trend"] * 0.5)  # Neutral wenn nicht genug Daten

    breakdown["sma_trend"] = {"score": sma_score, "max": weights["sma_trend"],
                               "value": {"sma50": round(sma50, 2) if sma50 else None,
                                         "sma200": round(sma200, 2) if sma200 else None}}

    # ── Volumen-Bestätigung (20 Punkte) ──────────────────────────────
    vol_20d_avg  = float(df["Volume"].rolling(20).mean().iloc[-1])
    vol_today    = float(df["Volume"].iloc[-1])
    volume_ratio = vol_today / vol_20d_avg if vol_20d_avg > 0 else 1.0
    vol_score    = weights["volume"] if volume_ratio >= VOLUME_FACTOR else int(weights["volume"] * (volume_ratio / VOLUME_FACTOR))

    breakdown["volume"] = {"score": vol_score, "max": weights["volume"], "value": round(volume_ratio, 2)}

    # ── KGV (15 Punkte) ──────────────────────────────────────────────
    pe = fundamentals.get("pe_ratio")
    if is_inverse_etf or pe is None:
        pe_score = int(weights["pe_ratio"] * 0.7)  # Neutral für ETFs
    elif PE_MIN <= pe <= PE_MAX:
        pe_score = weights["pe_ratio"]
    elif pe < PE_MIN or pe > PE_MAX * 1.5:
        pe_score = 0
    else:
        pe_score = int(weights["pe_ratio"] * 0.4)

    breakdown["pe_ratio"] = {"score": pe_score, "max": weights["pe_ratio"], "value": round(pe, 1) if pe else None}

    # ── Verschuldungsgrad (15 Punkte) ────────────────────────────────
    de = fundamentals.get("debt_to_equity")
    if is_inverse_etf or de is None:
        de_score = int(weights["debt_equity"] * 0.7)
    elif de <= DE_MAX:
        de_score = weights["debt_equity"]
    else:
        de_score = 0

    breakdown["debt_equity"] = {"score": de_score, "max": weights["debt_equity"], "value": round(de, 1) if de else None}

    # ── Revenue-Wachstum (10 Punkte) ─────────────────────────────────
    rev_growth = fundamentals.get("revenue_growth")
    if is_inverse_etf or rev_growth is None:
        rev_score = int(weights["revenue_growth"] * 0.7)
    elif rev_growth > 0:
        rev_score = weights["revenue_growth"]
    else:
        rev_score = 0

    breakdown["revenue_growth"] = {"score": rev_score, "max": weights["revenue_growth"],
                                    "value": f"{rev_growth:.1%}" if rev_growth else None}

    # ── Gesamtscore ───────────────────────────────────────────────────
    total_score = sum(v["score"] for v in breakdown.values())
    approved    = total_score >= MIN_SIGNAL_SCORE

    # Stop Loss & Take Profit
    stop_loss   = round(current_price * (1 - STOP_LOSS_PCT), 2)
    take_profit = round(current_price * (1 + TAKE_PROFIT_PCT), 2)

    return SignalResult(
        ticker          = ticker,
        score           = total_score,
        direction       = "LONG",
        instrument_type = "INVERSE_ETF" if is_inverse_etf else "STOCK",
        approved        = approved,
        current_price   = round(current_price, 2),
        stop_loss       = stop_loss,
        take_profit     = take_profit,
        score_breakdown = breakdown,
        rsi             = round(rsi, 1),
        pe_ratio        = round(pe, 1) if pe else None,
        debt_to_equity  = round(de, 1) if de else None,
        revenue_growth  = rev_growth,
        volume_ratio    = round(volume_ratio, 2),
        sma50           = round(sma50, 2) if sma50 else None,
        sma200          = round(sma200, 2) if sma200 else None,
    )


def analyze_ticker(ticker: str) -> SignalResult:
    """
    Hauptfunktion: Vollständige Analyse eines Tickers.
    Gibt SignalResult zurück – entweder mit approved=True oder mit ko_reason.
    """
    is_inverse_etf = ticker in ACTIVE_SHORT_INSTRUMENTS

    # Marktdaten laden
    df = fetch_market_data(ticker)
    if df is None:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="INVERSE_ETF" if is_inverse_etf else "STOCK",
            approved=False, ko_reason="Keine Marktdaten verfügbar"
        )

    # Fundamentaldaten (nicht für Inverse ETFs relevant)
    fundamentals = {} if is_inverse_etf else fetch_fundamentals(ticker)

    # KO-Kriterien prüfen
    ko = check_ko_criteria(ticker, df, fundamentals)
    if ko:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="INVERSE_ETF" if is_inverse_etf else "STOCK",
            approved=False, ko_reason=ko,
            current_price=float(df["Close"].iloc[-1])
        )

    # Score berechnen
    result = calculate_score(ticker, df, fundamentals, is_inverse_etf)
    return result


def scan_all_watchlists(long_watchlist: list, short_watchlist: list) -> list[SignalResult]:
    """
    Scannt alle Watchlist-Ticker und gibt sortierte Liste der Signale zurück.
    Nur Ergebnisse mit approved=True werden priorisiert.
    """
    results = []
    all_tickers = long_watchlist + short_watchlist
    print(f"🔍 Scanne {len(all_tickers)} Ticker...")

    for ticker in all_tickers:
        print(f"   → {ticker}", end=" ")
        result = analyze_ticker(ticker)
        if result.approved:
            print(f"✅ Score: {result.score}")
        elif result.ko_reason:
            print(f"🚫 KO: {result.ko_reason}")
        else:
            print(f"📉 Score: {result.score} (unter Limit)")
        results.append(result)

    # Sortiert: Approved zuerst, dann nach Score
    results.sort(key=lambda r: (not r.approved, -r.score))
    return results


if __name__ == "__main__":
    # Schnelltest
    print("=== VIX Check ===")
    vix, ok = check_vix()
    print(f"VIX: {vix:.1f} – Bot {'AKTIV' if ok else 'PAUSIERT'}")

    print("\n=== Einzelanalyse AAPL ===")
    result = analyze_ticker("AAPL")
    print(f"Score: {result.score}/100 | Freigegeben: {result.approved}")
    print(f"Preis: ${result.current_price} | SL: ${result.stop_loss} | TP: ${result.take_profit}")
    for k, v in result.score_breakdown.items():
        print(f"  {k}: {v['score']}/{v['max']} (Wert: {v['value']})")
