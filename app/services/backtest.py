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


class BollingerBandsStrategy(bt.Strategy):
    """布林带均值回归策略

    逻辑：价格触及/跌破下轨视为超卖，买入博反弹；
    价格回到中轨即卖出止盈（不赌突破上轨）。
    """
    params = (
        ('period', 20),
        ('devfactor', 2.0),
        ('printlog', False),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        self.bbands = bt.indicators.BollingerBands(
            self.datas[0],
            period=self.params.period,
            devfactor=self.params.devfactor,
        )

    def next(self):
        if self.order:
            return

        if not self.position:
            # 收盘跌破下轨 -> 超卖买入
            if self.dataclose[0] <= self.bbands.lines.bot[0]:
                self.order = self.buy()
        else:
            # 收盘回到中轨上方 -> 止盈卖出
            if self.dataclose[0] >= self.bbands.lines.mid[0]:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class BollingerMACDStrategy(bt.Strategy):
    """MACD趋势过滤 + 布林高卖低买（综合策略）

    只在 MACD 多头状态（DIF > DEA）中做布林均值回归：
    - 买入：收盘触/破下轨（超卖低吸）
    - 卖出：收盘触上轨（高抛）或 MACD 死叉（趋势走坏止损）
    """
    params = (
        ('period', 20),
        ('devfactor', 2.0),
        ('fast_period', 12),
        ('slow_period', 26),
        ('signal_period', 9),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        self.bbands = bt.indicators.BollingerBands(
            self.datas[0], period=self.params.period, devfactor=self.params.devfactor
        )
        self.macd = bt.indicators.MACD(
            self.datas[0],
            period_me1=self.params.fast_period,
            period_me2=self.params.slow_period,
            period_signal=self.params.signal_period,
        )

    def next(self):
        if self.order:
            return

        trend_up = self.macd.macd[0] > self.macd.signal[0]
        if not self.position:
            if trend_up and self.dataclose[0] <= self.bbands.lines.bot[0]:
                self.order = self.buy()
        else:
            if (self.dataclose[0] >= self.bbands.lines.top[0]
                    or self.macd.macd[0] < self.macd.signal[0]):
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class MAMACDStrategy(bt.Strategy):
    """均线多头 + MACD金叉共振（综合策略）

    - 买入：收盘价站上 20 日线 且 MACD 金叉（趋势与动能共振）
    - 卖出：跌破 20 日线 或 MACD 死叉
    """
    params = (
        ('ma_period', 20),
        ('fast_period', 12),
        ('slow_period', 26),
        ('signal_period', 9),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        self.sma = bt.indicators.SimpleMovingAverage(self.datas[0], period=self.params.ma_period)
        macd = bt.indicators.MACD(
            self.datas[0],
            period_me1=self.params.fast_period,
            period_me2=self.params.slow_period,
            period_signal=self.params.signal_period,
        )
        self.crossover = bt.indicators.CrossOver(macd.macd, macd.signal)

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.dataclose[0] > self.sma[0] and self.crossover[0] > 0:
                self.order = self.buy()
        else:
            if self.dataclose[0] < self.sma[0] or self.crossover[0] < 0:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class RSIBollingerStrategy(bt.Strategy):
    """RSI超卖 + 布林下轨双重确认（综合策略）

    - 买入：RSI < 30 且收盘触/破布林下轨（双重超卖）
    - 卖出：回到中轨 或 RSI > 70
    """
    params = (
        ('period', 20),
        ('devfactor', 2.0),
        ('rsi_period', 14),
        ('rsi_oversold', 30),
        ('rsi_overbought', 70),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        self.bbands = bt.indicators.BollingerBands(
            self.datas[0], period=self.params.period, devfactor=self.params.devfactor
        )
        self.rsi = bt.indicators.RSI(self.datas[0], period=self.params.rsi_period)

    def next(self):
        if self.order:
            return

        if not self.position:
            if (self.rsi[0] < self.params.rsi_oversold
                    and self.dataclose[0] <= self.bbands.lines.bot[0]):
                self.order = self.buy()
        else:
            if (self.dataclose[0] >= self.bbands.lines.mid[0]
                    or self.rsi[0] > self.params.rsi_overbought):
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class MACDVolumeStrategy(bt.Strategy):
    """MACD金叉 + 放量确认（综合策略）

    - 买入：MACD 金叉 且成交量 > 20日均量 x 1.5（过滤无量弱金叉）
    - 卖出：MACD 死叉
    """
    params = (
        ('fast_period', 12),
        ('slow_period', 26),
        ('signal_period', 9),
        ('vol_period', 20),
        ('vol_mult', 1.5),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        macd = bt.indicators.MACD(
            self.datas[0],
            period_me1=self.params.fast_period,
            period_me2=self.params.slow_period,
            period_signal=self.params.signal_period,
        )
        self.crossover = bt.indicators.CrossOver(macd.macd, macd.signal)
        self.vol_ma = bt.indicators.SimpleMovingAverage(self.datas[0].volume, period=self.params.vol_period)

    def next(self):
        if self.order:
            return

        if not self.position:
            if (self.crossover[0] > 0
                    and self.datas[0].volume[0] > self.vol_ma[0] * self.params.vol_mult):
                self.order = self.buy()
        else:
            if self.crossover[0] < 0:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class KDJStrategy(bt.Strategy):
    """KDJ 低位金叉/高位死叉（基础版）

    - 买入：K 线上穿 D 线（金叉）且 K 值处于低位（< 30，超卖区）
    - 卖出：K 线下穿 D 线（死叉）且 K 值处于高位（> 70，超买区）
    """
    params = (
        ('period', 9),
        ('k_period', 3),
        ('d_period', 3),
        ('low_thresh', 30),
        ('high_thresh', 70),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        stoch = bt.indicators.StochasticSlow(
            self.datas[0],
            period=self.params.period,
            period_dfast=self.params.k_period,
            period_dslow=self.params.d_period,
        )
        self.percK = stoch.percK
        self.percD = stoch.percD
        self.kcross = bt.indicators.CrossOver(self.percK, self.percD)

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.kcross[0] > 0 and self.percK[0] < self.params.low_thresh:
                self.order = self.buy()
        else:
            if self.kcross[0] < 0 and self.percK[0] > self.params.high_thresh:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


class KDJMACDStrategy(bt.Strategy):
    """KDJ低位金叉 + MACD趋势确认（综合策略）

    - 买入：KDJ 低位金叉（K < 30 上穿 D）且 MACD 处于多头（DIF > DEA），
            超卖反弹叠加趋势向上，过滤下跌途中的接飞刀
    - 卖出：KDJ 高位死叉（K > 70 下穿 D）或 MACD 死叉（趋势走坏）
    """
    params = (
        ('period', 9),
        ('k_period', 3),
        ('d_period', 3),
        ('low_thresh', 30),
        ('high_thresh', 70),
        ('fast_period', 12),
        ('slow_period', 26),
        ('signal_period', 9),
    )

    def __init__(self):
        self.dataclose = self.datas[0].close
        self.order = None

        stoch = bt.indicators.StochasticSlow(
            self.datas[0],
            period=self.params.period,
            period_dfast=self.params.k_period,
            period_dslow=self.params.d_period,
        )
        self.percK = stoch.percK
        self.percD = stoch.percD
        self.kcross = bt.indicators.CrossOver(self.percK, self.percD)

        macd = bt.indicators.MACD(
            self.datas[0],
            period_me1=self.params.fast_period,
            period_me2=self.params.slow_period,
            period_signal=self.params.signal_period,
        )
        self.macd_dif = macd.macd
        self.macd_dea = macd.signal

    def next(self):
        if self.order:
            return

        if not self.position:
            if (self.kcross[0] > 0 and self.percK[0] < self.params.low_thresh
                    and self.macd_dif[0] > self.macd_dea[0]):
                self.order = self.buy()
        else:
            if (self.kcross[0] < 0 and self.percK[0] > self.params.high_thresh) \
                    or self.macd_dif[0] < self.macd_dea[0]:
                self.order = self.sell()

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return
        self.order = None


def kline_to_dataframe(klines: List[Dict]) -> pd.DataFrame:
    """将 kline 数据转换为 backtrader 需要的 DataFrame

    兼容两种日期格式：
    - MM-DD（持仓缓存格式，补当前年份）
    - YYYY-MM-DD（实时拉取的长周期数据，跨年安全）
    """
    if not klines:
        return pd.DataFrame()

    df = pd.DataFrame(klines)
    current_year = datetime.now().year

    def _parse_date(x):
        x = str(x)
        if len(x) >= 10:
            return pd.to_datetime(x)
        return pd.to_datetime(f"{current_year}-{x}", format="%Y-%m-%d")

    df["date"] = df["date"].apply(_parse_date)
    df = df.sort_values("date")
    df.set_index("date", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]
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
    elif strategy_name == 'boll':
        cerebro.addstrategy(BollingerBandsStrategy, **kwargs)
    elif strategy_name == 'boll_macd':
        cerebro.addstrategy(BollingerMACDStrategy, **kwargs)
    elif strategy_name == 'ma_macd':
        cerebro.addstrategy(MAMACDStrategy, **kwargs)
    elif strategy_name == 'rsi_boll':
        cerebro.addstrategy(RSIBollingerStrategy, **kwargs)
    elif strategy_name == 'macd_vol':
        cerebro.addstrategy(MACDVolumeStrategy, **kwargs)
    elif strategy_name == 'kdj':
        cerebro.addstrategy(KDJStrategy, **kwargs)
    elif strategy_name == 'kdj_macd':
        cerebro.addstrategy(KDJMACDStrategy, **kwargs)
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
    
    def _fmt_date(ts):
        # 分钟数据带时间，日线只显示日期
        if ts.hour or ts.minute:
            return ts.strftime("%Y-%m-%d %H:%M")
        return ts.strftime("%Y-%m-%d")

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
        # 回测数据区间（分钟数据含时间）
        'start_date': _fmt_date(df.index[0]),
        'end_date': _fmt_date(df.index[-1]),
        'total_days': len(df),
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
        {
            'name': 'boll',
            'display_name': '布林带均值回归',
            'description': '收盘跌破下轨（超卖）买入，回到中轨止盈卖出',
            'params': {
                'period': 20,
                'devfactor': 2.0,
            }
        },
        {
            'name': 'boll_macd',
            'display_name': '布林+MACD（趋势中的高卖低买）',
            'description': 'MACD金叉状态中触下轨低吸，触上轨高抛或死叉止损',
            'params': {
                'period': 20,
                'devfactor': 2.0,
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
            }
        },
        {
            'name': 'ma_macd',
            'display_name': '均线+MACD共振',
            'description': '站上20日线且MACD金叉买入，破线或死叉卖出',
            'params': {
                'ma_period': 20,
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
            }
        },
        {
            'name': 'rsi_boll',
            'display_name': 'RSI+布林双重超卖',
            'description': 'RSI<30且触下轨买入，回中轨或RSI>70卖出',
            'params': {
                'period': 20,
                'devfactor': 2.0,
                'rsi_period': 14,
                'rsi_oversold': 30,
                'rsi_overbought': 70,
            }
        },
        {
            'name': 'macd_vol',
            'display_name': 'MACD+放量确认',
            'description': 'MACD金叉且量超20日均量1.5倍买入，死叉卖出',
            'params': {
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
                'vol_period': 20,
                'vol_mult': 1.5,
            }
        },
        {
            'name': 'kdj',
            'display_name': 'KDJ金叉死叉',
            'description': 'K<30低位金叉买入，K>70高位死叉卖出',
            'params': {
                'period': 9,
                'k_period': 3,
                'd_period': 3,
                'low_thresh': 30,
                'high_thresh': 70,
            }
        },
        {
            'name': 'kdj_macd',
            'display_name': 'KDJ+MACD共振',
            'description': 'KDJ低位金叉且MACD多头买入，高位死叉或MACD死叉卖出',
            'params': {
                'period': 9,
                'k_period': 3,
                'd_period': 3,
                'low_thresh': 30,
                'high_thresh': 70,
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
            }
        },
    ]
