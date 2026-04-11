"""
Time of Day Analysis — 60 Day
Analyzes when low float runners hit their intraday high over 60 days.
Uses top gainers list from Finviz as the ticker universe.

Usage (GitHub Actions): trigger 'analyze' session
"""

import yfinance as yf
import pandas as pd
import json, glob, os
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

et = ZoneInfo("America/New_York")
DATA_DIR = "docs/data"

# ── Ticker universe ────────────────────────────────────────────────────────────

GAINERS_60D = [
    'FUSE','RAYA','CREG','SKYQ','CUE','ZNTL','UCAR','SQFT','TPST','MAXN',
    'OGN','SIDU','LWLG','AIXI','PSTV','CBUS','ALOY','NCEL','OCC','MFI',
    'MTVA','TMCI','XNDU','CIGL','YYGH','HURA','POET','GPRK','TRON','AIB',
    'CNCK','MKDW','INDP','COHN','WGRX','FBYD','EKSO','ATOM','CURR','MVO',
    'TE','SXTC','WOK','ALDX','BTTC','FCUV','HYPD','DBGI','LNKS','CSTE',
    'ALLR','UMAC','RCEL','PLCE','BENF','ASST','QNCX','PRFX','SG','RBBN',
    'TMCR','ZJYL','MRLN','SPHL','ENVX','CTNT','PCT','XRTX','AREN','BOLT',
    'OTLY','GRNQ','MWG','IXHL','SGML','AIFU','SKIL','FATN','QNTM','DPRO',
    'BNKK','TLX','KXIN','IZEA','AAME','CARS','ROLR','SPWH','SUNE','VMAR',
    'OBAI','NIO','ZEPP','WLAC','BMR','TOYO','VTSI','CTW','UGRO','ACH',
    'PBM','WYFI','HOVR','PURR','BBGI','MAIA','CMND','AMPX','HTOO','BLDP',
    'ASPI','OFS','EMAT','NTHI','GTN','HKIT','LINK','LPL','HUIZ','MDBH',
    'MHH','GOVX','DSWL','CURV','ONEG','INFQ','CMTG','CULP','UPXI','STAK',
    'KYIV','ASYS','PDM','SHIM','DFDV','DTI','PYXS','SSL','AREC','LPTH',
    'DAIO','LTRN','BTMD','LPCN','NOMA','RPAY','LIXT','ACHV','ODD','MVST',
    'NPT','LI','NCT'
]

# Also include our watchlist tickers
watchlist_tickers = set()
for fpath in glob.glob(f'{DATA_DIR}/*.json'):
    if 'eod_results' in fpath:
        continue
    try:
        with open(fpath) as f:
            data = json.load(f)
        for t in data.get('tickers', []):
            ticker = t.get('ticker', '')
            if ticker:
                watchlist_tickers.add(ticker)
    except:
        continue

all_tickers = sorted(set(GAINERS_60D) | watchlist_tickers)
print(f"\n📋 Analyzing {len(all_tickers)} tickers total")
print(f"   {len(GAINERS_60D)} from Finviz top gainers")
print(f"   {len(watchlist_tickers)} from our watchlist history")
print(f"   Combined unique: {len(all_tickers)}\n")

# ── Download in batches ────────────────────────────────────────────────────────

print("⏳ Fetching 5-min bars (60 days)...")
BATCH_SIZE = 50
all_data = {}

for i in range(0, len(all_tickers), BATCH_SIZE):
    batch = all_tickers[i:i+BATCH_SIZE]
    print(f"   Batch {i//BATCH_SIZE + 1}/{(len(all_tickers)-1)//BATCH_SIZE + 1} — {len(batch)} tickers...")
    try:
        data = yf.download(
            batch,
            period="60d",
            interval="5m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
            prepost=True,
        )
        for ticker in batch:
            try:
                if len(batch) == 1:
                    df = data
                else:
                    if ticker not in data.columns.get_level_values(0):
                        continue
                    df = data[ticker]
                if df is not None and not df.empty:
                    all_data[ticker] = df
            except:
                continue
    except Exception as e:
        print(f"   Batch error: {e}")
        continue

print(f"\n✅ Downloaded data for {len(all_data)} tickers\n")

# ── Analyze ────────────────────────────────────────────────────────────────────

time_buckets_30 = defaultdict(int)
ticker_results  = []
MIN_MOVE = 0.08

