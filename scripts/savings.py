from decimal import Decimal
from typing import Dict, List

import pandas as pd

from hummingbot.client.ui.interface_utils import format_df_for_printout
from hummingbot.connector.connector_base import ConnectorBase
from hummingbot.core.clock import Clock
from hummingbot.core.data_type.common import OrderType, TradeType
from hummingbot.strategy_v2.models.executor_actions import CreateExecutorAction, StopExecutorAction
from hummingbot.strategy_v2.models.executors import CloseType
from scripts.pk.pk_strategy import PkStrategy
from scripts.pk.pk_triple_barrier import TripleBarrier
from scripts.pk.tracked_order_details import TrackedOrderDetails
from scripts.savings_config import ExcaliburConfig

# Generate config file: create --script-config savings
# Start the bot: start --script savings.py --conf conf_savings_SOL.yml
#                start --script savings.py --conf conf_savings_XXX.yml
# Quickstart script: -p=a -f savings.py -c conf_savings_SOL.yml

ORDER_REF: str = "Savings"
CANDLE_DURATION_MINUTES: int = 3


class ExcaliburStrategy(PkStrategy):
    @classmethod
    def init_markets(cls, config: ExcaliburConfig):
        cls.markets = {config.connector_name: {config.trading_pair}}

    def __init__(self, connectors: Dict[str, ConnectorBase], config: ExcaliburConfig):
        super().__init__(connectors, config)

        self.processed_data = pd.DataFrame()
        self.latest_saved_candles_timestamp: float = 0

    def start(self, clock: Clock, timestamp: float) -> None:
        self._last_timestamp = timestamp
        self.apply_initial_setting()

    def apply_initial_setting(self):
        for connector_name, connector in self.connectors.items():
            if self.is_perpetual(connector_name):
                connector.set_position_mode(self.config.position_mode)

                for trading_pair in self.market_data_provider.get_trading_pairs(connector_name):
                    connector.set_leverage(trading_pair, self.config.leverage)

    def update_processed_data(self):
        candles_config = self.config.candles_config[0]

        candles_df = self.market_data_provider.get_candles_df(connector_name=candles_config.connector,
                                                              trading_pair=candles_config.trading_pair,
                                                              interval=candles_config.interval,
                                                              max_records=candles_config.max_records)
        num_rows = candles_df.shape[0]

        if num_rows == 0:
            return

        self.check_if_candles_missed_beats(candles_df["timestamp"])

        candles_df["index"] = candles_df["timestamp"]
        candles_df.set_index("index", inplace=True)

        candles_df["timestamp_iso"] = pd.to_datetime(candles_df["timestamp"], unit="s")

        candles_df.dropna(inplace=True)

        self.processed_data = candles_df

    def check_if_candles_missed_beats(self, timestamp_series: pd.Series):
        current_timestamp: float = timestamp_series.iloc[-1]

        if self.latest_saved_candles_timestamp == 0:
            self.latest_saved_candles_timestamp = current_timestamp

        delta: int = int(current_timestamp - self.latest_saved_candles_timestamp)

        if delta > CANDLE_DURATION_MINUTES * 60:
            self.logger().error(f"check_if_candles_missed_beats() | missed {delta/60} minutes between the last two candles fetch")

        self.latest_saved_candles_timestamp = current_timestamp

    def create_actions_proposal(self) -> List[CreateExecutorAction]:
        self.update_processed_data()

        processed_data_num_rows = self.processed_data.shape[0]

        if processed_data_num_rows == 0:
            self.logger().error("create_actions_proposal() > ERROR: processed_data_num_rows == 0")
            return []

        if not hasattr(self, "saved_last_dca_price"):
            self.reset_context()

        self.create_actions_proposal_savings()

        return []  # Always return []

    def stop_actions_proposal(self) -> List[StopExecutorAction]:
        processed_data_num_rows = self.processed_data.shape[0]

        if processed_data_num_rows == 0:
            return []

        self.check_orders()
        self.stop_actions_proposal_savings()

        return []  # Always return []

    def format_status(self) -> str:
        original_status = super().format_status()
        custom_status = ["\n"]

        if self.ready_to_trade:
            if not self.processed_data.empty:
                columns_to_display = [
                    "timestamp_iso",
                    "low",
                    "high",
                    "close",
                    "volume"
                ]

                custom_status.append(format_df_for_printout(self.processed_data[columns_to_display].tail(20), table_format="psql"))

        return original_status + "\n".join(custom_status)

    #
    # Quote amount and Triple Barrier
    #

    @staticmethod
    def get_triple_barrier() -> TripleBarrier:
        return TripleBarrier(
            open_order_type=OrderType.MARKET
        )

    #
    # Start/stop action proposals
    #

    def create_actions_proposal_savings(self):
        _, active_buy_orders = self.get_active_tracked_orders_by_side(ORDER_REF)

        if self.can_create_savings_order(TradeType.BUY, active_buy_orders):
            triple_barrier = self.get_triple_barrier()
            self.create_order(TradeType.BUY, self.get_current_close(), triple_barrier, self.config.amount_quote, ORDER_REF)

            self.save_last_dca_price()
            self.increment_buy_counter()

    def can_create_savings_order(self, side: TradeType, active_tracked_orders: List[TrackedOrderDetails]) -> bool:
        if not self.can_create_order(side, self.config.amount_quote, ORDER_REF, 0):
            return False

        if len(active_tracked_orders) >= self.config.pyramiding:
            return False

        if self.is_current_dca_price_below_threshold():
            self.logger().info(f"can_create_savings_order() > Opening Buy at {self.get_current_close()}")
            return True

        return False

    def stop_actions_proposal_savings(self):
        _, filled_buy_orders = self.get_filled_tracked_orders_by_side(ORDER_REF)

        if len(filled_buy_orders) > 0:
            if self.has_avg_position_reached_tp(filled_buy_orders):
                self.logger().info(f"stop_actions_proposal_savings() > Closing Buy positions at {self.get_current_close()}")
                self.close_filled_orders(filled_buy_orders, OrderType.MARKET, CloseType.TAKE_PROFIT)
                self.reset_context()

    #
    # Getters on `self.processed_data[]`
    #

    def get_current_close(self) -> Decimal:
        close_series: pd.Series = self.processed_data["close"]
        return Decimal(close_series.iloc[-1])

    def get_current_open(self) -> Decimal:
        open_series: pd.Series = self.processed_data["open"]
        return Decimal(open_series.iloc[-1])

    def get_current_low(self) -> Decimal:
        low_series: pd.Series = self.processed_data["low"]
        return Decimal(low_series.iloc[-1])

    def get_current_high(self) -> Decimal:
        high_series: pd.Series = self.processed_data["high"]
        return Decimal(high_series.iloc[-1])

    #
    # Context functions
    #

    def reset_context(self):
        self.save_last_dca_price()
        self.buy_counter: int = 0

        self.logger().info("Context is reset")

    def save_last_dca_price(self):
        current_price: Decimal = self.get_current_close()
        self.saved_last_dca_price: Decimal = current_price

    def increment_buy_counter(self):
        self.buy_counter += 1

    #
    # Strategy functions
    #

    def is_current_dca_price_below_threshold(self) -> bool:
        current_price: Decimal = self.get_current_close()
        dca_threshold: Decimal = self.saved_last_dca_price * (1 - self.config.dca_trigger_pct / 100)

        is_below_threshold: bool = current_price < dca_threshold

        if is_below_threshold:
            self.logger().info(f"is_current_price_below_dca_threshold() | current_price:{current_price} | dca_threshold:{dca_threshold}")

        return is_below_threshold

    def has_avg_position_reached_tp(self, filled_buy_orders: List[TrackedOrderDetails]) -> bool:
        avg_position_price: Decimal = self.compute_avg_position_price(filled_buy_orders)
        tp_price: Decimal = avg_position_price * (1 + self.config.tp_pct / 100)

        current_price: Decimal = self.get_current_close()
        has_reached_tp: bool = current_price > tp_price

        # TODO: remove
        self.logger().info(f"has_avg_position_reached_tp() | avg_position_price:{avg_position_price} | tp_price:{tp_price} | current_price:{current_price}")

        return has_reached_tp

    @staticmethod
    def compute_avg_position_price(filled_buy_orders: List[TrackedOrderDetails]) -> Decimal:
        total_amount = sum(order.filled_amount for order in filled_buy_orders)
        total_cost = sum(order.filled_amount * order.last_filled_price for order in filled_buy_orders)

        return Decimal(total_cost / total_amount)
