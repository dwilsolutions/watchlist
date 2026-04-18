"""
build_backtest.py — yfinance only, no external API needed
Pulls 90 days of daily OHLCV, finds 20%+ intraday runner days,
outputs backtest_runners.csv for threshold optimisation.

Usage:
    pip install yfinance pandas
    python3 build_backtest.py
"""

import os, sys, csv
from datetime import date, timedelta
from collections import defaultdict, Counter

START_DATE   = (date.today() - timedelta(days=90)).isoformat()
END_DATE     = date.today().isoformat()
MIN_GAIN_PCT = 20
MAX_PRICE    = 20.0
RVOL_WINDOW  = 20
OUT_FILE     = "backtest_runners.csv"

UNIVERSE = sorted(set([
    "ABLV","ACHV","AGAE","AHMA","ALGS","ARAI","ASTI","BIRD","BOLD","BTBD",
    "BTOG","BZAI","CAPS","CMCT","CMPS","COSM","CRML","CTNT","DARE","DEVS",
    "DFNS","DGNX","DLXY","EFOI","ELAB","ENVB","EVTL","FRMM","FUSE","GAME",
    "GBR","GCTK","HOTH","HXHX","IFRX","IMA","INO","ISPC","ITP","LASE",
    "LIMN","LNAI","LRHC","MAMO","MIMI","MITQ","MLSS","MNTS","MYSE","NCI",
    "NEXA","NNBR","NOTV","NPT","ONFO","PBM","PMNT","RCT","RECT","RETO",
    "RMSG","ROLR","RPAY","SBEV","SGLY","SIDU","SLNH","SNAL","SNYR","SOAR",
    "TRVI","UAVS","VRAX","VSA","WATT","WGRX","YXT","ZSPC","HUBC","IMMP",
    "BEAT","SURG","ASBP","PMAX","GOVX","CLOV","MVIS","EXPR","BBIG","BFRI",
    "ATER","ILUS","RNAZ","NCTY","GFAI","BACK","ATXG","BNED","CLPS","DATS",
    "EDSA","ELOX","FTFT","GCBC","GNPX","GRPN","HOOK","HPNN","HYMC","IDEX",
    "IMVT","INBS","INPX","JAGX","JBDI","KALA","KAVL","LFLY","LGVN","LIQT",
    "LKCO","LPCN","MAXN","MBOT","MEGL","MEIP","MGAM","MITI","MNDO","MOBQ",
    "MOGO","MOTS","MPLN","MRAI","MREO","MYMD","MYSZ","NCNA","NDRA","NKGN",
    "NNOX","NRXP","NTBL","NVAX","NVFY","NVOS","OCAX","OCGN","ONCY","ONVO",
]))
UNIVERSE = sorted(set(t for t in UNIVERSE if t.isalpha() and len(t) <= 5))


def simulate_rank(rvol, gap_pct):
    if rvol < 3:                     return "avoid"
    if gap_pct < -10:                return "avoid"
    if rvol >= 50 and gap_pct >= 20: return "hot"
    if rvol >= 100 and gap_pct >= 5: return "hot"
    if rvol >= 200:                  return "hot"
    if rvol >= 15:                   return "warm"
    if rvol >= 8 and gap_pct >= 5:   return "warm"
    return "watch"


def process_ticker(ticker, df):
    """Process a single ticker DataFrame and return runner rows."""
    import pandas as pd
    rows = []
    try:
        df = df.dropna(subset=["Open","High","Close","Volume"])
        if len(df) < RVOL_WINDOW + 5:
            return rows

        df = df.copy()
        df["avg_vol"]    = df["Volume"].shift(1).rolling(RVOL_WINDOW).mean()
        df["rvol"]       = df["Volume"] / df["avg_vol"]
        df["prev_close"] = df["Close"].shift(1)
        df["gap_pct"]    = (df["Open"]  - df["prev_close"]) / df["prev_close"] * 100
        df["gain_pct"]   = (df["High"]  - df["Open"])       / df["Open"]       * 100
        df["close_pct"]  = (df["Close"] - df["Open"])       / df["Open"]       * 100

        # Only look at our target window and price range
        df = df[df.index >= pd.Timestamp(START_DATE)]
        df = df[df["Open"] <= MAX_PRICE]

        for idx, row in df[df["gain_pct"] >= MIN_GAIN_PCT].iterrows():
            rvol      = round(float(row["rvol"]),      1) if pd.notna(row["rvol"])      else 0
            gap_pct   = round(float(row["gap_pct"]),   1) if pd.notna(row["gap_pct"])   else 0
            gain_pct  = round(float(row["gain_pct"]),  1)
            close_pct = round(float(row["close_pct"]), 1)
            rows.append({
                "date":      idx.strftime("%Y-%m-%d"),
                "ticker":    ticker,
                "open":      round(float(row["Open"]),  2),
                "high":      round(float(row["High"]),  2),
                "close":     round(float(row["Close"]), 2),
                "volume":    int(row["Volume"]),
                "avg_vol":   int(row["avg_vol"]) if pd.notna(row["avg_vol"]) else 0,
                "rvol":      rvol,
                "gap_pct":   gap_pct,
                "gain_pct":  gain_pct,
                "close_pct": close_pct,
                "dumped":    close_pct < (gain_pct - 15),
                "rank":      simulate_rank(rvol, gap_pct),
            })
    except Exception as e:
        pass
    return rows


