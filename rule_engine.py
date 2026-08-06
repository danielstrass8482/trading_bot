"""
rule_engine.py – Berechnet den Signal-Score (0–100) für jeden Ticker.
Entscheidet ob ein Trade freigegeben wird. Kein LLM involviert.
"""

import yfinance as yf
import pandas as pd
import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
import warnings
warnings.filterwarnings("ignore")

from trading_shared import scoring as shared_scoring
from trading_shared.atr import calculate_atr
from trading_shared.graceful_shutdown import is_shutdown_requested

from config import (
    RSI_OVERSOLD, RSI_OVERBOUGHT,
    VOLUME_FACTOR, PE_MIN, PE_MAX, DE_MAX,
    EARNINGS_BUFFER_DAYS, MAX_5DAY_MOVE_PCT,
    ACTIVE_SHORT_INSTRUMENTS, get_live_config
)
from database import get_session, get_active_weights, get_open_trades
from fair_value import get_fair_value_for_ticker

_THRESHOLDS = {
    "RSI_OVERSOLD": RSI_OVERSOLD,
    "RSI_OVERBOUGHT": RSI_OVERBOUGHT,
    "VOLUME_FACTOR": VOLUME_FACTOR,
    "PE_MIN": PE_MIN,
    "PE_MAX": PE_MAX,
    "DE_MAX": DE_MAX,
}


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
    # ATR-basierter SL/TP (siehe calculate_atr) – None wenn ATR nicht verfügbar
    # war und auf feste Prozente aus bot_config zurückgefallen wurde.
    atr:              Optional[float] = None
    sl_pct:           Optional[float] = None
    tp_pct:           Optional[float] = None
    # Marktregime zum Analysezeitpunkt (siehe get_market_regime)
    market_regime:    Optional[str] = None
    # Fair Value (Stufe-1-Gatekeeper, siehe fair_value.py) – None wenn kein
    # Cache-Eintrag vorhanden war (z.B. ETFs/Inverse ETFs ohne KGV/Cashflow).
    fair_value_avg:      Optional[float] = None
    fair_value_discount: Optional[float] = None
    # Rohdaten für LLM-Analyse
    rsi:              Optional[float] = None
    pe_ratio:         Optional[float] = None
    debt_to_equity:   Optional[float] = None
    revenue_growth:   Optional[float] = None
    volume_ratio:     Optional[float] = None
    sma50:            Optional[float] = None
    sma200:           Optional[float] = None
    # yfinance-Sektor (siehe fetch_fundamentals) – bereits für die Branchen-
    # Blacklist-Prüfung geladen, hier zusätzlich durchgereicht statt verworfen,
    # damit broker.place_trade() ihn auf Trade.sector speichern kann (siehe
    # Feature "Sektor-Spalte Handelshistorie"). None bei Inverse ETFs (keine
    # Fundamentaldaten) oder falls yfinance keinen Sektor liefert.
    sector:           Optional[str] = None


def fetch_market_data(ticker: str, period: str = "1y", min_rows: int = 50) -> Optional[pd.DataFrame]:
    """Lädt historische OHLCV-Daten via yfinance.

    Nutzt yf.Ticker(ticker).history() statt yf.download() – Letzteres teilt
    sich intern Shared-State über yfinances Multi-Ticker-Download-Maschinerie,
    was unter dem 15-Worker-ThreadPoolExecutor (siehe
    scan_all_watchlists_parallel) nachweislich zu Cross-Contamination
    zwischen gleichzeitig gescannten Tickern führte (ES-Vorfall 2026-07-27:
    SL/TP/Menge wurden mit dem Kurs eines anderen, parallel gescannten
    Tickers berechnet – siehe [[trading-bot-deployment]]). Ticker(...) legt
    pro Aufruf eine eigene, isolierte Session an.
    """
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
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
            "sector":         info.get("sector"),
            "industry":       info.get("industry"),
        }
    except Exception as e:
        # Fund 14 (Code-Audit 2026-08-06): Sichtbarkeit statt stillem
        # Verschlucken – der Fallback (leere Fundamentaldaten -> neutrale
        # Score-Teilkredite) bleibt unverändert, ein systematisches
        # yfinance-Problem für eine Ticker-Untergruppe soll aber im Log
        # auffallen statt unbegrenzt lange unbemerkt zu bleiben.
        print(f"⚠️  fetch_fundamentals({ticker}): yfinance-Fehler ({e}) – keine Fundamentaldaten verfügbar.")
        return {}


