"""
Python strategy cli_X_AAABBB.py <-> <margin_wrapper> <-> exchanges-wrapper <-> Exchanges API/WSS
"""
__author__ = "Jerry Fedorenko"
__copyright__ = "Copyright © 2021 Jerry Fedorenko aka VM"
__license__ = "MIT"
__version__ = "2.0.2"
__maintainer__ = "Jerry Fedorenko"
__contact__ = "https://github.com/DogsTailFarmer"

import ast
import asyncio
import simplejson as json
import logging
import os
import time
import sqlite3
import random
import traceback
import pandas as pd
import shutil
import psutil
import csv
import queue

from colorama import init as color_init
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
from pathlib import Path
from datetime import datetime, timedelta
from tqdm import tqdm

# noinspection PyPackageRequirements
import grpc
import jsonpickle
# noinspection PyPackageRequirements
from google.protobuf import json_format
from margin_strategy_sdk import LogLevel, OrderUpdate, Dict, List
# noinspection PyUnresolvedReferences
from margin_strategy_sdk import StrategyConfig  # lgtm [py/unused-import]

from exchanges_wrapper.definitions import Interval
from exchanges_wrapper import api_pb2, api_pb2_grpc

from martin_binance import executor as ms, BACKTEST_PATH, copy, LOG_PATH
from martin_binance.client import Trade
from martin_binance.backtest.exchange_simulator import Account as backTestAccount

# For more channel options, please see https://grpc.io/grpc/core/group__grpc__arg__keys.html
CHANNEL_OPTIONS = [
    ('grpc.lb_policy_name', 'pick_first'),
    ('grpc.enable_retries', 0),
    ('grpc.keepalive_timeout_ms', 10000)
]

loop = asyncio.get_event_loop()
save_trade_queue = asyncio.Queue()

KLINES_INIT = [Interval.ONE_MINUTE, Interval.FIFTY_MINUTES, Interval.ONE_HOUR]
KLINES_LIM = 50  # Number of candles must be <= 1000
CANCEL_ALL_ORDERS = True  # Ask about cancel all active orders before start strategy and ms.LOAD_LAST_STATE = 0
TRADES_LIST_LIMIT = 100
HEARTBEAT = 2  # Sec
RATE_LIMITER = HEARTBEAT * 5
ORDER_TIMEOUT = HEARTBEAT * 15  # Sec
TRY_LIMIT = 30

logger = logging.getLogger('logger')
color_init()

ORDER_BOOK_PKL = "order_book.pkl"
TICKER_PKL = "ticker.pkl"
MS_ORDER_ID = 'ms.order_id'
MS_ORDERS = 'ms.orders'
EQUAL_STR = "================================================================"

session_result = {}


class Style:
    __slots__ = ()

    BLACK: str = '\033[30m'
    RED: str = '\033[31m'
    B_RED: str = '\033[1;31m'
    GREEN: str = '\033[32m'
    YELLOW: str = '\033[33m'
    B_YELLOW: str = "\033[33;1m"
    BLUE: str = '\033[34m'
    MAGENTA: str = '\033[35m'
    CYAN: str = '\033[36m'
    GRAY: str = '\033[37m'
    WHITE: str = '\033[0;37m'
    B_WHITE: str = '\033[1;37m'
    UNDERLINE: str = '\033[4m'
    RESET: str = '\033[0m'

    @classmethod
    def __add__(cls, b):
        return Style() + b


def any2str(_x) -> str:
    return f"{_x:.8f}".rstrip('0').rstrip('.')


def write_log(level: LogLevel, message: str) -> None:
    if level == LogLevel.DEBUG:
        logger.debug(message)
    elif level == LogLevel.INFO:
        logger.info(message)
    elif level == LogLevel.WARNING:
        logger.warning(message)
    elif level == LogLevel.ERROR:
        logger.error(message)
    elif level == LogLevel.CRITICAL:
        logger.critical(message)


def convert_from_minute(m: int) -> str:
    if 1 <= m < 3:
        return '1m'
    if 3 <= m < 5:
        return '3m'
    if 5 <= m < 15:
        return '5m'
    if 15 <= m < 30:
        return '15m'
    if 30 <= m < 60:
        return '30m'
    if 60 <= m < 120:
        return '1h'
    if 120 <= m < 240:
        return '2h'
    if 240 <= m < 360:
        return '4h'
    if 360 <= m < 480:
        return '6h'
    if 480 <= m < 720:
        return '8h'
    if 720 <= m < 1440:
        return '12h'
    if 1440 <= m < 4320:
        return '1d'
    if 4320 <= m < 10080:
        return '3d'
    return '1w' if 10080 <= m < 44640 else '1m'


def trade_not_exist(_order_id: int, _trade_id: int) -> bool:
    return all(
        trade.order_id != _order_id or trade.id != _trade_id
        for trade in StrategyBase.trades
    )


def order_trades_sum(_order_id: int) -> Decimal:
    saved_filled_quantity = Decimal("0")
    for _trade in StrategyBase.trades:
        if _trade.order_id == _order_id:
            saved_filled_quantity += _trade.amount
    return saved_filled_quantity.quantize(Decimal("1.01234567"), rounding=ROUND_FLOOR)


class PrivateTrade:
    __slots__ = ("amount", "buy", "id", "order_id", "price", "timestamp")

    def __init__(self, _trade: {}) -> None:
        # Amount of the trade.
        self.amount = Decimal(_trade["qty"])
        # True, if the trade was a buy.
        self.buy = _trade.get('isBuyer', False)
        # id of the trade.
        self.id = _trade["id"]
        # id of the order that the trade belongs to.
        self.order_id = int(_trade["orderId"])
        # Price at which the trade was executed.
        self.price = Decimal(_trade["price"])
        # Timestamp of the trade.
        self.timestamp = int(_trade["time"])

    def __call__(self):
        return self


# noinspection PyRedeclaration
class OrderUpdate(OrderUpdate):
    __slots__ = ("original_order", "resulting_trades", "status", "timestamp", "updated_order")

    def __init__(self, event: {}) -> None:
        super().__init__()

        class OriginalOrder:
            __slots__ = ("id",)

            def __init__(self, _event: {}):
                self.id = _event['order_id']

        # Original order previous to this update.
        self.original_order = OriginalOrder(event)
        # Trades that belong to the order, if any exist so far.
        self.resulting_trades = []
        for trade in StrategyBase.trades:
            if trade.order_id == event['order_id']:
                self.resulting_trades.append(trade)
        # Update status defining what happened to the order since the last update.
        if event['order_status'] == 'FILLED':
            self.status = OrderUpdate.FILLED
        elif event['order_status'] == 'PARTIALLY_FILLED':
            self.status = OrderUpdate.PARTIALLY_FILLED
        elif event['order_status'] == 'CANCELED':
            self.status = OrderUpdate.CANCELED
        else:
            self.status = OrderUpdate.OTHER_CHANGE
        # Time of the change.
        self.timestamp = event['transaction_time']
        # Newly updated order
        self.updated_order = None

    def __call__(self):
        return self


class Order:
    __slots__ = ("amount", "buy", "id", "order_type", "price", "received_amount", "remaining_amount", "timestamp")

    def __init__(self, order: {}) -> None:
        # Overall amount of the order.
        self.amount = Decimal(order['origQty'])
        # True if the order is a buy order.
        self.buy = order['side'] == 'BUY'
        # id of the order.
        self.id = int(order['orderId'])
        # Type of the order.
        self.order_type = order['type']
        # Price of the order.
        self.price = Decimal(order['price'])
        # Amount that has been filled already.
        self.received_amount = Decimal(order['executedQty'])
        # Amount that has not been filled yet.
        self.remaining_amount = self.amount - self.received_amount
        # Timestamp of the order.
        self.timestamp = int(order.get('transactTime', order.get('time', time.time())))

    def __call__(self):
        return self


class Candle:
    __slots__ = ("min_time", "open", "high", "low", "close", "volume", "max_time", "trade_number", "vwap")

    def __init__(self, _candle: []) -> None:
        # Start time of the candle.
        self.min_time = int(_candle[0])
        # Price of the first trade in the candle.
        self.open = float(_candle[1])
        # Highest traded price in the candle.
        self.high = float(_candle[2])
        # Lowest traded price in the candle.
        self.low = float(_candle[3])
        # Price of the last trade in the candle.
        self.close = float(_candle[4])
        # Volume traded within the candle.
        self.volume = float(_candle[5])
        # Time of the latest trade in the candle or closing time of the candle.
        self.max_time = int(_candle[6])
        # Number of trades included in the candle.
        self.trade_number = int(_candle[8])
        # Value weighted average price of the candle.
        self.vwap = (float(_candle[7]) / self.volume) if self.volume else self.close

    def __call__(self):
        return self


