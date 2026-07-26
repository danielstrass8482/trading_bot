"""
fair_value.py – Stufe 1 des 2-Stufen-Filters: WAS kaufen?
Berechnet einen groben Fair Value je Ticker (KGV-/KCV-/Dividenden-basiert)
und markiert Aktien mit ≥10% Rabatt als unterbewertet. Läuft wöchentlich
(siehe main.py: fair_value_update Job) und cached das Ergebnis in
fair_value_cache – rule_engine.analyze_ticker() liest daraus als Gatekeeper
VOR dem täglichen 8-Faktoren-Score (Stufe 2: WANN kaufen?).
"""

from datetime import datetime

import yfinance as yf

from database import get_session, FairValueCache

# Sektor-spezifische faire KGV-Multiples (Kurs/Gewinn) – Basiswerte aus
# historischen Sektordurchschnitten, keine Live-Marktdaten.
SEKTOR_KGV = {
    "Technology":             25.0,
    "Communication Services": 20.0,
    "Consumer Discretionary": 18.0,
    "Consumer Staples":       20.0,
    "Financials":             12.0,
    "Health Care":            22.0,
    "Industrials":            18.0,
    "Materials":              15.0,
    "Real Estate":            35.0,  # REITs anders bewertet
    "Utilities":              18.0,
    "Energy":                 12.0,
    "default":                17.0,  # S&P 500 historischer Durchschnitt
}

# Sektor-spezifische faire KCV-Multiples (Kurs/Cashflow)
SEKTOR_KCV = {
    "Technology":             20.0,
    "Communication Services": 15.0,
    "Consumer Discretionary": 14.0,
    "Consumer Staples":       16.0,
    "Financials":             10.0,
    "Health Care":            18.0,
    "Industrials":            14.0,
    "Materials":              12.0,
    "Real Estate":            18.0,
    "Utilities":              14.0,
    "Energy":                 10.0,
    "default":                14.0,
}


def calculate_fair_value(ticker: str) -> dict | None:
    """
    Berechnet den Fair Value eines einzelnen Tickers aus bis zu drei
    Teilbewertungen (KGV/KCV/Dividende) und mittelt die validen davon.
    Gibt None zurück wenn keine der drei Methoden anwendbar ist (z.B. ETFs
    ohne KGV/Cashflow/Dividende – die werden dadurch implizit vom Fair-Value-
    Gatekeeper ausgenommen, siehe rule_engine.analyze_ticker).
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info

        if not info:
            return None

        current_price = info.get("currentPrice") or info.get("regularMarketPrice", 0)
        if not current_price or current_price <= 0:
            return None

        sector = info.get("sector") or "default"

        # 1. KGV-basierter Fair Value
        eps = info.get("trailingEps", 0)
        pe_ratio = info.get("trailingPE", 0)
        fair_kgv = None
        if eps and eps > 0:
            target_pe = SEKTOR_KGV.get(sector, SEKTOR_KGV["default"])
            fair_kgv = eps * target_pe

        # 2. KCV-basierter Fair Value (Cashflow)
        cashflow = info.get("operatingCashflow", 0)
        shares = info.get("sharesOutstanding", 1)
        fair_kcv = None
        cashflow_per_share = None
        if cashflow and cashflow > 0 and shares:
            cashflow_per_share = cashflow / shares
            target_kcv = SEKTOR_KCV.get(sector, SEKTOR_KCV["default"])
            fair_kcv = cashflow_per_share * target_kcv

        # 3. Dividenden-basierter Fair Value (nur für Dividendentitel sinnvoll)
        div_yield = info.get("dividendYield", 0)
        div_rate = info.get("dividendRate", 0)
        fair_div = None
        if div_rate and div_rate > 0:
            target_yield = 0.03  # historische Ø-Dividendenrendite stabiler Sektoren
            fair_div = div_rate / target_yield

        # Durchschnitt Fair Value (nur valide Werte)
        valid_values = [v for v in [fair_kgv, fair_kcv, fair_div] if v and v > 0]
        fair_avg = sum(valid_values) / len(valid_values) if valid_values else None

        if not fair_avg:
            return None

        # Bewertung: positiv = günstig (unter Fair Value), negativ = teuer
        discount = (fair_avg - current_price) / fair_avg * 100
        is_undervalued = discount >= 10  # ≥10% unter Fair Value

        # Value Trap Risiko: hohe Schulden + fallender Umsatz = Warnsignal
        debt_equity = info.get("debtToEquity", 0) or 0
        rev_growth = info.get("revenueGrowth", 0) or 0

        if debt_equity > 200 or rev_growth < -0.1:
            value_trap_risk = "high"
        elif debt_equity > 100 or rev_growth < 0:
            value_trap_risk = "medium"
        else:
            value_trap_risk = "low"

        return {
            "ticker": ticker,
            "current_price": current_price,
            "pe_ratio": pe_ratio,
            "eps": eps,
            "cashflow_per_share": round(cashflow_per_share, 2) if cashflow_per_share else None,
            "dividend_yield": div_yield,
            "sector": sector,
            "fair_value_kgv": round(fair_kgv, 2) if fair_kgv else None,
            "fair_value_kcv": round(fair_kcv, 2) if fair_kcv else None,
            "fair_value_div": round(fair_div, 2) if fair_div else None,
            "fair_value_avg": round(fair_avg, 2),
            "discount_pct": round(discount, 1),
            "is_undervalued": is_undervalued,
            "value_trap_risk": value_trap_risk,
        }
    except Exception as e:
        print(f"Fair Value Fehler {ticker}: {e}")
        return None


def update_fair_value_cache(watchlist: list) -> int:
    """Berechnet Fair Value für alle Ticker der Watchlist und upserted den Cache."""
    print(f"Fair Value Update für {len(watchlist)} Ticker...")
    updated = 0

    with get_session() as session:
        for ticker in watchlist:
            result = calculate_fair_value(ticker)
            if not result:
                continue

            existing = session.query(FairValueCache).filter_by(ticker=ticker).first()
            if existing:
                for key, value in result.items():
                    if hasattr(existing, key):
                        setattr(existing, key, value)
                existing.updated_at = datetime.utcnow()
            else:
                session.add(FairValueCache(**result))

            updated += 1

        session.commit()

    print(f"✅ Fair Value Cache aktualisiert: {updated} Ticker")
    return updated


def get_undervalued_tickers() -> list[tuple]:
    """Alle unterbewerteten (≥10% Rabatt, kein hohes Value-Trap-Risiko) Ticker, höchster Rabatt zuerst."""
    with get_session() as session:
        rows = session.query(FairValueCache).filter(
            FairValueCache.is_undervalued == True,
            FairValueCache.value_trap_risk != "high"
        ).order_by(
            FairValueCache.discount_pct.desc()
        ).all()

        return [(r.ticker, r.discount_pct, r.fair_value_avg, r.current_price, r.value_trap_risk)
                for r in rows]


def get_fair_value_for_ticker(ticker: str) -> dict | None:
    """Liest den gecachten Fair-Value-Eintrag eines Tickers (siehe rule_engine.analyze_ticker)."""
    with get_session() as session:
        row = session.query(FairValueCache).filter_by(ticker=ticker).first()
        if not row:
            return None
        return {
            "fair_value_avg": row.fair_value_avg,
            "discount_pct": row.discount_pct,
            "is_undervalued": row.is_undervalued,
            "value_trap_risk": row.value_trap_risk,
            "sector": row.sector,
            "updated_at": row.updated_at,
        }
