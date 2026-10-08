#!/usr/bin/env python3
"""定时任务 - 刷新行情数据"""
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# 允许从仓库任意位置运行：python scripts/refresh.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.database import get_db, init_db
from app.services.market import fetch_realtime_tencent, fetch_kline_baidu, fetch_news_eastmoney, calc_ma, calc_volume_ratio

async def refresh_all():
    """刷新所有持仓的行情数据"""
    conn = get_db()
    positions = conn.execute("SELECT code FROM positions").fetchall()
    
    if not positions:
        print("无持仓数据")
        return
    
    codes = [p["code"] for p in positions]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 刷新行情: {codes}")
    
    # 获取实时行情
    quotes = await fetch_realtime_tencent(codes)
    
    for code in codes:
        q = quotes.get(code, {})
        if not q:
            continue
            
        # 更新实时价格
        conn.execute("""
            UPDATE positions 
            SET latest_price=?, change_pct=?, high=?, low=?, open_price=?, volume=?,
                quote_time=?, updated_at=CURRENT_TIMESTAMP
            WHERE code=?
        """, (
            q.get("latest_price", 0),
            q.get("change_pct", 0),
            q.get("high", 0),
            q.get("low", 0),
            q.get("open_price", 0),
            q.get("volume", 0),
            q.get("quote_time", ""),
            code
        ))
        
        # 检查是否需要更新K线（每天一次）
        row = conn.execute("SELECT kline_data FROM positions WHERE code=?", (code,)).fetchone()
        kline_data = json.loads(row[0]) if row else []
        
        # 如果K线数据少于60天或最后一天不是今天，更新K线
        today = datetime.now().strftime("%m-%d")
        need_kline_update = len(kline_data) < 60 or (kline_data and kline_data[-1].get("date") != today)
        
        if need_kline_update:
            klines = await fetch_kline_baidu(code, 120)
            if klines:
                ma5 = calc_ma(klines, 5)
                ma20 = calc_ma(klines, 20)
                ma60 = calc_ma(klines, 60)
                volume_ratio = calc_volume_ratio(klines)
                
                conn.execute("""
                    UPDATE positions 
                    SET kline_data=?, ma5=?, ma20=?, ma60=?, volume_ratio=?
                    WHERE code=?
                """, (json.dumps(klines), ma5, ma20, ma60, volume_ratio, code))
                print(f"  ✓ {code} K线已更新")
    
    conn.commit()
    conn.close()
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 刷新完成")

def main():
    init_db()  # 首次运行时自动建库建表
    asyncio.run(refresh_all())

if __name__ == "__main__":
    main()
