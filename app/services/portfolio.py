"""持仓服务 - 风控计算和数据处理"""
import json
from app.models.database import get_db


def get_account() -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM account WHERE id = 1").fetchone()
    conn.close()
    if not row:
        return {"principal": 300000, "total_asset": 300000}
    return {"principal": row["principal"], "total_asset": row["total_asset"]}


def get_all_positions() -> list[dict]:
    conn = get_db()
    rows = conn.execute("SELECT * FROM positions ORDER BY code").fetchall()
    conn.close()
    positions = []
    for r in rows:
        kline_data = json.loads(r["kline_data"] or "[]")
        news = json.loads(r["news"] or "[]")
        risk_notes = json.loads(r["risk_notes"] or "[]")
        positions.append({
            "code": r["code"],
            "name": r["name"],
            "sector": r["sector"],
            "quantity": r["quantity"],
            "cost_price": r["cost_price"],
            "latest_price": r["latest_price"],
            "change_pct": r["change_pct"],
            "high": r["high"],
            "low": r["low"],
            "open_price": r["open_price"],
            "volume": r["volume"],
            "kline": kline_data,
            "ma3": r["ma3"],
            "ma8": r["ma8"],
            "ma20": r["ma20"],
            "ma60": r["ma60"],
            "ma120": r["ma120"],
            "ma250": r["ma250"],
            "boll_upper": r["boll_upper"],
            "boll_lower": r["boll_lower"],
            "kdj_k": r["kdj_k"],
            "kdj_d": r["kdj_d"],
            "kdj_j": r["kdj_j"],
            "macd_dif": r["macd_dif"],
            "macd_dea": r["macd_dea"],
            "macd_hist": r["macd_hist"],
            "regime_label": r["regime_label"],
            "volume_ratio": r["volume_ratio"],
            "news": news,
            "risk_level": r["risk_level"],
            "badge": r["badge"],
            "fundamental": r["fundamental"],
            "risk_notes": risk_notes,
            "quote_time": r["quote_time"],
        })
    return positions


def calc_swing_levels(positions: list[dict]) -> list[dict]:
    """波段压力位/支撑位：截至昨日的近60交易日高低点(排除今日K线，避免现价永远在区间内)。
    压力位 = 前高(需向上突破)；支撑位 = 前低(跌破即破位)。次级 res20/sup20 为近20日高低点。"""
    for p in positions:
        kl = p.get("kline") or []
        res = sup = res20 = sup20 = 0.0
        if len(kl) >= 2:
            prior = kl[:-1]
            highs = [k["high"] for k in prior[-60:]]
            lows = [k["low"] for k in prior[-60:]]
            res, sup = max(highs), min(lows)
            res20, sup20 = max([k["high"] for k in prior[-20:]]), min([k["low"] for k in prior[-20:]])
        p["resistance"] = round(res, 3)
        p["support"] = round(sup, 3)
        p["resistance_20"] = round(res20, 3)
        p["support_20"] = round(sup20, 3)
    return positions


def calc_boundary_proximity(positions: list[dict]) -> list[dict]:
    """计算现价与各指标边界值(MA/BOLL轨/波段压力支撑)的接近度，用于前端颜色高亮。
    prox = 1 - 距离%/2.0（2% 以内视为靠近，越近越接近 1）；side: up=边界在上方(压力) / down=下方(支撑)"""
    calc_swing_levels(positions)
    for p in positions:
        price = p.get("latest_price") or 0
        near = {}
        if price > 0:
            checks = [
                ("MA3", p.get("ma3")), ("MA8", p.get("ma8")), ("MA20", p.get("ma20")),
                ("MA120", p.get("ma120")), ("MA250", p.get("ma250")),
                ("BOLL上", p.get("boll_upper")), ("BOLL下", p.get("boll_lower")),
                ("压力位", p.get("resistance")), ("支撑位", p.get("support")),
            ]
            for label, v in checks:
                if not v or v <= 0:
                    continue
                dist_pct = abs(price - v) / price * 100
                prox = max(0.0, 1.0 - dist_pct / 2.0)
                if prox <= 0:
                    continue
                near[label] = {
                    "prox": round(prox, 3),
                    "side": "up" if v >= price else "down",
                    "dist": round(dist_pct, 2),
                }
        p["near"] = near
    return positions


