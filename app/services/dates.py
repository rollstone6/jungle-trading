"""K线 bar 日期解析公共模块（审计 t_edf48869 P1-5）。

持仓缓存 kline_data 存 MM-DD 短日期（market.py 归一化产物），旧消费点朴素补
"当前年"，跨年样本（如 12-31 后接 01-02）排序后时序颠倒。统一改为单调性推年：
以末根≈今天为锚向前倒推，月份回跳即减一年——与 quant_pipeline._repair_mmdd
同口径（那是 lab 级联专用、含源特性推断的实现，勿合并、勿动）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, List, Optional

import pandas as pd


def parse_bar_date(dates: Iterable[str], anchor: Optional[datetime] = None) -> List[pd.Timestamp]:
    """把一组 K 线 bar 日期（MM-DD 或完整 YYYY-MM-DD[ HH:MM:SS] 混合）归一为 Timestamp 列表。

    - 完整日期：原样解析，并作为向前倒推的（年, 月）锚点。
    - MM-DD：从末根向前倒推年份。默认锚=当前时间（末根 bar ≈ 今天）；
      月份回跳（MM 大于参考月）视为跨过新年界，年份 -1。
    """
    vals = [str(d).strip() for d in dates]
    n = len(vals)
    out: List[Optional[pd.Timestamp]] = [None] * n

    # 第一遍：完整日期直接解析
    for i, d in enumerate(vals):
        if len(d) >= 10:
            try:
                out[i] = pd.Timestamp(d)
            except ValueError:
                pass  # 残破值落回 MM-DD/NaT 处理

    # 第二遍：自尾向前倒推，维护 (year, month) 参考
    now = anchor or datetime.now()
    year, month = now.year, now.month
    for i in range(n - 1, -1, -1):
        if out[i] is not None:
            ts = out[i]
            year, month = ts.year, ts.month
            continue
        d = vals[i]
        try:
            mm, dd = int(d[:2]), int(d[3:5])
        except (ValueError, IndexError):
            out[i] = pd.NaT
            continue
        if mm > month:  # 向前回退跨过一年边界
            year -= 1
        month = mm
        try:
            out[i] = pd.Timestamp(year=year, month=mm, day=dd)
        except ValueError:
            out[i] = pd.NaT
    return out
