"""统一日线回测引擎。

策略负责在收盘后生成信号；本模块负责在下一交易日开盘成交、
仓位管理、交易成本、退出规则及绩效统计。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping

import pandas as pd

# 涨跌停判定容差（元）：复权/四舍五入误差
_TICK = 0.005


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
    # 涨跌停约束：按前收盘价算板价；开盘≥涨停放弃买入；跌停封死日卖出顺延
    enforce_price_limits: bool = True
    # 停牌导致挂单未成交时，最多顺延几根该标的bar，超过则显式作废
    max_buy_defer: int = 5
    # 代码级涨跌停幅度覆盖，如 {"600519": 0.10, "000001": 0.05}
    limit_pct_map: Mapping[str, float] = field(default_factory=dict)
    # ST 代码集合（6位数字），涨跌停按 5% 算
    st_symbols: frozenset = frozenset()
    # 仓位模式：fixed=按初始资金比例（不复利），equity=按当前权益比例（复利）
    sizing_mode: str = "fixed"
    # 单票持仓市值占当前权益上限，None=不限制
    max_symbol_exposure_pct: float | None = None
    # 行业持仓市值占当前权益上限，None=不限制（需配合 sector_map）
    max_sector_exposure_pct: float | None = None
    # symbol -> 行业名 映射，用于行业集中度约束
    sector_map: Mapping[str, str] = field(default_factory=dict)


@dataclass
class Position:
    symbol: str
    quantity: int
    entry_date: pd.Timestamp
    entry_price: float
    entry_cost: float
    entry_bar_index: int
    peak_price: float
    deferred_exit: str | None = None  # 跌停封死顺延中的卖出原因
    deferred_since: pd.Timestamp | None = None  # 开始顺延的日期


class UnifiedBacktester:
    """按交易日推进的多标的、长仓日线回测器。"""

    def __init__(self, config: BacktestConfig | None = None):
        self.cfg = config or BacktestConfig()
        if self.cfg.sizing_mode not in ("fixed", "equity"):
            raise ValueError(
                f"未知 sizing_mode: {self.cfg.sizing_mode}（可选 fixed/equity）")

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
        signal_dates, signal_scores = self._normalise_signals(
            signals, data_by_symbol)
        calendar = self._build_calendar(
            data_by_symbol, start_date, end_date
        )

        cash = self.cfg.initial_cash
        positions: dict[str, Position] = {}
        # 挂单: 日期 -> {symbol: 信号评分(None=无打分)}，同日多信号按评分降序分配现金
        pending_buys: dict[pd.Timestamp, dict[str, float | None]] = {}
        # 停牌顺延中的挂单: symbol -> {"attempts", "next_date", "score"}
        deferred_buys: dict[str, dict] = {}
        trades: list[dict[str, object]] = []
        equity_rows: list[dict[str, object]] = []
        last_prices: dict[str, float] = {}
        last_price_dates: dict[str, pd.Timestamp] = {}
        # 因资金/仓位约束被丢弃的信号清单（date/symbol/score/reason）
        dropped_signals: list[dict[str, object]] = []
        stats = {
            "limit_up_skips": 0,        # 开盘≥涨停放弃的买入
            "limit_down_defers": 0,     # 跌停封死卖出顺延次数
            "suspension_defers": 0,     # 停牌无bar挂单顺延次数
            "suspension_expired": 0,    # 顺延超限显式作废的挂单数
            "max_defer_wait": 0,        # 跌停顺延最长等待（bar数）
            "symbol_cap_skips": 0,      # 单票集中度超限放弃买入
            "sector_cap_skips": 0,      # 行业集中度超限放弃买入
            "due_signals": 0,           # 到期进入买入流程的信号总数（丢弃率分母）
            "cash_starved_skips": 0,    # 资金不足（买不起1手）丢弃
            "position_cap_skips": 0,    # 持仓数已满丢弃
            "drop_rate": 0.0,           # (资金不足+仓位满)/到期信号
        }
        calendar_index = {d: i for i, d in enumerate(calendar)}

        for date in calendar:
            current_bars = self._bars_for_date(data_by_symbol, date)
            for symbol, bar in current_bars.items():
                last_prices[symbol] = float(bar["close"])
                last_price_dates[symbol] = date

            for symbol, position in list(positions.items()):
                bar = current_bars.get(symbol)
                if bar is None:
                    continue
                if position.deferred_exit is not None:
                    # 此前跌停封死，今天继续尝试卖出
                    if self._sell_blocked(data_by_symbol, symbol, bar):
                        stats["limit_down_defers"] += 1
                        continue
                    wait = self._bars_since(data_by_symbol, symbol,
                                            position.deferred_since, date)
                    stats["max_defer_wait"] = max(
                        stats["max_defer_wait"], wait)
                    exit_price = float(bar["open"]) * (
                        1 - self.cfg.slippage_rate)
                    cash += self._close_position(
                        position, date, exit_price,
                        position.deferred_exit + "(顺延)", trades)
                    del positions[symbol]
                    continue
                exit_price, reason = self._exit_price(position, bar)
                if exit_price is not None:
                    if self._sell_blocked(data_by_symbol, symbol, bar):
                        # 跌停封死卖不掉，顺延到下一交易日开盘
                        position.deferred_exit = reason
                        position.deferred_since = date
                        stats["limit_down_defers"] += 1
                        continue
                    cash += self._close_position(
                        position, date, exit_price, reason, trades
                    )
                    del positions[symbol]

            # 当前权益（卖出后、买入前），用于复利模式与集中度上限
            equity_now = cash + sum(
                p.quantity * last_prices[p.symbol]
                for p in positions.values() if p.symbol in last_prices
            )

            # 当日到期挂单 + 停牌顺延中的挂单，一起尝试成交。
            # 同日多信号按评分降序分配现金（无评分退化为代码字典序=旧行为），
            # 不再依赖 sorted(symbol) 这个无意义变量决定先到先得。
            due: dict[str, float | None] = dict(pending_buys.pop(date, {}))
            for symbol, info in list(deferred_buys.items()):
                if info["next_date"] == date:
                    due[symbol] = info.get("score")

            def _due_key(item):
                symbol, score = item
                return (0 if score is not None else 1,
                        -(score or 0.0), symbol)

            for symbol, score in sorted(due.items(), key=_due_key):
                if symbol in positions:
                    # 重复信号（已持仓），不算资金丢弃
                    deferred_buys.pop(symbol, None)
                    continue
                stats["due_signals"] += 1
                if len(positions) >= self.cfg.max_positions:
                    deferred_buys.pop(symbol, None)
                    stats["position_cap_skips"] += 1
                    self._record_drop(dropped_signals, date, symbol,
                                      score, "仓位已满")
                    continue
                bar = current_bars.get(symbol)
                if bar is None:
                    # 停牌无bar：按全局日历逐日顺延计数，超过 max_buy_defer
                    # 个交易日显式作废（评分随挂单携带）
                    info = deferred_buys.setdefault(symbol, {
                        "attempts": 0, "next_date": None, "score": score})
                    info["attempts"] += 1
                    pos = calendar_index.get(date, -1)
                    nxt = (calendar[pos + 1]
                           if 0 <= pos + 1 < len(calendar) else None)
                    info["next_date"] = nxt
                    if info["attempts"] > self.cfg.max_buy_defer or nxt is None:
                        del deferred_buys[symbol]
                        stats["suspension_expired"] += 1
                    else:
                        stats["suspension_defers"] += 1
                    continue
                if self._buy_blocked(data_by_symbol, symbol, bar):
                    # 开盘≥涨停价，放弃本次买入（不顺延）
                    deferred_buys.pop(symbol, None)
                    stats["limit_up_skips"] += 1
                    continue
                deferred_buys.pop(symbol, None)
                cap_before = (stats["symbol_cap_skips"]
                              + stats["sector_cap_skips"])
                budget = self._entry_budget(
                    symbol, cash, equity_now, positions, last_prices, stats)
                if budget is None:
                    # _entry_budget 拒绝：集中度超限 or 现金买不起最小预算
                    if (stats["symbol_cap_skips"]
                            + stats["sector_cap_skips"]) > cap_before:
                        self._record_drop(dropped_signals, date, symbol,
                                          score, "集中度超限")
                    else:
                        stats["cash_starved_skips"] += 1
                        self._record_drop(dropped_signals, date, symbol,
                                          score, "资金不足")
                    continue
                position = self._open_position(
                    symbol, date, bar, cash, int(bar.name), budget
                )
                if position is None:
                    # 现金买不起1手 → 资金约束丢弃
                    stats["cash_starved_skips"] += 1
                    self._record_drop(dropped_signals, date, symbol,
                                      score, "资金不足")
                    continue
                cash -= position.entry_cost
                positions[symbol] = position

            for symbol, frame in data_by_symbol.items():
                if date not in signal_dates.get(symbol, set()):
                    continue
                # 挂单调度用全局日历的下一交易日：停牌（本票当日无bar）
                # 时进入顺延/作废逻辑，而不是被静默丢弃
                pos = calendar_index.get(date)
                if pos is None or pos + 1 >= len(calendar):
                    continue
                next_date = calendar[pos + 1]
                score = signal_scores.get(symbol, {}).get(date)
                day_queue = pending_buys.setdefault(next_date, {})
                # 同日同票重复信号取最高分（None 视为最低）
                prev = day_queue.get(symbol, "missing")
                if prev == "missing" or (
                        score is not None
                        and (prev is None or score > prev)):
                    day_queue[symbol] = score

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
        # 结束时仍有未成交挂单/顺延单的，计入作废并记录清单（未到期，
        # 不算资金约束丢弃，不进 drop_rate 分子）
        leftover_items: list[tuple[str, float | None]] = [
            (sym, score)
            for day in pending_buys.values() for sym, score in day.items()
        ] + [
            (sym, info.get("score")) for sym, info in deferred_buys.items()
        ]
        if leftover_items:
            stats["suspension_expired"] += len(leftover_items)
        for sym, score in leftover_items:
            self._record_drop(dropped_signals, None, sym, score,
                              "回测结束未成交")
        due_total = int(stats["due_signals"])
        stats["drop_rate"] = round(
            (int(stats["cash_starved_skips"])
             + int(stats["position_cap_skips"])) / due_total, 4
        ) if due_total > 0 else 0.0
        return {
            "trades": trades_df,
            "equity_curve": equity_df,
            "metrics": self._metrics(trades_df, equity_df),
            "execution_stats": stats,
            "dropped_signals": pd.DataFrame(dropped_signals),
            "config": asdict(self.cfg),
        }

    @staticmethod
    def _record_drop(dropped, date, symbol, score, reason):
        """记录一条被约束丢弃的信号。"""
        dropped.append({
            "date": date,
            "symbol": symbol,
            "score": None if score is None else round(float(score), 2),
            "reason": reason,
        })

    def _normalise_data(
        self, price_data: Mapping[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        required = {"date", "open", "high", "low", "close"}
        normalised = {}
        for symbol, frame in price_data.items():
            missing = required - set(frame.columns)
            if missing:
                raise ValueError(
                    f"{symbol} 缺少行情列: {sorted(missing)}"
                )
            data = frame.copy()
            data["date"] = pd.to_datetime(data["date"]).dt.normalize()
            data = (
                data.sort_values("date")
                .drop_duplicates("date")
                .reset_index(drop=True)
            )
            if not data.empty:
                normalised[symbol] = data
        if not normalised:
            raise ValueError("没有可用于回测的行情数据")
        return normalised

    def _normalise_signals(
        self,
        signals: Mapping[str, pd.DataFrame],
        price_data: Mapping[str, pd.DataFrame],
    ) -> tuple[dict[str, set[pd.Timestamp]], dict[str, dict]]:
        """提取信号日期与评分。

        返回 (dates, scores)；scores 为 symbol -> {交易日: 评分}，
        同日多条信号取最高分。策略若未提供 score/score_value 列，
        评分为 None，同日排序退化为代码字典序（保持旧行为）。
        """
        dates: dict[str, set[pd.Timestamp]] = {}
        scores: dict[str, dict[pd.Timestamp, float]] = {}
        for symbol, frame in signals.items():
            if symbol not in price_data or frame is None or frame.empty:
                continue
            if "date" not in frame.columns:
                raise ValueError(f"{symbol} 的信号缺少 date 列")
            norm_dates = pd.to_datetime(frame["date"]).dt.normalize()
            dates[symbol] = set(norm_dates)
            score_col = next(
                (c for c in ("score", "score_value") if c in frame.columns),
                None)
            if score_col is not None:
                merged: dict[pd.Timestamp, float] = {}
                score_series = pd.Series(
                    pd.to_numeric(frame[score_col], errors="coerce")
                ).tolist()
                for d, s in zip(list(norm_dates), score_series):
                    if pd.notna(s):
                        v = float(s)
                        if d not in merged or v > merged[d]:
                            merged[d] = v
                if merged:
                    scores[symbol] = merged
        return dates, scores

    def _build_calendar(
        self, price_data, start_date, end_date
    ) -> list[pd.Timestamp]:
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

    def _limit_pct(self, symbol: str) -> float | None:
        """按代码板块返回涨跌停幅度；None=不约束（港美股等）。"""
        if not self.cfg.enforce_price_limits:
            return None
        override = self.cfg.limit_pct_map.get(symbol)
        if override is not None:
            return float(override)
        code = symbol.split(".")[-1] if "." in symbol else symbol
        code = "".join(ch for ch in code if ch.isdigit())
        if len(code) != 6:
            return None
        if code in self.cfg.st_symbols:
            return 0.05
        if code.startswith(("300", "301", "302", "688", "689")):
            return 0.20
        if code.startswith(("4", "8", "92")):
            return 0.30
        if code.startswith(("60", "00", "90")):
            return 0.10
        return None

    def _price_limits(
        self, frame: pd.DataFrame, bar, symbol: str
    ) -> tuple[float, float] | None:
        """按前一交易日收盘价计算 (跌停价, 涨停价)；无法判定返回 None。"""
        pct = self._limit_pct(symbol)
        try:
            idx = int(bar.name)
        except (TypeError, ValueError):
            return None
        if pct is None or idx <= 0:
            return None
        prev_close = float(frame.iloc[idx - 1]["close"])
        if prev_close <= 0:
            return None
        # 停牌导致"前一根bar"是很久之前收盘价时，板价失真，不做约束
        gap_days = (pd.Timestamp(frame.iloc[idx]["date"])
                    - pd.Timestamp(frame.iloc[idx - 1]["date"])).days
        if gap_days > 10:
            return None
        return (round(prev_close * (1 - pct), 2),
                round(prev_close * (1 + pct), 2))

    def _buy_blocked(self, data_by_symbol, symbol, bar) -> bool:
        """开盘价 ≥ 涨停价（含容差）视为一字/开盘封板，放弃买入。"""
        limits = self._price_limits(data_by_symbol[symbol], bar, symbol)
        if limits is None:
            return False
        return float(bar["open"]) >= limits[1] - _TICK

    def _sell_blocked(self, data_by_symbol, symbol, bar) -> bool:
        """跌停封死判定：开盘即跌停价且全天最低价未离开跌停价，卖单难以成交。"""
        limits = self._price_limits(data_by_symbol[symbol], bar, symbol)
        if limits is None:
            return False
        return (float(bar["open"]) <= limits[0] + _TICK
                and float(bar["low"]) <= limits[0] + _TICK)

    def _bars_since(self, data_by_symbol, symbol, since_date, date) -> int:
        frame = data_by_symbol[symbol]
        return int(((frame["date"] > since_date)
                    & (frame["date"] <= date)).sum())

    def _entry_budget(
        self, symbol, cash, equity_now, positions, last_prices, stats
    ) -> float | None:
        """计算本次买入预算；集中度超限返回 None 并计数。

        fixed 模式按初始资金的比例（原行为，不复利）；
        equity 模式按当前权益比例（复利）。
        """
        if self.cfg.sizing_mode == "equity":
            base = equity_now
        elif self.cfg.sizing_mode == "fixed":
            base = self.cfg.initial_cash
        else:
            raise ValueError(
                f"未知 sizing_mode: {self.cfg.sizing_mode}（可选 fixed/equity）")
        budget = min(cash, base * self.cfg.position_fraction)

        # 单票集中度：该票已有持仓市值 + 新买入 不得超过上限
        if self.cfg.max_symbol_exposure_pct is not None:
            cap = equity_now * self.cfg.max_symbol_exposure_pct
            held = 0.0
            pos = positions.get(symbol)
            if pos is not None and symbol in last_prices:
                held = pos.quantity * last_prices[symbol]
            room = cap - held
            if room <= 0:
                stats["symbol_cap_skips"] += 1
                return None
            budget = min(budget, room)

        # 行业集中度：本行业持仓市值 + 新买入 不得超过上限
        if self.cfg.max_sector_exposure_pct is not None and self.cfg.sector_map:
            sector = self.cfg.sector_map.get(symbol)
            if sector:
                cap = equity_now * self.cfg.max_sector_exposure_pct
                held = sum(
                    p.quantity * last_prices[p.symbol]
                    for p in positions.values()
                    if self.cfg.sector_map.get(p.symbol) == sector
                    and p.symbol in last_prices
                )
                room = cap - held
                if room <= 0:
                    stats["sector_cap_skips"] += 1
                    return None
                budget = min(budget, room)
        return budget if budget > 0 else None

    def _open_position(
        self, symbol, date, bar, cash, bar_index, budget=None
    ) -> Position | None:
        raw_price = float(bar["open"])
        if raw_price <= 0:
            return None
        entry_price = raw_price * (1 + self.cfg.slippage_rate)
        if budget is None:
            budget = min(
                cash, self.cfg.initial_cash * self.cfg.position_fraction
            )
        quantity = (
            int(budget / entry_price / self.cfg.lot_size)
            * self.cfg.lot_size
        )
        if quantity <= 0:
            return None
        gross = quantity * entry_price
        commission = max(
            gross * self.cfg.commission_rate,
            self.cfg.minimum_commission,
        )
        while quantity > 0 and gross + commission > cash:
            quantity -= self.cfg.lot_size
            gross = quantity * entry_price
            commission = max(
                gross * self.cfg.commission_rate,
                self.cfg.minimum_commission,
            )
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
        target_price = position.entry_price * (
            1 + self.cfg.take_profit_pct
        )
        if open_price <= stop_price:
            return (
                open_price * (1 - self.cfg.slippage_rate),
                "跳空止损",
            )
        if low_price <= stop_price:
            return (
                stop_price * (1 - self.cfg.slippage_rate),
                "止损/移动止损",
            )
        if high_price >= target_price:
            return (
                target_price * (1 - self.cfg.slippage_rate),
                "止盈",
            )
        position.peak_price = max(position.peak_price, high_price)
        held_bars = int(bar.name) - position.entry_bar_index
        if held_bars >= self.cfg.max_holding_bars:
            return (
                float(bar["close"]) * (1 - self.cfg.slippage_rate),
                "最长持有期",
            )
        return None, None

    def _close_position(
        self, position, date, exit_price, reason, trades
    ):
        gross = position.quantity * exit_price
        commission = max(
            gross * self.cfg.commission_rate,
            self.cfg.minimum_commission,
        )
        stamp_duty = gross * self.cfg.sell_stamp_duty
        net_proceeds = gross - commission - stamp_duty
        pnl = net_proceeds - position.entry_cost
        # 单笔风险额：初始止损距离（入场时 peak=entry，stop=entry*(1-stop_loss_pct)）
        risk = position.entry_cost * self.cfg.stop_loss_pct
        holding_days = (date - position.entry_date).days
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
            "risk_amount": round(risk, 2),
            "r_multiple": round(pnl / risk, 4) if risk > 0 else None,
            "holding_days": int(holding_days),
        })
        return net_proceeds

    def _metrics(
        self, trades: pd.DataFrame, equity: pd.DataFrame
    ) -> dict[str, object]:
        empty = {
            "trade_count": 0,
            "total_return": 0.0,
            "annual_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "calmar": 0.0,
            "win_rate": 0.0,
            "profit_factor": None,
            "payoff_ratio": None,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy_r": None,
            "avg_holding_days": None,
            "by_exit_reason": [],
        }
        if equity.empty:
            return empty
        values = equity["equity"].astype(float)
        drawdown = values / values.cummax() - 1
        total_return = values.iloc[-1] / self.cfg.initial_cash - 1
        daily_returns = values.pct_change().dropna()
        sharpe = 0.0
        sortino = 0.0
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = (
                (daily_returns.mean() / daily_returns.std())
                * (252 ** 0.5)
            )
            # 下行标准差用全样本（目标收益=0），Sortino 业界惯例
            downside = daily_returns.clip(upper=0.0)
            downside_dev = (downside ** 2).mean() ** 0.5
            if downside_dev > 0:
                sortino = (
                    (daily_returns.mean() / downside_dev)
                    * (252 ** 0.5)
                )
        # 年化收益：按日历跨度复利折算（不足一年不放大到全年以上口径歧义，
        # 跨天数<30 时退化为 252 根bar口径）
        n_bars = max(len(daily_returns), 1)
        span_days = (int((equity["date"].iloc[-1] - equity["date"].iloc[0]).days)
                     if len(equity) > 1 else 0)
        years = span_days / 365.0 if span_days >= 30 else n_bars / 252.0
        annual_return = 0.0
        if years > 0 and total_return > -1:
            annual_return = (1 + total_return) ** (1 / years) - 1
        max_drawdown = float(drawdown.min())
        calmar = (
            annual_return / abs(max_drawdown)
            if max_drawdown < 0 else None
        )
        win_rate = (
            0.0 if trades.empty
            else float((trades["pnl"] > 0).mean())
        )
        # ---- 交易级指标 ----
        profit_factor = None
        payoff_ratio = None
        avg_win = None
        avg_loss = None
        expectancy_r = None
        avg_holding_days = None
        by_exit_reason: list[dict[str, object]] = []
        if not trades.empty:
            pnl = trades["pnl"].astype(float)
            gross_win = float(pnl[pnl > 0].sum())
            gross_loss = float(-pnl[pnl < 0].sum())
            if gross_loss > 0:
                profit_factor = round(gross_win / gross_loss, 4)
            avg_win_f = pnl[pnl > 0].mean() if (pnl > 0).any() else None
            avg_loss_f = pnl[pnl < 0].mean() if (pnl < 0).any() else None
            avg_win = None if avg_win_f is None else round(float(avg_win_f), 2)
            avg_loss = None if avg_loss_f is None else round(float(avg_loss_f), 2)
            if avg_win_f is not None and avg_loss_f is not None:
                payoff_ratio = round(float(avg_win_f / -avg_loss_f), 4)
            if "r_multiple" in trades.columns and bool(
                    trades["r_multiple"].notna().any()):
                expectancy_r = round(
                    float(trades["r_multiple"].mean()), 4)
            if "holding_days" in trades.columns:
                avg_holding_days = round(
                    float(trades["holding_days"].mean()), 2)
            # 按退出原因拆解（跌停顺延后缀并入主因，否则"止损"被拆成两行）
            reason = trades["exit_reason"].astype(str).str.replace(
                r"\(顺延\)$", "", regex=True)
            grouped = trades.assign(_reason=reason).groupby("_reason")
            rows = []
            for key, g in grouped:
                gp = g["pnl"].astype(float)
                win_f = gp[gp > 0].mean() if (gp > 0).any() else None
                loss_f = gp[gp < 0].mean() if (gp < 0).any() else None
                rows.append({
                    "exit_reason": key,
                    "trade_count": int(len(g)),
                    "total_pnl": round(float(gp.sum()), 2),
                    "win_rate": round(float((gp > 0).mean()), 4),
                    "avg_return_pct": round(
                        float(g["return_pct"].mean()), 6),
                    "avg_r": (None if g["r_multiple"].isna().all()
                              else round(float(g["r_multiple"].mean()), 4)),
                    "avg_holding_days": round(
                        float(g["holding_days"].mean()), 2),
                    "payoff_ratio": (
                        round(float(win_f / -loss_f), 4)
                        if win_f is not None and loss_f is not None
                        else None),
                })
            rows.sort(key=lambda r: r["total_pnl"], reverse=True)
            by_exit_reason = rows
        return {
            "trade_count": int(len(trades)),
            "total_return": round(float(total_return), 6),
            "annual_return": round(float(annual_return), 6),
            "max_drawdown": round(max_drawdown, 6),
            "sharpe": round(float(sharpe), 4),
            "sortino": round(float(sortino), 4),
            "calmar": None if calmar is None else round(float(calmar), 4),
            "win_rate": round(win_rate, 6),
            "profit_factor": profit_factor,
            "payoff_ratio": payoff_ratio,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "expectancy_r": expectancy_r,
            "avg_holding_days": avg_holding_days,
            "by_exit_reason": by_exit_reason,
        }