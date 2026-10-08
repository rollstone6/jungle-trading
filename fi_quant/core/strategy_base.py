"""策略基类。

所有策略必须继承 StrategyBase 并实现 scan_signals()，
返回统一的 SignalFrame 对象。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

import pandas as pd

from core.signal import SignalFrame

if TYPE_CHECKING:
    from config import StrategyConfig


class StrategyBase(ABC):
    """策略抽象基类。"""

    #: 策略名称（子类必须覆盖）
    name: str = "BaseStrategy"

    def __init__(self, config: StrategyConfig) -> None:
        self.cfg = config

    @abstractmethod
    def prepare_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算技术指标。

        Args:
            df: 原始行情数据，至少包含 date/open/high/low/close/volume。

        Returns:
            添加了指标列的 DataFrame。
        """
        ...

    @abstractmethod
    def scan_signals(
        self,
        df: pd.DataFrame,
        symbol: str,
        debug: bool = False,
    ) -> SignalFrame:
        """扫描买入信号。

        Args:
            df: 行情数据（已含指标或原始数据，由实现决定）。
            symbol: 股票代码。
            debug: 是否输出调试信息。

        Returns:
            SignalFrame: 统一信号集合。
        """
        ...

    def __repr__(self) -> str:
        return f"<Strategy: {self.name}>"