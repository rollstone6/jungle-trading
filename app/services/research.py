"""研究中心数据服务 - 整合 hermes-skills 的四个研究技能

1. 基金经理跟踪（fund-managers-tracker）：天天基金 fundgz JSONP 接口
2. 东方财富模拟组合排行榜（eastmoney-rankings）：simqry2 JSON API
3. 资金流向背离监测（cn-finance-data Part E）：push2 实时资金流 API
4. 央行流动性仪表板（pboc-liquidity-dashboard）：pbc.gov.cn 公开市场操作公告
"""
import asyncio
import json
import re
from datetime import datetime, timedelta

import httpx

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# === 1. 基金经理跟踪：4 位长跑型经理的核心 A 份额基金 ===
TRACK_FUNDS = [
    {"code": "161005", "name": "富国天惠成长混合A", "manager": "朱少醒", "company": "富国基金", "note": "从业20年只管一只，累计回报1847%"},
    {"code": "001513", "name": "易方达信息产业混合A", "manager": "郑希", "company": "易方达", "note": "科技成长，任期回报795%+"},
    {"code": "010013", "name": "易方达信息行业精选股票A", "manager": "郑希", "company": "易方达", "note": ""},
    {"code": "519702", "name": "交银趋势混合A", "manager": "杨金金", "company": "交银施罗德", "note": "画线派，任期回报201%，回撤仅20.9%"},
    {"code": "014038", "name": "交银启诚混合A", "manager": "杨金金", "company": "交银施罗德", "note": ""},
    {"code": "310328", "name": "申万菱信新动力混合A", "manager": "贾成东", "company": "申万菱信", "note": "与龚霄共管"},
]


async def fetch_fund_nav() -> dict:
    """天天基金历史净值 API（f10/lsjz，确认净值+日增长率）"""

    async def one(client: httpx.AsyncClient, fund: dict) -> dict:
        url = (
            "https://api.fund.eastmoney.com/f10/lsjz"
            f"?fundCode={fund['code']}&pageIndex=1&pageSize=1"
        )
        headers = {**UA, "Referer": "https://fundf10.eastmoney.com/"}
        try:
            data = (await client.get(url, headers=headers)).json()
            row = (data.get("Data") or {}).get("LSJZList") or []
            if not row:
                raise ValueError("无净值数据")
            d = row[0]
            return {
                **fund,
                "nav": d.get("DWJZ"),            # 最新确认单位净值
                "acc_nav": d.get("LJJZ"),        # 累计净值
                "nav_date": d.get("FSRQ"),       # 净值日期
                "chg": d.get("JZZZL"),           # 日增长率 %
                "ok": True,
            }
        except Exception as e:
            return {**fund, "ok": False, "error": str(e)[:80]}

    async with httpx.AsyncClient(timeout=12) as client:
        rows = await asyncio.gather(*[one(client, f) for f in TRACK_FUNDS])

    ok_rows = [r for r in rows if r.get("ok")]
    best, worst = None, None
    chgs = []
    for r in ok_rows:
        try:
            c = float(r.get("chg") or 0)
            chgs.append((c, r))
        except (TypeError, ValueError):
            pass
    if chgs:
        best = max(chgs, key=lambda x: x[0])[1]
        worst = min(chgs, key=lambda x: x[0])[1]
    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "funds": rows,
        "best": {"name": best["name"], "manager": best["manager"], "chg": best.get("chg")} if best else None,
        "worst": {"name": worst["name"], "manager": worst["manager"], "chg": worst.get("chg")} if worst else None,
    }


# === 2. 东方财富模拟组合排行榜 ===
RANK_DIMS = [
    ("10001", "日排行"), ("10005", "5日排行"), ("10020", "20日排行"),
    ("10250", "250日排行"), ("10000", "总收益排行"),
]


def _is_bj(code: str) -> bool:
    """北交所标的：代码以 4/8/9 开头"""
    return bool(code) and code[0] in "489"


