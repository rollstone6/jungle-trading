"""Martin Luk 回调买法(Pullback Buy)回测复刻 — 日线近似版

策略规则(源自小红书笔记):
  筛选: 回调低点抬高 + 波动收窄 + 收盘站上走平/向上的9/21日EMA
  支撑: AVWAP(锚定阶段高点) / 21EMA / 50EMA / 前期波段高点 多重共振区
  入场: 回踩支撑区后, 收盘突破前一根K线高点(日线近似分钟图触发)
  离场: 跌破摆动低点初始止损, 或收盘跌破21EMA趋势止损, 或止盈3R

注意: 原文用1/5/15分钟图择时, 此处用日线触发近似; 969.8%年化来自美股期权
杠杆玩法, 本回测为现货无杠杆, 口径不同。
"""
import asyncio
import sys
import math

sys.path.insert(0, "/root/workspace/jungle-trading")
from app.services.market import fetch_kline_tencent  # noqa: E402

POOL = {
    "603678": "火炬电子",
    "300291": "百纳千成",
    "300394": "天孚通信",
    "300505": "川金诺",
    "300999": "大普微",
}

COST_BUY = 0.0005   # 佣金+滑点
COST_SELL = 0.0010  # 佣金+印花税+滑点

# 允许环境变量覆盖, 便于 A/B 对比原版参数
import os
TREND_STOP_CONFIRM = int(os.environ.get("MTP_CONFIRM", 2))
BE_AFTER_R = float(os.environ.get("MTP_BE", 1.0))
CHASE_MAX = float(os.environ.get("MTP_CHASE", 1.02))
COOLDOWN = int(os.environ.get("MTP_COOLDOWN", 5))


def ema(vals, n):
    out = []
    k = 2 / (n + 1)
    e = vals[0]
    for v in vals:
        e = k * v + (1 - k) * e
        out.append(e)
    return out


