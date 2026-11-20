"""大盘情绪·智能点金（2026-11-16 上线；2026-11-21 t_ba8ee005 广度阳谱改造）。

数据源（真实接口）：
- 上证指数日线：akshare stock_zh_index_daily(symbol="sh000001")（新浪，3年+）
- 大盘资金流：akshare stock_market_fund_flow()（东财，约120个交易日）
- 市场广度：data/sentiment_breadth.json（沪深300成分股当日上涨家数占比日序缓存；
  种子由 scripts/build_breadth_seed.py 从研究卡历史数据生成，运行时每日增量：
  最新交易日走腾讯实时批量行情（1次请求），多日缺口走 akshare 成分股短窗口回补，
  拉取失败2次即停并沿用旧缓存——诚实降级，禁止静默冒充）

指标定义（全部公开规则，非黑盒）：
- 阳谱/阴谱：广度口径 = 沪深300成分股当日上涨家数占比×100 的 EMA5 平滑（研究卡
  t_6db7abf2 实证唯一稳定改进：5年 gold 命中率 50→57.1%、样本 n=16→49）。
  广度缓存不可用时退回「指数自身20日上涨日占比」并在面板显著标注（该口径历史
  命中率≈随机）。阴谱=100-阳谱。
- 机构力道：主力净流入净占比（超大单+大单）的3日EMA，正=吸筹红柱、负=抛压绿柱。
  东财加工口径，大单可拆小单致统计失真（东财官方投教自认），且本机覆盖仅约120日。
  柱上标记：吸=力道由负转正；减=由正转负；弹=深跌后力道连续2日回升。
- 趋势线：收盘 vs MA20，上方=红（多头）、下方=绿（空头）。
- 金手指：趋势线转红 且 阳谱阈值 且 机构力道同向（力道覆盖段=三重确认，
  覆盖前/源不可达=趋势翻转+阳谱穿50 双重确认；事件去重维持现状——研究卡实证
  取消跨向去重为负收益，阈值/斜率/量能过滤无益或负，禁止改）。
  银手指：反之。
- 回测统计：近3年每次金/银手指后20个交易日的指数真实涨跌（真实计算，
  上涨概率/平均幅度/最大/最小），与界面"近三年大数据回测"同形式。
"""
from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

HORIZON = 20          # 信号后评估窗口（交易日）
SPECTRUM_WIN = 20     # 阳谱统计窗口（仅指数自身口径使用）
SMOOTH = 5            # EMA 平滑
YEARS = 3

BREADTH_CACHE_NAME = "sentiment_breadth.json"
CONS_STALE_DAYS = 183        # 成分股列表半年更新一次即可
BACKFILL_MAX_DAYS = 15       # 多日缺口回补上限（超过=手动重播种，不硬拉）
PROVISIONAL_CUTOFF_HOUR = 15 # 当日15点前的盘中快照标记 provisional，收盘后定稿重算
MIN_VALID_STOCKS = 100       # 单日有效样本数下限（低于=该日广度不可信）