async def fetch_em_rankings(top: int = 30) -> dict:
    """五个维度各取 TOP N，并统计热门股票出现频率"""
    headers = {**UA, "Referer": "https://group.eastmoney.com/"}

    async def one_dim(client: httpx.AsyncClient, syl_type: str, label: str) -> dict:
        url = (
            "https://simqry2.eastmoney.com/qry_tzzh_v2?type=spo_rank_syl&plat=2&ver=web20"
            f"&sylType={syl_type}&recIdx=1&recCnt={top}"
        )
        try:
            data = (await client.get(url, headers=headers)).json()
            rows = (data.get("data") or [])[:top]
            items = [{
                "rank": r.get("rank"), "name": r.get("zuheName"),
                "ret_day": r.get("syl_dr"), "ret_5d": r.get("syl_5r"),
                "ret_20d": r.get("syl_20r"), "ret_250d": r.get("syl_250r"),
                "ret_total": r.get("zsyl"), "stock": r.get("stkName"),
                "stock_code": r.get("stkCode"), "op": r.get("mmbz"),
                "win": r.get("dealWinCnt"), "lose": r.get("dealfailCnt"),
                "win_rate": r.get("dealRate"), "op_time": r.get("cjsj"),
                "followers": r.get("concerned"),
            } for r in rows]
            return {"dim": syl_type, "label": label, "items": items}
        except Exception as e:
            return {"dim": syl_type, "label": label, "items": [], "error": str(e)[:80]}

    async with httpx.AsyncClient(timeout=15) as client:
        dims = await asyncio.gather(*[one_dim(client, t, l) for t, l in RANK_DIMS])

    # 热门股票频率统计（跨维度，含北交所标注）
    freq: dict = {}
    for d in dims:
        for it in d["items"]:
            code, name = it.get("stock_code") or "", it.get("stock") or ""
            if not code or not name:
                continue
            key = (code, name)
            e = freq.setdefault(key, {"code": code, "name": name, "count": 0, "bj": _is_bj(code)})
            e["count"] += 1
    hot = sorted(freq.values(), key=lambda x: x["count"], reverse=True)[:20]

    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "top": top,
        "dims": dims,
        "hot_stocks": hot,
    }


# === 3. 资金流向背离监测（东财 push2 实时接口，镜像轮询） ===
PUSH2_HOSTS = [
    "push2.eastmoney.com", "1.push2.eastmoney.com", "17.push2.eastmoney.com",
    "33.push2.eastmoney.com", "92.push2.eastmoney.com",
]
MONEY_FLOW_PATH = (
    "/api/qt/clist/get?pn=1&pz=500&po=1&np=1&ut=b2884a393a59ad64002292a3e90d46f5"
    "&fltt=2&invt=2&fid=f62&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    "&fields=f12,f14,f2,f3,f62,f128,f136,f115"
)


