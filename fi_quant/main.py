"""
主程序入口
- 策略选择菜单
- 多股票批量扫描
- 汇总报告输出
- 全量处理时减少控制台输出，结果写入 result.txt
- 多线程并发优化性能
- 各策略独立过滤模式 + 动态数据起始日期
"""

import os
import sys
import time
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import (
    StrategyConfig,
    STOCK_LIST_LEAD_LAG,
    STOCK_LIST,
)
from data import (
    load_real_data,
    load_copper_futures,
    load_fund_flow,
    load_margin_trading,
    load_all_stocks_filtered,
    parallel_fetch_dispatch,
)
from strategies import (
    BreakoutPullbackEngine,
    LeadLagEngine,
    PairTradingEngine,
    SmartMoneyEngine,
    MeanReversionEngine,
    AccumulationEngine,
)
from backtesting import run_backtest
from utils import run_box_detection, TrendFilter, ChipDistributionAnalyzer


# 全量处理时的并发线程数（可根据网络情况调整）
MAX_WORKERS = 50

# 结果输出文件
RESULT_FILE = os.path.join(os.path.dirname(__file__), "result.txt")


def calc_start_date(lookback_days):
    """根据回溯天数计算起始日期（YYYYMMDD格式）"""
    start = datetime.now() - timedelta(days=lookback_days)
    return start.strftime("%Y%m%d")


class ProgressBar:
    """简单的控制台进度条（单行刷新）"""

    def __init__(self, total, label="进度", bar_width=30):
        self.total = total
        self.label = label
        self.bar_width = bar_width
        self.start_time = time.time()
        self.completed = 0

    def update(self, n=1):
        """更新进度"""
        self.completed += n
        self._display()

    def _format_time(self, seconds):
        """格式化时间为 mm:ss 或 hh:mm:ss"""
        if seconds < 0:
            return "--:--"
        seconds = int(seconds)
        if seconds < 3600:
            return f"{seconds // 60}分{seconds % 60:02d}秒"
        else:
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h}时{m:02d}分{s:02d}秒"

    def _display(self):
        """显示进度条"""
        if self.total == 0:
            return
        pct = self.completed / self.total
        filled = int(self.bar_width * pct)
        bar = '█' * filled + '░' * (self.bar_width - filled)

        # 计算 ETA
        elapsed = time.time() - self.start_time
        if self.completed > 0:
            eta = elapsed * (self.total - self.completed) / self.completed
            eta_str = self._format_time(eta)
        else:
            eta_str = "--:--"

        line = (f"\r  {self.label}: [{bar}] {self.completed}/{self.total} "
                f"({pct:.0%}) ETA: {eta_str}  ")
        sys.stdout.write(line)
        sys.stdout.flush()

    def finish(self):
        """完成进度条，换行"""
        self._display()
        print()  # 换行


def print_banner():
    """打印欢迎横幅"""
    print("\n" + "=" * 60)
    print("  量化信号扫描系统 v2.0")
    print("  五大策略 · 多票扫描 · 条件开关")
    print("=" * 60)


# 全局扫描模式标志
SCAN_MODE = "pool"  # "pool" = 股票池, "all" = 全量A股


def is_verbose():
    """是否打印详细日志（股票池模式打印，全量模式不打印）"""
    return SCAN_MODE == "pool"


