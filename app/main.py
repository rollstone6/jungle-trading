"""Jungle 天才交易员持仓工作台 - FastAPI 主应用"""
import os
import json
import asyncio
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
    fetch_etf_money_flow, calc_ma, calc_boll, calc_kdj, calc_macd, calc_volume_ratio
)
from app.services.portfolio import (
    get_account, get_all_positions, calc_position_metrics,
    calc_risk, get_summary
)
from app.services.reports import get_available_dates, get_report
# 量化策略统一股票池（与 app/quant 各策略共用同一份）
from app.quant.config import STOCK_LIST

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
        # 与 app/quant/config.py 的 STOCK_LIST 保持一致（2026-10-10 持仓）
        defaults = [
            ("603678", "火炬电子", "军工电子/电容", 100, 0),
            ("300291", "百纳千成", "影视传媒", 100, 0),
            ("300394", "天孚通信", "光模块/CPO", 100, 0),
            ("300505", "川金诺", "磷化工", 100, 0),
            ("300999", "大普微", "芯片/存储", 100, 0),
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

    # Fetch kline for each if not cached or recalculate indicators
    conn = get_db()
    for p in positions:
        # 检查是否缺少技术指标字段（新表结构）
        needs_indicators = not all([
            p.get("ma3"), p.get("ma8"), p.get("ma20"), p.get("ma60"),
            p.get("ma120"), p.get("ma250"), p.get("boll_upper"),
            p.get("kdj_k"), p.get("kdj_d"), p.get("kdj_j"),
            p.get("macd_dif"), p.get("macd_dea"), p.get("macd_hist")
        ])
        
        # 如果没有K线或需要重新计算指标
        if not p["kline"] or len(p["kline"]) < 10 or needs_indicators:
            klines = await fetch_kline_baidu(p["code"], 250)
            if klines:
                p["kline"] = klines
                # 计算技术指标
                p["ma3"] = calc_ma(klines, 3)
                p["ma8"] = calc_ma(klines, 8)
                p["ma20"] = calc_ma(klines, 20)
                p["ma60"] = calc_ma(klines, 60)
                p["ma120"] = calc_ma(klines, 120)
                p["ma250"] = calc_ma(klines, 250)
                p["volume_ratio"] = calc_volume_ratio(klines)
                
                # 布林带
                boll_upper, _, boll_lower = calc_boll(klines)
                p["boll_upper"] = boll_upper
                p["boll_lower"] = boll_lower
                
                # KDJ
                kdj_k, kdj_d, kdj_j = calc_kdj(klines)
                p["kdj_k"] = kdj_k
                p["kdj_d"] = kdj_d
                p["kdj_j"] = kdj_j
                
                # MACD
                macd_dif, macd_dea, macd_hist = calc_macd(klines)
                p["macd_dif"] = macd_dif
                p["macd_dea"] = macd_dea
                p["macd_hist"] = macd_hist
                
                # 走势分类
                from app.services.regime import classify_regime, REGIME_LABEL
                regime_info = classify_regime(klines)
                p["regime_label"] = regime_info.get("label", "")
                
                conn.execute("""
                    UPDATE positions SET kline_data=?, ma3=?, ma8=?, ma20=?, ma60=?, ma120=?, ma250=?,
                        boll_upper=?, boll_lower=?, kdj_k=?, kdj_d=?, kdj_j=?,
                        macd_dif=?, macd_dea=?, macd_hist=?, regime_label=?, volume_ratio=?
                    WHERE code=?
                """, (json.dumps(klines), p["ma3"], p["ma8"], p["ma20"], p["ma60"], 
                      p["ma120"], p["ma250"], boll_upper, boll_lower,
                      kdj_k, kdj_d, kdj_j, macd_dif, macd_dea, macd_hist,
                      p["regime_label"], p["volume_ratio"], p["code"]))
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
@app.get("/api/etf-moneyflow")
async def api_etf_moneyflow(pwd: str = check_password):
    """ETF资金流向（实时）"""
    data = await fetch_etf_money_flow(15)
    return {"items": data}


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
    held_codes = {p["code"] for p in positions}
    # 量化股票池：与 app/quant 策略共用 STOCK_LIST，剔除已持仓的避免重复
    seen = set()
    quant_pool = []
    for s in STOCK_LIST:
        symbol = s["symbol"]
        if symbol in held_codes or symbol in seen:
            continue
        seen.add(symbol)
        quant_pool.append(s)
    return templates.TemplateResponse(
        request,
        "backtest.html",
        {
            "request": request,
            "positions": positions,
            "quant_pool": quant_pool,
            "pwd": pwd,
        }
    )


# === 回测 API ===

async def _fetch_backtest_klines(code: str, period: str) -> list:
    """按周期获取回测用K线：日线约1.5年（550根）；分钟线走新浪源（60/30/15）"""
    # 统一去掉 sh/sz/bj 前缀，腾讯/新浪源均要求裸代码（如 601127）
    for p in ("sh", "sz", "bj"):
        if code.startswith(p):
            code = code[len(p):]
            break
    conn = get_db()
    row = conn.execute("SELECT kline_data FROM positions WHERE code=?", (code,)).fetchone()
    conn.close()

    if period == "1d":
        from app.services.market import fetch_kline_tencent
        klines = await fetch_kline_tencent(code, days=550, full_date=True)
        if len(klines) < 30 and row and row["kline_data"]:
            # 实时拉取失败时回退到持仓缓存
            klines = json.loads(row["kline_data"])
    elif period in ("60m", "30m", "15m"):
        from app.services.market import fetch_kline_sina_minute
        klines = await fetch_kline_sina_minute(code, scale=int(period[:-1]))
    else:
        raise HTTPException(status_code=400, detail="不支持的周期，可选: 1d / 60m / 30m / 15m")

    if len(klines) < 30:
        raise HTTPException(status_code=404, detail="K线数据不足，至少需要30根")
    return klines


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


@app.post("/api/backtest/classify")
async def classify_api(request: Request, pwd: str = check_password):
    """走势量化分类：判断选中股票是 区间型/趋势型/挤压待变/中性，并推荐策略"""
    data = await request.json()
    code = data.get("code")
    period = data.get("period", "1d")
    if not code:
        raise HTTPException(status_code=400, detail="缺少股票代码")

    klines = await _fetch_backtest_klines(code, period)
    from app.services.regime import classify_regime, REGIME_ADVICE
    info = classify_regime(klines)
    advice = REGIME_ADVICE[info["regime"]]
    info.pop("alert", None)
    return {**info, **advice}


# === 走势分类扫描页（全池共享数据） ===

REGIME_CACHE_FILE = BASE_DIR / "data" / "regime_scan.json"


@app.get("/regime", response_class=HTMLResponse)
async def regime_page(request: Request, pwd: str = check_password):
    """全池走势分类扫描页"""
    return templates.TemplateResponse(
        request, "regime.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/regime/latest")
async def regime_latest(request: Request, pwd: str = check_password, refresh: int = 0):
    """最新全池分类扫描结果：当天已有缓存直接返回，否则现场扫描并落盘到 data/regime_scan.json"""
    from app.services.regime import classify_regime, combine_timeframes, REGIME_LABEL, REGIME_ADVICE

    today = datetime.now().strftime("%Y-%m-%d")
    if not refresh and REGIME_CACHE_FILE.exists():
        try:
            cached = json.loads(REGIME_CACHE_FILE.read_text(encoding="utf-8"))
            if cached.get("asOf") == today and cached.get("stocks"):
                return cached
        except Exception:
            pass  # 缓存损坏则重新扫描

    from app.services.market import fetch_kline_tencent, fetch_kline_sina_minute

    pool, seen_symbols = [], set()
    for s in STOCK_LIST:
        if s["symbol"] in seen_symbols:
            continue  # 配置里 688017 出现两次，扫描去重
        seen_symbols.add(s["symbol"])
        pool.append(s)

    async def scan_one(s):
        """日线 + 30分钟双周期并发取数与分类"""
        try:
            kl_d, kl_m = await asyncio.gather(
                fetch_kline_tencent(s["symbol"], days=550, full_date=True),
                fetch_kline_sina_minute(s["symbol"], scale=30),
            )
        except Exception:
            kl_d, kl_m = [], []
        if len(kl_d) < 30:
            return None
        info_d = classify_regime(kl_d)
        info_m = classify_regime(kl_m) if len(kl_m) >= 30 else None
        regime = combine_timeframes(info_d, info_m)
        return {
            "code": s["symbol"], "name": s["name"],
            "regime": regime, "label": REGIME_LABEL[regime],
            "regime_30m": info_m["regime"] if info_m else "unknown",
            "label_30m": info_m["label"] if info_m else "无数据",
            "outside_ratio": info_d["outside_ratio"],
            "bandwidth_pct": info_d["bandwidth_pct"],
            "bandwidth_pct_30m": info_m["bandwidth_pct"] if info_m else None,
            "ma_dev_pct": info_d["ma_dev_pct"],
            "alert": info_d.get("alert"),
            "as_of": kl_d[-1]["date"],
        }

    results = await asyncio.gather(*[scan_one(s) for s in pool])
    stocks, alerts, as_of = [], [], ""
    for r in results:
        if not r:
            continue
        if not as_of:
            as_of = r["as_of"]
        stocks.append({k: v for k, v in r.items() if k not in ("alert", "as_of")})
        if r["alert"]:
            alerts.append({
                "code": r["code"], "name": r["name"],
                "type": r["alert"][0], "text": r["alert"][1],
            })

    counts = {"squeeze": 0, "trending": 0, "ranging": 0, "neutral": 0}
    for st in stocks:
        counts[st["regime"]] = counts.get(st["regime"], 0) + 1
    summary = (
        f"扫描{len(stocks)}只：挤压待变{counts['squeeze']}、趋势型{counts['trending']}、"
        f"区间型{counts['ranging']}、中性{counts['neutral']}"
        + (f"；带宽张开预警{len(alerts)}只" if alerts else "；暂无带宽张开预警")
    )
    result = {
        "asOf": as_of,
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stale": False,
        "counts": counts,
        "stocks": stocks,
        "alerts": alerts,
        "summary": summary,
    }
    try:
        REGIME_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        REGIME_CACHE_FILE.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass  # 落盘失败不影响返回
    return result


# === 研究中心（整合 hermes-skills 的 4 个研究技能） ===
RESEARCH_CACHE_DIR = BASE_DIR / "data"


@app.get("/research", response_class=HTMLResponse)
async def research_hub(request: Request, pwd: str = check_password):
    """研究中心入口页"""
    return templates.TemplateResponse(
        request, "research_hub.html", {"request": request, "pwd": pwd}
    )


@app.get("/research/funds", response_class=HTMLResponse)
async def research_funds_page(request: Request, pwd: str = check_password):
    """基金经理跟踪页"""
    return templates.TemplateResponse(
        request, "research_funds.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/research/funds")
async def research_funds_api(request: Request, pwd: str = check_password, refresh: int = 0):
    """长跑型基金经理核心基金净值（缓存30分钟）"""
    from app.services import research as rs

    async def produce():
        return await rs.fetch_fund_nav()

    return await _research_produce("funds", 30, refresh, produce)


@app.get("/api/research/funds/holdings")
async def research_fund_holdings_api(request: Request, pwd: str = check_password, 
                                      fund_code: str = None, refresh: int = 0):
    """基金经理持仓和季度变化（缓存6小时）"""
    from app.services import research as rs
    
    if not fund_code:
        return {"error": "missing fund_code"}

    async def produce():
        return await rs.fetch_fund_holdings(fund_code)

    return await _research_produce(f"holdings_{fund_code}", 360, refresh, produce)


@app.get("/research/rankings", response_class=HTMLResponse)
async def research_rankings_page(request: Request, pwd: str = check_password):
    """东财模拟组合排行榜页"""
    return templates.TemplateResponse(
        request, "research_rankings.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/research/rankings")
async def research_rankings_api(request: Request, pwd: str = check_password,
                                refresh: int = 0, top: int = 30):
    """东财模拟组合排行榜五维度 + 热门股票统计（缓存30分钟）"""
    from app.services import research as rs
    top = max(10, min(top, 100))

    async def produce():
        return await rs.fetch_em_rankings(top)

    return await _research_produce("rankings", 30, refresh, produce, suffix=f"_{top}")


@app.get("/research/moneyflow", response_class=HTMLResponse)
async def research_moneyflow_page(request: Request, pwd: str = check_password):
    """资金流向背离监测页"""
    return templates.TemplateResponse(
        request, "research_moneyflow.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/research/moneyflow")
async def research_moneyflow_api(request: Request, pwd: str = check_password, refresh: int = 0):
    """价资背离信号 + 主力净流入榜（实时数据，缓存5分钟）"""
    from app.services import research as rs

    async def produce():
        return await rs.fetch_money_flow()

    return await _research_produce("moneyflow", 5, refresh, produce)


@app.get("/research/pboc", response_class=HTMLResponse)
async def research_pboc_page(request: Request, pwd: str = check_password):
    """央行流动性仪表板页"""
    return templates.TemplateResponse(
        request, "research_pboc.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/research/pboc")
async def research_pboc_api(request: Request, pwd: str = check_password, refresh: int = 0):
    """央行公开市场操作：逆回购/买断式，投放到期净投放（缓存6小时）"""
    from app.services import research as rs

    async def produce():
        return await rs.fetch_pboc_liquidity()

    return await _research_produce("pboc", 360, refresh, produce)


async def _research_produce(name: str, ttl_minutes: int, refresh: int, produce, suffix: str = ""):
    """async 版通用缓存：TTL 内返回缓存，否则 await producer 并落盘。
    抓取失败时若有旧缓存则返回旧数据并标记 stale，否则抛 502"""
    RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = RESEARCH_CACHE_DIR / f"research_{name}{suffix}.json"
    if not refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            fetched = datetime.strptime(cached.get("fetchedAt", ""), "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - fetched).total_seconds() < ttl_minutes * 60:
                return cached
        except Exception:
            pass
    try:
        result = await produce()
    except Exception as e:
        if cache_file.exists():
            try:
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                cached["stale"] = True
                cached["error"] = str(e)[:120]
                return cached
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=str(e)[:200])
    try:
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass  # 落盘失败不影响返回
    return result


@app.post("/api/backtest/run")
async def run_backtest_api(request: Request, pwd: str = check_password):
    """运行回测"""
    data = await request.json()

    code = data.get("code")
    strategy = data.get("strategy", "ma")
    initial_cash = data.get("initial_cash", 100000)
    engine = data.get("engine", "backtrader")
    commission = data.get("commission", 0.001)  # 单边手续费率，默认千一
    period = data.get("period", "1d")

    if not code:
        raise HTTPException(status_code=400, detail="缺少股票代码")

    klines = await _fetch_backtest_klines(code, period)

    # 运行回测（引擎模块懒加载，缺装时返回 503；zipline 无策略参数）
    if engine in ("backtrader", "zipline", "vnpy"):
        run_fn, _ = _load_backtest_engine(engine)
        if engine == "zipline":
            result = run_fn(klines, initial_cash)
        else:
            result = run_fn(klines, strategy, initial_cash, commission=commission)
    else:
        raise HTTPException(status_code=400, detail="不支持的回测引擎")

    return result


# === 量化信号中心（app/quant 四大能力 Web 化：策略信号/趋势过滤/箱体/筹码） ===

@app.get("/quant", response_class=HTMLResponse)
async def quant_hub(request: Request, pwd: str = check_password):
    """量化信号中心入口页"""
    return templates.TemplateResponse(
        request, "quant.html", {"request": request, "pwd": pwd}
    )


@app.get("/api/quant/strategies")
async def quant_strategies(request: Request, pwd: str = check_password):
    """可扫描策略列表"""
    from app.services.quant_scan import list_strategies
    return {"strategies": list_strategies()}


@app.get("/api/quant/signals")
async def quant_signals_api(request: Request, pwd: str = check_password,
                            strategy: str = "breakout", refresh: int = 0):
    """策略信号扫描（当日缓存，refresh=1 强制重扫）"""
    from app.services.quant_scan import scan_signals
    try:
        return await asyncio.to_thread(scan_signals, strategy, bool(refresh))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/quant/trend")
async def quant_trend_api(request: Request, pwd: str = check_password,
                          mode: str = "strong", refresh: int = 0):
    """全池趋势过滤（strong/loose/oversold）"""
    from app.services.quant_scan import scan_trend
    try:
        return await asyncio.to_thread(scan_trend, mode, bool(refresh))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/quant/box")
async def quant_box_api(request: Request, pwd: str = check_password,
                        refresh: int = 0):
    """全池箱体检测"""
    from app.services.quant_scan import scan_box
    return await asyncio.to_thread(scan_box, bool(refresh))


@app.get("/api/quant/chip")
async def quant_chip_api(request: Request, pwd: str = check_password,
                         refresh: int = 0):
    """全池筹码集中度扫描"""
    from app.services.quant_scan import scan_chip
    return await asyncio.to_thread(scan_chip, bool(refresh))


@app.get("/api/quant/chip/detail")
async def quant_chip_detail_api(request: Request, pwd: str = check_password,
                                symbol: str = ""):
    """单股筹码分布明细（画图用）"""
    from app.services.quant_scan import chip_detail
    if not symbol:
        raise HTTPException(status_code=400, detail="缺少股票代码")
    try:
        return await asyncio.to_thread(chip_detail, symbol)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8888)
