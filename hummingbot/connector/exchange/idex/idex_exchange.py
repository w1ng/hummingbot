import logging
import math
import time
import asyncio
import aiohttp

from decimal import Decimal
from typing import Optional, List, Dict, Any, AsyncIterable
from async_timeout import timeout

from hummingbot.connector.exchange_base import ExchangeBase, s_decimal_NaN
from hummingbot.connector.trading_rule import TradingRule
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.limit_order import LimitOrder
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.cancellation_result import CancellationResult
from hummingbot.core.event.events import (
    OrderType, OrderCancelledEvent, TradeType, TradeFee, MarketEvent, BuyOrderCreatedEvent, SellOrderCreatedEvent,
    MarketOrderFailureEvent, BuyOrderCompletedEvent, SellOrderCompletedEvent, OrderFilledEvent
)
from hummingbot.core.network_iterator import NetworkStatus
from hummingbot.core.utils.async_utils import safe_ensure_future, safe_gather
from hummingbot.core.utils.estimate_fee import estimate_fee

from hummingbot.connector.exchange.idex.idex_auth import IdexAuth, OrderTypeEnum, OrderSideEnum
from hummingbot.connector.exchange.idex.idex_in_flight_order import IdexInFlightOrder
from hummingbot.connector.exchange.idex.idex_order_book_tracker import IdexOrderBookTracker
from hummingbot.connector.exchange.idex.idex_user_stream_tracker import IdexUserStreamTracker
from hummingbot.connector.exchange.idex.idex_utils import (
    hb_order_type_to_idex_param, hb_trade_type_to_idex_param, EXCHANGE_NAME, get_new_client_order_id, DEBUG,
    GAS_EXTRA_FEES,
)
from hummingbot.connector.exchange.idex.idex_resolve import (
    get_idex_rest_url, set_domain, get_throttler, HTTP_PUBLIC_ENDPOINTS_LIMIT_ID,
    HTTP_USER_ENDPOINTS_LIMIT_ID
)
from hummingbot.core.utils import async_ttl_cache
from hummingbot.logger import HummingbotLogger

s_decimal_0 = Decimal("0.0")
ie_logger = None

NORMALIZED_PRECISION = 1e-08  # see Numbers & Precision at: https://docs.idex.io/#data-types