def _ema(vals: list[float], span: int) -> list[float]:
    if not vals:
        return []
    k = 2 / (span + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


# ======================================================================
# 广度数据层（缓存 + 增量 + 诚实降级）
# ======================================================================

def _breadth_path() -> Path:
    from app.services import quant_scan as qs
    return qs.DATA_DIR / BREADTH_CACHE_NAME


def _load_breadth() -> dict | None:
    p = _breadth_path()
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        daily = d.get("daily")
        if isinstance(daily, dict) and daily:
            return d
    except Exception as exc:  # noqa: BLE001
        logger.warning("广度缓存读取失败 %s: %s", p, exc)
    return None


def _save_breadth(d: dict) -> None:
    try:
        p = _breadth_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        logger.warning("广度缓存落盘失败: %s", exc)


def _sina_sym(code: str) -> str:
    code = str(code).zfill(6)
    return ("sh" if code.startswith(("6", "9"))
            else ("bj" if code.startswith(("4", "8")) else "sz")) + code


def _refresh_cons(d: dict) -> list[str]:
    """成分股列表（半年一次）。akshare 失败2次即停，保留旧列表。"""
    import akshare as ak
    for attempt in range(2):
        try:
            cons = [str(c).zfill(6)
                    for c in ak.index_stock_cons(symbol="000300")["品种代码"].tolist()]
            if len(cons) >= 200:
                d["cons"] = cons
                d["cons_updated_at"] = datetime.now().strftime("%Y-%m-%d")
                return cons
        except Exception as exc:  # noqa: BLE001
            logger.warning("HS300成分股列表拉取失败(%d/2): %s", attempt + 1, exc)
    return [str(c).zfill(6) for c in d.get("cons") or []]


def _ensure_cons(d: dict) -> list[str]:
    cons = [str(c).zfill(6) for c in d.get("cons") or []]
    upd = d.get("cons_updated_at")
    fresh = False
    if cons and upd:
        try:
            fresh = (datetime.now()
                     - datetime.strptime(upd, "%Y-%m-%d")).days <= CONS_STALE_DAYS
        except ValueError:
            fresh = False
    return cons if fresh else _refresh_cons(d)


def _today_breadth(cons: list[str]) -> float | None:
    """当日上涨家数占比：腾讯实时批量行情（1次HTTP，300只，非历史重拉）。

    口径与种子一致：未复权价，最新价>昨收 计涨；成交量0（停牌）不计入分母。
    """
    import httpx
    pairs, total, up = [], 0, 0
    for i in range(0, len(cons), 60):
        chunk = cons[i:i + 60]
        q = ",".join(_sina_sym(c) for c in chunk)
        r = httpx.get(f"https://qt.gtimg.cn/q={q}", timeout=8)
        r.encoding = "gbk"
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            fields = line.split("=", 1)[1].strip('";\n').split("~")
            if len(fields) < 7:
                continue
            try:
                last, prev, vol = float(fields[3]), float(fields[4]), float(fields[6])
            except ValueError:
                continue
            if last <= 0 or prev <= 0:
                continue
            pairs.append((last, prev, vol))
    for last, prev, vol in pairs:
        if vol <= 0:
            continue  # 停牌：不计入分母（与种子日线缺行语义一致）
        total += 1
        if last > prev:
            up += 1
    if total < MIN_VALID_STOCKS:
        return None
    return round(up / total * 100, 2)


def _backfill_days(cons: list[str], need: list[str]) -> dict[str, float]:
    """多日缺口回补：akshare 成分股短窗口日线（4并发，每股失败2次即止）。"""
    import akshare as ak
    start = (datetime.strptime(min(need), "%Y-%m-%d") - timedelta(days=10)).strftime("%Y%m%d")
    end = datetime.now().strftime("%Y%m%d")
    need_set = set(need)

    def fetch_one(code: str) -> pd.DataFrame | None:
        for attempt in range(2):
            try:
                df = ak.stock_zh_a_daily(symbol=_sina_sym(code), start_date=start,
                                         end_date=end, adjust="")
                if df is not None and not df.empty:
                    return df[["date", "close"]].assign(code=code)
            except Exception:  # noqa: BLE001
                import time
                time.sleep(1.0 * (attempt + 1))
        return None

    frames, fails = [], 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(fetch_one, c) for c in cons]
        for fut in as_completed(futs):
            r = fut.result()
            if r is None:
                fails += 1
            else:
                frames.append(r)
    if not frames:
        raise RuntimeError(f"广度回补全部失败（{len(cons)}只，失败{fails}）")
    allp = pd.concat(frames, ignore_index=True)
    allp["date"] = pd.to_datetime(allp["date"]).dt.strftime("%Y-%m-%d")
    allp = allp.drop_duplicates(subset=["code", "date"])
    piv = allp.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    # ffill 与研究卡种子口径一致（停牌缺价沿用前收，恢复日计真实跳变）
    ret = piv.ffill().pct_change(fill_method=None)
    out = {}
    for dstr in need:
        if dstr not in ret.index:
            continue
        row = ret.loc[dstr]
        tot = int(row.notna().sum())
        if tot < MIN_VALID_STOCKS:
            continue
        out[dstr] = round(float((row > 0).sum()) / tot * 100, 2)
    return out


