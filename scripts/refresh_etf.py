#!/usr/bin/env python3
"""定时任务 - 刷新 ETF 申购净流入（份额法，交易所官方数据）"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.etf_flow import refresh


async def main():
    result = await refresh()
    print(
        f"ETF申购净流入刷新完成: 数据日期 {result['data_date']} | "
        f"参与排名 {result['total_etfs']} 只 | "
        f"净申购 {result['inflow_count']} 只(+{result['total_inflow']/1e8:.1f}亿) / "
        f"净赎回 {result['outflow_count']} 只({result['total_outflow']/1e8:.1f}亿)"
    )
    for it in result["top_inflow"][:5]:
        print(f"  +{it['net_inflow']/1e8:8.2f}亿  {it['code']} {it['name']}")


if __name__ == "__main__":
    asyncio.run(main())
