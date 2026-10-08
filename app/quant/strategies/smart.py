"""
策略三：聪明资金追踪策略（Smart Money Strategy）
================================================
核心逻辑：筹码清洗
- 主力资金逆势抄底
- 融资盘（杠杆资金）爆仓离场
- 主力完美接盘，散户割肉出局

量化判定条件：
1. 主力逆势抄底：主力资金连续3天净流入
2. 杠杆客爆仓：融资余额出现断崖式下跌 > 15%
3. 股价破下轨：跌破布林带下轨

信号位置：scan_signals() 方法
"""

import pandas as pd
import talib

from app.quant.config import StrategyConfig


class SmartMoneyEngine:
    """聪明资金追踪策略引擎"""

    def __init__(
        self,
        config: StrategyConfig,
        fund_flow_df: pd.DataFrame,
        margin_df: pd.DataFrame
    ):
        """
        Args:
            config: 策略配置
            fund_flow_df: 个股主力资金流向DataFrame
            margin_df: 融资余额DataFrame
        """
        self.cfg = config
        self.fund_flow_df = fund_flow_df
        self.margin_df = margin_df

    def prepare_indicators(self, df):
        """计算技术指标"""
        close = df['close'].values.astype(float)

        # 布林带
        upper, middle, lower = talib.BBANDS(
            close, timeperiod=20, nbdevup=2, nbdevdn=2
        )
        df['boll_upper'] = upper
        df['boll_middle'] = middle
        df['boll_lower'] = lower

        return df

    def check_fund_flow(self, date):
        """
        检查主力资金是否连续净流入
        """
        if self.fund_flow_df is None or self.fund_flow_df.empty:
            return False

        # 找到date之前的数据
        flow_data = self.fund_flow_df[
            self.fund_flow_df['date'] <= date
        ].tail(self.cfg.north_flow_days)

        if len(flow_data) < self.cfg.north_flow_days:
            return False

        # 检查是否连续净流入（主力净流入 > 0）
        return all(flow_data['net_main'] > 0)

    def check_margin_drop(self, date):
        """
        检查融资余额是否骤降
        """
        if self.margin_df.empty:
            return False

        # 找到date之前的数据
        margin_data = self.margin_df[
            self.margin_df['date'] <= date
        ].tail(self.cfg.margin_drop_days + 1)

        if len(margin_data) < 2:
            return False

        # 计算降幅
        start_balance = margin_data.iloc[0]['balance']
        end_balance = margin_data.iloc[-1]['balance']

        if start_balance == 0:
            return False

        drop_rate = (start_balance - end_balance) / start_balance
        return drop_rate >= self.cfg.margin_drop_threshold

    def scan_signals(self, stock_df, debug=False):
        """
        扫描聪明资金信号
        Args:
            stock_df: 股票DataFrame
            debug: 是否输出调试信息
        Returns:
            信号DataFrame
        """
        stock_df = self.prepare_indicators(stock_df)
        signals = []

        stats = {
            'total_days': 0,
            'pass_boll_break': 0,
            'pass_north_flow': 0,
            'pass_margin_drop': 0,
        }

        start_idx = 30
        for i in range(start_idx, len(stock_df)):
            stats['total_days'] += 1
            today = stock_df.iloc[i]
            date = today['date']

            # 条件1：跌破布林下轨
            if pd.isna(today['boll_lower']):
                continue

            break_pct = (
                (today['close'] - today['boll_lower']) / today['boll_lower']
            )
            if break_pct > self.cfg.boll_break_threshold:
                continue
            stats['pass_boll_break'] += 1

            # 条件2：主力资金连续净流入
            if not self.check_fund_flow(date):
                continue
            stats['pass_north_flow'] += 1

            # 条件3：融资余额骤降
            if not self.check_margin_drop(date):
                continue
            stats['pass_margin_drop'] += 1

            # 生成信号
            signals.append({
                'date': date.strftime('%Y-%m-%d'),
                'close': round(today['close'], 2),
                'boll_lower': round(today['boll_lower'], 2),
                'break_pct': f"{round(break_pct * 100, 2)}%",
                'signal_type': '聪明资金',
            })

        if debug:
            print(f"     [调试-聪明资金] 总天数:{stats['total_days']}"
                  f"  破下轨:{stats['pass_boll_break']}"
                  f"  主力流入:{stats['pass_north_flow']}"
                  f"  融资骤降:{stats['pass_margin_drop']}")

        return pd.DataFrame(signals)