# -*- coding: utf-8 -*-
"""量化股票池多策略60分钟对比扫描：每只股票拉一次K线，跑全部策略"""
import asyncio
import csv
import sys
from pathlib import Path

REPO = Path(r"D:\store_for_prgram\py_program\jungle-trading")
sys.path.insert(0, str(REPO))

from app.quant.config import STOCK_LIST  # noqa: E402
from app.services.market import fetch_kline_sina_minute  # noqa: E402
from app.services.backtest import run_backtest  # noqa: E402

STRATEGIES = ["boll", "boll_macd", "ma_macd", "rsi_boll", "macd_vol"]
COMMISSION = 0.0001  # 万一，接近真实券商成本


def main():
    seen, pool = set(), []
    for s in STOCK_LIST:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            pool.append(s)

    async def fetch_all():
        out = {}
        for s in pool:
            try:
                out[s["symbol"]] = await fetch_kline_sina_minute(s["symbol"], scale=60)
            except Exception:
                out[s["symbol"]] = []
        return out

    klines_map = asyncio.run(fetch_all())

    # per[strategy] = list of result rows
    per = {name: [] for name in STRATEGIES}
    failures = []
    for s in pool:
        kl = klines_map.get(s["symbol"], [])
        if len(kl) < 30:
            failures.append(s)
            continue
        for name in STRATEGIES:
            r = run_backtest(kl, strategy_name=name, initial_cash=100000,
                             commission=COMMISSION)
            if r.get("success"):
                per[name].append({
                    "symbol": s["symbol"], "name": s["name"],
                    "total_return": r["total_return"], "win_rate": r["win_rate"],
                    "total_trades": r["total_trades"], "max_drawdown": r["max_drawdown"],
                    "sharpe": r["sharpe_ratio"], "final_value": r["final_value"],
                })
            else:
                print(f"  ! {name} {s['name']}: {r.get('error')}")

    # CSV：每只股票 x 每个策略
    csv_path = Path(r"D:\store_for_prgram\strategy_compare_60m.csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["策略", "代码", "名称", "总收益率%", "胜率%", "交易次数",
                    "最大回撤%", "夏普", "期末资产"])
        for name in STRATEGIES:
            rows = sorted(per[name], key=lambda x: x["total_return"], reverse=True)
            for r in rows:
                w.writerow([name, r["symbol"], r["name"], r["total_return"],
                            r["win_rate"], r["total_trades"], r["max_drawdown"],
                            r["sharpe"], r["final_value"]])

    # HTML：每策略一个表格
    STRAT_LABELS = {
        "boll": "布林带均值回归（基线）",
        "boll_macd": "布林+MACD 趋势中高卖低买",
        "ma_macd": "均线+MACD 共振",
        "rsi_boll": "RSI+布林双重超卖",
        "macd_vol": "MACD+放量确认",
    }
    tables = ""
    for name in STRATEGIES:
        rows = sorted(per[name], key=lambda x: x["total_return"], reverse=True)
        trs = "".join(
            f"<tr><td>{i}</td><td>{r['symbol']}</td><td class='name'>{r['name']}</td>"
            f"<td class='{'gain' if r['total_return']>=0 else 'loss'}'>{r['total_return']:+.2f}%</td>"
            f"<td>{r['win_rate']:.1f}%</td><td>{r['total_trades']}</td>"
            f"<td class='loss'>{r['max_drawdown']:.2f}%</td><td>{r['sharpe']}</td>"
            f"<td>{r['final_value']:,.0f}</td></tr>"
            for i, r in enumerate(rows, 1))
        avg = sum(r["total_return"] for r in rows) / len(rows) if rows else 0
        tables += (f"<h2>{STRAT_LABELS[name]} <small>（平均收益 {avg:+.3f}%）</small></h2>"
                   f"<table><tr><th>排名</th><th>代码</th><th>名称</th><th>总收益率</th>"
                   f"<th>胜率</th><th>交易次数</th><th>最大回撤</th><th>夏普</th><th>期末资产</th></tr>"
                   f"{trs}</table>")

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>多策略60分钟对比扫描</title><style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#eef3f8;margin:0;padding:20px}}
.wrap{{max-width:1100px;margin:0 auto}}
.hero{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:28px;border-radius:16px;text-align:center;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1);margin-bottom:8px}}
th{{background:#667eea;color:#fff;padding:10px;font-size:13px;text-align:left}}
td{{padding:9px 10px;font-size:13px;border-bottom:1px solid #eef0f4}}
tr:hover{{background:#f8f7ff}}
.name{{font-weight:600}}
.gain{{color:#10b981;font-weight:700}}
.loss{{color:#ef4444;font-weight:700}}
h2{{font-size:16px;color:#1e293b;border-left:4px solid #667eea;padding-left:10px;margin:28px 0 12px}}
h2 small{{color:#888;font-weight:400;font-size:12px}}
</style></head><body><div class="wrap">
<div class="hero"><h1>🧪 五策略 60 分钟全池对比</h1>
<p>股票池 {len(pool)} 只（成功 {len(pool)-len(failures)}）｜ 手续费万一（0.0001）｜ 初始资金 ¥100,000</p></div>
{tables}</div></body></html>"""
    Path(r"D:\store_for_prgram\strategy_compare_60m.html").write_text(html, encoding="utf-8")

    # 控制台汇总：每策略的平均收益/胜率/交易数
    print("策略对比汇总（60分钟，万一手续费）")
    for name in STRATEGIES:
        rows = per[name]
        if not rows:
            print(f"{name}: 无结果")
            continue
        avg_ret = sum(r["total_return"] for r in rows) / len(rows)
        avg_win = sum(r["win_rate"] for r in rows) / len(rows)
        avg_tr = sum(r["total_trades"] for r in rows) / len(rows)
        best = max(rows, key=lambda x: x["total_return"])
        print(f"{name:10s} 平均收益{avg_ret:+.3f}% 平均胜率{avg_win:.0f}% 平均交易{avg_tr:.0f}笔 "
              f"最佳:{best['name']}{best['total_return']:+.2f}%")
    if failures:
        print("跳过:", ", ".join(f"{s['name']}({s['symbol']})" for s in failures))


if __name__ == "__main__":
    main()