# calculate_atr kommt seit Audit Chunk 1 (2026-08-05) aus trading_shared.atr
# (siehe Import oben) – war hier 1:1 identisch zur Saxo-Version dupliziert.

_REGIME_CACHE = {"value": None, "ts": None}
_REGIME_CACHE_TTL_SEC = 900  # 15 Min – vermeidet einen SPY-Download pro gescanntem Ticker


def get_market_regime() -> str:
    """
    Bestimmt das aktuelle Marktregime anhand SPY vs. SMA50/SMA200:
    "bullish" (Kurs > SMA200 und SMA50 > SMA200), "bearish" (umgekehrt)
    oder "neutral". Ergebnis wird kurz gecacht, da sonst jeder gescannte
    Ticker in analyze_ticker() einen eigenen SPY-Download auslösen würde.
    """
    now = datetime.now()
    if (_REGIME_CACHE["value"] is not None and _REGIME_CACHE["ts"] is not None
            and (now - _REGIME_CACHE["ts"]).total_seconds() < _REGIME_CACHE_TTL_SEC):
        return _REGIME_CACHE["value"]

    regime = "neutral"
    try:
        spy = fetch_market_data("SPY", period="1y", min_rows=200)
        if spy is not None:
            current = float(spy["Close"].iloc[-1])
            sma200 = float(spy["Close"].rolling(200).mean().iloc[-1])
            sma50 = float(spy["Close"].rolling(50).mean().iloc[-1])

            if current > sma200 and sma50 > sma200:
                regime = "bullish"
            elif current < sma200 and sma50 < sma200:
                regime = "bearish"
    except Exception as e:
        print(f"⚠️  Regime-Erkennung fehlgeschlagen: {e}")

    _REGIME_CACHE["value"] = regime
    _REGIME_CACHE["ts"] = now
    return regime


def check_correlation(ticker: str, open_tickers: list) -> tuple[bool, Optional[str]]:
    """
    Prüft ob ticker zu stark mit einer bereits offenen Position korreliert
    (3-Monats-Tagesrenditen, Schwelle 0.8) – vermeidet Klumpenrisiko durch
    mehrere Positionen, die faktisch dieselbe Bewegung abbilden.
    Gibt (True, None) zurück wenn unbedenklich oder bei fehlenden Daten.
    """
    if not open_tickers:
        return True, None

    try:
        # Einzeln statt als yf.download(all_tickers, ...)-Batch abfragen:
        # Letzteres läuft unter dem 15-Worker-ThreadPoolExecutor (siehe
        # scan_all_watchlists_parallel) in dieselbe Cross-Contamination-
        # Falle wie fetch_market_data() (siehe dort) - hier zusätzlich
        # verschärft, weil sich sogar die Ticker-Liste je nach gerade
        # offenen Positionen zwischen parallelen Aufrufen unterscheidet.
        closes = {}
        for t in [ticker] + open_tickers:
            hist = yf.Ticker(t).history(period="3mo", interval="1d", auto_adjust=True)
            if not hist.empty:
                closes[t] = hist["Close"]

        if ticker not in closes:
            return True, None

        data = pd.DataFrame(closes)
        returns = data.pct_change().dropna()

        for open_ticker in open_tickers:
            if ticker not in returns.columns or open_ticker not in returns.columns:
                continue
            corr = returns[ticker].corr(returns[open_ticker])
            if corr is not None and corr > 0.8:
                return False, f"Korrelation mit {open_ticker}: {corr:.2f} > 0.8"

        return True, None
    except Exception as e:
        print(f"⚠️  Korrelationsprüfung fehlgeschlagen für {ticker}: {e}")
        return True, None


