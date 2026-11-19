#!/usr/bin/env python3
"""波动率共享口径 + 组合解读规则回归测试（离线合成数据，无网络）。

背景：走势分类 × 波动率观察 合并面板（t_1ad2d251）要求 portfolio.calc_volatility_watch
与 /api/regime/latest 的 vol 字段共用单一真源 app/services/volatility.py，
且组合解读规则透明可验证。本测试锁定：
1. calc_volatility 与旧实现逐位一致的等价性（持仓入口委托后行为不变）；
2. 数据不足（<22 根）返回占位不抛错；
3. combined_read 四种组合 + 边界（分位≤25 触发、数据不足 None、中性不解读）。

运行: cd 仓库根 && /opt/hermes-agent/.venv/bin/python -m pytest tests/test_volatility.py -q
或直接 /opt/hermes-agent/.venv/bin/python tests/test_volatility.py（退出码 0 = 全过）
"""
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.volatility import calc_volatility, combined_read  # noqa: E402
from app.services.portfolio import calc_volatility_watch  # noqa: E402


def _make_klines(n=300, wave=0.3, amp_trend=0.0):
    """合成 OHLC 序列：close = 10 + sin + 线性趋势，high/low 有真实波幅。"""
    kl = []
    for i in range(n):
        c = 10 + wave * math.sin(i / 7.0) + amp_trend * i
        kl.append({"date": f"d{i:04d}", "open": c, "high": c + 0.25,
                   "low": c - 0.25, "close": c, "volume": 1000 + i})
    return kl


def test_reference_implementation_equivalence():
    """抽函数等价性：委托后的 calc_volatility_watch 输出 == 内联参考实现（旧口径逐行复刻）。"""
    kl = _make_klines()
    v = calc_volatility(kl)

    # --- 参考实现（与合并前 portfolio.py 原代码逐行同口径）---
    ref = {"hv20": None, "hv60": None, "pctile": None, "atr_pct": None,
           "state": "数据不足", "state_cls": "flat", "alert": False, "alert_text": ""}
    closes = [float(k["close"]) for k in kl if k.get("close")]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    def hv(n):
        if len(rets) < n:
            return None
        return round(statistics.stdev(rets[-n:]) * math.sqrt(252) * 100, 1)

    ref["hv20"], ref["hv60"] = hv(20), hv(60)
    roll = [statistics.stdev(rets[i - 20:i]) * math.sqrt(252) * 100
            for i in range(20, len(rets) + 1)][-250:]
    cur = roll[-1]
    ref["pctile"] = round(sum(1 for x in roll if x <= cur) / len(roll) * 100)
    trs = [max(float(kl[i]["high"]) - float(kl[i]["low"]),
               abs(float(kl[i]["high"]) - float(kl[i - 1]["close"])),
               abs(float(kl[i]["low"]) - float(kl[i - 1]["close"])))
           for i in range(1, len(kl))]
    atr = statistics.mean(trs[:14])
    for tr in trs[14:]:
        atr = (atr * 13 + tr) / 14
    ref["atr_pct"] = round(atr / float(closes[-1]) * 100, 2)
    h20, h60 = ref["hv20"], ref["hv60"]
    ratio = h20 / h60
    if ratio >= 1.25:
        ref["state"], ref["state_cls"] = "扩张", "expand"
    elif ratio <= 0.75:
        ref["state"], ref["state_cls"] = "收敛", "squeeze"
    else:
        ref["state"], ref["state_cls"] = "平稳", "flat"
    pct = ref["pctile"]
    if pct >= 80:
        ref["alert"], ref["alert_text"] = True, f"波动率处一年高位（{pct}%分位），注意回撤与仓位"
    elif ref["state"] == "扩张" and h20 >= 40:
        ref["alert"], ref["alert_text"] = True, f"波动快速放大（HV20 {h20}%），振幅加剧"
    elif ref["state"] == "收敛" and pct <= 25:
        ref["alert"], ref["alert_text"] = True, f"波动收敛至低位（{pct}%分位），留意变盘窗口"

    assert v == ref, f"口径漂移: {v} != {ref}"


