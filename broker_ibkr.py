"""
broker_ibkr.py – IBKR Web API (REST) Broker-Implementierung.
Ersetzt die frühere ib_insync + IB Gateway Anbindung: keine lokale
Gateway-Instanz mehr nötig, Authentifizierung per Username/Passwort
(Individual Client) direkt gegen https://api.ibkr.com/v1/api/.
Dokumentation: https://ibkrcampus.com/docs/web-api/
"""

import os
import time
from datetime import datetime
from typing import Optional

import pytz
import requests
import yfinance as yf

from broker_interface import (BrokerInterface, OrderResult,
                               AccountInfo, Position)


class IBKRWebBroker(BrokerInterface):
    """
    IBKR Web API Broker Implementation
    Dokumentation: https://ibkrcampus.com/docs/web-api/
    Authentifizierung: Username + Passwort (Individual Account)
    """

    BASE_URL = "https://api.ibkr.com/v1/api"

    def __init__(self):
        self.username = os.getenv("IBKR_USERNAME", "")
        self.password = os.getenv("IBKR_PASSWORD", "")
        self.account_id = os.getenv("IBKR_ACCOUNT_ID", "")
        self.session = requests.Session()
        self.session.verify = True
        self._authenticated = False
        self.broker_name = "ibkr"

    def _authenticate(self) -> bool:
        """Login bei IBKR Web API"""
        try:
            resp = self.session.post(
                f"{self.BASE_URL}/iserver/auth/ssodh/init",
                json={
                    "compete": True,
                    "publish": True
                },
                timeout=30
            )

            if resp.status_code == 200:
                self._authenticated = True
                print("✅ IBKR Web API: Authentifiziert")
                return True
            else:
                print(f"❌ IBKR Auth fehlgeschlagen: {resp.status_code}")
                print(resp.text)
                return False
        except Exception as e:
            print(f"❌ IBKR Auth Error: {e}")
            return False

    def _ensure_auth(self):
        """Stellt sicher dass Session aktiv ist"""
        # Auth-Status prüfen
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/iserver/auth/status",
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("authenticated"):
                    return True
        except Exception:
            pass

        # Neu authentifizieren
        return self._authenticate()

    def _get(self, endpoint: str) -> dict:
        """GET Request gegen IBKR API"""
        self._ensure_auth()
        resp = self.session.get(
            f"{self.BASE_URL}{endpoint}",
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, endpoint: str, data: dict) -> dict:
        """POST Request gegen IBKR API"""
        self._ensure_auth()
        resp = self.session.post(
            f"{self.BASE_URL}{endpoint}",
            json=data,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()

    def _get_account_id(self) -> str:
        """Account ID ermitteln"""
        if self.account_id:
            return self.account_id
        accounts = self._get("/iserver/accounts")
        if accounts and "accounts" in accounts:
            self.account_id = accounts["accounts"][0]
        return self.account_id

    def _get_conid(self, ticker: str) -> Optional[int]:
        """
        Contract ID für Ticker ermitteln
        IBKR verwendet Contract IDs statt Ticker-Symbole
        """
        # Exchange aus Ticker-Suffix ermitteln
        exchange = "SMART"
        currency = "USD"
        clean_ticker = ticker

        if ticker.endswith(".DE"):
            clean_ticker = ticker.replace(".DE", "")
            exchange = "XETRA"
            currency = "EUR"
        elif ticker.endswith(".L"):
            clean_ticker = ticker.replace(".L", "")
            exchange = "LSE"
            currency = "GBP"
        elif ticker.endswith(".PA"):
            clean_ticker = ticker.replace(".PA", "")
            exchange = "EURONEXT"
            currency = "EUR"

        try:
            result = self._get(
                f"/iserver/secdef/search?symbol={clean_ticker}"
                f"&secType=STK&exchange={exchange}"
            )

            if result and len(result) > 0:
                # Ersten passenden Contract nehmen
                for contract in result:
                    if contract.get("currency") == currency:
                        return contract.get("conid")
                # Fallback: ersten nehmen
                return result[0].get("conid")
        except Exception as e:
            print(f"ConID Fehler für {ticker}: {e}")

        return None

    def get_account(self) -> AccountInfo:
        account_id = self._get_account_id()

        # Portfolio Summary
        summary = self._get(
            f"/portfolio/{account_id}/summary"
        )

        cash = 0.0
        portfolio_value = 0.0
        currency = "EUR"

        if "totalcashvalue" in summary:
            cash = float(summary["totalcashvalue"].get("amount", 0))
            currency = summary["totalcashvalue"].get("currency", "EUR")

        if "netliquidation" in summary:
            portfolio_value = float(
                summary["netliquidation"].get("amount", 0))

        return AccountInfo(
            cash=cash,
            buying_power=cash,
            portfolio_value=portfolio_value,
            currency=currency,
            broker="ibkr"
        )

    def get_positions(self) -> list[Position]:
        account_id = self._get_account_id()
        positions_data = self._get(
            f"/portfolio/{account_id}/positions/0"
        )

        result = []
        for pos in positions_data:
            ticker = pos.get("ticker", "")
            exchange = pos.get("listingExchange", "")

            # Ticker-Suffix hinzufügen
            if exchange == "XETRA":
                ticker += ".DE"
            elif exchange == "LSE":
                ticker += ".L"
            elif exchange in ["EURONEXT", "AEB"]:
                ticker += ".PA"

            result.append(Position(
                ticker=ticker,
                quantity=float(pos.get("position", 0)),
                avg_entry_price=float(pos.get("avgCost", 0)),
                current_price=float(pos.get("mktPrice", 0)),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                broker="ibkr"
            ))

        return result

    def place_market_order(self, ticker, quantity, side,
                            stop_loss=None, take_profit=None):
        account_id = self._get_account_id()
        conid = self._get_conid(ticker)

        if not conid:
            raise ValueError(f"Kein Contract für {ticker} gefunden")

        action = "BUY" if side == "buy" else "SELL"

        # Order aufbauen
        order = {
            "acctId": account_id,
            "conid": conid,
            "orderType": "MKT",
            "side": action,
            "quantity": quantity,
            "tif": "DAY",  # Time in Force: Day
        }

        # Bracket Order wenn SL/TP gesetzt und ganze Aktie
        if stop_loss and take_profit and quantity >= 1:
            order["orderType"] = "MKT"
            order["isSingleGroup"] = True

            bracket_orders = [
                order,
                {
                    "acctId": account_id,
                    "conid": conid,
                    "orderType": "STP",
                    "side": "SELL",
                    "quantity": quantity,
                    "tif": "GTC",
                    "price": stop_loss,
                    "isSingleGroup": True
                },
                {
                    "acctId": account_id,
                    "conid": conid,
                    "orderType": "LMT",
                    "side": "SELL",
                    "quantity": quantity,
                    "tif": "GTC",
                    "price": take_profit,
                    "isSingleGroup": True
                }
            ]

            result = self._post(
                f"/iserver/account/{account_id}/orders",
                {"orders": bracket_orders}
            )
        else:
            result = self._post(
                f"/iserver/account/{account_id}/orders",
                {"orders": [order]}
            )

        # Order Reply verarbeiten
        # IBKR sendet manchmal Bestätigungsfragen
        if isinstance(result, list) and len(result) > 0:
            first = result[0]

            # Wenn Bestätigungsfrage kommt
            if "id" in first and "message" in first:
                # Auto-bestätigen
                confirm_id = first["id"]
                self._post(
                    f"/iserver/reply/{confirm_id}",
                    {"confirmed": True}
                )
                time.sleep(1)
                # Nochmal versuchen
                result = self._post(
                    f"/iserver/account/{account_id}/orders",
                    {"orders": [order]}
                )

            if isinstance(result, list) and len(result) > 0:
                order_result = result[0]
                return OrderResult(
                    order_id=str(order_result.get("orderId", "")),
                    ticker=ticker,
                    quantity=quantity,
                    filled_price=0.0,  # Wird nach Fill aktualisiert
                    status=order_result.get("order_status", "submitted"),
                    broker="ibkr",
                    stop_loss=stop_loss,
                    take_profit=take_profit
                )

        raise ValueError(f"Unerwartete Order-Antwort: {result}")

    def cancel_order(self, order_id: str) -> bool:
        account_id = self._get_account_id()
        try:
            self._post(
                f"/iserver/account/{account_id}/order/{order_id}",
                {}
            )
            return True
        except Exception:
            return False

    def get_current_price(self, ticker: str) -> float:
        try:
            return float(
                yf.Ticker(ticker).fast_info.get("lastPrice", 0))
        except Exception:
            return 0.0

    def is_market_open(self) -> bool:
        now_et = datetime.now(pytz.timezone("America/New_York"))
        now_cet = datetime.now(pytz.timezone("Europe/Berlin"))

        us_open = (now_et.weekday() < 5 and
                   9 * 60 + 30 <= now_et.hour * 60 + now_et.minute <= 16 * 60)
        eu_open = (now_cet.weekday() < 5 and
                   9 * 60 <= now_cet.hour * 60 + now_cet.minute <= 17 * 60 + 30)

        return us_open or eu_open


# IBKRWebBroker als Standard verwenden
IBKRBroker = IBKRWebBroker
