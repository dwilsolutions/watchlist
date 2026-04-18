"""
build_backtest.py
=================
Pulls 90 days of daily OHLCV for a broad low-float universe via yfinance.
Finds every day a stock ran 20%+ intraday (high vs open).
Computes gap%, rvol proxy, simulates HOT/WARM/WATCH/AVOID ranking.
Outputs backtest_runners.csv for threshold optimisation.

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
    "BEAT","SURG","ASBP","PMAX","GOVX","MULN","MARK","PROG","CLOV","MVIS",
    "EXPR","BBIG","SPRT","BFRI","ATER","ILUS","RNAZ","NCTY","GFAI","BACK",
    "DPSI","ATNF","ABSI","AGLE","ATXG","BNED","CLPS","DATS","EDSA","ELOX",
    "FTFT","GCBC","GNPX","GRPN","HLVX","HOOK","HPNN","HYMC","IDEX","IMVT",
    "INBS","INPX","JAGX","JBDI","KALA","KAVL","KERN","KTTA","LFLY","LGVN",
    "LIQT","LKCO","LPCN","MARK","MAXN","MBOT","MDNA","MEGL","MEIP","MGAM",
    "MITI","MKUL","MNDO","MOBQ","MOGO","MOTS","MPLN","MRAI","MREO","MULN",
    "MYMD","MYSZ","NBRV","NCNA","NDRA","NKGN","NLSP","NNOX","NRXP","NTBL",
    "NVAX","NVFY","NVOS","OCAX","OCGN","ONCY","ONVO","OPTN",
]))
UNIVERSE = [t for t in UNIVERSE if t.isalpha() and len(t) <= 5]


def simulate_rank(rvol, gap_pct):
    if rvol < 3:                              return "avoid"
    if gap_pct < -10:                         return "avoid"
    if rvol >= 50 and gap_pct >= 20:          return "hot"
    if rvol >= 100 and gap_pct >= 5:          return "hot"
    if rvol >= 200:                           return "hot"
    if rvol >= 15:                            return "warm"
    if rvol >= 8 and gap_pct >= 5:            return "warm"
    return "watch"


def main():
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("pip install yfinance pandas")
        sys.exit(1)

    print(f"Universe: {len(UNIVERSE)} tickers | {START_DATE} to {END_DATE}")
    extended_start = (date.fromisoformat(START_DATE) - timedelta(days=RVOL_WINDOW + 10)).isoformat()

    data = yf.download(
        UNIVERSE,
        start=extended_start,
        end=END_DATE,
        interval="1d",
        auto_adjust=True,
        progress=True,
        threads=True,
    )

    if data.empty:
        print("ERROR: No data returned.")
        sys.exit(1)

    print(f"Downloaded {data.shape}")
    runners = []
    skipped = 0

    for ticker in UNIVERSE:
        try:
            df = data[ticker].copy() if len(UNIVERSE) > 1 else data.copy()
            df = df.dropna(subset=["Open","High","Close","Volume"])
            if len(df) < RVOL_WINDOW + 5:
                skipped += 1; continue

            df["avg_vol"]   = df["Volume"].shift(1).rolling(RVOL_WINDOW).mean()
            df["rvol"]      = df["Volume"] / df["avg_vol"]
            df["prev_close"]= df["Close"].shift(1)
            df["gap_pct"]   = (df["Open"] - df["prev_close"]) / df["prev_close"] * 100
            df["gain_pct"]  = (df["High"]  - df["Open"])      / df["Open"]       * 100
            df["close_pct"] = (df["Close"] - df["Open"])      / df["Open"]       * 100

            df = df[df.index >= pd.Timestamp(START_DATE)]
            df = df[df["Open"] <= MAX_PRICE]

            for idx, row in df[df["gain_pct"] >= MIN_GAIN_PCT].iterrows():
                rvol      = round(float(row["rvol"]),      1) if not pd.isna(row["rvol"])      else 0
                gap_pct   = round(float(row["gap_pct"]),   1) if not pd.isna(row["gap_pct"])   else 0
                gain_pct  = round(float(row["gain_pct"]),  1)
                close_pct = round(float(row["close_pct"]), 1)
                runners.append({
                    "date":      idx.strftime("%Y-%m-%d"),
                    "ticker":    ticker,
                    "open":      round(float(row["Open"]),  2),
                    "high":      round(float(row["High"]),  2),
                    "close":     round(float(row["Close"]), 2),
                    "volume":    int(row["Volume"]),
                    "avg_vol":   int(row["avg_vol"]) if not pd.isna(row["avg_vol"]) else 0,
                    "rvol":      rvol,
                    "gap_pct":   gap_pct,
                    "gain_pct":  gain_pct,
                    "close_pct": close_pct,
                    "dumped":    close_pct < (gain_pct - 15),
                    "rank":      simulate_rank(rvol, gap_pct),
                })
        except Exception:
            skipped += 1

    print(f"Skipped {skipped} | Found {len(runners)} runner days\n")
    if not runners:
        print("No runners found.")
        sys.exit(1)

    runners.sort(key=lambda x: (x["date"], -x["gain_pct"]))

    print(f"{'Date':<12} {'Ticker':<6} {'Gain%':>7} {'Close%':>8} {'RVol':>7} {'Gap%':>6} {'Rank':<6} {'Dump?'}")
    print("-"*65)
    for r in sorted(runners, key=lambda x: -x["gain_pct"])[:30]:
        print(f"{r['date']:<12} {r['ticker']:<6} {r['gain_pct']:>+6.1f}% "
              f"{r['close_pct']:>+7.1f}% {r['rvol']:>7.1f}x {r['gap_pct']:>+5.1f}% "
              f"{r['rank']:<6} {'DUMP' if r['dumped'] else ''}")

    print(f"\n=== Rank performance ===")
    rank_stats = defaultdict(lambda: {"n":0,"gain_sum":0,"dump":0,"run50":0})
    for r in runners:
        s = rank_stats[r["rank"]]
        s["n"] += 1; s["gain_sum"] += r["gain_pct"]
        s["dump"] += int(r["dumped"]); s["run50"] += int(r["gain_pct"] >= 50)

    print(f"{'Rank':<8} {'n':>4} {'Avg Gain%':>10} {'Run 50%+':>10} {'Dump%':>7}")
    print("-"*45)
    for rank in ["hot","warm","watch","avoid"]:
        s = rank_stats[rank]; n = s["n"]
        if n == 0: continue
        print(f"{rank:<8} {n:>4} {s['gain_sum']/n:>+9.1f}%  "
              f"{s['run50']:>3} ({s['run50']/n*100:>4.0f}%)   {s['dump']/n*100:>5.0f}%")

    print(f"\nTop tickers:")
    for ticker, n in Counter(r["ticker"] for r in runners).most_common(15):
        print(f"  {ticker:<6} {n} runner days")

    fields = ["date","ticker","open","high","close","volume","avg_vol",
              "rvol","gap_pct","gain_pct","close_pct","dumped","rank"]
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(runners)

    print(f"\nWritten {len(runners)} rows to {OUT_FILE}")
    print("Drop this CSV back into Claude to run full threshold optimisation.")

if __name__ == "__main__":
    main()
