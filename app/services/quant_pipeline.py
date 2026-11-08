"""量化管线闭环原型 — DuckDB(存储/日志) + VectorBT(回测) + APScheduler(定时)

数据流：行情源(百度→腾讯→新浪级联) → DuckDB kline_daily 落库
        → VectorBT 双均线组合回测 → 指标/权益曲线/成交记录
        → DuckDB bt_runs 落库 + data/pipeline_result.json 供前端

设计约束（3.4Gi 内存主机）：单标的 ≤550 根日线，池只有 8 只，全程 pandas
DataFrame 极小，无并发下载，内存峰值可忽略。
"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import vectorbt as vbt

from app.services.market import (
    fetch_kline_baidu, fetch_kline_tencent, fetch_kline_sina_minute,
)
from app.quant.config import STOCK_LIST

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_PATH = BASE_DIR / "data" / "jungle.duckdb"
RESULT_PATH = BASE_DIR / "data" / "pipeline_result.json"

# 回测默认参数（前 fast 均线 > 慢均线持有，否则空仓；A股单边费用近似）
DEFAULT_FAST = 10
DEFAULT_SLOW = 30
FEES = 0.0005
SLIPPAGE = 0.001
KLINE_DAYS = 550


def _conn() -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS kline_daily (
            code      VARCHAR NOT NULL,
            date      DATE    NOT NULL,
            open      DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE,
            volume    DOUBLE,
            source    VARCHAR,
            updated_at TIMESTAMP DEFAULT current_timestamp,
            PRIMARY KEY (code, date)
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS bt_runs (
            run_id     INTEGER PRIMARY KEY,
            ts         TIMESTAMP DEFAULT current_timestamp,
            strategy   VARCHAR,
            params     VARCHAR,
            universe   VARCHAR,
            ok_count   INTEGER,
            metrics    VARCHAR,
            note       VARCHAR
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS job_log (
            ts     TIMESTAMP DEFAULT current_timestamp,
            task   VARCHAR,
            status VARCHAR,
            detail VARCHAR
        )""")
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_bt_runs START 1")
    return con


def log_job(task: str, status: str, detail: str = "") -> None:
    con = _conn()
    try:
        con.execute("INSERT INTO job_log (task, status, detail) VALUES (?,?,?)",
                    (task, status, detail[:500]))
    finally:
        con.close()


# ---------------------------------------------------------------- ingestion

def _repair_mmdd(bars: list[dict]) -> list[dict]:
    """百度返回 MM-DD 日期，跨年错序。以末根≈今天为锚，向前倒推年份。

    从最后一根开始往前走：MM 变大（如 12-30 跟在 01-02 前面 = 回退越过了新年界）
    即 year-1。550 根可能跨 3 个自然年，此法均正确。
    """
    if not bars:
        return bars
    now = datetime.now()
    year, month = now.year, now.month
    out = []
    for b in reversed(bars):
        d = str(b.get("date", ""))
        if len(d) == 5:  # MM-DD
            mm = int(d[:2])
            if mm > month:  # 向前回退跨过了一年边界
                year -= 1
            month = mm
            b = dict(b)
            b["date"] = f"{year:04d}-{d}"
        out.append(b)
    return list(reversed(out))


async def fetch_kline_cascade(code: str, days: int = KLINE_DAYS) -> tuple[list[dict], str]:
    """日K级联（全部输出 YYYY-MM-DD）：腾讯full_date → 百度(MM-DD修复) → 新浪240。"""
    try:
        bars = await fetch_kline_tencent(code, days=days, full_date=True)
    except Exception:
        bars = []
    if len(bars) >= 30:
        return bars, "tencent"
    try:
        bars = _repair_mmdd(await fetch_kline_baidu(code, days=days))
    except Exception:
        bars = []
    if len(bars) >= 30:
        return bars, "baidu"
    try:
        bars = await fetch_kline_sina_minute(code, scale=240, datalen=days)
        for b in bars:
            b["date"] = str(b.get("date", ""))[:10]
    except Exception:
        bars = []
    return bars, "sina240"


