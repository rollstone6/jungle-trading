"""
主力吸筹评分模块（AccumulationEngine 的评分因子部分）
=====================================================

从 accumulation.py 拆出的五大评分因子，以 Mixin 形式供引擎复用：

1. 低位承接因子   (30分)：价格位置 + 近10根K线创新低次数
2. 主力吸货因子   (25分)：恐慌释放/低位增强的滚动归一化（向量化预计算）
3. OBV资金流因子 (20分)：OBV > MA10、OBV 接近 40 周期新高
4. 成交量吸筹因子 (15分)：上涨均量 / 下跌均量 的量比分档
5. RSI修复因子   (10分)：RSI14 处于 30-55 吸筹区间

关键预计算（向量化，整列一次算完）：
- acc_score_precomputed : 主力吸货因子
- new_low_count         : 滚动窗口创新低次数
- volume_ratio          : 上涨/下跌平均量比

引擎侧（信号扫描、突破确认、筹码过滤）见 accumulation.py。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import talib


class AccumulationScoring:
    """吸筹评分 Mixin：依赖 ``self.cfg``（StrategyConfig）。"""

    # ------------------------------------------------------------------
    # 预计算（向量化）
    # ------------------------------------------------------------------

    def _precompute_acc_score(self, df):
        """预计算吸筹因子评分（向量化优化版）"""
        low = df['low'].values.astype(float)
        n = len(low)
        result = np.zeros(n)

        if n < 40:
            return result

        # Step1: 低点变化
        low_change = np.abs(np.diff(low, prepend=low[0]))

        # Step2: 恐慌释放
        sma_low_change = talib.SMA(low_change, timeperiod=3)
        low_diff = np.diff(low, prepend=low[0])
        positive_change = np.maximum(-low_diff, 0)
        sma_positive = talib.SMA(positive_change, timeperiod=3)

        with np.errstate(divide='ignore', invalid='ignore'):
            panic = np.where(sma_positive > 0, sma_low_change / sma_positive, 1.0)

        # Step3: 低位增强
        ema_panic = talib.EMA(panic, timeperiod=3)

        # 计算每个位置的 llv40
        llv40 = talib.MIN(low, timeperiod=self.cfg.acc_low_period)

        with np.errstate(divide='ignore', invalid='ignore'):
            acc = np.where(low > 0, ema_panic * (llv40 / low), 0)

        # 向量化滚动归一化（40周期窗口）
        acc_series = pd.Series(acc)
        rolling_max = acc_series.rolling(window=40, min_periods=1).max().values

        with np.errstate(divide='ignore', invalid='ignore'):
            normalized = np.where(
                (rolling_max > 0) & (~np.isnan(acc)),
                (acc / rolling_max) * 25,
                0
            )

        # 裁剪到 [0, 25] 范围
        result = np.clip(normalized, 0, 25)
        result[:40] = 0  # 前40个数据不计算

        return result

    def _precompute_new_low_count(self, df, window=10):
        """预计算创新低次数（向量化优化版）

        使用 cummin 和滚动窗口计算创新低次数
        """
        low = df['low'].values.astype(float)
        n = len(low)
        result = np.zeros(n, dtype=int)

        if n <= window:
            return result

        low_series = pd.Series(low)

        # 前window-1期的最小值（不包含当前值）
        prev_min = low_series.shift(1).rolling(window=window-1, min_periods=1).min().values

        # 当前值 < 前window-1期最小值 = 创新低
        is_new_low = (low < prev_min).astype(int)
        is_new_low[:window] = 0

        # 滚动求和得到窗口内创新低次数
        result = pd.Series(is_new_low).rolling(window=window, min_periods=1).sum().values.astype(int)
        result[:window] = 0

        return result

    def _precompute_volume_ratio(self, df, window=20):
        """预计算量比（向量化优化版）：上涨平均量 / 下跌平均量"""
        close = df['close'].values.astype(float)
        open_price = df['open'].values.astype(float)
        volume = df['volume'].values.astype(float)
        n = len(close)
        result = np.zeros(n)

        if n <= window:
            return result

        is_up = close > open_price
        is_down = close < open_price

        up_volume = np.where(is_up, volume, 0)
        down_volume = np.where(is_down, volume, 0)
        up_flag = is_up.astype(float)
        down_flag = is_down.astype(float)

        up_vol_sum = pd.Series(up_volume).rolling(window=window, min_periods=1).sum().values
        down_vol_sum = pd.Series(down_volume).rolling(window=window, min_periods=1).sum().values
        up_count = pd.Series(up_flag).rolling(window=window, min_periods=1).sum().values
        down_count = pd.Series(down_flag).rolling(window=window, min_periods=1).sum().values

        with np.errstate(divide='ignore', invalid='ignore'):
            up_avg = np.where(up_count > 0, up_vol_sum / up_count, 0)
            down_avg = np.where(down_count > 0, down_vol_sum / down_count, 1)
            result = np.where(down_avg > 0, up_avg / down_avg, 0)

        result[:window] = 0

        return result

    # ------------------------------------------------------------------
    # 五大评分因子
    # ------------------------------------------------------------------

    def _calc_low_support_score(self, df, idx):
        """
        低位承接因子（30分）

        A. 价格靠近低位: position < 阈值 → 15分
        B. 最近10根K线低点不再创新低: new_low_count <= 2 → 15分
        """
        score = 0
        detail = {}

        close = df.iloc[idx]['close']
        hhv40 = df.iloc[idx]['hhv40']
        llv40 = df.iloc[idx]['llv40']

        if hhv40 == llv40:
            position = 0.5
        else:
            position = (close - llv40) / (hhv40 - llv40)

        detail['position'] = round(position, 3)
        detail['llv40'] = round(llv40, 2)
        detail['hhv40'] = round(hhv40, 2)

        if position < self.cfg.acc_position_threshold:
            score += 15
            detail['low_position'] = True
        else:
            detail['low_position'] = False

        new_low_count = int(df.iloc[idx]['new_low_count'])
        detail['new_low_count'] = new_low_count
        if new_low_count <= 2:
            score += 15
            detail['low_support'] = True
        else:
            detail['low_support'] = False

        detail['score'] = score
        return score, detail

    def _calc_accumulation_score(self, df, idx):
        """主力吸货因子（25分）：直接读取预计算列"""
        acc_score = float(df.iloc[idx]['acc_score_precomputed'])

        detail = {
            'score': round(acc_score, 1),
            'acc_value': 0,
            'panic': 0,
        }

        return round(acc_score, 1), detail

    def _calc_obv_score(self, df, idx):
        """
        OBV资金流因子（20分）

        - OBV > MA10(OBV) → +10分
        - OBV接近40周期新高 (>= 95%) → +10分
        """
        score = 0
        detail = {}

        obv = df.iloc[idx]['obv']
        obv_ma10 = df.iloc[idx]['obv_ma10']
        obv_hhv40 = df.iloc[idx]['obv_hhv40']

        detail['obv'] = round(obv, 0)
        detail['obv_ma10'] = round(obv_ma10, 0)
        detail['obv_hhv40'] = round(obv_hhv40, 0)

        if obv > obv_ma10:
            score += 10
            detail['above_ma10'] = True
        else:
            detail['above_ma10'] = False

        if obv_hhv40 > 0:
            obv_ratio = obv / obv_hhv40
            detail['obv_ratio'] = round(obv_ratio, 3)
            if obv_ratio >= 0.95:
                score += 10
                detail['near_high'] = True
            else:
                detail['near_high'] = False
        else:
            detail['near_high'] = False

        detail['score'] = score
        return score, detail

    def _calc_volume_score(self, df, idx):
        """
        成交量吸筹因子（15分）

        - ratio > 1.2 → 5分
        - ratio > 1.5 → 10分
        - ratio > 2.0 → 15分
        """
        score = 0
        detail = {}

        volume_ratio = float(df.iloc[idx]['volume_ratio'])
        detail['volume_ratio'] = round(volume_ratio, 2)

        if volume_ratio > 2.0:
            score = 15
        elif volume_ratio > 1.5:
            score = 10
        elif volume_ratio > 1.2:
            score = 5

        detail['score'] = score
        return score, detail

    def _calc_rsi_score(self, df, idx):
        """
        RSI修复因子（10分）

        吸筹阶段最佳RSI范围：30-55 → 10分
        """
        score = 0
        detail = {}

        rsi = df.iloc[idx]['rsi']
        detail['rsi'] = round(rsi, 1) if not np.isnan(rsi) else 0

        if not np.isnan(rsi):
            if self.cfg.acc_rsi_min < rsi < self.cfg.acc_rsi_max:
                score = 10
                detail['in_range'] = True
            else:
                detail['in_range'] = False
        else:
            detail['in_range'] = False

        detail['score'] = score
        return score, detail

    def calc_total_score(self, df, idx):
        """
        计算综合吸筹评分（0-100分）

        返回:
            tuple: (总分, 各因子详情dict)
        """
        low_support_score, low_support_detail = self._calc_low_support_score(df, idx)
        acc_score, acc_detail = self._calc_accumulation_score(df, idx)
        obv_score, obv_detail = self._calc_obv_score(df, idx)
        vol_score, vol_detail = self._calc_volume_score(df, idx)
        rsi_score, rsi_detail = self._calc_rsi_score(df, idx)

        total_score = low_support_score + acc_score + obv_score + vol_score + rsi_score

        details = {
            'low_support': low_support_detail,
            'accumulation': acc_detail,
            'obv': obv_detail,
            'volume': vol_detail,
            'rsi': rsi_detail,
            'total_score': round(total_score, 1),
        }

        return total_score, details

    # ------------------------------------------------------------------
    # 展示辅助
    # ------------------------------------------------------------------

    def _format_score_detail(self, details):
        """格式化评分详情为字符串"""
        return (
            f"低位:{details['low_support']['score']}/30 "
            f"吸货:{details['accumulation']['score']}/25 "
            f"OBV:{details['obv']['score']}/20 "
            f"量能:{details['volume']['score']}/15 "
            f"RSI:{details['rsi']['score']}/10"
        )

    def _print_signal_detail(self, sig, score_details, breakout_detail):
        """打印信号详细信息"""
        md = sig.metadata
        print(f"\n     📍 信号时间: {sig.date}")
        print(f"        收盘价: {sig.close}  MA20: {md.get('ma20')}")
        print(f"        吸筹评分: {md.get('score')}分")
        print("        评分明细:")
        ls = score_details['low_support']
        print(
            f"          - 低位承接: {ls['score']}/30 "
            f"(位置:{ls.get('position', '-')})"
        )
        acc = score_details['accumulation']
        print(f"          - 主力吸货: {acc['score']}/25")
        obv = score_details['obv']
        print(
            f"          - OBV资金流: {obv['score']}/20 "
            f"(OBV>MA10:{obv.get('above_ma10', '-')})"
        )
        vol = score_details['volume']
        print(
            f"          - 成交量: {vol['score']}/15 "
            f"(量比:{vol.get('volume_ratio', '-')})"
        )
        rsi = score_details['rsi']
        print(
            f"          - RSI修复: {rsi['score']}/10 "
            f"(RSI:{rsi.get('rsi', '-')})"
        )
        print(f"        放量确认: {md.get('vol_ratio')}倍")
        print(
            f"        止损: {sig.stop_loss}  止盈: {sig.take_profit}"
        )
