"""
Micro-Cap Momentum Swing Scanner
Reads config from environment variables set in watchlist.yml.
Fetches catalyst dates: earnings (Finviz), 8-K (EDGAR), M&A closing (EDGAR S-4),
clinical trial readout (ClinicalTrials.gov), investor day (EDGAR 8-K text),
FDA date (news headline parsing).
"""

import os, sys, json, math, argparse, csv, io, time, re
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import urllib.request, urllib.parse

TOKEN        = os.environ.get("FINVIZ_TOKEN", "")
ET           = ZoneInfo("America/New_York")
MIN_SCORE    = float(os.environ.get("SWING_MIN_SCORE",    "0.35"))
SPIKE_DAYS   = int(os.environ.get("SWING_SPIKE_DAYS",     "90"))
SPIKE_PCT    = float(os.environ.get("SWING_SPIKE_PCT",    "40"))
SHORT_MIN    = float(os.environ.get("SWING_SHORT_INT_MIN","10"))
DTC_MAX      = float(os.environ.get("SWING_DTC_MAX",      "3"))
MANUAL       = [t.strip() for t in os.environ.get("SWING_MANUAL","BTBD,FLYX,AZTR,BFRG,UGRO,DEFT,BYAH,OXBR,SST,SER,EEIQ").split(",")]
OUT_DIR      = os.environ.get("SWING_OUT_DIR",  "docs")
DATA_DIR     = os.environ.get("SWING_DATA_DIR", "docs/data/swing")

