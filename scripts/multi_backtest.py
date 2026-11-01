"""多策略并行回测 — 多个策略在同一股票池上同时执行, 同口径横向对比

用法:
  python multi_backtest.py                      # 全部策略 x 默认股票池
  python multi_backtest.py ema_cross turtle     # 只跑指定策略
  BT_DAYS=500 python multi_backtest.py          # 改K线长度

纪律: 入场=信号日收盘; T+1(买入日不卖, 最早次日出); 只用 j<=i 已确认数据(无未来函数);
双边成本 买0.05%/卖0.10%(含印花税); 每策略每标的单仓位; 期末强制结算.
结果: 控制台对比表 + reports/multi_backtest_<date>.json 全量明细.
"""
import asyncio
import json
import os
import statistics
import sys
from datetime import date

sys.path.insert(0, "/root/workspace/jungle-trading")
from app.services.market import fetch_kline_tencent  # noqa: E402

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

COST_BUY, COST_SELL = 0.0005, 0.0010
DAYS = int(os.environ.get("BT_DAYS", 800))
START_BAR = 60  # 指标预热期

# ---------------- 指标工具 (与 martin_pullback_backtest.py 同源) ----------------

def ema(vals, n):
    out, e, k = [], vals[0], 2 / (n + 1)
    for v in vals:
        e = k * v + (1 - k) * e
        out.append(e)
    return out