def get_benchmark_performance(days: int = 30) -> dict:
    """
    Performance von S&P 500 und Nasdaq über die letzten `days` Tage (%) –
    für den Bot-vs-Markt-Vergleich in Dashboard und Tages-E-Mail. Liegt hier
    (statt in dashboard.py, das wegen seiner Streamlit-Top-Level-Aufrufe
    nicht importierbar ist), damit main.py es für die Tages-Mail mitnutzen kann.
    """
    benchmarks = {
        "S&P 500": "^GSPC",
        "Nasdaq": "^IXIC",
    }
    results = {}
    for name, ticker in benchmarks.items():
        try:
            df = yf.download(ticker, period=f"{days}d", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            closes = df["Close"].dropna()
            if not closes.empty:
                start = float(closes.iloc[0])
                end = float(closes.iloc[-1])
                pct = (end - start) / start * 100
                results[name] = round(pct, 2)
            else:
                results[name] = None
        except Exception:
            results[name] = None
    return results


def check_vix() -> tuple[float, bool]:
    """Prüft ob VIX unter dem Pausenschwellwert liegt."""
    df = fetch_market_data("^VIX", period="5d", min_rows=1)
    if df is None:
        return 0.0, True  # Im Zweifel: nicht pausieren
    vix = float(df["Close"].iloc[-1])
    threshold = get_live_config()["VIX_PAUSE_THRESHOLD"]
    return vix, vix <= threshold


def check_earnings_risk(ticker: str, days_buffer: int = 3) -> tuple[bool, Optional[str]]:
    """
    Prüft ob Earnings in den nächsten days_buffer Handelstagen sind – via
    yf.Ticker.calendar (deutlich zuverlässiger als das mittlerweile oft
    fehlende info["earningsTimestamp"], das dieser Check vorher nutzte).
    Gibt (True, Grund) bei Risiko zurück, sonst (False, None).
    """
    try:
        t = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None or (hasattr(cal, "empty") and cal.empty):
            return False, None

        earnings_date = None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date")
            if dates:
                earnings_date = pd.Timestamp(dates[0] if isinstance(dates, list) else dates)
        elif "Earnings Date" in getattr(cal, "columns", []):
            dates = cal["Earnings Date"].dropna()
            if not dates.empty:
                earnings_date = pd.Timestamp(dates.iloc[0])

        if earnings_date is None:
            return False, None

        now = pd.Timestamp.now(tz="America/New_York").tz_localize(None)
        days_until = (earnings_date - now).days

        if 0 <= days_until <= days_buffer:
            return True, (f"Earnings in {days_until} Tagen "
                         f"({earnings_date.strftime('%d.%m.%Y')})")
        return False, None
    except Exception as e:
        print(f"Earnings-Check Fehler {ticker}: {e}")
        return False, None


def check_ko_criteria(ticker: str, df: pd.DataFrame, fundamentals: dict) -> Optional[str]:
    """
    Prüft alle KO-Kriterien. Gibt Grund zurück wenn KO ausgelöst, sonst None.
    KO-Kriterien überschreiben alle anderen Signale.
    """
    # 1. Earnings innerhalb der nächsten N Tage (siehe check_earnings_risk)
    days_buffer = int(get_live_config().get("EARNINGS_BUFFER_DAYS", EARNINGS_BUFFER_DAYS))
    earnings_risk, earnings_reason = check_earnings_risk(ticker, days_buffer)
    if earnings_risk:
        return f"Earnings-Risiko: {earnings_reason}"

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

    cfg = get_live_config()  # MIN_SIGNAL_SCORE / SL / TP aus DB (mit Fallback)

    current_price = float(df["Close"].iloc[-1])

    # 6-Faktoren-Score (RSI/SMA-Trend/Volumen/KGV/Verschuldungsgrad/Umsatz-
    # wachstum) – seit Audit Chunk 1 (2026-08-05) in trading_shared.scoring,
    # geteilt mit trading_bot_saxo (identische Formeln/Punkteverteilung).
    total_score, breakdown, raw = shared_scoring.calculate_score_factors(
        df, fundamentals, weights, _THRESHOLDS, is_inverse_etf=is_inverse_etf
    )
    rsi = raw["rsi"]
    sma50 = raw["sma50"]
    sma200 = raw["sma200"]
    volume_ratio = raw["volume_ratio"]
    pe = raw["pe_ratio"]
    de = raw["debt_to_equity"]
    rev_growth = raw["revenue_growth"]

    approved = total_score >= cfg["MIN_SIGNAL_SCORE"]

    # Stop Loss & Take Profit – ATR-basiert (volatilitätsabhängiger Abstand),
    # mit Fallback auf feste Prozente aus bot_config falls ATR nicht verfügbar.
    atr = calculate_atr(ticker)
    atr_multiplier_sl = cfg.get("ATR_MULTIPLIER_SL", 1.5)
    atr_multiplier_tp = cfg.get("ATR_MULTIPLIER_TP", 3.0)  # CRV 2:1

    if atr and atr > 0:
        sl_distance = atr * atr_multiplier_sl
        tp_distance = atr * atr_multiplier_tp

        # Sicherheitsnetz: SL zwischen ATR_MIN_SL_PCT und ATR_MAX_SL_PCT
        min_sl_pct = cfg.get("ATR_MIN_SL_PCT", 0.01)
        max_sl_pct = cfg.get("ATR_MAX_SL_PCT", 0.08)
        sl_pct = max(min_sl_pct, min(max_sl_pct, sl_distance / current_price))
        tp_pct = max(min_sl_pct * 2, min(max_sl_pct * 2, tp_distance / current_price))

        stop_loss   = round(current_price * (1 - sl_pct), 2)
        take_profit = round(current_price * (1 + tp_pct), 2)

        print(f"ATR: ${atr:.2f} → SL: -{sl_pct*100:.1f}% TP: +{tp_pct*100:.1f}%")
    else:
        sl_pct = float(cfg["STOP_LOSS_PCT"])
        tp_pct = float(cfg["TAKE_PROFIT_PCT"])
        stop_loss   = round(current_price * (1 - sl_pct), 2)
        take_profit = round(current_price * (1 + tp_pct), 2)

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
        atr             = round(atr, 2) if not shared_scoring.is_missing(atr) else None,
        sl_pct          = sl_pct,
        tp_pct          = tp_pct,
        rsi             = round(rsi, 1),
        pe_ratio        = round(pe, 1) if not shared_scoring.is_missing(pe) else None,
        debt_to_equity  = round(de, 1) if not shared_scoring.is_missing(de) else None,
        revenue_growth  = rev_growth,
        volume_ratio    = round(volume_ratio, 2),
        sma50           = round(sma50, 2) if not shared_scoring.is_missing(sma50) else None,
        sma200          = round(sma200, 2) if not shared_scoring.is_missing(sma200) else None,
        sector          = fundamentals.get("sector"),
    )