def print_menu():
    """打印策略选择菜单"""
    mode_text = "股票池" if SCAN_MODE == "pool" else "全量A股"
    print(f"\n  当前扫描范围：【{mode_text}】")
    print("  请选择策略：")
    print("  ─────────────────────────────────────")
    print("  [1] 突破回踩策略（箱体突破+回踩确认）")
    print("  [2] 产业链动量滞后（下游启动→上游潜伏）")
    print("  [3] 铜价配对交易（铜价破位+成本释放）")
    print("  [4] 聪明资金追踪（主力资金+融资清洗）")
    print("  [5] 均值回归+周期共振（大周期支撑+超卖）")
    print("  [6] 主力吸筹策略（60分钟吸筹评分+突破确认）")
    print("  [7] 全部策略扫描")
    print("  ─────────────────────────────────────")
    print("  [8] 箱体检测（批量扫描）")
    print("  [9] 突破回踩统一回测（日线，次日开盘成交）")
    print("  [10] 筹码峰集中度扫描（高度集中正态分布筛选）")
    print("  [S] 切换扫描范围（股票池 ↔ 全量A股）")
    print("  [0] 退出")
    print("  ─────────────────────────────────────")


def apply_trend_filter(config, stock_list, verbose=None,
                       max_workers=1, return_data=False,
                       mode="strong"):
    """
    对股票池进行日线趋势预筛选

    参数:
        config: 策略配置
        stock_list: 股票列表
        verbose: 是否打印详细日志
        max_workers: 并发线程数
        return_data: 是否返回日线数据缓存
        mode: 过滤模式 "strong" / "loose" / "oversold"
    """
    if not config.enable_trend_filter:
        if return_data:
            return stock_list, {}
        return stock_list

    if verbose is None:
        verbose = is_verbose()

    tf = TrendFilter(config)
    result = tf.filter_pool(
        stock_list,
        print_detail=verbose,
        max_workers=max_workers,
        return_data=return_data,
        mode=mode,
    )
    return result


def get_stock_pool(verbose=None):
    """
    根据 SCAN_MODE 获取股票池
    - "pool": 返回 STOCK_LIST（预定义股票池）
    - "all": 返回全量A股（过滤后）
    """
    if verbose is None:
        verbose = is_verbose()
    if SCAN_MODE == "all":
        all_stocks = load_all_stocks_filtered(verbose=verbose)
        if not all_stocks:
            return []
        return [
            {"symbol": s["symbol"], "name": s["name"]}
            for s in all_stocks
        ]
    else:
        return STOCK_LIST


# ==========================================
# 单股票扫描函数（用于多线程调用）
# ==========================================

def _scan_single_breakout(stock, engine, tf, verbose, start_date):
    """扫描单只股票 - 突破回踩"""
    symbol = stock["symbol"]
    name = stock["name"]
    df = load_real_data(
        symbol=symbol, start_date=start_date, timeframe=tf,
        verbose=verbose
    )
    if df.empty:
        return None
    signals = engine.scan_signals(df, debug=verbose)
    if not signals.empty:
        signals['stock'] = f"{name}({symbol})"
        return signals
    return None


def _scan_single_pair(stock, engine, verbose, start_date):
    """扫描单只股票 - 铜价配对"""
    symbol = stock["symbol"]
    name = stock["name"]
    df = load_real_data(
        symbol=symbol, start_date=start_date, verbose=verbose
    )
    if df.empty:
        return None
    signals = engine.scan_signals(df, debug=verbose)
    if not signals.empty:
        signals['stock'] = f"{name}({symbol})"
        return signals
    return None


def _scan_single_smart(stock, engine_cls, config, verbose,
                       start_date):
    """扫描单只股票 - 聪明资金"""
    symbol = stock["symbol"]
    name = stock["name"]
    df = load_real_data(
        symbol=symbol, start_date=start_date, verbose=verbose
    )
    if df.empty:
        return None
    fund_flow_df = load_fund_flow(symbol=symbol, verbose=verbose)
    margin_df = load_margin_trading(symbol=symbol, verbose=verbose)
    engine = engine_cls(config, fund_flow_df, margin_df)
    signals = engine.scan_signals(df, debug=verbose)
    if not signals.empty:
        signals['stock'] = f"{name}({symbol})"
        return signals
    return None


