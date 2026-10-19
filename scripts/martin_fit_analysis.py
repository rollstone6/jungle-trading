"""分析: Martin回调买法(日线+防反噬版)适合哪类股票

从多板块拉24只代表性股票, 跑策略 + 给股票打特征标签:
  - regime分类(趋势/区间/挤压/中性, 复用 regime.py)
  - 波动率(ATR% 中位数) 高/中/低
  - 价格水平(高股价 vs 低股价)
  - 250日趋势强度(买入持有收益)
按特征分组聚合策略表现。
"""
import asyncio
import statistics as st
import sys

sys.path.insert(0, "/root/workspace/jungle-trading")
sys.path.insert(0, "/root/workspace/jungle-trading/scripts")

from app.services.market import fetch_kline_tencent  # noqa: E402
from app.services.regime import classify_regime  # noqa: E402
from martin_pullback_backtest import run_stock, stats  # noqa: E402

POOL = {
    # 芯片/AI算力(强趋势高波动)
    "300308": "中际旭创", "002916": "深南电路", "688981": "中芯国际",
    "603501": "韦尔股份", "601138": "工业富联", "300502": "新易盛",
    # 白马大盘(低波动)
    "600519": "贵州茅台", "601398": "工商银行", "600900": "长江电力",
    "601088": "中国神华", "000858": "五粮液", "600309": "万华化学",
    # 周期资源(波段)
    "601899": "紫金矿业", "603799": "华友钴业", "002460": "赣锋锂业",
    "600585": "海螺水泥",
    # 中小盘题材
    "300059": "东方财富", "603259": "药明康德", "300750": "宁德时代",
    "002230": "科大讯飞", "688017": "绿的谐波", "002371": "北方华创",
    "300124": "汇川技术", "600893": "航发动力",
}


async def one(code, name):
    kl = await fetch_kline_tencent(code, days=800, full_date=True)
    if not kl or len(kl) < 300:
        return None
    trades = run_stock(code, name, kl) or []
    st_ = stats(trades, kl)
    closes = [k["close"] for k in kl]
    highs, lows = [k["high"] for k in kl], [k["low"] for k in kl]
    atr = []
    for i in range(15, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        atr.append(tr / closes[i] * 100)
    atr_med = st.median(atr)
    trend250 = (closes[-1] / closes[-250] - 1) * 100 if len(closes) >= 250 else 0
    rg = classify_regime(kl[-120:])["regime"]
    return {"name": name, "st": st_, "trades": trades, "atr": atr_med,
            "px": closes[-1], "trend": trend250, "regime": rg, "bh": (closes[-1]/closes[0]-1)*100}


def agg(rows, label):
    tl = [t for r in rows for t in r["trades"]]
    if not tl:
        print(f"{label:<12} 股票{len(rows):>2}只 | 无交易")
        return
    rets = [t["ret"] for t in tl]
    wins = [r for r in rets if r > 0]
    tot = 1.0
    for r in rows:
        if r["st"]:
            tot *= 1 + r["st"]["total"] / 100
    print(f"{label:<12} 股票{len(rows):>2}只 | {len(tl):>3}笔 胜率{len(wins)/len(rets)*100:>5.1f}% "
          f"均益{st.mean(rets):>+6.2f}% 中位{st.median(rets):>+6.2f}% | 最差{min(rets):>+6.1f}% 最好{max(rets):>+6.1f}%")


async def main():
    res = []
    for code, name in POOL.items():
        r = await one(code, name)
        if r:
            res.append(r)
            s = r["st"]
            print(f"  {r['name']:<6} regime={r['regime']:<9} ATR%={r['atr']:>4.1f} 价={r['px']:>7.1f} "
                  f"250日趋势={r['trend']:>+7.1f}% | 策略: {s['trades'] if s else 0}笔 "
                  f"{s['winrate'] if s else '-':>5}胜率 {s['total'] if s else '-':>6}累计")

    print("\n================ 分组聚合 ================")
    agg([r for r in res if r["regime"] == "trending"], "趋势型")
    agg([r for r in res if r["regime"] == "ranging"], "区间型")
    agg([r for r in res if r["regime"] == "squeeze"], "挤压待变")
    agg([r for r in res if r["regime"] == "neutral"], "中性")
    print("---")
    agg([r for r in res if r["atr"] < 2.5], "低波动ATR<2.5")
    agg([r for r in res if 2.5 <= r["atr"] < 4.0], "中波动2.5-4")
    agg([r for r in res if r["atr"] >= 4.0], "高波动ATR>4")
    print("---")
    agg([r for r in res if r["px"] >= 100], "高股价>100元")
    agg([r for r in res if r["px"] < 100], "低股价<100元")
    print("---")
    agg([r for r in res if r["trend"] > 50], "强上涨(250日+50%)")
    agg([r for r in res if -20 <= r["trend"] <= 50], "温和趋势")
    agg([r for r in res if r["trend"] < -20], "下跌/震荡市")


if __name__ == "__main__":
    asyncio.run(main())
