"""ETF 申购净流入（C 方案：交易所份额法 + 每日快照滚动）

口径说明（对标同花顺"实时ETF申购净流入"，但为 T+1 官方数据）:
- 上交所: fund_etf_scale_sse(date) 可按日查询官方份额 → 当日/前一交易日快照都可直接取
- 深交所: 官方只提供"当前份额"，无历史接口 → 本服务每日收盘后抓快照存盘，滚动对比
- 申购净额 ≈ Δ份额 × 单位净值（净值优先深交所公布值，缺失则用腾讯行情的基金净值字段）

结果缓存 data/etf_flow.json，前端接口随时可读；定时任务每晚 19:30 调用 refresh 刷新。
"""
import asyncio
import json
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
SNAP_FILE = DATA_DIR / "etf_share_snap.json"
RESULT_FILE = DATA_DIR / "etf_flow.json"

# akshare 内部 V8 池不支持并发初始化，全局串行（与 app/quant/data/providers.py 同一约定）
_AK_LOCK = threading.Lock()

_MIN_TOP_SHARES = 20_000_000  # 份额不足 2000 万的迷你基金不参与排名


def _fetch_sse_shares(day: str) -> dict[str, dict]:
    """上交所份额 {code: {'name':..., 'shares': float}}，day 格式 YYYYMMDD

    注意：非交易日/未发布时 akshare 会对空结果 DataFrame 抛 KeyError，这里统一吞掉返回 {}。
    """
    import akshare as ak
    try:
        with _AK_LOCK:
            df = ak.fund_etf_scale_sse(date=day)
        cols = set(df.columns)
    except Exception:
        return {}
    if not {"基金代码", "基金份额"}.issubset(cols):
        return {}
    out = {}
    for _, row in df.iterrows():
        code = str(row["基金代码"]).zfill(6)
        try:
            shares = float(row["基金份额"])
        except (TypeError, ValueError):
            continue
        out[code] = {"name": str(row.get("基金简称", "")), "shares": shares}
    return out


def _fetch_szse_shares() -> tuple[str, dict[str, dict]]:
    """深交所当前份额（快照即最新）。返回 (数据日期 YYYY-MM-DD, {code: {...}})"""
    import akshare as ak
    with _AK_LOCK:
        df = ak.fund_etf_scale_szse()
    out = {}
    today = date.today().isoformat()
    for _, row in df.iterrows():
        code = str(row["基金代码"]).zfill(6)
        try:
            shares = float(row["基金份额"])
        except (TypeError, ValueError):
            continue
        nav = None
        try:
            nav = float(row["净值"])
            if nav <= 0:
                nav = None
        except (TypeError, ValueError):
            nav = None
        out[code] = {"name": str(row["基金简称"]), "shares": shares, "nav": nav}
    return today, out


async def _fetch_quote_tencent(codes: list[str]) -> dict[str, dict]:
    """腾讯行情批量取 ETF 现价/涨跌幅/净值（字段 3=现价 32=涨跌幅 78=单位净值）。分块避免 URL 过长。"""
    quotes: dict[str, dict] = {}
    if not codes:
        return quotes
    def prefixed(c: str) -> str:
        # 沪市股票6xxxxx、基金5xxxxx；深市股票0/3xxxxx、基金1xxxxx
        return ("sh" if c[0] in ("6", "5", "9") else "sz") + c
    async with httpx.AsyncClient(timeout=15) as client:
        for i in range(0, len(codes), 50):
            batch = codes[i:i + 50]
            q = ",".join(prefixed(c) for c in batch)
            # 连续快批偶发限流（返回 v_pv_none_match），逐批短休 + 失败重试
            for attempt in range(3):
                try:
                    await asyncio.sleep(0.15)
                    r = await client.get(f"https://qt.gtimg.cn/q={q}")
                    got = 0
                    for m in re.finditer(r'v_(?:sh|sz)(\d{6})="([^"]*)"', r.text):
                        fields = m.group(2).split("~")
                        if len(fields) < 79:
                            continue
                        item = {}
                        try:
                            item["price"] = float(fields[3])
                        except (ValueError, IndexError):
                            pass
                        try:
                            item["change_pct"] = float(fields[32])
                        except (ValueError, IndexError):
                            pass
                        try:
                            nav = float(fields[78])
                            if nav > 0:
                                item["nav"] = nav
                        except (ValueError, IndexError):
                            pass
                        quotes[m.group(1)] = item
                        got += 1
                    if got >= len(batch) * 0.5:
                        break
                except Exception:
                    pass
                await asyncio.sleep(1.0 * (attempt + 1))
    return quotes


