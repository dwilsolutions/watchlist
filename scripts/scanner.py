"""
Micro-Cap Momentum Swing Scanner
Reads config from environment variables set in watchlist.yml.
"""

import os, sys, json, math, argparse, csv, io, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import urllib.request

TOKEN        = os.environ.get("FINVIZ_TOKEN", "")
ET           = ZoneInfo("America/New_York")
PRICE_MIN    = float(os.environ.get("SWING_PRICE_MIN",    "0.20"))
PRICE_MAX    = float(os.environ.get("SWING_PRICE_MAX",    "5.00"))
FLOAT_MAX_M  = float(os.environ.get("SWING_FLOAT_MAX_M",  "10"))
MIN_SCORE    = float(os.environ.get("SWING_MIN_SCORE",    "0.35"))
SPIKE_DAYS   = int(os.environ.get("SWING_SPIKE_DAYS",     "90"))
SPIKE_PCT    = float(os.environ.get("SWING_SPIKE_PCT",    "40"))
SHORT_MIN    = float(os.environ.get("SWING_SHORT_INT_MIN","10"))
DTC_MAX      = float(os.environ.get("SWING_DTC_MAX",      "3"))
MANUAL       = [t.strip() for t in os.environ.get("SWING_MANUAL", "BTBD,FLYX,AZTR,BFRG,UGRO,DEFT,BYAH,OXBR,SST,SER,EEIQ").split(",")]
OUT_DIR      = os.environ.get("SWING_OUT_DIR",  "docs")
DATA_DIR     = os.environ.get("SWING_DATA_DIR", "docs/data/swing")

WEIGHTS = {
    "fresh_8k": 0.25, "merger_pivot": 0.20, "previous_spike": 0.20,
    "short_squeeze": 0.15, "high_rel_vol": 0.10, "fda_binary": 0.10,
}

MERGER_KEYWORDS = [
    "merger","acquisition","acquires","letter of intent","loi",
    "artificial intelligence"," ai ","crypto","blockchain","drone",
    "reverse merger","change of business","pivot",
    "strategic alternative","definitive agreement",
]
FDA_KEYWORDS = [
    "fda","nda","bla","pdufa","approval","clinical trial",
    "phase 3","fast track","breakthrough therapy",
]

SCREENERS = [
    ("Low Float",
     "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136"
     "&f=cap_smallunder,sh_curvol_o5000,sh_float_u20,sh_price_u10,sh_relvol_o2&ex=nasdaq,nyse,amex"),
    ("Mid Float",
     "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136"
     "&f=cap_smallunder,sh_curvol_o5000,sh_float_20to100x,sh_price_u20,sh_relvol_o3&ex=nasdaq,nyse,amex"),
]

def fetch_finviz(scan_label, filters):
    if not TOKEN:
        print(f"  [!] No FINVIZ_TOKEN — skipping {scan_label}"); return []
    url = f"https://elite.finviz.com/export.ashx?{filters}&auth={TOKEN}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
        rows = list(csv.DictReader(io.StringIO(raw)))
        print(f"  [+] {scan_label}: {len(rows)} tickers"); return [(scan_label, r) for r in rows]
    except Exception as e:
        print(f"  [!] Finviz error ({scan_label}): {e}"); return []

def safe_f(v, default=0.0):
    try:
        f = float(str(v).replace("%","").replace("$","").replace(",","").strip())
        return default if math.isnan(f) or math.isinf(f) else f
    except: return default

def fetch_yf_batch(tickers):
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("  [!] yfinance not installed"); return {}
    results = {}
    for i in range(0, len(tickers), 40):
        batch = tickers[i:i+40]
        try:
            raw = yf.download(batch, period=f"{SPIKE_DAYS}d", interval="1d",
                              group_by="ticker", auto_adjust=True, progress=False, threads=True)
            for ticker in batch:
                try:
                    df = raw.copy() if len(batch)==1 else (
                        raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex)
                        and ticker in raw.columns.get_level_values(0) else None)
                    if df is not None:
                        df = df.dropna(subset=["Open","High","Close","Volume"])
                        if not df.empty: results[ticker] = df
                except: continue
        except Exception as e:
            print(f"  [!] yfinance error: {e}")
    print(f"  [+] yfinance: {len(results)}/{len(tickers)} loaded")
    return results

def check_spike(df):
    try:
        df = df.copy()
        df["mv"] = (df["High"] - df["Close"].shift(1)) / df["Close"].shift(1) * 100
        mx = df["mv"].max()
        return mx >= SPIKE_PCT, round(float(mx), 1) if not math.isnan(mx) else 0
    except: return False, 0

def check_edgar_8k(ticker, days=7):
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    url   = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=8-K"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "swing-scanner research@example.com"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        hits = data.get("hits",{}).get("hits",[])
        if not hits: return False, False, ""
        text = " ".join(str(h.get("_source",{}).get("entity_name","")) for h in hits).lower()
        for kw in MERGER_KEYWORDS + FDA_KEYWORDS:
            if kw in text: return True, True, kw
        return True, False, ""
    except: return False, False, ""

def check_news(title):
    if not title: return False, ""
    text = title.lower()
    for kw in MERGER_KEYWORDS + FDA_KEYWORDS:
        if kw in text: return True, kw
    return False, ""

