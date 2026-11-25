#!/usr/bin/env python3
"""央行买断式公告（栏目 5492845）在线核查——断言A的可复跑版本。

真抓列表 + 全部正文，断言 bo_ops ≥12 条且含探针实测锚点
（2026-11-16/2000亿/6个月/到期2027-05-15、2026-11-05/3000亿/3个月/到期2027-02-04，
金额期限以官网为准，官网若续发新公告锚点只会向前滚动，失败时打印全量表供人工判读）。

运行: cd 仓库根 && /opt/hermes-agent/.venv/bin/python scripts/pboc_bo_live_check.py
上游失败即非零退出（不做静默兜底）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.services.research import (  # noqa: E402
    BO_LIST, UA, _decode, _fetch_bo_list, _parse_bo_article,
)


async def main() -> int:
    bo_ops = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        pairs = await _fetch_bo_list(client)
        print(f"BO_LIST={BO_LIST}\nlist_pairs={len(pairs)}")
        if len(pairs) < 12:
            print("FAIL | 列表条目 <12（栏目结构可能又变了）")
            return 1
        resps = await asyncio.gather(*[client.get(u, headers=UA) for u, _ in pairs],
                                     return_exceptions=True)
        for (url, ld), r in zip(pairs, resps):
            if isinstance(r, BaseException):
                print(f"FETCH_FAIL {url}: {r}")
                continue
            resp: httpx.Response = r
            for o in _parse_bo_article(_decode(resp)):
                o["date"] = o.get("date") or ld
                bo_ops.append(o)
    bo_ops = [o for o in bo_ops if o.get("date") and o.get("date_maturity")]
    bo_ops.sort(key=lambda x: x["date"], reverse=True)
    print(f"bo_ops={len(bo_ops)}")
    for o in bo_ops:
        print(f'  op={o["date"]} amt={o["amount"]:g} term={o.get("term")} mat={o["date_maturity"]}')
    ok = len(bo_ops) >= 12
    for d, amt, term, mat in [
        ("2026-11-16", 2000.0, "6个月", "2027-05-15"),
        ("2026-11-05", 3000.0, "3个月", "2027-02-04"),
    ]:
        hit = any(o["date"] == d and o["amount"] == amt and o.get("term") == term
                  and o["date_maturity"] == mat for o in bo_ops)
        print(f"{'PASS' if hit else 'FAIL'} | bo_ops 含 {d}/{amt:g}/{term}/到期{mat}")
        ok = ok and hit
    print("RESULT:", "ALL PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
