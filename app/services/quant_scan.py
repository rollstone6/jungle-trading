"""量化信号中心服务。

把 app/quant 模块的四大能力包装成 Web 可直接消费的 JSON 扫描结果：

- 策略信号扫描：突破回踩 / 均值回归 / 主力吸筹（引擎来自 STRATEGY_REGISTRY）
- 趋势过滤：强趋势 / 宽松 / 超卖 三种模式（TrendFilter）
- 箱体检测：箱体参数、价格位置、质量评分（BoxDetector）
- 筹码分布：池扫描评分排名 + 单股分布明细（ChipDistributionAnalyzer）

缓存策略：当日有效，落盘 data/quant_scan_*.json；refresh=True 强制重扫。
与网站共用同一份 STOCK 股票池（去重后约 30 只），行情走 load_real_data
的内存 + 磁盘双级缓存，重复扫描很快。
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from app.quant.config import STOCK_LIST, StrategyConfig
from app.quant.data import load_real_data
from app.quant.strategies import STRATEGY_REGISTRY
from app.quant.utils import BoxDetector, ChipDistributionAnalyzer, TrendFilter

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

#: 吸筹策略引擎需要 60 分钟K线，其余策略用日线（与 CLI / 回测入口一致）
_TIMEFRAME_BY_STRATEGY = {"accumulation": "60m"}

#: 信号只保留最近 N 个自然日（全历史信号对网页没有意义）
SIGNAL_LOOKBACK_DAYS = 30

MAX_WORKERS = 8


# ----------------------------------------------------------------------
# 工具函数
# ----------------------------------------------------------------------

def _pool() -> list[dict]:
    """统一股票池（去重，配置里 688017 出现两次）。"""
    seen, pool = set(), []
    for s in STOCK_LIST:
        if s["symbol"] in seen:
            continue
        seen.add(s["symbol"])
        pool.append(s)
    return pool


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _native(obj):
    """递归把 numpy 类型转成 Python 原生类型（FastAPI/JSON 序列化要求）。"""
    if isinstance(obj, dict):
        return {k: _native(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_native(v) for v in obj]
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _load_cache(key: str) -> dict | None:
    file = DATA_DIR / f"quant_scan_{key}.json"
    if not file.exists():
        return None
    try:
        cached = json.loads(file.read_text(encoding="utf-8"))
        if cached.get("asOf") == _today():
            return cached
    except Exception:
        pass
    return None


def _save_cache(key: str, payload: dict) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / f"quant_scan_{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # 落盘失败不影响返回


def _fetch_daily(symbol: str) -> pd.DataFrame:
    return load_real_data(symbol=symbol, start_date="20240101", verbose=False)


# ----------------------------------------------------------------------
# 1. 策略信号扫描
# ----------------------------------------------------------------------

def scan_signals(strategy: str, refresh: bool = False) -> dict:
    """扫描指定策略在全池的近期信号。

    返回:
        dict: asOf / strategy / strategyName / signals[] / failures[] / total
    """
    engine_cls = STRATEGY_REGISTRY.get(strategy)
    if engine_cls is None:
        raise ValueError(
            f"未知策略: {strategy}，可选: {list(STRATEGY_REGISTRY.keys())}"
        )

    cache_key = f"signals_{strategy}"
    if not refresh:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    cfg = StrategyConfig(signal_timeframe="1d")
    engine = engine_cls(cfg)
    timeframe = _TIMEFRAME_BY_STRATEGY.get(strategy, "1d")
    pool = _pool()
    names = {s["symbol"]: s["name"] for s in pool}
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=SIGNAL_LOOKBACK_DAYS)

    rows: list[dict] = []
    failures: list[dict] = []

    def work(stock: dict):
        symbol = stock["symbol"]
        df = load_real_data(
            symbol=symbol, start_date="20240101",
            timeframe=timeframe, verbose=False,
        )
        if df is None or df.empty:
            return symbol, None
        return symbol, engine.scan_signals(df.copy(), symbol=symbol, debug=False)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(work, s): s for s in pool}
        for future in as_completed(futures):
            stock = futures[future]
            symbol = stock["symbol"]
            try:
                _, frame = future.result()
            except Exception as exc:
                failures.append({"symbol": symbol, "reason": str(exc)})
                continue
            if frame is None or frame.is_empty():
                continue
            recent = frame.df[frame.df["date"] >= cutoff]
            for _, row in recent.iterrows():
                meta = {
                    k: (None if pd.isna(v) else v)
                    for k, v in row.to_dict().items()
                    if k not in ("date", "symbol", "close", "strategy")
                }
                rows.append({
                    "date": str(row["date"])[:10],
                    "symbol": symbol,
                    "name": names.get(symbol, ""),
                    "close": round(float(row["close"]), 2),
                    "metadata": meta,
                })

    rows.sort(key=lambda r: r["date"], reverse=True)
    payload = _native({
        "asOf": _today(),
        "fetchedAt": _now(),
        "strategy": strategy,
        "strategyName": engine.name,
        "lookbackDays": SIGNAL_LOOKBACK_DAYS,
        "signals": rows,
        "failures": failures,
        "total": len(rows),
    })
    _save_cache(cache_key, payload)
    return payload


def list_strategies() -> list[dict]:
    """列出 Web 端可扫描的策略（含中文名）。"""
    cfg = StrategyConfig()
    result = []
    for key, cls in STRATEGY_REGISTRY.items():
        result.append({
            "key": key,
            "name": cls(cfg).name,
            "timeframe": _TIMEFRAME_BY_STRATEGY.get(key, "1d"),
        })
    return result


# ----------------------------------------------------------------------
# 2. 趋势过滤
# ----------------------------------------------------------------------

_TREND_MODES = {"strong", "loose", "oversold"}


def scan_trend(mode: str = "strong", refresh: bool = False) -> dict:
    """全池趋势过滤，返回每只股票的逐项判定。"""
    if mode not in _TREND_MODES:
        raise ValueError(f"未知模式: {mode}，可选: {sorted(_TREND_MODES)}")

    cache_key = f"trend_{mode}"
    if not refresh:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    tf = TrendFilter(StrategyConfig())
    pool = _pool()

    def work(stock: dict):
        df = _fetch_daily(stock["symbol"])
        if df is None or df.empty:
            return stock, None
        return stock, tf.filter_stock(df, mode=mode)

    rows: list[dict] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(work, s): s for s in pool}
        for future in as_completed(futures):
            stock, result = future.result()
            if result is None:
                failures.append(stock["symbol"])
                continue
            rows.append({
                "symbol": stock["symbol"],
                "name": stock["name"],
                "pass": result["pass"],
                "ma_bullish": result["ma_bullish"],
                "ma20_up": result["ma20_up"],
                "near_high": result["near_high"],
                "vol_ok": result["vol_ok"],
                "detail": result["detail"],
                "reasons": result["reasons"],
            })

    rows.sort(key=lambda r: (not r["pass"], r["symbol"]))
    passed = [r for r in rows if r["pass"]]
    payload = _native({
        "asOf": _today(),
        "fetchedAt": _now(),
        "mode": mode,
        "stocks": rows,
        "passed": [{"symbol": r["symbol"], "name": r["name"]} for r in passed],
        "passedCount": len(passed),
        "failures": failures,
    })
    _save_cache(cache_key, payload)
    return payload


# ----------------------------------------------------------------------
# 3. 箱体检测
# ----------------------------------------------------------------------

def scan_box(refresh: bool = False) -> dict:
    """全池箱体检测，返回处于箱体的股票及箱体参数。"""
    cache_key = "box"
    if not refresh:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    detector = BoxDetector(StrategyConfig(signal_timeframe="1d"))
    pool = _pool()

    def work(stock: dict):
        df = _fetch_daily(stock["symbol"])
        if df is None or df.empty:
            return None
        info = detector.detect_box(df)
        if not info.get("has_box"):
            return None
        info["symbol"] = stock["symbol"]
        info["name"] = stock["name"]
        return info

    box_stocks: list[dict] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(work, s): s for s in pool}
        for future in as_completed(futures):
            stock = futures[future]
            try:
                info = future.result()
            except Exception:
                failures.append(stock["symbol"])
                continue
            if info:
                box_stocks.append(info)

    box_stocks.sort(key=lambda r: r["quality_score"], reverse=True)
    payload = _native({
        "asOf": _today(),
        "fetchedAt": _now(),
        "boxBars": detector.box_bars,
        "stocks": box_stocks,
        "total": len(box_stocks),
        "failures": failures,
    })
    _save_cache(cache_key, payload)
    return payload


# ----------------------------------------------------------------------
# 4. 筹码分布
# ----------------------------------------------------------------------

def scan_chip(refresh: bool = False) -> dict:
    """全池筹码集中度扫描，按评分降序。"""
    cache_key = "chip"
    if not refresh:
        cached = _load_cache(cache_key)
        if cached:
            return cached

    analyzer = ChipDistributionAnalyzer(StrategyConfig())
    results = analyzer.scan_stocks(_pool(), max_workers=MAX_WORKERS,
                                   print_detail=False)
    payload = _native({
        "asOf": _today(),
        "fetchedAt": _now(),
        "stocks": results,
        "passedCount": sum(1 for r in results if r["metrics"]["pass"]),
    })
    _save_cache(cache_key, payload)
    return payload


def chip_detail(symbol: str) -> dict:
    """单股筹码分布明细（用于网页画图）：分布曲线 + 指标 + 近期收盘。"""
    df = _fetch_daily(symbol)
    if df is None or df.empty:
        raise ValueError(f"无法获取 {symbol} 的行情数据")

    analyzer = ChipDistributionAnalyzer(StrategyConfig())
    prices, dist = analyzer.calc_chip_distribution(df)
    if prices is None:
        raise ValueError(f"{symbol} 筹码分布计算失败（数据不足）")

    metrics = analyzer.calc_concentration(prices, dist) or {}
    metrics["pass"] = analyzer._check_filter(metrics)

    # 分布曲线降采样到 ~120 个点，网页画图足够且 payload 小
    step = max(1, len(prices) // 120)
    sampled = [
        {"price": round(float(prices[i]), 2),
         "ratio": round(float(dist[i]) * 100, 4)}
        for i in range(0, len(prices), step)
    ]

    recent = df.tail(60)[["date", "close"]]
    kline = [
        {"date": str(r["date"])[:10], "close": round(float(r["close"]), 2)}
        for _, r in recent.iterrows()
    ]

    return _native({
        "asOf": _today(),
        "symbol": symbol,
        "close": round(float(df.iloc[-1]["close"]), 2),
        "metrics": metrics,
        "distribution": sampled,
        "recentClose": kline,
    })
