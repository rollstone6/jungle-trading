"""Jungle 天才交易员持仓工作台 - FastAPI 主应用"""
import os
import json
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.models.database import init_db, get_db
from app.services.market import (
    fetch_realtime_tencent, fetch_kline_baidu, fetch_news_eastmoney,
    calc_ma, calc_volume_ratio
)
from app.services.portfolio import (
    get_account, get_all_positions, calc_position_metrics,
    calc_risk, get_summary
)
from app.services.reports import get_available_dates, get_report

# backtrader / zipline / vnpy 三个回测引擎均为重量级可选依赖，
# 改为在路由内懒加载，缺装时返回 503 而不是拖垮整个应用启动。


def _load_backtest_engine(engine: str):
    """按引擎名懒加载回测模块，未安装时抛 503。"""
    try:
        if engine == "backtrader":
            from app.services.backtest import run_backtest, get_available_strategies
            return run_backtest, get_available_strategies
        if engine == "zipline":
            from app.services.zipline_backtest import run_zipline_backtest
            return run_zipline_backtest, None
        if engine == "vnpy":
            from app.services.vnpy_service import run_vnpy_backtest, get_vnpy_strategies
            return run_vnpy_backtest, get_vnpy_strategies
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"回测引擎 {engine} 未安装，请先执行: pip install -r requirements.txt ({exc.name})"
        )
    raise HTTPException(status_code=400, detail="不支持的回测引擎")

# Config
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "0mGecaPX3duCfVXhEb")
BASE_DIR = Path(__file__).parent.parent

app = FastAPI(title="Jungle Genius Trading Desktop")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --- Auth middleware ---
def check_password(request: Request):
    pwd = request.query_params.get("pwd", "")
    if pwd != SITE_PASSWORD:
        raise HTTPException(status_code=403, detail="访问链接缺少有效 pwd 参数。")
    return pwd


# --- Let's Encrypt 验证路由（无需密码） ---
from fastapi.responses import PlainTextResponse
import os as _os

@app.get("/.well-known/acme-challenge/{token}")
async def acme_challenge(token: str):
    certbot_path = Path("/var/www/certbot/.well-known/acme-challenge") / token
    if certbot_path.exists():
        return PlainTextResponse(certbot_path.read_text())
    raise HTTPException(status_code=404)


# --- Models ---
class ManualPosition(BaseModel):
    code: str
    quantity: int
    cost_price: float

class ManualUpdate(BaseModel):
    account: dict | None = None
    positions: list[ManualPosition]


# --- Startup ---
@app.on_event("startup")
async def startup():
    init_db()
    # Seed default positions if empty
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    if count == 0:
        defaults = [
            ("603629", "利通电子", "科技/算力相关", 1540, 132.882),
            ("601398", "工商银行", "银行/红利防守", 8000, 7.169),
            ("002281", "光迅科技", "光模块/算力", 100, 218.032),
            ("159016", "广发证券 ETF", "券商/市场情绪", 10000, 1.048),
        ]
        for code, name, sector, qty, cost in defaults:
            conn.execute(
                "INSERT OR REPLACE INTO positions (code, name, sector, quantity, cost_price) VALUES (?,?,?,?,?)",
                (code, name, sector, qty, cost)
            )
        conn.commit()
    conn.close()


# --- Pages ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, pwd: str = check_password):
    account = get_account()
    positions = get_all_positions()

    # Fetch realtime quotes
    codes = [p["code"] for p in positions]
    quotes = await fetch_realtime_tencent(codes)

    # Update positions with latest data
    conn = get_db()
    quote_time = ""
    for p in positions:
        code = p["code"]
        q = quotes.get(code, {})
        if q:
            p["latest_price"] = q.get("latest_price", p["latest_price"])
            p["change_pct"] = q.get("change_pct", 0)
            p["high"] = q.get("high", p["high"])
            p["low"] = q.get("low", p["low"])
            p["open_price"] = q.get("open_price", p["open_price"])
            p["volume"] = q.get("volume", p["volume"])
            qt = q.get("quote_time", "")
            if qt and len(qt) >= 14:
                quote_time = f"{qt[:4]}-{qt[4:6]}-{qt[6:8]} {qt[8:10]}:{qt[10:12]}:{qt[12:14]}"
            p["quote_time"] = quote_time or p["quote_time"]

            # Update DB
            conn.execute("""
                UPDATE positions SET latest_price=?, change_pct=?, high=?, low=?,
                open_price=?, volume=?, quote_time=?, updated_at=CURRENT_TIMESTAMP
                WHERE code=?
            """, (p["latest_price"], p["change_pct"], p["high"], p["low"],
                  p["open_price"], p["volume"], p["quote_time"], code))
    conn.commit()
    conn.close()

    # Fetch kline for each if not cached
    conn = get_db()
    for p in positions:
        if not p["kline"] or len(p["kline"]) < 10:
            klines = await fetch_kline_baidu(p["code"], 120)
            if klines:
                p["kline"] = klines
                p["ma5"] = calc_ma(klines, 5)
                p["ma20"] = calc_ma(klines, 20)
                p["ma60"] = calc_ma(klines, 60)
                p["volume_ratio"] = calc_volume_ratio(klines)
                conn.execute("""
                    UPDATE positions SET kline_data=?, ma5=?, ma20=?, ma60=?, volume_ratio=?
                    WHERE code=?
                """, (json.dumps(klines), p["ma5"], p["ma20"], p["ma60"], p["volume_ratio"], p["code"]))
    conn.commit()
    conn.close()

    # Fetch news if not cached
    conn = get_db()
    for p in positions:
        if not p["news"]:
            news = await fetch_news_eastmoney(p["code"])
            if news:
                p["news"] = news
                conn.execute("UPDATE positions SET news=? WHERE code=?", (json.dumps(news), p["code"]))
    conn.commit()
    conn.close()

    # Calculate metrics
    positions = calc_position_metrics(positions, account)
    positions = calc_risk(positions)
    summary = get_summary(positions, account)

    # Get report info
    available_dates = get_available_dates()
    current_date = available_dates[0] if available_dates else ""
    report_time = ""

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "summary": summary,
            "positions": positions,
            "quote_time": quote_time or "暂无",
            "available_dates": ",".join(available_dates),
            "current_date": current_date,
            "report_time": report_time,
            "pwd": pwd,
            "is_update_mode": request.query_params.get("update") == "1",
        }
    )


