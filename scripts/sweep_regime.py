# -*- coding: utf-8 -*-
"""带走势分类的全池扫描：先分类，再对比 分类匹配策略 vs 统一策略 的效果

用法：
    python scripts/sweep_regime.py 1d    # 默认日线1.5年
    python scripts/sweep_regime.py 60m

输出（保存到仓库上级工作目录）：
    regime_scan_<scale>.csv   宽表：每只股票 x 分类指标 x 9个策略收益
    regime_scan_<scale>.html  可视化报告
"""
import asyncio
import csv
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.quant.config import STOCK_LIST  # noqa: E402
from app.services.market import (  # noqa: E402
    fetch_kline_sina_minute, fetch_kline_tencent,
)
from app.services.backtest import run_backtest  # noqa: E402
from app.services.regime import classify_regime, REGIME_ADVICE  # noqa: E402

STRATEGIES = ["ma", "macd", "boll", "boll_macd", "ma_macd", "rsi_boll", "macd_vol", "kdj", "kdj_macd"]
COMMISSION = 0.0001


def fetch(scale: str, code: str):
    if scale == "1d":
        return fetch_kline_tencent(code, days=550, full_date=True)
    return fetch_kline_sina_minute(code, scale=int(scale[:-1]))


def main():
    scale = sys.argv[1] if len(sys.argv) > 1 else "1d"

    seen, pool = set(), []
    for s in STOCK_LIST:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            pool.append(s)

    async def fetch_all():
        out = {}
        for s in pool:
            try:
                out[s["symbol"]] = await fetch(scale, s["symbol"])
            except Exception:
                out[s["symbol"]] = []
        return out

    klines_map = asyncio.run(fetch_all())

    rows = []
    for s in pool:
        kl = klines_map.get(s["symbol"], [])
        if len(kl) < 30:
            print(f"  ! 跳过 {s['name']}({s['symbol']}): K线不足({len(kl)})")
            continue
        info = classify_regime(kl)
        matched = REGIME_ADVICE[info["regime"]]["strategies"]

        returns = {}
        for name in STRATEGIES:
            r = run_backtest(kl, strategy_name=name, initial_cash=100000,
                             commission=COMMISSION)
            returns[name] = r["total_return"] if r.get("success") else None

        matched_vals = [returns[m] for m in matched if returns.get(m) is not None]
        rows.append({
            "symbol": s["symbol"], "name": s["name"],
            "regime": info["regime"], "matched": matched,
            "outside_ratio": info["outside_ratio"],
            "bandwidth_pct": info["bandwidth_pct"],
            "bullish_align": info["bullish_align"],
            "ma_dev_pct": info["ma_dev_pct"],
            "returns": returns,
            "matched_avg": round(sum(matched_vals) / len(matched_vals), 4) if matched_vals else None,
            "matched_best": round(max(matched_vals), 4) if matched_vals else None,
        })

    out_dir = REPO.parent.parent

    # CSV 宽表
    csv_path = out_dir / f"regime_scan_{scale}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["代码", "名称", "分类", "轨外比例", "带宽百分位", "均线多头", "偏离MA20%"]
                   + [f"{n}收益%" for n in STRATEGIES]
                   + ["匹配策略平均收益%", "匹配策略最佳收益%", "匹配策略列表"])
        for r in rows:
            w.writerow([r["symbol"], r["name"], r["regime"],
                        r["outside_ratio"], r["bandwidth_pct"],
                        "是" if r["bullish_align"] else "否", r["ma_dev_pct"]]
                       + [r["returns"][n] for n in STRATEGIES]
                       + [r["matched_avg"], r["matched_best"], "/".join(r["matched"])])

    # 汇总统计
    n = len(rows)
    uniform_avg = {name: sum(r["returns"][name] for r in rows if r["returns"][name] is not None)
                   / max(1, sum(1 for r in rows if r["returns"][name] is not None))
                   for name in STRATEGIES}
    matched_avg_pool = sum(r["matched_avg"] for r in rows if r["matched_avg"] is not None) \
        / max(1, sum(1 for r in rows if r["matched_avg"] is not None))
    matched_best_pool = sum(r["matched_best"] for r in rows if r["matched_best"] is not None) \
        / max(1, sum(1 for r in rows if r["matched_best"] is not None))
    regime_counts = {}
    for r in rows:
        regime_counts[r["regime"]] = regime_counts.get(r["regime"], 0) + 1

    # HTML 报告
    REGIME_LABEL = {k: v["label"] for k, v in REGIME_ADVICE.items()}
    STRAT_LABELS = {
        "ma": "均线交叉", "macd": "MACD", "boll": "布林", "boll_macd": "布林+MACD",
        "ma_macd": "均线+MACD", "rsi_boll": "RSI+布林", "macd_vol": "MACD+放量",
        "kdj": "KDJ", "kdj_macd": "KDJ+MACD",
    }
    body_rows = ""
    for r in rows:
        tds = ""
        for name in STRATEGIES:
            v = r["returns"][name]
            star = "★" if name in r["matched"] else ""
            cls = "gain" if (v or 0) >= 0 else "loss"
            tds += f"<td class='{cls}'>{v:+.2f}{star}</td>" if v is not None else "<td>-</td>"
        body_rows += (f"<tr><td>{r['symbol']}</td><td class='name'>{r['name']}</td>"
                      f"<td><span class='badge-regime {r['regime']}'>{REGIME_LABEL[r['regime']]}</span></td>"
                      f"<td>{(r['outside_ratio']*100):.0f}%</td><td>{(r['bandwidth_pct']*100):.0f}%</td>"
                      f"<td>{r['ma_dev_pct']:+.1f}%</td>{tds}"
                      f"<td class='{'gain' if (r['matched_avg'] or 0)>=0 else 'loss'}'>{r['matched_avg']:+.3f}</td></tr>")

    uniform_lis = "".join(
        f"<tr><td>{STRAT_LABELS[name]}</td><td class='{'gain' if v>=0 else 'loss'}'>{v:+.4f}%</td></tr>"
        for name, v in sorted(uniform_avg.items(), key=lambda x: x[1], reverse=True))

    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>走势分类全池扫描 {scale}</title><style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;background:#eef3f8;margin:0;padding:20px}}