def main():
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("pip install yfinance pandas")
        sys.exit(1)

    # Pull with extra history for rvol baseline
    extended_start = (date.fromisoformat(START_DATE) - timedelta(days=RVOL_WINDOW + 10)).isoformat()
    print(f"Universe: {len(UNIVERSE)} tickers | {START_DATE} to {END_DATE}")

    # Download in batches of 50 to avoid yfinance multi-ticker issues
    BATCH = 50
    all_runners = []
    skipped = 0

    for i in range(0, len(UNIVERSE), BATCH):
        batch = UNIVERSE[i:i+BATCH]
        print(f"\nBatch {i//BATCH + 1}: {batch[0]} to {batch[-1]} ({len(batch)} tickers)")

        try:
            raw = yf.download(
                batch,
                start=extended_start,
                end=END_DATE,
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=True,
                group_by="ticker",
            )
        except Exception as e:
            print(f"  Download failed: {e}")
            skipped += len(batch)
            continue

        if raw.empty:
            print(f"  Empty result")
            skipped += len(batch)
            continue

        print(f"  Shape: {raw.shape}")

        for ticker in batch:
            try:
                # Handle both single and multi-ticker responses
                if len(batch) == 1:
                    df = raw.copy()
                elif isinstance(raw.columns, pd.MultiIndex):
                    if ticker in raw.columns.get_level_values(0):
                        df = raw[ticker].copy()
                    else:
                        skipped += 1
                        continue
                else:
                    skipped += 1
                    continue

                rows = process_ticker(ticker, df)
                all_runners.extend(rows)
                if rows:
                    print(f"  {ticker}: {len(rows)} runner days")

            except Exception as e:
                skipped += 1

    print(f"\nSkipped {skipped} tickers | Found {len(all_runners)} runner days total")

    if not all_runners:
        print("No runners found. The tickers may all be delisted or outside price range.")
        sys.exit(1)

    all_runners.sort(key=lambda x: (x["date"], -x["gain_pct"]))

    # Print top results
    print(f"\n{'Date':<12} {'Ticker':<6} {'Gain%':>7} {'Close%':>8} {'RVol':>7} {'Gap%':>6} {'Rank':<6} {'Dump?'}")
    print("-"*65)
    for r in sorted(all_runners, key=lambda x: -x["gain_pct"])[:30]:
        print(f"{r['date']:<12} {r['ticker']:<6} {r['gain_pct']:>+6.1f}% "
              f"{r['close_pct']:>+7.1f}% {r['rvol']:>7.1f}x "
              f"{r['gap_pct']:>+5.1f}% {r['rank']:<6} "
              f"{'DUMP' if r['dumped'] else ''}")

    # Rank performance
    print(f"\n=== Rank performance ===")
    rank_stats = defaultdict(lambda: {"n":0,"gain_sum":0,"dump":0,"run50":0})
    for r in all_runners:
        s = rank_stats[r["rank"]]
        s["n"] += 1
        s["gain_sum"] += r["gain_pct"]
        s["dump"]     += int(r["dumped"])
        s["run50"]    += int(r["gain_pct"] >= 50)

    print(f"{'Rank':<8} {'n':>4} {'Avg Gain%':>10} {'Run 50%+':>10} {'Dump%':>7}")
    print("-"*45)
    for rank in ["hot","warm","watch","avoid"]:
        s = rank_stats[rank]
        n = s["n"]
        if n == 0: continue
        print(f"{rank:<8} {n:>4} {s['gain_sum']/n:>+9.1f}%  "
              f"{s['run50']:>3} ({s['run50']/n*100:>4.0f}%)  "
              f"{s['dump']/n*100:>5.0f}%")

    # Top tickers
    print(f"\nMost frequent runners:")
    for ticker, n in Counter(r["ticker"] for r in all_runners).most_common(15):
        print(f"  {ticker:<6} {n} days")

    # Write CSV
    fields = ["date","ticker","open","high","close","volume","avg_vol",
              "rvol","gap_pct","gain_pct","close_pct","dumped","rank"]
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(all_runners)

    print(f"\nWritten {len(all_runners)} rows to {OUT_FILE}")
    print("Drop this CSV back into Claude to run full threshold optimisation.")


if __name__ == "__main__":
    main()
