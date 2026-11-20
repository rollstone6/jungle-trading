#!/usr/bin/env python3
"""走势 × 波动 合并面板后端集成回归测试（离线 mock，无网络）。

覆盖 t_1ad2d251 验收标准：
- /api/regime/latest 每只 stock 含 vol 字段，且与 calc_volatility_watch 对
  同一 K 线序列的输出逐项一致（单一口径，禁止漂移）；
- combined 组合解读字段与 volatility.combined_read 一致；
- 旧缓存（无 vol 字段）视为过期 → 自动重扫，不 500；
- 新缓存（含 vol 且当日、覆盖持仓）命中直接返回，不重扫。

不走 HTTP（避免 TestClient 启动 scheduler / 鉴权），直接 await 路由协程，
fetcher 用 monkeypatch 注入合成 K 线。DB 与 regime 扫描缓存全部重定向到
tempfile 临时目录（见下方隔离块），init_db/get_db/缓存读写只作用于临时
路径，生产库与生产缓存零触碰；teardown 只删临时目录。

运行: cd 仓库根 && /opt/hermes-agent/.venv/bin/python -m pytest tests/test_regime_vol.py -q
或直接 /opt/hermes-agent/.venv/bin/python tests/test_regime_vol.py
"""
import asyncio
import atexit
import json
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# main.py 有 SITE_PASSWORD 缺失 fail-fast；测试口令不参与任何逻辑。
os.environ.setdefault("SITE_PASSWORD", "test-dummy-n… real")

import app.main as m  # noqa: E402
import app.models.database as db_mod  # noqa: E402
import app.services.market as market_mod  # noqa: E402
from app.models.database import init_db, get_db  # noqa: E402
from app.services.volatility import calc_volatility, combined_read  # noqa: E402
from app.services.portfolio import calc_volatility_watch  # noqa: E402
from app.services.regime import REGIME_LABEL  # noqa: E402

# ── P0 隔离（审查 t_c86fbad9）：DB 与 regime 缓存重定向到 tempfile 目录 ──
# database.get_db()/init_db() 与 main.regime_latest() 都在调用时刻读取模块级
# DB_PATH / REGIME_CACHE_FILE（非 cwd 解析），这里在任何首次 DB/缓存操作前
# patch 两个模块属性即全局生效；teardown 只删临时目录，生产库与生产缓存
# 零触碰（主仓、worktree 一律安全）。
_TMP_DIR = Path(tempfile.mkdtemp(prefix="jt_regime_vol_"))
db_mod.DB_PATH = _TMP_DIR / "jungle.db"
m.REGIME_CACHE_FILE = _TMP_DIR / "regime_scan.json"

atexit.register(shutil.rmtree, _TMP_DIR, ignore_errors=True)

HELD = [("600001", "测试甲"), ("000002", "测试乙")]
# STOCK_LIST 其余票的 K 线用同一生成器（mock 不区分代码）
WAVE = 0.3


def _klines_daily(code: str, days: int = 550, full_date: bool = False):
    """确定性合成日K（含 OHLC，close 波动足以让 classify_regime 出结果）。
    末根日期 = 今天，保证 regime_latest 当日缓存命中判断可通过。"""
    out = []
    seed = sum(ord(c) for c in code)
    today = datetime.now().strftime("%Y-%m-%d")
    for i in range(days):
        c = 10 + WAVE * math.sin((i + seed) / 9.0) + 0.002 * i
        date = today if i == days - 1 else f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}"
        out.append({"date": date,
                    "open": c - 0.05, "high": c + 0.2, "low": c - 0.2,
                    "close": c, "volume": 1000 + i})
    return out


async def _fake_tencent(code, days=120, full_date=False):
    return _klines_daily(code, days)


async def _fake_sina_minute(code, scale=30):
    return _klines_daily(code + "m", 300)


def _setup_db():
    # 保险丝：隔离未生效（DB/缓存仍指仓库 data/）直接拒绝运行，绝不写生产库。
    assert db_mod.DB_PATH.resolve().parent == _TMP_DIR, \
        f"DB 隔离失效: {db_mod.DB_PATH}"
    assert m.REGIME_CACHE_FILE.resolve().parent == _TMP_DIR, \
        f"缓存隔离失效: {m.REGIME_CACHE_FILE}"
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM positions")
    for code, name in HELD:
        conn.execute(
            "INSERT INTO positions (code, name, sector, quantity, cost_price) VALUES (?,?,?,?,?)",
            (code, name, "测试", 100, 10.0))
    conn.commit()
    conn.close()


