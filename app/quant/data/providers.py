"""数据源模块。

多数据源级联获取：Tushare / 东方财富 / 新浪财经 / 腾讯财经。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import akshare as ak
import pandas as pd
import requests
import tushare as ts

#: py_mini_racer（akshare 的 JS 解码依赖）的 V8 引擎池不支持并发初始化：
#: 多线程同时首次调用会直接 FATAL 崩溃整个进程。
#: 所有 akshare 调用统一经此锁串行化（网络 IO 本身也是串行瓶颈，影响有限）。
import threading
_AK_LOCK = threading.Lock()

# 代理绕过配置
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""

# 先加载仓库根目录的 .env，再读 TUSHARE_TOKEN；
# .env 中显式设置的代理环境变量也会覆盖上面的清空操作。
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[3] / ".env")
except ImportError:
    pass  # python-dotenv 未安装时退回纯环境变量方式

# Tushare 初始化
TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "")
if TUSHARE_TOKEN:
    ts.set_token(TUSHARE_TOKEN)
    ts_pro = ts.pro_api()
else:
    ts_pro = None

# 时间框架映射表
_TIMEFRAME_MAP = {
    "1d": {"eastmoney": "daily", "tushare": "daily", "tencent": "day"},
    "60m": {"eastmoney": "60", "tushare": "60min", "tencent": "min60"},
    "30m": {"eastmoney": "30", "tushare": "30min", "tencent": "min30"},
    "120m": {"eastmoney": "120", "tushare": "120min", "tencent": "min120"},
}


def fetch_with_retry(
    func,
    max_retries: int = 3,
    delay: int = 2,
    source_name: str = "未知源",
    verbose: bool = True,
):
    """通用重试装饰器，指数退避。"""
    for attempt in range(1, max_retries + 1):
        try:
            return func()
        except Exception as e:
            if verbose:
                wait_time = delay * (2 ** (attempt - 1))
                err_msg = str(e)[:80]
                print(f"  [{source_name}] 第{attempt}次失败: {err_msg}...")
                if attempt < max_retries:
                    print(f"     {wait_time}秒后重试...")
                    time.sleep(wait_time)
                else:
                    print(f"     [{source_name}] 已达最大重试次数，放弃。")
            else:
                wait_time = delay * (2 ** (attempt - 1))
                if attempt < max_retries:
                    time.sleep(wait_time)
    return None


def _get_tf_config(timeframe: str) -> dict:
    """获取时间框架对应的数据源配置。"""
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"不支持的时间框架: {timeframe}，可选值: {list(_TIMEFRAME_MAP.keys())}"
        )
    return _TIMEFRAME_MAP[timeframe]


def fetch_tushare(symbol: str, start_date: str, timeframe: str = "1d") -> pd.DataFrame:
    """数据源0: Tushare Pro（优先级最高）。"""
    tf_cfg = _get_tf_config(timeframe)

    if symbol.startswith("6"):
        ts_code = f"{symbol}.SH"
    else:
        ts_code = f"{symbol}.SZ"

    try:
        if timeframe == "1d":
            df = ts_pro.daily(ts_code=ts_code, start_date=start_date)
        else:
            df = ts_pro.stk_mins(
                ts_code=ts_code,
                freq=tf_cfg["tushare"],
                start_date=start_date,
            )
        if df is None or df.empty:
            raise ValueError("Tushare 返回数据为空")
        df = df.rename(
            columns={
                "trade_date": "date",
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
                "vol": "volume",
            }
        )
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").reset_index(drop=True)
        return df[["date", "open", "high", "low", "close", "volume"]]
    except Exception as e:
        raise RuntimeError(f"Tushare 获取失败: {str(e)[:50]}")


def fetch_eastmoney(symbol: str, start_date: str, timeframe: str = "1d") -> pd.DataFrame:
    """数据源1: 东方财富（AKShare）。

    日线走 stock_zh_a_hist；分钟数据在新版 akshare 中迁移到
    stock_zh_a_hist_min_em（period 支持 1/5/15/30/60）。
    """
    tf_cfg = _get_tf_config(timeframe)
    if timeframe == "1d":
        with _AK_LOCK:
            df = ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                start_date=start_date,
                adjust="qfq",
            )
    else:
        start_fmt = (
            f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]} 09:30:00"
        )
        with _AK_LOCK:
            df = ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date=start_fmt,
                period=tf_cfg["eastmoney"],
                adjust="qfq",
            )
    df = df.rename(
        columns={
            "日期": "date",
            "时间": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_sina(symbol: str, start_date: str, timeframe: str = "1d") -> pd.DataFrame:
    """数据源2: 新浪财经（AKShare stock_zh_a_daily）。仅支持日线。"""
    if timeframe != "1d":
        raise ValueError("新浪财经仅支持日线数据")

    if symbol.startswith("6"):
        full_symbol = f"sh{symbol}"
    else:
        full_symbol = f"sz{symbol}"

    with _AK_LOCK:
        df = ak.stock_zh_a_daily(symbol=full_symbol, adjust="qfq")
    df = df.rename(
        columns={
            "date": "date",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "volume",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= pd.to_datetime(start_date)]
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_sina_minute(symbol: str, start_date: str = "20240101",
                      timeframe: str = "60m") -> pd.DataFrame:
    """数据源2.5: 新浪财经分钟K线（60/30/15分钟，单次约1023根）。

    60分钟约覆盖13个月。腾讯 fqkline 分钟参数已失效（param error），
    分钟数据主力走新浪源，与本站走势分类扫描同一实现。
    """
    scale_map = {"60m": 60, "30m": 30, "15m": 15}
    scale = scale_map.get(timeframe)
    if scale is None:
        raise ValueError(f"新浪财经分钟仅支持 60m/30m/15m，收到: {timeframe}")

    prefix = "sh" if symbol.startswith("6") else "sz"
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
        f"?symbol={prefix}{symbol}&scale={scale}&ma=no&datalen=1023"
    )
    resp = requests.get(
        url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15
    )
    resp.raise_for_status()
    m = re.search(r"\((\[.*\])\)", resp.text, re.S)
    if not m:
        raise ValueError("新浪分钟接口返回为空")
    items = json.loads(m.group(1))
    if not items:
        raise ValueError("新浪分钟接口返回为空")

    records = [
        {
            "date": item.get("day", ""),
            "open": float(item.get("open", 0)),
            "high": float(item.get("high", 0)),
            "low": float(item.get("low", 0)),
            "close": float(item.get("close", 0)),
            "volume": float(item.get("volume", 0)),
        }
        for item in items
    ]
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= pd.to_datetime(start_date)]
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_tencent(symbol: str, start_date: str, timeframe: str = "1d") -> pd.DataFrame:
    """数据源3: 腾讯财经（直接 HTTP 请求）。"""
    tf_cfg = _get_tf_config(timeframe)

    if symbol.startswith("6") or symbol.startswith("5"):
        market = "sh"
    else:
        market = "sz"

    period = tf_cfg["tencent"]
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {
        "param": f"{market}{symbol},{period},,{start_date},,320,qfq",
        "_var": f"kline_{period}qfq",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://web.sqt.gtimg.cn/",
    }

    resp = requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()

    text = resp.text
    if "=" in text:
        text = text.split("=", 1)[1]

    data = json.loads(text)
    kline_data = data.get("data", {}).get(f"{market}{symbol}", {})
    day_data = kline_data.get("qfqday", kline_data.get("day", []))

    if not day_data:
        raise ValueError("腾讯接口返回数据为空")

    records = []
    for item in day_data:
        records.append(
            {
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]) if len(item) > 5 else 0,
            }
        )

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] >= pd.to_datetime(start_date)]
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_copper_futures(start_date: str = "20240101") -> pd.DataFrame:
    """获取沪铜期货日线数据。"""
    with _AK_LOCK:
        df = ak.futures_main_sina(symbol="CU0", start_date=start_date)
    df = df.rename(
        columns={
            "日期": "date",
            "开盘价": "open",
            "收盘价": "close",
            "最高价": "high",
            "最低价": "low",
            "成交量": "volume",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "open", "high", "low", "close", "volume"]]


def fetch_fund_flow(symbol: str = "600183") -> pd.DataFrame:
    """获取个股主力资金流向数据。"""
    market = "sh" if symbol.startswith("6") else "sz"
    with _AK_LOCK:
        df = ak.stock_individual_fund_flow(stock=symbol, market=market)
    df = df.rename(
        columns={
            "日期": "date",
            "主力净流入-净额": "net_main",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "net_main"]]


def fetch_margin_trading(symbol: str = "600183") -> pd.DataFrame:
    """获取个股融资融券数据。"""
    from datetime import datetime

    end_date = datetime.now()

    with _AK_LOCK:
        if symbol.startswith("6"):
            df = ak.stock_margin_detail_sse(date=end_date.strftime("%Y%m%d"))
            code_col = "标的证券代码"
        else:
            df = ak.stock_margin_detail_szse(date=end_date.strftime("%Y%m%d"))
            code_col = "证券代码"

    df = df[df[code_col] == symbol]
    df = df.rename(
        columns={
            "日期": "date",
            "融资余额": "balance",
        }
    )
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df[["date", "balance"]]


def fetch_all_stocks() -> list[dict]:
    """获取全量A股股票列表。"""
    with _AK_LOCK:
        df = ak.stock_info_a_code_name()
    df = df.rename(
        columns={
            "code": "symbol",
            "name": "name",
        }
    )
    return df[["symbol", "name"]].to_dict("records")