class TradingCapabilityManager:
    __slots__ = (
        "base_asset_precision",
        "quote_asset_precision",
        "min_qty",
        "max_qty",
        "step_size",
        "min_notional",
        "tick_size",
        "multiplier_up",
        "multiplier_down",
        "min_price",
        "max_price",
    )

    def __init__(self, _exchange_info_symbol, price_limit_rules):
        self.base_asset_precision = int(_exchange_info_symbol.get('baseAssetPrecision'))
        self.quote_asset_precision = int(_exchange_info_symbol.get('quoteAssetPrecision'))
        self.min_qty = Decimal(_exchange_info_symbol['filters']['lotSize']['minQty'])
        self.max_qty = Decimal(_exchange_info_symbol['filters']['lotSize']['maxQty'])
        self.step_size = Decimal(_exchange_info_symbol['filters']['lotSize']['stepSize'].rstrip('0'))
        self.min_notional = (
                Decimal(_exchange_info_symbol['filters'].get('notional', {}).get('minNotional', '0'))
                or Decimal(_exchange_info_symbol['filters'].get('minNotional', {}).get('minNotional', '0'))
        )
        self.tick_size = Decimal(_exchange_info_symbol['filters']['priceFilter']['tickSize'].rstrip('0'))
        self.min_price = Decimal(_exchange_info_symbol['filters']['priceFilter']['minPrice'])
        self.max_price = Decimal(_exchange_info_symbol['filters']['priceFilter']['maxPrice'])
        if price_limit_rules:
            self.multiplier_up = 1 + price_limit_rules / 100
            self.multiplier_down = 1 - price_limit_rules / 100
        else:
            self.multiplier_up = Decimal(_exchange_info_symbol['filters']['percentPrice']['multiplierUp'])
            self.multiplier_down = Decimal(_exchange_info_symbol['filters']['percentPrice']['multiplierDown'])

    def __call__(self):
        return self

    def round_amount(self, unrounded_amount: Decimal, rounding_type: str) -> Decimal:
        return unrounded_amount.quantize(self.step_size, rounding=rounding_type)

    def round_price(self, unrounded_price: Decimal, rounding_type: str) -> Decimal:
        return unrounded_price.quantize(self.tick_size, rounding=rounding_type)

    def get_min_sell_amount(self, price: Decimal) -> Decimal:
        # print(f"get_min_sell_amount: price:{price}, min_qty:{self.min_qty}, min_notional:{self.min_notional}")
        return max(self.min_qty, self.round_amount(self.min_notional / price, ROUND_CEILING))

    def get_max_sell_amount(self, _unused_price: Decimal) -> Decimal:
        """
        Returns the maximally possible sell amount that can be placed at a given price.
        """
        return self.max_qty

    def get_min_buy_amount(self, price: Decimal) -> Decimal:
        # print(f"get_min_buy_amount: price:{price}, min_notional:{self.min_notional}")
        return max(self.min_qty, self.round_amount(self.min_notional / price, ROUND_CEILING))

    def get_minimal_price_change(self) -> Decimal:
        return self.tick_size

    def get_minimal_amount_change(self) -> Decimal:
        """
        Get the minimal amount change that is possible to use on the exchange.
        """
        return self.step_size

    def get_max_sell_price(self, avg_price: Decimal) -> Decimal:
        return self.round_price(avg_price * self.multiplier_up, ROUND_FLOOR)

    def get_max_price(self) -> Decimal:
        return self.max_price

    def get_min_buy_price(self, avg_price: Decimal) -> Decimal:
        return self.round_price(avg_price * self.multiplier_down, ROUND_CEILING)

    def get_min_price(self) -> Decimal:
        return self.min_price


class Ticker:
    __slots__ = ("last_day_price", "last_price", "timestamp")

    def __init__(self, _ticker):
        # Price of the currency pair one day ago.
        self.last_day_price = Decimal(_ticker.get('openPrice', '0'))
        # Last traded price of the currency pair.
        self.last_price = Decimal(_ticker.get('lastPrice', '0'))
        # Timestamp of the ticker data.
        self.timestamp = int(_ticker.get('closeTime', 0))
        # print(f"self.last_price: {self.last_price}")

    def __call__(self):
        return self


class FundsEntry:
    __slots__ = ("available", "reserved", "total_for_currency")

    def __init__(self, _funds):
        # The available amount for a currency.
        self.available = Decimal(_funds.get('free'))
        # The reserved amount for a currency.
        self.reserved = Decimal(_funds.get('locked'))
        # Total amount of a currency in the account.
        self.total_for_currency = self.available + self.reserved
        # print(f"self.total_for_currency: {self.total_for_currency}")

    def __call__(self):
        return self


class OrderBook:
    __slots__ = ("asks", "bids")

    """
    order_book.bids[0].price
    order_book.asks[0].amount
    """

    def __init__(self, _order_book) -> None:
        class _OrderBookRow:
            __slots__ = ("price", "amount")

            def __init__(self, _order) -> None:
                self.price = Decimal(_order[0])
                self.amount = Decimal(_order[1])

        self.asks = []
        # List of asks ordered by price in ascending order.
        self.bids = []
        # List of bids ordered by price in descending order.
        self.asks.extend(_OrderBookRow(v) for v in _order_book['asks'])
        self.bids.extend(_OrderBookRow(v) for v in _order_book['bids'])

    def __call__(self):
        return self


class StrategyBase:
    __slots__ = (
        "time_operational",
        "s_ticker",
        "s_order_book",
        "klines",
        "candles",
        "account",
        "grid_buy",
        "grid_sell",
        "get_buffered_funds_last_time",
        "queue_to_tlg",
        "local_time",
        "status_time",
        "tlg_header",
    )

    session = None
    client: api_pb2.OpenClientConnectionId = None
    exchange = str()
    symbol = str()
    channel: grpc.Channel = None
    stub = api_pb2_grpc.MartinStub
    client_id = int()
    strategy = None
    info_symbol = {}
    base_asset = str()
    quote_asset = str()
    ticker = {}
    funds = {}
    order_book = {}
    order_id = int(datetime.now().strftime("%S%M")) * 1000
    wait_order_id = []  # List of placed orders for time-out detect
    canceled_order_id = []  # List canceled orders for time-out detect
    trades = []  # List of trades associated with strategy (limit = TRADES_LIST_LIMIT)
    orders = {}  # {int(id): Order(), } of orders associated with strategy
    tcm = None  # TradingCapabilityManager
    last_state = None
    rate_limiter = RATE_LIMITER
    start_time_ms = int(time.time() * 1000)
    send_request = None
    for_request = None
    wss_fire_up = False
    backtest = {}
    delay_ordering_s = 0.5
    bulk_orders_cancel = {}
    session_root: Path
    state_file: Path
    operational_status = None

    def __init__(self):
        print("Init StrategyBase")
        self.time_operational = {'start': 0.0, 'ts': 0.0, 'new': 0.0}  # - See get_time()
        self.s_ticker = {}
        self.s_order_book = {}
        self.klines = {}  # KLines snapshot
        self.candles = {}  # Candles stream
        self.account = backTestAccount(ms.SAVE_DS) if ms.MODE == 'S' else None
        self.grid_buy = {}
        self.grid_sell = {}
        self.get_buffered_funds_last_time = self.get_time()
        self.queue_to_tlg = queue.Queue()  # - Queue for sending message to Telegram
        self.local_time = self.get_time
        self.status_time = None  # + Last time sending status message
        self.tlg_header = ''  # - Header for Telegram message

    def __call__(self):
        return self

    class Klines:
        klines_series = {}
        klines_lim = int()

        def __init__(self, _interval):
            self.interval = _interval
            self.kline = []
            self.klines_series[_interval] = self.kline

        def refresh(self, _candle):
            candle = Candle(_candle)
            # print(f"refresh.interval: {self.interval}, candle: {candle.min_time}")
            new_time = candle.min_time
            last_time = self.kline[-1].min_time if self.kline else 0
            if new_time >= last_time:
                if new_time == last_time:
                    self.kline[-1] = candle
                else:
                    self.kline.append(candle)
                    if len(self.kline) > self.klines_lim:
                        del self.kline[0]
                self.klines_series[self.interval] = self.kline

        @classmethod
        def get_kline(cls, _interval) -> []:
            return cls.klines_series.get(_interval, [])

    def reset_var(self):
        self.s_ticker = {}
        self.s_order_book = {}
        self.klines = {}  # KLines snapshot
        self.candles = {}  # Candles stream
        self.grid_buy = {}
        self.grid_sell = {}

    @staticmethod
    def reset_class_var():
        cls = StrategyBase
        cls.ticker = {}
        cls.funds = {}
        cls.order_book = {}
        cls.order_id = int(datetime.now().strftime("%S%M")) * 1000
        cls.wait_order_id = []  # List of placed orders for time-out detect
        cls.canceled_order_id = []  # List canceled orders  for time-out detect
        cls.trades = []  # List of trades associated with strategy (limit = TRADES_LIST_LIMIT)
        cls.orders = {}  # Set of orders associated with strategy
        cls.strategy.get_buffered_funds_last_time = cls.strategy.get_time()
        cls.rate_limiter = RATE_LIMITER
        cls.start_time_ms = int(time.time() * 1000)
        cls.backtest = {}
        cls.bulk_orders_cancel = {}

    @classmethod
    def order_exist(cls, _id) -> bool:
        return bool(cls.orders.get(_id))

    def get_trading_capability_manager(self) -> TradingCapabilityManager:
        return self.tcm

    def get_first_currency(self) -> str:
        return self.info_symbol.get('baseAsset')

    def get_second_currency(self) -> str:
        return self.info_symbol.get('quoteAsset')

    def get_buffered_ticker(self) -> Ticker:
        # print(f"get_buffered_ticker.ticker: {self.ticker}")
        return Ticker(self.ticker)

    def get_buffered_funds(self) -> Dict[str, FundsEntry]:
        # print(f"get_buffered_funds.funds: {self.funds}")
        if self.strategy.local_time() - self.get_buffered_funds_last_time > self.rate_limiter:
            loop.create_task(buffered_funds(print_info=False))
            self.get_buffered_funds_last_time = self.get_time()
        return {self.base_asset: FundsEntry(self.funds[self.base_asset]),
                self.quote_asset: FundsEntry(self.funds[self.quote_asset])}

    def get_buffered_order_book(self) -> OrderBook:
        # print(f"get_buffered_order_book.order_book: {self.order_book}")
        return OrderBook(self.order_book)

    def place_limit_order(self, buy: bool, amount: Decimal, price: Decimal) -> int:
        cls = StrategyBase
        cls.order_id += 1
        self.message_log(f"Send order id:{cls.order_id} for {'BUY' if buy else 'SELL'}"
                         f" {any2str(amount)} by {any2str(price)} = {any2str(amount * price)}",
                         color=Style.B_YELLOW)
        loop.create_task(place_limit_order_timeout(cls.order_id))
        loop.create_task(create_limit_order(cls.order_id, buy, any2str(amount), any2str(price)))
        if cls.exchange == 'huobi':
            time.sleep(0.02)
        return cls.order_id

    def get_buffered_completed_trades(self, _get_all_trades: bool = False) -> List[PrivateTrade]:
        return self.trades

    def get_buffered_open_orders(self) -> List[Order]:
        return list(self.orders.values())

    @classmethod
    def get_buffered_open_order(cls, _id) -> Order:
        return cls.orders.get(_id)

    def get_time(self) -> float:
        """
        For backtesting purpose. Calculating monotonic local time based on self.time_operational['new'] value.
        It can be set from external source as int(time.time()) getting from historical data. If can't setting
        return system int(time.time()) Unix time.
        :return: int
        """
        if self.time_operational['new']:
            if self.time_operational['ts']:
                diff = time.time() - self.time_operational['ts']
            else:
                diff = 0.0
            if self.time_operational['start'] == self.time_operational['new']:
                last = self.time_operational['new'] + diff
                self.time_operational['start'] = self.time_operational['new'] = last
            elif self.time_operational['start'] > self.time_operational['new']:
                last = self.time_operational['start'] + diff
                self.time_operational['start'] = self.time_operational['new'] = last
            else:
                self.time_operational['start'] = last = self.time_operational['new']
            self.time_operational['ts'] = time.time()
        else:
            last = time.time()
        return last

    def open_orders_snapshot(self):
        orders_buy = {}
        orders_sell = {}
        for k, order in self.orders.items():
            if order.buy:
                orders_buy[k] = order.price
            else:
                orders_sell[k] = order.price
        self.grid_buy.update({int(time.time() * 1000): pd.Series(orders_buy)})
        self.grid_sell.update({int(time.time() * 1000): pd.Series(orders_sell)})

    @staticmethod
    def get_buffered_recent_candles(candle_size_in_minutes: int, number_of_candles: int = 50,
                                    include_current_building_candle: bool = False) -> List[Candle]:
        size = convert_from_minute(candle_size_in_minutes)
        kline = StrategyBase.Klines.get_kline(size)
        if len(kline) > number_of_candles + 1:
            return kline[-number_of_candles - (0 if include_current_building_candle else 1):
                         None if include_current_building_candle else -1]
        return kline[:None if include_current_building_candle else -1]

    @staticmethod
    def cancel_order(order_id: int, cancel_all=False) -> None:
        loop.create_task(cancel_order_timeout(order_id))
        loop.create_task(cancel_order_call(order_id, cancel_all))

    @staticmethod
    def transfer_to_master(symbol: str, amount: str):
        if ms.MODE in ('T', 'TC'):
            loop.create_task(transfer2master(symbol, amount))

    def message_log(self, msg: str, log_level=LogLevel.INFO, tlg=False, color=Style.WHITE) -> None:
        if tlg and color == Style.WHITE:
            color = Style.B_WHITE
        if log_level in (LogLevel.ERROR, LogLevel.CRITICAL):
            tlg = True
            color = Style.B_RED
        color_msg = color+msg+Style.RESET if color else msg
        if log_level not in ms.LOG_LEVEL_NO_PRINT:
            if ms.MODE in ('T', 'TC'):
                print(f"{datetime.now().strftime('%d/%m %H:%M:%S')} {color_msg}")
            else:
                tqdm.write(f"{datetime.fromtimestamp(self.local_time()).strftime('%H:%M:%S.%f')} {color_msg}")
        if ms.MODE in ('T', 'TC'):
            write_log(log_level, msg)
            if tlg and self.queue_to_tlg:
                msg = self.tlg_header + msg
                self.status_time = self.local_time()
                self.queue_to_tlg.put(msg)


