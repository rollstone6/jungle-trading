#!/usr/bin/env python3
"""moneyflow '-' 值解析回归测试（离线合成数据，无网络）。

背景：东财 push2 资金流接口对停牌/无数据个股返回 '-' 字符串，
research.py 原 `float(r.get("f128") or 0)` 里 '-' 为真值不兜底 →
ValueError 且在 try 块之外 → /api/research/moneyflow 整页 502（t_eab5a42a）。
本测试锁定：
1. _safe_f 对 '-'/None/''/非法串返回 default，对正常数字与 float() 逐值一致；
2. fetch_money_flow 端到端（mock httpx 传输层）：含 '-' 行的 payload 不抛异常、
   '-' 按 0 处理、正常值口径不变、坏行(f3='-')被跳过、无行可用时如实 RuntimeError；
3. 回归护栏：旧代码路径 `float('-' or 0)` 必然抛 ValueError（证明被测缺陷真实存在）。

运行: cd 仓库根 && /opt/hermes-agent/.venv/bin/python -m pytest tests/test_moneyflow_parse.py -q
或直接 /opt/hermes-agent/.venv/bin/python tests/test_moneyflow_parse.py（退出码 0 = 全过）
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.services import research as rs  # noqa: E402


# ---------- 1. _safe_f 单元 ----------

def test_safe_f_dirty_values_return_default():
    for bad in ("-", "", None, "null", "--", "  -  ", "abc", [1], {}):
        assert rs._safe_f(bad) == 0.0, f"{bad!r} 应返回 default 0.0"
        assert rs._safe_f(bad, default=-1.5) == -1.5


def test_safe_f_normal_values_match_float():
    good = [0, 1, -1, 3.14, 1e8, "0", "-2.5", "12345678.9", "1e3", " -0.5 ",
            True, False]  # bool 是 int 子类，float() 语义一致
    for v in good:
        assert rs._safe_f(v) == float(v), f"{v!r} 解析结果与 float() 不一致"


# ---------- 2. fetch_money_flow 端到端（mock 传输层，真实解析逻辑） ----------

class _Resp:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self):
        return self._payload


def _mk_client(rows):
    """伪造 AsyncClient：get 恒返回东财 push2 结构的 rows（不触网）。"""
    class FakeClient:
        async def get(self, url, headers=None, **kw):
            return _Resp({"data": {"diff": rows}})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return FakeClient()


def _patch_client(monkeypatch, rows):
    monkeypatch.setattr(rs.httpx, "AsyncClient", lambda *a, **k: _mk_client(rows))
    # 绕开 push2 熔断冷却状态，保证本用例必走东财分支
    monkeypatch.setattr(rs, "_push2_breaker_until", 0.0)


def test_moneyflow_dash_rows_do_not_crash(monkeypatch):
    """核心复现用例：含 '-' 的 f128/f136 不再 502，'-' 按 0、正常值口径不变。"""
    rows = [
        # 正常行：跌 2.15%、主力净流入 2000万、成交额 8亿 → 背离信号
        {"f12": "600519", "f14": "贵州茅台", "f2": 1500.0, "f3": -2.15,
         "f62": 2e7, "f115": 8e8, "f128": 12345678, "f136": 5e6},
        # 停牌行：f128/f136 为 '-' → super/large 按 0
        {"f12": "000001", "f14": "平安银行", "f2": 10.5, "f3": -1.5,
         "f62": 3e6, "f115": 1e8, "f128": "-", "f136": "-"},
        # 缺字段行：f128/f136 为 None → 按 0
        {"f12": "300750", "f14": "宁德时代", "f2": 200.0, "f3": -3.0,
         "f62": 1e7, "f115": 2e8, "f128": None, "f136": None},
        # 空串行 + 字符串数字行混合
        {"f12": "688001", "f14": "华兴源创", "f2": 30.0, "f3": -1.2,
         "f62": 5e6, "f115": 6e7, "f128": "", "f136": "12345.6"},
        # 坏行：f3='-'（涨跌未知无法评估信号）→ 整行跳过，不崩
        {"f12": "601999", "f14": "小企邮", "f2": "-", "f3": "-",
         "f62": "-", "f115": "-", "f128": "-", "f136": "-"},
    ]
    _patch_client(monkeypatch, rows)
    out = asyncio.run(rs.fetch_money_flow())

    assert out["source"] == "东方财富"
    assert out["universe"] == len(rows)
    by_code = {r["code"]: r for r in out["signals"] + out["inflow_top"]}
    # 正常值口径不变：12345678 元 → 1234.6 万（round(x/1e4,1)）
    assert by_code["600519"]["super_inflow_wan"] == 1234.6
    assert by_code["600519"]["large_inflow_wan"] == 500.0
    assert by_code["600519"]["main_inflow_wan"] == 2000.0
    # '-' / None / '' 全部按 0
    assert by_code["000001"]["super_inflow_wan"] == 0.0
    assert by_code["000001"]["large_inflow_wan"] == 0.0
    assert by_code["300750"]["super_inflow_wan"] == 0.0
    assert by_code["300750"]["large_inflow_wan"] == 0.0
    assert by_code["688001"]["super_inflow_wan"] == 0.0
    assert by_code["688001"]["large_inflow_wan"] == 1.2  # 12345.6/1e4=1.2
    # 坏行被跳过（f3='-'），不进任何榜单
    assert "601999" not in by_code


def test_moneyflow_all_dash_still_returns_ok(monkeypatch):
    """全市场都是 '-' 脏行时：不 502，返回空榜单（universe 保留）。"""
    rows = [{"f12": f"60000{i}", "f14": f"票{i}", "f2": "-", "f3": "-",
             "f62": "-", "f115": "-", "f128": "-", "f136": "-"} for i in range(3)]
    _patch_client(monkeypatch, rows)
    out = asyncio.run(rs.fetch_money_flow())
    assert out["signals"] == [] and out["inflow_top"] == []
    assert out["universe"] == 3


def test_moneyflow_no_rows_reports_degradation_not_crash(monkeypatch):
    """双源都拿不到行：如实 RuntimeError（上游 _research_produce 转 502+stale 降级），
    而不是解析崩溃 TypeError/ValueError。"""
    _patch_client(monkeypatch, [])
    async def no_sina(client):
        return []
    monkeypatch.setattr(rs, "_sina_flow_fetch", no_sina)
    try:
        asyncio.run(rs.fetch_money_flow())
        assert False, "应抛 RuntimeError"
    except RuntimeError as e:
        assert "资金流接口不可用" in str(e)


# ---------- 3. 回归护栏：旧缺陷真实存在 ----------

def test_legacy_expression_would_raise():
    """旧代码 `float(r.get('f128') or 0)` 对 '-' 必抛 ValueError（本次修复的靶子）。"""
    try:
        float("-" or 0)
        assert False, "旧表达式竟然没抛错？修复必要性存疑"
    except ValueError:
        pass


if __name__ == "__main__":
    import subprocess
    sys.exit(subprocess.call(
        [sys.executable, "-m", "pytest", __file__, "-q"]))