class BreadthUpdateFailed(RuntimeError):
    pass


def update_breadth(idx_dates: list[str]) -> dict:
    """增量维护广度缓存，返回 {"status","asOf","daily","error"?}，永不抛异常。

    status: ok=覆盖最新交易日 | stale=沿用旧缓存（标注截至日）| unavailable=无缓存。
    """
    now = datetime.now()
    try:
        d = _load_breadth()
        if d is None:
            return {"status": "unavailable", "asOf": None, "daily": {},
                    "error": "广度缓存缺失（首次部署请跑 scripts/build_breadth_seed.py）"}
        daily = d["daily"]
        idx_last = idx_dates[-1]
        today = now.strftime("%Y-%m-%d")
        changed = False
        # 盘中 provisional：过15点且当日仍是最新交易日 → 删除待定稿重算
        if d.get("provisional_date") == idx_last == today and now.hour >= PROVISIONAL_CUTOFF_HOUR:
            daily.pop(idx_last, None)
            d.pop("provisional_date", None)
            d["updated_at"] = today
            _save_breadth(d)  # 落盘删除：盘中快照不可当定稿数据用
        tail = [x for x in idx_dates if x not in daily]
        if tail:
            try:
                cons = _ensure_cons(d)
                if not cons:
                    raise BreadthUpdateFailed("成分股列表不可用")
                need = [t for t in tail if t <= idx_last]
                hist = [t for t in need if t != idx_last or idx_last != today]
                if hist:
                    if len(hist) > BACKFILL_MAX_DAYS:
                        raise BreadthUpdateFailed(
                            f"广度缺口{len(hist)}日超回补上限{BACKFILL_MAX_DAYS}，需手动重播种")
                    filled = _backfill_days(cons, hist)
                    if not filled:
                        raise BreadthUpdateFailed("广度回补0日成功")
                    daily.update(filled)
                if idx_last == today and need and need[-1] == today:
                    r = _today_breadth(cons)
                    if r is None:
                        raise BreadthUpdateFailed("当日实时批量行情不可用")
                    daily[today] = r
                    changed = True
                    if now.hour < PROVISIONAL_CUTOFF_HOUR:
                        d["provisional_date"] = today
                d["updated_at"] = today
                _save_breadth(d)
            except BreadthUpdateFailed as exc:
                if changed:
                    _save_breadth(d)
                cov = max((k for k in daily if daily[k] is not None), default=None)
                return {"status": "stale", "asOf": cov, "daily": daily, "error": str(exc)}
            except Exception as exc:  # noqa: BLE001  红线：网络层失败2次即停，不重试风暴
                logger.warning("广度增量更新异常: %s", exc)
                if changed:
                    _save_breadth(d)
                cov = max((k for k in daily if daily[k] is not None), default=None)
                return {"status": "stale", "asOf": cov, "daily": daily, "error": str(exc)}
        covered = {k for k, v in daily.items() if v is not None}
        as_of = max(covered, default=None)
        status = "ok" if as_of and as_of >= idx_last else "stale"
        return {"status": status, "asOf": as_of, "daily": {k: daily[k] for k in covered}}
    except Exception as exc:  # noqa: BLE001  广度层任何异常不许炸掉整个面板
        logger.exception("广度数据层异常")
        return {"status": "unavailable", "asOf": None, "daily": {}, "error": str(exc)}


# ======================================================================
# 核心计算（纯函数：注入指数 df + 广度 + 力道，测试可离线复算）
# ======================================================================

