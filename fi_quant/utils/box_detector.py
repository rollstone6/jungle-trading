"""
箱体检测模块
- 检测股票是否处于箱体震荡状态
- 输出箱体参数（上沿、下沿、宽度、中轴）
- 计算价格位置和质量评分
- 支持批量扫描股票池
- 提供操作建议
"""

import numpy as np
from config import StrategyConfig, STOCK_LIST


class BoxDetector:
    """箱体检测器"""

    def __init__(self, config=None):
        if config is None:
            config = StrategyConfig(signal_timeframe="1d")
        self.box_bars = config.box_bars          # 箱体周期（K线根数）
        self.max_box_width = config.max_box_width  # 35%
        # 触及上下沿的阈值（距离上下沿 2% 以内算触及）
        self.touch_threshold = 0.02

    def detect_box(self, df):
        """
        检测箱体并返回箱体信息
        
        参数:
            df: DataFrame，包含 date, open, high, low, close, volume 列
        
        返回:
            dict: 箱体信息字典
                - has_box: bool, 是否处于箱体
                - box_high: float, 箱体上沿
                - box_low: float, 箱体下沿
                - box_width: float, 箱体宽度%
                - box_mid: float, 箱体中轴
                - current_price: float, 当前价格
                - price_position: float, 价格位置%
                - price_hint: str, 价格位置提示
                - touch_high_count: int, 触及上沿次数
                - touch_low_count: int, 触及下沿次数
                - quality_score: int, 质量评分(1-5)
                - quality_text: str, 质量描述
                - box_start_date: str, 箱体起始日期
                - box_end_date: str, 箱体结束日期
                - volatility: float, 波动率
                - avg_volume: float, 平均成交量
                - trend: str, 箱体趋势
                - action_hint: str, 操作建议
        """
        if df is None or len(df) < self.box_bars + 1:
            return {'has_box': False, 'error': '数据不足'}

        # 取最近 box_bars 根K线的数据作为箱体切片
        box_data = df.iloc[-self.box_bars:]
        
        # 计算箱体参数
        box_high = box_data['high'].max()
        box_low = box_data['low'].min()
        box_width = (box_high - box_low) / box_low * 100
        box_mid = (box_high + box_low) / 2

        # 判断是否有效箱体
        if box_width > self.max_box_width:
            msg = '箱体宽度 {:.1f}% 超过阈值 {}%'.format(
                box_width, self.max_box_width
            )
            return {
                'has_box': False,
                'box_width': round(box_width, 2),
                'error': msg
            }

        # 当前价格
        current_price = df.iloc[-1]['close']
        
        # 价格位置（在箱体中的百分位）
        if box_high == box_low:
            price_position = 50.0
        else:
            price_position = (
                (current_price - box_low) / (box_high - box_low) * 100
            )
        
        # 价格位置提示和操作建议
        if price_position >= 80:
            price_hint = "⚡ 接近突破位"
            action_hint = "关注放量突破，突破上沿可追"
        elif price_position <= 20:
            price_hint = "🛡️ 接近支撑位"
            action_hint = "接近下沿，可考虑低吸布局"
        elif price_position >= 60:
            price_hint = "📈 箱体中上部"
            action_hint = "持仓观望，等待突破或回踩"
        elif price_position <= 40:
            price_hint = "📉 箱体中下部"
            action_hint = "观望为主，等待企稳信号"
        else:
            price_hint = "📊 箱体中部"
            action_hint = "持仓观望"

        # 计算触及上下沿次数
        touch_high_count = 0
        touch_low_count = 0
        
        for _, row in box_data.iterrows():
            # 触及上沿：最高价距离上沿 <= 2%
            if (box_high - row['high']) / box_high <= self.touch_threshold:
                touch_high_count += 1
            # 触及下沿：最低价距离下沿 <= 2%
            if (row['low'] - box_low) / box_low <= self.touch_threshold:
                touch_low_count += 1

        # 质量评分（触及总次数）
        total_touch = touch_high_count + touch_low_count
        if total_touch >= 8:
            quality_score = 5
            quality_text = "★★★★★ (极佳)"
        elif total_touch >= 6:
            quality_score = 4
            quality_text = "★★★★☆ (优秀)"
        elif total_touch >= 4:
            quality_score = 3
            quality_text = "★★★☆☆ (良好)"
        elif total_touch >= 2:
            quality_score = 2
            quality_text = "★★☆☆☆ (一般)"
        else:
            quality_score = 1
            quality_text = "★☆☆☆☆ (差)"

        # 日期范围
        box_start_date = str(box_data.iloc[0]['date'])[:10]
        box_end_date = str(box_data.iloc[-1]['date'])[:10]

        # 波动率（收盘价标准差 / 均值）
        close_prices = box_data['close'].values
        volatility = round(float(np.std(close_prices) / np.mean(close_prices) * 100), 2)

        # 平均成交量
        avg_volume = round(float(box_data['volume'].mean()), 0)

        # 趋势判断（比较箱体前后半段的均价）
        half = len(box_data) // 2
        first_half_avg = box_data['close'].iloc[:half].mean()
        second_half_avg = box_data['close'].iloc[half:].mean()
        trend_diff = (second_half_avg - first_half_avg) / first_half_avg * 100
        
        if trend_diff > 3:
            trend = "📈 上升箱体"
        elif trend_diff < -3:
            trend = "📉 下降箱体"
        else:
            trend = "➡️ 水平箱体"

        return {
            'has_box': True,
            'box_high': round(box_high, 2),
            'box_low': round(box_low, 2),
            'box_width': round(box_width, 2),
            'box_mid': round(box_mid, 2),
            'current_price': round(current_price, 2),
            'price_position': round(price_position, 1),
            'price_hint': price_hint,
            'action_hint': action_hint,
            'touch_high_count': touch_high_count,
            'touch_low_count': touch_low_count,
            'quality_score': quality_score,
            'quality_text': quality_text,
            'box_start_date': box_start_date,
            'box_end_date': box_end_date,
            'volatility': volatility,
            'avg_volume': avg_volume,
            'trend': trend,
        }

    def print_report(self, stock_name, symbol, box_info):
        """打印单只股票的箱体检测报告"""
        print("\n" + "=" * 50)
        print("  股票：{} ({})".format(stock_name, symbol))
        print("  数据级别：日K线")
        print("  箱体周期：最近 {} 根K线".format(self.box_bars))
        print("-" * 50)

        if not box_info.get('has_box'):
            error_msg = box_info.get('error', '未知原因')
            print("  【箱体状态】❌ 未形成箱体")
            print("  【原因】{}".format(error_msg))
            print("=" * 50)
            return

        print("  【箱体状态】✅ 处于箱体震荡中")
        print("  【箱体趋势】{}".format(box_info['trend']))
        print()
        print("  ┌─────────────────────────────────────────┐")
        print("  │  箱体上沿：{:>8.2f} 元                    │".format(box_info['box_high']))
        print("  │  箱体下沿：{:>8.2f} 元                    │".format(box_info['box_low']))
        print("  │  箱体中轴：{:>8.2f} 元                    │".format(box_info['box_mid']))
        print("  │  箱体宽度：{:>7.2f}%                      │".format(box_info['box_width']))
        print("  └─────────────────────────────────────────┘")
        print()
        print("  【当前价格】{:.2f} 元".format(box_info['current_price']))
        print("  【价格位置】{:.1f}% ({})".format(
            box_info['price_position'], box_info['price_hint']
        ))
        print("  【操作建议】{}".format(box_info['action_hint']))
        print()
        print("  【箱体时间】{} ~ {}".format(
            box_info['box_start_date'], box_info['box_end_date']
        ))
        print("  【波动率】{:.2f}%".format(box_info['volatility']))
        print()
        print("  【箱体质量】")
        print("    - 触及上沿次数：{}次".format(box_info['touch_high_count']))
        print("    - 触及下沿次数：{}次".format(box_info['touch_low_count']))
        print("    - 震荡评分：{}".format(box_info['quality_text']))
        print("=" * 50)

    def scan_stocks(self, stock_list, print_detail=True):
        """
        批量扫描股票池
        
        参数:
            stock_list: list, 股票列表
            print_detail: bool, 是否打印详细报告
        
        返回:
            list: 处于箱体的股票信息列表
        """
        from data.fetcher import load_real_data
        
        box_stocks = []
        
        print("\n" + "=" * 70)
        print("  箱体检测 - 批量扫描")
        print("  股票池：{} 只 | 箱体周期：{} 根日K线".format(
            len(stock_list), self.box_bars
        ))
        print("=" * 70)

        for stock in stock_list:
            symbol = stock['symbol']
            name = stock['name']
            
            if print_detail:
                print("\n  >> 检测 {} ({})".format(name, symbol))

            # 加载数据
            df = load_real_data(symbol=symbol, start_date="20240101")
            if df.empty:
                if print_detail:
                    print("     数据获取失败，跳过")
                continue

            # 检测箱体
            box_info = self.detect_box(df)
            
            if print_detail:
                self.print_report(name, symbol, box_info)

            # 记录有箱体的股票
            if box_info.get('has_box'):
                box_info['symbol'] = symbol
                box_info['name'] = name
                box_stocks.append(box_info)

        # 打印汇总
        print("\n" + "=" * 70)
        print("  扫描汇总")
        print("=" * 70)
        print("  扫描股票数：{}".format(len(stock_list)))
        print("  处于箱体：{}".format(len(box_stocks)))
        
        if box_stocks:
            print("\n  处于箱体的股票（按评分排序）：")
            print("-" * 70)
            print("  {:<12} {:>8} {:>8} {:>6} {:>6} {:>8}".format(
                "股票", "上限", "下限", "宽度%", "位置%", "评分"
            ))
            print("-" * 70)
            sorted_stocks = sorted(
                box_stocks,
                key=lambda x: x['quality_score'],
                reverse=True,
            )
            for info in sorted_stocks:
                print("  {:<10} {:>8.2f} {:>8.2f} {:>6.1f} {:>6.1f} {:>8}".format(
                    "{}({})".format(info['name'], info['symbol']),
                    info['box_high'],
                    info['box_low'],
                    info['box_width'],
                    info['price_position'],
                    info['quality_text'].split()[0]
                ))
            print("-" * 70)
            
            # 显示操作建议汇总
            print("\n  操作建议：")
            for info in sorted_stocks:
                print("    {}({}): {} | {}".format(
                    info['name'], info['symbol'],
                    info['price_hint'], info['action_hint']
                ))
        print("=" * 70)

        return box_stocks


def run_box_detection():
    """运行箱体检测（使用统一股票池，日线级别）"""
    config = StrategyConfig(signal_timeframe="1d")
    detector = BoxDetector(config)
    return detector.scan_stocks(STOCK_LIST)


if __name__ == "__main__":
    run_box_detection()