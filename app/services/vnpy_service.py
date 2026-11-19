"""vnpy 集成服务 - 实盘交易和回测"""
from typing import List, Dict

# vnpy imports
try:
    from vnpy.trader.object import BarData
    VNPY_AVAILABLE = True
except ImportError:
    VNPY_AVAILABLE = False
    print("Warning: vnpy modules not available")


class CTAStrategy:
    """CTA 策略基类 - 用于 vnpy 回测"""
    
    def __init__(self, engine, symbol: str):
        self.engine = engine
        self.symbol = symbol
        self.pos = 0  # 当前持仓
        
    def on_bar(self, bar: BarData):
        """K线数据回调 - 子类需要实现"""
        raise NotImplementedError


class MAStrategy(CTAStrategy):
    """均线策略"""
    
    def __init__(self, engine, symbol: str, fast_period: int = 5, slow_period: int = 20):
        super().__init__(engine, symbol)
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.bars: List[BarData] = []
    
    def on_bar(self, bar: BarData):
        self.bars.append(bar)
        
        if len(self.bars) < self.slow_period:
            return
        
        # 计算均线
        closes = [b.close_price for b in self.bars[-self.slow_period:]]
        ma_fast = sum(closes[-self.fast_period:]) / self.fast_period
        ma_slow = sum(closes) / self.slow_period
        
        # 交易逻辑
        if ma_fast > ma_slow and self.pos == 0:
            self.engine.buy(bar.close_price, 1)
            self.pos = 1
        elif ma_fast < ma_slow and self.pos == 1:
            self.engine.sell(bar.close_price, 1)
            self.pos = 0


class MACDStrategy(CTAStrategy):
    """MACD 策略"""
    
    def __init__(self, engine, symbol: str, fast_period: int = 12, 
                 slow_period: int = 26, signal_period: int = 9):
        super().__init__(engine, symbol)
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.signal_period = signal_period
        self.bars: List[BarData] = []
        # DEA 信号线所需的 MACD 线历史（标准口径：signal = EMA(macd, signal_period)）
        self.macd_history: List[float] = []
    
    def _calc_ema(self, prices: List[float], period: int) -> float:
        """计算 EMA"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        
        multiplier = 2 / (period + 1)
        ema = sum(prices[:period]) / period
        
        for price in prices[period:]:
            ema = (price - ema) * multiplier + ema
        
        return ema
    
    def on_bar(self, bar: BarData):
        self.bars.append(bar)
        
        if len(self.bars) < self.slow_period + self.signal_period:
            return
        
        closes = [b.close_price for b in self.bars]
        
        # 计算 MACD
        ema_fast = self._calc_ema(closes, self.fast_period)
        ema_slow = self._calc_ema(closes, self.slow_period)
        macd_line = ema_fast - ema_slow
        
        # 计算信号线 DEA：MACD 线的 signal_period 日 EMA（标准 MACD 口径）
        self.macd_history.append(macd_line)
        signal_line = self._calc_ema(self.macd_history, self.signal_period)
        
        # 交易逻辑
        if macd_line > signal_line and self.pos == 0:
            self.engine.buy(bar.close_price, 1)
            self.pos = 1
        elif macd_line < signal_line and self.pos == 1:
            self.engine.sell(bar.close_price, 1)
            self.pos = 0


def run_vnpy_backtest(
    klines: List[Dict],
    strategy_name: str = 'ma',
    initial_cash: float = 100000.0,
    commission: float = 0.001,
    **kwargs
) -> Dict:
    """
    运行 vnpy 回测
    
    Args:
        klines: K线数据
        strategy_name: 策略名称
        initial_cash: 初始资金
        commission: 手续费率
    
    Returns:
        回测结果字典
    """
    if not VNPY_AVAILABLE:
        return {
            'success': False,
            'error': 'vnpy 未安装或不可用'
        }
    
    if not klines or len(klines) < 30:
        return {
            'success': False,
            'error': 'K线数据不足，至少需要30天数据'
        }
    
    # P0 诚实性降级（审计 t_edf48869 P0-2）：原实现虽创建 BacktestingEngine，
    # 但从不调用 load_data()/calculate_result()——引擎空转，set_parameters 的
    # start 传 int 年份（非法参数类型），返回的指标是写死的示例值
    # （cash*1.02 / sharpe=0.8 / win_rate=62.5），与输入 K 线完全无关。
    # 在接入真实数据源与策略加载之前一律显式报告不可用，
    # 不得返回任何数值指标字段。
    return {
        'success': False,
        'error': 'vnpy 引擎未接数据源，暂不可用',
    }


def get_vnpy_strategies() -> List[Dict]:
    """获取 vnpy 可用策略（按可用性过滤：vnpy 未安装时不展示卡片，
    避免前端出现点了必报错的死路策略，审计 t_edf48869 P0-2）。"""
    if not VNPY_AVAILABLE:
        return []
    return [
        {
            'name': 'vnpy_ma',
            'display_name': 'vnpy 均线策略',
            'category': '趋势跟随',
            'description': '基于 vnpy 框架的均线交叉策略',
            'params': {
                'fast_period': 5,
                'slow_period': 20,
            }
        },
        {
            'name': 'vnpy_macd',
            'display_name': 'vnpy MACD策略',
            'category': '趋势跟随',
            'description': '基于 vnpy 框架的 MACD 策略',
            'params': {
                'fast_period': 12,
                'slow_period': 26,
                'signal_period': 9,
            }
        },
    ]
