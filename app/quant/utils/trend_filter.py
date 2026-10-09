"""
日线趋势预筛选模块
- 策略运行前自动过滤股票池
- 四项条件：均线多头、MA20向上、20日新高附近、量能不过度萎缩
- 支持多线程并发筛选

打印/报告逻辑集中在 app.quant.utils.reporting，本模块只保留筛选计算与编排。
"""

import time
import talib
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.quant.config import StrategyConfig
from app.quant.utils import reporting


class TrendFilter:
    """日线趋势预筛选器"""

    def __init__(self, config=None):
        if config is None:
            config = StrategyConfig()
        self.cfg = config

    def _calc_ma_indicators(self, df):
        """计算均线指标（第一阶段，用于短路判断）"""
        close = df['close'].values.astype(float)

        # 均线
        ma5 = talib.SMA(close, timeperiod=self.cfg.trend_ma_short)
        ma10 = talib.SMA(close, timeperiod=self.cfg.trend_ma_mid)
        ma20 = talib.SMA(close, timeperiod=self.cfg.trend_ma_long)

        return ma5, ma10, ma20

    def _calc_extra_indicators(self, df):
        """计算额外指标（第二阶段，仅在需要时调用）"""
        close = df['close'].values.astype(float)
        volume = df['volume'].values.astype(float)

        # 20日最高价
        high20 = talib.MAX(close, timeperiod=self.cfg.trend_high_days)

        # 成交量均线
        vol_ma = talib.SMA(volume, timeperiod=self.cfg.trend_vol_ma_days)

        return high20, vol_ma

    def _calc_indicators(self, df):
        """计算预筛选所需的全部技术指标（兼容旧接口）"""
        ma5, ma10, ma20 = self._calc_ma_indicators(df)
        high20, vol_ma = self._calc_extra_indicators(df)
        return ma5, ma10, ma20, high20, vol_ma

    def filter_stock(self, df, mode="strong"):
        """
        对单只股票进行趋势预筛选

        参数:
            df: DataFrame，包含 date, open, high, low, close, volume
            mode: 过滤模式
                "strong"   = 强趋势（均线多头+20日新高），适合趋势策略
                "loose"    = 宽松（只检查数据量充足），适合非趋势策略
                "oversold" = 反向（股价低于MA20），适合均值回归类策略

        返回:
            dict:
                - pass: bool, 是否通过筛选
                - ma_bullish: bool, 均线多头
                - ma20_up: bool, MA20向上
                - near_high: bool, 20日新高附近
                - vol_ok: bool, 量能正常
                - detail: dict, 详细数值
                - reasons: list, 未通过的原因列表
        """
        min_rows = max(
            self.cfg.trend_ma_long,
            self.cfg.trend_high_days,
            self.cfg.trend_vol_ma_days,
        ) + self.cfg.trend_ma20_lookback + 5

        if df is None or len(df) < min_rows:
            return {
                'pass': False,
                'ma_bullish': False,
                'ma20_up': False,
                'near_high': False,
                'vol_ok': False,
                'detail': {},
                'reasons': ['数据不足'],
            }

        # ---- loose 模式：只检查数据量，直接通过 ----
        if mode == "loose":
            return {
                'pass': True,
                'ma_bullish': True,
                'ma20_up': True,
                'near_high': True,
                'vol_ok': True,
                'detail': {},
                'reasons': [],
            }

        # 取最新一天的数据
        idx = len(df) - 1
        close_today = df.iloc[idx]['close']

        # ---- 第一阶段：只计算均线（短路优化） ----
        ma5, ma10, ma20 = self._calc_ma_indicators(df)

        ma5_today = ma5[idx]
        ma10_today = ma10[idx]
        ma20_today = ma20[idx]

        # MA20 对比 N 天前
        lookback = self.cfg.trend_ma20_lookback
        ma20_prev = ma20[idx - lookback]

        # ---- 条件 ① 均线多头 ----
        ma_bullish = (
            ma5_today > ma10_today
            and ma10_today > ma20_today
            and close_today > ma5_today
        )

        # ---- 条件 ② MA20 向上 ----
        ma20_up = ma20_today > ma20_prev

        # ---- oversold 模式：只需均线数据 ----
        if mode == "oversold":
            below_ma20 = close_today < ma20_today
            passed = below_ma20

            detail = {
                'close': round(close_today, 2),
                'ma5': round(ma5_today, 2),
                'ma10': round(ma10_today, 2),
                'ma20': round(ma20_today, 2),
            }

            reasons = []
            if not below_ma20:
                reasons.append('股价高于MA20（非超卖区域）')

            return {
                'pass': passed,
                'ma_bullish': ma_bullish,
                'ma20_up': ma20_up,
                'near_high': False,
                'vol_ok': False,
                'detail': detail,
                'reasons': reasons,
            }

        # ---- strong 模式：短路逻辑 ----
        # 条件①不满足 → 直接返回，不计算 high20/vol_ma
        if not ma_bullish:
            return {
                'pass': False,
                'ma_bullish': False,
                'ma20_up': ma20_up,
                'near_high': False,
                'vol_ok': False,
                'detail': {
                    'close': round(close_today, 2),
                    'ma5': round(ma5_today, 2),
                    'ma10': round(ma10_today, 2),
                    'ma20': round(ma20_today, 2),
                },
                'reasons': ['均线非多头排列'],
            }

        # 条件②不满足 → 直接返回
        if not ma20_up:
            return {
                'pass': False,
                'ma_bullish': True,
                'ma20_up': False,
                'near_high': False,
                'vol_ok': False,
                'detail': {
                    'close': round(close_today, 2),
                    'ma5': round(ma5_today, 2),
                    'ma10': round(ma10_today, 2),
                    'ma20': round(ma20_today, 2),
                    'ma20_prev': round(ma20_prev, 2),
                },
                'reasons': ['MA20下行'],
            }

        # ---- 第二阶段：条件①②通过，才计算额外指标 ----
        high20, vol_ma = self._calc_extra_indicators(df)

        vol_today = df.iloc[idx]['volume']
        high20_today = high20[idx]
        vol_ma_today = vol_ma[idx]

        # ---- 条件 ③ 20日新高附近 ----
        threshold_price = high20_today * self.cfg.trend_high_threshold
        near_high = close_today >= threshold_price

        if not near_high:
            return {
                'pass': False,
                'ma_bullish': True,
                'ma20_up': True,
                'near_high': False,
                'vol_ok': False,
                'detail': {
                    'close': round(close_today, 2),
                    'ma5': round(ma5_today, 2),
                    'ma10': round(ma10_today, 2),
                    'ma20': round(ma20_today, 2),
                    'ma20_prev': round(ma20_prev, 2),
                    'high20': round(high20_today, 2),
                },
                'reasons': ['远离20日高点'],
            }

        # ---- 条件 ④ 量能不过度萎缩 ----
        min_vol = vol_ma_today * self.cfg.trend_vol_min_ratio
        vol_ok = vol_today >= min_vol

        detail = {
            'close': round(close_today, 2),
            'ma5': round(ma5_today, 2),
            'ma10': round(ma10_today, 2),
            'ma20': round(ma20_today, 2),
            'ma20_prev': round(ma20_prev, 2),
            'high20': round(high20_today, 2),
            'volume': round(vol_today, 0),
            'vol_ma': round(vol_ma_today, 0),
            'vol_min': round(min_vol, 0),
        }

        if not vol_ok:
            return {
                'pass': False,
                'ma_bullish': True,
                'ma20_up': True,
                'near_high': True,
                'vol_ok': False,
                'detail': detail,
                'reasons': ['量能萎缩'],
            }

        # ---- 四项全部通过 ----
        return {
            'pass': True,
            'ma_bullish': True,
            'ma20_up': True,
            'near_high': True,
            'vol_ok': True,
            'detail': detail,
            'reasons': [],
        }

    def _filter_single_stock(self, stock, verbose=True, mode="strong"):
        """
        筛选单只股票（用于多线程调用）

        参数:
            stock: 股票信息 dict
            verbose: 是否打印详细日志
            mode: 过滤模式 "strong" / "loose" / "oversold"

        返回:
            tuple: (stock, result, df)
                - stock: 股票信息 dict
                - result: 筛选结果 dict
                - df: 日线数据（可用于后续策略，避免重复获取）
        """
        from app.quant.data.fetcher import load_real_data

        symbol = stock['symbol']

        df = load_real_data(
            symbol=symbol, start_date="20240101", verbose=verbose
        )
        if df.empty:
            return stock, None, None

        result = self.filter_stock(df, mode=mode)
        return stock, result, df

    def filter_pool(self, stock_list, print_detail=True, max_workers=1,
                    return_data=False, mode="strong"):
        """
        批量筛选股票池

        参数:
            stock_list: list, [{'symbol': '600183', 'name': '生益科技'}, ...]
            print_detail: bool, 是否打印详细报告
            max_workers: int, 并发线程数，1=串行，>1=多线程
            return_data: bool, 是否返回日线数据（用于后续策略复用）
            mode: 过滤模式 "strong" / "loose" / "oversold"

        返回:
            list: 通过筛选的股票列表
            或 tuple: (passed_stocks, data_cache) 当 return_data=True
        """
        passed_stocks = []
        failed_stocks = []
        data_cache = {}  # symbol -> df，缓存日线数据

        mode_desc = {
            "strong": "强趋势（均线多头+新高）",
            "loose": "宽松（仅检查数据量）",
            "oversold": "超卖（股价低于MA20）",
        }.get(mode, mode)

        if print_detail:
            reporting.section(f"日线趋势预筛选 [{mode_desc}]")

        total = len(stock_list)
        verbose = print_detail

        if max_workers <= 1:
            # 串行处理
            for i, stock in enumerate(stock_list, 1):
                if print_detail:
                    print(f"\n  [{i}/{total}] 处理中...")
                stock, result, df = self._filter_single_stock(
                    stock, verbose=verbose, mode=mode
                )
                if result is None:
                    if print_detail:
                        name = stock['name']
                        symbol = stock['symbol']
                        print(f"    {name}({symbol}) 数据获取失败")
                    failed_stocks.append(stock)
                    continue

                if print_detail:
                    reporting.print_trend_stock_report(
                        self.cfg, stock['name'], stock['symbol'], result
                    )

                if result['pass']:
                    passed_stocks.append(stock)
                    if return_data and df is not None:
                        data_cache[stock['symbol']] = df
                else:
                    failed_stocks.append(stock)
        else:
            # 多线程并发处理 + 进度条
            print(f"\n  使用 {max_workers} 线程并发筛选 {total} 只股票...")
            completed = 0
            start_time = time.time()

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        self._filter_single_stock, stock, False, mode
                    ): stock
                    for stock in stock_list
                }

                for future in as_completed(futures):
                    completed += 1
                    stock, result, df = future.result()

                    # 进度条（单行刷新）
                    reporting.progress_line(
                        "趋势筛选", completed, total, start_time
                    )

                    if result is None:
                        failed_stocks.append(stock)
                        continue

                    if result['pass']:
                        passed_stocks.append(stock)
                        if return_data and df is not None:
                            data_cache[stock['symbol']] = df
                    else:
                        failed_stocks.append(stock)

            # 进度条完成，换行
            print()

        # 打印汇总
        if print_detail:
            reporting.print_trend_summary(total, passed_stocks, failed_stocks)

        if return_data:
            return passed_stocks, data_cache
        return passed_stocks


if __name__ == "__main__":
    from app.quant.config import STOCK_LIST

    tf = TrendFilter()
    tf.filter_pool(STOCK_LIST)