def analyze_ticker(ticker: str) -> SignalResult:
    """
    Hauptfunktion: Vollständige Analyse eines Tickers.
    Gibt SignalResult zurück – entweder mit approved=True oder mit ko_reason.
    """
    is_inverse_etf = ticker in ACTIVE_SHORT_INSTRUMENTS
    regime = get_market_regime()

    # ── Fair Value Check (Stufe 1 Gatekeeper – WAS kaufen?) ──────────────
    # Läuft VOR dem teuren Marktdaten-Download: nur Ticker mit ≥10% Rabatt
    # zum wöchentlich berechneten Fair Value (siehe fair_value.py) erreichen
    # überhaupt die technische Stufe-2-Prüfung unten. Inverse ETFs haben kein
    # KGV/Cashflow/Dividende und werden daher vom Gatekeeper ausgenommen.
    fv = None
    fv_bonus = 0
    if not is_inverse_etf:
        fv = get_fair_value_for_ticker(ticker)

        if fv is not None:
            if not fv["is_undervalued"]:
                discount = fv.get("discount_pct", 0)
                return SignalResult(
                    ticker=ticker, score=0, direction="BLOCKED",
                    instrument_type="STOCK", approved=False,
                    ko_reason=(f"Fair Value: Aktie {abs(discount):.0f}% "
                               f"{'über' if discount < 0 else 'unter'} "
                               f"Fair Value ${fv['fair_value_avg']:.0f} "
                               f"(mind. 10% Rabatt nötig)"),
                    market_regime=regime,
                    fair_value_avg=fv.get("fair_value_avg"),
                    fair_value_discount=discount,
                )

            # Value Trap Warnung → nicht blocken, aber Hinweis fürs Log/LLM
            if fv["value_trap_risk"] == "high":
                print(f"⚠️ {ticker}: Value Trap Risiko hoch!")

            # Fair Value Discount als Score-Bonus (max +10 für tiefe Unterbewertung)
            fv_bonus = min(10, int(fv["discount_pct"] / 2))
        # Kein Cache-Eintrag (z.B. ETFs ohne KGV) → nicht blocken, fv_bonus=0

    # Marktdaten laden
    df = fetch_market_data(ticker)
    if df is None:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="INVERSE_ETF" if is_inverse_etf else "STOCK",
            approved=False, ko_reason="Keine Marktdaten verfügbar",
            market_regime=regime,
        )

    # Fundamentaldaten (nicht für Inverse ETFs relevant)
    fundamentals = {} if is_inverse_etf else fetch_fundamentals(ticker)

    # Branchen-Blacklist prüfen (vor der Score-Berechnung)
    sector = fundamentals.get("sector", "") or ""
    industry = fundamentals.get("industry", "") or ""
    blacklist_key = shared_scoring.is_sector_blacklisted(sector, industry)
    if blacklist_key:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="STOCK", approved=False,
            ko_reason=f"Blacklist: {blacklist_key}",
            market_regime=regime,
        )

    # KO-Kriterien prüfen
    ko = check_ko_criteria(ticker, df, fundamentals)
    if ko:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="INVERSE_ETF" if is_inverse_etf else "STOCK",
            approved=False, ko_reason=ko,
            current_price=float(df["Close"].iloc[-1]),
            market_regime=regime,
        )

    # Korrelationsfilter: zu hohe Korrelation mit einer bereits offenen
    # Position vermeidet Klumpenrisiko (siehe check_correlation).
    with get_session() as session:
        open_tickers = [t.ticker for t in get_open_trades(session)]
    corr_ok, corr_reason = check_correlation(ticker, open_tickers)
    if not corr_ok:
        return SignalResult(
            ticker=ticker, score=0, direction="BLOCKED",
            instrument_type="INVERSE_ETF" if is_inverse_etf else "STOCK",
            approved=False, ko_reason=f"Korrelationsfilter: {corr_reason}",
            current_price=float(df["Close"].iloc[-1]),
            market_regime=regime,
        )

    # Score berechnen
    result = calculate_score(ticker, df, fundamentals, is_inverse_etf)
    result.market_regime = regime
    result.fair_value_avg = fv.get("fair_value_avg") if fv else None
    result.fair_value_discount = fv.get("discount_pct") if fv else None

    if fv_bonus:
        result.score += fv_bonus
        print(f"💰 Fair Value Bonus: +{fv_bonus} für {ticker} ({fv['discount_pct']:.0f}% Rabatt)")

    # Markt-Regime-Filter: in einem bärischen Markt werden LONG-Aktien
    # abgestraft und Inverse ETFs bevorzugt (score muss danach neu gegen
    # MIN_SIGNAL_SCORE geprüft werden).
    if regime == "bearish" and result.instrument_type == "STOCK" and result.direction == "LONG":
        result.score -= 10
        print(f"📉 Bearish Regime: Score -10 für {ticker}")
    elif regime == "bearish" and result.instrument_type == "INVERSE_ETF":
        result.score += 10
        print(f"📉 Bearish Regime: Score +10 für {ticker} (Inverse ETF)")

    cfg = get_live_config()
    result.approved = result.score >= cfg["MIN_SIGNAL_SCORE"]

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


