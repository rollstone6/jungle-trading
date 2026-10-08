"""策略模块，统一管理所有交易策略。"""

from app.quant.strategies.breakout import BreakoutPullbackEngine
from app.quant.strategies.mean_reversion import MeanReversionEngine
from app.quant.strategies.accumulation import AccumulationEngine
from app.quant.strategies.lead_lag import LeadLagEngine
from app.quant.strategies.pair import PairTradingEngine
from app.quant.strategies.smart import SmartMoneyEngine

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