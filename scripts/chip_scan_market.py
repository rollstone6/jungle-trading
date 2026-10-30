"""全A股筹码5条件流式扫描器（低内存版）。

替代旧 chip_download_klines.py（旧版把几千只 DataFrame 全留内存→OOM）。
管线：下载日K → 立即算 chip_path → 只留最新日指标+信号统计 → 丢弃 DataFrame。
断点续跑：结果增量落盘到 /tmp/chip_scan_results.jsonl，重跑自动跳过已完成代码。

用法: /opt/hermes-agent/.venv/bin/python scripts/chip_scan_market.py [workers]
"""
import json, os, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import pandas as pd
import requests

from app.quant.utils.chip_peaks import chip_path, signal_days, DEFAULT_RULES

UNIV = os.environ.get("CHIP_UNIV", "/tmp/astock_universe.json")
OUT = os.environ.get("CHIP_OUT", "/tmp/chip_scan_results.jsonl")
UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}

def sina_daily(symbol: str, datalen: int = 1023, retries: int = 3):
    prefix = "sh" if symbol.startswith(("6", "5", "9")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{symbol}&scale=240&ma=no&datalen={datalen}")
    for a in range(retries):
        try:
            r = requests.get(url, timeout=20, headers=UA)
            m = __import__("re").search(r"\((\[.*\])\)", r.text, __import__("re").S)
            if not m or not m.group(1):
                return None
            items = json.loads(m.group(1))
            if not items:
                return None
            return pd.DataFrame([{
                "date": it["day"], "open": float(it["open"]), "high": float(it["high"]),
                "low": float(it["low"]), "close": float(it["close"]), "volume": float(it["volume"]),
            } for it in items])
        except Exception:
            time.sleep(0.5 * (a + 1))
    return None

def process(code: str, name: str, price: float, float_mv: float) -> dict | None:
    """下载→算指标→只返回结果行（不留 DataFrame）。"""
    df = sina_daily(code)
    if df is None or len(df) < 220:
        return {"code": code, "name": name, "err": "nodata"}
    df = df.sort_values("date").reset_index(drop=True)
    fs = float_mv / price if price > 0 else 0.0
    try:
        p = chip_path(df, fs or None)
    except Exception as e:
        return {"code": code, "name": name, "err": str(e)[:80]}
    if p is None:
        return {"code": code, "name": name, "err": "short"}
    li = len(df) - 1
    valid = ~np.isnan(p["conc_90"])
    if not valid.any():
        return {"code": code, "name": name, "err": "nowarm"}
    sig = signal_days(p)
    dates = df["date"].astype(str).to_numpy()
    r = DEFAULT_RULES
    c = p
    ok = ((c["conc_90"] < r["conc90_max"]) & (c["peak_ratio"] > r["peak_ratio_min"])
          & (c["second_ratio"] < r["second_peak_max"]) & (c["turnover60"] > r["turnover60_min"])
          & (c["amplitude60"] < r["amplitude60_max"]) & valid)
    # 当前是否满足5条件
    cur_ok = bool(ok[li])
    def f(x):
        return None if (x is None or np.isnan(x)) else round(float(x), 2)
    return {
        "code": code, "name": name, "close": float(df["close"].iloc[li]),
        "conc90": f(p["conc_90"][li]), "peak": f(p["peak_ratio"][li]),
        "second": f(p["second_ratio"][li]), "turn60": f(p["turnover60"][li] * 100),
        "amp60": f(p["amplitude60"][li] * 100),
        "cur5": cur_ok, "sig_days": int(len(sig)),
        "last_sig": dates[sig[-1]][:10] if len(sig) else None,
        "sigs": [dates[i][:10] for i in sig][-6:],
    }

def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    uni = [x for x in json.load(open(UNIV)) if "ST" not in x["name"].upper()]
    done = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                done.add(json.loads(line)["code"])
            except Exception:
                pass
    todo = [x for x in uni if x["code"] not in done]
    print(f"universe={len(uni)} done={len(done)} todo={len(todo)} workers={workers}", flush=True)

    lock = threading.Lock()
    fout = open(OUT, "a", buffering=1)
    t0 = time.time()
    n = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        # 分批提交，避免一次性创建 5000 个 future；每批 workers*8 个任务在飞
        it = iter(todo)
        futs = {}
        def submit_more():
            for _ in range(workers * 8):
                try:
                    x = next(it)
                except StopIteration:
                    return False
                futs[ex.submit(process, x["code"], x["name"], x.get("price", 0), x.get("float_mv", 0))] = x["code"]
            return True
        submit_more()
        while futs:
            fut = next(as_completed(futs))
            code = futs.pop(fut)
            try:
                row = fut.result()
            except Exception as e:
                row = {"code": code, "err": str(e)[:80]}
            if row is not None:
                with lock:
                    fout.write(json.dumps(row, ensure_ascii=False) + "\n")
            n += 1
            if n % 200 == 0:
                el = time.time() - t0
                print(f"  {n}/{len(todo)} elapsed={el/60:.1f}m eta={(el/n)*(len(todo)-n)/60:.1f}m", flush=True)
            submit_more()
    fout.close()
    print(f"DONE total={n} in {(time.time()-t0)/60:.1f} min", flush=True)

if __name__ == "__main__":
    main()
