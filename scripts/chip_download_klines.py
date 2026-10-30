"""全量池日K下载器：新浪 scale=240，1023根，磁盘缓存到 /tmp/chip_klines.pkl。"""
import json, os, pickle, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"}
OUT = "/tmp/chip_klines.pkl"
LOG = "/tmp/chip_klines_fail.json"

def sina_daily(symbol: str, datalen: int = 1023, retries: int = 3):
    prefix = "sh" if symbol.startswith(("6", "5", "9")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{symbol}&scale=240&ma=no&datalen={datalen}")
    for a in range(retries):
        try:
            r = requests.get(url, timeout=20, headers=UA)
            m = re.search(r"\((\[.*\])\)", r.text, re.S)
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

def main():
    uni = json.load(open("/tmp/astock_universe.json"))
    uni = [x for x in uni if "ST" not in x["name"].upper()]
    print(f"universe (excl ST): {len(uni)}")

    cache = {}
    if os.path.exists(OUT):
        cache = pickle.load(open(OUT, "rb"))
        print(f"resume: {len(cache)} already cached")

    todo = [x for x in uni if x["code"] not in cache]
    print(f"to fetch: {len(todo)}")

    t0 = time.time()
    fails = []
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(sina_daily, x["code"]): x["code"] for x in todo}
        for fut in as_completed(futs):
            code = futs[fut]
            df = fut.result()
            done += 1
            if df is None or len(df) < 200:
                fails.append(code)
            else:
                cache[code] = df
            if done % 500 == 0:
                el = time.time() - t0
                eta = el / done * (len(todo) - done)
                print(f"  {done}/{len(todo)} ok={len(cache)} fail={len(fails)} "
                      f"elapsed={el/60:.1f}m eta={eta/60:.1f}m", flush=True)
                pickle.dump(cache, open(OUT, "wb"))

    pickle.dump(cache, open(OUT, "wb"))
    json.dump(fails, open(LOG, "w"))
    print(f"DONE stocks={len(cache)} fails={len(fails)} in {(time.time()-t0)/60:.1f} min")

if __name__ == "__main__":
    main()