def atr_pct(highs, lows, closes, n=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]),
                       abs(lows[i] - closes[i-1])))
    out = [None] * len(closes)
    a = sum(trs[:n]) / n
    out[n-1] = a / closes[n-1] * 100
    for i in range(n, len(closes)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a / closes[i] * 100
    return out


def swing_lows(lows, lb=3, rb=3):
    return [i for i in range(lb, len(lows) - rb) if lows[i] == min(lows[i-lb:i+rb+1])]


def swing_highs(highs, lb=5, rb=5):
    return [i for i in range(lb, len(highs) - rb) if highs[i] == max(highs[i-lb:i+rb+1])]


def avwap_series(highs, lows, closes, vols, anchor):
    out, pv, v = [None] * len(closes), 0.0, 0.0
    for i in range(anchor, len(closes)):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        pv += tp * vols[i]
        v += vols[i]
        out[i] = pv / v if v > 0 else closes[i]
    return out


def rsi_wilder(closes, n=14):
    out = [None] * len(closes)
    gains = losses = 0.0
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        g, l = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            gains += g; losses += l
            if i == n:
                ag, al = gains / n, losses / n
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def boll(closes, n=20, k=2.0):
    mid = up = lo = None
    mids, ups, los = [None] * len(closes), [None] * len(closes), [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i-n+1:i+1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        mids[i], ups[i], los[i] = m, m + k * sd, m - k * sd
    return mids, ups, los


def net_ret(entry, exit_px):
    """净收益率%(含双边成本)"""
    return ((exit_px * (1 - COST_SELL)) / (entry * (1 + COST_BUY)) - 1) * 100


def finish(trades, pos, i, px, why, dates, risk):
    ret = net_ret(pos["entry"], px)
    trades.append({"buy_date": dates[pos["i"]], "buy": round(pos["entry"], 3),
                   "sell_date": dates[i], "sell": round(px, 3),
                   "ret": ret, "r": (px - pos["entry"]) / risk if risk else 0.0,
                   "why": why, "days": i - pos["i"]})


# ---------------- 策略1: Martin 回调买法 (原版, 默认参数) ----------------

def strat_martin(code, name, kl):
    closes = [k["close"] for k in kl]; highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]; opens = [k["open"] for k in kl]
    vols = [k["volume"] for k in kl]; dates = [k["date"] for k in kl]
    n = len(closes)
    if n < 150:
        return []
    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    atrp = atr_pct(highs, lows, closes)
    sl, sh = swing_lows(lows), swing_highs(highs)
    trades, pos, below_e21, last_loss_i = [], None, 0, -999
    CONFIRM, BE_R, CHASE, COOLDOWN = 2, 1.0, 1.02, 5

    for i in range(START_BAR, n):
        px = closes[i]
        if pos:
            exit_px, why = None, None
            if lows[i] <= pos["stop"]:
                exit_px, why = pos["stop"], "保本" if pos["stop"] >= pos["entry"] else "止损"
            else:
                below_e21 = below_e21 + 1 if px < e21[i] else 0
                if below_e21 >= CONFIRM:
                    exit_px, why = px, f"连{CONFIRM}日破21EMA"
                elif px >= pos["t3"]:
                    exit_px, why = pos["t3"], "止盈3R"
            if exit_px is None and i == n - 1:
                exit_px, why = px, "期末"
            if exit_px:
                finish(trades, pos, i, exit_px, why, dates, pos["risk"])
                if exit_px < pos["entry"]:
                    last_loss_i = i
                pos, below_e21 = None, 0
            else:
                if px >= pos["entry"] + BE_R * pos["risk"]:
                    pos["stop"] = max(pos["stop"], pos["entry"] * 1.001)
                new_sl = max([lows[j] for j in sl if pos["i"] < j < i-3] or [0])
                if new_sl > 0:
                    pos["stop"] = max(pos["stop"], new_sl * 0.995)
            continue
        if atrp[i] is None:
            continue
        if not (px > e9[i] and px > e21[i] and e21[i] >= e21[i-3]):
            continue
        recent = [j for j in sl if i - 90 <= j <= i - 4]
        if len(recent) < 2 or lows[recent[-1]] <= lows[recent[-2]]:
            continue
        if not (atrp[i-10] and atrp[i] < atrp[i-10]):
            continue
        anchors = [j for j in sh if 20 <= i - j <= 150 and highs[j] == max(highs[max(0, j-150):j+1])]
        if not anchors:
            continue
        av = avwap_series(highs, lows, closes, vols, anchors[-1])
        prev_high = highs[anchors[-1]]
        touched, zone_top = False, 0
        for j in range(max(START_BAR, i - 3), i + 1):
            levels = [x for x in [av[j], e21[j], e50[j], prev_high] if x and x > 0]
            near = [x for x in levels if abs(lows[j] - x) / x <= 0.03]
            if len(near) >= 2:
                touched, zone_top = True, max(near)
                break
        if not touched:
            continue
        if i - last_loss_i < COOLDOWN:
            continue
        if closes[i] > closes[i-1] and closes[i] > highs[i-1]:
            entry = closes[i]
            if entry > zone_top * CHASE:
                continue
            cand = [lows[i], lows[recent[-1]], entry - 2 * (atrp[i] or 2) / 100 * entry]
            stop = max(x for x in cand if x < entry * 0.995)
            stop = min(stop, entry * 0.98)
            risk = entry - stop
            if risk <= 0 or risk / entry > 0.08:
                continue
            pos = {"entry": entry, "i": i, "stop": stop, "risk": risk, "t3": entry + 3 * risk}
    return trades


# ---------------- 策略2: 双均线金叉 (EMA20 上穿 EMA60) ----------------

def strat_ema_cross(code, name, kl):
    closes = [k["close"] for k in kl]; highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]; dates = [k["date"] for k in kl]
    n = len(closes)
    e20, e60 = ema(closes, 20), ema(closes, 60)
    atrp = atr_pct(highs, lows, closes)
    trades, pos = [], None
    for i in range(max(START_BAR, 61), n):
        px = closes[i]
        if pos:
            stop = pos["stop"]
            if lows[i] <= stop:
                finish(trades, pos, i, stop, "止损", dates, pos["risk"]); pos = None
            elif e20[i] < e60[i]:
                finish(trades, pos, i, px, "死叉", dates, pos["risk"]); pos = None
            elif i == n - 1:
                finish(trades, pos, i, px, "期末", dates, pos["risk"]); pos = None
            continue
        crossed = e20[i] > e60[i] and e20[i-1] <= e60[i-1]
        if crossed and px > e60[i] and e60[i] >= e60[i-5] and atrp[i]:
            entry = px
            risk = entry * min(0.08, max(0.02, 2.5 * atrp[i] / 100))
            pos = {"entry": entry, "i": i, "stop": entry - risk, "risk": risk}
    return trades


# ---------------- 策略3: RSI 超卖反弹 ----------------

def strat_rsi_rebound(code, name, kl):
    closes = [k["close"] for k in kl]; highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]; dates = [k["date"] for k in kl]
    n = len(closes)
    rs = rsi_wilder(closes)
    e21 = ema(closes, 21)
    atrp = atr_pct(highs, lows, closes)
    trades, pos = [], None
    for i in range(max(START_BAR, 15), n):
        px = closes[i]
        if pos:
            stop = pos["stop"]
            if lows[i] <= stop:
                finish(trades, pos, i, stop, "止损", dates, pos["risk"]); pos = None
            elif rs[i] is not None and rs[i] >= 55:
                finish(trades, pos, i, px, "RSI修复", dates, pos["risk"]); pos = None
            elif px > e21[i] and i - pos["i"] > 2:
                finish(trades, pos, i, px, "回21EMA", dates, pos["risk"]); pos = None
            elif i == n - 1:
                finish(trades, pos, i, px, "期末", dates, pos["risk"]); pos = None
            continue
        if rs[i] is None or atrp[i] is None:
            continue
        # 近5日内出现过RSI<30, 今日收盘转阳 → 反弹确认
        low_seen = any(rs[j] is not None and rs[j] < 30 for j in range(max(14, i-5), i))
        if low_seen and px > closes[i-1] and px < e21[i] * 1.05:
            entry = px
            risk = entry * min(0.08, max(0.03, 2 * atrp[i] / 100))
            pos = {"entry": entry, "i": i, "stop": entry - risk, "risk": risk}
    return trades


