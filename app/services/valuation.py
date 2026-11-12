"""指数估值均值回归 — 数据服务 + 策略复刻回测

数据：乐咕乐股 PE 序列（akshare stock_index_pe_lg，月频快照，2005 至今）
    + 东财指数日线收盘（index_zh_a_hist），PE 前向填充到日频。
指标：当前 PE、滚动 10 年均值、溢价 %、历史分位、理论均值回归回撤 = 均值/当前 - 1。
策略复刻：PE 高于滚动均值 X% → 减仓；溢价回落到 Y% 以内 → 再进入。对比一直持有。
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# akshare 支持的指数（乐咕 PE 序列）
PE_INDEXES = {
    "沪深300": "000300",
    "上证50": "000016",
    "中证500": "000905",
    "中证1000": "000852",
    "创业板50": "399673",
    "上证红利": "000015",
}


def _fetch_pe_monthly(symbol: str) -> pd.DataFrame:
    """乐咕 PE-TTM 序列：date / pe"""
    import akshare as ak
    df = ak.stock_index_pe_lg(symbol=symbol)
    out = df[["日期", "滚动市盈率"]].rename(columns={"日期": "date", "滚动市盈率": "pe"})
    out["date"] = pd.to_datetime(out["date"])
    out = out.dropna(subset=["pe"]).sort_values("date").reset_index(drop=True)
    return out


def _fetch_index_daily(code: str) -> pd.DataFrame:
    """东财指数日线：date / close"""
    import akshare as ak
    df = ak.index_zh_a_hist(symbol=code, period="daily",
                            start_date="20050101",
                            end_date=datetime.now().strftime("%Y%m%d"))
    out = df[["日期", "收盘"]].rename(columns={"日期": "date", "收盘": "close"})
    out["date"] = pd.to_datetime(out["date"])
    return out.sort_values("date").reset_index(drop=True)


def build_daily(symbol: str) -> pd.DataFrame:
    """在 to_thread 中调用（纯同步）。
    合成日频表：date / close / pe / roll_mean / premium / percentile"""
    pe_m = _fetch_pe_monthly(symbol)
    code = PE_INDEXES.get(symbol, "000300")
    px = _fetch_index_daily(code)
    df = px.copy()
    # PE 序列前向填充到日频
    df = pd.merge_asof(df, pe_m.sort_values("date"), on="date", direction="backward")
    df = df.dropna(subset=["pe", "close"]).reset_index(drop=True)
    # 滚动 10 年（约 2440 交易日）PE 均值
    df["roll_mean"] = df["pe"].rolling(2440, min_periods=600).mean()
    # 当前 PE 在全部历史中的分位
    df["percentile"] = df["pe"].expanding(min_periods=120).apply(
        lambda x: float((x <= x.iloc[-1]).mean()))
    df["premium"] = df["pe"] / df["roll_mean"] - 1.0
    return df


def snapshot(df: pd.DataFrame) -> dict:
    """最新一行的均值回归体检"""
    last = df.dropna(subset=["roll_mean"]).iloc[-1]
    cur_pe = float(last["pe"])
    mean_pe = float(last["roll_mean"])
    reversion_dd = mean_pe / cur_pe - 1.0 if cur_pe > 0 else 0.0
    return {
        "date": last["date"].strftime("%Y-%m-%d"),
        "close": round(float(last["close"]), 2),
        "pe": round(cur_pe, 2),
        "pe_10y_mean": round(mean_pe, 2),
        "premium_pct": round(float(last["premium"]) * 100, 1),
        "percentile": round(float(last["percentile"]) * 100, 1),
        "reversion_drawdown_pct": round(reversion_dd * 100, 1),
    }


def backtest_reversion(df: pd.DataFrame, exit_premium: float = 0.5,
                       reenter_premium: float = 0.1, exit_ratio: float = 1.0,
                       fee: float = 0.001) -> dict:
    """复刻估值均值回归纪律：
    PE 高于滚动均值 exit_premium(默认+50%) → 减仓 exit_ratio（默认清仓）；
    溢价回落到 +reenter_premium(默认+10%) 以内 → 再进入。
    信号日收盘决策，次一交易日生效（避免未来函数）。
    """
    d = df.dropna(subset=["roll_mean"]).reset_index(drop=True)
    if d.empty:
        return {"error": "无可用数据"}
    close = d["close"].to_numpy(dtype=float)
    prem = d["premium"].to_numpy(dtype=float)
    n = len(d)

    pos = np.ones(n)          # 目标仓位（当日收盘决策）
    state = 1.0
    events = []
    for i in range(n):
        if state >= 1.0 and prem[i] > exit_premium:
            state = 1.0 - exit_ratio
            events.append({"date": d["date"].iloc[i].strftime("%Y-%m-%d"),
                           "act": "减仓", "pe": round(float(d["pe"].iloc[i]), 2),
                           "premium": round(float(prem[i]) * 100, 1)})
        elif state < 1.0 and prem[i] <= reenter_premium:
            state = 1.0
            events.append({"date": d["date"].iloc[i].strftime("%Y-%m-%d"),
                           "act": "再进入", "pe": round(float(d["pe"].iloc[i]), 2),
                           "premium": round(float(prem[i]) * 100, 1)})
        pos[i] = state

    # 次日生效：shift(1)
    eff = np.concatenate([[pos[0]], pos[:-1]])
    ret = np.concatenate([[0.0], np.diff(close) / close[:-1]])
    strat_ret = eff[1:] * ret[1:]
    # 调仓费用（仓位变动日按变动比例计费，diff 后长度 n-1 与收益序列天然对齐）
    turnover = np.abs(np.diff(eff))
    strat_ret = strat_ret - turnover * fee

    def perf(r):
        nav = np.cumprod(1 + r)
        dd = float((nav / np.maximum.accumulate(nav) - 1).min())
        yrs = len(r) / 244
        cagr = float(nav[-1] ** (1 / yrs) - 1) if nav[-1] > 0 and yrs > 0 else 0.0
        sharpe = float(np.mean(r) / np.std(r) * np.sqrt(244)) if np.std(r) > 0 else 0.0
        return {"total_return_pct": round(float(nav[-1] - 1) * 100, 1),
                "cagr_pct": round(cagr * 100, 2),
                "max_drawdown_pct": round(dd * 100, 1),
                "sharpe": round(sharpe, 2)}

    hold_ret = ret[1:]
    s = perf(strat_ret)
    h = perf(hold_ret)
    time_in_market = float((eff[1:] > 0.5).mean())
    return {
        "params": {"exit_premium_pct": round(exit_premium * 100),
                   "reenter_premium_pct": round(reenter_premium * 100),
                   "exit_ratio_pct": round(exit_ratio * 100)},
        "period": f"{d['date'].iloc[0]:%Y-%m-%d} ~ {d['date'].iloc[-1]:%Y-%m-%d}",
        "strategy": s, "buy_hold": h,
        "time_in_market_pct": round(time_in_market * 100, 1),
        "events": events[-30:], "event_count": len(events),
    }


CACHE_DIR = Path(__file__).resolve().parents[2] / "data"
CACHE_TTL_HOURS = 6


def _cache_path(symbol: str) -> Path:
    safe = symbol.replace("/", "_")
    return CACHE_DIR / f"valuation_{safe}.pkl"


def _load_df_cached(symbol: str) -> pd.DataFrame:
    """6 小时内用磁盘缓存的日频合成表，避免反复打 akshare"""
    cp = _cache_path(symbol)
    if cp.exists() and (datetime.now().timestamp() - cp.stat().st_mtime) < CACHE_TTL_HOURS * 3600:
        return pd.read_pickle(cp)
    df = build_daily(symbol)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_pickle(cp)
    return df


async def get_valuation(symbol: str, exit_premium: float = 0.5,
                        reenter_premium: float = 0.1, exit_ratio: float = 1.0) -> dict:
    df = await asyncio.to_thread(_load_df_cached, symbol)
    snap = snapshot(df)
    bt = await asyncio.to_thread(backtest_reversion, df,
                                 exit_premium, reenter_premium, exit_ratio)
    # 全历史 PE/滚动均值（画图用，降采样到周）
    hist = df.dropna(subset=["roll_mean"]).copy()
    hist = hist.set_index("date").resample("W").last().dropna(subset=["pe"])
    chart = [{"date": t.strftime("%Y-%m-%d"), "pe": round(float(r["pe"]), 2),
              "mean": round(float(r["roll_mean"]), 2)} for t, r in hist.iterrows()]
    # 事件日期映射到 PE 序列（图上标记）
    marks = []
    for e in bt["events"]:
        marks.append({"date": e["date"], "act": e["act"], "pe": e["pe"],
                      "premium": e["premium"]})
    bt["event_marks"] = marks
    return {"symbol": symbol, "snapshot": snap, "backtest": bt,
            "chart": chart, "indexes": list(PE_INDEXES.keys()),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
