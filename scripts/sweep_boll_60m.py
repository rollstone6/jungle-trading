# -*- coding: utf-8 -*-
"""量化股票池布林带60分钟全池扫描"""
import asyncio
import csv
import sys
from pathlib import Path

REPO = Path(r"D:\store_for_prgram\py_program\jungle-trading")
sys.path.insert(0, str(REPO))

from app.quant.config import STOCK_LIST  # noqa: E402
from app.services.market import fetch_kline_sina_minute  # noqa: E402
from app.services.backtest import run_backtest  # noqa: E402


def main():
    # 去重保序
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

    results, failures = [], []
    for s in pool:
        kl = klines_map.get(s["symbol"], [])
        if len(kl) < 30:
            failures.append((s, f"K线不足({len(kl)}根)"))
            continue
        r = run_backtest(kl, strategy_name="boll", initial_cash=100000)
        if r.get("success"):
            results.append({
                "symbol": s["symbol"],
                "name": s["name"],
                "total_return": r["total_return"],
                "win_rate": r["win_rate"],
                "total_trades": r["total_trades"],
                "max_drawdown": r["max_drawdown"],
                "sharpe": r["sharpe_ratio"],
                "final_value": r["final_value"],
                "start": r["start_date"],
                "end": r["end_date"],
                "bars": r["total_days"],
            })
        else:
            failures.append((s, r.get("error", "未知错误")))

    results.sort(key=lambda x: x["total_return"], reverse=True)

    # CSV
    csv_path = Path(r"D:\store_for_prgram\boll_60m_sweep.csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["排名", "代码", "名称", "总收益率%", "胜率%", "交易次数", "最大回撤%",
                    "夏普", "期末资产", "数据区间", "K线根数"])
        for i, r in enumerate(results, 1):
            w.writerow([i, r["symbol"], r["name"], r["total_return"], r["win_rate"],
                        r["total_trades"], r["max_drawdown"], r["sharpe"],
                        r["final_value"], f'{r["start"]} ~ {r["end"]}', r["bars"]])

    # HTML 报告
    rows = "".join(
        f"<tr><td>{i}</td><td>{r['symbol']}</td><td class='name'>{r['name']}</td>"
        f"<td class='{'gain' if r['total_return']>=0 else 'loss'}'>{r['total_return']:+.2f}%</td>"
        f"<td>{r['win_rate']:.1f}%</td><td>{r['total_trades']}</td>"
        f"<td class='loss'>{r['max_drawdown']:.2f}%</td><td>{r['sharpe']}</td>"
        f"<td>{r['final_value']:,.0f}</td></tr>"
        for i, r in enumerate(results, 1)
    )
    fail_html = "".join(f"<li>{s['name']} ({s['symbol']}): {err}</li>" for s, err in failures)
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>布林带60分钟全池扫描</title><style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#eef3f8;margin:0;padding:20px}}
.wrap{{max-width:1100px;margin:0 auto}}
.hero{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:28px;border-radius:16px;text-align:center;margin-bottom:24px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
th{{background:#667eea;color:#fff;padding:10px;font-size:13px;text-align:left}}
td{{padding:9px 10px;font-size:13px;border-bottom:1px solid #eef0f4}}
tr:hover{{background:#f8f7ff}}
.name{{font-weight:600}}
.gain{{color:#10b981;font-weight:700}}
.loss{{color:#ef4444;font-weight:700}}
.fail{{background:#fff;border-radius:12px;padding:16px 20px;margin-top:20px;color:#666;font-size:13px}}
h2{{font-size:16px;color:#1e293b;border-left:4px solid #667eea;padding-left:10px}}
</style></head><body><div class="wrap">
<div class="hero"><h1>🧪 布林带均值回归 · 60分钟 · 全池扫描</h1>
<p>股票池：app/quant STOCK_LIST（{len(pool)} 只）｜ 区间约1年 ｜ 初始资金 ¥100,000</p></div>
<table><tr><th>排名</th><th>代码</th><th>名称</th><th>总收益率</th><th>胜率</th><th>交易次数</th><th>最大回撤</th><th>夏普</th><th>期末资产</th></tr>
{rows}</table>
<div class="fail"><h2>跳过/失败（{len(failures)}）</h2><ul>{fail_html}</ul></div>
</div></body></html>"""
    html_path = Path(r"D:\store_for_prgram\boll_60m_sweep.html")
    html_path.write_text(html, encoding="utf-8")

    print(f"OK {len(results)} / FAIL {len(failures)}")
    for i, r in enumerate(results, 1):
        print(f"{i:2d}. {r['name']}({r['symbol']}) 收益{r['total_return']:+.2f}% 胜率{r['win_rate']:.0f}% 交易{r['total_trades']}笔 回撤{r['max_drawdown']:.2f}%")
    for s, err in failures:
        print(f"  ! {s['name']}({s['symbol']}): {err}")


if __name__ == "__main__":
    main()