async def save_to_csv() -> None:
    cls = StrategyBase
    file_name = Path(LOG_PATH, f"{ms.ID_EXCHANGE}_{ms.SYMBOL}.csv")
    with open(file_name, mode="a", buffering=1) as fp:
        writer = csv.writer(fp)
        writer.writerow(["TRADE",
                         "transaction_time",
                         "side",
                         "order_id",
                         "client_order_id",
                         "trade_id",
                         "order_quantity",
                         "order_price",
                         "cumulative_filled_quantity",
                         "quote_asset_transacted",
                         "last_executed_quantity",
                         "last_executed_price",
                         ])
        writer.writerow(['TRANSFER',
                         "event_time",
                         "asset",
                         "balance_delta",
                         ])
        while cls.strategy:
            writer.writerow(await save_trade_queue.get())
            save_trade_queue.task_done()


async def heartbeat(_session):
    cls = StrategyBase
    # print(f"tik-tak:' {int(time.time() * 1000)}")
    last_exec_time = time.time()
    while cls.strategy:
        try:
            last_state = cls.strategy.save_strategy_state()
            if ms.MODE in ('T', 'TC'):
                last_state_update(cls, last_state)
                # print(f"heartbeat.last_state: {last_state}")
                if ms.LAST_STATE_FILE.exists():
                    ms.LAST_STATE_FILE.replace(ms.LAST_STATE_FILE.with_suffix('.prev'))
                with ms.LAST_STATE_FILE.open(mode='w') as outfile:
                    json.dump(last_state, outfile, sort_keys=True, indent=4, ensure_ascii=False)
                #
                update_max_queue_size = False
                if cls.operational_status and (time.time() - last_exec_time > HEARTBEAT * 30):
                    last_exec_time = time.time()
                    try:
                        res = await cls.send_request(cls.stub.CheckStream, api_pb2.MarketRequest, symbol=cls.symbol)
                    except Exception as ex:
                        logger.warning(f"Exception on Check WSS: {ex}")
                    else:
                        if not res.success:
                            logger.warning(f"Not active WSS for {cls.symbol} on {cls.exchange}, restart request sent")
                            update_max_queue_size = True
                            cls.wss_fire_up = True
                #
                if cls.client_id and cls.wss_fire_up:
                    try:
                        if await cls.session.get_client():
                            update_class_var(cls.session)
                            await cls.send_request(cls.stub.StopStream, api_pb2.MarketRequest, symbol=cls.symbol)
                            await wss_init(update_max_queue_size=update_max_queue_size)
                            cls.wss_fire_up = False
                    except Exception as ex:
                        logger.warning(f"Exception on fire up WSS: {ex}")
                        cls.wss_fire_up = True
            await asyncio.sleep(HEARTBEAT)
        except (KeyboardInterrupt, asyncio.CancelledError):
            break


def last_state_update(cls, last_state):
    last_state[MS_ORDER_ID] = json.dumps(cls.order_id)
    last_state['ms_start_time_ms'] = json.dumps(cls.start_time_ms)
    last_state[MS_ORDERS] = jsonpickle.encode(cls.orders, keys=True)
    last_state['ms_trades'] = jsonpickle.encode(cls.trades)


async def save_asset():
    """
    Update account asset list and value in t_asset
    """
    cls = StrategyBase
    connection_analytic = None
    while connection_analytic is None:
        connection_analytic = cls.strategy.connection_analytic
        await asyncio.sleep(HEARTBEAT)
    delay = HEARTBEAT * 300  # 10 min
    max_use_update = 60 * 60 * 24  # 24h if the row has not been updated that the asset is not traded
    while True:
        try:
            res = await cls.send_request(cls.stub.FetchAccountInformation, api_pb2.OpenClientConnectionId)
        except asyncio.CancelledError:
            pass
        except Exception as _ex:
            logger.warning(f"Exception save_asset: {_ex}")
        else:
            balances = json_format.MessageToDict(res).get('balances', [])
            # Refresh actual balance
            try:
                balance_f = next(item for item in balances if item["asset"] == cls.base_asset)
            except StopIteration:
                balance_f = {'asset': cls.base_asset, 'free': '0.0', 'locked': '0.0'}
            try:
                balance_s = next(item for item in balances if item["asset"] == cls.quote_asset)
            except StopIteration:
                balance_s = {'asset': cls.base_asset, 'free': '0.0', 'locked': '0.0'}
            funds = {cls.base_asset: {'free': balance_f['free'], 'locked': balance_f['locked']},
                     cls.quote_asset: {'free': balance_s['free'], 'locked': balance_s['locked']}}
            cls.funds = funds
            # Get asset balances from Funding Wallet
            cursor = connection_analytic.cursor()
            try:
                cursor.execute('SELECT 1 FROM t_asset WHERE id_exchange=:id_exchange AND use=:use',
                               {'id_exchange': ms.ID_EXCHANGE, 'use': 1})
                main_active = cursor.fetchone()
                cursor.close()
            except sqlite3.Error as err:
                cursor.close()
                main_active = (2,)
                print(f"SELECT from t_asset: {err}")
            funding_wallet = []
            assets_fw = {}
            if cls.exchange not in ('bitfinex', 'huobi'):
                try:
                    res = await cls.send_request(cls.stub.FetchFundingWallet, api_pb2.FetchFundingWalletRequest)
                except asyncio.CancelledError:
                    pass
                except Exception as _ex:
                    logger.warning(f"FetchFundingWallet: {_ex}")
                else:
                    funding_wallet = json_format.MessageToDict(res).get('balances', [])
                for fw in funding_wallet:
                    assets_fw[fw['asset']] = Decimal(fw['free']) + Decimal(fw['locked']) + Decimal(fw['freeze'])
            # Create list of cumulative asset without current pair, from SPOT wallet
            # and all assets from Funding wallet on Binance
            assets = {}
            for balance in balances:
                if cls.exchange != 'bitfinex':
                    total = assets_fw.pop(balance['asset'], Decimal('0.0'))
                else:
                    total = Decimal('0.0')
                if balance['asset'] not in (cls.base_asset, cls.quote_asset) or ms.GRID_ONLY:
                    total += Decimal(balance['free']) + Decimal(balance['locked'])
                assets[balance['asset']] = float(total)
            cursor_analytic = connection_analytic.cursor()
            try:
                cursor_analytic.execute('SELECT id_exchange, currency, value, use, timestamp\
                                         FROM t_asset\
                                         WHERE id_exchange=:id_exchange',
                                        {'id_exchange': ms.ID_EXCHANGE})
                rows = cursor_analytic.fetchall()
                cursor_analytic.close()
            except sqlite3.Error as err:
                rows = []
                print(f"SELECT from t_asset: {err}")
            cursor = connection_analytic.cursor()
            try:
                cursor.execute('BEGIN')
                cursor.execute('DELETE\
                                FROM t_asset\
                                WHERE id_exchange=:id_exchange\
                                and use=:use',
                               {'id_exchange': ms.ID_EXCHANGE, 'use': 0})
                for row in rows:
                    if row[1] in (cls.base_asset, cls.quote_asset) and main_active == (1,):
                        amount = float(assets.pop(row[1], 0))
                        cursor.execute('UPDATE t_asset SET value=:value, timestamp=:timestamp, use=:use\
                                        WHERE id_exchange=:id_exchange\
                                        and currency=:currency',
                                       {'value': amount if ms.GRID_ONLY else 0, 'timestamp': int(time.time()), 'use': 1,
                                        'id_exchange': ms.ID_EXCHANGE, 'currency': row[1]})
                    elif row[3]:
                        # Check used currency from other pair for last update time
                        if time.time() - row[4] > max_use_update:
                            cursor.execute('DELETE FROM t_asset\
                                            WHERE id_exchange=:id_exchange\
                                            and currency=:currency',
                                           {'id_exchange': ms.ID_EXCHANGE, 'currency': row[1]})
                        assets.pop(row[1], None)
                if assets:
                    for key, value in assets.items():
                        use = 1 if key in (cls.base_asset, cls.quote_asset) else 0
                        cursor.execute('INSERT into t_asset values(?, ?, ?, ?, ?)',
                                       (ms.ID_EXCHANGE, key, value, use, int(time.time())))
                if assets_fw:
                    for key, value in assets_fw.items():
                        cursor.execute('INSERT into t_asset values(?, ?, ?, ?, ?)',
                                       (ms.ID_EXCHANGE, key, float(value), 0, int(time.time())))
                cursor.execute('COMMIT')
                cursor.close()
            except sqlite3.Error as err:
                cursor.execute('ROLLBACK')
                cursor.close()
                logger.warning(f"Refresh t_asset: {err}")
        await asyncio.sleep(delay)