class IdexExchange(ExchangeBase):

    SHORT_POLL_INTERVAL = 11.0
    LONG_POLL_INTERVAL = 120.0
    UPDATE_ORDER_STATUS_MIN_INTERVAL = 45.0

    @classmethod
    def logger(cls) -> HummingbotLogger:
        global ie_logger
        if ie_logger is None:
            ie_logger = logging.getLogger(__name__)
        return ie_logger

    def __init__(self,
                 idex_api_key: str,
                 idex_api_secret_key: str,
                 idex_wallet_private_key: str,
                 trading_pairs: Optional[List[str]] = None,
                 trading_required: bool = True,
                 domain="matic"):
        """
        :param idex_com_api_key: The API key to connect to private idex.io APIs.
        :param idex_com_secret_key: The API secret.
        :param trading_pairs: The market trading pairs which to track order book data.
        :param trading_required: Whether actual trading is needed.
        """
        self._domain = domain
        set_domain(domain)
        super().__init__()
        self._trading_required = trading_required
        self._trading_pairs = trading_pairs
        self._idex_auth: IdexAuth = IdexAuth(idex_api_key, idex_api_secret_key, idex_wallet_private_key, domain=domain)
        self._account_available_balances = {}  # Dict[asset_name:str, Decimal]
        self._order_book_tracker = IdexOrderBookTracker(trading_pairs=trading_pairs, domain=domain)
        self._user_stream_tracker = IdexUserStreamTracker(self._idex_auth, trading_pairs, domain=domain)
        self._user_stream_tracker_task = None
        self._ev_loop = asyncio.get_event_loop()
        self._shared_client: Optional[aiohttp.ClientSession] = None
        self._poll_notifier = asyncio.Event()
        self._last_timestamp = 0
        self._in_flight_orders: Dict[str, IdexInFlightOrder] = {}  # Dict[client_order_id:str, idexComInFlightOrder]
        self._order_not_found_records = {}  # Dict[client_order_id:str, count:int]
        self._trading_rules = {}  # Dict[trading_pair:str, TradingRule]
        self._status_polling_task = None
        self._user_stream_event_listener_task = None
        self._trading_rules_polling_task = None
        self._last_poll_timestamp = 0
        self._exchange_info = None  # stores info about the exchange. Periodically polled from GET /v1/exchange
        self._market_info = None  # stores info about the markets. Periodically polled from GET /v1/markets
        self._assets_info = None  # stores ifo about assets. Periodically polled from GET /v1/assets
        self._order_lock = asyncio.Lock()  # exclusive access for modifying orders

    @property
    def trading_rules(self) -> Dict[str, TradingRule]:
        """Returns the trading rules associated with Idex orders/trades"""
        return self._trading_rules

    @property
    def name(self) -> str:
        """Returns the exchange name"""
        if self._domain == "matic":  # prod with MATIC blockchain
            return "idex"
        else:
            return f"idex_{self._domain}"

    @property
    def order_books(self) -> Dict[str, OrderBook]:
        """Returns the order books of all tracked trading pairs"""
        return self._order_book_tracker.order_books

    @property
    def status_dict(self) -> Dict[str, bool]:
        """
        A dictionary of statuses of various connector's components.
        """
        return {
            "order_books_initialized": self._order_book_tracker.ready,
            "account_balance": len(self._account_balances) > 0 if self._trading_required else True,
            "trading_rule_initialized": len(self._trading_rules) > 0,
            "user_stream_initialized":
                self._user_stream_tracker.data_source.last_recv_time > 0 if self._trading_required else True,
        }

    @property
    def ready(self) -> bool:
        """
        :return True when all statuses pass, this might take 5-10 seconds for all the connector's components and
        services to be ready.
        """
        return all(self.status_dict.values())

    @property
    def limit_orders(self) -> List[LimitOrder]:
        """Returns a list of active limit orders being tracked"""
        return [
            in_flight_order.to_limit_order()
            for in_flight_order in self._in_flight_orders.values()
        ]

    @property
    def in_flight_orders(self) -> Dict[str, IdexInFlightOrder]:
        """ Returns a list of all active orders being tracked """
        return self._in_flight_orders

    @property
    def tracking_states(self) -> Dict[str, any]:
        """
        :return active in-flight orders in json format, is used to save in sqlite db.
        """
        return {
            key: value.to_json()
            for key, value in self._in_flight_orders.items()
            if not value.is_done
        }

    async def _http_client(self) -> aiohttp.ClientSession:
        """
        :returns: Shared client session instance
        """
        if self._shared_client is None:
            self._shared_client = aiohttp.ClientSession()
        return self._shared_client

    def restore_tracking_states(self, saved_states: Dict[str, any]):
        """
        Restore in-flight orders from saved tracking states, this is so the connector can pick up on where it left off
        when it disconnects.
        :param saved_states: The saved tracking_states.
        """
        self._in_flight_orders.update({
            key: IdexInFlightOrder.from_json(value)
            for key, value in saved_states.items()
        })

    def supported_order_types(self) -> List[OrderType]:
        """
        :return a list of OrderType supported by this connector.
        Note that Market order type is no longer required and will not be used.
        """
        return [OrderType.LIMIT, OrderType.LIMIT_MAKER]

    def start(self, clock: Clock, timestamp: float):
        """
        This function is called automatically by the clock.
        """
        super().start(clock, timestamp)

    def stop(self, clock: Clock):
        """
        This function is called automatically by the clock.
        """
        super().stop(clock)

    def get_order_price_quantum(self, trading_pair: str, price: Decimal) -> Decimal:
        """Provides the Idex standard minimum price increment across all trading pairs"""
        trading_rule = self._trading_rules[trading_pair]
        return trading_rule.min_price_increment

    def get_order_size_quantum(self, trading_pair: str, order_size: Decimal) -> Decimal:
        """Provides the Idex standard minimum order increment across all trading pairs"""
        trading_rule = self._trading_rules[trading_pair]
        return Decimal(trading_rule.min_base_amount_increment)

    async def start_network(self):
        await self.stop_network()
        self._order_book_tracker.start()
        self._trading_rules_polling_task = safe_ensure_future(self._trading_rules_polling_loop())
        if self._trading_required:
            self._status_polling_task = safe_ensure_future(self._status_polling_loop())
            self._user_stream_tracker_task = safe_ensure_future(self._user_stream_tracker.start())
            self._user_stream_event_listener_task = safe_ensure_future(self._user_stream_event_listener())

    async def stop_network(self):
        self._order_book_tracker.stop()

        if self._status_polling_task is not None:
            self._status_polling_task.cancel()
        if self._trading_rules_polling_task is not None:
            self._trading_rules_polling_task.cancel()
        if self._user_stream_tracker_task is not None:
            self._user_stream_tracker_task.cancel()
        if self._user_stream_event_listener_task is not None:
            self._user_stream_event_listener_task.cancel()
        self._status_polling_task = self._trading_rules_polling_task = \
            self._user_stream_tracker_task = self._user_stream_event_listener_task = None

    async def check_network(self) -> NetworkStatus:
        """
        This function is required by NetworkIterator base class and is called periodically to check
        the network connection. Simply ping the network (or call any light weight public API).
        """
        try:
            await self.get_ping()
        except asyncio.CancelledError:
            raise
        except Exception:
            return NetworkStatus.NOT_CONNECTED
        return NetworkStatus.CONNECTED

    async def _trading_rules_polling_loop(self):
        """
        Periodically update trading rule.
        """
        while True:
            try:
                await self._update_trading_rules()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().network(f"Unexpected error while fetching trading rules. Error: {str(e)}",
                                      exc_info=True,
                                      app_warning_msg="Could not fetch new trading rules from Idex. "
                                                      "Check network connection.")
                await asyncio.sleep(0.5)

    async def _update_trading_rules(self):
        # load all data we depend on if missing. later _status_polling_loop() will periodically refresh them
        exchange_info = self._exchange_info if self._exchange_info else await self.get_exchange_info_from_api()
        market_info = self._market_info if self._market_info else await self.get_market_info_from_api()
        assets_info = self._assets_info if self._assets_info else await self.get_assets_from_api()
        # recompute trading rules
        self._trading_rules = self._format_trading_rules(exchange_info, market_info, assets_info)

    def _format_trading_rules(
            self, exchange_info: Dict[str, Any], market_info: List[Dict], assets_info: List[Dict]
    ) -> Dict[str, TradingRule]:
        """
        Converts json API response into a dictionary of trading rules.
        :param exchange_info: The json API responsen for exchange rules
        :param market_info: The json API response for trading pairs
        :return A dictionary of trading rules.
        Exchange Response Example:
        {
            "timeZone": "UTC",
            "serverTime": 1637440989597,
            "maticDepositContractAddress": "0x...",
            "maticCustodyContractAddress": "0x...",
            "maticUsdPrice": "1.64",
            "gasPrice": 4,
            "volume24hUsd": "1431.07",
            "totalVolumeUsd": "79631.43",
            "totalTrades": 76474,
            "totalValueLockedUsd": "24.58",
            "idexTokenAddress": "0x...",
            "idexUsdPrice": "0.39",
            "idexMarketCapUsd": "221192826.00",
            "makerFeeRate": "0.0010",
            "takerFeeRate": "0.0025",
            "takerIdexFeeRate": "0.0005",
            "takerLiquidityProviderFeeRate": "0.0020",
            "makerTradeMinimum": "1.00000000",
            "takerTradeMinimum": "0.10000000",
            "withdrawMinimum": "0.05000000",
            "liquidityAdditionMinimum": "0.05000000",
            "liquidityRemovalMinimum": "0.04000000",
            "blockConfirmationDelay": 15
        }

        Market Response Example:
        [
            {
                "market": "MATIC-USD",
                "type": "hybrid",
                "status": "activeHybrid",
                "baseAsset": "MATIC",
                "baseAssetPrecision": 8,
                "quoteAsset": "USD",
                "quoteAssetPrecision": 8,
                "makerFeeRate": "0.0010",
                "takerFeeRate": "0.0025",
                "takerIdexFeeRate": "0.0005",
                "takerLiquidityProviderFeeRate": "0.0020"
            },
            ...
        ]

        Assets Response Example:
        [
            {
                "name": "Ether",
                "symbol": "ETH",
                "contractAddress": "0x0000000000000000000000000000000000000000",
                "assetDecimals": 18,
                "exchangeDecimals": 8,
                "maticPrice": "152.67175572"
            },
            {
                "name": "USD Coin",
                "symbol": "USDC",
                "contractAddress": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                "assetDecimals": 6,
                "exchangeDecimals": 8,
                "maticPrice": "0.76335877"
            },
            ...
        ]
        """
        rules = {}
        price_step = Decimal(str(NORMALIZED_PRECISION))
        quantity_step = Decimal(str(NORMALIZED_PRECISION))
        minimum_order_size_matic = Decimal(str(exchange_info["makerTradeMinimum"]))
        asset_by_symbol = {asset['symbol']: asset for asset in assets_info}
        for t_pair in market_info:
            trading_pair = t_pair["market"]
            try:
                base_symbol = trading_pair.split('-')[0]
                base_matic_price = asset_by_symbol[base_symbol]['maticPrice']
                minimum_order_size = Decimal(minimum_order_size_matic) / Decimal(base_matic_price)
                rules[trading_pair] = TradingRule(trading_pair=trading_pair,
                                                  min_order_size=minimum_order_size,
                                                  min_price_increment=price_step,
                                                  min_base_amount_increment=quantity_step)
            except Exception:
                self.logger().error(f"Error parsing the exchange rules for {t_pair}. Skipping.", exc_info=True)
        return rules

    def buy(self, trading_pair: str, amount: Decimal, order_type=OrderType.MARKET,
            price: Decimal = s_decimal_NaN, **kwargs) -> str:
        """
        Buys an amount of base asset (of the given trading pair). This function returns immediately.
        To see an actual order, you'll have to wait for BuyOrderCreatedEvent.
        :param trading_pair: The market (e.g. BTC-USDT) to buy from
        :param amount: The amount in base token value
        :param order_type: The order type
        :param price: The price (note: this is no longer optional)
        :returns A new internal order id
        """
        order_id: str = get_new_client_order_id(True, trading_pair)
        safe_ensure_future(self._create_order(TradeType.BUY, order_id, trading_pair, amount, order_type, price))
        return order_id

    def sell(self, trading_pair: str, amount: Decimal, order_type=OrderType.MARKET,
             price: Decimal = s_decimal_NaN, **kwargs) -> str:
        """
        Sells an amount of base asset (of the given trading pair). This function returns immediately.
        To see an actual order, you'll have to wait for SellOrderCreatedEvent.
        :param trading_pair: The market (e.g. BTC-USDT) to sell from
        :param amount: The amount in base token value
        :param order_type: The order type
        :param price: The price (note: this is no longer optional)
        :returns A new internal order id
        """
        order_id: str = get_new_client_order_id(False, trading_pair)
        safe_ensure_future(self._create_order(TradeType.SELL, order_id, trading_pair, amount, order_type, price))
        return order_id

    def cancel(self, trading_pair: str, client_order_id: str):
        """
        Cancel an order. This function returns immediately.
        To get the cancellation result, you'll have to wait for OrderCancelledEvent.
        :param trading_pair: The market (e.g. BTC-USDT) of the order.
        :param client_order_id: The internal order id
        """
        order_cancellation = safe_ensure_future(self._execute_cancel(trading_pair, client_order_id))
        return order_cancellation

    async def _execute_cancel(self, trading_pair: str, client_order_id: str) -> str:
        """
        Executes order cancellation process by first calling cancel-order API. The API result doesn't confirm whether
        the cancellation is successful, it simply states it receives the request.
        :param trading_pair: The market trading pair
        :param client_order_id: The internal order id
        order.last_state to change to CANCELED
        """
        async with self._order_lock:
            try:
                tracked_order = self._in_flight_orders.get(client_order_id)
                if tracked_order is None:
                    raise IOError(f"Failed to cancel order - {client_order_id}: order not found.")
                exchange_order_id = tracked_order.exchange_order_id
                cancelled_id = await self.delete_order(trading_pair, client_order_id)
                if not cancelled_id:
                    if DEBUG:
                        self.logger().error(f'self.delete_order({trading_pair}, {client_order_id}) returned empty')
                    raise IOError(f"call to delete_order {client_order_id} returned empty: order not found")
                format_cancelled_id = (cancelled_id[0] or {}).get("orderId")
                if exchange_order_id == format_cancelled_id:
                    self.logger().info(f"Successfully cancelled order:{client_order_id}. "
                                       f"exchange id:{exchange_order_id}")
                    self.stop_tracking_order(client_order_id)
                    self.trigger_event(MarketEvent.OrderCancelled,
                                       OrderCancelledEvent(
                                           self.current_timestamp,
                                           client_order_id,
                                           tracked_order.exchange_order_id))
                    tracked_order.cancelled_event.set()
                    return client_order_id
                else:
                    raise IOError(f"delete_order({client_order_id}) tracked with exchange id: {exchange_order_id} "
                                  f"returned a different order id {format_cancelled_id}: order not found")
            except IOError as e:
                self.logger().error(f"_execute_cancel error: order {client_order_id} does not exist on Idex. "
                                    f"No cancellation performed: {str(e)}")
                if tracked_order is not None and "order not found" in str(e).lower():
                    # The order was never there to begin with. So cancelling it is a no-op but semantically successful.
                    self.stop_tracking_order(client_order_id)
                    self.trigger_event(MarketEvent.OrderCancelled,
                                       OrderCancelledEvent(
                                           self.current_timestamp,
                                           client_order_id,
                                           tracked_order.exchange_order_id))
                    return client_order_id
                else:
                    self.logger().network(
                        f"Failed to cancel not found order {client_order_id}: {str(e)}",
                        exc_info=True,
                        app_warning_msg=f"Failed to cancel the order {client_order_id} on Idex.")
                    raise e
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().exception(f'_execute_cancel raised unexpected exception: {e}. Details:')
                self.logger().network(
                    f"Failed to cancel order {client_order_id}: {str(e)}",
                    exc_info=True,
                    app_warning_msg=f"Failed to cancel the order {client_order_id} on Idex. "
                                    f"Check API key and network connection.")

    # API Calls

    async def get_ping(self):
        """Requests status of current connection."""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/ping/"
            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address()
            }
            auth_dict = self._idex_auth.generate_auth_dict(http_method="GET", url=url, params=params)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(auth_dict["url"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}. {response}")
            return

    async def list_orders(self) -> List[Dict[str, Any]]:
        """Requests status of all active orders. Returns json data of all orders associated with wallet address"""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/orders"
            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address()
            }
            auth_dict = self._idex_auth.generate_auth_dict(http_method="GET", url=url, params=params)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(auth_dict["url"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}. {response}")
                data = await response.json()
                return data

    async def get_order(self, exchange_order_id: str) -> Dict[str, Any]:
        """Requests order information through API with exchange order Id. Returns json data with order details"""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/orders"
            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address(),
                "orderId": exchange_order_id
            }
            auth_dict = self._idex_auth.generate_auth_dict(http_method="GET", url=url, params=params)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(auth_dict["url"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    data = await response.json()
                    raise IOError(f"Error fetching data from {url}, {auth_dict['url']}. HTTP status is "
                                  f"{response.status}. {data}")
                data = await response.json()
                return data

    async def post_order(self, params) -> Dict[str, Any]:
        """Posts an order request to the Idex API. Returns json data with order details"""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/orders"

            params.update({
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address()
            })

            if params["type"] == "market":
                order_type = OrderTypeEnum.market
            elif params["type"] == "limit":
                order_type = OrderTypeEnum.limit
            elif params["type"] == "limitMaker":
                order_type = OrderTypeEnum.limitMaker

            if params["side"] == "buy":
                trade_type = OrderSideEnum.buy
            elif params["side"] == "sell":
                trade_type = OrderSideEnum.sell

            signature_parameters = self._idex_auth.build_signature_params_for_order(
                market=params["market"],
                order_type=order_type,
                order_side=trade_type,
                order_quantity=params["quantity"],
                quantity_in_quote=False,
                price=params["price"],
                client_order_id=params["clientOrderId"],
            )
            wallet_signature = self._idex_auth.wallet_sign(signature_parameters)

            body = {
                "parameters": params,
                "signature": wallet_signature
            }

            auth_dict = self._idex_auth.generate_auth_dict_for_post(url=url, body=body)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.post(auth_dict["url"], data=auth_dict["body"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    data = await response.json()
                    raise IOError(f"Error posting data to {url}. HTTP status is {response.status}."
                                  f"Data is: {data}")
                data = await response.json()
                return data

    async def delete_order(self, trading_pair: str, client_order_id: str):
        """
        Deletes an order or all orders associated with a wallet from the Idex API.
        Returns json data with order id confirming deletion
        """
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/orders"

            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address(),
                "orderId": f"client:{client_order_id}",
            }
            signature_parameters = self._idex_auth.build_signature_params_for_cancel_order(
                # potential value: client_order_id=f"client:{order_id}"
                client_order_id=f"client:{client_order_id}",
            )
            wallet_signature = self._idex_auth.wallet_sign(signature_parameters)

            body = {
                "parameters": params,
                "signature": wallet_signature
            }

            auth_dict = self._idex_auth.generate_auth_dict_for_delete(url=url, body=body, wallet_signature=wallet_signature)
            session: aiohttp.ClientSession = await self._http_client()
            if DEBUG:
                self.logger().info(f"Cancelling order {client_order_id} for {trading_pair}.")
            async with session.delete(auth_dict["url"], data=auth_dict["body"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    data = await response.json()
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}. {data}")
                data = await response.json()
                return data

    async def get_balances_from_api(self) -> List[Dict[str, Any]]:
        """Requests current balances of all assets through API. Returns json data with balance details"""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/balances"
            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address(),
            }
            auth_dict = self._idex_auth.generate_auth_dict(http_method="GET", url=url, params=params)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(auth_dict["url"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}. {response}")
                data = await response.json()
                return data

    async def get_exchange_info_from_api(self) -> Dict[str, Any]:
        """Requests basic info about idex exchange. We are mostly interested in the gas price in gwei"""
        async with get_throttler().execute_task(HTTP_USER_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/exchange"
            params = {
                "nonce": self._idex_auth.generate_nonce(),
                "wallet": self._idex_auth.get_wallet_address()
            }
            auth_dict = self._idex_auth.generate_auth_dict(http_method="GET", url=url, params=params)
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(auth_dict["url"], headers=auth_dict["headers"]) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}")
                return await response.json()

    async def get_market_info_from_api(self) -> List[Dict]:
        """Requests all markets (trading pairs) available to Idex users."""
        async with get_throttler().execute_task(HTTP_PUBLIC_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/markets"
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(url) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}")
                return await response.json()

    async def get_assets_from_api(self) -> List[Dict]:
        """Requests info about assets traded in Idex"""
        async with get_throttler().execute_task(HTTP_PUBLIC_ENDPOINTS_LIMIT_ID):
            rest_url = get_idex_rest_url(domain=self._domain)
            url = f"{rest_url}/v1/assets"
            session: aiohttp.ClientSession = await self._http_client()
            async with session.get(url) as response:
                if response.status != 200:
                    raise IOError(f"Error fetching data from {url}. HTTP status is {response.status}")
                return await response.json()

    async def _create_order(self,
                            trade_type: TradeType,
                            client_order_id: str,
                            trading_pair: str,
                            amount: Decimal,
                            order_type: OrderType,
                            price: Decimal):
        """
        Calls create-order API end point to place an order, starts tracking the order and triggers order created event.
        :param trade_type: BUY or SELL
        :param client_order_id: Internal order id (also called client_order_id)
        :param trading_pair: The market to place order
        :param amount: The order amount (in base token value)
        :param order_type: The order type (MARKET, LIMIT, etc..)
        :param price: The order price
        """
        async with self._order_lock:
            try:
                if not order_type.is_limit_type():
                    raise Exception(f"Unsupported order type: {order_type}")
                trading_rule = self._trading_rules[trading_pair]  # No trading rules applied at this time

                idex_order_param = hb_order_type_to_idex_param(order_type)
                idex_trade_param = hb_trade_type_to_idex_param(trade_type)

                amount = self.quantize_order_amount(trading_pair, amount)
                price = self.quantize_order_price(trading_pair, price)

                if amount < trading_rule.min_order_size:
                    raise ValueError(f"Buy order amount {amount} is lower than the minimum order size "
                                     f"{trading_rule.min_order_size}. client_order_id: {client_order_id}")

                api_params = {
                    "market": trading_pair,
                    "type": idex_order_param,
                    "side": idex_trade_param,
                    "quantity": f'{amount:.8f}',
                    "price": f'{price:.8f}',
                    "clientOrderId": client_order_id,
                    "timeInForce": "gtc",
                    "selfTradePrevention": "dc"
                }

                order_result = await self.post_order(api_params)
                exchange_order_id = order_result.get("orderId")
                self.start_tracking_order(client_order_id,
                                          exchange_order_id,
                                          trading_pair,
                                          trade_type,
                                          price,
                                          amount,
                                          order_type
                                          )
                tracked_order = self._in_flight_orders.get(client_order_id)
                if DEBUG:
                    self.logger().info(f"Created {order_type.name} {trade_type.name} order {client_order_id} for "
                                       f"{amount} {trading_pair}.")
                tracked_order.update_exchange_order_id(exchange_order_id)
                event_tag = MarketEvent.BuyOrderCreated if trade_type is TradeType.BUY else MarketEvent.SellOrderCreated
                event_class = BuyOrderCreatedEvent if trade_type is TradeType.BUY else SellOrderCreatedEvent
                self.trigger_event(event_tag,
                                   event_class(
                                       self.current_timestamp,
                                       order_type,
                                       trading_pair,
                                       amount,
                                       price,
                                       client_order_id,
                                       exchange_order_id))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().network(
                    f"Error submitting {trade_type.name} {order_type.name} order to Idex for "
                    f"{amount} {trading_pair} "
                    f"{price}.",
                    exc_info=True,
                    app_warning_msg=str(e)
                )
                self.trigger_event(MarketEvent.OrderFailure, MarketOrderFailureEvent(
                    self.current_timestamp, client_order_id, order_type))
                self.stop_tracking_order(client_order_id)

    def start_tracking_order(self,
                             order_id: str,
                             exchange_order_id: str,
                             trading_pair: str,
                             trade_type: TradeType,
                             price: Decimal,
                             amount: Decimal,
                             order_type: OrderType):
        """
        Starts tracking an order by simply adding it into _in_flight_orders dictionary.
        """
        if DEBUG:
            if order_id in self._in_flight_orders:
                self.logger().warning(
                    f'start_tracking_order: About to overwrite an in flight order with client_order_id={order_id}'
                )
        self._in_flight_orders[order_id] = IdexInFlightOrder(
            client_order_id=order_id,
            exchange_order_id=exchange_order_id,
            trading_pair=trading_pair,
            order_type=order_type,
            trade_type=trade_type,
            price=price,
            amount=amount
        )

    def stop_tracking_order(self, order_id: str):
        if order_id in self._in_flight_orders:
            del self._in_flight_orders[order_id]
        else:
            if DEBUG:
                self.logger().warning(
                    f'stop_tracking_order: cannot delete order not stored in flight client_order_id={order_id}')

    def get_order_book(self, trading_pair: str) -> OrderBook:
        if trading_pair not in self._order_book_tracker.order_books:
            raise ValueError(f"No order book exists for '{trading_pair}'.")
        return self._order_book_tracker.order_books[trading_pair]

    async def _status_polling_loop(self):
        """Periodically update user balances and order status via REST API. Fallback measure for ws API updates."""

        while True:
            try:
                self._poll_notifier = asyncio.Event()
                await self._poll_notifier.wait()
                await safe_gather(
                    self._update_balances(),
                    self._update_order_status(),
                    self._update_exchange_info(),
                    self._update_market_info(),
                    self._update_assets_info(),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().exception(f'_status_polling_loop received exception: {e}. Details: ')
                self.logger().network("Unexpected error while fetching account updates.",
                                      exc_info=True,
                                      app_warning_msg="Could not fetch account updates from Idex. "
                                                      "Check API key and network connection.")
                await asyncio.sleep(0.5)
            finally:
                self._last_poll_timestamp = self.current_timestamp

    def get_fee(self,
                base_currency: str,
                quote_currency: str,
                order_type: OrderType,
                order_side: TradeType,
                amount: Decimal,
                price: Decimal = s_decimal_NaN) -> TradeFee:
        """
        Return an estimation for fees before orders are submitted. Called by hummingbot strategies before
        submitting orders to estimate profitability of trade proposals and constrain operations to budget.
        We assume that some limit orders may end up incurring gas fees as takers if they cross the spread,
        so the fee estimate here is relatively conservative.
        For actual fees incurred by running orders see: IdexInFlightOrder.update_with_fill_update
        """
        is_maker = order_type is OrderType.LIMIT_MAKER
        percent_fees: Decimal = estimate_fee(EXCHANGE_NAME, is_maker).percent
        # for taker idex v3 collects additional gas fee, collected in the asset received by the taker
        # we grossly approximate this as an extra percentage fee
        gas_extra_fees = GAS_EXTRA_FEES[0] if is_maker else GAS_EXTRA_FEES[1]
        percent_fees += Decimal(str(gas_extra_fees)) / Decimal("100")
        return TradeFee(percent=percent_fees)

    async def _update_order_status(self):
        """
        Calls REST API to get status update for each in-flight order.
        """
        last_tick = int(self._last_poll_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        current_tick = int(self.current_timestamp / self.UPDATE_ORDER_STATUS_MIN_INTERVAL)
        if current_tick > last_tick and len(self._in_flight_orders) > 0:
            async with self._order_lock:
                tracked_orders = list(self._in_flight_orders.values())
                if DEBUG:
                    exchange_order_ids = [tracked_order.exchange_order_id for tracked_order in tracked_orders]
                    self.logger().info(f"Polling order status updates for orders: {exchange_order_ids}")
                tasks = [self.get_order(tracked_order.exchange_order_id) for tracked_order in tracked_orders]
                update_results = await safe_gather(*tasks, return_exceptions=True)
                tracked_order_result = [(o, r) for o, r in zip(tracked_orders, update_results)]
                for tracked_order, result in tracked_order_result:
                    if isinstance(result, Exception):
                        self.logger().error(f"exception in _update_order_status get_order subtask: {result}")
                        # remove failed order from tracked_orders
                        self.stop_tracking_order(tracked_order.client_order_id)
                        self.logger().error(f'Stopped tracking not found order: {tracked_order.client_order_id}')
                        self.trigger_event(MarketEvent.OrderFailure, MarketOrderFailureEvent(
                            self.current_timestamp, tracked_order.client_order_id, tracked_order.order_type))
                        continue
                    await self._process_fill_message(result)
                    self._process_order_message(result)

    def _process_order_message(self, order_msg: Dict[str, Any]):
        """
        Updates in-flight order and triggers cancellation or failure event if needed.
        :param order_msg: The order response from either REST or web socket API (they are different formats)
        """
        client_order_id = order_msg["c"] if "c" in order_msg else order_msg.get("clientOrderId")
        if client_order_id not in self._in_flight_orders:
            return
        tracked_order = self._in_flight_orders[client_order_id]
        # Update order execution status
        tracked_order.last_state = order_msg["X"] if "X" in order_msg else order_msg.get("status")
        if tracked_order.is_cancelled:
            self.trigger_event(MarketEvent.OrderCancelled,
                               OrderCancelledEvent(
                                   self.current_timestamp,
                                   client_order_id,
                                   tracked_order.exchange_order_id))
            tracked_order.cancelled_event.set()
            self.logger().info(f"The order {client_order_id} is no longer tracked!")
            self.stop_tracking_order(client_order_id)
        elif tracked_order.is_failure:
            self.logger().info(f"The market order {client_order_id} has been rejected according to order status API.")
            self.trigger_event(MarketEvent.OrderFailure,
                               MarketOrderFailureEvent(
                                   self.current_timestamp,
                                   client_order_id,
                                   tracked_order.order_type
                               ))
            self.stop_tracking_order(client_order_id)

    async def _process_fill_message(self, update_msg: Dict[str, Any]):
        """
        Updates in-flight order and trigger order filled event for trade message received. Triggers order completed
        event if the total executed amount equals to the specified order amount.
        """

        client_order_id = update_msg["c"] if "c" in update_msg else update_msg.get("clientOrderId")
        tracked_order = self._in_flight_orders.get(client_order_id)
        if not tracked_order:
            return
        if update_msg.get("F") or update_msg.get("fills") is not None:
            for fill_msg in update_msg["F"] if "F" in update_msg else update_msg.get("fills"):
                if DEBUG:
                    self.logger().info(f'Fill Message:{fill_msg}')
                updated = tracked_order.update_with_fill_update(fill_msg)
                if not updated:
                    return
                self.trigger_event(
                    MarketEvent.OrderFilled,
                    OrderFilledEvent(
                        self.current_timestamp,
                        tracked_order.client_order_id,
                        tracked_order.trading_pair,
                        tracked_order.trade_type,
                        tracked_order.order_type,
                        Decimal(str(fill_msg["p"] if "p" in fill_msg else fill_msg.get("price"))),
                        Decimal(str(fill_msg["q"] if "q" in fill_msg else fill_msg.get("quantity"))),
                        TradeFee(0.0, [(fill_msg["a"] if "a" in fill_msg else fill_msg.get("feeAsset"),
                                        Decimal(str(fill_msg["f"] if "f" in fill_msg else fill_msg.get("fee"))))]),
                        exchange_trade_id=update_msg["i"] if "i" in update_msg else update_msg.get("orderId")
                    )
                )
        if math.isclose(tracked_order.executed_amount_base, tracked_order.amount, rel_tol=NORMALIZED_PRECISION) or \
                tracked_order.executed_amount_base >= tracked_order.amount:
            tracked_order.last_state = "filled"
            self.logger().info(f"The {tracked_order.trade_type.name} order "
                               f"{tracked_order.client_order_id} has completed "
                               f"according to order status API.")
            event_tag = MarketEvent.BuyOrderCompleted if tracked_order.trade_type is TradeType.BUY \
                else MarketEvent.SellOrderCompleted
            event_class = BuyOrderCompletedEvent if tracked_order.trade_type is TradeType.BUY \
                else SellOrderCompletedEvent
            self.trigger_event(event_tag,
                               event_class(self.current_timestamp,
                                           tracked_order.client_order_id,
                                           tracked_order.base_asset,
                                           tracked_order.quote_asset,
                                           tracked_order.fee_asset,
                                           tracked_order.executed_amount_base,
                                           tracked_order.executed_amount_quote,
                                           tracked_order.fee_paid,
                                           tracked_order.order_type,
                                           tracked_order.exchange_order_id))
            self.stop_tracking_order(tracked_order.client_order_id)

    async def cancel_all(self, timeout_seconds: float):
        """
        Cancels all in-flight orders and waits for cancellation results.
        Used by bot's top level stop and exit commands (cancelling outstanding orders on exit)
        :param timeout_seconds: The timeout at which the operation will be canceled.
        :returns List of CancellationResult which indicates whether each order is successfully cancelled.
        """
        async with self._order_lock:
            incomplete_orders = [o for o in self._in_flight_orders.values() if not o.is_done]
            tasks = [self.delete_order(o.trading_pair, o.client_order_id) for o in incomplete_orders]
            order_id_set = set([o.client_order_id for o in incomplete_orders])
            successful_cancellations = []
            try:
                async with timeout(timeout_seconds):
                    results = await safe_gather(*tasks, return_exceptions=True)
                    incomplete_order_result = list(zip(incomplete_orders, results))
                    for incomplete_order, result in incomplete_order_result:
                        if isinstance(result, Exception):
                            self.logger().error(
                                f"exception in cancel_all , subtask delete_order. "
                                f"client_order_id: {incomplete_order.client_order_id}, error: {result}",
                            )
                            continue
                        order_id_set.remove(incomplete_order.client_order_id)
                        successful_cancellations.append(CancellationResult(incomplete_order.client_order_id, True))
                        if not result:
                            self.logger().error(
                                f'cancel_all: self.delete_order({incomplete_order.trading_pair}, '
                                f'{incomplete_order.client_order_id}) returned empty response: order not found')
                            response_order_id = '--no-value--'
                        else:
                            response_order_id = (result[0] or {}).get("orderId")
                        if incomplete_order.exchange_order_id != response_order_id:
                            self.logger().error(
                                f"cancel_all: delete_order({incomplete_order.client_order_id}) "
                                f"tracked with exchange id: {incomplete_order.exchange_order_id} "
                                f"returned a different order id {response_order_id}: order not found")
                        # let's stop tracking the order whether we failed or not
                        self.stop_tracking_order(incomplete_order.client_order_id)
                        self.trigger_event(MarketEvent.OrderCancelled,
                                           OrderCancelledEvent(
                                               self.current_timestamp,
                                               incomplete_order.client_order_id,
                                               incomplete_order.exchange_order_id))
                        incomplete_order.cancelled_event.set()
                        self.logger().info(
                            f"cancel_all: finished processing cancel of order:{incomplete_order.client_order_id}. "
                            f"exchange id:{incomplete_order.exchange_order_id}")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger().network(
                    f"Unexpected error cancelling orders. Error: {str(e)}",
                    exc_info=True,
                    app_warning_msg="Failed to cancel order on Idex. Check API key and network connection."
                )
            failed_cancellations = [CancellationResult(oid, False) for oid in order_id_set]
            return successful_cancellations + failed_cancellations

    def tick(self, timestamp: float):
        """
        Is called automatically by the clock for each clock's tick (1 second by default).
        It checks if status polling task is due for execution.
        """
        now = time.time()
        poll_interval = (self.SHORT_POLL_INTERVAL
                         if now - self._user_stream_tracker.last_recv_time > 60.0
                         else self.LONG_POLL_INTERVAL)
        last_tick = self._last_timestamp / poll_interval
        current_tick = timestamp / poll_interval
        if current_tick > last_tick:
            if not self._poll_notifier.is_set():
                self._poll_notifier.set()
        self._last_timestamp = timestamp

    async def _update_balances(self, sender=None):
        """ Calls REST API to update total and available balances. """

        local_asset_names = set(self._account_balances.keys())
        remote_asset_names = set()
        balance_info = await self.get_balances_from_api()
        for balance in balance_info:
            asset_name = balance["asset"]
            self._account_available_balances[asset_name] = Decimal(str(balance["availableForTrade"]))
            self._account_balances[asset_name] = Decimal(str(balance["quantity"]))
            remote_asset_names.add(asset_name)

        asset_names_to_remove = local_asset_names.difference(remote_asset_names)
        for asset_name in asset_names_to_remove:
            del self._account_available_balances[asset_name]
            del self._account_balances[asset_name]

    @async_ttl_cache(ttl=60 * 10, maxsize=1)
    async def _update_exchange_info(self):
        """Call REST API to update basic exchange info"""
        self._exchange_info = await self.get_exchange_info_from_api()

    @async_ttl_cache(ttl=60 * 10, maxsize=1)
    async def _update_market_info(self):
        """Call REST API to update basic market info"""
        self._market_info = await self.get_market_info_from_api()

    @async_ttl_cache(ttl=60 * 7, maxsize=1)
    async def _update_assets_info(self):
        """Call REST API to update basic market info"""
        self._assets_info = await self.get_assets_from_api()

    async def _iter_user_event_queue(self) -> AsyncIterable[Dict[str, any]]:
        while True:
            try:
                yield await self._user_stream_tracker.user_stream.get()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    "Unknown error. Retrying after 1 seconds.",
                    exc_info=True,
                    app_warning_msg="Could not fetch user events from Idex. Check API key and network connection."
                )
                await asyncio.sleep(1.0)

    async def _user_stream_event_listener(self):
        """
        Listens to message in _user_stream_tracker.user_stream queue. The messages are put in by
        IdexAPIUserStreamDataSource.
        """
        async for event_message in self._iter_user_event_queue():
            try:
                if 'type' not in event_message or 'data' not in event_message:
                    if DEBUG:
                        self.logger().warning(f'unknown event received: {event_message}')
                    continue
                event_type, event_data = event_message['type'], event_message['data']
                if event_type == 'orders':
                    await self._process_fill_message(event_data)
                    self._process_order_message(event_data)
                elif event_type == 'balances':
                    asset_name = event_data['a']
                    # q	quantity	string	Total quantity of the asset held by the wallet on the exchange
                    # f	availableForTrade	string	Quantity of the asset available for trading; quantity - locked
                    # d	usdValue	string	Total value of the asset held by the wallet on the exchange in USD
                    self._account_balances[asset_name] = Decimal(str(event_data['q']))
                    self._account_available_balances[asset_name] = Decimal(str(event_data['f']))
                elif event_type == 'error':
                    self.logger().error(f"Unexpected error message received from api."
                                        f"Code: {event_data['code']}"
                                        f"message:{event_data['message']}", exc_info=True)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error in user stream listener loop.", exc_info=True)
                await asyncio.sleep(5.0)
