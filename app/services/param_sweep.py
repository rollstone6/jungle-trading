"""均线策略参数扫描服务（2026-11-16）。

对股票池逐票跑 MA fast×slow 参数网格回测（复用 backtest.run_backtest，
与策略K线/回测页同口径），聚合出：
- 参数热力矩阵（跨股票平均收益/胜率/回撤）
- 综合最优参数（收益与回撤平衡的评分）
- 个股最优参数明细

范围：held=当前持仓 / pool=持仓+量化池 / market=全市场成交额Top300粗筛。
后台任务走 quant_jobs（singleflight + 当日缓存 + 并发闸），3.4G 小机安全：
取数并发 4、逐票回测串行、每股取数后立即释放 K 线引用。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from app.services import quant_scan as qs

#: 参数网格：fast × slow（fast < slow），12 组合
FAST_PERIODS = [3, 5, 10, 20]
SLOW_PERIODS = [10, 20, 30, 60]
GRID = [(f, s) for f in FAST_PERIODS for s in SLOW_PERIODS if f < s]

#: 每股取数根数（约1.5年，与回测页日线口径一致）
KLINE_DAYS = 550
#: 取数并发（小内存机安全线，与 quant_scan MAX_WORKERS 一致）
FETCH_CONCURRENCY = 4


def _resolve_scope(scope: str) -> tuple[list[dict], str]:
    """解析扫描范围，返回 (股票列表, 实际scope)。market 粗筛失败降级 pool。"""
    from app.services.portfolio import get_all_positions

    held = [{"symbol": p["code"], "name": p["name"]} for p in get_all_positions()]
    if scope == "held":
        return held, "held"
    seen = {s["symbol"] for s in held}
    pool = list(held)
    for s in qs._pool():
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            pool.append(s)
    if scope == "market":
        cands = qs._market_candidates()
        if cands:
            mseen = set()
            merged = []
            for s in pool + cands:
                if s["symbol"] in mseen:
                    continue
                mseen.add(s["symbol"])
                merged.append(s)
            return merged, "market"
    return pool, "pool"


async def _fetch_one(code: str) -> list[dict]:
    """单票取数：腾讯日线(前复权) → 新浪日线兜底。每源硬超时12s，失败返回空。

    全市场300票时新浪兜底可能限流挂起，asyncio.wait_for 保证单票不拖死整批。
    """
    from app.services.market import fetch_kline_tencent, fetch_kline_sina_minute

    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            code = code[len(p):]
            break
    try:
        klines = await asyncio.wait_for(
            fetch_kline_tencent(code, days=KLINE_DAYS, full_date=True), timeout=12)
    except Exception:
        klines = []
    if len(klines) < 60:
        try:
            klines = await asyncio.wait_for(
                fetch_kline_sina_minute(code, scale=240, datalen=KLINE_DAYS), timeout=12)
        except Exception:
            klines = []
    return klines


def _score(avg_return: float, avg_dd: float, win_rate: float) -> float:
    """综合评分：收益为主、回撤惩罚、胜率微调。越高越好。"""
    return avg_return - 0.5 * avg_dd + 0.1 * (win_rate - 50)


def _preconvert(klines: list[dict]):
    """每股一次性把 K 线转成 backtrader DataFrame（日期解析只做1次，
    而非每参数组合重复解析——300股×12组合下这是主要耗时）。"""
    from app.services.backtest import kline_to_dataframe
    return kline_to_dataframe(klines)


def _run_bt_df(df, f: int, sl: int) -> dict:
    """复用已转换 DataFrame 跑单组参数（绕过 run_backtest 的重复转换）。"""
    import backtrader as bt
    from app.services.backtest import MAStrategy, ChartRecorder

    cerebro = bt.Cerebro()
    cerebro.adddata(bt.feeds.PandasData(dataname=df))
    cerebro.addstrategy(MAStrategy, fast_period=f, slow_period=sl)
    cerebro.broker.setcash(100000.0)
    cerebro.broker.setcommission(commission=0.001)
    # P1-7（审计 t_edf48869）：同 backtest.py——SharpeRatio 显式 timeframe=Days + annualize=True
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe',
                        timeframe=bt.TimeFrame.Days, annualize=True)
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')
    cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
    cerebro.addanalyzer(ChartRecorder, _name='chart')
    start_value = cerebro.broker.getvalue()
    strat = cerebro.run()[0]
    final_value = cerebro.broker.getvalue()
    total_return = (final_value - start_value) / start_value * 100
    dd = strat.analyzers.drawdown.get_analysis()
    ta = strat.analyzers.trades.get_analysis()
    total_trades = ta.get('total', {}).get('closed', 0) or 0
    won = ta.get('won', {}).get('total', 0) or 0
    win_rate = (won / total_trades * 100) if total_trades else 0.0
    return {
        "total_return": round(total_return, 2),
        "max_drawdown": round(dd.get('max', {}).get('drawdown', 0) or 0, 2),
        "win_rate": round(win_rate, 1),
        "total_trades": total_trades,
    }


def compute_sweep(scope: str = "pool") -> dict:
    """同步计算入口（quant_jobs 在 to_thread 里跑）。"""
    started = time.time()
    stocks, actual_scope = _resolve_scope(scope)
    # 分批取数+回测（每批50票）：避免300票K线同时驻留内存撑爆3.4G主机。
    # 信号量在每批的协程内创建（asyncio.run 每批新建事件循环，
    # 跨循环复用 Semaphore 会报 "bound to a different event loop"）。
    BATCH = 50

    async def fetch_batch(batch):
        sem = asyncio.Semaphore(FETCH_CONCURRENCY)

        async def guarded(s):
            async with sem:
                try:
                    return s, await _fetch_one(s["symbol"])
                except Exception:
                    return s, []

        return await asyncio.gather(*[guarded(s) for s in batch])

    per_stock = []
    agg = {(f, s): {"ret": [], "dd": [], "win": [], "trades": 0} for f, s in GRID}
    skipped = []
    import gc
    for bi in range(0, len(stocks), BATCH):
        batch = stocks[bi:bi + BATCH]
        fetched = asyncio.run(fetch_batch(batch))
        gc.collect()
        for s, klines in fetched:
            if len(klines) < 60:
                skipped.append({"code": s["symbol"], "name": s["name"], "reason": "K线不足"})
                continue
            try:
                df = _preconvert(klines)
            except Exception:
                skipped.append({"code": s["symbol"], "name": s["name"], "reason": "数据转换失败"})
                continue
            rows = []
            for f, sl in GRID:
                try:
                    r = _run_bt_df(df, f, sl)
                except Exception:
                    continue
                ret = float(r["total_return"])
                dd = float(r["max_drawdown"])
                win = float(r["win_rate"])
                nt = int(r["total_trades"])
                rows.append({"fast": f, "slow": sl, "return": ret,
                             "dd": dd, "win": win, "trades": nt})
                a = agg[(f, sl)]
                a["ret"].append(ret)
                a["dd"].append(dd)
                a["win"].append(win)
                a["trades"] += nt
            del df  # 逐票释放，防内存累积
            if not rows:
                skipped.append({"code": s["symbol"], "name": s["name"], "reason": "回测失败"})
                continue
            best = max(rows, key=lambda x: _score(x["return"], x["dd"], x["win"]))
            cur = next((x for x in rows if x["fast"] == 5 and x["slow"] == 20), None)
            per_stock.append({
                "code": s["symbol"], "name": s["name"],
                "bars": len(klines),
                "start": klines[0]["date"], "end": klines[-1]["date"],
                "best": best, "current": cur, "rows": rows,
            })
        del fetched
        gc.collect()

    # 聚合矩阵
    matrix = []
    for (f, sl), a in agg.items():
        n = len(a["ret"])
        if not n:
            continue
        avg_ret = sum(a["ret"]) / n
        avg_dd = sum(a["dd"]) / n
        avg_win = sum(a["win"]) / n
        pos_ratio = sum(1 for x in a["ret"] if x > 0) / n * 100
        matrix.append({
            "fast": f, "slow": sl, "stocks": n,
            "avg_return": round(avg_ret, 2),
            "avg_dd": round(avg_dd, 2),
            "avg_win": round(avg_win, 1),
            "pos_ratio": round(pos_ratio, 1),
            "total_trades": a["trades"],
            "score": round(_score(avg_ret, avg_dd, avg_win), 2),
        })
    best_global = max(matrix, key=lambda x: x["score"]) if matrix else None
    current_global = next((x for x in matrix if x["fast"] == 5 and x["slow"] == 20), None)
    # 个股最优分布统计
    best_dist: dict[str, int] = {}
    for ps in per_stock:
        k = f'{ps["best"]["fast"]}/{ps["best"]["slow"]}'
        best_dist[k] = best_dist.get(k, 0) + 1

    return {
        "asOf": datetime.now().strftime("%Y-%m-%d"),
        "scope": actual_scope,
        "requested_scope": scope,
        "grid": {"fast": FAST_PERIODS, "slow": SLOW_PERIODS},
        "stock_count": len(per_stock),
        "skipped": skipped,
        "matrix": sorted(matrix, key=lambda x: (x["fast"], x["slow"])),
        "best_global": best_global,
        "current_global": current_global,
        "best_distribution": dict(sorted(best_dist.items(), key=lambda kv: -kv[1])),
        "stocks": sorted(per_stock, key=lambda x: -_score(x["best"]["return"], x["best"]["dd"], x["best"]["win"])),
        "elapsed_sec": round(time.time() - started, 1),
        "note": "MA均线策略参数扫描，与回测页/策略K线同口径（金叉买死叉卖，次日开盘成交，千一手续费）。评分=平均收益-0.5×平均回撤+0.1×(胜率-50)。",
    }


def compute_sweep_cached(scope: str = "pool") -> dict:
    """quant_jobs 入口：计算后落当日磁盘缓存（重启不重扫）。"""
    payload = compute_sweep(scope)
    qs._save_cache(f"sweep_ma_{scope}", payload)
    return payload