async def ask_exit():
    cls = StrategyBase
    if cls.strategy:
        cls.strategy.message_log("Got signal for exit", color=Style.MAGENTA)

        cls.operational_status = False
        await asyncio.sleep(HEARTBEAT)

        if ms.MODE in ('T', 'TC'):
            try:
                await cls.send_request(cls.stub.StopStream, api_pb2.MarketRequest, symbol=cls.symbol)
            except Exception as ex:
                logger.warning(f"ask_exit: {ex}")

            if ms.MODE == 'TC':
                # Save stream data for backtesting
                session_data_handler(cls.strategy)

        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        [task.cancel() for task in tasks]
        print(f"Cancelling {len(tasks)} outstanding tasks")
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            cls.strategy.stop()
        except Exception as _err:
            print(f"ask_exit.strategy.stop: {_err}")
        await cls.channel.close()
        cls.strategy = None
        if ms.MODE in ('T', 'TC') and ms.LAST_STATE_FILE.exists():
            print(f"Current state saved into {ms.LAST_STATE_FILE}")


def session_data_handler(cls):
    """
    Save raw data for back testing and session snapshot for compare.
    :param cls: StrategyBase.strategy
    :return:
    """
    session_root = Path(BACKTEST_PATH, f"{cls.exchange}_{cls.symbol}")
    raw_path = Path(session_root, "raw")
    raw_path.mkdir(parents=True, exist_ok=True)
    # Save ticker
    ds = pd.Series(cls.s_ticker)
    ds.to_pickle(Path(raw_path, TICKER_PKL))
    # Save order_book
    ds = pd.Series(cls.s_order_book)
    ds.to_pickle(Path(raw_path, ORDER_BOOK_PKL))
    # Save klines snapshot
    with open(Path(raw_path, "klines.json"), 'w') as f:
        json.dump(cls.klines, f)
    # Save candles
    for k, v in cls.candles.items():
        ds = pd.Series(v)
        ds.to_pickle(Path(raw_path, f"candles_{k}.pkl"))
    # Save session detail for analytics
    session_data = Path(session_root, "snapshot")
    session_data.mkdir(parents=True, exist_ok=True)
    d_ticker = {k: v['lastPrice'] for k, v in cls.s_ticker.items()}
    ds_ticker = pd.Series(d_ticker).astype(float)
    ds_ticker.index = pd.to_datetime(ds_ticker.index, unit='ms')
    #
    df_grid_sell = pd.DataFrame().from_dict(cls.grid_sell, orient='index')
    df_grid_sell.index = pd.to_datetime(df_grid_sell.index, unit='ms')
    df_grid_buy = pd.DataFrame().from_dict(cls.grid_buy, orient='index')
    df_grid_buy.index = pd.to_datetime(df_grid_buy.index, unit='ms')
    #
    ds_ticker.to_pickle(Path(session_data, TICKER_PKL))
    df_grid_sell.to_pickle(Path(session_data, "sell.pkl"))
    df_grid_buy.to_pickle(Path(session_data, "buy.pkl"))
    #
    copy(ms.PARAMS, Path(session_root, Path(ms.PARAMS).name))

    shutil.make_archive(str(Path(BACKTEST_PATH, f"{session_root}_{datetime.now().strftime('%m%d-%H-%M')}")),
                        'zip',
                        session_root)

    print(f"Stream data for backtesting saved to {session_root}")


async def backtest_data_control():
    """
    Control memory usage and safe saving by predefined timetable
    """
    cls = StrategyBase.strategy
    delay = HEARTBEAT * 300  # 10 min
    ts = time.time()
    while 1:
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        total_used_percent = 100 * float(swap.used + memory.used) / (swap.total + memory.total)
        if time.time() - ts > ms.SAVE_PERIOD or total_used_percent > 70:
            if sc := cls.start_collect:
                cls.start_collect = False
                session_data_handler(cls)
                cls.reset_var()
                cls.start_collect = sc
                ts = time.time()
        await asyncio.sleep(delay)


async def buffered_candle():
    cls = StrategyBase
    cls.Klines.klines_lim = KLINES_LIM
    klines = {}
    klines_from_file = {}
    if ms.MODE == 'S':
        klines_from_file = json.load(open(Path(BACKTEST_PATH, f"{cls.exchange}_{cls.symbol}/raw/klines.json")))
    for i in KLINES_INIT:
        if ms.MODE in ('T', 'TC'):
            try:
                res = await cls.send_request(cls.stub.FetchKlines, api_pb2.FetchKlinesRequest,
                                             symbol=cls.symbol,
                                             interval=i.value,
                                             limit=KLINES_LIM)
            except Exception as ex:
                kline = {}
                logger.warning(f"FetchKlines: {ex}")
            else:
                kline = json_format.MessageToDict(res)
                if ms.MODE == 'TC' and (cls.strategy.start_collect or cls.strategy.start_collect is None):
                    cls.strategy.klines[i.value] = kline
        else:
            kline = klines_from_file.get(i.value, {})

        if candles := kline.get('klines'):
            kline_i = cls.Klines(i.value)
            for candle in candles:
                kline_i.refresh(json.loads(candle))
                # print(f"buffered_candle.candle: {candle}")
            klines[i.value] = kline_i
    if len(klines) == len(KLINES_INIT):
        loop.create_task(on_klines_update(klines))
    else:
        logger.info("Init buffered candle failed. try one else...")
        await asyncio.sleep(random.uniform(1, 5))
        cls.wss_fire_up = True


async def on_klines_update(_klines: {str: StrategyBase.Klines}):
    cls = StrategyBase
    _interval = list(_klines.keys())
    if ms.MODE in ('T', 'TC'):
        try:
            async for candle in cls.for_request(cls.stub.OnKlinesUpdate, api_pb2.FetchKlinesRequest,
                                                symbol=cls.symbol,
                                                interval=json.dumps(_interval)):
                # print(f"on_klines_update: {candle.symbol}, {candle.interval}, candle: {json.loads(candle.candle)[0]}")
                _klines.get(candle.interval).refresh(json.loads(candle.candle))
                if ms.MODE == 'TC' and (cls.strategy.start_collect or cls.strategy.start_collect is None):
                    new_raw = {int(time.time() * 1000): candle.candle}
                    cls.strategy.candles.setdefault(candle.interval, new_raw).update(new_raw)
        except Exception as ex:
            logger.warning(f"Exception on WSS, on_klines_update loop closed: {ex}")
            cls.wss_fire_up = True
    else:
        for i in _interval:
            ds = pd.read_pickle(Path(BACKTEST_PATH, f"{cls.exchange}_{cls.symbol}/raw/candles_{i}.pkl"))
            loop.create_task(aiter_candles(ds, _klines, i))


async def aiter_candles(ds: pd.Series, _klines: {str: StrategyBase.Klines}, _i: str):
    async for row in loop_ds(ds):
        _klines.get(_i).refresh(json.loads(row))
    StrategyBase.strategy.message_log(f"Backtest candles *** {_i} *** timeSeries ended")


async def buffered_funds(print_info: bool = True):
    cls = StrategyBase
    try:
        if ms.MODE in ('T', 'TC'):
            res = await cls.send_request(cls.stub.FetchAccountInformation, api_pb2.OpenClientConnectionId)
            balances = json_format.MessageToDict(res).get('balances', [])
        else:
            balances = cls.strategy.account.funds.get_funds()
    except asyncio.CancelledError:
        pass
    except Exception as _ex:
        logger.warning(f"Exception buffered_funds: {_ex}")
    else:
        # print(f"buffered_funds.balances: {balances}")
        try:
            balance_f = next(item for item in balances if item["asset"] == cls.base_asset)
        except StopIteration:
            balance_f = {'asset': cls.base_asset, 'free': '0.0', 'locked': '0.0'}
        try:
            balance_s = next(item for item in balances if item["asset"] == cls.quote_asset)
        except StopIteration:
            balance_s = {'asset': cls.quote_asset, 'free': '0.0', 'locked': '0.0'}
        funds = {cls.base_asset: {'free': balance_f['free'], 'locked': balance_f['locked']},
                 cls.quote_asset: {'free': balance_s['free'], 'locked': balance_s['locked']}}

        cls.funds = funds
        if print_info:
            print(EQUAL_STR)
            print(f"Base asset balance: {balance_f}")
            print(f"Quote asset balance: {balance_s}")
            print(EQUAL_STR)
        else:
            # print(f"buffered_funds.funds: {cls.funds}")
            funds = {cls.base_asset: FundsEntry(cls.funds[cls.base_asset]),
                     cls.quote_asset: FundsEntry(cls.funds[cls.quote_asset])}
            cls.strategy.on_new_funds(funds)


