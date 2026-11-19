"""行情数据服务 - 腾讯财经 + 东方财富"""
import logging

import httpx
import json
import re

logger = logging.getLogger(__name__)


async def fetch_realtime_tencent(codes: list[str]) -> dict:
    """腾讯财经实时行情 - 优先使用"""
    code_map = {}
    for c in codes:
        prefix = "sh" if c.startswith(("6", "5", "9")) else "sz"
        code_map[f"{prefix}{c}"] = c

    if not code_map:
        return {}

    query = ",".join(code_map.keys())
    url = f"https://qt.gtimg.cn/q={query}"

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url)
        text = resp.text

    results = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var_part, data_part = line.split("=", 1)
        data_part = data_part.strip('";\n')
        fields = data_part.split("~")
        if len(fields) < 45:
            continue

        tc_code = var_part.split("_")[-1]
        stock_code = code_map.get(tc_code, "")
        if not stock_code:
            continue

        results[stock_code] = {
            "name": fields[1],
            "latest_price": float(fields[3]) if fields[3] else 0,
            "open_price": float(fields[5]) if fields[5] else 0,
            "high": float(fields[33]) if fields[33] else 0,
            "low": float(fields[34]) if fields[34] else 0,
            "volume": float(fields[6]) if fields[6] else 0,
            "change_pct": float(fields[32]) if fields[32] else 0,
            "quote_time": fields[30] if len(fields) > 30 else "",
        }

    return results



async def fetch_kline_sina_minute(code: str, scale: int = 60, datalen: int = 1023) -> list[dict]:
    """新浪分钟K线（支持 60/30/15 分钟，单次最多约 1023 根）

    腾讯 mkline 接口在本机无法解析（web3.ifzq.gtimg.cn DNS 失败），
    分钟数据改走新浪源。数据源单次上限 1023 根，各周期大约覆盖：
    - 60分钟 ≈ 13 个月
    - 30分钟 ≈ 6.5 个月
    - 15分钟 ≈ 3 个月
    - 240 = 日线（腾讯 WAF 拦截时的备用日线源）

    返回的 date 为完整 'YYYY-MM-DD HH:MM:SS' 字符串。
    """
    if scale not in (60, 30, 15, 240):
        return []
    prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
        f"?symbol={prefix}{code}&scale={scale}&ma=no&datalen={datalen}"
    )

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            m = re.search(r"\((\[.*\])\)", resp.text, re.S)
            if not m:
                return []
            items = json.loads(m.group(1))
            return [
                {
                    "date": item.get("day", ""),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": float(item.get("volume", 0)),
                }
                for item in items
            ]
        except Exception:
            return []


async def fetch_kline_baidu(code: str, days: int = 120) -> list[dict]:
    """百度股市通日K线"""
    # 审计 t_edf48869 §四.2 判定：保守保留。百度源已 403 停用（README 有记），
    # 前缀推断仅留作恢复该数据源时的参考，URL 本身不依赖这两值。
    prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"  # noqa: F841
    market = "SH" if code.startswith(("6", "5", "9")) else "SZ"  # noqa: F841
    url = f"https://finance.pae.baidu.com/vapi/v1/getquotation?srcid=5353&pointType=string&group=quotation_kline_ab&query={code}&code={code}&market_type=stock&newFormat=1&finClientType=pc&ktype=day&start_time=&end_time=&count={days}"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            data = resp.json()
            result = data.get("Result", {})
            new_format = result.get("newFormat", [])
            klines = []
            for item in new_format:
                klines.append({
                    "date": item.get("date", ""),
                    "open": float(item.get("open", 0)),
                    "high": float(item.get("high", 0)),
                    "low": float(item.get("low", 0)),
                    "close": float(item.get("close", 0)),
                    "volume": float(item.get("volume", 0)),
                })
            # 百度可能返回 200/403 + 空数据体（不抛异常），
            # 空结果必须落到备用源，不能直接 return
            if klines:
                return klines
        except Exception as exc:
            logger.warning("百度K线拉取失败 %s，转腾讯备用源: %s", code, exc)

    # Fallback: tencent kline
    return await fetch_kline_tencent(code, days)


async def fetch_kline_tencent(code: str, days: int = 120, full_date: bool = False) -> list[dict]:
    """腾讯日K线 - 备用

    full_date=True 时保留完整 YYYY-MM-DD 日期（跨年的长周期数据回测用），
    默认仍归一化为 MM-DD（持仓缓存格式）。
    """
    prefix = "sh" if code.startswith(("6", "5", "9")) else "sz"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},day,,,{days},qfq"

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            data = resp.json()
            kline_key = f"{prefix}{code}"
            day_data = data.get("data", {}).get(kline_key, {})
            # Try qfqday first, then day
            raw = day_data.get("qfqday") or day_data.get("day") or []
            klines = []
            for item in raw:
                date_str = item[0]
                # Normalize date to MM-DD unless full_date requested
                if not full_date and len(date_str) >= 10:
                    date_str = date_str[5:]
                klines.append({
                    "date": date_str,
                    "open": float(item[1]),
                    "high": float(item[3]),
                    "low": float(item[4]),
                    "close": float(item[2]),
                    "volume": float(item[5]) if len(item) > 5 else 0,
                })
            return klines
        except Exception:
            return []


