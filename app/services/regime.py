"""市场状态分类器 - 布林带带宽/趋势量化分类

把一只股票的近期走势量化为四类状态，用于推荐匹配的量化策略：

- squeeze   挤压待变：带宽处于自身历史低位（近120根最低20%分位），变盘前兆
- trending  趋势型：近20根K线中 >=20% 收盘在轨道外，或均线多头排列且偏离MA20超5%
- ranging   区间型：近20根中 <=5% 在轨道外且非多头排列，价格严格回归轨道
- neutral   中性：以上都不满足，特征不明显

量化指标：
- outside_ratio  轨外运行比例 = 近20根中 (close>上轨 或 close<下轨) 的比例
- bandwidth_pct  带宽百分位   = 当前带宽在过去120根中的分位（0~1）
- bullish_align  均线多头排列 = MA5 > MA10 > MA20 且收盘价 > MA5
- ma_dev_pct     偏离度       = (收盘价 / MA20 - 1) * 100
"""
import pandas as pd

REGIME_LABEL = {
    "squeeze": "挤压待变",
    "trending": "趋势型",
    "ranging": "区间型",
    "neutral": "中性",
}


def classify_regime(klines: list[dict]) -> dict:
    """输入K线列表，输出量化分类结果"""
    if not klines or len(klines) < 30:
        return {"regime": "unknown", "reason": "K线数据不足（至少30根）"}

    df = pd.DataFrame(klines)
    close = df["close"].astype(float)
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std(ddof=0)
    upper = ma20 + 2 * std20
    lower = ma20 - 2 * std20
    bandwidth = (upper - lower) / ma20

    n = len(df)
    recent = min(20, n)
    outside = ((close > upper) | (close < lower)).astype(float)
    outside_ratio = float(outside.tail(recent).mean())

    lookback = bandwidth.tail(min(120, n))
    bw_pct = float((lookback <= bandwidth.iloc[-1]).mean())

    ma5 = float(close.rolling(5).mean().iloc[-1])
    ma10 = float(close.rolling(10).mean().iloc[-1])
    ma20_last = float(ma20.iloc[-1])
    last_close = float(close.iloc[-1])
    bullish_align = bool(ma5 > ma10 > ma20_last and last_close > ma5)
    ma_dev = (last_close / ma20_last - 1) if ma20_last else 0.0

    if bw_pct <= 0.20:
        regime = "squeeze"
    elif outside_ratio >= 0.20 or (bullish_align and ma_dev > 0.05):
        regime = "trending"
    elif outside_ratio <= 0.05 and not bullish_align:
        regime = "ranging"
    else:
        regime = "neutral"

    # 带宽张开预警：昨日带宽仍在20%低分位内，今日收盘已突破轨道
    alert = None
    if n >= 2 and bw_pct > 0.20:
        prev_bw_pct = float((lookback.iloc[:-1] <= bandwidth.iloc[-2]).mean())
        if prev_bw_pct <= 0.20:
            if last_close > float(upper.iloc[-1]):
                alert = ("squeeze_break_up", "挤压后向上突破上轨，关注变盘向上")
            elif last_close < float(lower.iloc[-1]):
                alert = ("squeeze_break_down", "挤压后向下突破下轨，警惕变盘向下")

    return {
        "regime": regime,
        "label": REGIME_LABEL[regime],
        "outside_ratio": round(outside_ratio, 3),
        "bandwidth": round(float(bandwidth.iloc[-1]), 4),
        "bandwidth_pct": round(bw_pct, 3),
        "bullish_align": bullish_align,
        "ma_dev_pct": round(ma_dev * 100, 2),
        "alert": alert,
        "bars": n,
    }


REGIME_ADVICE = {
    "squeeze": {
        "label": "挤压待变",
        "strategies": ["ma_macd", "macd_vol"],
        "note": "带宽处于自身历史低位，变盘前兆：先观望，带宽张开后顺势跟进",
    },
    "trending": {
        "label": "趋势型",
        "strategies": ["ma_macd", "macd_vol", "ma"],
        "note": "价格经常运行在轨道外，均值回归策略会连环止损——用趋势策略",
    },
    "ranging": {
        "label": "区间型",
        "strategies": ["boll", "rsi_boll", "kdj"],
        "note": "价格严格回归轨道，是布林带/KDJ 均值回归策略的主场",
    },
    "neutral": {
        "label": "中性",
        "strategies": ["kdj", "ma_macd"],
        "note": "特征不明显，建议小仓试错或等待状态明朗",
    },
    "unknown": {
        "label": "未知",
        "strategies": [],
        "note": "K线数据不足，无法分类",
    },
}
