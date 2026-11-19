"""Zipline 回测服务 - 集成 zipline-reloaded"""
import pandas as pd
import numpy as np
from typing import List, Dict
import warnings
warnings.filterwarnings('ignore')

# Zipline imports
try:
    from zipline.api import order_target_percent, record, symbol
    ZIPLINE_AVAILABLE = True
except ImportError:
    ZIPLINE_AVAILABLE = False
    print("Warning: zipline not available")


def initialize(context):
    """Zipline 策略初始化"""
    context.asset = symbol('AAPL')  # 默认用AAPL，实际使用时会替换
    context.ma_short = 5
    context.ma_long = 20


def handle_data(context, data):
    """Zipline 策略主逻辑 - 均线交叉"""
    # 获取历史数据
    history = data.history(context.asset, 'price', 30, '1d')
    
    if len(history) < context.ma_long:
        return
    
    # 计算均线
    ma_short = history.rolling(window=context.ma_short).mean().iloc[-1]
    ma_long = history.rolling(window=context.ma_long).mean().iloc[-1]
    
    current_price = data.current(context.asset, 'price')
    
    # 记录数据
    record(
        ma_short=ma_short,
        ma_long=ma_long,
        price=current_price
    )
    
    # 交易逻辑
    if ma_short > ma_long and not context.portfolio.positions:
        order_target_percent(context.asset, 1.0)
    elif ma_short < ma_long and context.portfolio.positions:
        order_target_percent(context.asset, 0.0)


def run_zipline_backtest(
    klines: List[Dict],
    initial_cash: float = 100000.0,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Dict:
    """
    运行 Zipline 回测
    
    Args:
        klines: K线数据
        initial_cash: 初始资金
        start_date: 开始日期 (YYYY-MM-DD)
        end_date: 结束日期 (YYYY-MM-DD)
    
    Returns:
        回测结果字典
    """
    if not ZIPLINE_AVAILABLE:
        return {
            'success': False,
            'error': 'Zipline 未安装或不可用'
        }
    
    if not klines or len(klines) < 30:
        return {
            'success': False,
            'error': 'K线数据不足，至少需要30天数据'
        }
    
    # P0 诚实性降级（审计 t_edf48869 P0-1）：本函数从未接入真实市场数据
    # bundle（run_algorithm 从未被调用），此前返回与输入 K 线完全无关的硬编码
    # 示例指标（cash*1.05 / sharpe=1.2 / win_rate=60）。在接入真实数据源之前，
    # 一律显式报告不可用，不得返回任何数值指标字段。
    return {
        'success': False,
        'error': 'zipline 引擎未接数据源，暂不可用',
    }


def analyze_performance(returns: pd.Series) -> Dict:
    """
    分析回测表现
    
    Args:
        returns: 收益率序列
    
    Returns:
        性能指标字典
    """
    if returns.empty:
        return {}
    
    # 累计收益
    cum_returns = (1 + returns).cumprod()
    total_return = cum_returns.iloc[-1] - 1
    
    # 年化收益
    days = len(returns)
    annual_return = (1 + total_return) ** (252 / days) - 1 if days > 0 else 0
    
    # 年化波动率
    annual_volatility = returns.std() * np.sqrt(252)
    
    # 夏普比率 (假设无风险利率 0.03)
    risk_free_rate = 0.03
    sharpe_ratio = (annual_return - risk_free_rate) / annual_volatility if annual_volatility > 0 else 0
    
    # 最大回撤
    cummax = cum_returns.cummax()
    drawdown = (cum_returns - cummax) / cummax
    max_drawdown = drawdown.min()
    
    # 卡尔玛比率
    calmar_ratio = annual_return / abs(max_drawdown) if max_drawdown != 0 else 0
    
    return {
        'total_return': round(total_return * 100, 2),
        'annual_return': round(annual_return * 100, 2),
        'annual_volatility': round(annual_volatility * 100, 2),
        'sharpe_ratio': round(sharpe_ratio, 3),
        'max_drawdown': round(max_drawdown * 100, 2),
        'calmar_ratio': round(calmar_ratio, 3),
    }
