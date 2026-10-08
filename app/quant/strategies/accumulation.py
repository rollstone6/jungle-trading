"""
60分钟主力吸筹策略引擎
======================
核心逻辑：
1. 日线趋势过滤（复用 TrendFilter）
2. 60分钟吸筹评分（0-100分）
   - 低位承接因子 (30分)
   - 主力吸货因子 (25分)
   - OBV资金流因子 (20分)
   - 成交量吸筹因子 (15分)
   - RSI修复因子 (10分)
3. 60分钟突破确认
   - close_60m > MA20_60m
   - volume > MA20_volume * 1.3
"""

import pandas as pd
import numpy as np
import talib

from app.quant.config import StrategyConfig
from app.quant.core.signal import Signal, SignalFrame
from app.quant.core.strategy_base import StrategyBase
from app.quant.utils.chip_distribution import ChipDistributionAnalyzer


class AccumulationEngine(StrategyBase):
    """60分钟主力吸筹策略引擎（集成筹码集中度过滤）"""

    name = "主力吸筹"

    def __init__(self, config: StrategyConfig):
        self.cfg = config
        self.chip_analyzer = ChipDistributionAnalyzer(config)
        self._chip_cache = {}  # 筹码分析结果缓存

    def prepare_indicators(self, df):
        """
        计算60分钟K线所需的技术指标（预计算优化版）
        
        参数:
            df: DataFrame，包含 date, open, high, low, close, volume
            
        返回:
            添加了技术指标列的 DataFrame
        """
        close = df['close'].values.astype(float)
        high = df['high'].values.astype(float)
        low = df['low'].values.astype(float)
        volume = df['volume'].values.astype(float)
        open_price = df['open'].values.astype(float)

        # 1. 均线
        df['ma20'] = talib.SMA(close, timeperiod=20)
        df['ma10'] = talib.SMA(close, timeperiod=10)
        df['ma5'] = talib.SMA(close, timeperiod=5)

        # 2. OBV 及 OBV 均线
        df['obv'] = talib.OBV(close, volume)
        df['obv_ma10'] = talib.SMA(df['obv'].values, timeperiod=self.cfg.acc_obv_ma)

        # 3. RSI
        df['rsi'] = talib.RSI(close, timeperiod=14)

        # 4. 成交量均线
        df['vol_ma20'] = talib.SMA(volume, timeperiod=20)

        # 5. 最高价和最低价（用于计算位置）
        df['hhv40'] = talib.MAX(high, timeperiod=self.cfg.acc_low_period)
        df['llv40'] = talib.MIN(low, timeperiod=self.cfg.acc_low_period)

        # 6. OBV最高值（用于OBV评分）
        df['obv_hhv40'] = talib.MAX(df['obv'].values, timeperiod=self.cfg.acc_obv_period)

        # ===== 预计算优化：一次性计算所有评分因子 =====
        
        # 7. 预计算吸筹因子（整个数组）
        df['acc_score_precomputed'] = self._precompute_acc_score(df)
        
        # 8. 预计算创新低次数（滚动窗口）
        df['new_low_count'] = self._precompute_new_low_count(df, window=10)
        
        # 9. 预计算量比（滚动窗口）
        df['volume_ratio'] = self._precompute_volume_ratio(df, window=20)

        return df

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
        # 使用 pandas rolling 计算滚动最大值
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
        
        # 计算每个位置是否为"创新低"（比前面所有值都低）
        # 使用 expanding min 的移位版本
        low_series = pd.Series(low)
        
        # 计算前window-1期的最小值（不包含当前值）
        prev_min = low_series.shift(1).rolling(window=window-1, min_periods=1).min().values
        
        # 当前值 < 前window-1期最小值 = 创新低
        is_new_low = (low < prev_min).astype(int)
        is_new_low[:window] = 0  # 前window个不计算
        
        # 滚动求和得到窗口内创新低次数
        result = pd.Series(is_new_low).rolling(window=window, min_periods=1).sum().values.astype(int)
        result[:window] = 0
        
        return result

    def _precompute_volume_ratio(self, df, window=20):
        """预计算量比（向量化优化版）
        
        上涨平均量 / 下跌平均量
        """
        close = df['close'].values.astype(float)
        open_price = df['open'].values.astype(float)
        volume = df['volume'].values.astype(float)
        n = len(close)
        result = np.zeros(n)
        
        if n <= window:
            return result
        
        # 标记上涨/下跌
        is_up = close > open_price
        is_down = close < open_price
        
        # 上涨量（下跌时为0）
        up_volume = np.where(is_up, volume, 0)
        down_volume = np.where(is_down, volume, 0)
        
        # 上涨/下跌标记
        up_flag = is_up.astype(float)
        down_flag = is_down.astype(float)
        
        # 滚动求和
        up_vol_sum = pd.Series(up_volume).rolling(window=window, min_periods=1).sum().values
        down_vol_sum = pd.Series(down_volume).rolling(window=window, min_periods=1).sum().values
        up_count = pd.Series(up_flag).rolling(window=window, min_periods=1).sum().values
        down_count = pd.Series(down_flag).rolling(window=window, min_periods=1).sum().values
        
        # 计算平均量（避免除零）
        with np.errstate(divide='ignore', invalid='ignore'):
            up_avg = np.where(up_count > 0, up_vol_sum / up_count, 0)
            down_avg = np.where(down_count > 0, down_vol_sum / down_count, 1)
            result = np.where(down_avg > 0, up_avg / down_avg, 0)
        
        result[:window] = 0
        
        return result

    def _calc_low_support_score(self, df, idx):
        """
        低位承接因子（30分）- 使用预计算值优化版
        
        评分：
        A. 价格靠近低位: position < 0.35 → 15分
        B. 最近10根K线低点不再创新低: new_low_count <= 2 → 15分
        """
        score = 0
        detail = {}

        close = df.iloc[idx]['close']
        hhv40 = df.iloc[idx]['hhv40']
        llv40 = df.iloc[idx]['llv40']

        # 避免除零错误
        if hhv40 == llv40:
            position = 0.5
        else:
            position = (close - llv40) / (hhv40 - llv40)

        detail['position'] = round(position, 3)
        detail['llv40'] = round(llv40, 2)
        detail['hhv40'] = round(hhv40, 2)

        # 条件A: 价格靠近低位
        if position < self.cfg.acc_position_threshold:
            score += 15
            detail['low_position'] = True
        else:
            detail['low_position'] = False

        # 条件B: 使用预计算的创新低次数
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
        """
        改良主力吸货因子（25分）- 使用预计算值优化版
        
        直接读取预计算的 acc_score_precomputed 列
        """
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
        
        评分：
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

        # 条件1: OBV > MA10
        if obv > obv_ma10:
            score += 10
            detail['above_ma10'] = True
        else:
            detail['above_ma10'] = False

        # 条件2: OBV接近40周期新高 (>= 95%)
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
        成交量吸筹因子（15分）- 使用预计算值优化版
        
        评分：
        - ratio > 1.2 → 5分
        - ratio > 1.5 → 10分
        - ratio > 2.0 → 15分
        """
        score = 0
        detail = {}

        # 使用预计算的量比
        volume_ratio = float(df.iloc[idx]['volume_ratio'])
        detail['volume_ratio'] = round(volume_ratio, 2)

        # 评分
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
        
        吸筹阶段最佳RSI范围：30-55
        
        评分：
        - 30 < RSI14 < 55 → 10分
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
        # 计算各因子得分
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

    def check_breakout_confirmation(self, df, idx):
        """
        60分钟突破确认
        
        条件：
        - close_60m > MA20_60m
        - volume > MA20_volume * 1.3
        
        返回:
            tuple: (是否突破, 详情dict)
        """
        close = df.iloc[idx]['close']
        ma20 = df.iloc[idx]['ma20']
        volume = df.iloc[idx]['volume']
        vol_ma20 = df.iloc[idx]['vol_ma20']

        # 条件1: 收盘价 > MA20
        price_breakout = close > ma20 if not np.isnan(ma20) else False

        # 条件2: 放量
        vol_threshold = vol_ma20 * self.cfg.acc_volume_ratio if not np.isnan(vol_ma20) else 0
        volume_breakout = volume > vol_threshold if vol_threshold > 0 else False

        confirmed = price_breakout and volume_breakout

        detail = {
            'close': round(close, 2),
            'ma20': round(ma20, 2) if not np.isnan(ma20) else 0,
            'price_breakout': price_breakout,
            'volume': round(volume, 0),
            'vol_ma20': round(vol_ma20, 0) if not np.isnan(vol_ma20) else 0,
            'vol_threshold': round(vol_threshold, 0),
            'volume_breakout': volume_breakout,
        }

        return confirmed, detail

    def _get_chip_score(self, df, symbol, current_idx):
        """
        获取当前日期的筹码集中度评分（带缓存）
        
        参数:
            df: 60分钟K线数据
            symbol: 股票代码
            current_idx: 当前K线索引
            
        返回:
            tuple: (筹码评分0-100, 是否通过集中度筛选)
        """
        if not self.cfg.enable_chip_filter:
            return 100.0, True  # 未启用筹码过滤时默认通过
        
        try:
            # 获取当前日期
            current_date = df.iloc[current_idx]['date']
            if hasattr(current_date, 'date'):
                date_str = str(current_date.date())
            else:
                date_str = str(current_date)[:10]
            
            # 缓存键：symbol + date
            cache_key = f"{symbol}_{date_str}"
            
            if cache_key in self._chip_cache:
                cached = self._chip_cache[cache_key]
                return cached['score'], cached['passed']
            
            # 提取截至当前日期的日线数据（用于筹码计算）
            daily_data = self._extract_daily_data(df, current_idx)
            
            if daily_data.empty or len(daily_data) < 30:
                return 50.0, True  # 数据不足时默认通过
            
            # 计算筹码分布
            result = self.chip_analyzer.analyze_stock(daily_data)
            
            if result is None:
                return 50.0, True
            
            score = result.get('score', 50)
            passed = result.get('pass', True)
            
            # 缓存结果
            self._chip_cache[cache_key] = {
                'score': score,
                'passed': passed,
                'conc90': result.get('conc_90', 0),
            }
            
            return score, passed
            
        except Exception:
            return 50.0, True  # 异常时默认通过
    
    def _extract_daily_data(self, df_60m, current_idx):
        """
        从60分钟K线提取日线数据（用于筹码计算）
        
        参数:
            df_60m: 60分钟K线DataFrame
            current_idx: 当前索引位置
            
        返回:
            DataFrame: 日线数据 [date, open, high, low, close, volume]
        """
        try:
            # 截取到当前索引的数据
            df_slice = df_60m.iloc[:current_idx + 1].copy()
            
            if df_slice.empty:
                return pd.DataFrame()
            
            # 转换为日期格式
            df_slice['trade_date'] = pd.to_datetime(df_slice['date']).dt.date
            
            # 按日期聚合为日线
            daily = df_slice.groupby('trade_date').agg({
                'open': 'first',
                'high': 'max',
                'low': 'min',
                'close': 'last',
                'volume': 'sum',
            }).reset_index()
            
            daily = daily.rename(columns={'trade_date': 'date'})
            
            # 确保列顺序
            cols = ['date', 'open', 'high', 'low', 'close', 'volume']
            daily = daily[cols]
            
            return daily
            
        except Exception:
            return pd.DataFrame()

    def scan_signals(
        self, df, symbol: str = "", debug=False
    ) -> SignalFrame:
        """
        扫描满足条件的交易信号（集成筹码集中度过滤）
        
        参数:
            df: DataFrame，60分钟K线数据
            symbol: 股票代码
            debug: 是否输出调试信息
            
        返回:
            SignalFrame: 统一信号集合
        """
        # 计算技术指标
        df = self.prepare_indicators(df)
        signals: list[Signal] = []

        # 调试统计
        stats = {
            'total_bars': 0,
            'high_score': 0,  # 评分 >= 70
            'breakout': 0,    # 突破确认
            'chip_filter': 0, # 筹码过滤通过
            'signals': 0,     # 最终信号
        }

        # 从第50根K线开始扫描（确保有足够数据计算指标）
        start_idx = max(50, self.cfg.acc_low_period + 10)

        for i in range(start_idx, len(df)):
            stats['total_bars'] += 1

            # 计算吸筹评分
            total_score, score_details = self.calc_total_score(df, i)

            # 检查评分是否达标
            if total_score < self.cfg.acc_min_score:
                continue
            stats['high_score'] += 1

            # 检查突破确认
            breakout_confirmed, breakout_detail = self.check_breakout_confirmation(df, i)

            if not breakout_confirmed:
                continue
            stats['breakout'] += 1

            # ===== 筹码集中度过滤 =====
            chip_score, chip_passed = self._get_chip_score(df, symbol, i)
            
            if not chip_passed:
                continue
            stats['chip_filter'] += 1
            
            # 筹码评分加分（可选）
            if self.cfg.enable_chip_filter:
                chip_bonus = (chip_score / 100) * self.cfg.chip_score_bonus
                total_score += chip_bonus

            # 生成信号
            stats['signals'] += 1
            today_data = df.iloc[i]

            vol_ratio = (
                round(
                    breakout_detail['volume']
                    / breakout_detail['vol_ma20'],
                    2,
                )
                if breakout_detail['vol_ma20'] > 0
                else 0
            )
            signals.append(Signal(
                date=pd.Timestamp(today_data['date']),
                symbol=symbol,
                close=round(float(today_data['close']), 2),
                strategy_name=self.name,
                stop_loss=round(
                    float(today_data['close'])
                    * (1 - self.cfg.acc_stop_loss),
                    2,
                ),
                take_profit=round(
                    float(today_data['close'])
                    * (1 + self.cfg.acc_take_profit),
                    2,
                ),
                metadata={
                    'score': round(total_score, 1),
                    'score_detail': self._format_score_detail(
                        score_details
                    ),
                    'ma20': round(breakout_detail['ma20'], 2),
                    'volume': round(breakout_detail['volume'], 0),
                    'vol_ratio': vol_ratio,
                    'chip_score': round(chip_score, 1) if self.cfg.enable_chip_filter else None,
                },
            ))

            if debug:
                self._print_signal_detail(
                    signals[-1], score_details, breakout_detail
                )

        if debug:
            print(f"     [调试] K线:{stats['total_bars']}"
                  f"  高分:{stats['high_score']}"
                  f"  突破:{stats['breakout']}"
                  f"  筹码通过:{stats['chip_filter']}"
                  f"  信号:{stats['signals']}")

        return SignalFrame(signals)

    def _format_score_detail(self, details):
        """格式化评分详情为字符串"""
        return (
            f"低位:{details['low_support']['score']}/30 "
            f"吸货:{details['accumulation']['score']}/25 "
            f"OBV:{details['obv']['score']}/20 "
            f"量能:{details['volume']['score']}/15 "
            f"RSI:{details['rsi']['score']}/10"
        )

    def _print_signal_detail(
        self, sig: Signal, score_details, breakout_detail
    ):
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