async def ingest(universe: list[dict]) -> dict:
    """拉行情写入 DuckDB，返回每只的行数与源。"""
    con = _conn()
    stats = {}
    try:
        for s in universe:
            code = s["symbol"]
            bars, source = await fetch_kline_cascade(code)
            rows = []
            for b in bars:
                d = str(b.get("date", ""))[:10]
                if len(d) != 10 or not b.get("close"):
                    continue
                rows.append((code, d, b.get("open", 0), b.get("high", 0),
                             b.get("low", 0), b.get("close", 0),
                             b.get("volume", 0), source))
            if rows:
                df = pd.DataFrame(rows, columns=[
                    "code", "date", "open", "high", "low", "close", "volume", "source"])
                # 去重后 upsert（同 code+date 覆盖）
                df = df.drop_duplicates(subset=["code", "date"], keep="last")
                con.register("tmp_k", df)
                con.execute("""
                    INSERT OR REPLACE INTO kline_daily
                    SELECT code, date::DATE, open, high, low, close, volume, source,
                           current_timestamp FROM tmp_k""")
                con.unregister("tmp_k")
            stats[code] = {"bars": len(rows), "source": source}
            await asyncio.sleep(0.3)  # 接口限速保护
    finally:
        con.close()
    return stats


def load_kline_df(code: str) -> pd.DataFrame | None:
    con = _conn()
    try:
        df = con.execute(
            "SELECT date, open, high, low, close, volume FROM kline_daily "
            "WHERE code=? ORDER BY date", [code]).fetchdf()
    finally:
        con.close()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


# ---------------------------------------------------------------- backtest

