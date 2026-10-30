"""筹码单峰密集策略（博主5条件）。

入场信号（无未来函数，逐日递推仅用截至当日数据）：
  1. 90%筹码集中度 < 12%
  2. 主峰筹码占比 > 18%（真·峰谷切割分段，非±2σ近似）
  3. 次峰/主峰 < 0.5
  4. 近60日累计换手率 > 80%
  5. 近60日价格振幅 < 30%
且为"首次满足"（条件带解除后重新满足才算新信号，连满足不重复买）。

可选确认（chip_need_breakout）：信号日之后收盘价突破主峰峰值价才触发，
对应"蓄势完成后突破买入"的原始玩法；默认关闭=纯5条件形态买入。

出场沿用统一引擎：止损 / 移动止损 / 止盈 / 最大持仓天数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.quant.core.signal import Signal, SignalFrame
from app.quant.core.strategy_base import StrategyBase
from app.quant.utils.chip_peaks import (
    DEFAULT_RULES,
    _triangle_row,
    chip_path,
    price_grid,
)


class ChipSinglePeakEngine(StrategyBase):
    """筹码单峰密集·吸筹蓄势策略。"""

    name = "chip_single_peak"

    def __init__(self, config, float_shares_map: dict | None = None):
        super().__init__(config)
        #: {symbol: 流通股本（股）}；缺失时用固定衰减系数近似
        self.float_shares_map = float_shares_map or {}
        self.rules = dict(DEFAULT_RULES)
        for key in ("conc90_max", "peak_ratio_min", "second_peak_max",
                    "turnover60_min", "amplitude60_max"):
            cfg_attr = f"chip_strategy_{key}"
            if hasattr(config, cfg_attr):
                self.rules[key] = getattr(config, cfg_attr)

    def prepare_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def _peak_price(self, df: pd.DataFrame, idx: int) -> float | None:
        """重算 idx 日筹码分布并返回主峰价格（用于突破确认）。"""
        sub = df.iloc[: idx + 1]
        lows = sub["low"].to_numpy(dtype=float)
        highs = sub["high"].to_numpy(dtype=float)
        vols = sub["volume"].to_numpy(dtype=float)
        fs = self.float_shares_map.get(str(df.attrs.get("symbol", "")))
        if fs:
            to = np.clip(vols / fs, 0.001, 0.5)
        else:
            to = np.full(len(sub), getattr(self.cfg, "chip_decay_factor", 0.03))
        prices = price_grid(lows, highs)
        tri = np.zeros(len(prices))
        chip = np.zeros(len(prices))
        for i in range(len(sub)):
            if vols[i] <= 0:
                continue
            _triangle_row(lows[i], highs[i], prices, tri)
            chip *= (1.0 - to[i])
            chip += tri * to[i]
        if chip.sum() <= 0:
            return None
        return float(prices[int(np.argmax(chip))])

    def scan_signals(self, df, symbol: str = "", debug=False) -> SignalFrame:
        if df is None or len(df) < 200:
            return SignalFrame()

        fs = self.float_shares_map.get(str(symbol))
        path = chip_path(df, fs)
        if path is None:
            return SignalFrame()

        r = self.rules
        c = path
        ok = (
            (c["conc_90"] < r["conc90_max"])
            & (c["peak_ratio"] > r["peak_ratio_min"])
            & (c["second_ratio"] < r["second_peak_max"])
            & (c["turnover60"] > r["turnover60_min"])
            & (c["amplitude60"] < r["amplitude60_max"])
            & ~np.isnan(c["conc_90"])
        )
        idxs = np.where(ok)[0]
        if len(idxs):
            idxs = idxs[np.concatenate(([True], np.diff(idxs) > 1))]

        need_breakout = getattr(self.cfg, "chip_need_breakout", False)
        dates = pd.to_datetime(df["date"]).to_numpy()
        closes = df["close"].to_numpy(dtype=float)

        signals: list[Signal] = []
        for i in idxs:
            trigger_day = int(i)
            peak_price = None
            if need_breakout:
                # 信号日起最多等10个交易日，收盘突破主峰才买；否则作废
                peak_price = self._peak_price(df, int(i))
                if peak_price is None:
                    continue
                found = -1
                for j in range(int(i), min(int(i) + 10, len(df))):
                    if not ok[j]:
                        break
                    if closes[j] > peak_price * 1.01:
                        found = j
                        break
                if found < 0:
                    continue
                trigger_day = found

            signals.append(Signal(
                date=pd.Timestamp(dates[trigger_day]),
                symbol=str(symbol),
                close=float(closes[trigger_day]),
                strategy_name=self.name,
                metadata={
                    "conc_90": round(float(c["conc_90"][i]), 2),
                    "peak_ratio": round(float(c["peak_ratio"][i]), 1),
                    "second_ratio": round(float(c["second_ratio"][i]), 2),
                    "turnover60_pct": round(float(c["turnover60"][i]) * 100, 0),
                    "amplitude60_pct": round(float(c["amplitude60"][i]) * 100, 1),
                    "peak_price": round(peak_price, 2) if peak_price else None,
                    "form_date": pd.Timestamp(dates[i]).strftime("%Y-%m-%d"),
                },
            ))

        return SignalFrame(signals)
