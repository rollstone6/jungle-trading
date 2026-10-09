"""数据获取主模块。

提供行情数据获取、期货数据、资金流向、股票池管理等功能。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from app.quant.data.cache import (
    get_mem_cache,
    get_mem_cache_safe,
    load_disk_cache,
    save_disk_cache,
    set_mem_cache,
    set_mem_cache_safe,
)
from app.quant.data.providers import (
    fetch_all_stocks,
    fetch_copper_futures,
    fetch_eastmoney,
    fetch_fund_flow,
    fetch_margin_trading,
    fetch_sina,
    fetch_sina_minute,
    fetch_tencent,
    fetch_tushare,
    fetch_with_retry,
)


def load_real_data(
    symbol: str = "688001",
    start_date: str = "20240101",
    timeframe: str = "1d",
    verbose: bool = True,
) -> pd.DataFrame:
    """多数据源级联获取 A 股K线数据（自动重试 + 自动降级 + 磁盘缓存）。

    参数:
        symbol: 股票代码
        start_date: 起始日期 YYYYMMDD
        timeframe: K线周期 "1d" | "60m" | "30m" | "120m"
        verbose: 是否打印详细日志

    优先级: 磁盘缓存 -> Tushare -> 东方财富 -> 新浪财经 -> 腾讯财经
    """
    # 内存缓存检查
    mem_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
    cached = get_mem_cache(mem_cache_key)
    if cached is not None:
        return cached

    # 磁盘缓存检查
    disk_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
    cached_df = load_disk_cache(disk_cache_key)
    if cached_df is not None and not cached_df.empty:
        set_mem_cache(mem_cache_key, cached_df)
        return cached_df

    sources = [
        ("Tushare", lambda: fetch_tushare(symbol, start_date, timeframe)),
        ("东方财富", lambda: fetch_eastmoney(symbol, start_date, timeframe)),
        ("新浪分钟", lambda: fetch_sina_minute(symbol, start_date, timeframe)),
        ("新浪财经", lambda: fetch_sina(symbol, start_date, timeframe)),
        ("腾讯财经", lambda: fetch_tencent(symbol, start_date, timeframe)),
    ]

    for source_name, fetch_func in sources:
        if verbose:
            print(f"  [{source_name}] 获取 {symbol} ...")
        df = fetch_with_retry(
            fetch_func,
            max_retries=1,
            delay=0,
            source_name=source_name,
            verbose=verbose,
        )

        if df is not None and not df.empty:
            if verbose:
                print(f"  [{source_name}] 成功 {len(df)} 条")
            set_mem_cache(mem_cache_key, df)
            save_disk_cache(disk_cache_key, df)
            return df
        else:
            if verbose:
                print(f"  [{source_name}] 失败，切换下一数据源...")

    if verbose:
        print(f"  所有数据源均失败: {symbol}")
    return pd.DataFrame()


def load_copper_futures(start_date: str = "20240101", verbose: bool = True) -> pd.DataFrame:
    """获取沪铜期货日线数据。"""
    cache_key = f"copper_{start_date}"
    cached = get_mem_cache(cache_key)
    if cached is not None:
        return cached

    if verbose:
        print("  [期货数据] 获取沪铜期货...")
    df = fetch_with_retry(
        lambda: fetch_copper_futures(start_date),
        max_retries=1,
        delay=0,
        source_name="期货",
        verbose=verbose,
    )
    if df is not None and not df.empty:
        if verbose:
            print(f"  [期货数据] 成功 {len(df)} 条")
        set_mem_cache(cache_key, df)
        return df
    if verbose:
        print("  [期货数据] 失败")
    return pd.DataFrame()


def load_fund_flow(symbol: str = "600183", verbose: bool = True) -> pd.DataFrame:
    """获取个股主力资金流向数据。"""
    cache_key = f"fund_flow_{symbol}"
    cached = get_mem_cache(cache_key)
    if cached is not None:
        return cached

    if verbose:
        print(f"  [资金流向] 获取 {symbol} 主力资金...")
    df = fetch_with_retry(
        lambda: fetch_fund_flow(symbol),
        max_retries=1,
        delay=0,
        source_name="资金",
        verbose=verbose,
    )
    if df is not None and not df.empty:
        if verbose:
            print(f"  [资金流向] 成功 {len(df)} 条")
        set_mem_cache(cache_key, df)
        return df
    if verbose:
        print("  [资金流向] 失败")
    return pd.DataFrame()


def load_margin_trading(symbol: str = "600183", verbose: bool = True) -> pd.DataFrame:
    """获取个股融资融券数据。"""
    cache_key = f"margin_{symbol}"
    cached = get_mem_cache(cache_key)
    if cached is not None:
        return cached

    if verbose:
        print(f"  [融资融券] 获取 {symbol} 融资余额...")
    df = fetch_with_retry(
        lambda: fetch_margin_trading(symbol),
        max_retries=1,
        delay=0,
        source_name="融资",
        verbose=verbose,
    )
    if df is not None and not df.empty:
        if verbose:
            print(f"  [融资融券] 成功 {len(df)} 条")
        set_mem_cache(cache_key, df)
        return df
    if verbose:
        print("  [融资融券] 失败")
    return pd.DataFrame()


def _fetch_single_stock(stock, start_date, timeframe, source_name, fetch_func):
    """单只股票数据获取（用于并行调用）。"""
    symbol = stock["symbol"] if isinstance(stock, dict) else stock

    # 先检查缓存
    mem_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
    cached = get_mem_cache_safe(mem_cache_key)
    if cached is not None:
        return (symbol, cached, True, "缓存")

    # 磁盘缓存检查
    disk_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
    cached_df = load_disk_cache(disk_cache_key)
    if cached_df is not None and not cached_df.empty:
        set_mem_cache_safe(mem_cache_key, cached_df)
        return (symbol, cached_df, True, "磁盘缓存")

    # 调用数据源获取
    try:
        df = fetch_func(symbol, start_date, timeframe)
        if df is not None and not df.empty:
            set_mem_cache_safe(mem_cache_key, df)
            save_disk_cache(disk_cache_key, df)
            return (symbol, df, True, source_name)
    except Exception:
        pass

    return (symbol, pd.DataFrame(), False, source_name)


def parallel_fetch_dispatch(
    stock_list,
    start_date: str = "20240101",
    timeframe: str = "1d",
    max_workers_per_source: int = 10,
    verbose: bool = False,
    progress_callback=None,
) -> dict[str, pd.DataFrame]:
    """分派式并行数据获取：将股票分配给不同数据源并行获取。"""
    sources = [
        ("Tushare", fetch_tushare),
        ("东方财富", fetch_eastmoney),
        ("腾讯财经", fetch_tencent),
    ]

    # 过滤已有缓存的股票
    stocks_to_fetch = []
    results = {}

    for stock in stock_list:
        symbol = stock["symbol"] if isinstance(stock, dict) else stock
        mem_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"

        cached = get_mem_cache_safe(mem_cache_key)
        if cached is not None:
            results[symbol] = cached
            if progress_callback:
                progress_callback(len(results), len(stock_list), symbol)
            continue

        # 磁盘缓存检查
        disk_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
        cached_df = load_disk_cache(disk_cache_key)
        if cached_df is not None and not cached_df.empty:
            set_mem_cache_safe(mem_cache_key, cached_df)
            results[symbol] = cached_df
            if progress_callback:
                progress_callback(len(results), len(stock_list), symbol)
            continue

        stocks_to_fetch.append(stock)

    if verbose:
        print(
            f"  [并行获取] 共 {len(stock_list)} 只，"
            f"缓存命中 {len(results)} 只，"
            f"待获取 {len(stocks_to_fetch)} 只"
        )

    if not stocks_to_fetch:
        return results

    # 分派：轮询分配股票到各数据源
    dispatch = {name: [] for name, _ in sources}
    source_funcs = {name: func for name, func in sources}

    for i, stock in enumerate(stocks_to_fetch):
        source_idx = i % len(sources)
        source_name = sources[source_idx][0]
        dispatch[source_name].append(stock)

    if verbose:
        for name, stocks in dispatch.items():
            print(f"    {name}: 分配 {len(stocks)} 只")

    # 并行获取
    failed_stocks = []
    completed = len(results)

    total_workers = len(sources) * max_workers_per_source

    with ThreadPoolExecutor(max_workers=total_workers) as executor:
        futures = {}

        for source_name, stocks in dispatch.items():
            fetch_func = source_funcs[source_name]
            for stock in stocks:
                future = executor.submit(
                    _fetch_single_stock,
                    stock,
                    start_date,
                    timeframe,
                    source_name,
                    fetch_func,
                )
                futures[future] = stock

        for future in as_completed(futures):
            stock = futures[future]
            symbol = stock["symbol"] if isinstance(stock, dict) else stock

            try:
                sym, df, success, src = future.result()
                if success and not df.empty:
                    results[sym] = df
                else:
                    failed_stocks.append(stock)
            except Exception:
                failed_stocks.append(stock)

            completed += 1
            if progress_callback:
                progress_callback(completed, len(stock_list), symbol)

    # 故障转移
    if failed_stocks and verbose:
        print(f"  [并行获取] {len(failed_stocks)} 只失败，尝试故障转移...")

    for stock in failed_stocks:
        symbol = stock["symbol"] if isinstance(stock, dict) else stock

        for source_name, fetch_func in sources:
            try:
                df = fetch_func(symbol, start_date, timeframe)
                if df is not None and not df.empty:
                    results[symbol] = df
                    mem_cache_key = f"stock_{symbol}_{timeframe}_{start_date}"
                    set_mem_cache_safe(mem_cache_key, df)
                    save_disk_cache(f"stock_{symbol}_{timeframe}_{start_date}", df)
                    break
            except Exception:
                continue

    if verbose:
        print(f"  [并行获取] 完成，成功 {len(results)}/{len(stock_list)}")

    return results


def parallel_fetch_simple(
    stock_list,
    start_date: str = "20240101",
    timeframe: str = "1d",
    max_workers: int = 50,
    verbose: bool = False,
    progress_callback=None,
) -> dict[str, pd.DataFrame]:
    """简化版并行获取：使用默认数据源优先级，每只股票独立获取。"""
    results = {}
    total = len(stock_list)
    completed = 0

    def fetch_one(stock):
        symbol = stock["symbol"] if isinstance(stock, dict) else stock
        df = load_real_data(
            symbol=symbol,
            start_date=start_date,
            timeframe=timeframe,
            verbose=False,
        )
        return (symbol, df)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_one, stock): stock for stock in stock_list}

        for future in as_completed(futures):
            stock = futures[future]
            symbol = stock["symbol"] if isinstance(stock, dict) else stock

            try:
                sym, df = future.result()
                if df is not None and not df.empty:
                    results[sym] = df
            except Exception:
                pass

            completed += 1
            if progress_callback:
                progress_callback(completed, total, symbol)

    if verbose:
        print(f"  [并行获取] 完成，成功 {len(results)}/{total}")

    return results


# ----------------------------------------------------------------------
# 股票池管理（原 universe.py，合并至此）
# ----------------------------------------------------------------------

def load_all_stocks(verbose: bool = True) -> list[dict]:
    """获取全量A股股票列表。

    Returns:
        list: [{"symbol": "600183", "name": "生益科技"}, ...]
    """
    cache_key = "all_stocks"
    cached = get_mem_cache(cache_key)
    if cached is not None:
        return cached

    if verbose:
        print("  [股票列表] 获取全量A股列表...")

    result = fetch_with_retry(
        fetch_all_stocks,
        max_retries=2,
        delay=1,
        source_name="列表",
        verbose=verbose,
    )

    if result is not None:
        if verbose:
            print(f"  [股票列表] 成功获取 {len(result)} 只股票")
        set_mem_cache(cache_key, result)
        return result

    if verbose:
        print("  [股票列表] 获取失败")
    return []


def load_all_stocks_filtered(verbose: bool = True) -> list[dict]:
    """获取过滤后的A股列表（排除ST、退市、北交所等）。

    Returns:
        list: [{"symbol": "600183", "name": "生益科技"}, ...]
    """
    cache_key = "all_stocks_filtered"
    cached = get_mem_cache(cache_key)
    if cached is not None:
        return cached

    all_stocks = load_all_stocks(verbose=verbose)
    if not all_stocks:
        return []

    filtered = []
    for stock in all_stocks:
        symbol = stock["symbol"]
        name = stock["name"]

        # 排除 ST 股
        if "ST" in name or "*ST" in name:
            continue
        # 排除退市股
        if "退" in name:
            continue
        # 排除北交所（8开头）
        if symbol.startswith("8"):
            continue

        filtered.append(stock)

    if verbose:
        print(f"  [股票列表] 过滤后剩余 {len(filtered)} 只")
    set_mem_cache(cache_key, filtered)
    return filtered