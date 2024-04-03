import logging
from decimal import Decimal

from hummingbot.connector.connector_base import ConnectorBase, Union
from hummingbot.connector.utils import split_hb_trading_pair
from hummingbot.core.data_type.common import OrderType, PriceType, TradeType
from hummingbot.core.data_type.order_candidate import OrderCandidate
from hummingbot.core.event.events import BuyOrderCompletedEvent, MarketOrderFailureEvent, SellOrderCompletedEvent
from hummingbot.core.rate_oracle.rate_oracle import RateOracle
from hummingbot.logger import HummingbotLogger
from hummingbot.smart_components.executors.executor_base import ExecutorBase
from hummingbot.smart_components.executors.xemm_executor.data_types import XEMMExecutorConfig
from hummingbot.smart_components.models.base import SmartComponentStatus
from hummingbot.smart_components.models.executors import CloseType, TrackedOrder
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase


class XEMMExecutor(ExecutorBase):
    _logger = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._logger is None:
            cls._logger = logging.getLogger(__name__)
        return cls._logger

    @staticmethod
    def _are_tokens_interchangeable(first_token: str, second_token: str):
        interchangeable_tokens = [
            {"WETH", "ETH"},
            {"WBTC", "BTC"},
            {"WBNB", "BNB"},
            {"WMATIC", "MATIC"},
            {"WAVAX", "AVAX"},
            {"WONE", "ONE"},
        ]
        same_token_condition = first_token == second_token
        tokens_interchangeable_condition = any(({first_token, second_token} <= interchangeable_pair
                                                for interchangeable_pair
                                                in interchangeable_tokens))
        # for now, we will consider all the stablecoins interchangeable
        stable_coins_condition = "USD" in first_token and "USD" in second_token
        return same_token_condition or tokens_interchangeable_condition or stable_coins_condition

    def is_arbitrage_valid(self, pair1, pair2):
        base_asset1, quote_asset1 = split_hb_trading_pair(pair1)
        base_asset2, quote_asset2 = split_hb_trading_pair(pair2)
        return self._are_tokens_interchangeable(base_asset1, base_asset2) and \
            self._are_tokens_interchangeable(quote_asset1, quote_asset2)

    def __init__(self, strategy: ScriptStrategyBase, config: XEMMExecutorConfig, update_interval: float = 1.0,
                 max_retries: int = 10):
        if not self.is_arbitrage_valid(pair1=config.buying_market.trading_pair,
                                       pair2=config.selling_market.trading_pair):
            raise Exception("XEMM is not valid since the trading pairs are not interchangeable.")
        self.config = config
        if config.maker_side == TradeType.BUY:
            self.maker_connector = config.buying_market.connector_name
            self.maker_trading_pair = config.buying_market.trading_pair
            self.maker_order_side = TradeType.BUY
            self.taker_connector = config.selling_market.connector_name
            self.taker_trading_pair = config.selling_market.trading_pair
            self.taker_order_side = TradeType.SELL
        else:
            self.maker_connector = config.selling_market.connector_name
            self.maker_trading_pair = config.selling_market.trading_pair
            self.maker_order_side = TradeType.SELL
            self.taker_connector = config.buying_market.connector_name
            self.taker_trading_pair = config.buying_market.trading_pair
            self.taker_order_side = TradeType.BUY
        self._taker_result_price = Decimal("1")
        self._maker_target_price = Decimal("1")
        self._tx_cost = Decimal("1")
        self._tx_cost_pct = Decimal("1")
        self.maker_order = None
        self.taker_order = None
        self.failed_orders = []
        self._current_retries = 0
        self._max_retries = max_retries
        super().__init__(strategy=strategy,
                         connectors=[config.buying_market.connector_name, config.selling_market.connector_name],
                         config=config, update_interval=update_interval)

    def validate_sufficient_balance(self):
        mid_price = self.get_price(self.maker_connector, self.maker_trading_pair,
                                   price_type=PriceType.MidPrice)
        maker_order_candidate = OrderCandidate(
            trading_pair=self.maker_trading_pair,
            is_maker=True,
            order_type=OrderType.LIMIT,
            order_side=self.maker_order_side,
            amount=self.config.order_amount,
            price=mid_price,)
        taker_order_candidate = OrderCandidate(
            trading_pair=self.taker_trading_pair,
            is_maker=False,
            order_type=OrderType.LIMIT,
            order_side=self.taker_order_side,
            amount=self.config.order_amount,
            price=mid_price,)
        maker_adjusted_candidate = self.adjust_order_candidates(self.maker_connector, [maker_order_candidate])[0]
        taker_adjusted_candidate = self.adjust_order_candidates(self.taker_connector, [taker_order_candidate])[0]
        if maker_adjusted_candidate.amount == Decimal("0") or taker_adjusted_candidate.amount == Decimal("0"):
            self.close_type = CloseType.INSUFFICIENT_BALANCE
            self.logger().error("Not enough budget to open position.")
            self.stop()

    async def control_task(self):
        if self.status == SmartComponentStatus.RUNNING:
            await self.update_prices_and_tx_costs()
            await self.control_maker_order()
        elif self.status == SmartComponentStatus.SHUTTING_DOWN:
            await self.control_shutdown_process()

    async def control_maker_order(self):
        if self.maker_order is None:
            await self.create_maker_order()
        else:
            await self.control_update_maker_order()

    async def update_prices_and_tx_costs(self):
        self._taker_result_price = await self.get_resulting_price_for_amount(
            connector=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            is_buy=self.taker_order_side == TradeType.BUY,
            order_amount=self.config.order_amount)
        self._tx_cost = await self.get_tx_cost()
        self._tx_cost_pct = self._tx_cost / self.config.order_amount
        if self.taker_order_side == TradeType.BUY:
            self._maker_target_price = self._taker_result_price * (1 + self.config.target_profitability + self._tx_cost_pct)
        else:
            self._maker_target_price = self._taker_result_price * (1 - self.config.target_profitability - self._tx_cost_pct)

    async def get_tx_cost(self):
        base, quote = split_hb_trading_pair(trading_pair=self.buying_market.trading_pair)
        # TODO: also due the fact that we don't have a good rate oracle source we have to use a fixed token
        base_without_wrapped = base[1:] if base.startswith("W") else base
        taker_fee = await self.get_tx_cost_in_asset(
            exchange=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            is_buy=True,
            order_amount=self.config.order_amount,
            asset=base_without_wrapped
        )
        maker_fee = await self.get_tx_cost_in_asset(
            exchange=self.maker_trading_pair,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT,
            is_buy=False,
            order_amount=self.config.order_amount,
            asset=base_without_wrapped)
        return taker_fee + maker_fee

    async def get_tx_cost_in_asset(self, exchange: str, trading_pair: str, is_buy: bool, order_amount: Decimal,
                                   asset: str, order_type: OrderType = OrderType.MARKET):
        connector = self.connectors[exchange]
        price = await self.get_resulting_price_for_amount(exchange, trading_pair, is_buy, order_amount)
        if self.is_amm_connector(exchange=exchange):
            gas_cost = connector.network_transaction_fee
            conversion_price = RateOracle.get_instance().get_pair_rate(f"{asset}-{gas_cost.token}")
            return gas_cost.amount / conversion_price
        else:
            fee = connector.get_fee(
                base_currency=asset,
                quote_currency=asset,
                order_type=order_type,
                order_side=TradeType.BUY if is_buy else TradeType.SELL,
                amount=order_amount,
                price=price,
                is_maker=False
            )
            return fee.fee_amount_in_token(
                trading_pair=trading_pair,
                price=price,
                order_amount=order_amount,
                token=asset,
                exchange=connector,
            )

    async def get_resulting_price_for_amount(self, connector: str, trading_pair: str, is_buy: bool,
                                             order_amount: Decimal):
        return await self.connectors[connector].get_quote_price(trading_pair, is_buy, order_amount)

    async def create_maker_order(self):
        order_id = self.place_order(
            connector_name=self.maker_connector,
            trading_pair=self.maker_trading_pair,
            order_type=OrderType.LIMIT,
            side=self.maker_order_side,
            amount=self.config.order_amount,
            price=self._maker_target_price)
        self.maker_order = TrackedOrder(order_id=order_id)
        self.maker_order.order = self.get_in_flight_order(self.maker_connector, order_id)
        self.logger().info(f"Created maker order {order_id} at price {self._maker_target_price}.")

    async def control_shutdown_process(self):
        if self.maker_order.order.is_done and self.taker_order.order.is_done:
            self.logger().info("Both orders are done, executor terminated.")
            self.stop()

    async def control_update_maker_order(self):
        if self.maker_order.order.is_open:
            maker_price = self.maker_order.order.price
            if self.maker_order_side == TradeType.BUY:
                trade_profitability = (self._taker_result_price - maker_price) / maker_price
            else:
                trade_profitability = (maker_price - self._taker_result_price) / maker_price
            if trade_profitability - self._tx_cost_pct < self.config.min_profitability:
                self.logger().info(f"Trade profitability {trade_profitability} is below minimum profitability. Cancelling order.")
                await self._strategy.cancel(self.maker_connector, self.maker_trading_pair, self.maker_order.order_id)
                self.maker_order = None

    def process_order_completed_event(self,
                                      event_tag: int,
                                      market: ConnectorBase,
                                      event: Union[BuyOrderCompletedEvent, SellOrderCompletedEvent]):
        if event.order_id == self.maker_order.order_id:
            self.logger().info(f"Maker order {event.order_id} completed. Executing taker order.")
            self.place_taker_order()
            self.status = SmartComponentStatus.SHUTTING_DOWN

    def place_taker_order(self):
        taker_order_id = self.place_order(
            connector_name=self.taker_connector,
            trading_pair=self.taker_trading_pair,
            order_type=OrderType.MARKET,
            side=self.taker_order_side,
            amount=self.config.order_amount)
        self.taker_order = TrackedOrder(order_id=taker_order_id)
        self.taker_order.order = self.get_in_flight_order(self.taker_connector, taker_order_id)

    def process_order_failed_event(self, _, market, event: MarketOrderFailureEvent):
        if self.maker_order.order_id == event.order_id:
            self.failed_orders.append(self.maker_order)
            self.maker_order = None
            self._current_retries += 1
        elif self.taker_order.order_id == event.order_id:
            self.failed_orders.append(self.taker_order)
            self._current_retries += 1
            self.place_taker_order()
