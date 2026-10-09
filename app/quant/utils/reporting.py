"""控制台报告输出模块。

把原先分散在 TrendFilter / ChipDistributionAnalyzer / BoxDetector 中的
打印逻辑集中到这里，工具类只保留核心计算与扫描编排：
- 趋势筛选报告（单股明细 + 汇总）
- 筹码分布扫描报告
- 箱体检测报告（单股 + 扫描头尾）
- 通用分节标题与单行进度条

所有函数均为纯输出，不参与计算，可在 Web 场景下安全忽略（传
print_detail=False 即不会被调用）。
"""

from __future__ import annotations

import sys
import time


# ----------------------------------------------------------------------
# 通用
# ----------------------------------------------------------------------

def section(title: str, width: int = 60) -> None:
    """打印分节标题。"""
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def progress_line(label: str, completed: int, total: int,
                  start_time: float, width: int = 30) -> None:
    """单行刷新的进度条（不换行）。"""
    pct = completed / total if total else 0
    filled = int(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    elapsed = time.time() - start_time
    if completed > 0:
        eta = elapsed * (total - completed) / completed
        eta_str = f"{int(eta) // 60}分{int(eta) % 60:02d}秒"
    else:
        eta_str = "--:--"
    sys.stdout.write(
        f"\r  {label}: [{bar}] {completed}/{total} ({pct:.0%}) "
        f"ETA: {eta_str}  "
    )
    sys.stdout.flush()


# ----------------------------------------------------------------------
# 趋势筛选（TrendFilter）
# ----------------------------------------------------------------------

def print_trend_stock_report(cfg, name, symbol, result) -> None:
    """打印单只股票的趋势筛选报告。"""
    print("\n  {}({})".format(name, symbol))
    print("  " + "-" * 40)

    d = result.get('detail', {})

    # ① 均线多头
    if result['ma_bullish']:
        print("    ①均线多头 ✅ MA5({})>MA10({})>MA20({}), "
              "Close({})>MA5".format(
                  d.get('ma5', '-'), d.get('ma10', '-'),
                  d.get('ma20', '-'), d.get('close', '-'),
              ))
    else:
        print("    ①均线多头 ❌ MA5={}, MA10={}, MA20={}, "
              "Close={}".format(
                  d.get('ma5', '-'), d.get('ma10', '-'),
                  d.get('ma20', '-'), d.get('close', '-'),
              ))

    # ② MA20向上
    if result['ma20_up']:
        print("    ②MA20向上 ✅ MA20: {} > {}({}日前)".format(
            d.get('ma20', '-'), d.get('ma20_prev', '-'),
            cfg.trend_ma20_lookback,
        ))
    else:
        print("    ②MA20向上 ❌ MA20: {} <= {}({}日前)".format(
            d.get('ma20', '-'), d.get('ma20_prev', '-'),
            cfg.trend_ma20_lookback,
        ))

    # ③ 20日新高附近
    threshold = cfg.trend_high_threshold
    if result['near_high']:
        print("    ③20日新高 ✅ Close {} >= High20 {}*{}".format(
            d.get('close', '-'), d.get('high20', '-'), threshold,
        ))
    else:
        print("    ③20日新高 ❌ Close {} < High20 {}*{}".format(
            d.get('close', '-'), d.get('high20', '-'), threshold,
        ))

    # ④ 量能
    vol_ratio = cfg.trend_vol_min_ratio
    if result['vol_ok']:
        print("    ④量能正常 ✅ Vol {} >= MA5Vol {}*{}".format(
            d.get('volume', '-'), d.get('vol_ma', '-'), vol_ratio,
        ))
    else:
        print("    ④量能正常 ❌ Vol {} < MA5Vol {}*{}".format(
            d.get('volume', '-'), d.get('vol_ma', '-'), vol_ratio,
        ))

    # 综合判定
    if result['pass']:
        print("    → ✅ 通过")
    else:
        reasons = ', '.join(result['reasons'])
        print("    → ❌ 未通过 ({})".format(reasons))


def print_trend_summary(total, passed, failed) -> None:
    """打印趋势筛选汇总报告。"""
    section("筛选汇总")
    print("  股票池总数：{} 只".format(total))
    print("  通过筛选：{} 只".format(len(passed)))
    print("  未通过：{} 只".format(len(failed)))

    if passed:
        print("\n  通过筛选的股票：")
        for s in passed:
            print("    - {} ({})".format(s['name'], s['symbol']))

    if failed:
        print("\n  被过滤的股票：")
        for s in failed:
            print("    - {} ({})".format(s['name'], s['symbol']))

    print("=" * 60)


# ----------------------------------------------------------------------
# 筹码分布（ChipDistributionAnalyzer）
# ----------------------------------------------------------------------

def print_chip_scan_report(results) -> None:
    """打印筹码峰集中度扫描报告。"""
    section("筹码峰集中度扫描结果", width=70)

    if not results:
        print("  未找到符合条件的股票")
        print("=" * 70)
        return

    header = (
        f"  {'排名':<4} {'股票':<16} {'现价':<8} "
        f"{'峰值价':<8} {'90%集中':<8} {'70%集中':<8} "
        f"{'中心偏离%':<9} {'主峰%':<7} {'峰度':<6} {'评分':<6}"
    )
    print(header)
    print("  " + "-" * 78)

    for i, r in enumerate(results, 1):
        m = r['metrics']
        name = r['name'][:6] if len(r['name']) > 6 else r['name']
        row = (
            f"  {i:<4} {name}({r['symbol']}) "
            f"{r['close']:<8.2f} {m['peak_price']:<8.2f} "
            f"{m['conc_90']:<8.1f} {m['conc_70']:<8.1f} "
            f"{m['center_offset']:<9.2f} "
            f"{m['peak_ratio']:<7.1f} "
            f"{m['kurtosis']:<6.1f} {m['score']:<6.1f}"
        )
        print(row)

    print("  " + "-" * 78)

    passed = [r for r in results if r['metrics']['pass']]
    print(f"\n  通过筛选：{len(passed)} 只")
    for r in passed:
        print(f"    ✅ {r['name']} ({r['symbol']})")

    print("=" * 70)


# ----------------------------------------------------------------------
# 箱体检测（BoxDetector）
# ----------------------------------------------------------------------

def print_box_report(box_bars: int, stock_name: str, symbol: str,
                     box_info: dict) -> None:
    """打印单只股票的箱体检测报告。"""
    print("\n" + "=" * 50)
    print("  股票：{} ({})".format(stock_name, symbol))
    print("  数据级别：日K线")
    print("  箱体周期：最近 {} 根K线".format(box_bars))
    print("-" * 50)

    if not box_info.get('has_box'):
        error_msg = box_info.get('error', '未知原因')
        print("  【箱体状态】❌ 未形成箱体")
        print("  【原因】{}".format(error_msg))
        print("=" * 50)
        return

    print("  【箱体状态】✅ 处于箱体震荡中")
    print("  【箱体趋势】{}".format(box_info['trend']))
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  箱体上沿：{:>8.2f} 元                    │".format(box_info['box_high']))
    print("  │  箱体下沿：{:>8.2f} 元                    │".format(box_info['box_low']))
    print("  │  箱体中轴：{:>8.2f} 元                    │".format(box_info['box_mid']))
    print("  │  箱体宽度：{:>7.2f}%                      │".format(box_info['box_width']))
    print("  └─────────────────────────────────────────┘")
    print()
    print("  【当前价格】{:.2f} 元".format(box_info['current_price']))
    print("  【价格位置】{:.1f}% ({})".format(
        box_info['price_position'], box_info['price_hint']
    ))
    print("  【操作建议】{}".format(box_info['action_hint']))
    print()
    print("  【箱体时间】{} ~ {}".format(
        box_info['box_start_date'], box_info['box_end_date']
    ))
    print("  【波动率】{:.2f}%".format(box_info['volatility']))
    print()
    print("  【箱体质量】")
    print("    - 触及上沿次数：{}次".format(box_info['touch_high_count']))
    print("    - 触及下沿次数：{}次".format(box_info['touch_low_count']))
    print("    - 震荡评分：{}".format(box_info['quality_text']))
    print("=" * 50)


def print_box_scan_header(total: int, box_bars: int) -> None:
    """打印箱体批量扫描开头信息。"""
    section("箱体检测 - 批量扫描", width=70)
    print("  股票池：{} 只 | 箱体周期：{} 根日K线".format(total, box_bars))
    print("=" * 70)


def print_box_scan_summary(total_scanned: int, box_stocks: list) -> None:
    """打印箱体批量扫描汇总。"""
    section("扫描汇总", width=70)
    print("  扫描股票数：{}".format(total_scanned))
    print("  处于箱体：{}".format(len(box_stocks)))

    if box_stocks:
        print("\n  处于箱体的股票（按评分排序）：")
        print("-" * 70)
        print("  {:<12} {:>8} {:>8} {:>6} {:>6} {:>8}".format(
            "股票", "上限", "下限", "宽度%", "位置%", "评分"
        ))
        print("-" * 70)
        sorted_stocks = sorted(
            box_stocks, key=lambda x: x['quality_score'], reverse=True,
        )
        for info in sorted_stocks:
            print("  {:<10} {:>8.2f} {:>8.2f} {:>6.1f} {:>6.1f} {:>8}".format(
                "{}({})".format(info['name'], info['symbol']),
                info['box_high'], info['box_low'],
                info['box_width'], info['price_position'],
                info['quality_text'].split()[0]
            ))
        print("-" * 70)

        print("\n  操作建议：")
        for info in sorted_stocks:
            print("    {}({}): {} | {}".format(
                info['name'], info['symbol'],
                info['price_hint'], info['action_hint']
            ))
    print("=" * 70)