def _scan_single_mean_rev(stock, engine, verbose, start_date):
    """扫描单只股票 - 均值回归"""
    symbol = stock["symbol"]
    name = stock["name"]
    df = load_real_data(
        symbol=symbol, start_date=start_date, verbose=verbose
    )
    if df.empty:
        return None
    signals = engine.scan_signals(df, debug=verbose)
    if not signals.empty:
        signals['stock'] = f"{name}({symbol})"
        return signals
    return None


def _scan_single_accumulation(stock, engine, verbose, start_date):
    """扫描单只股票 - 主力吸筹"""
    symbol = stock["symbol"]
    name = stock["name"]
    df = load_real_data(
        symbol=symbol, start_date=start_date, timeframe="60m",
        verbose=verbose
    )
    if df.empty:
        return None
    signals = engine.scan_signals(df, debug=verbose)
    if not signals.empty:
        signals['stock'] = f"{name}({symbol})"
        return signals
    return None


# ==========================================
# 通用并发扫描器
# ==========================================

def _run_parallel(stock_pool, scan_func, verbose, max_workers,
                  progress_label="扫描"):
    """
    通用并发扫描框架

    参数:
        stock_pool: 股票列表
        scan_func: callable(stock) -> DataFrame or None
        verbose: 是否打印详细日志
        max_workers: 并发线程数
        progress_label: 进度显示标签

    返回:
        list[DataFrame]: 所有信号
    """
    all_signals = []
    total = len(stock_pool)

    if max_workers <= 1:
        # 串行处理
        for i, stock in enumerate(stock_pool, 1):
            name = stock["name"]
            symbol = stock["symbol"]
            if verbose:
                print(f"\n  >> {name} ({symbol})")
            result = scan_func(stock)
            if result is not None:
                all_signals.append(result)
                if verbose:
                    print(f"     发现 {len(result)} 个信号")
            else:
                if verbose:
                    print("     无信号")
    else:
        # 多线程并发 + 进度条
        print(f"\n  {progress_label}: {total} 只股票，"
              f"{max_workers} 线程并发...")

        progress = ProgressBar(total, label=progress_label)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(scan_func, stock): stock
                for stock in stock_pool
            }

            for future in as_completed(futures):
                progress.update(1)

                try:
                    result = future.result()
                    if result is not None:
                        all_signals.append(result)
                except Exception:
                    pass

        progress.finish()

    return all_signals


# ==========================================
# 各策略运行函数
# ==========================================

def run_breakout_pullback(config):
    """运行策略0：突破回踩"""
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_breakout)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：突破回踩（箱体突破+回踩确认）")
        print(f"  时间框架：{config.signal_timeframe}")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    raw_pool = get_stock_pool(verbose=verbose)
    if not raw_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    stock_pool = apply_trend_filter(
        config, raw_pool, verbose=verbose, max_workers=workers,
        mode=config.filter_mode_breakout
    )
    if not stock_pool:
        if verbose:
            print("  预筛选后无符合条件的股票")
        return []

    engine = BreakoutPullbackEngine(config)
    tf = config.signal_timeframe

    def scan_func(stock):
        return _scan_single_breakout(
            stock, engine, tf, verbose, start_date
        )

    return _run_parallel(
        stock_pool, scan_func, verbose, workers, "突破回踩扫描"
    )