for ticker, df in all_data.items():
    try:
        df.index = pd.to_datetime(df.index).tz_convert(et)
        df = df.between_time('04:00', '20:00')

        for day, group in df.groupby(df.index.date):
            if group.empty or len(group) < 5:
                continue

            high_val  = group['High'].max()
            open_val  = group['Open'].iloc[0]
            close_val = group['Close'].iloc[-1]

            if not open_val or open_val == 0:
                continue

            pct_move = (high_val - open_val) / open_val
            if pct_move < MIN_MOVE:
                continue

            high_idx = group['High'].idxmax()
            h, m     = int(high_idx.hour), int(high_idx.minute)
            m30      = 0 if m < 30 else 30
            bucket30 = f"{h:02d}:{m30:02d}"

            time_buckets_30[bucket30] += 1
            ticker_results.append({
                'ticker':    ticker,
                'date':      str(day),
                'high_time': high_idx.strftime('%H:%M'),
                'pct_move':  round(pct_move * 100, 1),
                'high':      round(high_val, 2),
                'open':      round(open_val, 2),
            })
    except:
        continue

total = sum(time_buckets_30.values())

# ── Output ─────────────────────────────────────────────────────────────────────

print("=" * 65)
print("📊 60-DAY TIME OF DAY ANALYSIS — When do runners hit their HIGH?")
print(f"   (Stocks that moved 8%+ | {total} events | {len(all_data)} tickers)")
print("=" * 65)

LABELS = {
    "04:00": "← Pre-market opens",
    "07:00": "← Robinhood opens",
    "09:00": "← Late pre-market",
    "09:30": "← Market OPENS",
    "10:00": "← Post-open momentum",
    "12:00": "← Lunch",
    "15:30": "← Power hour",
    "16:00": "← After hours opens",
    "19:30": "← AH thins out",
}

for time_str in sorted(time_buckets_30.keys()):
    h, m = map(int, time_str.split(':'))
    if h < 4 or h >= 20:
        continue
    count = time_buckets_30[time_str]
    pct   = round(count / total * 100) if total else 0
    bar   = "█" * (pct // 2)
    label = LABELS.get(time_str, "")
    print(f"  {time_str}  {bar:<25} {pct:3}% ({count:4}) {label}")

print("=" * 65)

top5 = sorted(time_buckets_30.items(), key=lambda x: x[1], reverse=True)[:5]
print("\n🎯 TOP 5 WINDOWS:")
for t, c in top5:
    pct = round(c / total * 100)
    print(f"   {t} ET  →  {pct}% of runners ({c} events)")

periods = {
    "Pre-Market    (4:00-9:30 AM)":  [f"{h:02d}:{m:02d}" for h in range(4,9) for m in [0,30]] + ["09:00"],
    "Market Open   (9:30-11:00 AM)": ["09:30","10:00","10:30"],
    "Mid-Morning   (11AM-12:30PM)":  ["11:00","11:30","12:00"],
    "Midday        (12:30-2:00 PM)": ["12:30","13:00","13:30"],
    "Afternoon     (2:00-4:00 PM)":  ["14:00","14:30","15:00","15:30"],
    "After Hours   (4:00-8:00 PM)":  [f"{h:02d}:{m:02d}" for h in range(16,20) for m in [0,30]],
}

print("\n📅 BY TRADING PERIOD:")
for period, buckets in periods.items():
    count = sum(time_buckets_30.get(b, 0) for b in buckets)
    pct   = round(count / total * 100) if total else 0
    bar   = "█" * (pct // 3)
    print(f"   {period:<38} {pct:3}% {bar}")

print(f"\n📈 TOP 20 BIGGEST MOVES:")
ticker_results.sort(key=lambda x: x['pct_move'], reverse=True)
for r in ticker_results[:20]:
    print(f"   {r['ticker']:6} {r['date']}  high at {r['high_time']} ET  +{r['pct_move']:.1f}%")

print(f"\n✅ Analysis complete — {len(ticker_results)} runner days analyzed")

print("\n💡 OPTIMAL SCAN SCHEDULE (based on 60-day data):")
top3_periods = sorted(periods.items(),
    key=lambda x: sum(time_buckets_30.get(b,0) for b in x[1]),
    reverse=True)[:3]
for period, _ in top3_periods:
    print(f"   ✓ {period.strip()}")
print()
