"""
60分钟主力吸筹策略引擎
======================
核心逻辑：
1. 日线趋势过滤（复用 TrendFilter）
2. 60分钟吸筹评分（0-100分），评分因子见 accumulation_scoring.py
   - 低位承接因子 (30分)
   - 主力吸货因子 (25分)
   - OBV资金流因子 (20分)
   - 成交量吸筹因子 (15分)
   - RSI修复因子 (10分)
3. 60分钟突破确认
   - close_60m > MA20_60m
   - volume > MA20_volume * 1.3
4. 筹码集中度过滤（复用 ChipDistributionAnalyzer）
"""

import pandas as pd
import numpy as np
import talib

from app.quant.config import StrategyConfig
from app.quant.core.signal import Signal, SignalFrame
from app.quant.core.strategy_base import StrategyBase
from app.quant.strategies.accumulation_scoring import AccumulationScoring
from app.quant.utils.chip_distribution import ChipDistributionAnalyzer


class AccumulationEngine(AccumulationScoring, StrategyBase):
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

        # ===== 预计算优化：一次性计算所有评分因子（见 accumulation_scoring.py） =====

        # 7. 预计算吸筹因子（整个数组）
        df['acc_score_precomputed'] = self._precompute_acc_score(df)

        # 8. 预计算创新低次数（滚动窗口）
        df['new_low_count'] = self._precompute_new_low_count(df, window=10)

        # 9. 预计算量比（滚动窗口）
        df['volume_ratio'] = self._precompute_volume_ratio(df, window=20)

        return df

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