async def buffered_orders():
    cls = StrategyBase
    exch_orders = []
    diff_id = set()
    restore = False
    while not cls.operational_status:
        try:
            res = await cls.send_request(cls.stub.CheckStream, api_pb2.MarketRequest, symbol=cls.symbol)
        except Exception as ex_1:
            logger.warning(f"Exception on Check WSS: {ex_1}")
        else:
            if res.success:
                cls.operational_status = True
        await asyncio.sleep(HEARTBEAT)
    while cls.operational_status:
        try:
            res = await cls.send_request(cls.stub.CheckStream, api_pb2.MarketRequest, symbol=cls.symbol)
            if res is None or not res.success:
                cls.wss_fire_up = True
                raise UserWarning(f"Not active WSS for {cls.symbol} on {cls.exchange}, restart request sent")

            if cls.last_state:
                cls.strategy.message_log("Trying restore saved state after restart", color=Style.GREEN)
                # Restore StrategyBase class var
                cls.order_id = json.loads(cls.last_state.pop(MS_ORDER_ID,
                                                             str(int(datetime.now().strftime("%S%M")) * 1000)))
                cls.start_time_ms = json.loads(cls.last_state.pop('ms_start_time_ms', str(int(time.time() * 1000))))
                cls.trades = jsonpickle.decode(cls.last_state.pop('ms_trades', '[]'))
                cls.orders = jsonpickle.decode(cls.last_state.pop(MS_ORDERS, '{}'), keys=True)
                #
                cls.strategy.restore_strategy_state(cls.last_state, restore=False)

            if restore:
                cls.strategy.message_log("Trying restore saved state after lost connection to host",
                                         color=Style.GREEN)

            _orders = await cls.send_request(cls.stub.FetchOpenOrders, api_pb2.MarketRequest, symbol=cls.symbol)
            if _orders is None:
                raise UserWarning("Can't fetch open orders")
            StrategyBase.rate_limiter = max(StrategyBase.rate_limiter, _orders.rate_limiter)

            orders = json_format.MessageToDict(_orders).get('items', [])
            for order in orders:
                _id = int(order['orderId'])
                exch_orders.append(_id)
                if (order.get('status') == 'PARTIALLY_FILLED'
                        and order_trades_sum(_id) < Decimal(order['executedQty']).quantize(Decimal("1.01234567"),
                                                                                           rounding=ROUND_FLOOR)):
                    diff_id.add(_id)
            # Missed fill event list
            diff_id.update(set(cls.orders).difference(set(exch_orders)))

            if diff_id:
                cls.strategy.message_log(f"Perhaps was missed event for order(s): {diff_id},"
                                         f" checking it", log_level=LogLevel.WARNING, tlg=False)
                for _id in diff_id:
                    res = await fetch_order(_id, _filled_update_call=True)
                    if res.get('status') == 'CANCELED':
                        await cancel_order_handler(_id, cancel_all=False)

            if cls.last_state:
                cls.strategy.restore_strategy_state(restore=True)

            if cls.last_state and ms.MODE == 'TC' and ms.SAVED_STATE:
                last_state = cls.strategy.save_strategy_state(return_only=True)
                last_state_update(cls, last_state)
                with cls.state_file.open(mode='w') as outfile:
                    json.dump(last_state, outfile, sort_keys=True, indent=4, ensure_ascii=False)
                cls.strategy.start_collect = True
            exch_orders.clear()
            diff_id.clear()
            cls.last_state = None
            restore = False

        except asyncio.CancelledError:
            # print("buffered_orders.Cancelled")
            cls.operational_status = False
        except UserWarning as ex_2:
            cls.strategy.message_log(f"Exception buffered_orders: {ex_2}", log_level=LogLevel.WARNING)
            restore = True
        except grpc.RpcError as ex_3:
            status_code = ex_3.code()
            cls.strategy.message_log(f"Exception buffered_orders: {status_code.name}, {ex_3.details()}",
                                     log_level=LogLevel.WARNING, color=Style.B_RED, tlg=True)
            if status_code == grpc.StatusCode.RESOURCE_EXHAUSTED:
                # Decrease requests frequency
                StrategyBase.rate_limiter += HEARTBEAT
                cls.strategy.message_log(f"RATE_LIMITER set to {StrategyBase.rate_limiter}s",
                                         log_level=LogLevel.WARNING)
                await asyncio.sleep(ORDER_TIMEOUT)
                try:
                    await cls.send_request(cls.stub.ResetRateLimit, api_pb2.OpenClientConnectionId,
                                           rate_limiter=StrategyBase.rate_limiter)
                except Exception as ex_4:
                    logger.warning(f"Exception buffered_orders:ResetRateLimit: {ex_4}")
            else:
                restore = True
        except Exception as ex_5:
            cls.strategy.message_log(f"Exception buffered_orders: {ex_5}\n{traceback.format_exc()}",
                                     log_level=LogLevel.ERROR)
            restore = True
        await asyncio.sleep(StrategyBase.rate_limiter)


async def on_funds_update():
    cls = StrategyBase
    if ms.MODE in ('T', 'TC'):
        try:
            async for _funds in cls.for_request(cls.stub.OnFundsUpdate, api_pb2.OnFundsUpdateRequest,
                                                symbol=cls.symbol,
                                                base_asset=cls.base_asset,
                                                quote_asset=cls.quote_asset):
                funds = json.loads(json.loads(json_format.MessageToJson(_funds))['funds'])
                if funds.get(cls.base_asset) or funds.get(cls.quote_asset):
                    on_funds_update_handler(cls, funds)
        except Exception as ex:
            logger.warning(f"Exception on WSS, on_funds_update loop closed: {ex}")
            cls.wss_fire_up = True
    else:
        funds = {}
        _funds = cls.strategy.account.funds.get_funds()
        [funds.update({d.get('asset'): {'free': d.get('free'), 'locked': d.get('locked')}}) for d in _funds]
        # print(f"on_funds_update.funds: {funds}")
        on_funds_update_handler(cls, funds)


def on_funds_update_handler(cls, funds):
    cls.funds.update(funds)
    funds = {cls.base_asset: FundsEntry(cls.funds[cls.base_asset]),
             cls.quote_asset: FundsEntry(cls.funds[cls.quote_asset])}
    cls.strategy.on_new_funds(funds)
    cls.strategy.get_buffered_funds_last_time = cls.strategy.get_time()


async def on_balance_update():
    cls = StrategyBase
    try:
        async for res in cls.for_request(cls.stub.OnBalanceUpdate, api_pb2.MarketRequest, symbol=cls.symbol):
            _res = json.loads(res.balance)
            if ms.SAVE_TRADE_HISTORY:
                row = ['TRANSFER',
                       _res["event_time"],
                       _res["asset"],
                       _res["balance_delta"],
                       ]
                await save_trade_queue.put(row)
            cls.strategy.on_balance_update(_res)
    except Exception as ex:
        logger.warning(f"Exception on WSS, on_balance_update loop closed: {ex}")
        cls.wss_fire_up = True


async def on_order_update():
    cls = StrategyBase
    try:
        async for event in cls.for_request(cls.stub.OnOrderUpdate, api_pb2.MarketRequest, symbol=cls.symbol):
            # Only for registered orders on own pair
            ed = ast.literal_eval(json.loads(event.result))
            await on_order_update_handler(cls, ed)
    except Exception as ex:
        logger.warning(f"Exception on WSS, on_order_update loop closed: {ex}\n{traceback.format_exc()}")
        cls.wss_fire_up = True


async def on_order_update_handler(cls, ed):
    if (
            cls.symbol != ed['symbol']
            or not cls.order_exist(ed['order_id'])
            or ed['order_status'] not in ('FILLED', 'PARTIALLY_FILLED')
    ):
        return
    if ed['order_status'] == 'FILLED':
        # Remove from orders dict
        remove_from_orders_lists([ed['order_id']])
    elif ed['order_status'] == 'PARTIALLY_FILLED':
        # Update order in orders dict
        _order = {
            "orderId": ed['order_id'],
            "price": ed['order_price'],
            "origQty": ed['order_quantity'],
            "executedQty": ed['cumulative_filled_quantity'],
            "type": ed['order_type'],
            "side": ed['side'],
            "transactTime": ed['transaction_time'],
        }
        cls.orders |= {ed['order_id']: Order(_order)}

    if trade_not_exist(ed['order_id'], ed['trade_id']):
        _on_order_update_handler_ext(ed, cls)
        if ms.SAVE_TRADE_HISTORY:
            row = ["TRADE",
                   ed["transaction_time"],
                   ed["side"],
                   ed["order_id"],
                   ed["client_order_id"],
                   ed["trade_id"],
                   ed["order_quantity"],
                   ed["order_price"],
                   ed["cumulative_filled_quantity"],
                   ed["quote_asset_transacted"],
                   ed["last_executed_quantity"],
                   ed["last_executed_price"],
                   ]
            await save_trade_queue.put(row)


def _on_order_update_handler_ext(ed, cls):
    trade = {
        "qty": ed['last_executed_quantity'],
        "isBuyer": ed['side'] == 'BUY',
        "id": ed['trade_id'],
        "orderId": ed['order_id'],
        "price": ed['last_executed_price'],
        "time": ed['transaction_time'],
    }
    #  Append to trades list
    cls.trades.append(PrivateTrade(trade))
    # noinspection PyStatementEffect
    cls.trades[-TRADES_LIST_LIMIT:]
    cumulative_quantity = Decimal(ed['cumulative_filled_quantity'])
    saved_filled_quantity = order_trades_sum(ed['order_id'])
    if ed['order_status'] == 'FILLED' and saved_filled_quantity != cumulative_quantity:
        cls.strategy.message_log(f"Order: {ed['order_id']} was missed partially filling event",
                                 log_level=LogLevel.INFO)
        # Remove trades associated with order from list
        remove_from_trades_lists(ed['order_id'])
        # Update current trade
        price = str(Decimal(ed['quote_asset_transacted']) / cumulative_quantity)
        trade |= {"qty": ed['cumulative_filled_quantity'], "price": price}
        # cls.strategy.message_log(f"on_order_update.trade: {trade}",
        #                                  log_level=LogLevel.DEBUG, color=ms.Style.YELLOW)
        # Append to list
        cls.trades.append(PrivateTrade(trade))
    cls.strategy.on_order_update(OrderUpdate(ed))
    if ms.MODE == 'TC' and cls.strategy.start_collect:
        cls.strategy.s_ticker[list(cls.strategy.s_ticker)[-1]].update({'lastPrice': ed['last_executed_price']})
        cls.strategy.open_orders_snapshot()


