"""波动率观察共享口径（单一真源）。

calc_volatility_watch（持仓 SSR）与 /api/regime/latest 扫描必须调用本模块，
禁止两处各算各的导致口径漂移。历史实现在 app/services/portfolio.py，
2026-11-19 抽出至此并新增组合解读 combined_read。

口径全透明：
- HV_n = 近 n 日对数收益样本标准差 × √252（年化）
- 分位 = 当前 HV20 在近一年（≤250 个滚动值）中的百分位排名
- ATR14% = Wilder 平滑真实波幅 / 现价（日均波动幅度，可直接对照止损距离）
- 状态：HV20 ≥ 1.25×HV60 → 扩张；≤ 0.75×HV60 → 收敛（挤压）；否则平稳
- 警示：分位 ≥80%（高波动区）或扩张且 HV20 ≥40%；收敛且分位 ≤25% 提示变盘窗口
"""
import math
import statistics

# 性能：statistics.stdev 走 Fraction 精确算法（Python 3.14 实测慢 18 倍），
# 首页 8 只×250 滚动窗口曾占 SSR ~0.6s。改用 float 两遍算法：
# 对真实持仓 K 线 1848 个窗口 + 300 组随机序列（含极端尺度）验证，
# round(...,1) 后与 statistics.stdev 结果 0 差异（2026-11-27 实测）。
def _stdev(xs: list[float]) -> float:
    n = len(xs)
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))

# K线少于此根数不出波动数据（HV20 需要 ≥21 个收益样本 + 1 根基准价）
MIN_BARS = 22


def calc_volatility(klines: list[dict]) -> dict:
    """输入日K列表（[{date,open,high,low,close,volume}, ...]），输出波动率观察字典。

    返回键：hv20 / hv60 / pctile / atr_pct / state / state_cls / alert / alert_text。
    数据不足（<MIN_BARS 根有效收盘）时返回全空占位（state="数据不足"），绝不抛错。
    """
    kl = klines or []
    v = {"hv20": None, "hv60": None, "pctile": None, "atr_pct": None,
         "state": "数据不足", "state_cls": "flat", "alert": False, "alert_text": ""}
    closes = [float(k["close"]) for k in kl if k.get("close")]
    if len(closes) < MIN_BARS:
        return v
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]

    def hv(n):
        if len(rets) < n:
            return None
        return round(_stdev(rets[-n:]) * math.sqrt(252) * 100, 1)

    v["hv20"], v["hv60"] = hv(20), hv(60)
    # HV20 滚动序列（近一年）→ 当前值分位
    if len(rets) >= 20:
        roll = [_stdev(rets[i - 20:i]) * math.sqrt(252) * 100
                for i in range(20, len(rets) + 1)]
        roll = roll[-250:]
        cur = roll[-1]
        v["pctile"] = round(sum(1 for x in roll if x <= cur) / len(roll) * 100)
    # ATR14（Wilder 平滑）
    if len(kl) >= 15:
        trs = []
        for i in range(1, len(kl)):
            h, l, pc = float(kl[i]["high"]), float(kl[i]["low"]), float(kl[i - 1]["close"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = statistics.mean(trs[:14])
        for tr in trs[14:]:
            atr = (atr * 13 + tr) / 14
        price = float(closes[-1])
        if price > 0:
            v["atr_pct"] = round(atr / price * 100, 2)
    # 状态与警示
    h20, h60 = v["hv20"], v["hv60"]
    if h20 and h60:
        ratio = h20 / h60
        if ratio >= 1.25:
            v["state"], v["state_cls"] = "扩张", "expand"
        elif ratio <= 0.75:
            v["state"], v["state_cls"] = "收敛", "squeeze"
        else:
            v["state"], v["state_cls"] = "平稳", "flat"
    pct = v["pctile"]
    if pct is not None and pct >= 80:
        v["alert"], v["alert_text"] = True, f"波动率处一年高位（{pct}%分位），注意回撤与仓位"
    elif v["state"] == "扩张" and h20 and h20 >= 40:
        v["alert"], v["alert_text"] = True, f"波动快速放大（HV20 {h20}%），振幅加剧"
    elif v["state"] == "收敛" and pct is not None and pct <= 25:
        v["alert"], v["alert_text"] = True, f"波动收敛至低位（{pct}%分位），留意变盘窗口"
    return v


REGIME_TO_CN = {"squeeze": "挤压待变", "trending": "趋势型",
                "ranging": "区间型", "neutral": "中性"}

# 组合解读（形态分类 × 波动状态）的透明规则，前端 combinedRead 与之同口径：
# - squeeze + 波动收敛：双信号共振 → 变盘窗口（警示）
# - squeeze + 分位≤25%（状态平稳但处一年低位）→ 变盘窗口（警示）
# - squeeze + 扩张：挤压带高波动，先按变盘窗口处理（警示）
# - trending + 扩张 → 趋势加速（振幅风险）
# - ranging + 收敛 → 窄幅蓄势
def combined_read(regime: str | None, vol: dict | None) -> dict | None:
    """走势分类 × 波动状态 组合解读。数据不足或无规则命中返回 None（不强行解读）。"""
    if not regime or regime not in REGIME_TO_CN or not vol or vol.get("hv20") is None:
        return None
    state = vol.get("state_cls")
    pct = vol.get("pctile")
    if regime == "squeeze":
        if state == "squeeze":
            return {"label": "变盘窗口", "cls": "warn",
                    "title": "形态挤压 + 波动收敛，双信号共振，变盘概率上升"}
        if state == "expand":
            return {"label": "变盘窗口", "cls": "warn",
                    "title": "形态挤压但波动已放大，方向即将选择，注意振幅"}
        if pct is not None and pct <= 25:
            return {"label": "变盘窗口", "cls": "warn",
                    "title": f"形态挤压 + 波动处一年低位（{pct}%分位），留意变盘窗口"}
        return None
    if regime == "trending" and state == "expand":
        return {"label": "趋势加速", "cls": "info",
                "title": "趋势延续且波动放大，顺势但注意回撤幅度"}
    if regime == "ranging" and state == "squeeze":
        return {"label": "窄幅蓄势", "cls": "info", "title": "区间震荡且波动收敛，等待方向"}
    return None

