"""统一日线回测引擎。

策略负责在收盘后生成信号；本模块负责在下一交易日开盘成交、
仓位管理、交易成本、退出规则及绩效统计。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

import pandas as pd


@dataclass(frozen=True)
class BacktestConfig:
    initial_cash: float = 1_000_000.0
    position_fraction: float = 0.10
    max_positions: int = 10
    commission_rate: float = 0.0003
    minimum_commission: float = 5.0
    sell_stamp_duty: float = 0.0005
    slippage_rate: float = 0.0005
    lot_size: int = 100
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.08
    trailing_stop_pct: float = 0.05
    max_holding_bars: int = 20


@dataclass
class Position:
    symbol: str
    quantity: int
    entry_date: pd.Timestamp
    entry_price: float
    entry_cost: float
    entry_bar_index: int
    peak_price: float


class UnifiedBacktester:
    """按交易日推进的多标的、长仓日线回测器。"""

    def __init__(self, config: BacktestConfig | None = None):
        self.cfg = config or BacktestConfig()

    def run(
        self,
        price_data: Mapping[str, pd.DataFrame],
        signals: Mapping[str, pd.DataFrame],
        start_date: str | pd.Timestamp | None = None,
        end_date: str | pd.Timestamp | None = None,
    ) -> dict[str, object]:
        """运行回测。

        ``signals`` 中每一行必须含有 ``date``，该信号在该日收盘后可见，
        所以只会在该标的的下一根日线开盘执行。
        """
        data_by_symbol = self._normalise_data(price_data)
        signal_dates = self._normalise_signals(signals, data_by_symbol)
        calendar = self._build_calendar(data_by_symbol, start_date, end_date)

        cash = self.cfg.initial_cash
        positions: dict[str, Position] = {}
        pending_buys: dict[pd.Timestamp, set[str]] = {}
        trades: list[dict[str, object]] = []
        equity_rows: list[dict[str, object]] = []
        last_prices: dict[str, float] = {}
        last_price_dates: dict[str, pd.Timestamp] = {}

        for date in calendar:
            current_bars = self._bars_for_date(data_by_symbol, date)
            for symbol, bar in current_bars.items():
                last_prices[symbol] = float(bar["close"])
                last_price_dates[symbol] = date

            for symbol, position in list(positions.items()):
                bar = current_bars.get(symbol)
                if bar is None:
                    continue
                exit_price, reason = self._exit_price(position, bar)
                if exit_price is not None:
                    cash += self._close_position(
                        position, date, exit_price, reason, trades
                    )
                    del positions[symbol]

            for symbol in sorted(pending_buys.pop(date, set())):
                if symbol in positions or len(positions) >= self.cfg.max_positions:
                    continue
                bar = current_bars.get(symbol)
                if bar is None:
                    continue
                position = self._open_position(
                    symbol, date, bar, cash, int(bar.name)
                )
                if position is None:
                    continue
                cash -= position.entry_cost
                positions[symbol] = position

            for symbol, frame in data_by_symbol.items():
                if date not in signal_dates.get(symbol, set()):
                    continue
                next_date = self._next_trade_date(frame, date)
                if next_date is not None:
                    pending_buys.setdefault(next_date, set()).add(symbol)

            market_value = sum(
                position.quantity * last_prices[position.symbol]
                for position in positions.values()
                if position.symbol in last_prices
            )
            equity_rows.append({
                "date": date,
                "cash": round(cash, 2),
                "market_value": round(market_value, 2),
                "equity": round(cash + market_value, 2),
                "positions": len(positions),
            })

        if calendar:
            last_date = calendar[-1]
            for symbol, position in list(positions.items()):
                if symbol not in last_prices:
                    continue
                cash += self._close_position(
                    position,
                    last_price_dates[symbol],
                    last_prices[symbol],
                    "回测结束",
                    trades,
                )
                del positions[symbol]
            if equity_rows:
                equity_rows[-1].update({
                    "cash": round(cash, 2),
                    "market_value": 0.0,
                    "equity": round(cash, 2),
                    "positions": 0,
                })

        trades_df = pd.DataFrame(trades)
        equity_df = pd.DataFrame(equity_rows)
        return {
            "trades": trades_df,
            "equity_curve": equity_df,
            "metrics": self._metrics(trades_df, equity_df),
            "config": asdict(self.cfg),
        }

    def _normalise_data(
        self, price_data: Mapping[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        required = {"date", "open", "high", "low", "close"}
        normalised = {}
        for symbol, frame in price_data.items():
            missing = required - set(frame.columns)
            if missing:
                raise ValueError(f"{symbol} 缺少行情列: {sorted(missing)}")
            data = frame.copy()
            data["date"] = pd.to_datetime(data["date"]).dt.normalize()
            data = data.sort_values("date").drop_duplicates("date").reset_index(drop=True)
            if not data.empty:
                normalised[symbol] = data
        if not normalised:
            raise ValueError("没有可用于回测的行情数据")
        return normalised

    def _normalise_signals(
        self,
        signals: Mapping[str, pd.DataFrame],
        price_data: Mapping[str, pd.DataFrame],
    ) -> dict[str, set[pd.Timestamp]]:
        dates: dict[str, set[pd.Timestamp]] = {}
        for symbol, frame in signals.items():
            if symbol not in price_data or frame is None or frame.empty:
                continue
            if "date" not in frame.columns:
                raise ValueError(f"{symbol} 的信号缺少 date 列")
            dates[symbol] = set(pd.to_datetime(frame["date"]).dt.normalize())
        return dates

    def _build_calendar(self, price_data, start_date, end_date) -> list[pd.Timestamp]:
        dates = pd.DatetimeIndex([])
        for frame in price_data.values():
            dates = dates.union(pd.DatetimeIndex(frame["date"]))
        if start_date is not None:
            dates = dates[dates >= pd.Timestamp(start_date)]
        if end_date is not None:
            dates = dates[dates <= pd.Timestamp(end_date)]
        return list(dates.sort_values())

    @staticmethod
    def _bars_for_date(price_data, date) -> dict[str, pd.Series]:
        bars = {}
        for symbol, frame in price_data.items():
            matches = frame[frame["date"] == date]
            if not matches.empty:
                bars[symbol] = matches.iloc[0]
        return bars

    @staticmethod
    def _next_trade_date(frame: pd.DataFrame, date: pd.Timestamp):
        future = frame.loc[frame["date"] > date, "date"]
        return future.iloc[0] if not future.empty else None

    def _open_position(self, symbol, date, bar, cash, bar_index) -> Position | None:
        raw_price = float(bar["open"])
        if raw_price <= 0:
            return None
        entry_price = raw_price * (1 + self.cfg.slippage_rate)
        budget = min(cash, self.cfg.initial_cash * self.cfg.position_fraction)
        quantity = int(budget / entry_price / self.cfg.lot_size) * self.cfg.lot_size
        if quantity <= 0:
            return None
        gross = quantity * entry_price
        commission = max(gross * self.cfg.commission_rate, self.cfg.minimum_commission)
        while quantity > 0 and gross + commission > cash:
            quantity -= self.cfg.lot_size
            gross = quantity * entry_price
            commission = max(gross * self.cfg.commission_rate, self.cfg.minimum_commission)
        if quantity <= 0:
            return None
        return Position(
            symbol=symbol,
            quantity=quantity,
            entry_date=date,
            entry_price=entry_price,
            entry_cost=gross + commission,
            entry_bar_index=bar_index,
            peak_price=entry_price,
        )

    def _exit_price(self, position: Position, bar: pd.Series):
        if pd.Timestamp(bar["date"]) <= position.entry_date:
            return None, None
        open_price = float(bar["open"])
        low_price = float(bar["low"])
        high_price = float(bar["high"])
        stop_price = max(
            position.entry_price * (1 - self.cfg.stop_loss_pct),
            position.peak_price * (1 - self.cfg.trailing_stop_pct),
        )
        target_price = position.entry_price * (1 + self.cfg.take_profit_pct)
        if open_price <= stop_price:
            return open_price * (1 - self.cfg.slippage_rate), "跳空止损"
        if low_price <= stop_price:
            return stop_price * (1 - self.cfg.slippage_rate), "止损/移动止损"
        if high_price >= target_price:
            return target_price * (1 - self.cfg.slippage_rate), "止盈"
        position.peak_price = max(position.peak_price, high_price)
        held_bars = int(bar.name) - position.entry_bar_index
        if held_bars >= self.cfg.max_holding_bars:
            return float(bar["close"]) * (1 - self.cfg.slippage_rate), "最长持有期"
        return None, None

    def _close_position(self, position, date, exit_price, reason, trades):
        gross = position.quantity * exit_price
        commission = max(gross * self.cfg.commission_rate, self.cfg.minimum_commission)
        stamp_duty = gross * self.cfg.sell_stamp_duty
        net_proceeds = gross - commission - stamp_duty
        pnl = net_proceeds - position.entry_cost
        trades.append({
            "symbol": position.symbol,
            "entry_date": position.entry_date,
            "exit_date": date,
            "quantity": position.quantity,
            "entry_price": round(position.entry_price, 4),
            "exit_price": round(exit_price, 4),
            "pnl": round(pnl, 2),
            "return_pct": round(pnl / position.entry_cost, 6),
            "exit_reason": reason,
        })
        return net_proceeds

    def _metrics(self, trades: pd.DataFrame, equity: pd.DataFrame) -> dict[str, float | int]:
        if equity.empty:
            return {"trade_count": 0, "total_return": 0.0, "max_drawdown": 0.0}
        values = equity["equity"].astype(float)
        drawdown = values / values.cummax() - 1
        total_return = values.iloc[-1] / self.cfg.initial_cash - 1
        daily_returns = values.pct_change().dropna()
        sharpe = 0.0
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (daily_returns.mean() / daily_returns.std()) * (252 ** 0.5)
        win_rate = 0.0 if trades.empty else float((trades["pnl"] > 0).mean())
        return {
            "trade_count": int(len(trades)),
            "total_return": round(float(total_return), 6),
            "max_drawdown": round(float(drawdown.min()), 6),
            "sharpe": round(float(sharpe), 4),
            "win_rate": round(win_rate, 6),
        }