.wrap{{max-width:1280px;margin:0 auto}}
.hero{{background:linear-gradient(135deg,#667eea,#764ba2);color:#fff;padding:28px;border-radius:16px;text-align:center;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:24px}}
.kpi{{background:#fff;border-radius:12px;padding:18px;box-shadow:0 2px 8px rgba(0,0,0,.1);text-align:center}}
.kpi .v{{font-size:26px;font-weight:800;color:#667eea}}
.kpi .l{{font-size:12px;color:#64748b;margin-top:6px}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1);margin-bottom:24px}}
th{{background:#667eea;color:#fff;padding:9px;font-size:12px;text-align:left;white-space:nowrap}}
td{{padding:8px 9px;font-size:12px;border-bottom:1px solid #eef0f4;white-space:nowrap}}
tr:hover{{background:#f8f7ff}}
.name{{font-weight:600}}
.gain{{color:#10b981;font-weight:700}}
.loss{{color:#ef4444;font-weight:700}}
.badge-regime{{padding:3px 10px;border-radius:12px;color:#fff;font-size:12px}}
.badge-regime.ranging{{background:#10b981}} .badge-regime.trending{{background:#667eea}}
.badge-regime.squeeze{{background:#f59e0b}} .badge-regime.neutral{{background:#94a3b8}}
h2{{font-size:16px;color:#1e293b;border-left:4px solid #667eea;padding-left:10px;margin:26px 0 12px}}
.note{{color:#64748b;font-size:12px;margin-top:-14px;margin-bottom:16px}}
</style></head><body><div class="wrap">
<div class="hero"><h1>📊 走势分类全池扫描（{scale}）</h1>
<p>{n} 只 · 手续费万一 · 初始资金 ¥100,000 · ★ = 该股票的分类匹配策略</p></div>
<div class="cards">
<div class="kpi"><div class="v">{matched_avg_pool:+.4f}%</div><div class="l">分类匹配策略 全池平均收益（无未来函数）</div></div>
<div class="kpi"><div class="v">{max(uniform_avg.values()):+.4f}%</div><div class="l">最佳单一策略全池平均（{STRAT_LABELS[max(uniform_avg,key=uniform_avg.get)]}）</div></div>
<div class="kpi"><div class="v">{' / '.join(f'{REGIME_LABEL.get(k,k)}{v}只' for k,v in regime_counts.items())}</div><div class="l">分类分布</div></div>
</div>
<h2>分类匹配 vs 统一策略（全池平均收益）</h2>
<p class="note">匹配策略 = 按量化分类结果选用策略；统一策略 = 全池都用同一个策略</p>
<table><tr><th>策略</th><th>全池平均收益</th></tr>{uniform_lis}
<tr style="background:#f8f7ff"><td><b>分类匹配（本功能）</b></td><td class="{'gain' if matched_avg_pool>=0 else 'loss'}"><b>{matched_avg_pool:+.4f}%</b></td></tr></table>
<h2>个股明细（按分类排序）</h2>
<table><tr><th>代码</th><th>名称</th><th>分类</th><th>轨外比例</th><th>带宽分位</th><th>偏离MA20</th>
{''.join(f'<th>{STRAT_LABELS[n]}</th>' for n in STRATEGIES)}<th>匹配均收益</th></tr>
{body_rows}</table>
</div></body></html>"""
    html_path = out_dir / f"regime_scan_{scale}.html"
    html_path.write_text(html, encoding="utf-8")

    print(f"=== 分类全池扫描 {scale}（{n}只）===")
    print("分类分布:", {REGIME_LABEL.get(k, k): v for k, v in regime_counts.items()})
    print(f"分类匹配策略 全池平均收益: {matched_avg_pool:+.4f}%  (事后最佳: {matched_best_pool:+.4f}%)")
    print("统一策略全池平均收益排名:")
    for name, v in sorted(uniform_avg.items(), key=lambda x: x[1], reverse=True):
        print(f"  {STRAT_LABELS[name]:8s} {v:+.4f}%")
    print(f"已保存: {csv_path}")
    print(f"已保存: {html_path}")


if __name__ == "__main__":
    main()
