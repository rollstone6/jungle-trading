"""Martin Luk 回调买法 — 15分钟图回测 (原版口径: 日线定背景, 分钟图择时)

架构(贴合笔记原文):
  背景: 用【前一交易日收盘】的日线数据判断筛选条件(低点抬高/波动收窄/站上EMA),
        当日全天有效, 无未来函数
  支撑区: 日线AVWAP/21EMA/50EMA/前高, 当日盘中15分钟低点回踩±2%
  触发: 15分钟阳线收盘突破前一根15分钟K线高点
  离场: 15分钟级别止损(回踩低点-1.5*ATR15, 上限3%风险), 1R保本, 3R止盈,
        连续4根收盘破15分EMA21趋势止损, 冷却12根K线

数据限制: 新浪15分钟上限1023根 ≈ 64个交易日(约3个月)。
T+1说明: A股当日买入不能当日卖出。提供两种模式:
  T0=1(默认): 假设持有底仓做T(你的5只都是实际持仓), 允许当日止损/止盈
  T0=0: 严格T+1, 当日信号最快次日开盘离场(止损跳空按次日开盘计)

运行: /opt/hermes-agent/.venv/bin/python scripts/martin_pullback_15m.py [T0]
"""
import asyncio
import sys

sys.path.insert(0, "/root/workspace/jungle-trading")
from app.services.market import fetch_kline_sina_minute, fetch_kline_tencent  # noqa: E402

POOL = {
    "300291": "百纳千成",
    "300394": "天孚通信",
    "300505": "川金诺",
    "300251": "光线传媒",
    "513310": "中韩芯片",
    "600580": "卧龙电驱",
    "603678": "火炬电子",
    "000592": "平潭发展",
}

CHASE_MAX = 1.02      # 入场不得超支撑区上沿2%
BE_R, TP_R = 1.0, 3.0 # 保本/止盈R倍数
COOLDOWN = 12         # 亏损出场后冷却K线数(=3小时)
EMA21_BREAK = 4       # 连续N根收盘破15分EMA21趋势止损
TOUCH_PCT = 0.02      # 回踩判定±2%
RISK_MAX = 0.03       # 单笔风险上限3%
COST = 0.0008         # 单边佣金+滑点


def ema(vals, n):
    out, k, e = [], 2 / (n + 1), vals[0]
    for v in vals:
        e = k * v + (1 - k) * e
        out.append(e)
    return out