def test_positions_wrapper_equivalence():
    """持仓入口委托：calc_volatility_watch(positions) 的 p['vol'] == calc_volatility(kline)。"""
    kl = _make_klines(260)
    pos = [{"code": "TEST01", "kline": kl}, {"code": "TEST02", "kline": kl[:10]},
           {"code": "TEST03"}]
    out = calc_volatility_watch(pos)
    assert out[0]["vol"] == calc_volatility(kl)
    assert out[1]["vol"]["state"] == "数据不足"
    assert out[2]["vol"]["hv20"] is None  # 无 kline 不抛错


def test_insufficient_data_no_raise():
    """数据不足边界：<22 根、空列表、None 全部返回占位，绝不抛错。"""
    for kl in ([], None, _make_klines(21), [{"close": 1}, {"close": 2}]):
        v = calc_volatility(kl)
        assert v["state"] == "数据不足" and v["hv20"] is None
    assert calc_volatility(_make_klines(22))["hv20"] is not None


def test_expand_and_alert_states():
    """构造前段平静后段剧烈的序列 → 扩张态 + 高位/放大警示触发。"""
    kl = []
    c = 10.0
    for i in range(280):
        # 前 250 根几乎不波动，后 30 根剧烈摆动：HV20 >> HV60
        step = 0.001 * (i % 2 or -1) if i < 250 else 0.9 * (1 if i % 2 else -1)
        c = max(c + step, 1.0)
        kl.append({"date": f"d{i:04d}", "open": c, "high": c + abs(step) + 0.05,
                   "low": c - abs(step) - 0.05, "close": c, "volume": 100})
    v = calc_volatility(kl)
    assert v["state_cls"] == "expand", v
    assert v["alert"] is True and v["alert_text"], v
    # 分位/ATR 一致性抽查
    assert 0 <= v["pctile"] <= 100 and v["atr_pct"] >= 0


def test_combined_read_rules():
    """组合解读：四种命中 + 不命中返回 None。"""
    squeeze_vol = {"hv20": 10.0, "hv60": 20.0, "pctile": 10, "atr_pct": 1.0,
                   "state_cls": "squeeze", "state": "收敛", "alert": False, "alert_text": ""}
    expand_vol = dict(squeeze_vol, hv20=30.0, hv60=20.0, pctile=90,
                      state_cls="expand", state="扩张")
    flat_vol = dict(squeeze_vol, hv20=20.0, hv60=20.0, pctile=50,
                    state_cls="flat", state="平稳")
    flat_lowpct = dict(flat_vol, pctile=25)
    no_vol = {"hv20": None, "state_cls": "flat", "pctile": None}

    r = combined_read("squeeze", squeeze_vol)
    assert r and r["label"] == "变盘窗口" and r["cls"] == "warn"
    r = combined_read("squeeze", dict(squeeze_vol, pctile=26))
    assert r and r["label"] == "变盘窗口"          # 收敛态命中不依赖分位
    r = combined_read("squeeze", flat_lowpct)
    assert r and r["label"] == "变盘窗口"          # 平稳但分位≤25 边界值命中
    r = combined_read("squeeze", dict(flat_vol, pctile=26))
    assert r is None                                # 平稳+分位>25 不强行解读
    r = combined_read("trending", expand_vol)
    assert r and r["label"] == "趋势加速" and r["cls"] == "info"
    r = combined_read("trending", squeeze_vol)
    assert r is None
    r = combined_read("ranging", squeeze_vol)
    assert r and r["label"] == "窄幅蓄势" and r["cls"] == "info"
    r = combined_read("ranging", expand_vol)
    assert r is None
    r = combined_read("neutral", squeeze_vol)
    assert r is None                                # 中性一律无解读
    assert combined_read("squeeze", no_vol) is None  # 数据不足不报错不显示
    assert combined_read("squeeze", None) is None
    assert combined_read(None, squeeze_vol) is None
    assert combined_read("unknown", squeeze_vol) is None


def _legacy_main():
    fns = [test_reference_implementation_equivalence, test_positions_wrapper_equivalence,
           test_insufficient_data_no_raise, test_expand_and_alert_states,
           test_combined_read_rules]
    for f in fns:
        f()
        print(f"✓ {f.__name__}")
    print(f"\n结果: {len(fns)}/{len(fns)} 通过")


if __name__ == "__main__":
    _legacy_main()
