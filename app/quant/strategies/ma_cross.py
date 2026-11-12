"""双均线交叉策略（实验室 MA 策略移植）。

信号语义与量化实验室（app/services/quant_pipeline.py VectorBT 回测）完全一致：
  买入 = 快均线上穿慢均线（金叉）；卖出 = 快均线下穿慢均线（死叉）。

均线参数按波动率自适应分档（2026-11-08 参数扫描验证的规则，直接复用
quant_pipeline._vol_er / adaptive_params）：
  vol60≥60% 且 ER120≥0.08 → 5/15（高波动·趋势）
  vol60≥60% 且 ER120<0.08 → 10/30（高波动·震荡守基准）
  30%≤vol60<60%          → 8/24
  其余                    → 10/30
注：分档用"当前"vol/ER 决定该标的的均线参数（与实验室回测同口径，
自适应的是标的而非逐日换线）。

信号带 side（买入/卖出），统一回测引擎 to_backtest_format 只消费买入日期
（引擎退出走止损/移动止损规则，不消费死叉日期）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.quant.core.signal import Signal, SignalFrame
from app.quant.core.strategy_base import StrategyBase


class MaCrossEngine(StrategyBase):
    """双均线金叉/死叉策略引擎（实验室同源）。"""

    name = "双均线交叉"

    def prepare_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def scan_signals(self, df, symbol: str = "", debug=False) -> SignalFrame:
        if df is None or len(df) < 40:
            return SignalFrame()

        # 延迟导入避免环依赖：quant_pipeline 顶层 import 本包 config
        from app.services.quant_pipeline import _vol_er, adaptive_params

        vol, er = _vol_er(df)
        fast, slow, bucket = adaptive_params(vol, er)

        close = df["close"].astype(float)
        ma_f = np.asarray(close.rolling(fast).mean(), dtype=float)
        ma_s = np.asarray(close.rolling(slow).mean(), dtype=float)
        valid = ~np.isnan(ma_f) & ~np.isnan(ma_s)
        with np.errstate(invalid="ignore"):
            f_gt_s = ma_f > ma_s   # NaN 比较为 False
        prev_gt = np.concatenate(([False], f_gt_s[:-1]))

        golden = valid & f_gt_s & ~prev_gt      # 金叉：今在上昨不在上
        death = valid & ~f_gt_s & prev_gt       # 死叉：今在下昨在上

        dates = pd.to_datetime(df["date"]).to_numpy()
        closes = close.to_numpy(dtype=float)
        signals: list[Signal] = []
        for mask, side, label in ((golden, "买入", "金叉"), (death, "卖出", "死叉")):
            for i in (int(p) for p in np.flatnonzero(mask)):
                signals.append(Signal(
                    date=pd.Timestamp(dates[i]),
                    symbol=str(symbol),
                    close=float(closes[i]),
                    strategy_name=self.name,
                    metadata={
                        "side": side,
                        "signal_type": label,
                        "ma": f"MA{fast}/MA{slow}",
                        "bucket": bucket,
                        "score": None,
                    },
                ))
        # 按日期升序，买卖交替（金叉/死叉穿插时序）
        signals.sort(key=lambda s: s.date)
        if debug:
            print(f"[ma_cross] {symbol} fast={fast} slow={slow} bucket={bucket} "
                  f"signals={len(signals)}")
        return SignalFrame(signals)