def _compute_core(df: pd.DataFrame, breadth: dict | None,
                  force_map: dict[str, float] | None,
                  now: datetime | None = None,
                  force_pre_smoothed: bool = False,
                  years: int = YEARS) -> dict:
    """force_pre_smoothed=True：force_map 已是力道值（跳过3日EMA）。仅供离线
    复算/测试与研究卡同口径比对；生产走原始净占比→EMA3。
    years=窗口年数（3=面板主窗口；5=长窗口统计）。"""
    now = now or datetime.now()
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    # 与研究卡同语义：按日历日截断（date 级，不带当日时分，避免首日多切一天）
    cutoff = pd.Timestamp((now.date() - timedelta(days=years * 365 + 40)))
    df = df[df["date"] >= cutoff].reset_index(drop=True)
    if len(df) < 120:
        raise RuntimeError("上证指数日线数据不足")

    close = df["close"].astype(float).tolist()
    opens = df["open"].astype(float).tolist()
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    dates = [d.strftime("%Y-%m-%d") for d in df["date"]]
    n = len(close)

    # 趋势线 MA20
    ma20 = [None] * n
    for i in range(19, n):
        ma20[i] = sum(close[i - 19:i + 1]) / 20
    trend_red = [ma20[i] is not None and close[i] >= ma20[i] for i in range(n)]

    # 阳谱：优先广度口径（HS300上涨家数占比→EMA5，缺失日 warm-up 50，与研究卡同口径）
    daily = (breadth or {}).get("daily") or {}
    breadth_status = (breadth or {}).get("status", "unavailable")
    if daily:
        raw_spec = [float(v) if isinstance((v := daily.get(d)), (int, float)) else 50.0
                    for d in dates]
        yang_src = "hs300_breadth"
    else:
        raw_spec = [50.0] * n
        for i in range(1, n):
            lo = max(1, i - SPECTRUM_WIN + 1)
            ups = sum(1 for j in range(lo, i + 1) if close[j] > close[j - 1])
            raw_spec[i] = ups / (i - lo + 1) * 100
        yang_src = "index_self"
    yang = [round(v, 2) for v in _ema(raw_spec, SMOOTH)]
    yin = [round(100 - v, 2) for v in yang]

    # 机构力道：主力净占比 3日EMA（覆盖约120日；源不可达=全 None → 双确认降级）
    fmap = force_map or {}
    force: list[float | None] = [None] * n
    force_source = "none"
    vals, start_i = [], None
    for i, d in enumerate(dates):
        if d in fmap:
            if start_i is None:
                start_i = i
            vals.append(fmap[d])
        elif start_i is not None:
            vals.append(vals[-1] if vals else 0.0)  # 停牌日沿用
    if vals:
        sm = vals if force_pre_smoothed else _ema(vals, 3)
        for k, i in enumerate(range(start_i, n)):
            force[i] = round(sm[k], 2)
        force_source = "eastmoney"

    # 柱标记：吸/减/弹
    marks = [None] * n
    for i in range(1, n):
        f0, f1 = force[i - 1], force[i]
        if f0 is None or f1 is None:
            continue
        if f0 <= 0 < f1:
            marks[i] = "吸"
        elif f0 >= 0 > f1:
            marks[i] = "减"
        elif i >= 2 and force[i - 2] is not None and f1 < 0 and f1 > f0 > force[i - 2] and f0 < -2:
            marks[i] = "弹"

    # 金手指/银手指（事件去重：同向连续只记首次；研究卡实证勿改）
    fingers = [None] * n
    last_fire = None
    for i in range(1, n):
        if ma20[i] is None or ma20[i - 1] is None:
            continue
        if force[i] is None:
            gold = trend_red[i] and not trend_red[i - 1] and yang[i] >= 50
            silver = not trend_red[i] and trend_red[i - 1] and yang[i] <= 50
        else:
            gold = (trend_red[i] and not trend_red[i - 1]
                    and yang[i] >= 50 > yang[i - 1] and force[i] > 0)
            silver = (not trend_red[i] and trend_red[i - 1]
                      and yang[i] <= 50 < yang[i - 1] and force[i] < 0)
            if not gold and trend_red[i] and not trend_red[i - 1] and force[i] > 0 and yang[i] >= 45:
                gold = True
            if not silver and not trend_red[i] and trend_red[i - 1] and force[i] < 0 and yang[i] <= 55:
                silver = True
        if gold and last_fire != "gold":
            fingers[i] = "gold"; last_fire = "gold"
        elif silver and last_fire != "silver":
            fingers[i] = "silver"; last_fire = "silver"

    # 近三年回测：信号后 HORIZON 交易日涨跌（先于信号文案计算，供统计句引用）
    def eval_finger(kind):
        rets = []
        for i in range(n):
            if fingers[i] != kind or i + HORIZON >= n:
                continue
            r = (close[i + HORIZON] - close[i]) / close[i] * 100
            rets.append(round(r, 2))
        if not rets:
            return {"count": 0}
        ups = [r for r in rets if r > 0]
        downs = [r for r in rets if r < 0]
        return {
            "count": len(rets),
            "up_prob": round(len(ups) / len(rets) * 100, 2),
            "down_prob": round(len(downs) / len(rets) * 100, 2),
            "avg_up": round(sum(ups) / len(ups), 2) if ups else 0,
            "avg_down": round(sum(downs) / len(downs), 2) if downs else 0,
            "max_up": max(rets), "min_up": min(ups) if ups else 0,
            "max_down": min(rets), "min_down": max(downs) if downs else 0,
        }

    bt_gold = eval_with_segments("gold")
    bt_silver = eval_with_segments("silver")

    # 随机日基准（同窗口全部可评估交易日）：银手指降级语境 + 面板对照用
    base_rets = [(close[i + HORIZON] - close[i]) / close[i] * 100
                 for i in range(19, n - HORIZON)]
    base_up = sum(1 for r in base_rets if r > 0)
    baseline = {"days": len(base_rets),
                "up_prob": round(base_up / len(base_rets) * 100, 1) if base_rets else None,
                "down_prob": round((len(base_rets) - base_up) / len(base_rets) * 100, 1) if base_rets else None}

    # 信号日志（文案引用与信号同口径段的统计；count=0 退化为无统计句）
    dual_reason = "力道源暂不可达" if force_source == "none" else "资金流覆盖前"
    signals = []
    last_rebound = -10
    for i in range(1, n):
        if fingers[i] == "gold":
            signals.append({"date": dates[i], "type": "opportunity", "tag": "低吸",
                            "finger": "金手指",
                            "text": _signal_text("gold", bt_gold,
                                                 dual=None if force[i] is not None else dual_reason)})
        elif fingers[i] == "silver":
            signals.append({"date": dates[i], "type": "risk", "tag": "减仓",
                            "finger": "银手指",
                            "text": _signal_text("silver", bt_silver,
                                                 dual=None if force[i] is not None else dual_reason)})
        elif (trend_red[i] and force[i] is not None and force[i] > 0
              and yang[i] < 50 and i - last_rebound >= 5 and ma20[i] is not None):
            signals.append({"date": dates[i], "type": "opportunity", "tag": "反弹",
                            "finger": None,
                            "text": "趋势线在红区、机构力道吸筹，但阳谱仍低于50，赚钱效应未确认，反弹信号仅供参考，宜待阳谱站上50再评估。"})
            last_rebound = i
    signals.reverse()  # 新→旧

    # 图表窗口：最近约120个交易日（与资金流覆盖对齐）
    win_start = max(0, n - 120)
    series = []
    for i in range(win_start, n):
        series.append({
            "date": dates[i],
            "open": round(opens[i], 2), "high": round(highs[i], 2),
            "low": round(lows[i], 2), "close": round(close[i], 2),
            "ma20": round(ma20[i], 2) if ma20[i] is not None else None,
            "trendRed": trend_red[i],
            "yang": yang[i], "yin": yin[i],
            "force": force[i], "mark": marks[i],
            "finger": fingers[i],
        })

    latest = series[-1]
    b_asof = (breadth or {}).get("asOf")
    return {
        "asOf": dates[-1],
        "index": "上证指数",
        "horizon": HORIZON,
        "latest": latest,
        "series": series,
        "table": series[-14:],   # 智能点金表：最近14日
        "signals": signals[:60],
        "forceSource": force_source,
        "yangSource": yang_src,
        "breadth": {"status": breadth_status, "asOf": b_asof,
                    "error": (breadth or {}).get("error")},
        "backtest": {"gold": bt_gold, "silver": bt_silver,
                     "years": years,
                     "sample_start": dates[0], "sample_end": dates[-1]},
        "note": _build_note(breadth_status, b_asof, force_source, yang_src),
    }


