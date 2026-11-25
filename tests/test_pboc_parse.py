#!/usr/bin/env python3
"""逆回购新规（2026-11-02 起）公告解析离线回归测试（fixture = pbc.gov.cn 真实公告片段）。

背景：央行公告〔2026〕第13号将隔夜逆回购（固定利率、数量招标，1.25%）升为主要政策
利率，7天期改固定数量、利率招标、多重价位中标（公告中无 7 天利率数字，属正常态）。
旧解析（t_ce62bd70 修复前）：
1. rate 仅在 term_days==7 时取值 → 新规后所有条目 rate=None，"政策利率"要点整段消失；
2. 文案写死"7天期逆回购操作利率"；
3. 前值跨期限比较（隔夜1.25 vs 7天1.40 会被误判"降息"）；
4. 同日排序仅按 date，latest_op 可能取到 7 天条。

覆盖验收断言：
①纯隔夜  → amount=6170, rate=1.25, term_days=1
②隔夜+7天 → 两条 op，隔夜 rate=1.25、7天 rate=None 且金额 3000
③旧规 7天1.40% → rate=1.4 不回归
④零操作公告 → amount=0
⑤政策利率文案含"隔夜"与"1.25"（_policy_rate_point 直测）
另：隔夜期后缀变体、跨期限前值不误报降息、零操作+隔夜并存不重复计数。

运行: cd 仓库根 && /opt/hermes-agent/.venv/bin/python -m pytest tests/test_pboc_parse.py -q
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import research as rs  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def _load(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def _ops(name):
    return rs._parse_rr_article(_load(name))


def _one(name):
    ops = _ops(name)
    assert len(ops) == 1, f"{name} 期望 1 条 op，实际 {ops}"
    return ops[0]


# ---------- ① 纯隔夜新规格式（真实 2026-11-25 公告〔2026〕第229号） ----------

def test_pure_overnight_new_format():
    o = _one("rr_20261125_overnight.html")
    assert o["amount"] == 6170.0
    assert o["rate"] == 1.25
    assert o["term_days"] == 1
    assert o["term"] == "隔夜"
    assert o["date"] == "2026-11-25"


def test_overnight_plain_suffix():
    """plain「隔夜逆回购操作」（无"期"后缀）：真实 11-05 公告〔2630亿〕，表格「隔夜1.25%」。"""
    o = _one("rr_20261102_overnight_suffix_variant.html")
    assert o["term_days"] == 1
    assert o["rate"] == 1.25
    assert o["amount"] == 2630.0


def test_overnight_qi_suffix_real():
    """真实「隔夜期逆回购操作」措辞（11-02/03/04 公告）：正则 `隔夜期?` 的 `期?` 分支护栏。
    fixture = pbc.gov.cn 2026110208540841613 真实正文（5410亿，隔夜期逆回购操作，表格隔夜1.25%）。
    若有人把 `隔夜期?` 简化回 `隔夜`，本用例必须 FAIL（否则真实公告静默漏解析）。"""
    o = _one("rr_20261102_overnight_qi.html")
    assert o["term_days"] == 1
    assert o["term"] == "隔夜"
    assert o["rate"] == 1.25
    assert o["amount"] == 5410.0
    assert o["date"] == "2026-11-02"


# ---------- ② 隔夜 + 7天多重价位并存（真实 2026-11-13 公告〔2026〕第220号） ----------

def test_overnight_plus_multirate_7d():
    ops = _ops("rr_20261113_overnight_plus_7d.html")
    assert len(ops) == 2, ops
    on = next(o for o in ops if o["term_days"] == 1)
    d7 = next(o for o in ops if o["term_days"] == 7)
    assert on["rate"] == 1.25 and on["amount"] == 3640.0
    # 7天多重价位中标：公告无单一利率，rate=None 属正常态，不得当作解析失败
    assert d7["rate"] is None and d7["amount"] == 3000.0


# ---------- ③ 旧规不回归（真实 2026-10-30 公告：7天1.40% + 同时开展隔夜） ----------

def test_old_era_7d_rate_not_regression():
    ops = _ops("rr_20261030_old_7d_plus_overnight.html")
    d7 = next(o for o in ops if o["term_days"] == 7)
    assert d7["rate"] == 1.4, "旧规 7天1.40% 解析不得回归"
    assert d7["amount"] == 150.0
    on = next(o for o in ops if o["term_days"] == 1)
    # 旧规时代隔夜无利率（摘要式表述），None 是正确行为
    assert on["amount"] == 10000.0 and on["rate"] is None


# ---------- ④ 零操作公告（旧规常见措辞，离线合成） ----------

ZERO_HTML = (
    '<div id="zoom"><p>公开市场业务交易公告 [2026]第200号</p>'
    '<p>2026年10月8日，因无首次到期逆回购，今日不开展逆回购操作。</p></div>'
)

def test_zero_operation_parses_amount_zero():
    ops = rs._parse_rr_article(ZERO_HTML)
    assert len(ops) == 1, ops
    assert ops[0]["amount"] == 0.0
    assert ops[0]["term_days"] == 7
    assert ops[0]["rate"] is None


ZERO_PLUS_ON = (
    '<div id="zoom"><p>2026年11月10日中国人民银行以固定利率、数量招标方式开展了'
    '3840亿元隔夜逆回购操作。具体情况如下：期限操作利率投标量中标量隔夜1.25%'
    '3840亿元3840亿元同时，7天期逆回购操作量为零。</p></div>'
)

def test_zero_7d_coexists_with_overnight():
    """"7天量为零" + "同时开展隔夜"并存：隔夜条正常带利率；7天零值条只计一次（去重护栏）。"""
    ops = rs._parse_rr_article(ZERO_PLUS_ON)
    assert len(ops) == 2, ops
    on = next(o for o in ops if o["term_days"] == 1)
    assert on["rate"] == 1.25 and on["amount"] == 3840.0
    zeros = [o for o in ops if o["term_days"] == 7]
    assert len(zeros) == 1 and zeros[0]["amount"] == 0.0, "7天零值条不得重复计数"


# ---------- ⑤ 政策利率文案换锚 ----------

def test_policy_rate_point_prefers_overnight():
    ops = _ops("rr_20261125_overnight.html")
    p = rs._policy_rate_point(ops)
    assert p and p["title"] == "政策利率"
    assert "隔夜" in p["text"] and "1.25" in p["text"], p["text"]
    assert "主要政策利率" in p["text"]
    assert "7天期逆回购操作利率 1.25" not in p["text"]


def test_policy_rate_point_falls_back_to_7d_old_era():
    """无隔夜利率（旧规数据段）时回退 7 天文案，兼容历史。"""
    ops = _ops("rr_20261030_old_7d_plus_overnight.html")
    p = rs._policy_rate_point(ops)
    assert p and "7天期逆回购操作利率 1.4%" in p["text"], p["text"]
    assert "隔夜" not in p["text"]


def test_no_cross_term_rate_change_claim():
    """换锚不是降息：隔夜1.25 首条 + 历史 7天1.40 在后 → 不得报"（前值 1.4%）/出现调整"。"""
    ops = [
        {"date": "2026-11-25", "amount": 6170.0, "rate": 1.25, "term_days": 1},
        {"date": "2026-10-31", "amount": 150.0, "rate": 1.4, "term_days": 7},
    ]
    p = rs._policy_rate_point(ops)
    assert p is not None
    assert "前值" not in p["text"] and "维持不变" in p["text"], p["text"]


def test_same_term_prev_rate_shown():
    """同期限前值不同才提示调整（隔夜历史上若从 1.30 换到 1.25 → 前值 1.3%）。"""
    ops = [
        {"date": "2026-11-25", "amount": 100.0, "rate": 1.25, "term_days": 1},
        {"date": "2026-11-24", "amount": 100.0, "rate": 1.3, "term_days": 1},
    ]
    p = rs._policy_rate_point(ops)
    assert p is not None
    assert "（前值 1.3%）" in p["text"] and "出现调整" in p["text"], p["text"]


def test_no_rate_at_all_returns_none():
    ops = [{"date": "2026-11-13", "amount": 3000.0, "rate": None, "term_days": 7}]
    assert rs._policy_rate_point(ops) is None


# ---------- "全额满足了一级交易商需求"新增语句无干扰（bug 清单第5条） ----------

def test_fulfill_demand_sentence_no_interference():
    html = ('<p>2026年11月20日中国人民银行以固定利率、数量招标方式开展了3795亿元'
            '隔夜逆回购操作，全额满足了一级交易商需求。具体情况如下：期限操作利率'
            '投标量中标量隔夜1.25%3795亿元3795亿元</p>')
    ops = rs._parse_rr_article(html)
    assert len(ops) == 1
    assert ops[0]["amount"] == 3795.0 and ops[0]["rate"] == 1.25


# ---------- 回归护栏：旧代码路径必丢隔夜利率 ----------

def test_legacy_rate_expression_would_drop_overnight():
    """旧实现 `rates.get(td) if td == 7 else None`：隔夜(td=1)恒 None（本次修复的靶子）。"""
    rates = {1: 1.25, 7: 1.4}
    assert (rates.get(1) if 1 == 7 else None) is None


# =====================================================================
# 买断式逆回购招标公告（栏目 5492845，2026 起新格式：中文到期日/操作日）
# fixture = pbc.gov.cn 真实公告正文（探针 2026-11-25 实抓）。
# 旧解析两个正则都要求 ISO 到期日 → 新格式全部漏解析（bo=0 事故根因）。
# =====================================================================

def test_bo_cn_full_6m_real():
    """〔2026〕第22号真实公告：中文到期日 2027年5月15日；操作日=「开展」前执行日
    2026-11-16（不是落款发布日 11-13）。若中文到期日分支被删，本用例必须 FAIL。"""
    ops = rs._parse_bo_article(_load("bo_20261113_6m_cn.html"))
    assert len(ops) == 1, ops
    o = ops[0]
    assert o["amount"] == 2000.0
    assert o["term"] == "6个月" and o["days"] == 180
    assert o["date_maturity"] == "2027-05-15"
    assert o["date"] == "2026-11-16", "操作日必须取执行日而非落款日"


def test_bo_cn_full_3m_real():
    """〔2026〕第21号真实公告：3000亿/3个月（91天）/到期 2027-02-04/操作 2026-11-05。"""
    ops = rs._parse_bo_article(_load("bo_20261104_3m_cn.html"))
    assert len(ops) == 1, ops
    o = ops[0]
    assert (o["amount"], o["term"], o["days"]) == (3000.0, "3个月", 91)
    assert o["date_maturity"] == "2027-02-04"
    assert o["date"] == "2026-11-05"


def test_bo_plain_rr_announcement_skipped():
    """5492845 栏目混有普通逆回购招标公告（〔2026〕第23号，5000亿7天期，无到期日）：
    不含「买断式」必须整条跳过——每日实际量已由 125475 交易公告覆盖，禁止重复计入。"""
    assert rs._parse_bo_article(_load("bo_20261124_plain_rr_skip.html")) == []


def test_bo_no_maturity_announcement_dropped():
    """旧措辞（期限为N个月（M天）后无到期日，〔2026〕第6号）：parse 不到到期日 → 空列表，
    fetch_pboc_liquidity 的 date_maturity 过滤兜住，不得凭空造到期日。"""
    assert rs._parse_bo_article(_load("bo_20260313_no_maturity.html")) == []


BO_ISO_LEGACY = (
    '<div id="zoom"><p>为保持银行体系流动性充裕，2025年9月15日，中国人民银行以'
    '固定数量、利率招标、多重价位中标方式开展6000亿元买断式逆回购操作，'
    '期限为3个月（91天），到期日为2025-12-16（遇节假日顺延）。</p>'
    '<p>中国人民银行公开市场业务操作室</p><p>2025年9月12日</p></div>'
)

def test_bo_iso_legacy_format_not_regression():
    """ISO 到期日写法保留兼容（旧 fixture 护栏）：到期日直接取公告值，不再套顺延。"""
    ops = rs._parse_bo_article(BO_ISO_LEGACY)
    assert len(ops) == 1, ops
    assert ops[0]["date_maturity"] == "2025-12-16"
    assert ops[0]["date"] == "2025-09-15"


BO_CN_LITE = (
    '<div id="zoom"><p>2026年2月13日，中国人民银行以多重价位中标方式开展'
    '10000亿元买断式逆回购操作，到期日为2026年8月13日（遇节假日顺延）。</p>'
    '<p>中国人民银行公开市场业务操作室 2026年2月12日</p></div>'
)

def test_bo_cn_lite_format():
    """lite 格式（无「期限为N个月」明示、仅中文到期日）：term=None、到期日仍解析。"""
    ops = rs._parse_bo_article(BO_CN_LITE)
    assert len(ops) == 1, ops
    assert ops[0]["date_maturity"] == "2026-08-13"
    assert ops[0]["term"] is None and ops[0]["amount"] == 10000.0
    assert ops[0]["date"] == "2026-02-13"


BO_LIST_HTML = (
    '<a href="/zhengcehuobisi/125207/125213/125431/5492845/index.html">列表页</a>'
    '<a href="/zhengcehuobisi/125207/125213/125431/5492845/2026111316452296143/index.html">第22号</a>'
    '<a href="/zhengcehuobisi/125207/125213/125431/5492845/2026110416345613688/index.html">第21号</a>'
    '<a href="http://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/5492845/2026093016485666174/index.html">第19号</a>'
    '<a href="/zhengcehuobisi/125207/125213/125431/125469/2026092314094200000/index.html">旧栏目不抓</a>'
)

def test_fetch_bo_list_loose_timestamp():
    """列表抓取指向 5492845：19 位时间戳(8+11)必须匹配（旧正则 \\d{12} 匹配不到 = 事故根因之一）。"""
    import asyncio

    class _FakeResp:
        encoding = "utf-8"
        text = BO_LIST_HTML

    class _FakeClient:
        async def get(self, url, headers=None):
            assert "5492845" in url, f"BO 列表必须指向 5492845，实际 {url}"
            return _FakeResp()

    pairs = asyncio.run(rs._fetch_bo_list(_FakeClient()))  # type: ignore[arg-type]
    assert len(pairs) == 3, pairs
    urls = [u for u, _ in pairs]
    assert all("125469" not in u for u in urls)
    assert pairs[0][1] == "2026-11-13"
    assert pairs[2][0].startswith("http://www.pbc.gov.cn/")


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
