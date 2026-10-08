"""
策略一：产业链动量滞后策略（Lead-Lag Strategy）
==============================================
核心逻辑：下游动量传导
- 下游PCB企业股价异动往往比上游覆铜板企业更敏锐
- 当下游PCB龙头带量启动后，上游存在2-5个交易日的"动量滞后"
- 在上游尚未突破前，左侧提前潜伏

量化判定条件：
1. 下游强势启动：下游PCB龙头过去3日累计涨幅 > 8%
2. 上游滞后滞涨：上游涨幅落后下游均值 > 5%
3. 资金预备流入：OBV指标开始勾头向上

信号位置：scan_signals() 方法
"""

import pandas as pd
import talib

from config import StrategyConfig


class LeadLagEngine:
    """产业链动量滞后策略引擎"""

    def __init__(self, config: StrategyConfig, downstream_data: dict):
        """
        Args:
            config: 策略配置
            downstream_data: 下游股票数据字典 {symbol: DataFrame}
        """
        self.cfg = config
        self.downstream_data = downstream_data

    def prepare_indicators(self, df):
        """计算技术指标"""
        close = df['close'].values.astype(float)
        volume = df['volume'].values.astype(float)

        # OBV
        df['obv'] = talib.OBV(close, volume)
        df['obv_ma5'] = talib.SMA(df['obv'].values, timeperiod=5)

        return df

    def calc_downstream_momentum(self, end_idx):
        """
        计算下游股票池的累计涨幅
        Args:
            end_idx: 当前日期索引
        Returns:
            下游平均涨幅
        """
        gains = []
        for symbol, df in self.downstream_data.items():
            if end_idx >= self.cfg.downstream_days and end_idx < len(df):
                start_idx = end_idx - self.cfg.downstream_days
                start_price = df.iloc[start_idx]['close']
                end_price = df.iloc[end_idx]['close']
                gain = (end_price - start_price) / start_price
                gains.append(gain)

        if not gains:
            return 0.0
        return sum(gains) / len(gains)

    def check_obv_turn_up(self, df, idx):
        """
        检查OBV是否开始勾头向上
        Args:
            df: 上游股票DataFrame
            idx: 当前索引
        Returns:
            bool
        """
        if idx < self.cfg.obv_turn_days:
            return False

        obv_values = df['obv'].iloc[idx - self.cfg.obv_turn_days:idx + 1]
        if len(obv_values) < 2:
            return False

        # 最近OBV高于前几天均值
        recent_obv = obv_values.iloc[-1]
        prev_obv_mean = obv_values.iloc[:-1].mean()

        return recent_obv > prev_obv_mean

    def scan_signals(self, upstream_df, debug=False):
        """
        扫描动量滞后信号
        Args:
            upstream_df: 上游股票DataFrame
            debug: 是否输出调试信息
        Returns:
            信号DataFrame
        """
        upstream_df = self.prepare_indicators(upstream_df)
        signals = []

        stats = {
            'total_days': 0,
            'pass_downstream': 0,
            'pass_lag': 0,
            'pass_obv': 0,
        }

        start_idx = max(self.cfg.downstream_days, 30)
        for i in range(start_idx, len(upstream_df)):
            stats['total_days'] += 1
            today = upstream_df.iloc[i]

            # 条件1：下游强势启动
            downstream_gain = self.calc_downstream_momentum(i)
            if downstream_gain < self.cfg.downstream_threshold:
                continue
            stats['pass_downstream'] += 1

            # 条件2：上游滞后滞涨
            upstream_gain = 0.0
            lag = 0.0
            if i >= self.cfg.downstream_days:
                prev_idx = i - self.cfg.downstream_days
                row = upstream_df.iloc[prev_idx]
                upstream_start = row['close']
                upstream_end = today['close']
                diff = upstream_end - upstream_start
                upstream_gain = diff / upstream_start
                lag = downstream_gain - upstream_gain
                if lag < self.cfg.upstream_lag_threshold:
                    continue
            stats['pass_lag'] += 1

            # 条件3：OBV勾头向上
            if not self.check_obv_turn_up(upstream_df, i):
                continue
            stats['pass_obv'] += 1

            # 生成信号
            signals.append({
                'date': today['date'].strftime('%Y-%m-%d'),
                'close': round(today['close'], 2),
                'downstream_gain': f"{round(downstream_gain * 100, 2)}%",
                'upstream_gain': f"{round(upstream_gain * 100, 2)}%",
                'lag': f"{round(lag * 100, 2)}%",
                'signal_type': '动量滞后',
            })

        if debug:
            print(f"     [调试-动量滞后] 总天数:{stats['total_days']}"
                  f"  下游启动:{stats['pass_downstream']}"
                  f"  上游滞后:{stats['pass_lag']}"
                  f"  OBV勾头:{stats['pass_obv']}")

        return pd.DataFrame(signals)