async def fetch_money_flow() -> dict:
    """全市场资金流 + 价资背离信号（跌>1% 且主力净流入占比>1.5% 且成交额>5000万）"""
    headers = {**UA, "Referer": "https://data.eastmoney.com/"}
    rows = []
    last_err = ""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        for host in PUSH2_HOSTS:
            try:
                data = (await client.get(f"https://{host}{MONEY_FLOW_PATH}", headers=headers)).json()
                rows = (data.get("data") or {}).get("diff") or []
                if rows:
                    break
                last_err = f"{host} 返回空"
            except Exception as e:
                last_err = f"{host}: {str(e)[:60]}"
    if not rows:
        raise RuntimeError(f"东财资金流接口不可用（{last_err}），可能触发限流，请稍后重试")
    signals, inflow_top = [], []
    for r in rows:
        code, name = str(r.get("f12") or ""), str(r.get("f14") or "")
        try:
            chg = float(r.get("f3"))
            inflow = float(r.get("f62"))        # 主力净流入（元）
            turnover = float(r.get("f115"))     # 成交额（元）
        except (TypeError, ValueError):
            continue
        if turnover <= 0:
            continue
        rate = inflow / turnover * 100
        rec = {
            "code": code, "name": name, "price": r.get("f2"),
            "chg": chg, "main_inflow_wan": round(inflow / 10000, 1),
            "inflow_rate": round(rate, 2),
            "turnover_yi": round(turnover / 1e8, 2),
            "super_inflow_wan": round(float(r.get("f128") or 0) / 10000, 1),
            "large_inflow_wan": round(float(r.get("f136") or 0) / 10000, 1),
        }
        if inflow > 0:
            inflow_top.append(rec)
        # 背离信号：跌幅、净流入、占比、成交额过滤；剔除 ST / 北交所 / 新股
        if (chg < -1.0 and inflow > 0 and rate > 1.5 and turnover > 5e7
                and "ST" not in name and not _is_bj(code) and not name.startswith("N")):
            signals.append(rec)

    signals.sort(key=lambda x: x["main_inflow_wan"], reverse=True)
    inflow_top.sort(key=lambda x: x["main_inflow_wan"], reverse=True)
    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "universe": len(rows),
        "signal_count": len(signals),
        "signals": signals[:50],
        "inflow_top": inflow_top[:20],
    }


# === 4. 央行公开市场操作流动性 ===
PBC_BASE = "http://www.pbc.gov.cn"
RR_LIST = f"{PBC_BASE}/zhengcehuobisi/125207/125213/125431/125475/index.html"   # 逆回购（每日）
BO_LIST = f"{PBC_BASE}/zhengcehuobisi/125207/125213/125431/125469/index.html"   # 买断式逆回购/央票


def _decode(resp: httpx.Response) -> str:
    """pbc.gov.cn 新老页面编码混杂：utf-8 优先，失败回退 gbk"""
    enc = resp.encoding or ""
    if enc.lower() in ("utf-8", "utf8"):
        return resp.text
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("gbk", errors="replace")


def _maturity(date_str: str, days: int = 7) -> str:
    """7 个自然日后到期，遇周末顺延（简化版，节假日未剔除）"""
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _parse_rr_article(html: str) -> dict | None:
    """解析逆回购公告：日期、量、利率；零操作也是有效数据点"""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", "", text)
    m = re.search(r"(20\d\d)年(\d{1,2})月(\d{1,2})日", text)
    if not m:
        return None
    date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.search(r"逆回购操作量为零|不开展逆回购操作", text):
        return {"date": date, "amount": 0.0, "rate": None, "term": "7天", "type": "逆回购"}
    m2 = re.search(r"开展了(\d+(?:\.\d+)?)亿元7天期逆回购操作", text)
    if not m2:
        return None
    rate = re.search(r"(?:操作利率|中标利率)[^0-9]{0,6}(\d+\.\d+)%", text) \
        or re.search(r"7天[^0-9%]{0,12}(\d+\.\d+)%", text) \
        or re.search(r"(\d+\.\d+)%", text)
    return {
        "date": date, "amount": float(m2.group(1)),
        "rate": float(rate.group(1)) if rate else None,
        "term": "7天", "type": "逆回购",
    }