WEIGHTS = {
    "fresh_8k":0.25,"merger_pivot":0.20,"previous_spike":0.20,
    "short_squeeze":0.15,"high_rel_vol":0.10,"fda_binary":0.10,
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
FDA_DATE_PATTERNS = [
    r"PDUFA\s+date\s+of\s+([A-Za-z]+ \d{1,2},?\s*\d{4})",
    r"PDUFA\s+date[:\s]+([A-Za-z]+ \d{1,2},?\s*\d{4})",
    r"FDA\s+(?:decision|action|approval)\s+expected\s+(?:by\s+)?([A-Za-z]+ \d{1,2},?\s*\d{4})",
    r"FDA\s+(?:decision|action)\s+(?:date[:\s]+)?([A-Za-z]+ \d{1,2},?\s*\d{4})",
]
SCREENERS = [
    ("Low Float",
     "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136"
     "&f=cap_smallunder,sh_curvol_o5000,sh_float_u20,sh_price_u10,sh_relvol_o2&ex=nasdaq,nyse,amex"),
    ("Mid Float",
     "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136"
     "&f=cap_smallunder,sh_curvol_o5000,sh_float_20to100x,sh_price_u20,sh_relvol_o3&ex=nasdaq,nyse,amex"),
]
EDGAR_HDR = {"User-Agent":"swing-scanner research@dwilsolutions.com"}

# ── Helpers ────────────────────────────────────────────────────────────────────

def safe_f(v, default=0.0):
    try:
        f = float(str(v).replace("%","").replace("$","").replace(",","").strip())
        return default if math.isnan(f) or math.isinf(f) else f
    except: return default

def fetch_url(url, headers=None, timeout=10):
    try:
        req = urllib.request.Request(url, headers=headers or {"User-Agent":"Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except: return None

def parse_date_str(s):
    if not s: return None
    for fmt in ("%Y-%m-%d","%m/%d/%Y","%B %d, %Y","%b %d, %Y","%B %d %Y","%b %d %Y","%Y-%m"):
        try: return datetime.strptime(s.strip(), fmt).date()
        except: continue
    return None

def fmt_date(d):
    if not d: return None
    try: return d.strftime("%b %-d, %Y")
    except: return str(d)

def days_until(d):
    if not d: return None
    return (d - date.today()).days

# ── Finviz ─────────────────────────────────────────────────────────────────────

def fetch_finviz(scan_label, filters):
    if not TOKEN:
        print(f"  [!] No FINVIZ_TOKEN — skipping {scan_label}"); return []
    url = f"https://elite.finviz.com/export.ashx?{filters}&auth={TOKEN}"
    raw = fetch_url(url, timeout=30)
    if not raw: print(f"  [!] Finviz failed ({scan_label})"); return []
    rows = list(csv.DictReader(io.StringIO(raw)))
    print(f"  [+] {scan_label}: {len(rows)} tickers")
    return [(scan_label, r) for r in rows]

# ── yfinance ───────────────────────────────────────────────────────────────────

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
        return mx >= SPIKE_PCT, round(float(mx),1) if not math.isnan(mx) else 0
    except: return False, 0

def calc_entries(price, yf_df):
    if yf_df is None or price <= 0: return None
    try:
        entry1 = round(float(yf_df["High"].iloc[-10:].max()), 4)
        entry2 = round(float(yf_df["Close"].iloc[-20:].mean()), 4)
        entry3 = round(entry1 * 1.15, 4)
        stop   = round(float(yf_df["Low"].iloc[-2]), 4)
        t1 = round(entry1 * 1.272, 4)
        t2 = round(entry1 * 1.618, 4)
        t3 = round(entry1 * 2.618, 4)
        pct = lambda t: round((t - entry1) / entry1 * 100, 1) if entry1 else 0
        return {
            "entry1":entry1,"entry1_note":"Break above 10d high",
            "entry2":entry2,"entry2_note":"Pulls to 20d average",
            "entry3":entry3,"entry3_note":"Makes new highs",
            "stop":stop,"stop_note":"Prior day low",
            "target1":t1,"target1_pct":pct(t1),
            "target2":t2,"target2_pct":pct(t2),
            "target3":t3,"target3_pct":pct(t3),
        }
    except: return None

# ── EDGAR 8-K ──────────────────────────────────────────────────────────────────

def check_edgar_8k(ticker, days=7):
    end   = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()
    url   = f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22&dateRange=custom&startdt={start}&enddt={end}&forms=8-K"
    raw   = fetch_url(url, headers=EDGAR_HDR)
    if not raw: return False, False, "", None
    try:
        hits = json.loads(raw).get("hits",{}).get("hits",[])
        if not hits: return False, False, "", None
        filed = hits[0].get("_source",{}).get("file_date","")
        text  = " ".join(str(h.get("_source",{}).get("entity_name","")) for h in hits).lower()
        for kw in MERGER_KEYWORDS + FDA_KEYWORDS:
            if kw in text: return True, True, kw, filed
        return True, False, "", filed
    except: return False, False, "", None

# ── EDGAR M&A closing ──────────────────────────────────────────────────────────

def fetch_ma_closing_date(ticker):
    url = (f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
           f"&dateRange=custom&startdt={(date.today()-timedelta(days=180)).isoformat()}"
           f"&enddt={date.today().isoformat()}&forms=S-4,DEFM14A")
    raw = fetch_url(url, headers=EDGAR_HDR)
    if not raw: return None
    try:
        hits = json.loads(raw).get("hits",{}).get("hits",[])
        for hit in hits[:3]:
            filed = hit.get("_source",{}).get("file_date","")
            d = parse_date_str(filed)
            if d:
                estimated = d + timedelta(days=60)
                if estimated > date.today(): return estimated
        return None
    except: return None

# ── ClinicalTrials.gov ─────────────────────────────────────────────────────────

def fetch_clinical_trial_date(company, ticker):
    for term in [company[:30] if company else "", ticker]:
        if not term: continue
        try:
            q   = urllib.parse.quote(term)
            url = (f"https://clinicaltrials.gov/api/v2/studies"
                   f"?query.term={q}&filter.overallStatus=RECRUITING,ACTIVE_NOT_RECRUITING"
                   f"&fields=primaryCompletionDate,studyType,phase&pageSize=5")
            raw = fetch_url(url, timeout=12)
            if not raw: continue
            for s in json.loads(raw).get("studies",[]):
                proto = s.get("protocolSection",{})
                phase = proto.get("designModule",{}).get("phases",[])
                if not any("PHASE2" in p or "PHASE3" in p for p in phase): continue
                pcd = proto.get("statusModule",{}).get("primaryCompletionDateStruct",{})
                d = parse_date_str(pcd.get("date",""))
                if d and d > date.today(): return d
        except: continue
    return None

# ── EDGAR Investor Day ─────────────────────────────────────────────────────────

def fetch_investor_day(ticker):
    url = (f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22+%22investor+day%22"
           f"&dateRange=custom&startdt={(date.today()-timedelta(days=90)).isoformat()}"
           f"&enddt={date.today().isoformat()}&forms=8-K")
    raw = fetch_url(url, headers=EDGAR_HDR)
    if not raw: return None
    try:
        hits = json.loads(raw).get("hits",{}).get("hits",[])
        for hit in hits[:3]:
            d = parse_date_str(hit.get("_source",{}).get("period_of_report",""))
            if d and d > date.today(): return d
        return None
    except: return None

# ── FDA date from news ─────────────────────────────────────────────────────────

def parse_fda_date(title):
    if not title: return None
    for pat in FDA_DATE_PATTERNS:
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            d = parse_date_str(m.group(1))
            if d and d > date.today(): return d
    return None

# ── News keyword check ─────────────────────────────────────────────────────────

def check_news(title):
    if not title: return False, ""
    text = title.lower()
    for kw in MERGER_KEYWORDS + FDA_KEYWORDS:
        if kw in text: return True, kw
    return False, ""

# ── Scoring ────────────────────────────────────────────────────────────────────

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
    earnings_raw = str(fv.get("Earnings Date",""))
    sector     = fv.get("Sector","").lower()
    company    = fv.get("Company","")

    scores = {k:0.0 for k in WEIGHTS}
    flags  = []

    # 1. Fresh 8-K
    has_8k, has_kw, kw, filed_date = check_edgar_8k(ticker)
    if has_8k:
        scores["fresh_8k"] = 1.0
        flags.append(("FRESH 8-K","catalyst"))
    time.sleep(0.1)

    # 2. Catalyst
    has_cat, cat_kw = check_news(news_title)
    kw_used = cat_kw or kw
    if has_cat or has_kw:
        scores["merger_pivot"] = 1.0
        if any(k in kw_used for k in ["merger","acquisition","loi","letter of intent","reverse merger"]):
            flags.append(("CATALYST · M&A","catalyst"))
        elif any(k in kw_used for k in ["ai","artificial intelligence","crypto","blockchain","drone"]):
            flags.append(("CATALYST · PIVOT","catalyst"))
        elif any(k in kw_used for k in ["fda","nda","bla","pdufa","approval"]):
            flags.append(("FDA EVENT","catalyst"))
        else:
            flags.append(("CATALYST · NEWS","catalyst"))

    # 3. Spike
    spike_pct = 0
    if yf_df is not None:
        had_spike, spike_pct = check_spike(yf_df)
        if had_spike:
            scores["previous_spike"] = 1.0
            flags.append((f"PREV SPIKE +{spike_pct:.0f}%","gap"))

    # 4. Short squeeze
    if short_pct >= SHORT_MIN and days_cover <= DTC_MAX:
        scores["short_squeeze"] = 1.0
        flags.append((f"SHORT SQUEEZE {short_pct:.0f}%","danger"))
    elif short_pct >= SHORT_MIN:
        scores["short_squeeze"] = 0.5
        flags.append((f"SHORT INT {short_pct:.0f}%","danger"))

    # 5. Volume
    scores["high_rel_vol"] = 1.0 if rvol>=10 else 0.7 if rvol>=5 else 0.4 if rvol>=2 else 0.0

    # 6. FDA
    fda_date = parse_fda_date(news_title)
    if (scores["merger_pivot"]==1.0 and kw_used and
        any(k in kw_used for k in ["fda","nda","pdufa","approval"])):
        scores["fda_binary"] = 1.0
    elif fda_date:
        scores["fda_binary"] = 1.0

    total = sum(scores[k] * WEIGHTS[k] for k in WEIGHTS)

    # ── Catalyst dates ──
    earnings_date = parse_date_str(earnings_raw) if earnings_raw not in ("","N/A","--") else None

    is_biotech = any(w in sector for w in ["health","biotech","pharma"])
    ma_close_date  = None
    clinical_date  = None
    investor_day   = None

    if scores["merger_pivot"]==1.0 and "m&a" in " ".join(f[0] for f in flags).lower():
        ma_close_date = fetch_ma_closing_date(ticker); time.sleep(0.1)
    if is_biotech or scores["fda_binary"] > 0:
        clinical_date = fetch_clinical_trial_date(company, ticker); time.sleep(0.2)
    investor_day = fetch_investor_day(ticker); time.sleep(0.1)

    dates = {
        "earnings":          fmt_date(earnings_date),
        "earnings_days":     days_until(earnings_date),
        "eightk_filed":      filed_date,
        "ma_close":          fmt_date(ma_close_date),
        "fda":               fmt_date(fda_date),
        "clinical":          fmt_date(clinical_date),
        "investor_day":      fmt_date(investor_day),
    }

    # Display overrides for manual tickers with no data
    price_display = f"${price:.2f}" if price > 0 else "—"
    rvol_display  = f"{rvol:.0f}x"  if rvol  > 0 else "—"
    float_display = f"{safe_f(fv.get('Shares Float',0)):.1f}M" if safe_f(fv.get("Shares Float",0)) > 0 else "—"

    return {
        "ticker":ticker,"scan":scan_label,"company":company,
        "sector":fv.get("Sector",""),"price":price,"change":change,
        "rvol":rvol,"float_m":safe_f(fv.get("Shares Float",0)),
        "short_pct":short_pct,"days_cover":days_cover,"spike_pct":spike_pct,
        "news":news_title[:120],"news_url":news_url,
        "score":round(total,3),"components":scores,"flags":flags,
        "dates":dates,"entries":calc_entries(price, yf_df),
        "price_display":price_display,"rvol_display":rvol_display,
        "float_display":float_display,
    }

def get_rank(t):
    s = t["score"]
    if s >= 0.70: return "hot"
    if s >= 0.50: return "warm"
    if s >= 0.35: return "watch"
    return "avoid"

# ── Main ───────────────────────────────────────────────────────────────────────

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
                 "Relative Volume":0,"News Title":"","News URL":"","Earnings Date":"",
                 "Shares Float":0,"Short Float":0,"Short Ratio":99}
        r = score_ticker(("Manual",dummy), yf_df=yf_data.get(ticker))
        r["manual"] = True
        results.append(r)

    rank_order = {"hot":0,"warm":1,"watch":2,"avoid":3}
    qualified  = [r for r in results if r["score"] >= MIN_SCORE or r["manual"]]
    for r in qualified:
        raw_rank = get_rank(r)
        r["rank"] = "watch" if r.get("manual") and raw_rank == "avoid" else raw_rank

    final = (sorted([r for r in qualified if not r["manual"]],
                    key=lambda x: (rank_order.get(x["rank"],4), -x["score"]))
             + [r for r in qualified if r["manual"]])

    print(f"\n  Qualified: {len(qualified)} | " +
          " | ".join(f"{rk.upper()}: {sum(1 for r in qualified if r.get('rank')==rk)}"
                     for rk in ["hot","warm","watch"]))

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{today}_{args.session}.json")
    with open(path,"w") as f:
        json.dump({"date":today.isoformat(),"session":args.session,
                   "generated":gen_time,"tickers":final}, f, indent=2)
    print(f"  [+] JSON → {path}")

if __name__ == "__main__":
    main()
