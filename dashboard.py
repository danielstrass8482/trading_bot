"""
dashboard.py – Streamlit Dashboard für den Trading Bot.
Zeigt Portfolio, offene Positionen, Trade-Log und Bot-Controls.
"""

import streamlit as st
import pandas as pd
import yfinance as yf
from datetime import datetime, date
import json

from config import (
    MAX_CAPITAL_TOTAL, MAX_CAPITAL_PER_TRADE,
    STOP_LOSS_PCT, TAKE_PROFIT_PCT, MIN_SIGNAL_SCORE,
    VIX_PAUSE_THRESHOLD, TRADING_MODE, LONG_WATCHLIST,
    ACTIVE_SHORT_INSTRUMENTS, PROFIT_ALERT_TARGET, get_live_config,
    DEFAULT_USER_ID,
)
from database import (
    init_db, get_session, get_open_trades, get_total_pnl,
    get_daily_trade_count, get_total_capital_in_trades, DailyLog, Trade, BotState,
    WeightHistory, get_active_weights, CLOSED_STATUSES, PostExitTracking
)
from rule_engine import analyze_ticker, check_vix, get_benchmark_performance
from broker import get_portfolio_value, get_bot_performance
from main import calculate_max_trades_today
from fair_value import get_undervalued_tickers
from post_exit_tracking import analyze_threshold_effectiveness