async def create_limit_order(_id: int, buy: bool, amount: str, price: str) -> None:
    cls = StrategyBase
    cls.wait_order_id.append(_id)
    _fetch_order = False
    try:
        if ms.MODE in ('T', 'TC'):
            ts = time.time()
            res = await cls.send_request(cls.stub.CreateLimitOrder, api_pb2.CreateLimitOrderRequest,
                                         symbol=cls.symbol,
                                         buy_side=buy,
                                         quantity=amount,
                                         price=price,
                                         new_client_order_id=_id)
            result = json_format.MessageToDict(res)
            cls.delay_ordering_s = time.time() - ts
        else:
            await asyncio.sleep(cls.delay_ordering_s / ms.XTIME)
            result = cls.strategy.account.create_order(symbol=cls.symbol,
                                                       client_order_id=_id,
                                                       buy=buy,
                                                       amount=amount,
                                                       price=price,
                                                       lt=int(cls.strategy.local_time() * 1000))
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error
    except grpc.RpcError as ex:
        _fetch_order = True
        cls.strategy.message_log(f"Exception creating order {_id}: {ex.code().name}, {ex.details()}")
    except Exception as _ex:
        _fetch_order = True
        cls.strategy.message_log(f"Exception creating order {_id}: {_ex}")
    else:
        if result:
            await create_order_handler(_id, result)
        else:
            _fetch_order = True
    finally:
        if _fetch_order:
            await asyncio.sleep(HEARTBEAT)
            res = await fetch_order(0, str(_id), _filled_update_call=True)
            if res.get('status') in ('NEW', 'PARTIALLY_FILLED', 'FILLED'):
                await create_order_handler(_id, res)


async def create_order_handler(_id, result):
    cls = StrategyBase
    order = Order(result)
    if cls.orders.get(order.id) is None or (result.get('status') == 'FILLED' and order_trades_sum(_id) == 0):
        cls.strategy.message_log(
            f"Order placed {order.id}({result.get('clientOrderId') or _id}) for {result.get('side')}"
            f" {any2str(order.amount)} by {any2str(order.price)}"
            f" Remaining amount {any2str(order.remaining_amount)}",
            color=Style.GREEN)
        orig_qty = Decimal(result['origQty'])
        executed_qty = Decimal(result['executedQty'])
        cummulative_quote_qty = Decimal(result['cummulativeQuoteQty'])
        if executed_qty > 0 and ms.MODE == 'TC' and cls.strategy.start_collect:
            cls.strategy.s_ticker[list(cls.strategy.s_ticker)[-1]].update(
                {'lastPrice': str(cummulative_quote_qty / executed_qty)}
            )
        if executed_qty < orig_qty:
            cls.orders[order.id] = order
        elif ms.SAVE_TRADE_HISTORY:
            row = ["TRADE_BY_MARKET",
                   int(time.time() * 1000),
                   result["side"],
                   result["orderId"],
                   result["clientOrderId"],
                   '-1',
                   result["origQty"],
                   result["price"],
                   result["executedQty"],
                   result["cummulativeQuoteQty"],
                   result["executedQty"],
                   result["price"],
                   ]
            await save_trade_queue.put(row)
        if ms.MODE == 'TC' and cls.strategy.start_collect:
            cls.strategy.open_orders_snapshot()
        elif ms.MODE == 'S':
            await on_funds_update()
        cls.strategy.on_place_order_success(_id, order)

    if _id in cls.wait_order_id:
        cls.wait_order_id.remove(_id)


async def place_limit_order_timeout(_id):
    cls = StrategyBase
    await asyncio.sleep(ORDER_TIMEOUT)
    if _id in cls.wait_order_id:
        cls.wait_order_id.remove(_id)
        cls.strategy.on_place_order_error_string(_id, 'Place order timeout')


async def cancel_order_call(_id: int, cancel_all: bool, count=0):
    cls = StrategyBase
    if count == 0:
        cls.canceled_order_id.append(_id)
    elif _id in cls.canceled_order_id:
        cls.canceled_order_id.remove(_id)
    _fetch_order = False
    try:
        if ms.MODE in ('T', 'TC'):
            if cancel_all:
                if cls.bulk_orders_cancel.get(_id) is None:
                    res = await cls.send_request(cls.stub.CancelAllOrders, api_pb2.MarketRequest, symbol=cls.symbol)
                    [cls.bulk_orders_cancel.update({v['orderId']: v}) for v in ast.literal_eval(json.loads(res.result))]
                result = cls.bulk_orders_cancel.pop(_id, None)
            else:
                res = await cls.send_request(cls.stub.CancelOrder, api_pb2.CancelOrderRequest,
                                             symbol=cls.symbol,
                                             order_id=_id)
                result = json_format.MessageToDict(res)
        else:
            result = cls.strategy.account.cancel_order(order_id=_id, ts=int(cls.strategy.local_time() * 1000))
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error.
    except grpc.RpcError as ex:
        _fetch_order = True
        cls.strategy.message_log(f"Exception on cancel order {_id}: {ex.code().name}, {ex.details()}")
    except Exception as _ex:
        _fetch_order = True
        cls.strategy.message_log(f"Exception on cancel order call for {_id}: {_ex}")
        logger.debug(f"Exception traceback: {traceback.format_exc()}")
    else:
        # print(f"cancel_order_call.result: {result}")
        # Remove from orders lists
        if result and result.get('status') == 'CANCELED':
            await cancel_order_handler(_id, cancel_all)
        else:
            cls.strategy.message_log(f"Cancel order {_id}: Warning, not result getting")
            _fetch_order = True
    finally:
        if _fetch_order:
            await asyncio.sleep(HEARTBEAT)
            res = await fetch_order(_id)
            if res.get('status') == 'CANCELED':
                await cancel_order_handler(_id, cancel_all)
            elif not res or res.get('status') in ('NEW', 'PARTIALLY_FILLED'):
                await asyncio.sleep(HEARTBEAT * count)
                if count <= TRY_LIMIT:
                    await cancel_order_call(_id, cancel_all, count+1)
                cls.strategy.on_cancel_order_error_string(_id, 'Cancel order timeout')


async def cancel_order_handler(_id, cancel_all):
    cls = StrategyBase
    if _id in cls.canceled_order_id:
        cls.canceled_order_id.remove(_id)
    remove_from_orders_lists([_id])
    cls.strategy.message_log(f"Cancel order {_id} success", color=Style.GREEN)
    cls.strategy.on_cancel_order_success(_id, cancel_all=cancel_all)
    if ms.MODE == 'TC' and cls.strategy.start_collect:
        cls.strategy.open_orders_snapshot()
    elif ms.MODE == 'S':
        await on_funds_update()


async def cancel_order_timeout(_id):
    cls = StrategyBase
    await asyncio.sleep(ORDER_TIMEOUT)
    if _id in cls.canceled_order_id:
        cls.canceled_order_id.remove(_id)
        cls.strategy.on_cancel_order_error_string(_id, 'Cancel order timeout')


async def fetch_order(_id: int, _client_order_id: str = None, _filled_update_call=False):
    cls = StrategyBase
    try:
        res = await cls.send_request(cls.stub.FetchOrder, api_pb2.FetchOrderRequest,
                                     symbol=cls.symbol,
                                     order_id=_id,
                                     client_order_id=_client_order_id,
                                     filled_update_call=_filled_update_call)
        result = json_format.MessageToDict(res)
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error.
    except Exception as _ex:
        cls.strategy.message_log(f"Exception in fetch_order: {_ex}", log_level=LogLevel.ERROR)
        return {}
    else:
        cls.strategy.message_log(f"For order {_id}({_client_order_id}) fetched status is {result.get('status')}",
                                 log_level=LogLevel.INFO, color=Style.GREEN)
        if result:
            return result
        cls.strategy.message_log(f"Can't get status for order {_id}({_client_order_id})",
                                 log_level=LogLevel.WARNING)
        return {}


async def transfer2master(symbol: str, amount: str):
    cls = StrategyBase
    try:
        res = await cls.send_request(cls.stub.TransferToMaster,
                                     api_pb2.MarketRequest,
                                     symbol=symbol,
                                     amount=amount)
    except asyncio.CancelledError:
        pass  # Task cancellation should not be logged as an error
    except grpc.RpcError as ex:
        status_code = ex.code()
        cls.strategy.message_log(f"Exception transfer {symbol} to main account: {status_code.name}, {ex.details()}")
    except Exception as _ex:
        cls.strategy.message_log(f"Exception transfer {symbol} to main account: {_ex}")
    else:
        if res.success:
            cls.strategy.message_log(f"Sent {amount} {symbol} to main account", log_level=LogLevel.INFO)
        else:
            cls.strategy.message_log(f"Not sent {amount} {symbol} to main account\n,{res.result}",
                                     log_level=LogLevel.ERROR)


def remove_from_orders_lists(_order_id_list: []) -> None:
    cls = StrategyBase
    [cls.orders.pop(i, None) for i in _order_id_list]


def remove_from_trades_lists(_order_id) -> None:
    cls = StrategyBase
    # print(f"remove_from_trades_lists._order_id: {_order_id}")
    cls.trades[:] = [i for i in cls.trades if i.order_id != _order_id]


async def loop_ds(ds, tik=False):
    """
    Pandas time Series asynchronous generator with delay (real time/XTIME) multiplier
    :param tik: True - update local time
    :param ds: pandas time Series object
    :return: next row
    """
    cls = StrategyBase
    while not cls.strategy.start_collect:
        await asyncio.sleep(0.1)

    index_prev = 0
    for index, row in ds.items():
        delay = (index - index_prev) if index_prev else 0
        index_prev = index
        if tik:
            cls.strategy.time_operational['new'] = index / 1000
        await asyncio.sleep(delay / (1000 * ms.XTIME))
        yield row