def calc_position_metrics(positions: list[dict], account: dict) -> list[dict]:
    """计算每个持仓的市值、盈亏、仓位占比"""
    principal = account["principal"]
    total_asset = account["total_asset"]
    total_market_value = 0

    for p in positions:
        market_value = p["quantity"] * p["latest_price"]
        total_market_value += market_value
        pnl = market_value - (p["quantity"] * p["cost_price"])
        pnl_pct = (pnl / (p["quantity"] * p["cost_price"]) * 100) if p["cost_price"] > 0 else 0
        p["market_value"] = round(market_value, 2)
        p["pnl"] = round(pnl, 2)
        p["pnl_pct"] = round(pnl_pct, 2)

    # Calculate position weight
    for p in positions:
        p["weight"] = round(p["market_value"] / total_market_value * 100, 1) if total_market_value > 0 else 0

    return positions


def calc_risk(positions: list[dict]) -> list[dict]:
    """计算风控判断（仓位 + 均线 + 波段压力/支撑位）"""
    if positions and "resistance" not in positions[0]:
        calc_swing_levels(positions)
    for p in positions:
        notes = []
        tech_watch = False   # 靠近边界 → 至少"中"
        tech_break = False   # 破位/创新高 → 高危或强信号
        # High concentration
        if p.get("weight", 0) > 40:
            notes.append(f"单只标的仓位约 {p['weight']:.1f}%，超过 40% 风控线")

        # Below MA20
        if p["ma20"] > 0 and p["latest_price"] < p["ma20"]:
            notes.append(f"现价低于 20 日线 {p['ma20']}，趋势需要防守")

        # Large loss
        if p.get("pnl_pct", 0) < -10:
            notes.append(f"当前估算浮亏约 {p['pnl_pct']:.1f}%，禁止无条件补仓摊平")

        # 波段压力/支撑位判断：60日前高/前低为强边界，20日高低点为近端边界（均不含今日K线）
        price = p.get("latest_price") or 0
        res, sup = p.get("resistance") or 0, p.get("support") or 0
        res20, sup20 = p.get("resistance_20") or 0, p.get("support_20") or 0
        if price > 0 and res > 0 and sup > 0:
            if price >= res:
                tech_break = True
                notes.append(f"已放量突破 60 日前高 {res:.2f}，打开上行空间，回踩不破可持，滞涨注意止盈")
            elif price >= res20 > 0 and (res20 - price) / price * 100 <= 1.5:
                tech_watch = True
                notes.append(f"贴近 20 日压力位 {res20:.2f}，上方 60 日前高 {res:.2f}，冲高乏力注意分批止盈")
            if price <= sup:
                tech_break = True
                notes.append(f"已跌破 60 日抄底支撑位 {sup:.2f} 创新低，趋势恶劣，禁止摊平、优先止损")
            elif sup20 > 0 and price <= sup20 * 1.025:
                tech_watch = True
                ds = (price - sup) / price * 100
                note = f"已回落至 20 日低位 {sup20:.2f} 附近，进入超卖区"
                if ds <= 15:
                    note += f"，距 60 日抄底支撑位 {sup:.2f} 约 {ds:.1f}%"
                if p.get("weight", 0) > 40:
                    note += "；当前仓位已重，不宜补仓，等企稳再定去留"
                else:
                    note += "，缩量企稳可轻仓试错，跌破支撑即离场"
                notes.append(note)
            else:
                ds = (price - sup) / price * 100
                if ds <= 3:
                    tech_watch = True
                    notes.append(f"距下方 60 日抄底支撑位 {sup:.2f} 仅 {ds:.1f}%，缩量企稳可轻仓试错，跌破即离场")

        # Determine risk level
        if p.get("weight", 0) > 40 or p.get("pnl_pct", 0) < -10 or tech_break:
            p["risk_level"] = "高"
        elif p.get("weight", 0) > 20 or p.get("pnl_pct", 0) < -5 or tech_watch:
            p["risk_level"] = "中"
        else:
            p["risk_level"] = "低"

        # Badge
        if p["risk_level"] == "高":
            p["badge"] = "减仓候选 · 风险高"
        elif p["risk_level"] == "中":
            p["badge"] = "观察 · 风险中"
        else:
            p["badge"] = "持有 · 风险低"

        p["risk_notes"] = notes

    return positions


def get_summary(positions: list[dict], account: dict) -> dict:
    principal = account["principal"]
    total_asset = account["total_asset"]
    total_market_value = sum(p.get("market_value", 0) for p in positions)
    total_pnl = total_asset - principal
    total_pnl_pct = (total_pnl / principal * 100) if principal > 0 else 0

    return {
        "principal": principal,
        "total_asset": total_asset,
        "total_market_value": round(total_market_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
    }
