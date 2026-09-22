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
            "ma5": r["ma5"],
            "ma10": r["ma10"],
            "ma20": r["ma20"],
            "ma60": r["ma60"],
            "volume_ratio": r["volume_ratio"],
            "news": news,
            "risk_level": r["risk_level"],
            "badge": r["badge"],
            "fundamental": r["fundamental"],
            "risk_notes": risk_notes,
            "quote_time": r["quote_time"],
        })
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
    """计算风控判断"""
    for p in positions:
        notes = []
        # High concentration
        if p.get("weight", 0) > 40:
            notes.append(f"单只标的仓位约 {p['weight']:.1f}%，超过 40% 风控线")

        # Below MA20
        if p["ma20"] > 0 and p["latest_price"] < p["ma20"]:
            notes.append(f"现价低于 20 日线 {p['ma20']}，趋势需要防守")

        # Large loss
        if p.get("pnl_pct", 0) < -10:
            notes.append(f"当前估算浮亏约 {p['pnl_pct']:.1f}%，禁止无条件补仓摊平")

        # Determine risk level
        if p.get("weight", 0) > 40 or p.get("pnl_pct", 0) < -10:
            p["risk_level"] = "高"
        elif p.get("weight", 0) > 20 or p.get("pnl_pct", 0) < -5:
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
