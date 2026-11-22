#!/usr/bin/env python3
"""广度阳谱种子缓存生成器（t_ba8ee005，一次性部署工具）。

从研究卡 t_6db7abf2 的 breadth_daily.csv（沪深300成分股自算，300/300成功）
转换生成 data/sentiment_breadth.json 种子，避免生产重新拉取 300 只成分股
5 年历史（研究卡红线：禁止重拉）。成分股列表用 akshare index_stock_cons
实时拉一次存入缓存（半年更新一次的机制由 market_sentiment 运行时维护）。

用法：
  /opt/hermes-agent/.venv/bin/python scripts/build_breadth_seed.py <breadth_daily.csv路径>
生成后回读打印覆盖范围与条数（不打印敏感内容，本文件无口令）。
"""
import csv
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"seed source not found: {src}")
        return 2

    daily: dict[str, float | None] = {}
    with src.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["date"][:10]
            r = (row.get("ratio") or "").strip()
            try:
                daily[d] = round(float(r), 2)
            except ValueError:
                daily[d] = None  # 无有效家数的日子（如窗口首行）

    # 成分股列表（一次轻接口；失败不阻塞种子生成，运行时会补拉）
    cons: list[str] = []
    cons_today = None
    try:
        import akshare as ak
        cons = [str(c).zfill(6) for c in ak.index_stock_cons(symbol="000300")["品种代码"].tolist()]
        cons_today = date.today().isoformat()
    except Exception as exc:  # noqa: BLE001
        print(f"WARN 成分股列表拉取失败（运行时会自动补拉）: {exc}")

    payload = {
        "version": 1,
        "source": "hs300_cons_daily_selfcomputed",
        "updated_at": date.today().isoformat(),
        "cons": cons,
        "cons_updated_at": cons_today,
        "daily": dict(sorted(daily.items())),
        "note": "沪深300成分股当日上涨家数占比%（未复权close pct_change>0，自算广度近似；种子=研究卡t_6db7abf2 breadth_daily.csv）",
    }
    out = ROOT / "data" / "sentiment_breadth.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    good = {d: v for d, v in daily.items() if v is not None}
    print(f"saved {out}  days={len(daily)} valid={len(good)} cons={len(cons)} "
          f"range={min(daily)}..{max(daily)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
