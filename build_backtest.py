"""
build_backtest.py
Pulls 6 months of daily OHLCV for a broad low-float/small-cap universe.
Finds every day a stock ran 20%+ intraday (high vs open).
Computes gap% and rvol proxy. Outputs backtest_runners.csv.

Usage:
    pip install yfinance pandas
    python3 build_backtest.py
"""

import os, sys, csv
from datetime import date, timedelta
from collections import defaultdict, Counter

START_DATE   = (date.today() - timedelta(days=180)).isoformat()
END_DATE     = date.today().isoformat()
MIN_GAIN_PCT = 20
MAX_PRICE    = 20.0
RVOL_WINDOW  = 20
OUT_FILE     = "backtest_runners.csv"

UNIVERSE = [
    "AALG","ABLV","ABTC","ACHV","ADAC","ADMA","AEF","AGAE","AHG","AHMA",
    "ALGS","ALOT","ALXO","AMC","AMS","ANGX","ARAI","ARBK","ARRY","ARTV",
    "ASBP","ASRT","ASTI","ATER","ATHE","ATLX","ATXG","AWAY","BABU","BACK",
    "BAIG","BB","BBIG","BBOT","BCAR","BCSS","BDRY","BDVG","BEAT","BEBE",
    "BEZ","BFRI","BGMS","BIRD","BIYA","BLRK","BMEA","BMNG","BNC","BNED",
    "BOLD","BRFH","BSAA","BTBD","BTCS","BTM","BTOG","BTX","BULX","BYFC",
    "BYND","BZAI","BZFD","CADL","CANG","CAPS","CCG","CGEN","CIFG","CLDI",
    "CLGN","CLOV","CLPS","CMCT","CMND","CMPS","COPZ","COSM","CPHI","CPOP",
    "CRDL","CRIS","CRMG","CRML","CRMU","CRWS","CSHR","CTAA","CTNT","CTW",
    "CULP","CVKD","CWD","DAIO","DAMD","DARE","DATS","DBL","DCGO","DEVS",
    "DFNS","DGNX","DHY","DLPN","DLXY","DMII","DOJE","DOMH","DRAY","DRIP",
    "DSWL","DVDN","DWTX","EDSA","EDUC","EEV","EFOI","EFU","EFZ","EGG",
    "ELAB","ELDN","ELOX","ENGN","ENGS","ENTX","ENVB","EQS","ERIC","EVAX",
    "EVOX","EVTL","EXPR","FAMI","FBLG","FCHL","FERA","FGI","FGII","FIGG",
    "FIXP","FKWL","FLD","FLYX","FOFO","FRMI","FRMM","FRSH","FSI","FTFT",
    "FTLF","FUFU","FUSE","GAME","GBR","GCBC","GCTK","GEVO","GFAI","GLOO",
    "GLXU","GNPX","GNS","GNSS","GOVX","GPRO","GRAG","GRCE","GREE","GRPN",
    "GTIM","HIVE","HLP","HMYY","HODU","HOOK","HOTH","HPNN","HQ","HTBK",
    "HTZ","HUBC","HXHX","HYMC","HYPD","HYPR","ICCC","ICG","IDEX","IEAG",
    "IFRX","ILLR","ILS","ILUS","IMA","IMMP","IMRN","IMSR","IMVT","INBS",
    "INFQ","INO","INPX","INTT","IONZ","IPHA","IPW","ISPC","ITP","JAGX",
    "JBDI","JLHL","KALA","KAVL","KDK","KMRK","KNOW","KTCC","KURA","KVHI",
    "KYN","LAFA","LAKE","LASE","LCDL","LCID","LEDS","LESL","LEXX","LFLY",
    "LGPS","LGVN","LIMN","LION","LIQT","LKCO","LMFA","LNAI","LNKB","LPCN",
    "LRHC","LSAK","LU","LZM","LZMH","MAAS","MAKO","MAMO","MAPS","MATH",
    "MAXN","MBOT","MDBH","MDCX","MEGL","MEIP","MGAM","MGTX","MIMI","MIND",
    "MITI","MITQ","MLSS","MNDO","MNTS","MOBQ","MOGO","MOTS","MPLN","MRAI",
    "MREO","MSAI","MSTP","MSTU","MSTZ","MTC","MVIS","MYMD","MYSE","MYSZ",
    "NB","NBIZ","NBRG","NCI","NCNA","NCPL","NCTY","NDRA","NEO","NEXA",
    "NKGN","NKTX","NMTC","NNBR","NNOX","NNVC","NOTV","NOWL","NPT","NPWR",
    "NRT","NRXP","NSPR","NTBL","NUCL","NVAX","NVFY","NVOS","OBE","OCAX",
    "OCGN","OCSL","OKLL","ONCY","ONFO","ONVO","OPTU","OPTX","ORCU","OTLY",
    "OWLS","PAL","PBM","PLBL","PLCE","PMAX","PMNT","PN","PNI","POM",
    "PONX","POWW","PPBT","PPSI","PRCH","PTOR","QRHC","QSEA","QTTB","RAVE",
    "RCT","RDWU","RECT","RETO","RKDA","RLJ","RMBI","RMSG","RNAC","RNAZ",
    "ROLR","RPAY","RRGB","RUM","SANG","SBC","SBEV","SBLX","SBTU","SCD",
    "SCLX","SCO","SGLY","SGRP","SHLS","SHMD","SHRT","SIDU","SKBL","SKK",
    "SKLZ","SLNH","SMU","SNAL","SNGX","SNTI","SNYR","SOAR","SOBR","SPPL",
    "SPWH","SQNS","SRBK","SSM","SURG","SVC","SVIV","SZZL","TBH","TDTH",
    "TGEN","TORO","TOVX","TPST","TRAW","TRUG","TRVI","TSHA","TTEC","TTRX",
    "UAVS","UBXG","UCAR","VACI","VNCE","VRAX","VSA","VVOS","VWAV","WAI",
    "WATT","WEA","WETH","WGRX","WHWK","WIMI","WKHS","WLAC","WLII","WSHP",
    "WTG","WTID","WXET","XAIR","XBP","XCBE","YDES","YXT","ZDAI","ZEO",
    "ZKH","ZONE","ZSPC",
]
UNIVERSE = sorted(set(t for t in UNIVERSE if t.isalpha() and len(t) <= 5))