def _last_sse_weekday(today: str) -> str:
    """today(YYYYMMDD) 之前最近的一个工作日（今天还没发布时兜底昨天）"""
    d = datetime.strptime(today, "%Y%m%d").date()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def _load_snap() -> dict:
    try:
        return json.loads(SNAP_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_snap(snap: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    snap["_updated_at"] = datetime.now().isoformat(timespec="seconds")
    SNAP_FILE.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")


async def _build_flows(sse_today: str, sse_map: dict, sse_prev_map: dict,
                       szse_date: str, szse_map: dict, szse_prev_map: dict) -> dict:
    """对比份额快照，计算申购净额并排名。

    注：首次运行时深市没有历史快照，若无条件把"当前份额全量"当作增量会算出天文数字。
    因此深市只统计上一快照中已存在的基金；沪市两天数据是现拉现比，不受影响。
    """
    first_run_szse = not szse_prev_map
    codes = sorted(set(sse_map) | set(szse_map))
    quotes = await _fetch_quote_tencent(codes)
    items = []
    skipped_new = 0
    for code in codes:
        cur = sse_map.get(code) or szse_map.get(code)
        prev = sse_prev_map.get(code) or szse_prev_map.get(code)
        if not cur:
            continue
        if first_run_szse and code in szse_map and code not in sse_map:
            skipped_new += 1
            continue  # 深市无历史基准，跳过本轮
        # 深交所自带净值，缺失时用腾讯净值兜底
        tq = quotes.get(code, {})
        nav = (cur.get("nav") if isinstance(cur, dict) else None) or tq.get("nav")
        if not nav:
            continue
        delta = cur["shares"] - (prev["shares"] if prev else 0)
        if not prev:
            skipped_new += 1
            continue  # 新上市基金无基准，份额全量不算申购
        if delta != delta or nav != nav:  # NaN 防护
            continue
        if cur["shares"] < _MIN_TOP_SHARES and abs(delta) < 1e9:
            continue  # 迷你基金不进榜（除非巨额申赎）
        net_inflow = delta * nav  # 元
        name = cur.get("name") or (prev or {}).get("name") or code
        items.append({
            "code": code,
            "name": name,
            "price": tq.get("price", 0),
            "change_pct": tq.get("change_pct", 0),
            "delta_shares": round(delta),
            "net_inflow": round(net_inflow),
            "shares": round(cur["shares"]),
            "nav": nav,
        })
    items.sort(key=lambda x: x["net_inflow"], reverse=True)
    items = [it for it in items if _is_equity_etf(it["name"])]  # 剔除债/货类，对齐同花顺口径
    inflow_pos = [it for it in items if it["net_inflow"] > 0]
    inflow_neg = [it for it in items if it["net_inflow"] < 0]
    total_pos = sum(it["net_inflow"] for it in inflow_pos)
    total_neg = sum(it["net_inflow"] for it in inflow_neg)
    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "data_date": sse_today if len(sse_today) == 8 else szse_date,
        "total_etfs": len(items),
        "inflow_count": len(inflow_pos),
        "outflow_count": len(inflow_neg),
        "total_inflow": round(total_pos),
        "total_outflow": round(total_neg),
        "top_inflow": items[:15],
        "top_outflow": list(reversed(items))[:5],
    }


async def refresh(force: bool = False) -> dict:
    """抓取最新份额 → 对比快照 → 写结果缓存。返回结果 dict。"""
    today = datetime.now().strftime("%Y%m%d")
    loop = asyncio.get_event_loop()

    # 1. 上交所：当日官方未发布则回退上一工作日
    sse_map = await loop.run_in_executor(None, _fetch_sse_shares, today)
    if not sse_map:
        today = _last_sse_weekday(today)
        sse_map = await loop.run_in_executor(None, _fetch_sse_shares, today)
    sse_today = today

    prev_day = _last_sse_weekday(sse_today)
    sse_prev_map = await loop.run_in_executor(None, _fetch_sse_shares, prev_day)

    # 2. 深交所：当前快照，与盘里存的上一次快照对比
    szse_date, szse_map = await loop.run_in_executor(None, _fetch_szse_shares)

    snap = _load_snap()
    szse_prev_map = snap.get("szse", {})

    # 3. 计算
    result = await _build_flows(sse_today, sse_map, sse_prev_map, szse_date, szse_map, szse_prev_map)

    # 4. 滚动快照（只保留最近两份，深够用）
    new_snap = {"sse_date": sse_today, "szse_date": szse_date, "sse": sse_map, "szse": szse_map}
    _save_snap(new_snap)

    # 5. 结果落盘
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def get_cached() -> dict | None:
    try:
        return json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 盘中实时主力净流入（新浪：ETF 成交额榜 → 逐只大单/超大单净额）
# 口径：主力净额 = 超大单(r0) + 大单(r1) 的流入-流出，当日累计，实时更新。
# 与"申购净流入"（一级市场）互补，对标东财 f62 但走服务器可达的新浪源。
# ---------------------------------------------------------------------------

_SINA_MF_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
                "json_v2.php/MoneyFlow.ssi_ssfx_flzjtj?daima={sym}")