async def fetch_news_eastmoney(code: str, count: int = 3) -> list[dict]:
    """东方财富公告"""
    # Determine stock type
    url = f"https://np-anotice-stock.eastmoney.com/api/security/ann?sr=-1&page_size={count}&page_index=1&ann_type=SHA&client_source=web&stock_list={code}&f_node=0"

    async with httpx.AsyncClient(timeout=10, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }) as client:
        try:
            resp = await client.get(url)
            data = resp.json()
            items = data.get("data", {}).get("list", [])
            news = []
            for item in items:
                notice_date = item.get("notice_date", "")
                if notice_date and len(notice_date) >= 10:
                    notice_date = notice_date[:10]
                display_title = item.get("title", "")
                columns = item.get("columns", [])
                stock_name = ""
                art_code = item.get("art_code", "")
                for col in columns:
                    stock_name = col.get("stock_name", "")
                    break
                ann_url = f"https://data.eastmoney.com/notices/detail/{code}/{art_code}.html"
                news.append({
                    "date": notice_date,
                    "title": f"{stock_name}:{display_title}" if stock_name else display_title,
                    "url": ann_url,
                })
            return news
        except Exception:
            return []


def calc_ma(klines: list[dict], period: int) -> float:
    """计算均线"""
    if len(klines) < period:
        return 0
    closes = [k["close"] for k in klines[-period:]]
    return round(sum(closes) / period, 3)


def calc_boll(klines: list[dict], period: int = 20, std_dev: float = 2.0) -> tuple[float, float, float]:
    """计算布林带（上轨、中轨、下轨）"""
    if len(klines) < period:
        return 0, 0, 0
    closes = [k["close"] for k in klines[-period:]]
    mid = sum(closes) / period
    variance = sum((c - mid) ** 2 for c in closes) / period
    std = variance ** 0.5
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    return round(upper, 3), round(mid, 3), round(lower, 3)


def calc_kdj_raw(klines: list[dict], n: int = 9, m1: int = 3, m2: int = 3) -> tuple[float, float, float]:
    """KDJ 全历史递推（不取整），返回未 round 的 (K, D, J)。

    P1-3 修复：旧实现只从倒数第 n 根开始迭代，前段全部历史完全不参与
    （暴涨/横盘两种前历史输出逐位相同），K/D 与行情软件系统性偏差。
    现按标准口径全历史递推 RSV→K→D（初值 K=D=50，与 talib/全历史参考一致，
    详见 README「指标口径」）。
    """
    if len(klines) < n:
        return 50.0, 50.0, 50.0

    k, d = 50.0, 50.0  # 初值
    for i in range(n - 1, len(klines)):
        window = klines[i - n + 1:i + 1]
        high_n = max(x["high"] for x in window)
        low_n = min(x["low"] for x in window)
        close = window[-1]["close"]
        # RSV（未成熟随机值）；高低价相同的退化情形取 50（中性）
        rsv = 50.0 if high_n == low_n else (close - low_n) / (high_n - low_n) * 100
        k = (m1 - 1) / m1 * k + 1 / m1 * rsv
        d = (m2 - 1) / m2 * d + 1 / m2 * k

    j = 3 * k - 2 * d
    return k, d, j


def calc_kdj(klines: list[dict], n: int = 9, m1: int = 3, m2: int = 3) -> tuple[float, float, float]:
    """计算 KDJ 指标（K值、D值、J值）——全历史递推口径，见 calc_kdj_raw"""
    k, d, j = calc_kdj_raw(klines, n, m1, m2)
    return round(k, 2), round(d, 2), round(j, 2)


def calc_macd_raw(klines: list[dict], short: int = 12, long: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """MACD 全历史递推（不取整），返回未 round 的 (DIF, DEA, 柱)。

    P1-4 修复：EMA 平滑系数由 1/N 改为标准 2/(N+1)（talib/主流行情软件口径），
    长历史末端 DIF/DEA 与 talib.MACD 对照误差 <1e-6。柱为国内通达信惯例
    (DIF-DEA)×2（talib 返回不×2 的差值，对照时仅比 DIF/DEA）。
    拆出 raw 版本供精度对照测试使用，详见 README「指标口径」。
    """
    if len(klines) < long:
        return 0.0, 0.0, 0.0

    closes = [k["close"] for k in klines]

    # 计算 EMA（标准 alpha = 2/(N+1)）
    a_short = 2.0 / (short + 1)
    a_long = 2.0 / (long + 1)
    a_signal = 2.0 / (signal + 1)
    ema_short = closes[0]
    ema_long = closes[0]
    dif_values = []

    for close in closes:
        ema_short = a_short * close + (1 - a_short) * ema_short
        ema_long = a_long * close + (1 - a_long) * ema_long
        dif_values.append(ema_short - ema_long)

    # 计算 DEA（DIF 的 EMA）
    dea = dif_values[0]
    for dif in dif_values:
        dea = a_signal * dif + (1 - a_signal) * dea

    dif = dif_values[-1]
    macd = (dif - dea) * 2
    return dif, dea, macd


def calc_macd(klines: list[dict], short: int = 12, long: int = 26, signal: int = 9) -> tuple[float, float, float]:
    """计算 MACD 指标（DIF、DEA、MACD柱）——标准 2/(N+1) EMA 口径，见 calc_macd_raw"""
    dif, dea, macd = calc_macd_raw(klines, short, long, signal)
    return round(dif, 3), round(dea, 3), round(macd, 3)


def calc_volume_ratio(klines: list[dict]) -> float:
    """计算量比(当日成交量/20日均量)"""
    if len(klines) < 21:
        return 0
    today_vol = klines[-1]["volume"]
    avg_vol = sum(k["volume"] for k in klines[-21:-1]) / 20
    if avg_vol <= 0:
        return 0
    return round(today_vol / avg_vol, 2)