def run_lead_lag(config):
    """运行策略1：产业链动量滞后"""
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_lead_lag)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：产业链动量滞后（下游PCB→上游覆铜板）")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    upstream_pool = [
        s for s in STOCK_LIST_LEAD_LAG if s["role"] == "upstream"
    ]
    downstream_pool = [
        s for s in STOCK_LIST_LEAD_LAG if s["role"] == "downstream"
    ]
    filtered_upstream = apply_trend_filter(
        config, upstream_pool,
        mode=config.filter_mode_lead_lag
    )
    if not filtered_upstream:
        print("  预筛选后无符合条件的上游股票")
        return []

    # 获取下游股票数据
    downstream_data = {}
    for stock in downstream_pool:
        if stock["role"] == "downstream":
            if verbose:
                print(f"\n  加载下游: {stock['name']} "
                      f"({stock['symbol']})")
            df = load_real_data(
                symbol=stock["symbol"], start_date=start_date,
                verbose=verbose
            )
            if not df.empty:
                downstream_data[stock["symbol"]] = df

    if not downstream_data:
        print("  下游数据获取失败")
        return []

    engine = LeadLagEngine(config, downstream_data)
    all_signals = []

    for stock in filtered_upstream:
        symbol = stock["symbol"]
        name = stock["name"]
        if verbose:
            print(f"\n  >> 扫描上游: {name} ({symbol})")

        df = load_real_data(
            symbol=symbol, start_date=start_date, verbose=verbose
        )
        if df.empty:
            if verbose:
                print("     数据获取失败，跳过")
            continue

        signals = engine.scan_signals(df, debug=verbose)
        if not signals.empty:
            signals['stock'] = f"{name}({symbol})"
            all_signals.append(signals)
            if verbose:
                print(f"     发现 {len(signals)} 个信号")
        else:
            if verbose:
                print("     无信号")

    return all_signals


def run_pair_trading(config):
    """运行策略2：铜价配对交易"""
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_pair)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：铜价配对交易（铜价破位+成本释放）")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    raw_pool = get_stock_pool(verbose=verbose)
    if not raw_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    stock_pool = apply_trend_filter(
        config, raw_pool, verbose=verbose, max_workers=workers,
        mode=config.filter_mode_pair
    )
    if not stock_pool:
        if verbose:
            print("  预筛选后无符合条件的股票")
        return []

    copper_df = load_copper_futures(start_date=start_date, verbose=verbose)
    if copper_df.empty:
        if verbose:
            print("  铜价数据获取失败")
        return []

    engine = PairTradingEngine(config, copper_df)

    def scan_func(stock):
        return _scan_single_pair(stock, engine, verbose, start_date)

    return _run_parallel(
        stock_pool, scan_func, verbose, workers, "铜价配对扫描"
    )


def run_smart_money(config):
    """运行策略3：聪明资金追踪"""
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_smart)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：聪明资金追踪（主力资金+融资清洗）")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    raw_pool = get_stock_pool(verbose=verbose)
    if not raw_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    stock_pool = apply_trend_filter(
        config, raw_pool, verbose=verbose, max_workers=workers,
        mode=config.filter_mode_smart
    )
    if not stock_pool:
        if verbose:
            print("  预筛选后无符合条件的股票")
        return []

    def scan_func(stock):
        return _scan_single_smart(
            stock, SmartMoneyEngine, config, verbose, start_date
        )

    return _run_parallel(
        stock_pool, scan_func, verbose, workers, "聪明资金扫描"
    )


def run_mean_reversion(config):
    """运行策略4：均值回归+周期共振"""
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_mean_rev)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：均值回归+周期共振（大周期支撑+超卖）")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    raw_pool = get_stock_pool(verbose=verbose)
    if not raw_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    stock_pool = apply_trend_filter(
        config, raw_pool, verbose=verbose, max_workers=workers,
        mode=config.filter_mode_mean_rev
    )
    if not stock_pool:
        if verbose:
            print("  预筛选后无符合条件的股票")
        return []

    engine = MeanReversionEngine(config)

    def scan_func(stock):
        return _scan_single_mean_rev(
            stock, engine, verbose, start_date
        )

    return _run_parallel(
        stock_pool, scan_func, verbose, workers, "均值回归扫描"
    )


def run_chip_scan(config):
    """运行筹码峰集中度扫描"""
    verbose = is_verbose()
    if verbose:
        print("\n" + "=" * 60)
        print("  筹码峰集中度扫描")
        print("  筛选高度集中的正态分布筹码峰")
        print("=" * 60)

    stock_pool = get_stock_pool(verbose=verbose)
    if not stock_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    analyzer = ChipDistributionAnalyzer(config)
    results = analyzer.scan_stocks(
        stock_pool, max_workers=workers, print_detail=verbose
    )
    return results


