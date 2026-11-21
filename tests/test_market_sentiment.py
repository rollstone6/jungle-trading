#!/usr/bin/env python3
"""大盘点金·广度阳谱与分段回测离线回归测试（t_ba8ee005，无网络）。

覆盖验收标准 3（降级路径）+ 2（分段输出结构）+ 文案动态注入：
a) 广度缓存缺失 → update_breadth unavailable + _compute_core 退 index_self，
   note 含「指数自身口径」「≈随机」标注；
b) 增量拉取失败（monkeypatch 网络函数抛异常）→ 旧缓存沿用 + stale + asOf=旧末日期，
   note 含「广度数据截至」；
c) count=0 退化：极短信号序列下 segments 全 0、文案无 NaN/0%、无统计句；
d) 分段结构：backtest.{gold,silver}.segments.{double,triple} 存在且
   double+triple count == 全量 count；
e) 银手指文案含真实随机基线对比数字（动态注入，非硬编码）。

数据全部合成（正弦+趋势指数、假广度、假力道），DB/缓存重定向 tempfile 目录，
生产 data/ 零触碰。

运行: /opt/hermes-agent/.venv/bin/python tests/test_market_sentiment.py
"""
import atexit
import json
import math
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

import app.services.quant_scan as qs

# ── P0 隔离：广度/扫描缓存目录重定向 tempfile ──
_TMP = Path(tempfile.mkdtemp(prefix="jt_sent_breadth_"))
qs.DATA_DIR = _TMP / "data"
qs.DATA_DIR.mkdir(parents=True)
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

import app.services.market_sentiment as ms

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    print(("PASS" if ok else "FAIL"), name, detail)
    if ok:
        PASS += 1
    else:
        FAIL += 1


def synth_index(n=900, end="2026-11-20"):
    d0 = datetime.strptime(end, "%Y-%m-%d") - timedelta(days=int(n * 1.5))
    dates = pd.bdate_range(d0, periods=n)
    close = [100 + 10 * math.sin(i / 25) + i * 0.02 + (3 if i % 90 < 45 else -3)
             for i in range(n)]
    return pd.DataFrame({"date": dates, "open": close, "high": [c * 1.01 for c in close],
                         "low": [c * 0.99 for c in close], "close": close,
                         "volume": [1e6 + 1e4 * (i % 7) for i in range(n)]})


def synth_breadth(dates, status="ok"):
    daily = {}
    for i, d in enumerate(dates):
        ds = d.strftime("%Y-%m-%d")
        daily[ds] = None if i % 10 == 3 else round(35 + 30 * math.sin(i / 17), 2)
    return {"status": status, "asOf": max(k for k, v in daily.items() if v is not None),
            "daily": daily}


NOW = datetime(2026, 11, 21, 12, 0, 0)
df = synth_index()
dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
fmap = {d: float((i % 11) - 5) * 0.6 for i, d in enumerate(dates[-100:])}  # 覆盖末100日

# ── a) 广度缓存缺失 → 指数口径 + 标注 ──
pay_a = ms._compute_core(df, {"status": "unavailable", "asOf": None, "daily": {}},
                         fmap, now=NOW)
check("a1 yangSource=index_self", pay_a["yangSource"] == "index_self")
check("a2 note 含降级标注", "指数自身口径" in pay_a["note"] and "≈随机" in pay_a["note"],
      pay_a["note"][:60])
# update_breadth：缓存文件不存在 → unavailable
r = ms.update_breadth(dates)
check("a3 update_breadth 无缓存→unavailable", r["status"] == "unavailable" and r["daily"] == {},
      str(r)[:80])

# ── b) 增量失败 → 旧缓存 + 截至标注 ──
seed = {"version": 1, "updated_at": "2026-11-20", "cons": [], "cons_updated_at": "2026-11-01",
        "daily": {d: 50.0 for d in dates[:-2]}}  # 缺最后2个交易日
