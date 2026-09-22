"""回测服务 - Backtrader 集成"""
import backtrader as bt
import pandas as pd
from datetime import datetime
from typing import List, Dict, Optional
import json


class MAStrategy(bt.Strategy):
    """均线策略"""
    params = (
        ('fast_period', 5),
        ('slow_period', 20),
        ('printlog', False),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        self.buyprice = None
        self.buycomm = None
        
        # 均线指标
        self.sma_fast = bt.indicators.SimpleMovingAverage(
            self.datas[0], period=self.params.fast_period
        )
        self.sma_slow = bt.indicators.SimpleMovingAverage(
            self.datas[0], period=self.params.slow_period
        )
        
        # 交叉信号
        self.crossover = bt.indicators.CrossOver(self.sma_fast, self.sma_slow)

    def next(self):
        if self.order:
            return

        if not self.position:
            # 金叉买入
            if self.crossover > 0:
                self.order = self.buy()
        else:
            # 死叉卖出
            if self.crossover < 0:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status in [order.Completed]:
            if order.isbuy():
                self.buyprice = order.executed.price
                self.buycomm = order.executed.comm
            elif order.issell():
                self.sellprice = order.executed.price
                self.sellcomm = order.executed.comm

        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return


class MACDStrategy(bt.Strategy):
    """MACD 策略"""
    params = (
        ('fast_period', 12),
        ('slow_period', 26),
        ('signal_period', 9),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None
        
        # MACD 指标
        self.macd = bt.indicators.MACD(
            self.datas[0],
            period_me1=self.params.fast_period,
            period_me2=self.params.slow_period,
            period_signal=self.params.signal_period
        )
        
        # MACD 交叉
        self.crossover = bt.indicators.CrossOver(self.macd.macd, self.macd.signal)

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.crossover > 0:
                self.order = self.buy()
        else:
            if self.crossover < 0:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Completed]:
            if order.isbuy():
                pass
            elif order.issell():
                pass
        self.order = None


def kline_to_dataframe(klines: List[Dict]) -> pd.DataFrame:
    """将 kline 数据转换为 backtrader 需要的 DataFrame"""
    if not klines:
        return pd.DataFrame()
    
    df = pd.DataFrame(klines)
    # 添加当前年份到日期字符串
    current_year = datetime.now().year
    df['date'] = pd.to_datetime(
        df['date'].apply(lambda x: f"{current_year}-{x}"),
        format='%Y-%m-%d'
    )
    df.set_index('date', inplace=True)
    df = df[['open', 'high', 'low', 'close', 'volume']]
    df = df.astype(float)
    return df


def run_backtest(
    klines: List[Dict],
    strategy_name: str = 'ma',
    initial_cash: float = 100000.0,
    commission: float = 0.001,
    **kwargs
) -> Dict:
    """
    运行回测
    
    Args:
        klines: K线数据列表
        strategy_name: 策略名称 ('ma' 或 'macd')
        initial_cash: 初始资金
        commission: 手续费率
        **kwargs: 策略参数
    
    Returns:
        回测结果字典
    """
    if not klines or len(klines) < 30:
        return {
            'success': False,
            'error': 'K线数据不足，至少需要30天数据'
        }
    
    # 创建 Cerebro 引擎
    cerebro = bt.Cerebro()
    
    # 转换数据
    df = kline_to_dataframe(klines)
    if df.empty:
        return {
            'success': False,
            'error': '数据转换失败'
        }
    
    # 添加数据
    data = bt.feeds.PandasData(dataname=df)
    cerebro.adddata(data)
    
    # 选择策略
    if strategy_name == 'ma':
        cerebro.addstrategy(MAStrategy, **kwargs)
    elif strategy_name == 'macd':
        cerebro.addstrategy(MACDStrategy, **kwargs)
    else:
        return {
            'success': False,
            'error': f'未知策略: {strategy_name}'
        }
    
    # 设置初始资金和手续费
    cerebro.broker.setcash(initial_cash)
    cerebro.broker.setcommission(commission=commission)
    
    # 添加分析器
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    
    # 运行回测
    start_value = cerebro.broker.getvalue()
    results = cerebro.run()
    end_value = cerebro.broker.getvalue()
    
    # 提取结果
    strat = results[0]
    
    # 计算收益
    total_return = (end_value - start_value) / start_value * 100
    
    # 提取分析器结果
    sharpe = strat.analyzers.sharpe.get_analysis()
    drawdown = strat.analyzers.drawdown.get_analysis()
    trades = strat.analyzers.trades.get_analysis()
    returns = strat.analyzers.returns.get_analysis()
    
    # 计算胜率
    total_trades = trades.get('total', {}).get('total', 0)
    won_trades = trades.get('won', {}).get('total', 0)
    win_rate = (won_trades / total_trades * 100) if total_trades > 0 else 0
    
    result = {
        'success': True,
        'strategy': strategy_name,
        'initial_cash': initial_cash,
        'final_value': round(end_value, 2),
        'total_return': round(total_return, 2),
        'sharpe_ratio': round(sharpe.get('sharperatio', 0) or 0, 3),
        'max_drawdown': round(drawdown.get('max', {}).get('drawdown', 0), 2),
        'total_trades': total_trades,
        'win_rate': round(win_rate, 2),
        'annual_return': round(returns.get('rnorm100', 0), 2),
    }
    
    return result


def get_available_strategies() -> List[Dict]:
    """获取可用策略列表"""
    return [
        {
            'name': 'ma',
            'display_name': '均线交叉策略',
            'description': '5日线上穿20日线买入，下穿卖出',
            'params': {
                'fast_period': 5,
                'slow_period': 20,
            }
        },
        {
            'name': 'macd',
            'display_name': 'MACD策略',
            'description': 'MACD金叉买入，死叉卖出',
            'params': {
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
            }
        },
    ]
