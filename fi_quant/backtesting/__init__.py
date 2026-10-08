"""回测引擎包。

采用 PEP 562 懒加载：避免包初始化时急切导入 runner 导致的
``python -m backtesting.runner`` RuntimeWarning，同时保持
``from backtesting import run_backtest`` 的兼容写法。
"""

__all__ = [
    "BacktestConfig",
    "Position",
    "UnifiedBacktester",
    "run_backtest",
    "STRATEGY_REGISTRY",
]


def __getattr__(name: str):
    if name in ("BacktestConfig", "Position", "UnifiedBacktester"):
        from backtesting import engine
        return getattr(engine, name)
    if name in ("run_backtest", "STRATEGY_REGISTRY"):
        from backtesting import runner
        return getattr(runner, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