(qs.DATA_DIR).mkdir(parents=True, exist_ok=True)
(qs.DATA_DIR / "sentiment_breadth.json").write_text(json.dumps(seed), encoding="utf-8")
_orig_today, _orig_backfill, _orig_cons = ms._today_breadth, ms._backfill_days, ms._ensure_cons
ms._ensure_cons = lambda d: ["600000"]
ms._today_breadth = lambda cons: (_ for _ in ()).throw(ms.BreadthUpdateFailed("模拟断网"))
ms._backfill_days = lambda cons, need: (_ for _ in ()).throw(RuntimeError("模拟断网"))
r2 = ms.update_breadth(dates)
check("b1 增量失败→stale", r2["status"] == "stale", r2["status"])
check("b2 旧缓存沿用+截至日", r2["asOf"] == dates[-3], f"asOf={r2['asOf']}")
pay_b = ms._compute_core(df, r2, fmap, now=NOW)
check("b3 note 含「沿用旧缓存」+截至日", "沿用旧缓存" in pay_b["note"] and r2["asOf"] in pay_b["note"])
# 成功路径冒烟：缺口回补可用 + 当日实时可用 → ok（_backfill_days 必须一并
# 打桩，否则走真实 akshare 网络）
ms._backfill_days = lambda cons, need: {d: 55.0 for d in need}
ms._today_breadth = lambda cons: 61.5
r3 = ms.update_breadth(dates)
check("b4 恢复后→ok 覆盖最新交易日", r3["status"] == "ok" and r3["asOf"] == dates[-1],
      f"{r3['status']}/{r3['asOf']}")
ms._today_breadth, ms._backfill_days, ms._ensure_cons = _orig_today, _orig_backfill, _orig_cons

# ── c) count=0 退化无 NaN ──
# 单调下跌 + 恒50广度：阳谱EMA=50 不穿越，力道全0 → 金/银手指条件都难满足
df_flat = synth_index(n=300).copy()
df_flat["close"] = 100.0
df_flat[["open", "high", "low"]] = 100.0
pay_c = ms._compute_core(df_flat, {"status": "ok", "asOf": dates[-1],
                                   "daily": {d.strftime("%Y-%m-%d"): 50.0 for d in df_flat["date"]}},
                         {}, now=NOW)  # 力道空
for kind in ("gold", "silver"):
    full = pay_c["backtest"][kind]
    segs = full["segments"]
    ok0 = all(("count" in x and x["count"] == 0) or x.get("hit_prob") is not None
              for x in [full, segs["double"], segs["triple"]])
    txt = json.dumps(full, allow_nan=False)  # 序列化即炸出 NaN
    check(f"c {kind} count=0 无NaN结构正常", ok0 and "NaN" not in txt, txt[:60])
from app.services.market_sentiment import _signal_text
t0 = _signal_text("silver", {"count": 0}, "double", dual=None,
                  baseline={"down_prob": 47.7})
check("c2 count=0 文案无统计句/NaN", "NaN" not in t0 and "0%" not in t0 and "概率" not in t0, t0)

# ── d) 分段结构守恒 ──
pay_d = ms._compute_core(df, synth_breadth(df["date"]), fmap, now=NOW)
for kind in ("gold", "silver"):
    full = pay_d["backtest"][kind]
    dd, tt = full["segments"]["double"], full["segments"]["triple"]
    if full["count"]:
        check(f"d {kind} double+triple=全量",
              dd.get("count", 0) + tt.get("count", 0) == full["count"],
              f"{dd.get('count', 0)}+{tt.get('count', 0)}={full['count']}")
        check(f"d {kind} hit_prob/ci 字段存在",
              "hit_prob" in full and "ci" in full and full["ci"].startswith("["))
check("d3 随机基线输出", pay_d["randomBaseline"]["down_prob"] is not None
      and 0 <= pay_d["randomBaseline"]["down_prob"] <= 100)

# ── e) 银手指文案动态注入基线数字 ──
silver_sigs = [s for s in pay_d["signals"] if s["finger"] == "银手指"]
if silver_sigs:
    st = silver_sigs[0]["text"]
    seg = silver_sigs[0]["seg"]
    seg_stat = pay_d["backtest"]["silver"]["segments"][seg]
    check("e1 银手指文案含同口径段命中率", f"{seg_stat['hit_prob']:g}%" in st, st[:100])
    check("e2 银手指文案含随机基线数字",
          "随机" in st and (f"{pay_d['randomBaseline']['down_prob']:g}%" in st), st[-120:])
    check("e3 tag=风险", silver_sigs[0]["tag"] == "风险")
else:
    check("e0 合成数据出银手指", False, "synthetic data produced no silver; 需换 seed")

