from decimal import Decimal
from typing import Dict, List, Set

from pydantic import Field

from hummingbot.core.data_type.common import PositionMode
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig
from hummingbot.strategy.strategy_v2_base import StrategyV2ConfigBase


class ExcaliburConfig(StrategyV2ConfigBase):
    # Standard attributes START - avoid renaming
    markets: Dict[str, Set[str]] = Field(default_factory=dict)

    candles_config: List[CandlesConfig] = Field(default_factory=lambda: [
        CandlesConfig(
            connector="binance",
            interval="3m",
            max_records=20,
            trading_pair = "SOL-USDT"
        )
    ])

    controllers_config: List[str] = Field(default_factory=list)
    config_update_interval: int = 10
    # Standard attributes END

    # Used by PkStrategy
    connector_name: str = "hyperliquid_perpetual"
    trading_pair: str = "SOL-USD"
    leverage: int = 20
    unfilled_order_expiration: int = 60
    limit_take_profit_price_delta_bps: int = 0

    position_mode: PositionMode = PositionMode.ONEWAY

    # Triple Barrier

    # Order settings
    amount_quote: Decimal = 20.0  # Hyperliquid Perpetual rejects orders less than $10 or 0.1 SOL
    pyramiding: int = 3
    dca_trigger_pct: Decimal = 0.1
    tp_pct: Decimal = 0.3
