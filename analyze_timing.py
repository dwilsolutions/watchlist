"""
Time of Day Analysis — 60 Day
Analyzes when low float runners hit their intraday high over 60 days.
Uses top gainers list + watchlist history as the ticker universe.

Usage (GitHub Actions): trigger 'analyze' session
"""

import yfinance as yf
import pandas as pd
import json, glob, os
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict
import math

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
    'NPT','LI','NCT',
    # Expanded from Finviz export + session JSONs
    'ABLV','ACHV','AGAE','AHMA','ALGS','ARAI','ASTI','BIRD','BOLD','BTBD',
    'BTOG','BZAI','CAPS','CMCT','CMPS','COSM','CRML','DARE','DEVS','DFNS',
    'DGNX','DLXY','EFOI','ELAB','ENVB','EVTL','FRMM','GAME','GBR','GCTK',
    'HOTH','HXHX','IFRX','IMA','INO','ISPC','ITP','LASE','LIMN','LNAI',
    'LRHC','MAMO','MIMI','MITQ','MLSS','MNTS','MYSE','NCI','NEXA','NNBR',
    'NOTV','ONFO','PMNT','RCT','RECT','RETO','RMSG','RPAY','SBEV','SGLY',
    'SLNH','SNAL','SNYR','SOAR','TRVI','UAVS','VRAX','VSA','WATT','YXT',
    'ZSPC','HUBC','IMMP','BEAT','SURG','ASBP','PMAX','CLOV','MVIS','EXPR',
    'BBIG','BFRI','ATER','ILUS','RNAZ','NCTY','GFAI','BACK','ATXG','BNED',
    'CLPS','DATS','EDSA','ELOX','FTFT','GNPX','GRPN','HOOK','HPNN','HYMC',
    'IDEX','IMVT','INBS','INPX','JAGX','JBDI','KALA','KAVL','LFLY','LGVN',
    'LIQT','LKCO','LPCN','MAXN','MBOT','MEGL','MEIP','MGAM','MITI','MNDO',
    'MOBQ','MOGO','MOTS','MPLN','MRAI','MREO','MYMD','MYSZ','NCNA','NDRA',
    'NKGN','NNOX','NRXP','NTBL','NVAX','NVFY','NVOS','OCAX','OCGN','ONCY',
    'ONVO',
]

# Pull from watchlist session JSONs
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

all_tickers = sorted(set(
    t for t in (set(GAINERS_60D) | watchlist_tickers)
    if t.isalpha() and len(t) <= 5
))
print(f"\nAnalyzing {len(all_tickers)} tickers | last 60 days")
print(f"  {len(GAINERS_60D)} from gainers list")
print(f"  {len(watchlist_tickers)} from watchlist history")

# ── Download in batches ────────────────────────────────────────────────────────

print("\nFetching 5-min bars (60 days)...")
BATCH_SIZE = 40
all_data = {}

for i in range(0, len(all_tickers), BATCH_SIZE):
    batch = all_tickers[i:i+BATCH_SIZE]
    print(f"  Batch {i//BATCH_SIZE + 1}/{-(-len(all_tickers)//BATCH_SIZE)}: "
          f"{batch[0]}..{batch[-1]}")
    try:
        raw = yf.download(
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
                    df = raw
                elif isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    df = raw[ticker]
                else:
                    continue
                if df is not None and not df.empty:
                    all_data[ticker] = df
            except:
                continue
    except Exception as e:
        print(f"  Batch error: {e}")
        continue

print(f"\nDownloaded data for {len(all_data)} tickers\n")

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

            if not open_val or open_val == 0 or open_val < 0.01 or open_val > 20.0:
                continue

            pct_move = (high_val - open_val) / open_val
            if pct_move < MIN_MOVE or pct_move > 20.0:
                continue
            if math.isnan(pct_move) or math.isinf(pct_move):
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
print(f"60-DAY TIME OF DAY — When do runners hit their HIGH?")
print(f"  Stocks that moved 8%+ | {total} events | {len(all_data)} tickers")
print("=" * 65)

LABELS = {
    "04:00": "<-- Early pre-market opens",
    "06:30": "<-- Robinhood pre-market",
    "09:00": "<-- Late pre-market",
    "09:30": "<-- Market OPENS",
    "10:00": "<-- Post-open momentum",
    "12:00": "<-- Lunch",
    "15:30": "<-- Power hour",
    "16:00": "<-- After hours opens",
    "19:30": "<-- AH thins out",
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
print("\nTOP 5 WINDOWS:")
for t, c in top5:
    pct = round(c / total * 100)
    print(f"  {t} ET  ->  {pct}% of runners ({c} events)")

periods = {
    "Early Pre-Market (4:00-6:55 AM)":  [f"{h:02d}:{m:02d}" for h in range(4,7) for m in [0,30]],
    "Pre-Market       (6:55-9:30 AM)":  [f"{h:02d}:{m:02d}" for h in range(7,10) for m in [0,30]],
    "Market Open      (9:30-11:00 AM)": ["09:30","10:00","10:30"],
    "Mid-Morning      (11AM-12:30PM)":  ["11:00","11:30","12:00"],
    "Midday           (12:30-3:30 PM)": [f"{h:02d}:{m:02d}" for h in range(12,16) for m in [0,30]],
    "After Hours      (4:00-8:00 PM)":  [f"{h:02d}:{m:02d}" for h in range(16,20) for m in [0,30]],
}

print("\nBY TRADING PERIOD:")
period_counts = {}
for period, buckets in periods.items():
    count = sum(time_buckets_30.get(b, 0) for b in buckets)
    pct   = round(count / total * 100) if total else 0
    bar   = "█" * (pct // 3)
    period_counts[period] = count
    print(f"  {period:<42} {pct:3}%  {bar}")

print("\nRANKED BY RUNNER CONCENTRATION:")
for period, count in sorted(period_counts.items(), key=lambda x: -x[1]):
    pct = round(count / total * 100) if total else 0
    print(f"  {pct:3}%  {period.strip()}")

print(f"\nTOP 20 BIGGEST MOVES:")
ticker_results.sort(key=lambda x: x['pct_move'], reverse=True)
for r in ticker_results[:20]:
    print(f"  {r['ticker']:6} {r['date']}  high at {r['high_time']} ET  +{r['pct_move']:.1f}%")

print(f"\nAnalysis complete — {len(ticker_results)} runner days analyzed")

print("\nOPTIMAL SCAN SCHEDULE (based on 60-day data):")
top3 = sorted(period_counts.items(), key=lambda x: -x[1])[:3]
for period, count in top3:
    pct = round(count / total * 100) if total else 0
    print(f"  {pct}%  {period.strip()}")
print()