# ── PAGE CONFIG ────────────────────────────────────────────────────
st.set_page_config(
    page_title="Trading Bot",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ── STYLING ────────────────────────────────────────────────────────
st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap');

  html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

  .block-container { padding: 2rem 2.5rem 2rem 2.5rem; max-width: 1400px; }

  /* KPI Cards */
  .kpi-card {
    background: #0f1117;
    border: 1px solid #1e2130;
    border-radius: 10px;
    padding: 1.2rem 1.5rem;
    margin-bottom: 0.5rem;
  }
  .kpi-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #6b7280;
    margin-bottom: 0.4rem;
  }
  .kpi-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 1.8rem;
    font-weight: 600;
    color: #f9fafb;
    line-height: 1.1;
  }
  .kpi-value.positive { color: #34d399; }
  .kpi-value.negative { color: #f87171; }
  .kpi-value.neutral  { color: #60a5fa; }

  /* Status badges */
  .badge {
    display: inline-block;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.05em;
  }
  .badge-open     { background: #1e3a5f; color: #60a5fa; }
  .badge-tp       { background: #064e3b; color: #34d399; }
  .badge-sl       { background: #450a0a; color: #f87171; }
  .badge-paper    { background: #312e81; color: #a5b4fc; }
  .badge-live     { background: #431407; color: #fb923c; }

  /* Score bar */
  .score-bar-bg {
    background: #1e2130;
    border-radius: 4px;
    height: 6px;
    width: 100%;
    margin-top: 0.4rem;
  }
  .score-bar-fill {
    height: 6px;
    border-radius: 4px;
    background: linear-gradient(90deg, #3b82f6, #34d399);
  }

  /* Guardrail status */
  .guardrail-ok     { color: #34d399; font-size: 0.85rem; }
  .guardrail-warn   { color: #fbbf24; font-size: 0.85rem; }
  .guardrail-block  { color: #f87171; font-size: 0.85rem; }

  /* Section headers */
  .section-label {
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #4b5563;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid #1e2130;
    margin-bottom: 1rem;
  }

  /* Hide streamlit default elements */
  #MainMenu { visibility: hidden; }
  footer    { visibility: hidden; }
</style>
""", unsafe_allow_html=True)

# ── INIT ───────────────────────────────────────────────────────────
init_db()


# ── HELPERS ───────────────────────────────────────────────────────
def fmt_usd(val):
    if val is None: return "—"
    color = "positive" if val > 0 else ("negative" if val < 0 else "neutral")
    sign  = "+" if val > 0 else ""
    return f'<span class="kpi-value {color}">{sign}${val:,.2f}</span>'

def fmt_pct(val):
    if val is None: return "—"
    color = "positive" if val > 0 else ("negative" if val < 0 else "neutral")
    sign  = "+" if val > 0 else ""
    return f'{sign}{val:.1f}%'

def status_badge(status):
    mapping = {
        "OPEN":              ("OPEN",       "badge-open"),
        "CLOSED_TP":         ("TP ✓",       "badge-tp"),
        "CLOSED_SL":         ("SL ✗",       "badge-sl"),
        "CLOSED_TRAILING_SL": ("TRAIL ✓",   "badge-tp"),
        "CLOSED_TIME_EXIT":  ("TIME-EXIT",  "badge-open"),
        "CLOSED_MANUAL":     ("MANUAL",     "badge-open"),
        "FAILED_ENTRY":      ("KAUF FEHLGESCHLAGEN", "badge-sl"),
    }
    label, cls = mapping.get(status, (status, "badge-open"))
    return f'<span class="badge {cls}">{label}</span>'

def mode_badge(mode):
    cls = "badge-paper" if mode == "PAPER" else "badge-live"
    return f'<span class="badge {cls}">{mode}</span>'


# ── SIDEBAR ───────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🤖 Trading Bot")
    st.markdown(f'<div style="margin-bottom:1rem">{mode_badge(TRADING_MODE)}</div>', unsafe_allow_html=True)

    st.markdown('<div class="section-label">Guardrails</div>', unsafe_allow_html=True)

    with get_session() as session:
        daily_count  = get_daily_trade_count(session)
        open_count   = len(get_open_trades(session))
        bot_paused   = BotState.get(session, "bot_paused") == "true"
        invested     = get_total_capital_in_trades(session)

    # Verfügbare Trades/Kapital – dynamisch aus aktuell investiertem Kapital
    # und offenen Positionen berechnet (siehe main.calculate_max_trades_today),
    # ersetzt die feste "Trades heute: X/5"-Anzeige.
    live_cfg = get_live_config()
    max_capital_total = live_cfg.get("MAX_CAPITAL_TOTAL", MAX_CAPITAL_TOTAL)
    available_capital = max_capital_total - invested
    available_trades = calculate_max_trades_today()
    cls = "guardrail-ok" if available_trades > 1 else ("guardrail-warn" if available_trades == 1 else "guardrail-block")
    st.markdown(f'<p class="{cls}">Verfügbar: {available_trades} Trades (${available_capital:.0f} frei)</p>', unsafe_allow_html=True)

    # Offene Positionen
    pos_pct = open_count / 5
    cls2 = "guardrail-ok" if pos_pct < 0.8 else ("guardrail-warn" if pos_pct < 1.0 else "guardrail-block")
    st.markdown(f'<p class="{cls2}">Offene Positionen: {open_count}/5</p>', unsafe_allow_html=True)

    # Stop Loss / Take Profit
    st.markdown(f'<p class="guardrail-ok">Stop Loss: {STOP_LOSS_PCT:.0%} | TP: {TAKE_PROFIT_PCT:.0%}</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="guardrail-ok">Min. Score: {MIN_SIGNAL_SCORE}/100</p>', unsafe_allow_html=True)
    st.markdown(f'<p class="guardrail-ok">VIX-Limit: {VIX_PAUSE_THRESHOLD}</p>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown('<div class="section-label">Bot-Steuerung</div>', unsafe_allow_html=True)

    if bot_paused:
        st.error("⏸️ Bot ist pausiert")
        if st.button("▶️ Bot fortsetzen", use_container_width=True):
            with get_session() as session:
                BotState.set(session, "bot_paused", "false")
                session.commit()
            st.rerun()
    else:
        st.success("▶️ Bot läuft")
        if st.button("⏸️ Bot pausieren", use_container_width=True):
            with get_session() as session:
                BotState.set(session, "bot_paused", "true")
                session.commit()
            st.rerun()

    st.markdown("---")
    st.caption(f"Stand: {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    if st.button("🔄 Seite aktualisieren", use_container_width=True):
        st.rerun()


# ── TABS ──────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Übersicht", "📋 Trade-Log", "🔍 Signal-Analyse", "📈 Performance", "🧠 Backlook"
])


# ══════════════════════════════════════════════════════════════════
# TAB 1: ÜBERSICHT
# ══════════════════════════════════════════════════════════════════
with tab1:
    # KPI-Reihe
    with get_session() as session:
        realized_pnl = get_total_pnl(session)
        open_trades  = get_open_trades(session)

    portfolio_value = get_portfolio_value()
    pnl_pct = (portfolio_value - MAX_CAPITAL_TOTAL) / MAX_CAPITAL_TOTAL * 100

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-label">Portfolio-Wert</div>
            {fmt_usd(portfolio_value)}
        </div>""", unsafe_allow_html=True)

    with col2:
        pnl_color = "positive" if realized_pnl >= 0 else "negative"
        sign = "+" if realized_pnl >= 0 else ""
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-label">Realisierter P&L</div>
            <div class="kpi-value {pnl_color}">{sign}${realized_pnl:,.2f}</div>
        </div>""", unsafe_allow_html=True)

    with col3:
        pct_color = "positive" if pnl_pct >= 0 else "negative"
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-label">Rendite gesamt</div>
            <div class="kpi-value {pct_color}">{fmt_pct(pnl_pct)}</div>
        </div>""", unsafe_allow_html=True)

    with col4:
        target_pct = portfolio_value / PROFIT_ALERT_TARGET * 100
        st.markdown(f"""
        <div class="kpi-card">
            <div class="kpi-label">Ziel ${PROFIT_ALERT_TARGET:,.0f} (→ Kapital raus)</div>
            <div class="kpi-value neutral">{target_pct:.0f}%</div>
            <div class="score-bar-bg"><div class="score-bar-fill" style="width:{min(target_pct,100):.0f}%"></div></div>
        </div>""", unsafe_allow_html=True)

    # Profit-Alert
    if portfolio_value >= PROFIT_ALERT_TARGET:
        st.success(f"🎯 **Profit-Ziel erreicht!** Du kannst jetzt ${MAX_CAPITAL_TOTAL:,.0f} entnehmen – dein Startkapital ist draußen.")

    st.markdown("<br>", unsafe_allow_html=True)

    # VIX Status
    col_vix, col_status = st.columns([1, 2])
    with col_vix:
        st.markdown('<div class="section-label">Markt-Status</div>', unsafe_allow_html=True)
        try:
            vix, vix_ok = check_vix()
            vix_color = "#34d399" if vix_ok else "#f87171"
            vix_status = "Handel aktiv" if vix_ok else "Bot pausiert (Fear-Modus)"
            st.markdown(f"""
            <div class="kpi-card">
                <div class="kpi-label">VIX (Angstindex)</div>
                <div class="kpi-value" style="color:{vix_color}">{vix:.1f}</div>
                <div style="color:{vix_color}; font-size:0.8rem; margin-top:0.3rem">{vix_status}</div>
            </div>""", unsafe_allow_html=True)
        except Exception:
            st.info("VIX nicht verfügbar")

    # Offene Positionen
    with col_status:
        st.markdown('<div class="section-label">Offene Positionen</div>', unsafe_allow_html=True)
        if not open_trades:
            st.markdown('<p style="color:#6b7280; font-size:0.9rem">Keine offenen Positionen.</p>', unsafe_allow_html=True)
        else:
            for t in open_trades:
                # Fix 2026-07-31: entry_price ist None, solange die Kauf-Order
                # noch WAITING_FILL ist (siehe broker.place_trade) - PnL lässt
                # sich dafür noch nicht berechnen.
                if t.entry_price is None:
                    st.markdown(f"""
                    <div style="margin-bottom:0.9rem">
                        <div><strong>{t.ticker}</strong> {mode_badge(t.mode)}</div>
                        <div style="color:#9ca3af; font-size:0.85rem; margin-top:0.15rem">
                            Kauf-Order wartet auf Fill-Bestätigung ·
                            SL: <code>${t.stop_loss:.2f}</code> ·
                            TP: <code>${t.take_profit:.2f}</code>
                        </div>
                    </div>""", unsafe_allow_html=True)
                    continue

                try:
                    current_price = float(yf.Ticker(t.ticker).fast_info.get("lastPrice", t.entry_price))
                except Exception:
                    current_price = t.entry_price

                unrealized_pnl = (current_price - t.entry_price) * t.quantity
                unrealized_pct = (
                    (current_price - t.entry_price) / t.entry_price * 100
                    if t.entry_price else 0.0
                )
                pnl_color = "positive" if unrealized_pnl >= 0 else "negative"
                sign = "+" if unrealized_pnl >= 0 else ""

                st.markdown(f"""
                <div style="margin-bottom:0.9rem">
                    <div><strong>{t.ticker}</strong> {mode_badge(t.mode)}</div>
                    <div style="color:#9ca3af; font-size:0.85rem; margin-top:0.15rem">
                        Entry: <code>${t.entry_price:.2f}</code> ·
                        Aktuell: <code>${current_price:.2f}</code> ·
                        SL: <code>${t.stop_loss:.2f}</code> ·
                        TP: <code>${t.take_profit:.2f}</code>
                    </div>
                    <div class="kpi-value {pnl_color}" style="font-size:1rem; margin-top:0.15rem">
                        G/V: {sign}${unrealized_pnl:.2f} ({sign}{unrealized_pct:.1f}%)
                    </div>
                </div>""", unsafe_allow_html=True)


# ══════════════════════════════════════════════════════════════════
# TAB 2: TRADE-LOG
# ══════════════════════════════════════════════════════════════════
with tab2:
    st.markdown('<div class="section-label">Alle Trades</div>', unsafe_allow_html=True)

    with get_session() as session:
        # Fix (Multi-Tenant-Datenleck, 2026-08-11, siehe TAB-4-Fix oben):
        # war ebenfalls ungefiltert, zeigte das komplette Trade-Log ALLER
        # verbundenen Nutzer statt nur Daniels eigenes.
        all_trades = session.query(Trade).filter(
            Trade.user_id == DEFAULT_USER_ID
        ).order_by(Trade.created_at.desc()).all()

    if not all_trades:
        st.info("Noch keine Trades. Der Bot startet täglich um 09:00 ET.")
    else:
        for t in all_trades:
            with st.expander(
                f"{t.ticker} · Score {t.rule_score}/100 · "
                f"{t.created_at.strftime('%d.%m.%Y %H:%M')} · "
                f"{'P&L: ' + fmt_pct(t.pnl_pct) if t.pnl_pct else 'OFFEN'}",
                expanded=False
            ):
                col1, col2, col3 = st.columns(3)
                with col1:
                    st.markdown(f"**Ticker:** {t.ticker}")
                    st.markdown(f"**Typ:** {t.instrument_type}")
                    st.markdown(f"**Status:** {status_badge(t.status)}", unsafe_allow_html=True)
                    st.markdown(f"**Modus:** {mode_badge(t.mode)}", unsafe_allow_html=True)
                with col2:
                    entry_label = f"${t.entry_price:.2f}" if t.entry_price is not None else "wartet auf Fill"
                    st.markdown(f"**Entry:** `{entry_label}`")
                    st.markdown(f"**Stop Loss:** `${t.stop_loss:.2f}`")
                    st.markdown(f"**Take Profit:** `${t.take_profit:.2f}`")
                    st.markdown(f"**Menge:** `{t.quantity}`")
                with col3:
                    st.markdown(f"**Rule Score:** `{t.rule_score}/100`")
                    if t.llm_sentiment:
                        st.markdown(f"**LLM Sentiment:** `{t.llm_sentiment}/10`")
                    if t.pnl_usd is not None:
                        pnl_color = "🟢" if t.pnl_usd >= 0 else "🔴"
                        st.markdown(f"**P&L:** {pnl_color} `${t.pnl_usd:.2f}` ({fmt_pct(t.pnl_pct)})")

                # LLM-Analyse
                if t.llm_summary:
                    st.markdown("---")
                    st.markdown("**🧠 KI-Analyse:**")
                    st.markdown(f"> {t.llm_summary}")
                    risks = t.get_llm_risks()
                    if risks:
                        st.markdown("**⚠️ Erkannte Risiken:**")
                        for r in risks:
                            st.markdown(f"- {r}")


# ══════════════════════════════════════════════════════════════════
# TAB 3: SIGNAL-ANALYSE (Live Ticker-Check)
# ══════════════════════════════════════════════════════════════════
with tab3:
    st.markdown('<div class="section-label">Live Signal-Analyse</div>', unsafe_allow_html=True)
    st.markdown("Gib einen Ticker ein um sofort den Rule-Engine-Score zu berechnen.")

    col_input, col_btn = st.columns([3, 1])
    with col_input:
        ticker_input = st.text_input("Ticker-Symbol", value="AAPL", label_visibility="collapsed")
    with col_btn:
        analyze_btn = st.button("Analysieren", use_container_width=True)

    if analyze_btn and ticker_input:
        with st.spinner(f"Analysiere {ticker_input.upper()}..."):
            try:
                result = analyze_ticker(ticker_input.upper())

                # Score-Anzeige
                score_color = "#34d399" if result.score >= MIN_SIGNAL_SCORE else "#f87171"
                status_text = "✅ FREIGEGEBEN" if result.approved else "❌ UNTER LIMIT"
                if result.ko_reason:
                    status_text = f"🚫 KO: {result.ko_reason}"
                    # Fair Value ist kein ethischer Ausschluss wie die Blacklist,
                    # daher Orange statt Rot (siehe Fair-Value-Gatekeeper).
                    if result.ko_reason.startswith("Fair Value"):
                        score_color = "#fb923c"

                st.markdown(f"""
                <div class="kpi-card" style="margin-top:1rem">
                    <div class="kpi-label">{result.ticker} · {result.instrument_type}</div>
                    <div style="display:flex; align-items:baseline; gap:1rem">
                        <div class="kpi-value" style="color:{score_color}">{result.score}<span style="font-size:1rem; color:#6b7280">/100</span></div>
                        <div style="color:{score_color}; font-weight:600">{status_text}</div>
                    </div>
                    <div class="score-bar-bg"><div class="score-bar-fill" style="width:{result.score}%; background:{'linear-gradient(90deg,#3b82f6,#34d399)' if result.approved else 'linear-gradient(90deg,#7f1d1d,#f87171)'}"></div></div>
                </div>""", unsafe_allow_html=True)

                # Score-Breakdown
                st.markdown("**Score-Aufschlüsselung:**")
                breakdown_data = []
                for key, val in result.score_breakdown.items():
                    breakdown_data.append({
                        "Kriterium": key.replace("_", " ").title(),
                        "Score": f"{val['score']}/{val['max']}",
                        "Wert": str(val.get("value", "—"))
                    })
                st.dataframe(pd.DataFrame(breakdown_data), use_container_width=True, hide_index=True)

                # Preise
                if result.current_price:
                    col_p1, col_p2, col_p3 = st.columns(3)
                    col_p1.metric("Aktueller Preis", f"${result.current_price:.2f}")
                    col_p2.metric("Stop Loss", f"${result.stop_loss:.2f}", f"-{STOP_LOSS_PCT:.0%}")
                    col_p3.metric("Take Profit", f"${result.take_profit:.2f}", f"+{TAKE_PROFIT_PCT:.0%}")

            except Exception as e:
                st.error(f"Fehler bei der Analyse: {e}")

    # Fair Value Übersicht (Stufe-1-Gatekeeper, siehe fair_value.py) – alle
    # aktuell unterbewerteten Ticker aus dem wöchentlichen Cache, höchster
    # Rabatt zuerst.
    st.markdown("---")
    st.markdown('<div class="section-label">Fair Value Übersicht</div>', unsafe_allow_html=True)
    st.markdown("Ticker, die den Stufe-1-Gatekeeper aktuell bestehen (≥10% unter Fair Value).")

    undervalued = get_undervalued_tickers()
    if undervalued:
        fv_data = [{
            "Ticker": ticker,
            "Kurs": f"${price:.2f}" if price else "—",
            "Fair Value": f"${fv_avg:.2f}" if fv_avg else "—",
            "Rabatt %": f"{discount:.1f}%",
            "Value Trap": risk,
        } for ticker, discount, fv_avg, price, risk in undervalued]
        st.dataframe(pd.DataFrame(fv_data), use_container_width=True, hide_index=True)
    else:
        st.info("Aktuell keine unterbewerteten Ticker im Cache (oder noch kein Fair-Value-Update gelaufen).")

    # Watchlist-Übersicht
    st.markdown("---")
    st.markdown("**Aktive Watchlists:**")
    col_w1, col_w2 = st.columns(2)
    with col_w1:
        st.markdown("**Long-Kandidaten:**")
        st.markdown(" · ".join([f"`{t}`" for t in LONG_WATCHLIST]))
    with col_w2:
        st.markdown("**Inverse ETFs (Bearish):**")
        st.markdown(" · ".join([f"`{t}`" for t in ACTIVE_SHORT_INSTRUMENTS]))


# ══════════════════════════════════════════════════════════════════
# TAB 4: PERFORMANCE-CHART
# ══════════════════════════════════════════════════════════════════
with tab4:
    st.markdown('<div class="section-label">Portfolio-Entwicklung</div>', unsafe_allow_html=True)

    with get_session() as session:
        # Fix (Multi-Tenant-Datenleck, gefunden bei der daily_log-Analyse
        # 2026-08-11): beide Queries liefen bisher OHNE user_id-Filter, im
        # Gegensatz zu jeder anderen Query in diesem Dashboard (get_open_
        # trades/get_total_pnl/etc. defaulten alle implizit auf
        # DEFAULT_USER_ID = Daniel, siehe deren Docstrings). Seit dem
        # Multi-Tenant-Handelsloop (2026-07-30) können mehrere Nutzer eigene
        # Trades/daily_log-Snapshots haben – ungefiltert landeten hier ALLE
        # Nutzer gemischt in Daniels privatem Admin-Dashboard (Streamlit,
        # kein Auth/Session-Konzept, daher explizit auf DEFAULT_USER_ID
        # verdrahtet statt auf einen "aktuellen Nutzer").
        snapshots = session.query(DailyLog).filter(
            DailyLog.user_id == DEFAULT_USER_ID
        ).order_by(DailyLog.log_date.asc()).all()
        closed_trades = session.query(Trade).filter(
            Trade.status.in_(CLOSED_STATUSES), Trade.user_id == DEFAULT_USER_ID
        ).all()

    if not snapshots:
        st.info("Noch keine Performance-Daten. Diese werden täglich gespeichert.")

        # Beispiel-Chart mit Startkapital
        st.markdown("**Vorschau:** So sieht der Chart aus sobald der Bot läuft:")
        demo_df = pd.DataFrame({
            "Datum": pd.date_range("2024-01-01", periods=10),
            "Portfolio ($)": [500, 498, 505, 512, 508, 519, 525, 531, 527, 540]
        })
        st.line_chart(demo_df.set_index("Datum"))
    else:
        df = pd.DataFrame([{
            "Datum": s.log_date,
            "Portfolio ($)": s.portfolio_value,
            "Trades": s.trades_count
        } for s in snapshots])
        st.line_chart(df.set_index("Datum")[["Portfolio ($)"]])

        # Trade-Statistiken
        if closed_trades:
            st.markdown("---")
            st.markdown("**Trade-Statistiken:**")
            wins  = [t for t in closed_trades if t.pnl_usd and t.pnl_usd > 0]
            total = len(closed_trades)
            col_s1, col_s2, col_s3, col_s4 = st.columns(4)
            col_s1.metric("Trades gesamt", total)
            col_s2.metric("Gewinner", len(wins))
            col_s3.metric("Trefferquote", f"{len(wins)/total*100:.0f}%" if total else "—")
            total_pnl_sum = sum(t.pnl_usd for t in closed_trades if t.pnl_usd)
            col_s4.metric("Gesamt P&L", f"${total_pnl_sum:.2f}")

        # Performance-Vergleich vs. Benchmarks (siehe Feature Benchmark-Vergleich)
        st.markdown("---")
        st.markdown("**Performance-Vergleich (30 Tage):**")
        bot_perf = get_bot_performance(days=30)
        if bot_perf is None:
            st.info("Noch nicht genug Historie für einen 30-Tage-Vergleich.")
        else:
            benchmark_perf = get_benchmark_performance(days=30)
            rows = [("Dein Bot", bot_perf)] + list(benchmark_perf.items())
            max_abs = max((abs(v) for _, v in rows if v is not None), default=1) or 1

            for label, pct in rows:
                if pct is None:
                    st.markdown(f"{label}: N/A")
                    continue
                if label == "Dein Bot":
                    cls = "positive" if pct > 0 else ("negative" if pct < 0 else "neutral")
                else:
                    cls = "positive" if bot_perf > pct else ("negative" if bot_perf < pct else "neutral")
                bar_len = max(1, int(round(abs(pct) / max_abs * 20))) if pct != 0 else 0
                bar = "█" * bar_len
                st.markdown(
                    f'<p style="margin:0.15rem 0">{label:<10} '
                    f'<span class="kpi-value {cls}" style="font-size:1rem">{pct:+.2f}%</span> '
                    f'<span class="kpi-value {cls}" style="font-size:1rem; letter-spacing:-1px">{bar}</span></p>',
                    unsafe_allow_html=True
                )


# ══════════════════════════════════════════════════════════════════
# TAB 5: BACKLOOK (Wöchentliches Selbstlernen der Score-Gewichtungen)
# ══════════════════════════════════════════════════════════════════
with tab5:
    st.markdown('<div class="section-label">Wöchentlicher Backlook</div>', unsafe_allow_html=True)
    st.markdown(
        "Jeden Montag 06:00 ET wertet der Bot die abgeschlossenen Trades der "
        "letzten Woche aus und passt die Score-Gewichtungen minimal an "
        "(max. ±2 Punkte pro Kriterium, Summe bleibt immer 100)."
    )

    with get_session() as session:
        history = session.query(WeightHistory).order_by(WeightHistory.run_at.asc()).all()
        current_weights = get_active_weights(session)

    if not history:
        st.info("Noch kein Backlook-Lauf mit ausreichend Trades (min. 5) protokolliert.")
        st.markdown("**Aktuelle Gewichtungen (Startwerte aus config.py):**")
        st.dataframe(
            pd.DataFrame(
                [{"Kriterium": k.replace("_", " ").title(), "Gewichtung": v} for k, v in current_weights.items()]
            ),
            use_container_width=True, hide_index=True
        )
    else:
        # Letzter Lauf (alle Zeilen mit dem jüngsten run_at)
        latest_run_at = max(h.run_at for h in history)
        latest_rows = [h for h in history if h.run_at == latest_run_at]

        st.markdown(f"**Letzte Anpassung:** {latest_run_at.strftime('%d.%m.%Y %H:%M')} UTC "
                    f"· Basis: {latest_rows[0].trades_analyzed} abgeschlossene Trades")

        table_data = [{
            "Kriterium": h.criterion.replace("_", " ").title(),
            "Alte Gewichtung": h.old_weight,
            "Neue Gewichtung": h.new_weight,
            "Änderung": f"{h.change:+d}",
        } for h in sorted(latest_rows, key=lambda h: h.criterion)]
        st.dataframe(pd.DataFrame(table_data), use_container_width=True, hide_index=True)

        st.markdown("---")
        st.markdown("**Gewichtungsentwicklung über Zeit:**")

        chart_df = pd.DataFrame([{
            "Datum": h.run_at,
            "Kriterium": h.criterion.replace("_", " ").title(),
            "Gewichtung": h.new_weight,
        } for h in history])
        pivot_df = chart_df.pivot_table(index="Datum", columns="Kriterium", values="Gewichtung", aggfunc="last")
        st.line_chart(pivot_df)

    st.markdown("---")
    st.markdown("**Aktuell aktive Gewichtungen:**")
    active_cols = st.columns(len(current_weights))
    for col, (criterion, weight) in zip(active_cols, current_weights.items()):
        col.metric(criterion.replace("_", " ").title(), f"{weight}")

    # ══════════════════════════════════════════════════════════════
    # SCHWELLENWERT-WIRKSAMKEIT (siehe post_exit_tracking.py)
    # ══════════════════════════════════════════════════════════════
    st.markdown("---")
    st.markdown('<div class="section-label">Schwellenwert-Wirksamkeit</div>', unsafe_allow_html=True)
    st.markdown(
        "Verfolgt den Kursverlauf für 10 Handelstage NACH einem Time-Exit "
        "(Holding-Days-Grenze) weiter – ohne neuen Trade, rein zur Beobachtung. "
        "Zeigt, ob die Grenze profitable Positionen systematisch zu früh kappt."
    )

    with get_session() as session:
        stats_month = analyze_threshold_effectiveness(session, "MAX_HOLDING_DAYS", lookback_days=30)
        stats_week = analyze_threshold_effectiveness(session, "MAX_HOLDING_DAYS", lookback_days=7)
        tracking_rows = session.query(PostExitTracking).filter(
            PostExitTracking.parameter == "MAX_HOLDING_DAYS"
        ).order_by(PostExitTracking.exit_date.desc()).limit(50).all()
        tracking_data = [{
            "Ticker": r.ticker,
            "Exit-Grund": r.exit_reason,
            "Exit-Datum": r.exit_date.strftime("%d.%m.%Y") if r.exit_date else None,
            "Exit-Preis": r.exit_price,
            "PnL% bei Exit": r.pnl_pct_at_exit,
            "Kurs +5 Tage": r.price_after_5_days,
            "Kurs +10 Tage": r.price_after_10_days,
            "PnL% +10 Tage": r.pnl_pct_after_10_days,
            "Mehr Gewinn möglich?": (
                "—" if r.would_have_more_profit is None
                else ("Ja" if r.would_have_more_profit else "Nein")
            ),
            "Entgangen/Vermieden %": r.forgone_profit_pct,
        } for r in tracking_rows]

    if not stats_month:
        st.info("Noch keine ausgewerteten Time-Exits (post_exit_tracking) – Daten füllen sich täglich (05:00 ET) nach.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Ausgewertete Time-Exits (30T)", stats_month["n"])
        m2.metric("Hätten mehr gebracht", f"{stats_month['better_count']} ({stats_month['better_pct']:.0f}%)")
        m3.metric("Ø entgangener Gewinn", f"{stats_month['avg_forgone_pct']:+.1f}%" if stats_month["avg_forgone_pct"] is not None else "–")
        m4.metric("Ø vermiedener Verlust", f"{stats_month['avg_avoided_pct']:+.1f}%" if stats_month["avg_avoided_pct"] is not None else "–")

        if stats_month["n"] >= 10 and stats_month["better_pct"] >= 70.0:
            st.warning(
                f"⚠️ Klares Muster (30 Tage, {stats_month['n']} Time-Exits): "
                f"{stats_month['better_pct']:.0f}% hätten bei längerem Halten mehr Gewinn gebracht "
                f"(Ø {stats_month['avg_forgone_pct']:+.1f}%, Median {stats_month['median_forgone_pct']:+.1f}%). "
                f"Die Holding-Days-Grenze schneidet Gewinner ggf. systematisch zu früh ab. "
                f"Keine automatische Änderung – dieselbe Einschätzung erscheint auch als Lernvorschlag "
                f"unter 'Einstellungen'."
            )

        if stats_week:
            st.caption(
                f"Letzte 7 Tage: {stats_week['n']} ausgewertete Time-Exits, "
                f"{stats_week['better_count']} hätten mehr gebracht ({stats_week['better_pct']:.0f}%)."
            )

    if tracking_data:
        st.markdown("**Einzelne Time-Exits (neueste zuerst):**")
        st.dataframe(pd.DataFrame(tracking_data), use_container_width=True, hide_index=True)
