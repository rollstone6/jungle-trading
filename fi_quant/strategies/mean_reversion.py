"""
策略四：均值回归+周期共振策略（Mean Reversion Strategy）
=======================================================
核心逻辑：大周期支撑
- 股价触及120日/250日均线（半年线/年线）
- RSI极度超卖 < 25
- MACD底背离

量化判定条件：
1. 大周期支撑：股价触及120日或250日均线 ±2%
2. RSI极度超卖：RSI < 25
3. MACD底背离：股价创新低但MACD未创新低

信号位置：scan_signals() 方法
"""

import pandas as pd
import talib

from config import StrategyConfig
from core.signal import Signal, SignalFrame
from core.strategy_base import StrategyBase


class MeanReversionEngine(StrategyBase):
    """均值回归+周期共振策略引擎"""

    name = "均值回归"

    def __init__(self, config: StrategyConfig):
        """
        Args:
            config: 策略配置
        """
        self.cfg = config

    def prepare_indicators(self, df):
        """计算技术指标"""
        close = df['close'].values.astype(float)

        # 长期均线
        df['ma120'] = talib.SMA(close, timeperiod=self.cfg.ma_long_days)
        df['ma250'] = talib.SMA(close, timeperiod=self.cfg.ma_year_days)

        # RSI
        df['rsi'] = talib.RSI(close, timeperiod=14)

        # MACD
        macd, signal, hist = talib.MACD(close)
        df['macd'] = macd
        df['macd_signal'] = signal
        df['macd_hist'] = hist

        return df

    def check_ma_support(self, close, ma120, ma250):
        """
        检查是否触及大周期均线支撑
        """
        if pd.isna(ma120) and pd.isna(ma250):
            return False, None

        # 检查120日均线
        if not pd.isna(ma120):
            deviation = abs(close - ma120) / ma120
            if deviation <= self.cfg.ma_deviation:
                return True, 'MA120'

        # 检查250日均线
        if not pd.isna(ma250):
            deviation = abs(close - ma250) / ma250
            if deviation <= self.cfg.ma_deviation:
                return True, 'MA250'

        return False, None

    def check_macd_divergence(self, df, idx):
        """
        检查MACD底背离
        股价创新低但MACD未创新低
        """
        if idx < self.cfg.macd_divergence_days * 2:
            return False

        # 当前价格区间
        current_low = df['low'].iloc[
            idx - self.cfg.macd_divergence_days:idx + 1
        ].min()
        current_macd = df['macd'].iloc[idx]

        # 前一个价格区间
        prev_low = df['low'].iloc[
            idx - self.cfg.macd_divergence_days * 2:
            idx - self.cfg.macd_divergence_days
        ].min()
        prev_macd = df['macd'].iloc[idx - self.cfg.macd_divergence_days]

        if pd.isna(current_macd) or pd.isna(prev_macd):
            return False

        # 底背离：股价创新低但MACD未创新低
        price_lower = current_low < prev_low
        macd_higher = current_macd > prev_macd

        return price_lower and macd_higher

    def scan_signals(
        self, df, symbol: str = "", debug=False
    ) -> SignalFrame:
        """
        扫描均值回归信号
        Args:
            df: 股票DataFrame
            symbol: 股票代码
            debug: 是否输出调试信息
        Returns:
            SignalFrame: 统一信号集合
        """
        stock_df = self.prepare_indicators(df)
        signals: list[Signal] = []

        stats = {
            'total_days': 0,
            'pass_ma_support': 0,
            'pass_rsi_oversold': 0,
            'pass_macd_divergence': 0,
        }

        start_idx = max(self.cfg.ma_year_days, 250)
        for i in range(start_idx, len(stock_df)):
            stats['total_days'] += 1
            today = stock_df.iloc[i]

            # 条件1：大周期均线支撑
            has_support, ma_type = self.check_ma_support(
                today['close'], today['ma120'], today['ma250']
            )
            if not has_support:
                continue
            stats['pass_ma_support'] += 1

            # 条件2：RSI极度超卖
            if pd.isna(today['rsi']):
                continue
            if today['rsi'] >= self.cfg.rsi_oversold:
                continue
            stats['pass_rsi_oversold'] += 1

            # 条件3：MACD底背离
            if not self.check_macd_divergence(stock_df, i):
                continue
            stats['pass_macd_divergence'] += 1

            # 生成信号
            signals.append(Signal(
                date=pd.Timestamp(today['date']),
                symbol=symbol,
                close=round(float(today['close']), 2),
                strategy_name=self.name,
                metadata={
                    'ma_support': ma_type,
                    'rsi': round(float(today['rsi']), 2),
                    'macd': round(float(today['macd']), 4),
                },
            ))

        if debug:
            print(f"     [调试-均值回归] 总天数:{stats['total_days']}"
                  f"  均线支撑:{stats['pass_ma_support']}"
                  f"  RSI超卖:{stats['pass_rsi_oversold']}"
                  f"  MACD背离:{stats['pass_macd_divergence']}")

        return SignalFrame(signals)