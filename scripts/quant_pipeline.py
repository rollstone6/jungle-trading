#!/usr/bin/env python
"""量化管线 CLI：python scripts/quant_pipeline.py [ingest|run] [fast slow]

run    = 完整闭环（拉行情→DuckDB→VectorBT→落库→写 data/pipeline_result.json）
ingest = 只更新行情库
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import quant_pipeline as qp  # noqa: E402


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    fast = int(sys.argv[2]) if len(sys.argv) > 2 else qp.DEFAULT_FAST
    slow = int(sys.argv[3]) if len(sys.argv) > 3 else qp.DEFAULT_SLOW
    if mode == "ingest":
        stats = asyncio.run(qp.ingest(
            [{"symbol": s["symbol"], "name": s["name"]} for s in __import__(
                "app.quant.config", fromlist=["STOCK_LIST"]).STOCK_LIST]))
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        payload = asyncio.run(qp.run_pipeline(fast, slow, trigger="cli"))
        print(f"run_id={payload['run_id']} ok={len(payload['stocks'])} failed={payload['failed']}")
        for r in payload["stocks"]:
            m = r["metrics"]
            print(f"  {r['code']} {r['name']:<6} bars={r['bars']} "
                  f"ret={m['total_return']}% dd={m['max_drawdown']}% "
                  f"sharpe={m['sharpe']} trades={m['trade_count']}")


if __name__ == "__main__":
    main()
