"""
broker_interface.py – Broker-Abstraktion (Alpaca/IBKR).
Definiert die gemeinsame Schnittstelle, die broker_alpaca.AlpacaBroker und
broker_ibkr.IBKRBroker implementieren – siehe broker.get_broker() für die
Auswahl anhand von bot_config.ACTIVE_BROKER.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    order_id: str
    ticker: str
    quantity: float
    filled_price: float
    status: str
    broker: str  # "alpaca" / "ibkr"
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None


@dataclass
class AccountInfo:
    cash: float
    buying_power: float
    portfolio_value: float
    currency: str
    broker: str


@dataclass
class Position:
    ticker: str
    quantity: float
    avg_entry_price: float
    current_price: float
    unrealized_pnl: float
    broker: str


class BrokerInterface(ABC):
    """Gemeinsame Schnittstelle aller Broker-Implementierungen."""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        pass

    @abstractmethod
    def place_market_order(
        self,
        ticker: str,
        quantity: float,
        side: str,  # "buy" / "sell"
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> OrderResult:
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        pass

    @abstractmethod
    def get_current_price(self, ticker: str) -> float:
        pass

    @abstractmethod
    def is_market_open(self) -> bool:
        pass