def run_accumulation(config):
    """运行策略5：60分钟主力吸筹（并行优化版）
    
    优化点：
    1. 使用分派式并行数据获取（多数据源并行）
    2. 数据获取与信号计算分离
    3. 向量化计算优化
    """
    verbose = is_verbose()
    start_date = calc_start_date(config.data_lookback_accumulation)
    if verbose:
        print("\n" + "=" * 60)
        print("  策略：主力吸筹（60分钟吸筹评分+突破确认）")
        print("  时间框架：60分钟")
        print(f"  最低评分：{config.acc_min_score}分")
        print(f"  数据起始：{start_date}")
        print("=" * 60)

    raw_pool = get_stock_pool(verbose=verbose)
    if not raw_pool:
        if verbose:
            print("  股票池为空")
        return []

    workers = 1 if verbose else MAX_WORKERS
    stock_pool = apply_trend_filter(
        config, raw_pool, verbose=verbose, max_workers=workers,
        mode=config.filter_mode_accumulation
    )
    if not stock_pool:
        if verbose:
            print("  预筛选后无符合条件的股票")
        return []

    engine = AccumulationEngine(config)
    
    # ===== 优化：使用分派式并行数据获取 =====
    if workers > 1 and len(stock_pool) > 10:
        print(f"\n  [并行优化] 使用分派式并行获取 {len(stock_pool)} 只股票数据...")
        
        # 进度回调
        progress = ProgressBar(len(stock_pool), label="数据获取")
        
        def progress_callback(completed, total, symbol):
            progress.update(1)
        
        # 并行获取所有数据
        data_dict = parallel_fetch_dispatch(
            stock_pool,
            start_date=start_date,
            timeframe="60m",
            max_workers_per_source=10,
            verbose=verbose,
            progress_callback=progress_callback if not verbose else None,
        )
        progress.finish()
        
        print(f"  [并行优化] 数据获取完成，成功 {len(data_dict)}/{len(stock_pool)}")
        
        # 并发处理信号计算
        all_signals = []
        
        def process_stock(stock):
            symbol = stock["symbol"]
            name = stock["name"]
            df = data_dict.get(symbol)
            if df is None or df.empty:
                return None
            signals = engine.scan_signals(df.copy(), debug=False)
            if not signals.empty:
                signals['stock'] = f"{name}({symbol})"
                return signals
            return None
        
        # 使用线程池并发计算信号
        print(f"  [并行优化] 并发计算信号...")
        progress2 = ProgressBar(len(stock_pool), label="信号计算")
        
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_stock, stock): stock
                for stock in stock_pool
            }
            for future in as_completed(futures):
                progress2.update(1)
                try:
                    result = future.result()
                    if result is not None:
                        all_signals.append(result)
                except Exception:
                    pass
        
        progress2.finish()
        return all_signals
    else:
        # 串行模式（股票池模式或股票数量少）
        def scan_func(stock):
            return _scan_single_accumulation(
                stock, engine, verbose, start_date
            )

        return _run_parallel(
            stock_pool, scan_func, verbose, workers, "主力吸筹扫描"
        )


# ==========================================
# 结果输出
# ==========================================

def print_summary(strategy_name, signals_list):
    """打印单个策略的汇总报告"""
    print(f"\n--- {strategy_name} ---")
    if signals_list:
        combined = pd.concat(signals_list, ignore_index=True)
        cols = ['stock'] + [
            c for c in combined.columns if c != 'stock'
        ]
        combined = combined[cols]
        print(f"  共 {len(combined)} 个信号:\n")
        print(combined.to_string(index=False))
    else:
        print("  无信号")