# --- API ---
@app.post("/api/positions/manual")
async def update_manual(data: ManualUpdate, request: Request, pwd: str = check_password):
    """手工更新持仓"""
    conn = get_db()

    # Update account
    if data.account:
        principal = data.account.get("principal")
        total_asset = data.account.get("total_asset")
        if principal is not None and total_asset is not None:
            conn.execute("UPDATE account SET principal=?, total_asset=? WHERE id=1",
                         (principal, total_asset))

    # Replace all positions
    new_codes = set()
    for pos in data.positions:
        new_codes.add(pos.code)
        # Try to keep existing name/sector
        existing = conn.execute("SELECT name, sector FROM positions WHERE code=?", (pos.code,)).fetchone()
        name = existing["name"] if existing else pos.code
        sector = existing["sector"] if existing else ""
        conn.execute("""
            INSERT OR REPLACE INTO positions (code, name, sector, quantity, cost_price, kline_data, news, updated_at)
            VALUES (?, ?, ?, ?, ?,
                COALESCE((SELECT kline_data FROM positions WHERE code=?), '[]'),
                COALESCE((SELECT news FROM positions WHERE code=?), '[]'),
                CURRENT_TIMESTAMP)
        """, (pos.code, name, sector, pos.quantity, pos.cost_price, pos.code, pos.code))

    # Remove old positions not in new list
    old_codes = [r[0] for r in conn.execute("SELECT code FROM positions").fetchall()]
    for c in old_codes:
        if c not in new_codes:
            conn.execute("DELETE FROM positions WHERE code=?", (c,))

    conn.commit()
    conn.close()
    return {"message": "持仓已更新，正在刷新行情..."}


@app.get("/api/reports/strategies")
async def get_strategies(date: str, request: Request, pwd: str = check_password):
    """获取指定日期的AI策略报告"""
    report = get_report(date)
    if not report:
        raise HTTPException(status_code=404, detail="当天没有 AI 报告。")
    return report


# === 回测页面 ===

@app.get("/backtest", response_class=HTMLResponse)
async def backtest_page(request: Request, pwd: str = check_password):
    """回测页面"""
    positions = get_all_positions()
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {"request": request, "positions": positions, "pwd": pwd}
    )


# === 回测 API ===

@app.get("/api/backtest/strategies")
async def list_strategies(request: Request, pwd: str = check_password):
    """获取可用的回测策略"""
    _, get_strategies = _load_backtest_engine("backtrader")
    strategies = get_strategies()
    try:
        _, get_vnpy = _load_backtest_engine("vnpy")
        strategies += get_vnpy()
    except HTTPException:
        pass  # vnpy 未安装时仅返回 backtrader 策略
    return strategies


@app.post("/api/backtest/run")
async def run_backtest_api(request: Request, pwd: str = check_password):
    """运行回测"""
    data = await request.json()
    
    code = data.get("code")
    strategy = data.get("strategy", "ma")
    initial_cash = data.get("initial_cash", 100000)
    engine = data.get("engine", "backtrader")
    
    if not code:
        raise HTTPException(status_code=400, detail="缺少股票代码")
    
    # 获取K线数据
    conn = get_db()
    row = conn.execute("SELECT kline_data FROM positions WHERE code=?", (code,)).fetchone()
    conn.close()
    
    if not row or not row["kline_data"]:
        raise HTTPException(status_code=404, detail="未找到该股票的K线数据")
    
    klines = json.loads(row["kline_data"])
    
    if len(klines) < 30:
        raise HTTPException(status_code=400, detail="K线数据不足，至少需要30天")
    
    # 运行回测（引擎模块懒加载，缺装时返回 503；zipline 无策略参数）
    if engine in ("backtrader", "zipline", "vnpy"):
        run_fn, _ = _load_backtest_engine(engine)
        if engine == "zipline":
            result = run_fn(klines, initial_cash)
        else:
            result = run_fn(klines, strategy, initial_cash)
    else:
        raise HTTPException(status_code=400, detail="不支持的回测引擎")
    
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