def _parse_bo_article(html: str) -> list[dict]:
    """解析买断式逆回购公告（期限/到期日在公告中明确；格式有变体，宽松匹配）"""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", "", text)
    out = []
    pat_full = re.compile(
        r"开展(\d+(?:\.\d+)?)亿元买断式逆回购操作，期限为(\d+)个月（(\d+)天），到期日为(20\d\d-\d{2}-\d{2})")
    pat_lite = re.compile(
        r"开展(\d+(?:\.\d+)?)亿元买断式逆回购操作[^。；]{0,100}?到期日为(20\d\d-\d{2}-\d{2})")
    for m in pat_full.finditer(text):
        out.append({
            "type": "买断式逆回购", "amount": float(m.group(1)),
            "term": f"{m.group(2)}个月", "days": int(m.group(3)),
            "date_maturity": m.group(4),
        })
    for m in pat_lite.finditer(text):
        if any(abs(float(o["amount"]) - float(m.group(1))) < 1e-6
               and o["date_maturity"] == m.group(2) for o in out):
            continue  # 已被完整模式捕获
        out.append({
            "type": "买断式逆回购", "amount": float(m.group(1)),
            "term": None, "days": None, "date_maturity": m.group(2),
        })
    m = re.search(r"(20\d\d)年(\d{1,2})月(\d{1,2})日", text)
    op_date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None
    for o in out:
        o["date"] = op_date
    return out


async def _article_links(client: httpx.AsyncClient, list_url: str, marker: str, limit: int) -> list[str]:
    html = _decode(await client.get(list_url, headers=UA))
    # pbc.gov.cn 页面混用单双引号
    links = re.findall(r'href=["\']([^"\']*' + re.escape(marker) + r'/[^"\']*?\.html)["\']', html)
    seen, out = set(), []
    for href in links:
        url = href if href.startswith("http") else PBC_BASE + href
        if url.rstrip("/").endswith(f"{marker}/index.html"):
            continue  # 列表页自身
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


async def fetch_pboc_liquidity() -> dict:
    """逆回购（近30条公告）+ 买断式逆回购，构建投放/到期/净投放序列"""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        rr_links, bo_links = await asyncio.gather(
            _article_links(client, RR_LIST, "125475", 30),
            _article_links(client, BO_LIST, "125469", 10),
        )
        rr_htmls = await asyncio.gather(
            *[client.get(u, headers=UA) for u in rr_links], return_exceptions=True)
        bo_htmls = await asyncio.gather(
            *[client.get(u, headers=UA) for u in bo_links], return_exceptions=True)

    rr_ops, errors = [], 0
    for resp in rr_htmls:
        try:
            op = _parse_rr_article(_decode(resp))
            if op:
                rr_ops.append(op)
        except Exception:
            errors += 1
    rr_ops.sort(key=lambda x: x["date"], reverse=True)

    bo_ops = []
    for resp in bo_htmls:
        try:
            bo_ops.extend(_parse_bo_article(_decode(resp)))
        except Exception:
            errors += 1
    bo_ops = [o for o in bo_ops if o.get("date")]
    bo_ops.sort(key=lambda x: x["date"], reverse=True)

    # 到期序列：近30日每日到期 = 7天前操作量（遇周末顺延推算）；买断式用公告到期日
    today = datetime.now().date()
    rr_by_date = {o["date"]: o["amount"] for o in rr_ops}
    daily = []
    for i in range(29, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        amt = rr_by_date.get(d, 0.0)
        if datetime.strptime(d, "%Y-%m-%d").weekday() < 5:
            daily.append({
                "date": d, "amount": amt, "maturity": rr_by_date.get(_maturity(d), 0.0),
                "net": round(amt - rr_by_date.get(_maturity(d), 0.0), 1),
            })
    upcoming_bo = [
        {**o, "days_to_maturity": (datetime.strptime(o["date_maturity"], "%Y-%m-%d").date() - today).days}
        for o in bo_ops if o.get("date_maturity") and datetime.strptime(o["date_maturity"], "%Y-%m-%d").date() >= today
    ]
    upcoming_bo.sort(key=lambda x: x["date_maturity"])

    recent = rr_ops[0] if rr_ops else None
    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "today": {
            "date": recent["date"] if recent else None,
            "amount": recent["amount"] if recent else None,
            "rate": recent["rate"] if recent else None,
        },
        "rr_ops": rr_ops[:15],
        "bo_ops": bo_ops[:10],
        "daily": daily,
        "upcoming_bo": upcoming_bo[:8],
        "parse_errors": errors,
    }
