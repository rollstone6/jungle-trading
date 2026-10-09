"""
筹码分布分析模块
- 计算筹码分布：三角分布 + 换手率衰减
- 计算集中度指标：90%集中度、70%集中度、主峰占比、峰度、偏度
- 支持单股分析和批量扫描

打印/报告逻辑集中在 app.quant.utils.reporting，本模块只保留计算与扫描编排。
"""

import sys
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.quant.config import StrategyConfig
from app.quant.utils import reporting


class ChipDistributionAnalyzer:
    """筹码分布分析器"""

    def __init__(self, config=None):
        if config is None:
            config = StrategyConfig()
        self.cfg = config

    def calc_chip_distribution(self, df, float_shares=None):
        """
        计算筹码分布

        参数:
            df: DataFrame，包含 date, open, high, low, close, volume, turnover(可选)
            float_shares: float, 流通股本（股），用于计算换手率

        返回:
            tuple: (prices, chip_dist)
                - prices: numpy数组，价格区间
                - chip_dist: numpy数组，各价格筹码占比（归一化）
        """
        lookback = min(self.cfg.chip_lookback_days, len(df))
        df = df.tail(lookback).reset_index(drop=True)

        if len(df) < 10:
            return None, None

        # 确定价格区间
        price_min = df['low'].min()
        price_max = df['high'].max()

        if price_max <= price_min:
            return None, None

        # 创建价格网格（步长为价格区间的0.5%）
        step = max(price_min * self.cfg.chip_price_step, 0.001)
        prices = np.arange(price_min, price_max + step, step)
        n_prices = len(prices)

        if n_prices < 2:
            return None, None

        lows = df['low'].values.astype(float)
        highs = df['high'].values.astype(float)
        volumes = df['volume'].values.astype(float)
        n_days = len(df)

        # ===== 向量化计算换手率数组 =====
        if 'turnover' in df.columns:
            to = df['turnover'].values.astype(float)
            valid = ~np.isnan(to)
        else:
            to = np.full(n_days, np.nan)
            valid = np.zeros(n_days, dtype=bool)

        if float_shares and float_shares > 0:
            fill = volumes / float_shares
            to = np.where(valid, to, fill)
        else:
            to = np.where(valid, to, self.cfg.chip_decay_factor)
        to = np.clip(to, 0.001, 0.5)

        # ===== 向量化构建每日三角分布矩阵 (n_days x n_prices) =====
        dist_matrix = self._triangle_distribution_matrix(lows, highs, prices)

        # ===== 顺序累积筹码（纯 numpy，无 Python 行循环） =====
        chip_dist = np.zeros(n_prices)
        for i in range(n_days):
            if volumes[i] <= 0:
                continue
            t = to[i]
            chip_dist *= (1.0 - t)
            chip_dist += dist_matrix[i] * t

        # 归一化
        total = chip_dist.sum()
        if total > 0:
            chip_dist /= total

        return prices, chip_dist

    def _triangle_distribution_matrix(self, lows, highs, prices):
        """
        向量化批量计算所有交易日的三角分布

        参数:
            lows: (n_days,) 每日最低价
            highs: (n_days,) 每日最高价
            prices: (n_prices,) 价格网格

        返回:
            (n_days, n_prices) 筹码分布矩阵，每行已归一化
        """
        n_days = len(lows)
        n_prices = len(prices)
        dist = np.zeros((n_days, n_prices))

        peaks = (lows + highs) / 2.0
        widths = highs - lows

        P = prices[None, :]        # (1, n_prices)
        L = lows[:, None]          # (n_days, 1)
        H = highs[:, None]
        PK = peaks[:, None]
        W = widths[:, None]

        flat_mask = widths <= 0    # 涨停/跌停
        normal_mask = ~flat_mask

        # 一字板：全部筹码落在最近价格点
        if flat_mask.any():
            flat_rows = np.where(flat_mask)[0]
            nearest = np.argmin(np.abs(P - lows[flat_mask][:, None]), axis=1)
            dist[flat_rows, nearest] = 1.0

        # 正常交易日：向量化三角分布
        if normal_mask.any():
            Ln, Hn, PKn, Wn = L[normal_mask], H[normal_mask], PK[normal_mask], W[normal_mask]
            left = (P >= Ln) & (P <= PKn)
            right = (P > PKn) & (P <= Hn)
            with np.errstate(divide='ignore', invalid='ignore'):
                lv = (P - Ln) / (PKn - Ln) * (2.0 / Wn)
                rv = (Hn - P) / (Hn - PKn) * (2.0 / Wn)
            vals = np.zeros_like(lv)
            vals[left] = lv[left]
            vals[right] = rv[right]
            row_sum = vals.sum(axis=1, keepdims=True)
            row_sum[row_sum <= 0] = 1.0
            vals /= row_sum
            dist[normal_mask] = vals

        return dist

    def _triangle_distribution(self, low, high, prices):
        """单条三角分布（兼容旧接口，内部调用向量化版本）"""
        return self._triangle_distribution_matrix(
            np.array([low]), np.array([high]), prices
        )[0]

    def calc_concentration(self, prices, chip_dist):
        """
        计算筹码集中度指标

        参数:
            prices: 价格数组
            chip_dist: 筹码分布数组

        返回:
            dict:
                - conc_90: 90%筹码集中度（%）
                - conc_70: 70%筹码集中度（%）
                - peak_price: 筹码峰值价格
                - peak_ratio: 主峰占比（%）
                - kurtosis: 峰度（越大越尖锐）
                - skewness: 偏度（0为对称）
                - center_offset: 90%与70%区间中心的偏离度（%），越小越对称集中
                - score: 综合评分（0-100）
        """
        if prices is None or chip_dist is None:
            return None

        total = chip_dist.sum()
        if total <= 0:
            return None

        chip_dist = chip_dist / total

        # 峰值价格
        peak_idx = np.argmax(chip_dist)
        peak_price = prices[peak_idx]

        # 主峰占比（峰值附近 ±2σ 范围内的筹码）
        mean_price = np.sum(prices * chip_dist)
        std_price = np.sqrt(np.sum((prices - mean_price) ** 2 * chip_dist))

        if std_price > 0:
            peak_mask = np.abs(prices - peak_price) <= 2 * std_price
            peak_ratio = chip_dist[peak_mask].sum() * 100
        else:
            peak_ratio = 100

        # 计算 90% 和 70% 集中度（返回 宽度%、区间下界、区间上界）
        conc_90, lower_90, upper_90 = self._calc_concentration_percent(
            prices, chip_dist, 0.90
        )
        conc_70, lower_70, upper_70 = self._calc_concentration_percent(
            prices, chip_dist, 0.70
        )

        # 中心重合度：90%区间中心 与 70%区间中心 的偏离度
        # 偏离越小，说明筹码分布越对称、越集中于同一中心（典型单峰形态）
        center_90 = (lower_90 + upper_90) / 2
        center_70 = (lower_70 + upper_70) / 2
        if mean_price > 0:
            center_offset = abs(center_90 - center_70) / mean_price * 100
        else:
            center_offset = 100

        # 峰度和偏度（相对于价格分布）
        if std_price > 0:
            # 峰度：四阶中心矩 / 标准差^4
            kurtosis = (
                np.sum((prices - mean_price) ** 4 * chip_dist)
                / (std_price ** 4)
            )
            # 偏度：三阶中心矩 / 标准差^3
            skewness = (
                np.sum((prices - mean_price) ** 3 * chip_dist)
                / (std_price ** 3)
            )
        else:
            kurtosis = 100
            skewness = 0

        # 综合评分（0-100）
        score = self._calc_score(
            conc_90, conc_70, peak_ratio, kurtosis, skewness, center_offset
        )

        return {
            'conc_90': round(conc_90, 2),
            'conc_70': round(conc_70, 2),
            'peak_price': round(peak_price, 2),
            'peak_ratio': round(peak_ratio, 1),
            'kurtosis': round(kurtosis, 2),
            'skewness': round(skewness, 3),
            'center_offset': round(center_offset, 2),
            'score': round(score, 1),
        }

    def _calc_concentration_percent(self, prices, chip_dist, percent):
        """
        计算指定百分比筹码的集中度

        集中度 = (价格上界 - 价格下界) / 中位价 * 100%
        值越小表示筹码越集中

        返回:
            tuple: (集中度%, 区间下界价格, 区间上界价格)
        """
        cumulative = np.cumsum(chip_dist)

        # 找到包含中间 percent 筹码的价格区间
        lower_q = (1 - percent) / 2
        upper_q = 1 - lower_q

        # 下界
        idx_lower = np.searchsorted(cumulative, lower_q)
        idx_lower = min(idx_lower, len(prices) - 1)

        # 上界
        idx_upper = np.searchsorted(cumulative, upper_q)
        idx_upper = min(idx_upper, len(prices) - 1)

        price_lower = prices[idx_lower]
        price_upper = prices[idx_upper]

        # 中位价
        mid_price = prices[np.searchsorted(cumulative, 0.5)]
        mid_price = min(mid_price, prices[-1])

        if mid_price <= 0:
            return 100, price_lower, price_upper

        concentration = (price_upper - price_lower) / mid_price * 100
        return concentration, price_lower, price_upper

    def _calc_score(self, conc_90, conc_70, peak_ratio, kurtosis, skewness,
                    center_offset):
        """
        计算综合评分（0-100，6个维度）

        评分权重：
        1. 90%集中度   25分（越小越好）
        2. 70%集中度   15分（越小越好）
        3. 主峰占比    25分（越大越好）
        4. 峰度        15分（越大越尖锐）
        5. 偏度        10分（接近0为对称正态）
        6. 中心重合度  10分（90%/70%区间中心偏离越小越好）
        """
        score = 0

        # 1. 90%集中度（0-25分）
        c90_max = self.cfg.chip_conc90_max
        if conc_90 <= c90_max:
            score += 25
        elif conc_90 <= c90_max * 1.5:
            score += 25 * (1 - (conc_90 - c90_max) / (c90_max * 0.5))

        # 2. 70%集中度（0-15分）
        c70_max = self.cfg.chip_conc70_max
        if conc_70 <= c70_max:
            score += 15
        elif conc_70 <= c70_max * 1.5:
            score += 15 * (1 - (conc_70 - c70_max) / (c70_max * 0.5))

        # 3. 主峰占比（0-25分）
        pr_min = self.cfg.chip_peak_ratio_min * 100
        if peak_ratio >= pr_min:
            score += 25
        elif peak_ratio >= 50:
            score += 25 * (peak_ratio - 50) / (pr_min - 50)

        # 4. 峰度（0-15分）
        if kurtosis >= self.cfg.chip_kurtosis_min:
            score += 15
        elif kurtosis >= 2:
            score += 15 * (kurtosis - 2) / (self.cfg.chip_kurtosis_min - 2)

        # 5. 偏度（0-10分）
        skew_min, skew_max = self.cfg.chip_skewness_range
        if skew_min <= skewness <= skew_max:
            score += 10
        elif abs(skewness) <= 1:
            score += 10 * (1 - abs(skewness))

        # 6. 中心重合度（0-10分）
        # 90%区间中心与70%区间中心的偏离度，越小越集中于同一中心
        offset_max = self.cfg.chip_center_offset_max
        offset_zero = self.cfg.chip_center_offset_zero
        if center_offset <= offset_max:
            score += 10
        elif center_offset <= offset_zero:
            score += 10 * (offset_zero - center_offset) / (offset_zero - offset_max)

        return score

    def analyze_stock(self, df, float_shares=None):
        """
        分析单只股票的筹码分布

        参数:
            df: DataFrame，日线数据
            float_shares: float, 流通股本

        返回:
            dict: 集中度指标 + pass（是否通过筛选）
        """
        if df is None or len(df) < 10:
            return None

        prices, chip_dist = self.calc_chip_distribution(df, float_shares)

        if prices is None:
            return None

        metrics = self.calc_concentration(prices, chip_dist)

        if metrics is None:
            return None

        # 判断是否通过筛选
        passed = self._check_filter(metrics)
        metrics['pass'] = passed

        return metrics

    def _check_filter(self, metrics):
        """检查是否通过筹码集中度筛选"""
        checks = [
            metrics['conc_90'] <= self.cfg.chip_conc90_max,
            metrics['conc_70'] <= self.cfg.chip_conc70_max,
            metrics['peak_ratio'] >= self.cfg.chip_peak_ratio_min * 100,
            metrics['kurtosis'] >= self.cfg.chip_kurtosis_min,
            (self.cfg.chip_skewness_range[0]
             <= metrics['skewness']
             <= self.cfg.chip_skewness_range[1]),
            metrics['score'] >= self.cfg.chip_min_score,
        ]
        return all(checks)

    def scan_stocks(self, stock_list, max_workers=1, print_detail=True,
                    data_cache=None):
        """
        批量扫描股票池

        参数:
            stock_list: list, [{'symbol': '600183', 'name': '生益科技'}, ...]
            max_workers: int, 并发线程数
            print_detail: bool, 是否打印详细报告
            data_cache: dict, 预加载的日线数据缓存 {symbol: df}

        返回:
            list: 扫描结果列表，按评分降序排列
        """
        from app.quant.data.fetcher import load_real_data

        results = []
        total = len(stock_list)
        cache = data_cache or {}

        if print_detail:
            reporting.section("筹码峰集中度扫描（最近交易日）", width=70)

        def process_stock(stock, verbose=False):
            symbol = stock['symbol']
            # 优先使用缓存
            df = cache.get(symbol)
            if df is None or df.empty:
                df = load_real_data(
                    symbol=symbol, start_date="20240101", verbose=verbose
                )
            if df is None or df.empty:
                return None

            metrics = self.analyze_stock(df)
            if metrics is None:
                return None

            return {
                'symbol': symbol,
                'name': stock.get('name', ''),
                'close': df.iloc[-1]['close'],
                'metrics': metrics,
            }

        if max_workers <= 1:
            for i, stock in enumerate(stock_list, 1):
                if print_detail:
                    sys.stdout.write(f"\r  扫描进度: {i}/{total}")
                    sys.stdout.flush()

                result = process_stock(stock, verbose=False)
                if result:
                    results.append(result)
        else:
            completed = 0
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(process_stock, stock): stock
                    for stock in stock_list
                }

                for future in as_completed(futures):
                    completed += 1
                    if print_detail:
                        sys.stdout.write(f"\r  扫描进度: {completed}/{total}")
                        sys.stdout.flush()

                    result = future.result()
                    if result:
                        results.append(result)

        if print_detail:
            print()

        # 按评分降序排列
        results.sort(key=lambda x: x['metrics']['score'], reverse=True)

        # 打印报告
        if print_detail:
            reporting.print_chip_scan_report(results)

        return results


if __name__ == "__main__":
    from app.quant.config import STOCK_LIST

    analyzer = ChipDistributionAnalyzer()
    analyzer.scan_stocks(STOCK_LIST)