def simulate_rank(rvol, gap_pct):
    if rvol < 3:                    return "avoid"
    if gap_pct < -20:               return "avoid"
    if rvol >= 50:                  return "hot"
    if rvol >= 20 and gap_pct >= 10: return "hot"
    if rvol >= 15:                  return "warm"
    if rvol >= 8 and gap_pct >= 5:  return "warm"
    return "watch"


def process_ticker(ticker, df):
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
    except Exception:
        pass
    return rows


def main():
    try:
        import yfinance as yf
        import pandas as pd
    except ImportError:
        print("pip install yfinance pandas")
        sys.exit(1)

    extended_start = (date.fromisoformat(START_DATE) - timedelta(days=RVOL_WINDOW + 10)).isoformat()
    print(f"Universe: {len(UNIVERSE)} tickers")
    print(f"Period:   {START_DATE} to {END_DATE} (6 months)")
    print(f"Looking for: intraday gain >= {MIN_GAIN_PCT}%, open price <= ${MAX_PRICE}\n")

    BATCH = 50
    all_runners = []
    skipped = 0

    for i in range(0, len(UNIVERSE), BATCH):
        batch = UNIVERSE[i:i+BATCH]
        print(f"Batch {i//BATCH + 1}/{-(-len(UNIVERSE)//BATCH)}: "
              f"{batch[0]} to {batch[-1]} ({len(batch)} tickers)")
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
            skipped += len(batch)
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    df = raw.copy()
                elif isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        skipped += 1; continue
                    df = raw[ticker].copy()
                else:
                    skipped += 1; continue

                rows = process_ticker(ticker, df)
                all_runners.extend(rows)
                if rows:
                    print(f"  {ticker}: {len(rows)} runner days")
            except Exception:
                skipped += 1

    print(f"\nSkipped {skipped} | Found {len(all_runners)} runner days total\n")

    if not all_runners:
        print("No runners found.")
        sys.exit(1)

    # Remove known data errors (rvol=1 means avg_vol calculation failed)
    clean = [r for r in all_runners if r["rvol"] > 1 or r["gain_pct"] < 1000]
    print(f"After cleaning: {len(clean)} rows")

    clean.sort(key=lambda x: (x["date"], -x["gain_pct"]))

    # Summary
    print(f"\nTop 30 runners:")
    print(f"{'Date':<12} {'Ticker':<6} {'Gain%':>7} {'Close%':>8} {'RVol':>7} {'Gap%':>6} {'Rank':<6}")
    print("-"*60)
    for r in sorted(clean, key=lambda x: -x["gain_pct"])[:30]:
        if r["gain_pct"] > 5000: continue  # skip data errors
        print(f"{r['date']:<12} {r['ticker']:<6} {r['gain_pct']:>+6.0f}% "
              f"{r['close_pct']:>+7.0f}% {r['rvol']:>7.1f}x "
              f"{r['gap_pct']:>+5.0f}% {r['rank']:<6}")

    print(f"\n=== Rank performance ===")
    for rank in ["hot","warm","watch","avoid"]:
        sub = [r for r in clean if r["rank"] == rank and r["gain_pct"] < 5000]
        if not sub: continue
        n = len(sub)
        avg  = sum(r["gain_pct"] for r in sub) / n
        m50  = sum(1 for r in sub if r["gain_pct"] >= 50)
        m100 = sum(1 for r in sub if r["gain_pct"] >= 100)
        dmp  = sum(1 for r in sub if r["dumped"])
        print(f"  {rank:<6} n={n:>4}  avg={avg:>+5.0f}%  50%+={m50:>3}({m50/n*100:>3.0f}%)  "
              f"100%+={m100:>3}({m100/n*100:>3.0f}%)  dump={dmp/n*100:>3.0f}%")

    print(f"\nTop runner tickers:")
    for t, n in Counter(r["ticker"] for r in clean).most_common(20):
        sub = [r for r in clean if r["ticker"] == t and r["gain_pct"] < 5000]
        if not sub: continue
        avg_gain = sum(r["gain_pct"] for r in sub)/len(sub)
        avg_rvol = sum(r["rvol"] for r in sub)/len(sub)
        print(f"  {t:<6} {n:>2} days  avg_gain={avg_gain:>+5.0f}%  avg_rvol={avg_rvol:>5.0f}x")

    fields = ["date","ticker","open","high","close","volume","avg_vol",
              "rvol","gap_pct","gain_pct","close_pct","dumped","rank"]
    with open(OUT_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(clean)

    print(f"\nWritten {len(clean)} rows to {OUT_FILE}")
    print("Drop this CSV back into Claude to run threshold optimisation.")


if __name__ == "__main__":
    main()