# ---------------- 策略4: 布林带均值回归 ----------------

def strat_boll_reversion(code, name, kl):
    closes = [k["close"] for k in kl]; lows = [k["low"] for k in kl]
    dates = [k["date"] for k in kl]
    n = len(closes)
    mids, ups, los = boll(closes)
    trades, pos = [], None
    for i in range(max(START_BAR, 21), n):
        px = closes[i]
        if pos:
            if px >= mids[i]:
                finish(trades, pos, i, px, "回中轨", dates, pos["risk"]); pos = None
            elif lows[i] <= pos["stop"]:
                finish(trades, pos, i, pos["stop"], "止损", dates, pos["risk"]); pos = None
            elif i - pos["i"] >= 10:
                finish(trades, pos, i, px, "超时10日", dates, pos["risk"]); pos = None
            elif i == n - 1:
                finish(trades, pos, i, px, "期末", dates, pos["risk"]); pos = None
            continue
        if los[i] is None or los[i-1] is None:
            continue
        # 昨日跌破下轨, 今日收回下轨上方 → 均值回归
        if closes[i-1] < los[i-1] and px > los[i]:
            entry = px
            risk = entry * 0.04  # 均值回归用小固定止损
            pos = {"entry": entry, "i": i, "stop": entry - risk, "risk": risk}
    return trades


# ---------------- 策略5: 海龟式 20日突破 ----------------

def strat_turtle(code, name, kl):
    closes = [k["close"] for k in kl]; highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]; dates = [k["date"] for k in kl]
    n = len(closes)
    atrp = atr_pct(highs, lows, closes)
    trades, pos = [], None
    for i in range(max(START_BAR, 21), n):
        px = closes[i]
        if pos:
            trail = min(lows[max(0, i-10):i])  # 10日最低(不含今日, 无未来)
            stop = max(pos["stop"], trail)
            if lows[i] <= stop:
                exit_px = stop if stop < px else px
                finish(trades, pos, i, exit_px,
                       "追踪止损" if exit_px == trail and trail > pos["stop"] else "止损",
                       dates, pos["risk"]); pos = None
            elif i == n - 1:
                finish(trades, pos, i, px, "期末", dates, pos["risk"]); pos = None
            else:
                pos["stop"] = max(pos["stop"], trail)
            continue
        if atrp[i] is None:
            continue
        prev_high_20 = max(highs[i-20:i])
        if px > prev_high_20:
            entry = px
            risk = entry * min(0.08, max(0.02, 3 * atrp[i] / 100))  # 海龟 2N ATR≈3×ATR%
            pos = {"entry": entry, "i": i, "stop": entry - risk, "risk": risk}
    return trades


STRATEGIES = {
    "martin": ("马丁回调买法", strat_martin),
    "ema_cross": ("双均线金叉", strat_ema_cross),
    "rsi_rebound": ("RSI超卖反弹", strat_rsi_rebound),
    "boll_rev": ("布林均值回归", strat_boll_reversion),
    "turtle": ("海龟20日突破", strat_turtle),
}


def stats(trades, kl):
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    eq, peak, maxdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= 1 + t["ret"] / 100
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    yrs = len(kl) / 244
    return {"trades": len(trades), "winrate": len(wins) / len(rets) * 100,
            "avg": sum(rets) / len(rets), "avg_r": sum(t["r"] for t in trades) / len(trades),
            "total": (eq - 1) * 100, "cagr": (eq ** (1 / yrs) - 1) * 100 if yrs > 0 else 0,
            "maxdd": maxdd * 100, "buyhold": (kl[-1]["close"] / kl[0]["close"] - 1) * 100,
            "best": max(rets), "worst": min(rets)}


async def fetch_one(sem, code):
    async with sem:
        try:
            return code, await fetch_kline_tencent(code, days=DAYS, full_date=True)
        except Exception as e:  # noqa: BLE001
            return code, {"error": str(e)}


