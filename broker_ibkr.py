"""
broker_ibkr.py – IBKRBroker: BrokerInterface-Implementierung für Interactive
Brokers via ib_insync. Benötigt TWS oder IB Gateway als lokal erreichbaren
Prozess (siehe README.md "IBKR Setup") – anders als AlpacaBroker also KEIN
reiner REST-Client, sondern eine aktive Socket-Verbindung, die vor jedem
Aufruf sichergestellt wird (siehe connect()).
"""

from datetime import datetime

import pytz
import yfinance as yf
from ib_insync import IB, Stock, MarketOrder

from broker_interface import BrokerInterface, OrderResult, AccountInfo, Position


class IBKRBroker(BrokerInterface):

    def __init__(self, host="127.0.0.1", port=7497, client_id=1):
        # TWS/Gateway läuft lokal auf dem Client.
        # Port: 7497=TWS Paper, 7496=TWS Live, 4002=Gateway Paper, 4001=Gateway Live
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = IB()
        self.broker_name = "ibkr"

    def connect(self):
        if not self.ib.isConnected():
            self.ib.connect(self.host, self.port, clientId=self.client_id)
            print(f"✅ IBKR verbunden: {self.host}:{self.port}")

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    def _get_contract(self, ticker: str):
        # Exchange automatisch anhand des Ticker-Suffix ermitteln.
        # US-Aktien: SMART (routet automatisch NYSE/NASDAQ/...)
        # EU-Aktien: XETRA, LSE, EURONEXT (siehe config.IBKR_EU_WATCHLIST)
        exchange = "SMART"
        currency = "USD"

        if ticker.endswith(".DE"):
            ticker_clean = ticker.replace(".DE", "")
            exchange = "XETRA"
            currency = "EUR"
        elif ticker.endswith(".L"):
            ticker_clean = ticker.replace(".L", "")
            exchange = "LSE"
            currency = "GBP"
        elif ticker.endswith(".PA"):
            ticker_clean = ticker.replace(".PA", "")
            exchange = "EURONEXT"
            currency = "EUR"
        else:
            ticker_clean = ticker

        contract = Stock(ticker_clean, exchange, currency)
        self.ib.qualifyContracts(contract)
        return contract

    def get_account(self) -> AccountInfo:
        self.connect()
        summary = self.ib.accountSummary()

        cash = 0.0
        portfolio_value = 0.0
        currency = "EUR"

        for item in summary:
            if item.tag == "TotalCashValue" and item.currency == "BASE":
                cash = float(item.value)
            if item.tag == "NetLiquidation" and item.currency == "BASE":
                portfolio_value = float(item.value)
            if item.tag == "Currency":
                currency = item.value

        return AccountInfo(
            cash=cash,
            buying_power=cash,
            portfolio_value=portfolio_value,
            currency=currency,
            broker="ibkr",
        )

    def get_positions(self) -> list[Position]:
        self.connect()
        positions = self.ib.positions()
        result = []

        for p in positions:
            ticker = p.contract.symbol
            if p.contract.exchange == "XETRA":
                ticker += ".DE"
            elif p.contract.exchange == "LSE":
                ticker += ".L"

            result.append(Position(
                ticker=ticker,
                quantity=float(p.position),
                avg_entry_price=float(p.avgCost),
                current_price=0.0,  # Wird separat via get_current_price geladen
                unrealized_pnl=0.0,
                broker="ibkr",
            ))

        return result

    def place_market_order(self, ticker, quantity, side,
                            stop_loss=None, take_profit=None) -> OrderResult:
        self.connect()
        contract = self._get_contract(ticker)
        action = "BUY" if side == "buy" else "SELL"

        if stop_loss and take_profit and quantity >= 1:
            # Bracket Order (Parent Market-Order + SL/TP-Kinder)
            bracket = self.ib.bracketOrder(
                action=action,
                quantity=quantity,
                limitPrice=None,  # Market
                takeProfitPrice=take_profit,
                stopLossPrice=stop_loss,
            )
            trades = [self.ib.placeOrder(contract, order) for order in bracket]

            self.ib.sleep(2)  # kurz auf Fill-Status der Parent-Order warten
            parent_trade = trades[0]

            return OrderResult(
                order_id=str(parent_trade.order.orderId),
                ticker=ticker,
                quantity=quantity,
                filled_price=float(parent_trade.orderStatus.avgFillPrice or 0),
                status=parent_trade.orderStatus.status,
                broker="ibkr",
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
        else:
            # Simple Market Order (Fractional oder ohne SL/TP)
            order = MarketOrder(action, quantity)
            trade = self.ib.placeOrder(contract, order)
            self.ib.sleep(2)

            return OrderResult(
                order_id=str(trade.order.orderId),
                ticker=ticker,
                quantity=quantity,
                filled_price=float(trade.orderStatus.avgFillPrice or 0),
                status=trade.orderStatus.status,
                broker="ibkr",
            )

    def cancel_order(self, order_id: str) -> bool:
        self.connect()
        try:
            for order in self.ib.openOrders():
                if str(order.orderId) == order_id:
                    self.ib.cancelOrder(order)
                    return True
            return False
        except Exception:
            return False

    def get_current_price(self, ticker: str) -> float:
        try:
            return float(yf.Ticker(ticker).fast_info.get("lastPrice", 0))
        except Exception:
            return 0.0

    def is_market_open(self) -> bool:
        # Prüft ob US- ODER EU-Markt offen ist (IBKR-Watchlist deckt beide ab).
        now_et = datetime.now(pytz.timezone("America/New_York"))
        now_cet = datetime.now(pytz.timezone("Europe/Berlin"))

        us_open = (now_et.weekday() < 5 and
                   9 * 60 + 30 <= now_et.hour * 60 + now_et.minute <= 16 * 60)

        eu_open = (now_cet.weekday() < 5 and
                   9 * 60 <= now_cet.hour * 60 + now_cet.minute <= 17 * 60 + 30)

        return us_open or eu_open
