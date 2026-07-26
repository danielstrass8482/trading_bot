"""
broker_alpaca.py – AlpacaBroker: BrokerInterface-Implementierung für Alpaca
Markets. Wrapt dieselbe alpaca_trade_api-Logik, die broker.py bereits für
Guardrail-Enforcement/DB-Logging nutzt (siehe broker._get_alpaca_client,
broker.place_trade) – hier aber als generischer BrokerInterface-Client ohne
Guardrails/DB-Anbindung, für den Broker-agnostischen Zugriff via
broker.get_broker().
"""

import alpaca_trade_api as tradeapi
import yfinance as yf

from broker_interface import BrokerInterface, OrderResult, AccountInfo, Position
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL


class AlpacaBroker(BrokerInterface):

    def __init__(self, api_key=None, secret_key=None, base_url=None, client=None):
        # client: optionaler, bereits fertig gebauter tradeapi.REST-Client
        # (siehe database.get_alpaca_api_for_user, das für Multi-Tenant-Nutzer
        # direkt einen Client statt einzelner Keys zurückgibt).
        self.api = client or tradeapi.REST(
            api_key or ALPACA_API_KEY,
            secret_key or ALPACA_SECRET_KEY,
            base_url or ALPACA_BASE_URL,
        )
        self.broker_name = "alpaca"

    def get_account(self) -> AccountInfo:
        acc = self.api.get_account()
        return AccountInfo(
            cash=float(acc.cash),
            buying_power=float(acc.buying_power),
            portfolio_value=float(acc.portfolio_value),
            currency="USD",
            broker="alpaca",
        )

    def get_positions(self) -> list[Position]:
        positions = self.api.list_positions()
        return [Position(
            ticker=p.symbol,
            quantity=float(p.qty),
            avg_entry_price=float(p.avg_entry_price),
            current_price=float(p.current_price),
            unrealized_pnl=float(p.unrealized_pl),
            broker="alpaca",
        ) for p in positions]

    def place_market_order(self, ticker, quantity, side,
                            stop_loss=None, take_profit=None) -> OrderResult:
        # Bracket-Order wenn ganze Aktie + SL/TP vorhanden, sonst Simple Order
        # – Alpaca erlaubt bei Fractional Shares keine Bracket-/Stop-Orders
        # ("fractional orders must be simple orders", siehe broker.place_trade).
        is_fractional = quantity != int(quantity)

        if is_fractional or not stop_loss:
            order = self.api.submit_order(
                symbol=ticker,
                qty=round(quantity, 6),
                side=side,
                type="market",
                time_in_force="day",
            )
        else:
            order = self.api.submit_order(
                symbol=ticker,
                qty=int(quantity),
                side=side,
                type="market",
                time_in_force="day",
                order_class="bracket",
                stop_loss={"stop_price": stop_loss},
                take_profit={"limit_price": take_profit},
            )

        return OrderResult(
            order_id=order.id,
            ticker=ticker,
            quantity=quantity,
            filled_price=float(order.filled_avg_price or 0),
            status=order.status,
            broker="alpaca",
            stop_loss=stop_loss,
            take_profit=take_profit,
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.api.cancel_order(order_id)
            return True
        except Exception:
            return False

    def get_current_price(self, ticker: str) -> float:
        try:
            return float(yf.Ticker(ticker).fast_info.get("lastPrice", 0))
        except Exception:
            return 0.0

    def is_market_open(self) -> bool:
        clock = self.api.get_clock()
        return clock.is_open
