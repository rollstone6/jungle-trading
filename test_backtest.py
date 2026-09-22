#!/usr/bin/env python3
"""测试回测功能"""
import asyncio
import sys
sys.path.insert(0, '/root/workspace/jungle-trading')

from app.services.market import fetch_kline_baidu
from app.services.backtest import run_backtest, get_available_strategies


async def test_backtest():
    """测试回测功能"""
    # 获取测试数据
    code = "603629"  # 利通电子
    print(f"获取 {code} 的K线数据...")
    klines = await fetch_kline_baidu(code, 120)
    
    if not klines:
        print("❌ 获取K线数据失败")
        return
    
    print(f"✓ 获取到 {len(klines)} 条K线数据")
    
    # 显示可用策略
    strategies = get_available_strategies()
    print(f"\n可用策略: {len(strategies)}")
    for s in strategies:
        print(f"  - {s['display_name']}: {s['description']}")
    
    # 运行回测
    print("\n" + "="*50)
    print("运行均线策略回测...")
    print("="*50)
    
    result = run_backtest(
        klines=klines,
        strategy_name='ma',
        initial_cash=100000,
        commission=0.001
    )
    
    if result['success']:
        print(f"✓ 回测成功")
        print(f"策略: {result['strategy']}")
        print(f"初始资金: ¥{result['initial_cash']:,.2f}")
        print(f"最终价值: ¥{result['final_value']:,.2f}")
        print(f"总收益率: {result['total_return']}%")
        print(f"年化收益: {result['annual_return']}%")
        print(f"夏普比率: {result['sharpe_ratio']}")
        print(f"最大回撤: {result['max_drawdown']}%")
        print(f"交易次数: {result['total_trades']}")
        print(f"胜率: {result['win_rate']}%")
    else:
        print(f"❌ 回测失败: {result.get('error', '未知错误')}")
    
    # 测试 MACD 策略
    print("\n" + "="*50)
    print("运行 MACD 策略回测...")
    print("="*50)
    
    result = run_backtest(
        klines=klines,
        strategy_name='macd',
        initial_cash=100000,
        commission=0.001
    )
    
    if result['success']:
        print(f"✓ 回测成功")
        print(f"策略: {result['strategy']}")
        print(f"总收益率: {result['total_return']}%")
        print(f"夏普比率: {result['sharpe_ratio']}")
        print(f"最大回撤: {result['max_drawdown']}%")
        print(f"交易次数: {result['total_trades']}")
        print(f"胜率: {result['win_rate']}%")
    else:
        print(f"❌ 回测失败: {result.get('error', '未知错误')}")


if __name__ == "__main__":
    asyncio.run(test_backtest())