async def on_ticker_update():
    """
    row = {'openPrice': '26923.97000000', 'lastPrice': '26882.51000000', 'closeTime': 1684572464013}
    :return:
    """
    cls = StrategyBase
    if ms.MODE in ('T', 'TC'):
        try:
            async for ticker in cls.for_request(cls.stub.OnTickerUpdate, api_pb2.MarketRequest, symbol=cls.symbol):
                ticker_24h = {'openPrice': ticker.open_price,
                              'lastPrice': ticker.close_price,
                              'closeTime': ticker.event_time}
                cls.ticker = ticker_24h
                # print(f"on_ticker_update.ticker_24h: {ticker_24h}")
                cls.strategy.on_new_ticker(Ticker(cls.ticker))
                #
                if ms.MODE == 'TC' and cls.strategy.start_collect:
                    # print(f"on_ticker_update.ticker_24h: {ticker_24h}")
                    ticker_24h['delay'] = cls.delay_ordering_s
                    cls.strategy.s_ticker.update({int(time.time() * 1000): ticker_24h})
                    cls.strategy.open_orders_snapshot()
        except Exception as ex:
            logger.warning(f"Exception on WSS, on_ticker_update loop closed: {ex}")
            cls.wss_fire_up = True
    else:
        pbar = tqdm(total=cls.backtest['ticker'].count())
        async for row in loop_ds(cls.backtest['ticker'], tik=True):
            cls.delay_ordering_s = row.pop('delay', 0)
            cls.ticker = row
            cls.strategy.on_new_ticker(Ticker(row))
            res = cls.strategy.account.on_ticker_update(row, int(cls.strategy.local_time() * 1000))
            for _res in res:
                await on_order_update_handler(cls, _res)
                await on_funds_update()
            pbar.update()
        pbar.close()
        cls.strategy.message_log("Backtest *** ticker *** timeSeries ended")
        back_test_handler(cls)


def back_test_handler(cls):
    # Test result handler
    test_time = datetime.utcnow() - cls.strategy.cycle_time
    original_time = (cls.backtest['ticker'].index.max() - cls.backtest['ticker'].index.min()) / 1000
    original_time = timedelta(seconds=original_time)
    print(f"Original time: {original_time}, test time: {test_time}, x = {original_time / test_time:.2f}")
    if ms.SAVE_DS:
        _back_test_handler_ext(cls)
    s_profit = session_result['profit'] = f"{cls.strategy.get_sum_profit()}"
    s_free = session_result['free'] = f"{cls.strategy.get_free_assets(mode='free', backtest=True)[2]}"
    print(f"Session profit: {s_profit}, free: {s_free}, total: {float(s_profit) + float(s_free)}")
    loop.stop()


def _back_test_handler_ext(cls):
    # Save test data
    session_path = Path(BACKTEST_PATH,
                        f"{cls.exchange}_{cls.symbol}_{datetime.now().strftime('%m%d-%H-%M-%S')}")
    session_path.mkdir(parents=True)
    ds_ticker = pd.Series(cls.strategy.account.ticker).astype(float)
    ds_ticker.index = pd.to_datetime(ds_ticker.index, unit='ms')
    df_grid_sell = pd.DataFrame().from_dict(cls.strategy.account.grid_sell, orient='index').astype(float)
    df_grid_sell.index = pd.to_datetime(df_grid_sell.index, unit='ms')
    df_grid_buy = pd.DataFrame().from_dict(cls.strategy.account.grid_buy, orient='index').astype(float)
    df_grid_buy.index = pd.to_datetime(df_grid_buy.index, unit='ms')
    #
    ds_ticker.to_pickle(Path(session_path, TICKER_PKL))
    df_grid_sell.to_pickle(Path(session_path, "sell.pkl"))
    df_grid_buy.to_pickle(Path(session_path, "buy.pkl"))
    copy(ms.PARAMS, Path(session_path, Path(ms.PARAMS).name))
    print(f"Session data saved to: {session_path}")


def order_book_prepare(_order_book: {}) -> {}:
    order_book = json_format.MessageToDict(_order_book)
    order_book_bids = order_book.pop('bids', [])
    order_book_asks = order_book.pop('asks', [])
    _bids = [json.loads(bid) for bid in order_book_bids]
    _asks = [json.loads(ask) for ask in order_book_asks]
    order_book.update({'bids': _bids})
    order_book.update({'asks': _asks})
    return order_book


async def on_order_book_update():
    cls = StrategyBase
    if ms.MODE in ('T', 'TC'):
        try:
            async for _order_book in cls.for_request(cls.stub.OnOrderBookUpdate, api_pb2.MarketRequest,
                                                     symbol=cls.symbol):
                order_book = order_book_prepare(_order_book)
                # print(f"on_order_book_update.order_book: {order_book}")
                cls.order_book = order_book
                cls.strategy.on_new_order_book(OrderBook(cls.order_book))
                if ms.MODE == 'TC' and cls.strategy.start_collect:
                    cls.strategy.s_order_book.update({int(time.time() * 1000): order_book})
        except Exception as ex:
            logger.warning(f"Exception on WSS, on_order_book_update loop closed: {ex}")
            cls.wss_fire_up = True
    else:
        async for row in loop_ds(cls.backtest['order_book']):
            cls.order_book = row
            cls.strategy.on_new_order_book(OrderBook(cls.order_book))
        cls.strategy.message_log("Backtest *** order_book *** timeSeries ended")


def load_file(name: Path) -> {}:
    _res = {}
    if name.exists():
        try:
            with name.open() as state_file:
                _last_state = json.load(state_file)
        except json.JSONDecodeError as er:
            print(f"Exception on decode last state file: {er}")
        else:
            if _last_state.get('ms_start_time_ms', None):
                _res = _last_state
    return _res


def load_last_state() -> {}:
    res = {}
    if ms.LAST_STATE_FILE.exists():
        res = load_file(ms.LAST_STATE_FILE)
        if not res:
            print("Can't load last state, try load previous saved state")
            res = load_file(ms.LAST_STATE_FILE.with_suffix('.prev'))
        if res:
            with ms.LAST_STATE_FILE.with_suffix('.bak').open(mode='w') as outfile:
                json.dump(res, outfile, sort_keys=True, indent=4, ensure_ascii=False)
    return res


async def wss_declare():
    # Market stream
    loop.create_task(on_ticker_update())
    await buffered_candle()
    loop.create_task(on_order_book_update())
    if ms.MODE in ('T', 'TC'):
        # User Stream
        loop.create_task(on_funds_update())
        loop.create_task(on_order_update())
        loop.create_task(on_balance_update())
        if ms.MODE == 'TC':
            loop.create_task(backtest_data_control())


async def wss_init(update_max_queue_size=False):
    cls = StrategyBase
    cls.strategy.message_log(f"Init WSS, client_id: {cls.client_id}")
    if cls.client_id:
        await wss_declare()
        # WSS start
        '''
        market_stream_count=5
        These values directly depend on the number of market ws streams used in the strategy and declared above
        '''
        try:
            await cls.send_request(cls.stub.StartStream,
                                   api_pb2.StartStreamRequest,
                                   symbol=cls.symbol,
                                   market_stream_count=5,
                                   update_max_queue_size=update_max_queue_size)
            cls.wss_fire_up = False
        except UserWarning:
            cls.strategy.message_log("Start WSS failed, retry", log_level=LogLevel.WARNING)
            cls.wss_fire_up = True
    else:
        cls.strategy.message_log("Init WSS failed, retry", log_level=LogLevel.WARNING)
        await asyncio.sleep(random.randint(HEARTBEAT, HEARTBEAT * 5))
        cls.wss_fire_up = True


def update_class_var(_session):
    cls = StrategyBase
    cls.client = _session.client
    cls.stub = _session.stub
    cls.channel = _session.channel
    cls.client_id = _session.client.client_id if _session.client else None
    cls.exchange = _session.client.exchange if _session.client else None
    cls.send_request = _session.send_request
    cls.for_request = _session.for_request


def restore_state_before_backtesting(cls):
    saved_state = load_file(cls.state_file)
    cls.order_id = json.loads(saved_state.pop(MS_ORDER_ID, "0"))
    cls.trades = jsonpickle.decode(saved_state.pop('ms_trades', '[]'))
    cls.orders = jsonpickle.decode(saved_state.pop(MS_ORDERS, '{}'))
    orders = json.loads(saved_state.get('orders'))
    # Restore initial state
    cls.strategy.cycle_buy = json.loads(saved_state.get('cycle_buy'))
    cls.strategy.reverse = json.loads(saved_state.get('reverse'))
    cls.strategy.deposit_first = ms.f2d(json.loads(saved_state.get('deposit_first')))
    cls.strategy.deposit_second = ms.f2d(json.loads(saved_state.get('deposit_second')))
    if cls.strategy.reverse:
        cls.strategy.initial_reverse_first = ms.f2d(json.loads(saved_state.get('initial_reverse_first')))
        cls.strategy.initial_reverse_second = ms.f2d(json.loads(saved_state.get('initial_reverse_second')))
        cls.strategy.account.funds.base = {'asset': cls.base_asset,
                                           'free': cls.strategy.initial_reverse_first,
                                           'locked': '0.0'}
        cls.strategy.account.funds.quote = {'asset': cls.quote_asset,
                                            'free': cls.strategy.initial_reverse_second,
                                            'locked': '0.0'}
    elif cls.strategy.cycle_buy:
        cls.strategy.initial_second = cls.strategy.deposit_second
    else:
        cls.strategy.initial_first = cls.strategy.deposit_first
    cls.strategy.account.restore_state(cls.symbol, cls.start_time_ms, orders)
    cls.strategy.last_shift_time = json.loads(saved_state.get('last_shift_time')) or cls.strategy.local_time()
    cls.strategy.order_q = json.loads(saved_state.get('order_q'))
    cls.strategy.orders_grid.restore(json.loads(saved_state.get('orders')))
    cls.strategy.orders_hold.restore(json.loads(saved_state.get('orders_hold')))
    cls.strategy.orders_save.restore(json.loads(saved_state.get('orders_save')))
    cls.strategy.over_price = json.loads(saved_state.get('over_price'))
    cls.strategy.part_amount = eval(json.loads(saved_state.get('part_amount')))
    cls.strategy.reverse_hold = json.loads(saved_state.get('reverse_hold'))
    cls.strategy.reverse_init_amount = ms.f2d(json.loads(saved_state.get('reverse_init_amount')))
    cls.strategy.reverse_price = json.loads(saved_state.get('reverse_price'))
    cls.strategy.reverse_target_amount = ms.f2d(json.loads(saved_state.get('reverse_target_amount')))
    cls.strategy.shift_grid_threshold = ms.f2d(json.loads(saved_state.get('shift_grid_threshold')))
    cls.strategy.sum_amount_first = ms.f2d(json.loads(saved_state.get('sum_amount_first')))
    cls.strategy.sum_amount_second = ms.f2d(json.loads(saved_state.get('sum_amount_second')))
    cls.strategy.tp_amount = ms.f2d(json.loads(saved_state.get('tp_amount')))
    cls.strategy.tp_order_id = json.loads(saved_state.get('tp_order_id'))
    cls.strategy.tp_part_amount_first = ms.f2d(json.loads(saved_state.get('tp_part_amount_first')))
    cls.strategy.tp_part_amount_second = ms.f2d(json.loads(saved_state.get('tp_part_amount_second')))
    cls.strategy.tp_target = ms.f2d(json.loads(saved_state.get('tp_target')))
    cls.strategy.tp_order = eval(json.loads(saved_state.get('tp_order')))
    cls.strategy.tp_wait_id = json.loads(saved_state.get('tp_wait_id'))
    # Restore TP order
    tp_order = [
        {"id": cls.strategy.tp_order_id,
         "buy": cls.strategy.tp_order[0],
         "amount": cls.strategy.tp_order[1],
         "price": cls.strategy.tp_order[2]}
    ]
    cls.strategy.account.restore_state(
        cls.symbol,
        cls.start_time_ms,
        tp_order,
        tp=(cls.strategy.sum_amount_first, cls.strategy.sum_amount_second)
    )
    cls.strategy.start_collect = True