async def main():
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")]
    unknown = [a for a in wanted if a not in STRATEGIES]
    if unknown:
        print(f"未知策略: {unknown}  可选: {list(STRATEGIES)}")
        sys.exit(1)
    sel = {k: v for k, v in STRATEGIES.items() if not wanted or k in wanted}

    # 并发拉数据(各策略共用同一份K线)
    sem = asyncio.Semaphore(6)
    pairs = await asyncio.gather(*[fetch_one(sem, c) for c in POOL])
    data = {c: kl for c, kl in pairs if isinstance(kl, list) and kl}
    lost = [POOL[c] for c, kl in pairs if c not in data]
    if not data:
        print("全部数据获取失败, 退出"); sys.exit(2)
    span = f"{next(iter(data.values()))[0]['date']} ~ {next(iter(data.values()))[-1]['date']}"
    print(f"股票池 {len(data)} 只 | 区间 {span} | 成本 买{COST_BUY:.2%}/卖{COST_SELL:.2%} | T+1")
    if lost:
        print(f"数据缺失: {lost}")

    report = {"date": str(date.today()), "span": span, "pool": {k: POOL[k] for k in data},
              "costs": {"buy": COST_BUY, "sell": COST_SELL}, "strategies": {}}

    # 策略汇总表
    print(f"\n{'策略':<10}{'笔数':>5}{'胜率%':>7}{'均益%':>7}{'均R':>6}"
          f"{'等权累计%':>9}{'等权年化%':>9}{'均回撤%':>8}{'买入持有%':>9}")
    print("-" * 78)
    for key, (cname, fn) in sel.items():
        all_trades, per_stock, sums = [], {}, []
        bh_list, win_all = [], []
        for code in data:
            name, kl = POOL[code], data[code]
            trades = fn(code, name, kl) or []
            for t in trades:
                t["code"], t["name"] = code, name
            st = stats(trades, kl)
            per_stock[name] = st
            all_trades.extend(trades)
            bh_list.append(st["buyhold"] if st else (kl[-1]["close"] / kl[0]["close"] - 1) * 100)
            if st:
                sums.append(st["total"]); win_all.append(st["winrate"])
        rets = [t["ret"] for t in all_trades]
        wins = [r for r in rets if r > 0]
        dd_avg = (sum(per_stock[n]["maxdd"] for n in per_stock if per_stock[n]) /
                  max(1, sum(1 for s in per_stock.values() if s)))
        line = (f"{cname:<10}{len(all_trades):>5}")
        if rets:
            line += (f"{len(wins)/len(rets)*100:>7.1f}{sum(rets)/len(rets):>7.1f}"
                     f"{sum(t['r'] for t in all_trades)/len(all_trades):>6.2f}"
                     f"{statistics.mean(sums):>9.1f}"
                     f"{((1+statistics.mean(sums)/100)**(244/len(next(iter(data.values()))))-1)*100:>9.1f}"
                     f"{dd_avg:>8.1f}{statistics.mean(bh_list):>9.1f}")
        else:
            line += f"    -      -     -        -        -       -{statistics.mean(bh_list):>9.1f}"
        print(line)
        report["strategies"][key] = {
            "name": cname,
            "summary": {"trades": len(all_trades),
                        "winrate": round(len(wins)/len(rets)*100, 1) if rets else None,
                        "avg_ret": round(sum(rets)/len(rets), 2) if rets else None,
                        "avg_r": round(sum(t["r"] for t in all_trades)/len(all_trades), 2) if all_trades else None,
                        "eq_total": round(statistics.mean(sums), 1) if sums else None,
                        "dd_avg": round(dd_avg, 1),
                        "buyhold_avg": round(statistics.mean(bh_list), 1)},
            "per_stock": {n: s for n, s in per_stock.items()},
            "trades": sorted(all_trades, key=lambda x: x["sell_date"]),
        }

    # 分股票矩阵(等权累计%)
    print(f"\n分标的累计收益% (买入持有在括号内):")
    head = f"{'策略':<10}" + "".join(f"{POOL[c]:<7}" for c in data)
    print(head); print("-" * len(head))
    for key, (cname, _) in sel.items():
        row = f"{cname:<10}"
        for code in data:
            s = report["strategies"][key]["per_stock"].get(POOL[code])
            bh = (data[code][-1]["close"] / data[code][0]["close"] - 1) * 100
            row += (f"{s['total']:>6.1f}({bh:>5.0f})" if s else f"     -({bh:>5.0f})")
            row += " " if len(POOL[code]) < 4 else ""
        print(row)

    # 每策略最近3笔
    print("\n各策略最近交易(每策略3笔):")
    for key, (cname, _) in sel.items():
        tr = report["strategies"][key]["trades"][-3:]
        for t in tr:
            print(f"  [{cname}] {t['name']} {t['buy_date']}买{t['buy']} → {t['sell_date']}卖{t['sell']} "
                  f"{t['ret']:+6.1f}% ({t['r']:+.1f}R) [{t['why']}] 持{t['days']}日")

    out = f"/root/workspace/jungle-trading/reports/multi_backtest_{date.today()}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n明细已存: {out}")


if __name__ == "__main__":
    asyncio.run(main())
