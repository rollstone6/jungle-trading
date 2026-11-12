"""ma_cross 策略单元自检（合成数据，无网络）。"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本机 vectorbt/numba 冷导入极慢（服务内早已加载），测试用 stub 短路
for _m in ("vectorbt", "duckdb"):
    sys.modules.setdefault(_m, MagicMock())

import numpy as np
import pandas as pd

from app.quant.strategies.ma_cross import MaCrossEngine
from app.quant.core.signal import SignalFrame

n = 300
rng = np.random.default_rng(7)
trend = np.concatenate([
    np.linspace(80, 110, 100), np.linspace(110, 85, 100), np.linspace(85, 130, 100),
])
close = trend + rng.normal(0, 0.6, n)
df = pd.DataFrame({
    "date": pd.date_range("2025-01-01", periods=n, freq="B"),
    "open": close, "high": close * 1.01, "low": close * 0.99,
    "close": close, "volume": rng.uniform(1e6, 2e6, n),
})

eng = MaCrossEngine(None)
sf = eng.scan_signals(df, symbol="TEST", debug=True)
d = sf.df
print("信号数:", len(d))
print(d[["date", "close", "side", "signal_type", "ma", "bucket"]]
      .tail(6).to_string(index=False))
assert {"买入", "卖出"} <= set(d["side"]), "买卖信号缺失"

# 交替性：任一相邻信号 side 应翻转（金叉/死叉不会连续同向）
s = d["side"].tolist()
assert all(a != b for a, b in zip(s, s[1:])), "买卖信号未交替"

# 与 pandas 状态机口径复核（用引擎给出的同一组均线参数）
fast, slow = (int(x) for x in d["ma"].iloc[0][2:].split("/MA"))
mf = df["close"].rolling(fast).mean()
ms = df["close"].rolling(slow).mean()
ab = (mf > ms) & ms.notna()
gold = int((ab & ~ab.shift(1).fillna(False)).sum())
assert gold == s.count("买入"), f"金叉数不一致 {gold} vs {s.count('买入')}"

# 回测格式只保留买入侧
bf = SignalFrame(d.copy()).to_backtest_format("TEST")
assert len(bf) == s.count("买入"), "to_backtest_format 未过滤卖出"
assert list(bf.columns)[0] == "date"

# 空/短数据不炸
assert MaCrossEngine(None).scan_signals(pd.DataFrame(), symbol="X").is_empty()
assert MaCrossEngine(None).scan_signals(df.iloc[:10], symbol="X").is_empty()
print("ALL OK")