def score_ticker(row, yf_df=None):
    scan_label, fv = row
    ticker     = fv.get("Ticker","").strip()
    news_title = str(fv.get("News Title",""))
    news_url   = str(fv.get("News URL",""))
    rvol       = safe_f(fv.get("Relative Volume",0))
    price      = safe_f(fv.get("Price",0))
    change     = safe_f(fv.get("Change",0))
    short_pct  = safe_f(fv.get("Short Float",0))
    days_cover = safe_f(fv.get("Short Ratio",99), default=99)

    scores = {k: 0.0 for k in WEIGHTS}
    flags  = []

    has_8k, has_kw, kw = check_edgar_8k(ticker)
    if has_8k:
        scores["fresh_8k"] = 1.0
        flags.append(("FRESH 8-K", "catalyst"))
    time.sleep(0.1)

    has_cat, cat_kw = check_news(news_title)
    kw_used = cat_kw or kw
    if has_cat or has_kw:
        scores["merger_pivot"] = 1.0
        if any(k in kw_used for k in ["merger","acquisition","loi","letter of intent","reverse merger"]):
            flags.append(("CATALYST · M&A", "catalyst"))
        elif any(k in kw_used for k in ["ai","artificial intelligence","crypto","blockchain","drone"]):
            flags.append(("CATALYST · PIVOT", "catalyst"))
        elif any(k in kw_used for k in ["fda","nda","bla","pdufa","approval"]):
            flags.append(("FDA EVENT", "catalyst"))
        else:
            flags.append(("CATALYST · NEWS", "catalyst"))

    spike_pct = 0
    if yf_df is not None:
        had_spike, spike_pct = check_spike(yf_df)
        if had_spike:
            scores["previous_spike"] = 1.0
            flags.append((f"PREV SPIKE +{spike_pct:.0f}%", "gap"))

    if short_pct >= SHORT_MIN and days_cover <= DTC_MAX:
        scores["short_squeeze"] = 1.0
        flags.append((f"SHORT SQUEEZE {short_pct:.0f}%", "danger"))
    elif short_pct >= SHORT_MIN:
        scores["short_squeeze"] = 0.5
        flags.append((f"SHORT INT {short_pct:.0f}%", "danger"))

    scores["high_rel_vol"] = 1.0 if rvol>=10 else 0.7 if rvol>=5 else 0.4 if rvol>=2 else 0.0

    if scores["merger_pivot"]==1.0 and any(k in kw_used for k in ["fda","nda","pdufa","approval"]):
        scores["fda_binary"] = 1.0

    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)

    return {
        "ticker": ticker, "scan": scan_label, "company": fv.get("Company",""),
        "sector": fv.get("Sector",""), "price": price, "change": change,
        "rvol": rvol, "float_m": safe_f(fv.get("Shares Float",0)),
        "short_pct": short_pct, "days_cover": days_cover, "spike_pct": spike_pct,
        "news": news_title[:120], "news_url": news_url,
        "score": round(total,3), "components": scores, "flags": flags,
    }

def get_rank(t):
    s = t["score"]
    if s >= 0.70: return "hot"
    if s >= 0.50: return "warm"
    if s >= 0.35: return "watch"
    return "avoid"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=["premarket","midday"], default="premarket")
    args = parser.parse_args()

    now_et   = datetime.now(ET)
    today    = now_et.date()
    gen_time = now_et.strftime("%I:%M %p ET")
    print(f"\nSwing Scanner · {args.session} · {today} · {gen_time}")

    all_rows = []
    for scan_label, filters in SCREENERS:
        all_rows.extend(fetch_finviz(scan_label, filters))

    seen, unique_rows = set(), []
    for scan_label, row in all_rows:
        t = row.get("Ticker","").strip()
        if t and t not in seen:
            seen.add(t); unique_rows.append((scan_label, row))

    manual_missing = [t for t in MANUAL if t not in seen]
    print(f"  [+] {len(unique_rows)} screener + {len(manual_missing)} manual tickers")

    all_tickers = [r[1].get("Ticker","").strip() for r in unique_rows] + manual_missing
    yf_data = fetch_yf_batch([t for t in all_tickers if t])

    results = []
    for scan_label, row in unique_rows:
        ticker = row.get("Ticker","").strip()
        if not ticker: continue
        r = score_ticker((scan_label, row), yf_df=yf_data.get(ticker))
        r["manual"] = ticker in MANUAL
        results.append(r)

    for ticker in manual_missing:
        dummy = {"Ticker":ticker,"Company":ticker,"Sector":"","Price":0,"Change":0,
                 "Relative Volume":0,"News Title":"","News URL":"",
                 "Shares Float":0,"Short Float":0,"Short Ratio":99}
        r = score_ticker(("Manual", dummy), yf_df=yf_data.get(ticker))
        r["manual"] = True
        results.append(r)

    rank_order = {"hot":0,"warm":1,"watch":2,"avoid":3}
    qualified  = [r for r in results if r["score"] >= MIN_SCORE or r["manual"]]
    for r in qualified: r["rank"] = get_rank(r)

    final = sorted([r for r in qualified if not r["manual"]],
                   key=lambda x: (rank_order.get(x["rank"],4), -x["score"])
               ) + [r for r in qualified if r["manual"]]

    print(f"\n  Qualified: {len(qualified)} | " +
          " | ".join(f"{rk.upper()}: {sum(1 for r in qualified if r.get('rank')==rk)}"
                     for rk in ["hot","warm","watch"]))

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{today}_{args.session}.json")
    with open(path, "w") as f:
        json.dump({"date":today.isoformat(),"session":args.session,
                   "generated":gen_time,"tickers":final}, f, indent=2)
    print(f"  [+] JSON → {path}")

if __name__ == "__main__":
    main()
