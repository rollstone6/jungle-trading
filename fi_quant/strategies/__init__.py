"""策略模块，统一管理所有交易策略。"""

from strategies.breakout import BreakoutPullbackEngine
from strategies.mean_reversion import MeanReversionEngine
from strategies.accumulation import AccumulationEngine
from strategies.lead_lag import LeadLagEngine
from strategies.pair import PairTradingEngine
from strategies.smart import SmartMoneyEngine

#: 策略注册表
STRATEGY_REGISTRY = {
    "breakout": BreakoutPullbackEngine,
    "mean_reversion": MeanReversionEngine,
    "accumulation": AccumulationEngine,
}

__all__ = [
    "STRATEGY_REGISTRY",
    "BreakoutPullbackEngine",
    "MeanReversionEngine",
    "AccumulationEngine",
    "LeadLagEngine",
    "PairTradingEngine",
    "SmartMoneyEngine",
]