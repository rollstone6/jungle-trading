"""大盘情绪·智能点金（2026-11-16，复刻同花顺大盘分析界面，规则透明可回测）。

数据源（真实接口，已验证）：
- 上证指数日线：akshare stock_zh_index_daily(symbol="sh000001")（新浪，3年+）
- 大盘资金流：akshare stock_market_fund_flow()（东财，约120个交易日）

指标定义（全部公开规则，非黑盒）：
- 阳谱/阴谱：近20日上涨日占比的平滑（EMA5），阴谱=100-阳谱。
  反映市场赚钱效应的持续性，而非单日涨跌。
- 机构力道：主力净流入净占比（超大单+大单）的3日EMA，正=吸筹红柱、负=抛压绿柱。
  柱上标记：吸=力道由负转正；减=由正转负；弹=深跌后力道连续2日回升。
- 趋势线：收盘 vs MA20，上方=红（多头）、下方=绿（空头）。
- 金手指：趋势线转红 且 阳谱上穿50 且 机构力道>0（三重确认，事件去重）。
  银手指：趋势线转绿 且 阳谱下穿50 且 机构力道<0。
- 信号日志：金手指→【低吸】大盘机会；银手指→【减仓】大盘风险；
  趋势红+力道>0但阳谱<50→【反弹】（谨慎参考）。描述统一为
  「触发规则+历史统计+温和参考」结构，不含操作指令（口径详见 note）。
- 回测统计：近3年每次金/银手指后20个交易日的指数涨跌（真实计算，
  上涨概率/平均幅度/最大/最小），与界面"近三年大数据回测"同形式。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pandas as pd

logger = logging.getLogger(__name__)

HORIZON = 20          # 信号后评估窗口（交易日）
SPECTRUM_WIN = 20     # 阳谱统计窗口
SMOOTH = 5            # EMA 平滑
YEARS = 3


def _ema(vals: list[float], span: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (span + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _signal_text(kind: str, bt: dict, dual: str | None = None) -> str:
    """金/银手指信号文案：触发规则（透明口径）+ 真实回测统计 + 温和参考（非指令）。

    bt 为该方向 eval_finger 结果；count=0（样本不足）时退化为不含统计句，
    绝不渲染 0%/NaN。dual 非空表示该日力道未计入，值为降级原因短语（与 note
    口径一致）。历史统计非预测，不构成投资建议（免责统一在 note）。
    """
    gold = kind == "gold"
    if dual is not None:
        rule = (f"趋势线转{'红' if gold else '绿'}且阳谱{'≥50' if gold else '≤50'}，"
                f"双重确认触发（{dual}）。")
    elif gold:
        rule = "趋势线转红、阳谱≥45、机构力道为正，三重确认触发。"
    else:
        rule = "趋势线转绿、阳谱≤55、机构力道转负，三重确认触发。"
    side, prob_key = ("上涨", "up_prob") if gold else ("下跌", "down_prob")
    ref = "多头窗口或开启，可关注低吸机会。" if gold else "注意回撤风险，参考控制仓位。"
    stat = ""
    if bt.get("count"):
        stat = (f"近{YEARS}年同类信号后{HORIZON}个交易日{side}概率"
                f"{bt[prob_key]:g}%（{bt['count']}次样本）。")
    return rule + stat + ref


def compute_sentiment() -> dict:
    import akshare as ak

    df = ak.stock_zh_index_daily(symbol="sh000001")
    df["date"] = pd.to_datetime(df["date"])
    cutoff = datetime.now() - timedelta(days=YEARS * 365 + 40)
    df = df[df["date"] >= cutoff].reset_index(drop=True)
    if len(df) < 120:
        raise RuntimeError("上证指数日线数据不足")

    close = df["close"].astype(float).tolist()
    opens = df["open"].astype(float).tolist()
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
    n = len(close)

    # 趋势线 MA20
    ma20 = [None] * n
    for i in range(19, n):
        ma20[i] = sum(close[i - 19:i + 1]) / 20
    trend_red = [ma20[i] is not None and close[i] >= ma20[i] for i in range(n)]

    # 阳谱：近20日上涨占比 → EMA5 平滑
    raw_spec = [50.0] * n
    for i in range(1, n):
        lo = max(1, i - SPECTRUM_WIN + 1)
        ups = sum(1 for j in range(lo, i + 1) if close[j] > close[j - 1])
        raw_spec[i] = ups / (i - lo + 1) * 100
    yang = [round(v, 2) for v in _ema(raw_spec, SMOOTH)]
    yin = [round(100 - v, 2) for v in yang]

    # 机构力道：主力净占比 3日EMA（东财大盘资金流，本机可能被封锁→降级）
    force = [None] * n
    force_source = "none"
    try:
        mf = ak.stock_market_fund_flow()
        mf_map = {}
        bad_rows = 0
        for _, r in mf.iterrows():
            d = str(r["日期"])[:10]
            try:
                mf_map[d] = float(r["主力净流入-净占比"])
            except (TypeError, ValueError):
                # 脏行跳过（逐行不 log 防刷屏）；全表皆脏时下面聚合告警
                bad_rows += 1
        if bad_rows and not mf_map:
            logger.warning("东财大盘资金流 %d 行全部解析失败，机构力道留空", bad_rows)
        idx_dates = dates
        vals, start_i = [], None
        for i, d in enumerate(idx_dates):
            if d in mf_map:
                if start_i is None:
                    start_i = i
                vals.append(mf_map[d])
            elif start_i is not None:
                vals.append(vals[-1] if vals else 0.0)  # 停牌日沿用
        if vals:
            sm = _ema(vals, 3)
            for k, i in enumerate(range(start_i, n)):
                force[i] = round(sm[k], 2)
            force_source = "eastmoney"
    except Exception as exc:
        logger.warning("东财大盘资金流拉取失败，机构力道留空(前端降级): %s", exc)  # 资金流失败不阻塞

    # 柱标记：吸/减/弹
    marks = [None] * n
    for i in range(1, n):
        f0, f1 = force[i - 1], force[i]
        if f0 is None or f1 is None:
            continue
        if f0 <= 0 < f1:
            marks[i] = "吸"
        elif f0 >= 0 > f1:
            marks[i] = "减"
        elif i >= 2 and force[i - 2] is not None and f1 < 0 and f1 > f0 > force[i - 2] and f0 < -2:
            marks[i] = "弹"

    # 金手指/银手指（三重确认 + 事件去重：同向连续只记首次）
    fingers = [None] * n   # 'gold' / 'silver'
    last_fire = None
    for i in range(1, n):
        if ma20[i] is None or ma20[i - 1] is None:
            continue
        if force[i] is None:
            # 资金流覆盖之前的历史段：退化为趋势翻转+阳谱同侧双重确认（口径在 note 中说明）
            gold = trend_red[i] and not trend_red[i - 1] and yang[i] >= 50
            silver = not trend_red[i] and trend_red[i - 1] and yang[i] <= 50
        else:
            gold = (trend_red[i] and not trend_red[i - 1]
                    and yang[i] >= 50 > yang[i - 1] and force[i] > 0)
            silver = (not trend_red[i] and trend_red[i - 1]
                      and yang[i] <= 50 < yang[i - 1] and force[i] < 0)
            # 放宽：趋势翻转当日阳谱未必恰好穿50，用"趋势翻转+力道同向+阳谱同侧"也算
            if not gold and trend_red[i] and not trend_red[i - 1] and force[i] > 0 and yang[i] >= 45:
                gold = True
            if not silver and not trend_red[i] and trend_red[i - 1] and force[i] < 0 and yang[i] <= 55:
                silver = True
        if gold and last_fire != "gold":
            fingers[i] = "gold"; last_fire = "gold"
        elif silver and last_fire != "silver":
            fingers[i] = "silver"; last_fire = "silver"

    # 近三年回测：信号后 HORIZON 交易日涨跌（先于信号文案计算，供统计句引用）
    def eval_finger(kind):
        rets = []
        for i in range(n):
            if fingers[i] != kind or i + HORIZON >= n:
                continue
            r = (close[i + HORIZON] - close[i]) / close[i] * 100
            rets.append(round(r, 2))
        if not rets:
            return {"count": 0}
        ups = [r for r in rets if r > 0]
        downs = [r for r in rets if r < 0]
        return {
            "count": len(rets),
            "up_prob": round(len(ups) / len(rets) * 100, 2),
            "down_prob": round(len(downs) / len(rets) * 100, 2),
            "avg_up": round(sum(ups) / len(ups), 2) if ups else 0,
            "avg_down": round(sum(downs) / len(downs), 2) if downs else 0,
            "max_up": max(rets), "min_up": min(ups) if ups else 0,
            "max_down": min(rets), "min_down": max(downs) if downs else 0,
        }

    bt_gold = eval_finger("gold")
    bt_silver = eval_finger("silver")

    # 信号日志（含反弹类）：文案=触发规则+真实回测统计+温和参考，非操作指令
    dual_reason = "力道源暂不可达" if force_source == "none" else "资金流覆盖前"
    signals = []
    last_rebound = -10
    for i in range(1, n):
        if fingers[i] == "gold":
            signals.append({"date": dates[i], "type": "opportunity", "tag": "低吸",
                            "finger": "金手指",
                            "text": _signal_text("gold", bt_gold,
                                                 dual=None if force[i] is not None else dual_reason)})
        elif fingers[i] == "silver":
            signals.append({"date": dates[i], "type": "risk", "tag": "减仓",
                            "finger": "银手指",
                            "text": _signal_text("silver", bt_silver,
                                                 dual=None if force[i] is not None else dual_reason)})
        elif (trend_red[i] and force[i] is not None and force[i] > 0
              and yang[i] < 50 and i - last_rebound >= 5 and ma20[i] is not None):
            signals.append({"date": dates[i], "type": "opportunity", "tag": "反弹",
                            "finger": None,
                            "text": "趋势线在红区、机构力道吸筹，但阳谱仍低于50，赚钱效应未确认，反弹信号仅供参考，宜待阳谱站上50再评估。"})
            last_rebound = i
    signals.reverse()  # 新→旧

    # 图表窗口：最近约120个交易日（与资金流覆盖对齐）
    win_start = max(0, n - 120)
    series = []
    for i in range(win_start, n):
        series.append({
            "date": dates[i],
            "open": round(opens[i], 2), "high": round(highs[i], 2),
            "low": round(lows[i], 2), "close": round(close[i], 2),
            "ma20": round(ma20[i], 2) if ma20[i] is not None else None,
            "trendRed": trend_red[i],
            "yang": yang[i], "yin": yin[i],
            "force": force[i], "mark": marks[i],
            "finger": fingers[i],
        })

    latest = series[-1]
    return {
        "asOf": dates[-1],
        "index": "上证指数",
        "horizon": HORIZON,
        "latest": latest,
        "series": series,
        "table": series[-14:],   # 智能点金表：最近14日
        "signals": signals[:60],
        "forceSource": force_source,
        "backtest": {"gold": bt_gold, "silver": bt_silver,
                     "years": YEARS,
                     "sample_start": dates[0], "sample_end": dates[-1]},
        "note": ("规则透明：阳谱=20日上涨占比EMA5；机构力道=主力净占比3日EMA；金手指=趋势转红+阳谱≥45+吸筹，银手指反之。"
                 + ("当前大盘资金流源不可达，力道暂缺，金/银手指按「趋势翻转+阳谱穿50」双重确认（全历史口径）。" if force_source == "none"
                    else "资金流覆盖前（约半年以前）退化为趋势翻转+阳谱穿50双重确认。")
                 + "信号描述中的历史统计=近3年同类信号后20个交易日真实涨跌，非预测，不构成投资建议。"),
    }


def compute_sentiment_cached() -> dict:
    from app.services import quant_scan as qs
    payload = compute_sentiment()
    qs._save_cache("market_sentiment", payload)
    return payload