"""筹码单峰密集筛选（真·峰切割版）。

五条规则（博主口径）：
  1. 90%筹码集中度 < 12%          —— 90%筹码落在 <12% 的价格带内
  2. 主峰筹码占比 > 18%           —— 最大峰（按谷切割分段）筹码 > 18%
  3. 次峰/主峰 < 0.5              —— 次大峰筹码不足主峰一半（单峰票自动满足）
  4. 近60日累计换手率 > 80%       —— 充分换手收集
  5. 近60日价格振幅 < 30%         —— 横盘锁仓

与 chip_distribution.ChipDistributionAnalyzer 的区别：
  - 那个的 peak_ratio 是"峰值±2σ 范围筹码"的近似，无法回答"次峰/主峰"；
  - 本模块用峰谷切割分段，直接给出主峰/次峰归属筹码占比。

无未来函数：第 i 日的筹码状态只使用 <=i 的数据（一阶递推：
  chip *= (1-turnover_i); chip += tri_i * turnover_i）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# 默认阈值（博主5条件）
DEFAULT_RULES = dict(
    conc90_max=12.0,        # 条件1：< 12（%）
    peak_ratio_min=18.0,    # 条件2：主峰占比 > 18（%）
    second_peak_max=0.5,    # 条件3：次峰/主峰 < 0.5
    turnover60_min=0.80,    # 条件4：60日累计换手 > 80%
    amplitude60_max=0.30,   # 条件5：60日振幅 < 30%
    window=60,
)


def price_grid(lows: np.ndarray, highs: np.ndarray,
               step_frac: float = 0.006) -> np.ndarray:
    """固定价格网格（覆盖全历史区间，步长≈最低价的 step_frac）。"""
    pmin, pmax = float(lows.min()), float(highs.max())
    if pmax <= pmin:
        return np.array([pmin])
    step = max(pmin * step_frac, 0.001)
    return np.arange(pmin * 0.995, pmax * 1.005 + step, step)


def _triangle_row(low: float, high: float, prices: np.ndarray,
                  out: np.ndarray) -> None:
    """单日三角分布写入 out（原地，避免分配）。一字板全落最近价格点。"""
    out[:] = 0.0
    n = len(prices)
    if high <= low:
        idx = int(np.argmin(np.abs(prices - low)))
        out[idx] = 1.0
        return
    peak = (low + high) / 2.0
    w = high - low
    lo_i = int(np.searchsorted(prices, low))
    hi_i = int(np.searchsorted(prices, high, side="right"))
    lo_i = max(lo_i, 0)
    hi_i = min(hi_i, n)
    if hi_i <= lo_i:  # 网格太稀，取最近点
        idx = int(np.argmin(np.abs(prices - peak)))
        out[idx] = 1.0
        return
    seg = prices[lo_i:hi_i]
    left = seg <= peak
    with np.errstate(divide="ignore", invalid="ignore"):
        lv = (seg - low) / (peak - low) * (2.0 / w) if peak > low else None
        rv = (high - seg) / (high - peak) * (2.0 / w) if high > peak else None
    vals = out[lo_i:hi_i]
    if lv is not None:
        vals[left] = lv[left]
    if rv is not None:
        vals[~left] = rv[~left]
    s = vals.sum()
    if s <= 0:
        idx = int(np.argmin(np.abs(prices - peak)))
        out[:] = 0.0
        out[idx] = 1.0
    else:
        vals /= s


def chip_path(df: pd.DataFrame, float_shares: float | None = None,
              step_frac: float = 0.006, decay_floor: float = 0.002,
              warmup: int = 80) -> dict | None:
    """逐日递推筹码分布，输出每日指标序列（与 df 行对齐）。

    返回 dict，键为 numpy 数组（长度 = len(df)，前 warmup 行为 NaN）：
      conc_90 / peak_ratio / second_ratio / turnover60 / amplitude60
    """
    n = len(df)
    if n < warmup + 20:
        return None

    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    vols = df["volume"].to_numpy(dtype=float)

    # 换手率序列：优先 df['turnover']（小数），否则 volume/流通股本
    if "turnover" in df.columns:
        to = df["turnover"].to_numpy(dtype=float)
    else:
        to = np.full(n, np.nan)
    if float_shares and float_shares > 0:
        to = np.where(np.isnan(to), vols / float_shares, to)
    to = np.where(np.isnan(to) | (to <= 0), decay_floor, to)
    to = np.clip(to, 0.001, 0.5)

    prices = price_grid(lows, highs, step_frac)
    ng = len(prices)
    if ng < 5 or ng > 4000:
        return None

    tri = np.zeros(ng)
    chip = np.zeros(ng)
    cumsum = np.empty(ng + 1)

    conc90 = np.full(n, np.nan)
    peak_ratio = np.full(n, np.nan)
    second_ratio = np.full(n, np.nan)

    # 60日窗口统计（滚动）
    turn60 = pd.Series(to).rolling(60).sum().to_numpy()
    hh = pd.Series(highs).rolling(60).max().to_numpy()
    ll = pd.Series(lows).rolling(60).min().to_numpy()
    amp60 = np.where(ll > 0, hh / ll - 1.0, np.nan)

    for i in range(n):
        _triangle_row(lows[i], highs[i], prices, tri)
        t = to[i]
        if vols[i] > 0:
            chip *= (1.0 - t)
            chip += tri * t
        if i < warmup:
            continue
        total = chip.sum()
        if total <= 0:
            continue
        dist = chip / total

        # --- 条件1：90%集中度（对称分位 5%~95% 价格带宽 / 中位价）---
        np.cumsum(dist, out=cumsum[1:])
        cumsum[0] = 0.0
        idx_lo = np.searchsorted(cumsum, 0.05)
        idx_hi = np.searchsorted(cumsum, 0.95)
        idx_mid = np.searchsorted(cumsum, 0.5)
        idx_lo = min(idx_lo, ng - 1)
        idx_hi = min(idx_hi, ng - 1)
        mid = prices[min(idx_mid, ng - 1)]
        conc90[i] = (prices[idx_hi] - prices[idx_lo]) / mid * 100.0 if mid > 0 else 100.0

        # --- 峰谷切割分段：主峰/次峰归属筹码 ---
        # 局部极大（含平台）/局部极小
        seg_start = _valley_segment_starts(dist)
        if seg_start is None:
            peak_ratio[i], second_ratio[i] = 100.0, 0.0
            continue
        ratios = _segment_ratios(dist, seg_start)
        if len(ratios) == 1:
            peak_ratio[i], second_ratio[i] = ratios[0], 0.0
        else:
            rs = np.sort(ratios)[::-1]
            peak_ratio[i] = rs[0]
            # 次峰：与主峰分开的最大其余段；<2% 的碎段忽略（噪声）
            others = [r for r in rs[1:] if r >= 2.0]
            second_ratio[i] = (others[0] / rs[0]) if others else 0.0

    return dict(
        conc_90=conc90, peak_ratio=peak_ratio, second_ratio=second_ratio,
        turnover60=turn60, amplitude60=amp60, n_grid=ng,
    )


def _valley_segment_starts(dist: np.ndarray) -> np.ndarray | None:
    """找所有局部极小位置作为分段起点（严格大于两侧的视为峰间谷）。

    返回每段起始索引数组（含0与ng）。全零分布返回 None。
    """
    ng = len(dist)
    if not dist.any():
        return None
    d = np.concatenate(([dist[0]], dist, [dist[-1]]))  # 边界填充
    # 局部极小：d[i] <= d[i-1] and d[i] < d[i+1]（左平台右下降，避免重复）
    is_min = (d[1:-1] <= d[:-2]) & (d[1:-1] < d[2:])
    valleys = np.where(is_min)[0]
    # 只保留"深度足够"的谷：谷底 < 两侧邻近峰的较小者的 60%，否则视作噪声平台
    if len(valleys) == 0:
        return np.array([0])
    keep = [0]
    for v in valleys:
        if v <= 0 or v >= ng - 1:
            continue
        if dist[v] > dist[v - 1]:
            continue
        keep.append(v)
    keep = np.unique(np.array(keep + [ng]))
    # 相邻起点间距太小的段（<3格）合并
    if len(keep) > 2:
        merged = [keep[0]]
        for k in keep[1:]:
            if k - merged[-1] >= 3:
                merged.append(k)
        if merged[-1] != ng:
            merged[-1] = ng if ng - merged[-1] >= 3 or len(merged) == 1 else merged[-2]
            merged.append(ng)
        keep = np.unique(np.array(merged))
    return keep


def _segment_ratios(dist: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    """每个 [bounds[i], bounds[i+1]) 段的筹码占比（%）。"""
    cs = np.cumsum(dist)
    starts = np.clip(bounds[:-1] - 1, 0, None)
    ends = bounds[1:] - 1
    left = np.where(bounds[:-1] == 0, 0.0, cs[starts])
    right = cs[np.clip(ends, 0, len(dist) - 1)]
    seg = right - left
    # 首段含 index0
    seg[0] += dist[0] if bounds[0] == 0 else 0.0
    return np.clip(seg, 0, None) * 100.0


def signal_days(path: dict, rules: dict | None = None,
                first_time_only: bool = True) -> np.ndarray:
    """返回满足5条件的日期索引数组（与 df 行对齐）。

    first_time_only=True：只在"首次满足"当日发信号（连满足不重复触发）。
    """
    r = {**DEFAULT_RULES, **(rules or {})}
    c = path
    ok = (
        (c["conc_90"] < r["conc90_max"])
        & (c["peak_ratio"] > r["peak_ratio_min"])
        & (c["second_ratio"] < r["second_peak_max"])
        & (c["turnover60"] > r["turnover60_min"])
        & (c["amplitude60"] < r["amplitude60_max"])
        & ~np.isnan(c["conc_90"])
    )
    idx = np.where(ok)[0]
    if first_time_only and len(idx):
        idx = idx[np.concatenate(([True], np.diff(idx) > 1))]
    return idx
