"""统一交易信号模型。

所有策略的 scan_signals() 必须返回 SignalFrame，
回测引擎只需读取 date 列即可执行，其余字段用于展示与风控。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class Signal:
    """单条交易信号。"""

    # === 必填字段 ===
    date: pd.Timestamp
    symbol: str
    close: float
    strategy_name: str

    # === 可选风控字段 ===
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_size: Optional[float] = None

    # === 策略特有字段 ===
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """转换为字典，便于构建 DataFrame。"""
        result: dict = {
            "date": self.date,
            "symbol": self.symbol,
            "close": self.close,
            "strategy": self.strategy_name,
        }
        if self.stop_loss is not None:
            result["stop_loss"] = self.stop_loss
        if self.take_profit is not None:
            result["take_profit"] = self.take_profit
        if self.position_size is not None:
            result["position_size"] = self.position_size
        result.update(self.metadata)
        return result


class SignalFrame:
    """信号集合，封装 DataFrame 操作。

    回测引擎通过 to_backtest_format(symbol) 获取仅含 date 列的 DataFrame。
    """

    REQUIRED_COLUMNS = {"date", "symbol", "close", "strategy"}

    def __init__(
        self,
        signals: list[Signal] | pd.DataFrame | None = None,
    ) -> None:
        if isinstance(signals, pd.DataFrame):
            self._df = signals.copy()
        elif signals:
            self._df = pd.DataFrame([s.to_dict() for s in signals])
        else:
            self._df = pd.DataFrame(columns=sorted(self.REQUIRED_COLUMNS))

        if not self._df.empty and "date" in self._df.columns:
            self._df["date"] = pd.to_datetime(self._df["date"])

    @property
    def df(self) -> pd.DataFrame:
        return self._df

    def is_empty(self) -> bool:
        return self._df.empty

    def __len__(self) -> int:
        return len(self._df)

    def filter_by_symbol(self, symbol: str) -> "SignalFrame":
        """按股票代码过滤。"""
        if self._df.empty or "symbol" not in self._df.columns:
            return SignalFrame()
        return SignalFrame(self._df[self._df["symbol"] == symbol])

    def filter_by_date(
        self,
        start: str | pd.Timestamp | None = None,
        end: str | pd.Timestamp | None = None,
    ) -> "SignalFrame":
        """按日期范围过滤。"""
        if self._df.empty or "date" not in self._df.columns:
            return SignalFrame()
        mask = pd.Series(True, index=self._df.index)
        if start is not None:
            mask &= self._df["date"] >= pd.Timestamp(start)
        if end is not None:
            mask &= self._df["date"] <= pd.Timestamp(end)
        return SignalFrame(self._df[mask])

    def to_backtest_format(self, symbol: str) -> pd.DataFrame:
        """转换为回测引擎所需格式（仅保留 date 列）。"""
        filtered = self.filter_by_symbol(symbol)
        if filtered.is_empty():
            return pd.DataFrame(columns=["date"])
        return filtered.df[["date"]].copy()

    def symbols(self) -> list[str]:
        """返回所有出现过的股票代码。"""
        if self._df.empty or "symbol" not in self._df.columns:
            return []
        return sorted(self._df["symbol"].unique().tolist())

    def merge(self, other: "SignalFrame") -> "SignalFrame":
        """合并两个 SignalFrame。"""
        if self.is_empty():
            return other
        if other.is_empty():
            return self
        return SignalFrame(pd.concat([self._df, other._df], ignore_index=True))

    def __repr__(self) -> str:
        if self.is_empty():
            return "SignalFrame(empty)"
        n_symbols = len(self.symbols())
        return f"SignalFrame({len(self._df)} signals, {n_symbols} symbols)"