def _f(x, nd=4):
    """安全序列化：nan/inf/None → None，numpy → float"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd)


def run_vectorbt_backtest(code: str, name: str, df: pd.DataFrame,
                          fast: int = DEFAULT_FAST, slow: int = DEFAULT_SLOW) -> dict | None:
    """单标的双均线回测。返回指标 + 权益曲线 + 成交记录（前端 Lightweight Charts 用）。"""
    if df is None or len(df) < slow + 10:
        return None
    close = df["close"]
    ma_fast = close.rolling(fast).mean()
    ma_slow = close.rolling(slow).mean()
    entries = (ma_fast > ma_slow) & ma_slow.notna()
    exits = (ma_fast < ma_slow) & ma_slow.notna()

    pf = vbt.Portfolio.from_signals(
        close, entries=entries, exits=exits,
        fees=FEES, slippage=SLIPPAGE, freq="1D")

    trades_df = pf.trades.records_readable
    trades = []
    for _, r in trades_df.iterrows():
        trades.append({
            "entry_date": str(r["Entry Timestamp"])[:10],
            "exit_date": str(r["Exit Timestamp"])[:10],
            "entry_price": _f(r["Avg Entry Price"], 3),
            "exit_price": _f(r["Avg Exit Price"], 3),
            "pnl": _f(r["PnL"], 2),
            "return_pct": _f(float(r["Return"]) * 100, 2),
            "status": str(r["Status"]),
        })

    equity = pf.value()
    eq_series = [{"t": str(idx)[:10], "v": _f(v, 2)} for idx, v in equity.items()]

    n_open = sum(1 for t in trades if t["status"] == "Open")
    win_rate = _f(pf.trades.win_rate() * 100, 1) if len(trades_df) else None
    buy_hold = _f((close.iloc[-1] / close.iloc[0] - 1) * 100, 2)

    return {
        "code": code,
        "name": name,
        "bars": int(len(df)),
        "candles": [{"t": str(idx)[:10], "o": _f(o, 3), "h": _f(h, 3),
                     "l": _f(l, 3), "c": _f(c, 3)}
                    for idx, o, h, l, c in df[["open", "high", "low", "close"]].itertuples()],
        "range": [str(df.index[0])[:10], str(df.index[-1])[:10]],
        "metrics": {
            "total_return": _f(pf.total_return() * 100, 2),
            "annual_return": _f(pf.annualized_return() * 100, 2),
            "max_drawdown": _f(pf.max_drawdown() * 100, 2),
            "sharpe": _f(pf.sharpe_ratio(), 2),
            "sortino": _f(pf.sortino_ratio(), 2),
            "calmar": _f(pf.calmar_ratio(), 2),
            "win_rate": win_rate,
            "trade_count": int(len(trades_df)),
            "open_count": int(n_open),
            "buy_hold_return": buy_hold,
        },
        "equity": eq_series,
        "trades": trades,
    }


def persist_run(strategy: str, params: dict, universe_size: int,
                results: list[dict], note: str = "") -> int:
    con = _conn()
    try:
        run_id = con.execute("SELECT nextval('seq_bt_runs')").fetchone()[0]
        summary = [{r["code"]: r["metrics"]["total_return"]} for r in results]
        con.execute(
            "INSERT INTO bt_runs (run_id, strategy, params, universe, ok_count, metrics, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(run_id), strategy, json.dumps(params), universe_size, len(results),
             json.dumps(summary), note[:300]))
    finally:
        con.close()
    return int(run_id)


# ---------------------------------------------------------------- orchestrator

async def run_pipeline(fast: int = DEFAULT_FAST, slow: int = DEFAULT_SLOW,
                       trigger: str = "manual") -> dict:
    """完整闭环：ingest → backtest → 落库 + 写结果 JSON。返回结果 dict。"""
    universe = [{"symbol": s["symbol"], "name": s["name"]} for s in STOCK_LIST]
    log_job("pipeline", "start", f"{trigger} fast={fast} slow={slow}")
    try:
        stats = await ingest(universe)
        results, failed = [], []
        for s in universe:
            df = load_kline_df(s["symbol"])
            r = run_vectorbt_backtest(s["symbol"], s["name"], df, fast, slow)
            if r:
                r["source"] = stats.get(s["symbol"], {}).get("source", "")
                r["ingest_bars"] = stats.get(s["symbol"], {}).get("bars", 0)
                results.append(r)
            else:
                failed.append(s["symbol"])
        run_id = persist_run("ma_cross", {"fast": fast, "slow": slow,
                                          "fees": FEES, "slippage": SLIPPAGE},
                             len(universe), results,
                             note=f"trigger={trigger} failed={','.join(failed) or '-'}")
        payload = {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "trigger": trigger,
            "run_id": run_id,
            "params": {"fast": fast, "slow": slow, "fees": FEES, "slippage": SLIPPAGE},
            "stocks": results,
            "failed": failed,
        }
        RESULT_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log_job("pipeline", "ok", f"run_id={run_id} ok={len(results)} fail={len(failed)}")
        return payload
    except Exception as exc:
        log_job("pipeline", "error", f"{type(exc).__name__}: {exc}")
        raise


def load_result() -> dict | None:
    if RESULT_PATH.exists():
        try:
            return json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def db_overview() -> dict:
    con = _conn()
    try:
        kline_rows = con.execute("SELECT COUNT(*) FROM kline_daily").fetchone()[0]
        per_code = con.execute(
            "SELECT code, COUNT(*), MIN(date), MAX(date), MAX(source) "
            "FROM kline_daily GROUP BY code ORDER BY code").fetchall()
        runs = con.execute(
            "SELECT run_id, ts, strategy, params, ok_count, note FROM bt_runs "
            "ORDER BY run_id DESC LIMIT 8").fetchall()
        jobs = con.execute(
            "SELECT ts, task, status, detail FROM job_log ORDER BY ts DESC LIMIT 12").fetchall()
    finally:
        con.close()
    size_kb = round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0
    return {
        "kline_rows": int(kline_rows),
        "db_size_kb": size_kb,
        "per_code": [{"code": c, "bars": int(n), "first": str(f)[:10],
                      "last": str(l)[:10], "source": s}
                     for c, n, f, l, s in per_code],
        "runs": [{"run_id": int(r[0]), "ts": str(r[1])[:19], "strategy": r[2],
                  "params": r[3], "ok_count": int(r[4]), "note": r[5]} for r in runs],
        "jobs": [{"ts": str(j[0])[:19], "task": j[1], "status": j[2], "detail": j[3]}
                 for j in jobs],
    }
