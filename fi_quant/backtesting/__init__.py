"""回测引擎包"""

from backtesting.engine import BacktestConfig, Position, UnifiedBacktester
from backtesting.runner import run_backtest, STRATEGY_REGISTRY

__all__ = [
    "BacktestConfig",
    "Position",
    "UnifiedBacktester",
    "run_backtest",
    "STRATEGY_REGISTRY",
]