_SINA_LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/jsonp.php/"
                  "IO.XSRV2.CallbackList['x']/Market_Center.getHQNodeDataSimple")
_rt_cache: dict = {"ts": 0.0, "items": []}
_RT_TTL = 180.0  # 3 分钟缓存
_RT_POOL = 60    # 只对成交额 TOP60 逐只取资金流

#: 债券/货币类 ETF 剔除：机构做市双边大单会让 r0/r1 净额严重失真（如科创债ETF虚增200亿），
#: 同花顺"实时ETF申购净流入"榜同样只覆盖股票型/跨境/商品类。
_NON_EQUITY_PAT = re.compile(r"债|货币|现金|理财|日利|快线")


def _is_equity_etf(name: str) -> bool:
    return not _NON_EQUITY_PAT.search(name or "")


async def _fetch_etf_spot_list(client: httpx.AsyncClient) -> list[dict]:
    """新浪 ETF 全列表（代码/名称/最新价/涨跌幅/成交额）"""
    params = {"page": "1", "num": "5000", "sort": "amount", "asc": "0", "node": "etf_hq_fund"}
    r = await client.get(_SINA_LIST_URL, params=params)
    text = r.text
    start = text.find("([")
    end = text.rfind(")")
    if start < 0 or end <= start:
        return []
    raw = json.loads(text[start + 1:end])
    out = []
    for it in raw:
        try:
            out.append({
                "code": str(it["symbol"])[2:],
                "sym": str(it["symbol"]),
                "name": it["name"],
                "price": float(it.get("trade") or 0),
                "change_pct": float(it.get("changepercent") or 0),
                "amount": float(it.get("amount") or 0),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


async def _fetch_main_net(client: httpx.AsyncClient, sem: asyncio.Semaphore, sym: str) -> float | None:
    async with sem:
        try:
            r = await client.get(_SINA_MF_URL.format(sym=sym))
            d = json.loads(r.text)
            r0 = float(d["r0_in"]) - float(d["r0_out"])
            r1 = float(d["r1_in"]) - float(d["r1_out"])
            return r0 + r1
        except Exception:
            return None


async def fetch_realtime_main_flow() -> list[dict]:
    """盘中主力净流入 TOP15（新浪源，3 分钟缓存）"""
    import time as _time
    now = _time.time()
    if now - _rt_cache["ts"] < _RT_TTL and _rt_cache["items"]:
        return _rt_cache["items"]

    async with httpx.AsyncClient(timeout=10) as client:
        spot = await _fetch_etf_spot_list(client)
        pool = [s for s in spot if s["amount"] > 0 and _is_equity_etf(s["name"])][: _RT_POOL]
        sem = asyncio.Semaphore(8)
        nets = await asyncio.gather(*(_fetch_main_net(client, sem, s["sym"]) for s in pool))

    items = []
    for s, net in zip(pool, nets):
        if net is None:
            continue
        items.append({
            "code": s["code"], "name": s["name"], "price": s["price"],
            "change_pct": s["change_pct"], "amount": s["amount"],
            "main_net_inflow": round(net),
        })
    items.sort(key=lambda x: x["main_net_inflow"], reverse=True)
    _rt_cache["ts"] = now
    _rt_cache["items"] = items
    return items