def save_results_to_file(results):
    """
    将扫描结果写入 result.txt

    参数:
        results: dict, {策略名: [signals_df, ...]}
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    mode_text = "全量A股" if SCAN_MODE == "all" else "股票池"

    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        f.write("=" * 60 + "\n")
        f.write("  量化信号扫描结果\n")
        f.write(f"  扫描时间: {timestamp}\n")
        f.write(f"  扫描模式: {mode_text}\n")
        f.write("=" * 60 + "\n\n")

        total_signals = 0

        for strategy_name, signals_list in results.items():
            f.write(f"\n--- {strategy_name} ---\n")
            if signals_list:
                combined = pd.concat(
                    signals_list, ignore_index=True
                )
                cols = ['stock'] + [
                    c for c in combined.columns if c != 'stock'
                ]
                combined = combined[cols]
                total_signals += len(combined)
                f.write(f"  共 {len(combined)} 个信号:\n\n")
                f.write(combined.to_string(index=False))
                f.write("\n")
            else:
                f.write("  无信号\n")

        f.write("\n" + "=" * 60 + "\n")
        f.write(f"  扫描完成，共发现 {total_signals} 个信号\n")
        f.write("=" * 60 + "\n")

    print(f"\n  结果已保存到: {RESULT_FILE}")


# ==========================================
# 主程序
# ==========================================

def main():
    global SCAN_MODE
    print_banner()
    config = StrategyConfig()

    while True:
        print_menu()
        choice = input("\n  请输入选项: ").strip().upper()

        if choice == "0":
            print("\n  再见！")
            break

        if choice == "S":
            SCAN_MODE = "all" if SCAN_MODE == "pool" else "pool"
            mode_text = "全量A股" if SCAN_MODE == "all" else "股票池"
            print(f"\n  已切换到：【{mode_text}】模式")
            continue

        results = {}
        start_time = time.time()

        if choice in ("1", "7"):
            signals = run_breakout_pullback(config)
            results["突破回踩"] = signals

        if choice in ("2", "7"):
            signals = run_lead_lag(config)
            results["产业链动量滞后"] = signals

        if choice in ("3", "7"):
            signals = run_pair_trading(config)
            results["铜价配对交易"] = signals

        if choice in ("4", "7"):
            signals = run_smart_money(config)
            results["聪明资金追踪"] = signals

        if choice in ("5", "7"):
            signals = run_mean_reversion(config)
            results["均值回归+周期共振"] = signals

        if choice == "6":
            signals = run_accumulation(config)
            results["主力吸筹"] = signals

        if choice == "8":
            run_box_detection()
            continue

        if choice == "10":
            run_chip_scan(config)
            continue

        if choice == "9":
            print("\n  开始日线突破回踩统一回测...")
            result = run_backtest(strategy_name="breakout")
            metrics = result.get("metrics")
            if metrics is not None:
                print("\n" + pd.Series(metrics).to_string())
            print("  明细已保存到: backtest_results")
            continue

        if not results:
            print("\n  无效选项，请重新输入")
            continue

        elapsed = time.time() - start_time

        # 全量模式：仅输出简要信息，详细结果写入文件
        if SCAN_MODE == "all":
            total_signals = sum(
                len(pd.concat(s, ignore_index=True)) if s else 0
                for s in results.values()
            )
            print("\n" + "=" * 60)
            print("  全量扫描完成")
            print(f"  耗时: {elapsed:.1f}秒")
            print(f"  共发现 {total_signals} 个信号")
            print("=" * 60)
            save_results_to_file(results)
        else:
            # 股票池模式：控制台显示详细报告
            print("\n" + "=" * 60)
            print("  扫描汇总报告")
            print(f"  耗时: {elapsed:.1f}秒")
            print("=" * 60)

            for name, signals in results.items():
                print_summary(name, signals)

            print("\n" + "=" * 60)
            print("  扫描完成")
            print("=" * 60)


if __name__ == "__main__":
    main()