def _clean_cache():
    if m.REGIME_CACHE_FILE.exists():
        m.REGIME_CACHE_FILE.unlink()


async def main():
    _setup_db()
    _clean_cache()
    market_mod.fetch_kline_tencent = _fake_tencent
    market_mod.fetch_kline_sina_minute = _fake_sina_minute

    results = []

    def check(name, cond, detail=""):
        results.append(cond)
        print(("✓ " if cond else "✗ ") + name + (f"  [{detail}]" if detail and not cond else ""))

    # 1) 冷缓存 → 现场扫描，返回含 vol/combined，不抛错
    data = await m.regime_latest(request=None, pwd="x", refresh=0)
    stocks = data["stocks"]
    check("扫描返回全部标的", len(stocks) > 0, str(len(stocks)))
    check("每只 stock 含 vol 字段", all("vol" in s for s in stocks))
    check("held 标的在结果中", {c for c, _ in HELD} <= {s["code"] for s in stocks})

    # 2) 口径一致性：regime vol == portfolio.calc_volatility_watch 同序列输出
    by_code = {s["code"]: s for s in stocks}
    ok = True
    for code, name in HELD:
        kl = _klines_daily(code)
        p = calc_volatility_watch([{"code": code, "kline": kl}])[0]
        s_vol = by_code[code]["vol"]
        if s_vol != p["vol"]:
            ok = False
            print(f"  漂移 {code}: api={s_vol} portfolio={p['vol']}")
    check("vol 与 calc_volatility_watch 逐项一致", ok)

    # 3) combined 与 combined_read 一致（含 None 态）
    check("combined 字段同口径",
          all(s.get("combined") == combined_read(s["regime"], s["vol"]) for s in stocks))

    # 4) 落盘缓存含 vol；当日命中直接返回（fetcher 计数不增长）
    cached = json.loads(m.REGIME_CACHE_FILE.read_text(encoding="utf-8"))
    check("缓存已落盘且含 vol", all("vol" in s for s in cached["stocks"]))
    calls = {"n": 0}

    async def counting_tencent(code, days=120, full_date=False):
        calls["n"] += 1
        return await _fake_tencent(code, days)

    market_mod.fetch_kline_tencent = counting_tencent
    data2 = await m.regime_latest(request=None, pwd="x", refresh=0)
    check("当日新缓存命中不重扫", calls["n"] == 0 and data2["stocks"] == stocks,
          f"calls={calls['n']}")

    # 5) 旧缓存兼容：无 vol 字段 + 当日 asOf → 视为过期自动重扫，不 500
    old = dict(cached)
    old["stocks"] = [{k: v for k, v in s.items() if k not in ("vol", "combined")}
                     for s in cached["stocks"]]
    m.REGIME_CACHE_FILE.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    calls["n"] = 0
    data3 = await m.regime_latest(request=None, pwd="x", refresh=0)
    check("旧缓存(无vol)自动重扫", calls["n"] > 0 and all("vol" in s for s in data3["stocks"]),
          f"calls={calls['n']}")

    # 6) 前端契约字段齐全：label/vol.hv20 等渲染所需键
    s0 = data3["stocks"][0]
    need = {"code", "name", "regime", "label", "regime_daily", "label_30m",
            "outside_ratio", "bandwidth_pct", "ma_dev_pct", "vol", "combined"}
    check("渲染契约字段齐全", need <= set(s0), str(sorted(need - set(s0))))
    vol_keys = {"hv20", "hv60", "pctile", "atr_pct", "state", "state_cls", "alert", "alert_text"}
    check("vol 结构键齐全", vol_keys <= set(s0["vol"]), str(sorted(vol_keys - set(s0["vol"]))))

    # 7) label 与 REGIME_LABEL 同步
    check("label 用词与后端一致",
          all(s["label"] == REGIME_LABEL[s["regime"]] for s in data3["stocks"]))

    # 8) summary 含四分类计数
    check("counts 四分类齐全", {"squeeze", "trending", "ranging", "neutral"} <= set(data3["counts"]))

    # 还原：仅清临时目录内的缓存与持仓（生产库/缓存从未被写入，见顶部隔离块；
    # 临时目录整体由 atexit shutil.rmtree 删除）
    _clean_cache()
    conn = get_db()
    conn.execute("DELETE FROM positions")
    conn.commit()
    conn.close()

    print(f"\n结果: {sum(results)}/{len(results)} 通过")
    return all(results)


def test_regime_vol_integration():
    """pytest 入口：包装协程 main()，任一断言失败即 assert。"""
    assert asyncio.run(main()) is True, "详见上方 ✓/✗ 明细"


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
