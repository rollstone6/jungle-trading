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


def parse_fund_holdings_html(html: str) -> list[dict]:
    """解析天天基金持仓HTML，返回最近两个季度的持仓数据"""
    # 按 <h4 class='t'> 分割各季度内容
    sections = re.split(r'<h4[^>]*class=[\'"]t[\'"]', html)
    # sections[0] 是前缀，sections[1] 是Q1（最新），sections[2] 是Q2...
    
    dates = re.findall(r'截止至：.*?<font[^>]*>(\d{4}-\d{2}-\d{2})</font>', html)
    
    quarters = []
    for idx in range(1, len(sections)):
        section = sections[idx]
        
        # 提取季度名称
        q_match = re.search(r'(\d{4})年(\d)季度', section)
        if not q_match:
            continue
        year, q_num = q_match.groups()
        quarter_name = f"{year}年{q_num}季度"
        
        # 日期：按顺序取（第1个section对应第1个日期）
        date_idx = idx - 1
        date = dates[date_idx] if date_idx < len(dates) else ""
        
        # 提取所有数据行
        trs = re.findall(r'<tr>(.*?)</tr>', section, re.DOTALL)
        stocks = []
        for tr in trs:
            tds = re.findall(r'<td[^>]*>(.*?)</td>', tr, re.DOTALL)
            # 最新季度9列(含实时价)，历史季度7列；但末3列(占比/万股/万元)固定
            if len(tds) < 7:
                continue
            # cell[0]=序号, [1]=股票代码, [2]=股票名称
            # 末3列: [-3]=占比, [-2]=万股, [-1]=万元
            rank_text = re.sub(r'<[^>]+>', '', tds[0]).strip()
            code_text = re.sub(r'<[^>]+>', '', tds[1]).strip()
            name_text = re.sub(r'<[^>]+>', '', tds[2]).strip()
            ratio_text = re.sub(r'<[^>]+>', '', tds[-3]).strip()
            shares_text = re.sub(r'<[^>]+>', '', tds[-2]).strip()
            mv_text = re.sub(r'<[^>]+>', '', tds[-1]).strip()
            
            try:
                rank = int(rank_text)
                code = code_text
                name = name_text
                ratio = float(ratio_text.replace('%', ''))
                shares = float(shares_text.replace(',', ''))
                mv = float(mv_text.replace(',', ''))
            except (ValueError, TypeError):
                continue
            
            stocks.append({
                "rank": rank,
                "code": code,
                "name": name,
                "ratio": ratio,
                "shares": shares,      # 万股
                "market_value": mv,    # 万元
            })
        
        if stocks:  # 只添加有数据的季度
            quarters.append({
                "quarter": quarter_name,
                "date": date,
                "stocks": stocks,
            })
    
    return quarters[:2]  # 只返回最近两个季度


def compare_quarters(prev_stocks: list[dict], curr_stocks: list[dict]) -> dict:
    """对比两个季度持仓，返回变化"""
    prev_map = {s["code"]: s for s in prev_stocks}
    curr_map = {s["code"]: s for s in curr_stocks}
    
    prev_codes = set(prev_map.keys())
    curr_codes = set(curr_map.keys())
    
    added = curr_codes - prev_codes
    removed = prev_codes - curr_codes
    kept = prev_codes & curr_codes
    
    changes = []
    for code in kept:
        prev = prev_map[code]
        curr = curr_map[code]
        ratio_diff = curr["ratio"] - prev["ratio"]
        if abs(ratio_diff) > 0.1:  # 超过0.1%的变化才算显著
            changes.append({
                "code": code,
                "name": curr["name"],
                "prev_ratio": prev["ratio"],
                "curr_ratio": curr["ratio"],
                "diff": round(ratio_diff, 2),
                "action": "加仓" if ratio_diff > 0 else "减仓",
            })
    
    return {
        "added": [curr_map[c] for c in added],
        "removed": [prev_map[c] for c in removed],
        "changed": sorted(changes, key=lambda x: abs(x["diff"]), reverse=True),
    }