def atr_series(highs, lows, closes, n=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    out, a = [None] * len(closes), sum(trs[:n]) / n
    out[n-1] = a
    for i in range(n, len(closes)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


def swing_lows(lows, lb=3, rb=3):
    return [i for i in range(lb, len(lows) - rb) if lows[i] == min(lows[i-lb:i+rb+1])]


def daily_context(dk):
    """对每个交易日, 用其【之前】的日线数据算筛选条件与支撑区. 返回 {date: dict|None}"""
    closes = [k["close"] for k in dk]; highs = [k["high"] for k in dk]
    lows = [k["low"] for k in dk]; vols = [k["volume"] for k in dk]
    dates = [k["date"] for k in dk]
    n = len(closes)
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    atrp_ = [None if a is None else a / c * 100 for a, c in zip(atr_series(highs, lows, closes), closes)]
    sl = swing_lows(lows)
    ctx = {}
    for d in range(60, n):
        i = d - 1  # 只用截至昨收的数据
        px, ok = closes[i], True
        ok &= px > e9[i] and px > e21[i] and e21[i] >= e21[i - 3]
        recent = [j for j in sl if i - 90 <= j <= i - 4]
        ok &= len(recent) >= 2 and lows[recent[-1]] > lows[recent[-2]]
        ok &= atrp_[i] is not None and atrp_[i - 10] is not None and atrp_[i] < atrp_[i - 10]
        if not ok:
            ctx[dates[d]] = None
            continue
        # AVWAP锚定: 最近一个20~150根前的阶段高点
        anchor = None
        for j in range(i - 20, max(0, i - 150), -1):
            if highs[j] == max(highs[max(0, j - 150):j + 1]) and all(highs[j] >= highs[k] for k in range(max(0, j-5), min(n, j+6))):
                anchor = j
                break
        levels = []
        if anchor is not None:
            pv = v = 0.0
            av = None
            for j in range(anchor, i + 1):
                tp = (highs[j] + lows[j] + closes[j]) / 3
                pv += tp * vols[j]; v += vols[j]
                av = pv / v
            levels.append(av)
            levels.append(highs[anchor])          # 前高
        levels += [e21[i], e50[i]]
        levels = [x for x in levels if x and x > 0]
        ctx[dates[d]] = {"levels": sorted(levels)}
    return ctx


def run_15m(code, name, mk, dk, t0=True):
    if len(mk) < 200:
        return []
    closes = [k["close"] for k in mk]; highs = [k["high"] for k in mk]
    lows = [k["low"] for k in mk]; dates = [k["date"] for k in mk]
    n = len(closes)
    e21m = ema(closes, 21)
    atrm = atr_series(highs, lows, closes, 14)
    dctx = daily_context(dk)

    # 每日bar索引
    day_of = [d[:10] for d in dates]
    first_bar_of_day = {}
    for i, dd in enumerate(day_of):
        first_bar_of_day.setdefault(dd, i)

    trades, pos, last_loss_i, below = [], None, -999, 0
    for i in range(60, n):
        dd, px = day_of[i], closes[i]

        if pos:
            exit_px, why = None, None
            same_day_allowed = t0 or dd != pos["bday"]
            if lows[i] <= pos["stop"] and (same_day_allowed or i == first_bar_of_day[dd]):
                exit_px, why = (pos["stop"], "保本") if pos["stop"] >= pos["entry"] else (pos["stop"], "止损")
                exit_px = pos["stop"]
            elif same_day_allowed:
                below = below + 1 if px < e21m[i] else 0
                if below >= EMA21_BREAK:
                    exit_px, why = px, f"连{EMA21_BREAK}根破EMA21"
                elif px >= pos["t3"]:
                    exit_px, why = pos["t3"], "止盈3R"
            # 严格T+1: 当日不允许离场, 次日起正常判断(止损跳空按止损价近似)
            if exit_px is not None:
                net = (exit_px / pos["entry"] - 1) * 100 - COST * 200
                trades.append({"name": name, "buy": f"{pos['bday']} {dates[pos['bi']][11:16]}",
                               "entry": pos["entry"],
                               "sell": f"{dd} {dates[i][11:16]}", "exit": exit_px,
                               "ret": net, "r": (exit_px - pos["entry"]) / pos["risk"],
                               "why": why, "bars": i - pos["bi"]})
                if net < 0:
                    last_loss_i = i
                pos, below = None, 0
                continue
            if px >= pos["entry"] + BE_R * pos["risk"]:
                pos["stop"] = max(pos["stop"], pos["entry"] * 1.001)
            pos["aged"] = True
            continue

        ctx = dctx.get(dd)
        if not ctx or i - last_loss_i < COOLDOWN:
            continue
        lv = ctx["levels"]
        # 回踩: 近12根(3小时)15分低点进入支撑带
        zone_lo, zone_hi = min(lv) * (1 - TOUCH_PCT), max(lv) * (1 + TOUCH_PCT)
        touched = any(lows[j] <= max(lv) * 1.01 and lows[j] >= zone_lo for j in range(i - 12, i))
        if not touched:
            continue
        # 触发: 阳线收盘破前一根高点
        if not (closes[i] > closes[i-1] and highs[i] > highs[i-1]):
            continue
        entry = px
        if entry > zone_hi * CHASE_MAX or entry < zone_lo:
            continue
        risk_frac = min(RISK_MAX, max(1.5 * (atrm[i] or entry * 0.01) / entry, 0.02))
        stop = entry * (1 - risk_frac)
        risk = entry - stop
        pos = {"entry": entry, "bi": i, "bday": dd, "stop": stop, "risk": risk,
               "t3": entry + TP_R * risk, "aged": False}

    return trades


def opens_high(mk, i):
    return mk[i]["open"]


def stats(trades):
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"] / 100
        peak = max(peak, eq); mdd = max(mdd, (peak - eq) / peak)
    return {"n": len(trades), "wr": len(wins) / len(rets) * 100,
            "avg": sum(rets) / len(rets), "tot": (eq - 1) * 100,
            "mdd": mdd * 100, "best": max(rets), "worst": min(rets),
            "avg_bars": sum(t["bars"] for t in trades) / len(trades)}


def resample(bars, factor):
    """把60分钟bar合并成120分钟(每2根一组, 按上午/下午场分组: 10:30+11:30, 14:00+15:00)"""
    out = []
    i = 0
    while i + 1 < len(bars):
        a, b = bars[i], bars[i + 1]
        da, db = a["date"][:10], b["date"][:10]
        ha, hb = a["date"][11:16], b["date"][11:16]
        # 只合并同日的60分对 (10:30&11:30 或 14:00&15:00)
        if da == db and ((ha, hb) == ("10:30", "11:30") or (ha, hb) == ("14:00", "15:00")):
            out.append({"date": b["date"], "open": a["open"], "high": max(a["high"], b["high"]),
                        "low": min(a["low"], b["low"]), "close": b["close"],
                        "volume": a["volume"] + b["volume"]})
            i += 2
        else:
            i += 1
    if i < len(bars):
        out.append(bars[-1])  # 孤儿bar保留(回测影响极小)
    return out


async def main():
    t0 = "--t1" not in sys.argv
    scale = 15
    for a in sys.argv[1:]:
        if a.isdigit():
            scale = int(a)
    print(f"周期: {scale}分钟 | 模式: {'底仓做T(T+0)' if t0 else '严格T+1'}")
    print(f"{'股票':<6}{'笔数':>5}{'胜率%':>7}{'均益%':>7}{'累计%':>8}{'回撤%':>7}{'最好%':>7}{'最差%':>7}{'均持K线':>7}")
    print("-" * 66)
    all_t, per = [], {}
    for code, name in POOL.items():
        if scale == 120:
            raw = await fetch_kline_sina_minute(code, scale=60, datalen=1023)
            mk = resample(raw, 2)
        elif scale in (15, 30, 60):
            mk = await fetch_kline_sina_minute(code, scale=scale, datalen=1023)
        else:
            print(f"{name:<6}  不支持周期{scale}")
            continue
        dk = await fetch_kline_tencent(code, days=500, full_date=True)
        if not mk or not dk:
            print(f"{name:<6}  数据缺失")
            continue
        trades = run_15m(code, name, mk, dk, t0=t0)
        all_t.extend(trades)
        st = stats(trades)
        per[name] = st
        if st:
            print(f"{name:<6}{st['n']:>5}{st['wr']:>7.1f}{st['avg']:>7.2f}{st['tot']:>8.2f}"
                  f"{st['mdd']:>7.1f}{st['best']:>7.2f}{st['worst']:>7.2f}{st['avg_bars']:>7.0f}")
        else:
            print(f"{name:<6}    0      -      -       -      -      -      -     -")

    if all_t:
        rets = [t["ret"] for t in all_t]
        wins = [r for r in rets if r > 0]
        print("-" * 66)
        print(f"合计 {len(all_t)} 笔 | 胜率 {len(wins)/len(rets)*100:.1f}% | 平均 {sum(rets)/len(rets):+.2f}% | "
              f"最好 {max(rets):+.2f}% / 最差 {min(rets):+.2f}%")
        print("\n全部交易明细:")
        for t in sorted(all_t, key=lambda x: x["buy"]):
            print(f"  {t['name']:<5} {t['buy']}买{t['entry']:.2f} → {t['sell']}卖{t['exit']:.2f} "
                  f"{t['ret']:+6.2f}% ({t['r']:+.1f}R) [{t['why']}] {t['bars']}根")


if __name__ == "__main__":
    asyncio.run(main())