def atr_pct(highs, lows, closes, n=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])))
    out = [None] * len(closes)
    a = sum(trs[:n]) / n
    out[n-1] = a / closes[n-1] * 100
    for i in range(n, len(closes)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a / closes[i] * 100
    return out, trs


def swing_lows(lows, lb=3, rb=3):
    """确认的摆动低点索引(rb根K线后确认, 无未来函数)"""
    idx = []
    for i in range(lb, len(lows) - rb):
        if lows[i] == min(lows[i-lb:i+rb+1]):
            idx.append(i)
    return idx


def swing_highs(highs, lb=5, rb=5):
    idx = []
    for i in range(lb, len(highs) - rb):
        if highs[i] == max(highs[i-lb:i+rb+1]):
            idx.append(i)
    return idx


def avwap_series(opens, highs, lows, closes, vols, anchor):
    """从anchor日起的锚定VWAP序列"""
    out = [None] * len(closes)
    pv = v = 0.0
    for i in range(anchor, len(closes)):
        tp = (highs[i] + lows[i] + closes[i]) / 3
        pv += tp * vols[i]
        v += vols[i]
        out[i] = pv / v if v > 0 else closes[i]
    return out


def run_stock(code, name, kl):
    closes = [k["close"] for k in kl]
    highs = [k["high"] for k in kl]
    lows = [k["low"] for k in kl]
    opens = [k["open"] for k in kl]
    vols = [k["volume"] for k in kl]
    dates = [k["date"] for k in kl]
    n = len(closes)
    if n < 150:
        return None

    e9, e21, e50 = ema(closes, 9), ema(closes, 21), ema(closes, 50)
    atrp, _ = atr_pct(highs, lows, closes)
    sl = swing_lows(lows)
    sh = swing_highs(highs)

    trades = []
    pos = None  # dict(entry_price, entry_i, stop, t3)
    below_e21 = 0   # 连续收盘低于21EMA计数
    last_loss_i = -999  # 上次亏损出场K线, 冷却期用

    for i in range(60, n):
        px = closes[i]

        # ---- 持仓管理 ----
        if pos:
            exit_px, why = None, None
            if lows[i] <= pos["stop"]:
                exit_px, why = max(pos["stop"], lows[i] if lows[i] < pos["stop"] else pos["stop"]), \
                    "保本" if pos["stop"] >= pos["entry"] else "止损"
                exit_px = pos["stop"]
            else:
                below_e21 = below_e21 + 1 if px < e21[i] else 0
                if below_e21 >= TREND_STOP_CONFIRM:
                    exit_px, why = px, f"连{TREND_STOP_CONFIRM}日破21EMA"
                elif px >= pos["t3"]:
                    exit_px, why = pos["t3"], "止盈3R"
            if exit_px is None and i == n - 1:
                exit_px, why = px, "期末"
            if exit_px:
                r = pos["risk"]
                ret = (exit_px / pos["entry"] - 1) * 100
                trades.append({
                    "code": code, "name": name, "buy_date": dates[pos["i"]],
                    "buy": pos["entry"], "sell_date": dates[i], "sell": exit_px,
                    "ret": ret, "r": (exit_px - pos["entry"]) / r, "why": why,
                    "days": i - pos["i"],
                })
                if ret < 0:
                    last_loss_i = i
                pos, below_e21 = None, 0
            else:
                # 保本推损: 盈利达BE_AFTER_R后止损上移至成本
                if px >= pos["entry"] + BE_AFTER_R * pos["risk"]:
                    pos["stop"] = max(pos["stop"], pos["entry"] * 1.001)
                # 移动止损: 上推至入场后新摆动低点
                new_sl = max([lows[j] for j in sl if pos["i"] < j < i-3] or [0])
                if new_sl > 0:
                    pos["stop"] = max(pos["stop"], new_sl * 0.995)
            continue

        if atrp[i] is None:
            continue

        # ---- 第一步: 趋势筛选 ----
        # 1a. 站上9/21EMA, 且21EMA走平或向上
        if not (px > e9[i] > 0 and px > e21[i] and e21[i] >= e21[i-3]):
            continue
        # 1b. 回调低点抬高: 最近两个已确认摆动低点
        recent = [j for j in sl if i - 90 <= j <= i - 4]
        if len(recent) < 2 or lows[recent[-1]] <= lows[recent[-2]]:
            continue
        # 1c. 波动收窄: 近10日ATR% < 20~30日前ATR%
        if not (atrp[i-10] and atrp[i] < atrp[i-10]):
            continue

        # ---- 第二步: 支撑共振区 ----
        # AVWAP锚点 = 最近一个已确认阶段高点(20~150根之前)
        anchors = [j for j in sh if 20 <= i - j <= 150 and highs[j] == max(highs[max(0, j-150):j+1])]
        if not anchors:
            continue
        av = avwap_series(opens, highs, lows, closes, vols, anchors[-1])
        prev_high = highs[anchors[-1]]
        # 回踩确认: 近3日内(含今日)低点曾触及至少2个支撑位±3%
        touched = False
        zone_top = 0
        for j in range(max(60, i - 3), i + 1):
            levels = [x for x in [av[j], e21[j], e50[j], prev_high] if x and x > 0]
            near = [x for x in levels if abs(lows[j] - x) / x <= 0.03]
            if len(near) >= 2:
                touched = True
                zone_top = max(near)
                break
        if not touched:
            continue

        # ---- 第三步: 触发 = 阳线收盘突破前一根K线高点(回踩后回升) ----
        if i - last_loss_i < COOLDOWN:  # 亏损出场后冷却
            continue
        if closes[i] > closes[i-1] and closes[i] > highs[i-1]:
            entry = closes[i]
            if entry > zone_top * CHASE_MAX:  # 防追高: 别在支撑区上方追大阳
                continue
            # 止损: 入场K线低点 / 最近一根摆动低点 / 入场-2ATR, 取最合理(最接近且不高于入场)
            cand = [lows[i], lows[recent[-1]], entry - 2 * (atrp[i] or 2) / 100 * entry]
            stop = max(x for x in cand if x < entry * 0.995)
            stop = min(stop, entry * 0.98)  # 至少2%缓冲, 避免被日内噪音扫损
            risk = entry - stop
            if risk <= 0 or risk / entry > 0.08:  # 风险过大放弃
                continue
            pos = {"entry": entry, "i": i, "stop": stop, "risk": risk, "t3": entry + 3 * risk}

    return trades


def stats(trades, kl):
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    equity, eq_curve, peak, maxdd = 1.0, [], 1.0, 0.0
    for t in trades:
        equity *= (1 + t["ret"] / 100)
        eq_curve.append(equity)
        peak = max(peak, equity)
        maxdd = max(maxdd, (peak - equity) / peak)
    first, last = kl[0]["date"], kl[-1]["date"]
    yrs = len(kl) / 244
    cagr = (equity ** (1 / yrs) - 1) * 100 if yrs > 0 else 0
    bh = (kl[-1]["close"] / kl[0]["close"] - 1) * 100
    return {
        "trades": len(trades), "winrate": len(wins) / len(rets) * 100,
        "avg": sum(rets) / len(rets), "total": (equity - 1) * 100,
        "cagr": cagr, "maxdd": maxdd * 100, "buyhold": bh,
        "avg_r": sum(t["r"] for t in trades) / len(trades),
    }


async def main():
    print(f"{'股票':<6}{'交易数':>6}{'胜率%':>7}{'均益%':>7}{'累计%':>9}{'年化%':>8}{'回撤%':>8}{'买入持有%':>10}")
    print("-" * 68)
    all_trades = []
    per_stock = {}
    for code, name in POOL.items():
        kl = await fetch_kline_tencent(code, days=800, full_date=True)
        if not kl:
            print(f"{name:<6}  无数据")
            continue
        trades = run_stock(code, name, kl) or []
        st = stats(trades, kl)
        per_stock[name] = st
        all_trades.extend(trades)
        if st:
            print(f"{name:<6}{st['trades']:>6}{st['winrate']:>7.1f}{st['avg']:>7.1f}"
                  f"{st['total']:>9.1f}{st['cagr']:>8.1f}{st['maxdd']:>8.1f}{st['buyhold']:>10.1f}")
        else:
            print(f"{name:<6}     0      -      -        -       -       -"
                  f"{(kl[-1]['close']/kl[0]['close']-1)*100:>10.1f}")

    if all_trades:
        rets = [t["ret"] for t in all_trades]
        wins = [r for r in rets if r > 0]
        print("-" * 68)
        print(f"合计 {len(all_trades)} 笔 | 胜率 {len(wins)/len(rets)*100:.1f}% | "
              f"平均 {sum(rets)/len(rets):+.1f}% | 平均R {sum(t['r'] for t in all_trades)/len(all_trades):+.2f} | "
              f"最好 {max(rets):+.1f}% / 最差 {min(rets):+.1f}%")
        print("\n最近10笔交易:")
        for t in sorted(all_trades, key=lambda x: x["sell_date"])[-10:]:
            print(f"  {t['name']:<5} {t['buy_date']}买{t['buy']:.2f} → {t['sell_date']}卖{t['sell']:.2f} "
                  f"{t['ret']:+6.1f}% ({t['r']:+.1f}R) [{t['why']}] 持{t['days']}日")


if __name__ == "__main__":
    asyncio.run(main())