async def fetch_fund_holdings(fund_code: str) -> dict:
    """获取基金持仓数据和季度变化"""
    url = f"https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code={fund_code}&topline=10"
    headers = {**UA, "Referer": "https://fundf10.eastmoney.com/"}
    
    async with httpx.AsyncClient(timeout=12) as client:
        try:
            resp = await client.get(url, headers=headers)
            text = resp.text
            
            # 提取HTML内容
            content_match = re.search(r'content:"(.*?)"', text, re.DOTALL)
            if not content_match:
                return {"holdings": [], "comparison": None, "error": "无法解析数据"}
            
            html = content_match.group(1)
            quarters = parse_fund_holdings_html(html)
            
            if len(quarters) < 2:
                return {
                    "holdings": quarters[0]["stocks"] if quarters else [],
                    "quarter": quarters[0]["quarter"] if quarters else "",
                    "comparison": None,
                }
            
            # 对比最近两个季度
            curr_q = quarters[0]
            prev_q = quarters[1]
            comparison = compare_quarters(prev_q["stocks"], curr_q["stocks"])
            
            return {
                "holdings": curr_q["stocks"],
                "quarter": curr_q["quarter"],
                "date": curr_q["date"],
                "comparison": comparison,
                "prev_quarter": prev_q["quarter"],
            }
        except Exception as e:
            return {"holdings": [], "comparison": None, "error": str(e)[:80]}


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
    """N 个自然日后到期，遇周末顺延（简化版，节假日未剔除）"""
    d = datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _make_mat_fn(trade_days: list[str]):
    """返回到期推算函数：顺延到 >= op+Nd 的首个交易日；无日历数据时退化为周末顺延"""
    from bisect import bisect_left

    def mat(date_str: str, days: int = 7) -> str:
        target = (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
        if not trade_days:
            return _maturity(date_str, days)
        i = bisect_left(trade_days, target)
        return trade_days[i] if i < len(trade_days) else _maturity(date_str, days)
    return mat


def _parse_rr_article(html: str) -> list[dict]:
    """解析逆回购公告：支持多期限（隔夜/7天/14天/28天），零操作与"同时开展隔夜"并存"""
    text = re.sub(r"<[^>]+>", "", html)
    text = re.sub(r"\s+", "", text)
    m = re.search(r"(20\d\d)年(\d{1,2})月(\d{1,2})日", text)
    if not m:
        return []
    date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # 从公告表格按期限提取操作利率：如 "7天1.40%"
    rates: dict[int, float] = {}
    for rm in re.finditer(r"(\d+)天(\d\.\d{2})%", text):
        rates.setdefault(int(rm.group(1)), float(rm.group(2)))
    ops = []
    if re.search(r"(?:7天期|七天期)逆回购操作量为零|不开展逆回购操作", text):
        ops.append({"date": date, "amount": 0.0, "rate": None, "term_days": 7, "term": "7天", "type": "逆回购"})
    for m2 in re.finditer(r"(\d+(?:\.\d+)?)亿元(?:(\d+)天期|隔夜)逆回购操作", text):
        td = int(m2.group(2)) if m2.group(2) else 1
        if td == 7 and any(o["term_days"] == 7 and o["amount"] == 0.0 for o in ops):
            continue
        ops.append({
            "date": date, "amount": float(m2.group(1)),
            "rate": rates.get(td) if td == 7 else None,
            "term_days": td, "term": "隔夜" if td == 1 else f"{td}天", "type": "逆回购",
        })
    return ops


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


async def _fetch_bo_list(client: httpx.AsyncClient) -> list[tuple[str, str]]:
    """买断式公告：标题无日期，从 URL 时间戳(20260923140942…)提取操作日"""
    html = _decode(await client.get(BO_LIST, headers=UA))
    seen, out = set(), []
    for m in re.finditer(r'href=["\']([^"\']*125469/(\d{8})\d{12}/index\.html)["\']', html):
        href, ds = m.group(1), m.group(2)
        url = href if href.startswith("http") else PBC_BASE + (href if href.startswith("/") else "/" + href)
        date = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"
        if url not in seen:
            seen.add(url)
            out.append((url, date))
    return out[:12]


async def _sse_index_kline(client: httpx.AsyncClient, days: int) -> dict[str, float]:
    """上证指数收盘价 {date: close}，腾讯源"""
    try:
        r = await client.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": f"sh000001,day,,,{days},qfq"})
        data = r.json()["data"]["sh000001"]
        rows = data.get("qfqday") or data.get("day") or []
        return {row[0]: float(row[2]) for row in rows}
    except Exception:
        return {}


async def fetch_pboc_liquidity() -> dict:
    """逆回购（近40条公告，支持多期限）+ 买断式逆回购 → 投放/到期/净投放/余额/未来分布"""
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        rr_links, bo_pairs = await asyncio.gather(
            _article_links(client, RR_LIST, "125475", 40),
            _fetch_bo_list(client),
        )
        rr_htmls, bo_htmls, sse = await asyncio.gather(
            asyncio.gather(*[client.get(u, headers=UA) for u in rr_links], return_exceptions=True),
            asyncio.gather(*[client.get(u, headers=UA) for u, _ in bo_pairs], return_exceptions=True),
            _sse_index_kline(client, 60),
        )

    rr_ops, errors = [], 0
    for resp in rr_htmls:
        try:
            rr_ops.extend(_parse_rr_article(_decode(resp)))
        except Exception:
            errors += 1
    rr_ops.sort(key=lambda x: x["date"], reverse=True)

    bo_ops = []
    for (url, list_date), resp in zip(bo_pairs, bo_htmls):
        try:
            for o in _parse_bo_article(_decode(resp)):
                o["date"] = o.get("date") or list_date  # 正文优先，列表页标题兜底
                bo_ops.append(o)
        except Exception:
            errors += 1
    bo_ops = [o for o in bo_ops if o.get("date") and o.get("date_maturity")]
    bo_ops.sort(key=lambda x: x["date"], reverse=True)

    today = datetime.now().date()
    horizon_end = today + timedelta(days=95)
    trade_days = sorted(sse.keys())
    mat_fn = _make_mat_fn(trade_days)

    # ---- 每日到期矩阵：逆回购按交易日历顺延（隔夜=次一交易日）；买断式用公告到期日 ----
    mat_by_date: dict[str, float] = {}
    for o in rr_ops:
        mdate = mat_fn(o["date"], o.get("term_days", 7))
        mat_by_date[mdate] = mat_by_date.get(mdate, 0.0) + o["amount"]
    for o in bo_ops:
        mat_by_date[o["date_maturity"]] = mat_by_date.get(o["date_maturity"], 0.0) + o["amount"]

    # ---- 每日投放 ----
    infl_rr: dict[str, float] = {}
    infl_bo: dict[str, float] = {}
    for o in rr_ops:
        infl_rr[o["date"]] = infl_rr.get(o["date"], 0.0) + o["amount"]
    for o in bo_ops:
        infl_bo[o["date"]] = infl_bo.get(o["date"], 0.0) + o["amount"]

    # ---- 近30个交易日的序列（用上证指数K线日历剔除节假日） ----
    daily = []
    cum = 0.0
    d = today - timedelta(days=29)
    while d <= today:
        ds = d.strftime("%Y-%m-%d")
        is_td = (ds in trade_days) if trade_days else d.weekday() < 5
        if is_td or (ds == today.strftime("%Y-%m-%d") and d.weekday() < 5):
            mat = mat_by_date.get(ds, 0.0)
            inf = infl_rr.get(ds, 0.0) + infl_bo.get(ds, 0.0)
            net = round(inf - mat, 1)
            cum = round(cum + net, 1)
            daily.append({
                "date": ds, "inflow": round(inf, 1), "maturity": round(mat, 1),
                "net": net, "cum": cum, "index": sse.get(ds),
                "rr": round(infl_rr.get(ds, 0.0), 1), "bo": round(infl_bo.get(ds, 0.0), 1),
            })
        d += timedelta(days=1)

    # ---- 存量余额：仍存活的逆回购 + 买断式（到期日 >= 今日） ----
    rr_balance = sum(o["amount"] for o in rr_ops
                     if datetime.strptime(mat_fn(o["date"], o.get("term_days", 7)), "%Y-%m-%d").date() >= today)
    bo_balance = sum(o["amount"] for o in bo_ops
                     if datetime.strptime(o["date_maturity"], "%Y-%m-%d").date() >= today)

    # ---- 未来到期分布（按月 + 按类型） ----
    future_month: dict[str, dict[str, float]] = {}
    for o in rr_ops:
        mdate = _maturity(o["date"], o.get("term_days", 7))
        md = datetime.strptime(mdate, "%Y-%m-%d").date()
        if today < md <= horizon_end:
            k = mdate[:7]
            future_month.setdefault(k, {"逆回购": 0.0, "买断式逆回购": 0.0})["逆回购"] += o["amount"]
    for o in bo_ops:
        md = datetime.strptime(o["date_maturity"], "%Y-%m-%d").date()
        if today < md <= horizon_end:
            k = o["date_maturity"][:7]
            future_month.setdefault(k, {"逆回购": 0.0, "买断式逆回购": 0.0})["买断式逆回购"] += o["amount"]
    future_dist = [{"month": k, "rr": round(v["逆回购"], 1), "bo": round(v["买断式逆回购"], 1)}
                   for k, v in sorted(future_month.items())]

    # ---- 未来两周单日到期 TOP（用于"下一个大额到期日"） ----
    upcoming_days = []
    for ds, amt in mat_by_date.items():
        dd = datetime.strptime(ds, "%Y-%m-%d").date()
        if today < dd <= today + timedelta(days=14) and amt > 0:
            upcoming_days.append({"date": ds, "amount": round(amt, 1)})
    upcoming_days.sort(key=lambda x: (-x["amount"], x["date"]))

    # ---- 今日/明日 ----
    today_s = today.strftime("%Y-%m-%d")
    tomorrow_s = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    latest_op = rr_ops[0] if rr_ops else None
    recent5 = [x for x in daily if x["inflow"] > 0 or x["maturity"] > 0][-5:]

    # ---- 未来买断式到期明细表 ----
    upcoming_bo = [
        {**o, "days_to_maturity": (datetime.strptime(o["date_maturity"], "%Y-%m-%d").date() - today).days}
        for o in bo_ops if datetime.strptime(o["date_maturity"], "%Y-%m-%d").date() >= today
    ]
    upcoming_bo.sort(key=lambda x: x["date_maturity"])

    # ---- 自动要点（当前状态/展望/资金面/风险） ----
    points = {"status": [], "outlook": [], "env": [], "risk": []}
    if latest_op:
        points["status"].append({
            "tone": "g" if latest_op["amount"] > 0 else "y",
            "title": "最新操作", "text": f"{latest_op['date']} 逆回购 {latest_op['amount']:g} 亿元"
            + (f"，利率 {latest_op['rate']}%" if latest_op.get("rate") else "") + "。"})
    if recent5:
        avg5 = sum(x["inflow"] for x in recent5) / len(recent5)
        points["status"].append({"tone": "b", "title": "操作节奏", "text":
                                 f"近5个交易日日均投放 {avg5:,.0f} 亿元，累计净投放 "
                                 f"{'+' if daily and daily[-1]['cum']>=0 else ''}{daily[-1]['cum']:,.0f} 亿元。"})
    points["status"].append({"tone": "g", "title": "存量余额", "text":
                             f"逆回购余额约 {rr_balance:,.0f} 亿元，买断式逆回购余额约 {bo_balance:,.0f} 亿元。"})
    if upcoming_days:
        top = upcoming_days[0]
        points["outlook"].append({"tone": "r", "title": "下一个大额到期日", "text":
                                  f"{top['date']} 预计到期 {top['amount']:,.0f} 亿元，为未来两周最大单日期量。"})
    if future_dist:
        fm = future_dist[0]
        points["outlook"].append({"tone": "y", "title": "到期压力分布", "text":
                                  f"{fm['month']} 合计到期约 {fm['rr'] + fm['bo']:,.0f} 亿元"
                                  f"（逆回购 {fm['rr']:,.0f} + 买断式 {fm['bo']:,.0f}）。"})
    points["outlook"].append({"tone": "b", "title": "关键观察点", "text":
                              "税期高峰、政府债缴款、跨季时点前后央行是否加大投放对冲。"})
    rate_op = next((o for o in rr_ops if o.get("rate")), None)
    if rate_op:
        prev_rate = next((o["rate"] for o in rr_ops[rr_ops.index(rate_op) + 1:] if o.get("rate")), None)
        chg = "" if prev_rate in (None, rate_op["rate"]) else f"（前值 {prev_rate}%）"
        points["env"].append({"tone": "g", "title": "政策利率", "text":
                              f"7天期逆回购操作利率 {rate_op['rate']}%{chg}，"
                              + ("维持不变，短期货币政策取向保持稳定。" if not chg else "出现调整，关注信号意义。")})
    if daily and len(daily) >= 5:
        net5 = sum(x["net"] for x in daily[-5:])
        points["risk"].append({"tone": "r" if net5 < 0 else "g", "title": "近期资金面", "text":
                               f"近5个交易日净{'投放' if net5>=0 else '回笼'} {abs(net5):,.0f} 亿元，"
                               + ("流动性呵护明显。" if net5 >= 0 else "若央行不加量续作，资金面或现边际收紧。")})
    for o in upcoming_bo[:2]:
        points["risk"].append({"tone": "y", "title": "买断式临近到期", "text":
                               f"{o['date_maturity']} 有 {o['amount']:,.0f} 亿元"
                               f"{o.get('term') or ''}买断式逆回购到期，需关注续作安排。"})

    return {
        "fetchedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "today": {
            "date": latest_op["date"] if latest_op else today_s,
            "inflow": round(infl_rr.get(today_s, 0.0) + infl_bo.get(today_s, 0.0), 1),
            "maturity": round(mat_by_date.get(today_s, 0.0), 1),
            "rate": latest_op.get("rate") if latest_op else None,
        },
        "tomorrow_maturity": round(mat_by_date.get(tomorrow_s, 0.0), 1),
        "balance": {"rr": round(rr_balance, 1), "bo": round(bo_balance, 1),
                    "total": round(rr_balance + bo_balance, 1)},
        "daily": daily,
        "future_dist": future_dist,
        "upcoming_days": upcoming_days[:6],
        "upcoming_bo": upcoming_bo[:8],
        "rr_ops": rr_ops[:40],
        "points": points,
        "parse_errors": errors,
    }