def scan_watchlist_parallel(watchlist: list, max_workers: int = 15) -> list[SignalResult]:
    """
    Wie scan_all_watchlists(), aber mit einem ThreadPoolExecutor parallelisiert –
    für die volle S&P-500-Watchlist (~390 Ticker) wäre der serielle Scan zu
    langsam für ein 09:00-ET-Zeitfenster. analyze_ticker() holt Marktdaten/
    Regime/Guardrails weiterhin selbst (keine zusätzlichen Parameter nötig),
    daher hier nur ticker-parallel statt die Signatur zu ändern.
    """
    results = []
    errors = 0
    # Graceful Shutdown (Bugfix 2026-08-06): der Scan läuft bei einem
    # SIGTERM-während-Deploy BEWUSST vollständig zu Ende (siehe
    # graceful_shutdown.py) – ein hier abgebrochener Scan war genau der
    # Incident vom 06.08.: ein später fertiggewordener Kandidat hätte über
    # der Kaufschwelle liegen können, ohne dass es jemand erfährt. Der Flag-
    # Check hier ist daher rein informativ (einmaliges Log), kein Abbruch.
    shutdown_notice_logged = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(analyze_ticker, ticker): ticker for ticker in watchlist}

        for future in concurrent.futures.as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result(timeout=30)
                if result:
                    results.append(result)
            except Exception as e:
                errors += 1
                print(f"⚠️ {ticker}: {e}")

            if is_shutdown_requested() and not shutdown_notice_logged:
                print(f"   ℹ️  Shutdown angefordert – Scan läuft trotzdem vollständig zu Ende "
                      f"(kein verpasster Kandidat), zuletzt fertig: {ticker}.")
                shutdown_notice_logged = True

    print(f"✅ Parallel-Scan: {len(results)} Ergebnisse, {errors} Fehler")
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
