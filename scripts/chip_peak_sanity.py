"""单股 sanity check：新浪日K + 流通股本 + 筹码峰切割。"""
import sys, json, re, time
sys.path.insert(0, "/root/workspace/jungle-trading")
import numpy as np
import pandas as pd
import requests
from app.quant.utils.chip_peaks import chip_path, signal_days, DEFAULT_RULES

UA = {"User-Agent": "Mozilla/5.0"}

def sina_daily(symbol: str, datalen: int = 1023) -> pd.DataFrame:
    prefix = "sh" if symbol.startswith(("6", "5", "9")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_=/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{symbol}&scale=240&ma=no&datalen={datalen}")
    r = requests.get(url, timeout=15, headers=UA)
    m = re.search(r"\((\[.*\])\)", r.text, re.S)
    if not m:
        raise ValueError("sina empty")
    items = json.loads(m.group(1))
    df = pd.DataFrame([{
        "date": it["day"], "open": float(it["open"]), "high": float(it["high"]),
        "low": float(it["low"]), "close": float(it["close"]), "volume": float(it["volume"]),
    } for it in items])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)

def float_shares_of(code: str) -> float:
    uni = json.load(open("/tmp/astock_universe.json"))
    for x in uni:
        if x["code"] == code:
            return x["float_mv"] / x["price"]
    return 0.0

def funnel(df, fs, rules=DEFAULT_RULES):
    """逐日5条件漏斗（用于诊断阈值松紧）。"""
    p = chip_path(df, fs)
    if p is None:
        return None
    n = len(df)
    valid = ~np.isnan(p["conc_90"])
    c1 = valid & (p["conc_90"] < rules["conc90_max"])
    c2 = c1 & (p["peak_ratio"] > rules["peak_ratio_min"])
    c3 = c2 & (p["second_ratio"] < rules["second_peak_max"])
    c4 = c3 & (p["turnover60"] > rules["turnover60_min"])
    c5 = c4 & (p["amplitude60"] < rules["amplitude60_max"])
    return dict(base=int(valid.sum()), c1=int(c1.sum()), c2=int(c2.sum()),
                c3=int(c3.sum()), c4=int(c4.sum()), c5=int(c5.sum()),
                path=p, last=valid.sum() - 1)

if __name__ == "__main__":
    codes = sys.argv[1:] or ["600580"]
    for code in codes:
        df = sina_daily(code)
        fs = float_shares_of(code)
        last = df.iloc[-1]
        f = funnel(df, fs)
        p, li = f["path"], f["last"]
        print(f"\n=== {code} bars={len(df)} {df['date'].iloc[0].date()}~{df['date'].iloc[-1].date()} "
              f"close={last['close']} float_shares={fs/1e8:.2f}亿 ===")
        print(f"  漏斗: 有效天={f['base']} conc90通过={f['c1']} +主峰={f['c2']} +次峰={f['c3']} "
              f"+换手={f['c4']} +振幅={f['c5']}")
        print(f"  最新日: conc90={p['conc_90'][li]:.2f}% 主峰={p['peak_ratio'][li]:.1f}% "
              f"次峰/主峰={p['second_ratio'][li]:.2f} 60换手={p['turnover60'][li]*100:.0f}% "
              f"60振幅={p['amplitude60'][li]*100:.1f}%")
        sig = signal_days(p)
        dates = df["date"].dt.strftime("%Y-%m-%d").to_numpy()
        print(f"  首次满足信号日: {[dates[i] for i in sig][-8:]}")
        # 手工验证最后一天 conc90：用现有 numpy 重算
        chip = p  # already path
        # 打印峰附近分布形态检查（主峰段边界）
        from app.quant.utils.chip_peaks import price_grid, _triangle_row
        print(f"  网格={p['n_grid']}档")