def _build_note(breadth_status: str, b_asof: str | None,
                force_source: str, yang_src: str) -> str:
    if yang_src == "hs300_breadth":
        if breadth_status == "ok":
            bdesc = f"阳谱=沪深300成分股当日上涨家数占比·EMA5（广度口径，数据截至{b_asof}）"
        elif breadth_status == "stale":
            bdesc = f"阳谱=广度口径，增量更新失败沿用旧缓存（广度数据截至{b_asof}）"
        else:
            bdesc = f"阳谱=广度口径（广度数据截至{b_asof}）"
    else:
        bdesc = ("⚠广度源不可用，当前为指数自身口径（近20日上涨日占比，"
                 "历史命中率≈随机），仅供占位参考")
    fdesc = ("机构力道源当前不可达，金/银手指按「趋势翻转+阳谱穿50」双重确认（全历史口径）"
             if force_source == "none"
             else "机构力道=东财主力净占比3日EMA（东财加工口径存在拆单失真，覆盖约半年），"
                  "力道覆盖前金/银手指按趋势翻转+阳谱穿50双重确认")
    return (f"规则透明：{bdesc}；{fdesc}。"
            "信号描述中的历史统计=近3年同类信号后20个交易日真实涨跌，非预测，不构成投资建议。")


def _signal_text(kind: str, bt: dict, dual: str | None = None) -> str:
    """金/银手指信号文案：触发规则（透明口径）+ 真实回测统计 + 温和参考（非指令）。

    bt 为该方向 eval_finger 结果；count=0（样本不足）时退化为不含统计句，
    绝不渲染 0%/NaN。dual 非空表示该日力道未计入，值为降级原因短语（与 note
    口径一致）。历史统计非预测，不构成投资建议（免责统一在 note）。
    """
    gold = kind == "gold"
    if dual is not None:
        rule = (f"趋势线转{'红' if gold else '绿'}且阳谱{'≥50' if gold else '≤50'}，"
                f"双重确认触发（{dual}）。")
    elif gold:
        rule = "趋势线转红、阳谱≥45、机构力道为正，三重确认触发。"
    else:
        rule = "趋势线转绿、阳谱≤55、机构力道转负，三重确认触发。"
    side, prob_key = ("上涨", "up_prob") if gold else ("下跌", "down_prob")
    ref = "多头窗口或开启，可关注低吸机会。" if gold else "注意回撤风险，参考控制仓位。"
    stat = ""
    if bt.get("count"):
        stat = (f"近{YEARS}年同类信号后{HORIZON}个交易日{side}概率"
                f"{bt[prob_key]:g}%（{bt['count']}次样本）。")
    return rule + stat + ref


# ======================================================================
# 编排入口
# ======================================================================

def compute_sentiment() -> dict:
    import akshare as ak

    df = ak.stock_zh_index_daily(symbol="sh000001")

    breadth = update_breadth(
        [pd.to_datetime(d).strftime("%Y-%m-%d") for d in df["date"]])

    force_map: dict[str, float] = {}
    try:
        mf = ak.stock_market_fund_flow()
        bad_rows = 0
        for _, r in mf.iterrows():
            d = str(r["日期"])[:10]
            try:
                force_map[d] = float(r["主力净流入-净占比"])
            except (TypeError, ValueError):
                bad_rows += 1
        if bad_rows and not force_map:
            logger.warning("东财大盘资金流 %d 行全部解析失败，机构力道留空", bad_rows)
    except Exception as exc:  # noqa: BLE001  资金流失败不阻塞（降级双确认）
        logger.warning("东财大盘资金流拉取失败，机构力道留空(前端降级): %s", exc)

    return _compute_core(df, breadth, force_map)


def compute_sentiment_cached() -> dict:
    from app.services import quant_scan as qs
    payload = compute_sentiment()
    qs._save_cache("market_sentiment", payload)
    return payload
