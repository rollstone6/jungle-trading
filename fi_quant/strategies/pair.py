"""
策略二：铜价配对交易策略（Pair Trading Strategy）
=================================================
核心逻辑：成本对冲
- 电解铜（铜箔）占覆铜板生产成本的30%-40%
- 铜价暴涨，毛利率受损，股价承压
- 铜价暴跌或高位回落，毛利率释放，股价暴涨

量化判定条件：
1. 铜价高位破位：沪铜期货跌破20日均线
2. 生益被错杀：股价处于60日箱体下沿
3. Ratio偏离：Price铜/Price生益 偏离120日均值 -2倍标准差

信号位置：scan_signals() 方法
"""

import pandas as pd
import talib

from config import StrategyConfig


class PairTradingEngine:
    """铜价配对交易策略引擎"""

    def __init__(self, config: StrategyConfig, copper_df: pd.DataFrame):
        """
        Args:
            config: 策略配置
            copper_df: 铜价期货DataFrame
        """
        self.cfg = config
        self.copper_df = copper_df

    def prepare_indicators(self, df):
        """计算技术指标"""
        close = df['close'].values.astype(float)

        # 箱体高低点
        df['box_high'] = df['high'].rolling(60).max()
        df['box_low'] = df['low'].rolling(60).min()

        return df

    def prepare_copper_indicators(self):
        """计算铜价指标"""
        copper = self.copper_df.copy()
        close = copper['close'].values.astype(float)

        # 铜价均线
        copper['ma'] = talib.SMA(close, timeperiod=self.cfg.copper_ma_days)

        self.copper_df = copper
        return copper

    def calc_ratio(self, stock_df, copper_df):
        """
        计算Ratio = 铜价 / 股价
        需要对齐日期
        """
        # 合并数据
        merged = pd.merge(
            stock_df[['date', 'close']],
            copper_df[['date', 'close']],
            on='date',
            suffixes=('_stock', '_copper'),
            how='inner'
        )

        merged['ratio'] = merged['close_copper'] / merged['close_stock']

        # 计算Ratio的均值和标准差
        merged['ratio_ma'] = merged['ratio'].rolling(
            self.cfg.ratio_ma_days
        ).mean()
        merged['ratio_std'] = merged['ratio'].rolling(
            self.cfg.ratio_ma_days
        ).std()

        # 计算偏离度（标准差倍数）
        merged['ratio_zscore'] = (
            (merged['ratio'] - merged['ratio_ma']) / merged['ratio_std']
        )

        return merged

    def check_copper_breakdown(self, copper_df, date):
        """
        检查铜价是否高位破位（跌破均线）
        """
        row = copper_df[copper_df['date'] == date]
        if row.empty:
            return False

        row = row.iloc[0]
        if pd.isna(row['ma']):
            return False

        # 铜价跌破均线
        return row['close'] < row['ma']

    def scan_signals(self, stock_df, debug=False):
        """
        扫描配对交易信号
        Args:
            stock_df: 股票DataFrame
            debug: 是否输出调试信息
        Returns:
            信号DataFrame
        """
        stock_df = self.prepare_indicators(stock_df)
        copper_df = self.prepare_copper_indicators()

        # 计算Ratio
        merged = self.calc_ratio(stock_df, copper_df)

        signals = []
        stats = {
            'total_days': 0,
            'pass_copper_break': 0,
            'pass_box_lower': 0,
            'pass_ratio': 0,
        }

        start_idx = max(self.cfg.ratio_ma_days, 60)
        for i in range(start_idx, len(merged)):
            stats['total_days'] += 1
            row = merged.iloc[i]
            date = row['date']

            # 条件1：铜价高位破位
            if not self.check_copper_breakdown(copper_df, date):
                continue
            stats['pass_copper_break'] += 1

            # 条件2：股价处于箱体下沿
            stock_row = stock_df[stock_df['date'] == date]
            if stock_row.empty:
                continue
            stock_row = stock_row.iloc[0]

            box_high = stock_row['box_high']
            box_low = stock_row['box_low']
            if pd.isna(box_high) or pd.isna(box_low):
                continue

            box_range = box_high - box_low
            lower_bound = box_low + box_range * self.cfg.box_lower_pct
            if stock_row['close'] > lower_bound:
                continue
            stats['pass_box_lower'] += 1

            # 条件3：Ratio极度偏离
            if pd.isna(row['ratio_zscore']):
                continue
            if row['ratio_zscore'] > self.cfg.ratio_std_threshold:
                continue
            stats['pass_ratio'] += 1

            # 生成信号
            signals.append({
                'date': date.strftime('%Y-%m-%d'),
                'close': round(stock_row['close'], 2),
                'copper_price': round(row['close_copper'], 2),
                'ratio': round(row['ratio'], 4),
                'ratio_zscore': round(row['ratio_zscore'], 2),
                'signal_type': '配对交易',
            })

        if debug:
            print(f"     [调试-配对交易] 总天数:{stats['total_days']}"
                  f"  铜价破位:{stats['pass_copper_break']}"
                  f"  箱体下沿:{stats['pass_box_lower']}"
                  f"  Ratio偏离:{stats['pass_ratio']}")

        return pd.DataFrame(signals)