async def main(_symbol):
    cls = StrategyBase
    cls.strategy = ms.Strategy()
    restore_state = None
    last_state = {}
    try:
        if cls.session is None:
            cls.symbol = _symbol
            if len(ms.EXCHANGE) > ms.ID_EXCHANGE:
                account_name = ms.EXCHANGE[ms.ID_EXCHANGE]
            else:
                print(f"ID_EXCHANGE = {ms.ID_EXCHANGE} not in list. See readme 'Add new exchange'")
                raise SystemExit(1)
            print(f"main.account_name: {account_name}")  # lgtm [py/clear-text-logging-sensitive-data]
            session = Trade(channel_options=CHANNEL_OPTIONS,
                            account_name=account_name,
                            rate_limiter=StrategyBase.rate_limiter,
                            symbol=_symbol)
            #
            cls.session = session
            #
            await session.get_client()
            update_class_var(session)
            send_request = session.send_request
            print(f"main.exchange: {cls.exchange}")
            print(f"main.client_id: {cls.client_id}")
            print(f"main.srv_version: {session.client.srv_version}")
            #
            if ms.MODE in ('T', 'TC'):
                # Check and Cancel ALL ACTIVE ORDER
                active_orders = None
                try:
                    _active_orders = await send_request(cls.stub.FetchOpenOrders, api_pb2.MarketRequest, symbol=_symbol)
                except Exception as ex:
                    print(f"Can't get active orders: {ex}")
                else:
                    active_orders = json_format.MessageToDict(_active_orders).get('items', [])
                    # print(f"main.active_orders: {active_orders}")
                # Try load last strategy state from saved files
                last_state = load_last_state()
                restore_state = bool(last_state)
                print(f"main.restore_state: {restore_state}")
                if CANCEL_ALL_ORDERS and active_orders and not ms.LOAD_LAST_STATE:
                    answer = input('Are you want cancel all active order for this pair? Y:\n')
                    if answer.lower() == 'y':
                        restore_state = False
                        try:
                            res = await send_request(cls.stub.CancelAllOrders, api_pb2.MarketRequest, symbol=_symbol)
                            cancel_orders = ast.literal_eval(json.loads(res.result))
                            print('Before start was canceled orders:')
                            for i in cancel_orders:
                                print(f"Order:{i['orderId']}, side:{i['side']},"
                                      f" amount:{i['origQty']}, price:{i['price']}, status:{i['status']}")
                            print(EQUAL_STR)
                        except asyncio.CancelledError:
                            pass  # Task cancellation should not be logged as an error.
                        except grpc.RpcError as ex:
                            status_code = ex.code()
                            print(f"Exception on cancel All order: {status_code.name}, {ex.details()}")
            # Init section
            _exchange_info_symbol = await send_request(cls.stub.FetchExchangeInfoSymbol,
                                                       api_pb2.MarketRequest,
                                                       symbol=_symbol)
            exchange_info_symbol = json_format.MessageToDict(_exchange_info_symbol)
            # print("\n".join(f"{k}\t{v}" for k, v in exchange_info_symbol.items()))
            filters = exchange_info_symbol.get('filters')
            for _filter in filters:
                print(f"{filters.get(_filter).pop('filterType')}: {filters.get(_filter)}")
            # init Strategy class var
            cls.info_symbol = exchange_info_symbol
            cls.tcm = TradingCapabilityManager(exchange_info_symbol, ms.PRICE_LIMIT_RULES)
            cls.base_asset = exchange_info_symbol.get('baseAsset')
            cls.quote_asset = exchange_info_symbol.get('quoteAsset')
            if ms.MODE in ('T', 'TC'):
                # region Get and processing Order book
                _order_book = await cls.send_request(cls.stub.FetchOrderBook, api_pb2.MarketRequest, symbol=_symbol)
                order_book = order_book_prepare(_order_book)
                if not order_book['bids'] or not order_book['asks']:
                    _price = await cls.send_request(cls.stub.FetchSymbolPriceTicker, api_pb2.MarketRequest,
                                                    symbol=_symbol)
                    price = json_format.MessageToDict(_price)
                    print(f"Not bids or asks for pair {price.get('symbol')}, last known price is {price.get('price')}")
                    amount = exchange_info_symbol['filters']['lotSize']['minQty']
                    order_book['bids'] = order_book['bids'] or [[price['price'], amount]]
                    order_book['asks'] = order_book['asks'] or [[price['price'], amount]]
                cls.order_book = order_book
                # endregion
                _ticker = await cls.send_request(cls.stub.FetchTickerPriceChangeStatistics,
                                                 api_pb2.MarketRequest,
                                                 symbol=_symbol)
                cls.ticker = json_format.MessageToDict(_ticker)
                # print(f"main.ticker: {cls.ticker}")
                loop.create_task(save_asset())
            #
            if ms.MODE == 'TC':
                BACKTEST_PATH.mkdir(parents=True, exist_ok=True)
            #
            if ms.MODE in ('TC', 'S') and ms.SAVED_STATE:
                cls.session_root = Path(BACKTEST_PATH, f"{cls.exchange}_{cls.symbol}")
                cls.state_file = Path(cls.session_root, "saved_state.json")
                #
                if ms.MODE == 'TC':
                    cls.session_root.mkdir(parents=True, exist_ok=True)
        #
        else:
            # Init class atr for reuse in next backtest cycle
            cls.reset_class_var()
        #
        if ms.MODE == 'S':
            cls.strategy.account.funds.base = {'asset': cls.base_asset,
                                               'free': f"{ms.AMOUNT_FIRST}",
                                               'locked': '0.0'}
            cls.strategy.account.funds.quote = {'asset': cls.quote_asset,
                                                'free': f"{ms.AMOUNT_SECOND}",
                                                'locked': '0.0'}
            cls.strategy.account.fee_maker = ms.FEE_MAKER
            cls.strategy.account.fee_taker = ms.FEE_TAKER
            #
            cls.backtest['ticker'] = pd.read_pickle(Path(BACKTEST_PATH,
                                                         f"{cls.exchange}_{cls.symbol}/raw/{TICKER_PKL}"))
            cls.backtest['order_book'] = pd.read_pickle(Path(BACKTEST_PATH,
                                                             f"{cls.exchange}_{cls.symbol}/raw/{ORDER_BOOK_PKL}"))
            cls.ticker = cls.backtest['ticker'].iat[0]
            cls.order_book = cls.backtest['order_book'].iat[0]
        #
        await buffered_funds()
        answer = str()
        restored = True
        if restore_state:
            if last_state.get("command", None) == '"stopped"':
                input('Saved state was "stopped". Press Enter for continue or Ctrl-Z for Cancel\n')
                last_state["command"] = 'null'
            if not ms.LOAD_LAST_STATE:
                answer = input('Restore saved state after restart? Y:\n')
            if ms.LOAD_LAST_STATE or answer.lower() == 'y':
                cls.last_state = last_state
                try:
                    await wss_init()
                    cls.strategy.init(check_funds=False)
                except Exception as ex:
                    print(f"Strategy init error: {ex}")
                    restored = False
        if ms.MODE in ('T', 'TC'):
            loop.create_task(buffered_orders())
        if not restore_state or (not ms.LOAD_LAST_STATE and answer.lower() != 'y'):
            if ms.MODE in ('T', 'TC'):
                cls.strategy.init()
                input('Press Enter for Start or Ctrl-Z for Cancel\n')
                await wss_init()
                cls.strategy.start()
            else:
                if ms.SAVED_STATE and cls.state_file.exists():
                    cls.strategy.init(check_funds=False)
                else:
                    cls.strategy.init()
                await wss_declare()
                # Set initial local time from backtest data
                cls.strategy.time_operational['new'] = cls.backtest['ticker'].index[0] / 1000
                cls.strategy.get_buffered_funds_last_time = cls.strategy.get_time()
                cls.start_time_ms = int(cls.strategy.local_time() * 1000)
                cls.strategy.cycle_time = datetime.utcnow()

                if ms.SAVED_STATE and cls.state_file.exists():
                    restore_state_before_backtesting(cls)
                elif not ms.SAVED_STATE:
                    cls.strategy.start()
                else:
                    print("Can't load saved state")
        if restored:
            loop.create_task(heartbeat(cls.session))
            loop.create_task(save_to_csv())
    except (KeyboardInterrupt, SystemExit):
        # noinspection PyProtectedMember, PyUnresolvedReferences
        os._exit(1)
