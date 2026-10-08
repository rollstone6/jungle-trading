"""股票池管理模块。

提供全量A股列表获取和过滤功能。
"""

from __future__ import annotations

from data.cache import get_mem_cache, set_mem_cache
from data.providers import fetch_all_stocks, fetch_with_retry


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