# gold 文案=同段统计+CI
gold_sigs = [s for s in pay_d["signals"] if s["finger"] == "金手指"]
if gold_sigs:
    gt = gold_sigs[0]["text"]
    gseg = gold_sigs[0]["seg"]
    g_stat = pay_d["backtest"]["gold"]["segments"][gseg]
    check("e4 金手指文案引用同段数字+CI", f"{g_stat['hit_prob']:g}%" in gt and g_stat["ci"] in gt,
          gt[:110])

# ── f) 跨日 provisional 冻结回归（t_73d0235d P1）+ provisional 披露 ──
# 缺陷场景：T日盘中写 provisional 后无人访问，T+1 起旧删除条件（三者相等）永不
# 成立 → T 日盘中值永久冻结冒充定稿且无披露。修复后按日期比较+收盘判定。
class _FD(datetime):
    _frozen = (2026, 11, 23, 10, 0)
    @classmethod
    def now(cls, tz=None): return cls(*cls._frozen)

_orig_dt = ms.datetime
ms.datetime = _FD
try:
    bd_f = {"version": 1, "updated_at": "2026-11-20", "cons": ["600000"],
            "cons_updated_at": "2026-11-01",
            "daily": {"2026-11-18": 50.0, "2026-11-19": 51.0, "2026-11-20": 52.0},
            "provisional_date": "2026-11-20"}  # 周五11-20盘中快照 52.0
    (qs.DATA_DIR / "sentiment_breadth.json").write_text(json.dumps(bd_f), encoding="utf-8")
    _oc, _ot, _ob = ms._ensure_cons, ms._today_breadth, ms._backfill_days
    ms._ensure_cons = lambda d: ["600000"]
    ms._today_breadth = lambda cons: 60.0
    ms._backfill_days = lambda cons, need: {x: 55.0 for x in need}  # 收盘定稿回补值
    FDATES = ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-23"]
    try:
        rf = ms.update_breadth(FDATES)  # 周一 10:00 运行
        dfr = json.loads((qs.DATA_DIR / "sentiment_breadth.json").read_text(encoding="utf-8"))
        check("f1 跨日provisional被定稿重算(52→55)不冻结",
              dfr["daily"]["2026-11-20"] == 55.0, str(dfr["daily"]))
        check("f2 当日盘中新provisional标记", dfr.get("provisional_date") == "2026-11-23",
              str(dfr.get("provisional_date")))
        check("f3 update_breadth返回值带provisionalDate", rf.get("provisionalDate") == "2026-11-23",
              str(rf.get("provisionalDate")))
        pay_f = ms._compute_core(df, rf, fmap, now=datetime(2026, 11, 23, 10, 0))
        check("f4 payload.breadth.provisionalDate透传",
              pay_f["breadth"].get("provisionalDate") == "2026-11-23", str(pay_f["breadth"]))
        check("f5 note含盘中暂定值披露", "盘中暂定值" in pay_f["note"], pay_f["note"][:70])
        # 同日收盘后正常定稿路径不回归：16:00 再运行 → 盘中值删除重算+标记清除
        bd_g = {"version": 1, "updated_at": "2026-11-23", "cons": ["600000"],
                "cons_updated_at": "2026-11-01",
                "daily": {"2026-11-18": 50.0, "2026-11-19": 51.0, "2026-11-20": 55.0,
                          "2026-11-23": 60.0},
                "provisional_date": "2026-11-23"}  # 周一盘中 60.0
        (qs.DATA_DIR / "sentiment_breadth.json").write_text(json.dumps(bd_g), encoding="utf-8")
        _FD._frozen = (2026, 11, 23, 16, 0)
        ms._today_breadth = lambda cons: 56.0  # 收盘后实时拉=定稿收盘价
        rg = ms.update_breadth(FDATES)
        dgr = json.loads((qs.DATA_DIR / "sentiment_breadth.json").read_text(encoding="utf-8"))
        check("f6 同日收盘后定稿重算(60→56)+标记清除",
              dgr["daily"]["2026-11-23"] == 56.0 and "provisional_date" not in dgr,
              str(dgr["daily"]))
        check("f7 定稿后返回值无provisionalDate", rg.get("provisionalDate") is None,
              str(rg.get("provisionalDate")))
        pay_g = ms._compute_core(df, rg, fmap, now=datetime(2026, 11, 23, 16, 0))
        check("f8 定稿后note无暂定值标注（不误报）", "盘中暂定值" not in pay_g["note"])
    finally:
        ms._ensure_cons, ms._today_breadth, ms._backfill_days = _oc, _ot, _ob
finally:
    ms.datetime = _orig_dt

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
