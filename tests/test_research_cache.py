#!/usr/bin/env python3
"""pboc 研究缓存 TTL 离线自检（无网络，mock producer 计数）。

背景：research.py 的 pboc producer 曾写 fetchedAt="%Y-%m-%d %H:%M"（分钟级），
而 main.py `_research_produce` 用 "%Y-%m-%d %H:%M:%S" strptime 解析 → 恒
ValueError → except pass → TTL 恒失效，每次请求都重抓央行官网。
本脚本验证修复后：TTL 内命中缓存不重抓、过期重抓、旧分钟级格式自愈、
refresh=1 强制重抓，以及全部 producer 的 fetchedAt 格式与解析端一致。

运行: /opt/hermes-agent/.venv/bin/python tests/test_research_cache.py
退出码 0 = 全部通过。
"""
import asyncio
import json
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# main.py 有 SITE_PASSWORD 缺失 fail-fast（P2-3 修复），且仓库 .env 只在
# 生产机存在——import 前兜底注入假口令（os.environ 已有值时不覆盖；
# dotenv 不会覆盖已存在的 env 变量，故假值必然生效）。全程离线 mock，
# 口令不参与任何逻辑，仅满足模块导入前置检查。
import os  # noqa: E402
os.environ.setdefault("SITE_PASSWORD", "test-dummy-not-real")

import app.main as m  # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
PASS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))
    PASS.append(cond)
    if not cond:
        raise SystemExit(f"断言失败: {name} {detail}")


def write_cache(dirpath: Path, fetched_at: str, payload=None):
    data = payload or {"fetchedAt": fetched_at, "today": {"date": "2026-11-19"},
                       "balance": {"rr": 100.0, "bo": 50.0, "total": 150.0}}
    data["fetchedAt"] = fetched_at
    (dirpath / "research_pboc.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="pboc_ttl_test_"))
    orig_dir = m.RESEARCH_CACHE_DIR
    m.RESEARCH_CACHE_DIR = tmp  # 隔离到临时目录，不碰真实 data/
    calls = {"n": 0}

    async def counting_producer():
        calls["n"] += 1
        return {"fetchedAt": datetime.now().strftime(FMT),
                "today": {"date": "2026-11-19"}, "mock": True,
                "call_seq": calls["n"]}

    try:
        # 1) 新格式（带秒）+ TTL 内 → 命中缓存，producer 零调用
        now = datetime.now()
        cached = write_cache(tmp, (now - timedelta(minutes=10)).strftime(FMT))
        r = await m._research_produce("pboc", 360, 0, counting_producer)
        check("TTL内命中缓存不重抓", calls["n"] == 0 and r == cached,
              f"producer调用={calls['n']}, fetchedAt={r.get('fetchedAt')}")

        # 2) 连续多次请求仍命中（TTL 恒失效 bug 的直接反证）
        for _ in range(3):
            r = await m._research_produce("pboc", 360, 0, counting_producer)
        check("连续4次请求均命中缓存", calls["n"] == 0 and r == cached,
              f"producer累计调用={calls['n']}")

        # 3) TTL 过期 → 重抓一次并落盘新格式
        write_cache(tmp, (now - timedelta(minutes=400)).strftime(FMT))
        r = await m._research_produce("pboc", 360, 0, counting_producer)
        on_disk = json.loads((tmp / "research_pboc.json").read_text(encoding="utf-8"))
        check("TTL过期后重抓", calls["n"] == 1 and r.get("mock") is True,
              f"producer调用={calls['n']}")
        check("重抓后落盘为带秒新格式",
              datetime.strptime(on_disk["fetchedAt"], FMT) is not None
              and on_disk == r, f"落盘fetchedAt={on_disk['fetchedAt']}")

        # 4) 旧分钟级格式存量缓存 → 解析失败重抓一次（自愈），随后命中新缓存
        calls["n"] = 0
        write_cache(tmp, (now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M"))
        r = await m._research_produce("pboc", 360, 0, counting_producer)
        check("旧分钟级格式触发一次重抓(自愈)", calls["n"] == 1 and r.get("mock") is True,
              f"producer调用={calls['n']}")
        r2 = await m._research_produce("pboc", 360, 0, counting_producer)
        check("自愈后第二次请求命中新格式缓存", calls["n"] == 1 and r2 == r,
              f"producer累计调用={calls['n']}")

        # 5) refresh=1 强制重抓（即使 TTL 内）
        calls["n"] = 0
        write_cache(tmp, datetime.now().strftime(FMT))
        r = await m._research_produce("pboc", 360, 1, counting_producer)
        check("refresh=1强制重抓", calls["n"] == 1 and r.get("mock") is True,
              f"producer调用={calls['n']}")

        # 6) 源码格式守卫：research.py 所有 fetchedAt 写入必须与解析端 %H:%M:%S 一致
        src = (ROOT / "app" / "services" / "research.py").read_text(encoding="utf-8")
        writers = re.findall(r'"fetchedAt":\s*datetime\.now\(\)\.strftime\("([^"]+)"\)', src)
        check("全部producer的fetchedAt格式带秒", len(writers) >= 4 and all(w == FMT for w in writers),
              f"共{len(writers)}处: {writers}")
        main_src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        check("解析端格式为%Y-%m-%d %H:%M:%S",
              'strptime(cached.get("fetchedAt", ""), "%Y-%m-%d %H:%M:%S")' in main_src)
    finally:
        m.RESEARCH_CACHE_DIR = orig_dir

    print(f"\n结果: {sum(PASS)}/{len(PASS)} 通过")


if __name__ == "__main__":
    asyncio.run(main())
