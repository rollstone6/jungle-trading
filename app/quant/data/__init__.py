"""数据层模块。

提供行情数据获取、缓存管理、股票池管理等功能。
"""

from app.quant.data.cache import (
    clear_cache,
    clear_disk_cache,
    get_mem_cache,
    load_disk_cache,
    save_disk_cache,
    set_mem_cache,
)
from app.quant.data.fetcher import (
    load_copper_futures,
    load_fund_flow,
    load_margin_trading,
    load_real_data,
    parallel_fetch_dispatch,
    parallel_fetch_simple,
)
from app.quant.data.universe import (
    load_all_stocks,
    load_all_stocks_filtered,
)

__all__ = [
    # 缓存
    "clear_cache",
    "clear_disk_cache",
    "get_mem_cache",
    "load_disk_cache",
    "save_disk_cache",
    "set_mem_cache",
    # 数据获取
    "load_copper_futures",
    "load_fund_flow",
    "load_margin_trading",
    "load_real_data",
    "parallel_fetch_dispatch",
    "parallel_fetch_simple",
    # 股票池
    "load_all_stocks",
    "load_all_stocks_filtered",
]