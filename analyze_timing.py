"""
Time of Day Analysis
Analyzes when low float runners hit their intraday high.
Run this script to find the optimal scan times.

Usage:
    pip install yfinance pandas
    python analyze_timing.py
"""

import yfinance as yf
import pandas as pd
import json, glob, os
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

et = ZoneInfo("America/New_York")
DATA_DIR = "docs/data"

# ── Collect tickers from session JSONs ─────────────────────────────────────────

tickers = set()
for fpath in glob.glob(f'{DATA_DIR}/*.json'):
    if 'eod_results' in fpath:
        continue
    with open(fpath) as f:
        data = json.load(f)
    for t in data.get('tickers', []):
        ticker = t.get('ticker', '')
        if ticker:
            tickers.add(ticker)

tickers = sorted(tickers)
print(f"\n📋 Analyzing {len(tickers)} tickers from watchlist history")
print(f"   {tickers}\n")

# ── Download 5-min bars ────────────────────────────────────────────────────────

print("⏳ Fetching 5-min bars (last 5 trading days)...")
data = yf.download(
    tickers,
    period="5d",
    interval="5m",
    group_by="ticker",
    auto_adjust=True,
    progress=True,
    threads=True,
    prepost=True,
)
print("✅ Download complete\n")

# ── Analyze time of day highs ──────────────────────────────────────────────────

time_buckets    = defaultdict(int)   # 5-min bucket → count
time_buckets_30 = defaultdict(int)   # 30-min bucket → count
ticker_results  = []

MIN_MOVE = 0.05  # only count days where stock moved 5%+

for ticker in tickers:
    try:
        df = data if len(tickers) == 1 else (
            data[ticker] if ticker in data.columns.get_level_values(0) else None
        )
        if df is None or df.empty:
            continue

        df.index = pd.to_datetime(df.index).tz_convert(et)
        df = df.between_time('04:00', '20:00')

        for day, group in df.groupby(df.index.date):
            if group.empty or len(group) < 3:
                continue

            high_val  = group['High'].max()
            open_val  = group['Open'].iloc[0]
            close_val = group['Close'].iloc[-1]

            if not open_val or open_val == 0:
                continue

            pct_move = (high_val - open_val) / open_val

            if pct_move < MIN_MOVE:
                continue  # skip flat days

            # When did the high occur?
            high_idx  = group['High'].idxmax()
            high_time = high_idx.strftime('%H:%M')
            h, m      = int(high_idx.hour), int(high_idx.minute)
            m30       = 0 if m < 30 else 30
            bucket30  = f"{h:02d}:{m30:02d}"

            time_buckets[high_time]    += 1
            time_buckets_30[bucket30]  += 1

            ticker_results.append({
                'ticker':     ticker,
                'date':       str(day),
                'high_time':  high_time,
                'pct_move':   round(pct_move * 100, 1),
                'high':       round(high_val, 2),
                'open':       round(open_val, 2),
                'close':      round(close_val, 2),
            })

    except Exception as e:
        print(f"  ⚠️  {ticker}: {e}")

# ── Print results ──────────────────────────────────────────────────────────────

total = sum(time_buckets_30.values())

print("=" * 60)
print("📊 TIME OF DAY ANALYSIS — When do runners hit their HIGH?")
print(f"   (Stocks that moved 5%+ | {total} events across {len(tickers)} tickers)")
print("=" * 60)

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

cumulative = 0
for time_str in sorted(time_buckets_30.keys()):
    h, m = map(int, time_str.split(':'))
    if h < 4 or (h == 20 and m > 0):
        continue
    count = time_buckets_30[time_str]
    pct   = round(count / total * 100) if total else 0
    cumulative += pct
    bar   = "█" * (pct // 2)
    label = LABELS.get(time_str, "")
    print(f"  {time_str}  {bar:<20} {pct:3}% ({count:3}) {label}")

print("=" * 60)

# Top windows
top5 = sorted(time_buckets_30.items(), key=lambda x: x[1], reverse=True)[:5]
print("\n🎯 TOP 5 WINDOWS — Highest concentration of runner highs:")
for t, c in top5:
    pct = round(c / total * 100)
    print(f"   {t} ET  →  {pct}% of runners ({c} events)")

# Cumulative by period
periods = {
    "Pre-Market (4:00-9:30 AM)":   [f"{h:02d}:{m:02d}" for h in range(4,9) for m in [0,30]] + ["09:00"],
    "Market Open (9:30-11:00 AM)": ["09:30","10:00","10:30"],
    "Mid-Morning (11:00AM-12:30PM)":["11:00","11:30","12:00"],
    "Midday (12:30-2:00 PM)":      ["12:30","13:00","13:30"],
    "Afternoon (2:00-4:00 PM)":    ["14:00","14:30","15:00","15:30"],
    "After Hours (4:00-8:00 PM)":  [f"{h:02d}:{m:02d}" for h in range(16,20) for m in [0,30]],
}

print("\n📅 BY TRADING PERIOD:")
for period, buckets in periods.items():
    count = sum(time_buckets_30.get(b, 0) for b in buckets)
    pct   = round(count / total * 100) if total else 0
    bar   = "█" * (pct // 3)
    print(f"   {period:<35} {pct:3}% {bar}")

# Individual ticker breakdown
print("\n📈 BIGGEST MOVERS — Individual results:")
ticker_results.sort(key=lambda x: x['pct_move'], reverse=True)
for r in ticker_results[:20]:
    print(f"   {r['ticker']:6} {r['date']}  high at {r['high_time']} ET  +{r['pct_move']:.1f}%  (open ${r['open']} → high ${r['high']})")

print(f"\n✅ Analysis complete — {len(ticker_results)} runner days analyzed")
print("\n💡 RECOMMENDATION:")
top_period = max(periods.items(), key=lambda x: sum(time_buckets_30.get(b,0) for b in x[1]))
print(f"   Most runners hit their high during: {top_period[0]}")
print(f"   Schedule your most important scan BEFORE this window opens\n")
