"""
Backfill script — re-runs EOD outcome calculations for all historical dates
using the fixed SESSION_WINDOWS (session-specific highs instead of full-day).

Run once: python3 backfill.py
"""

import os, sys, json, math
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ── Import everything we need from results.py ──────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from results import (
    fetch_session_highs, fetch_real_vwap, fetch_quotes,
    calc_outcome, load_cumulative, render_html, fmt_date,
    SESSION_WINDOWS, SESSIONS_ORDER, DATA_DIR, OUTPUT_DIR,
)

def safe(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except Exception:
        return default

def backfill_date(target_date):
    """Re-run EOD for a single date using session JSONs already on disk."""
    date_str  = target_date.isoformat()
    print(f"\n{'='*60}")
    print(f"Backfilling {date_str}")

    # Find all session JSONs for this date
    sessions_data = {}
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.startswith(date_str) or "eod" in fname or not fname.endswith(".json"):
            continue
        session_key = fname.replace(f"{date_str}_", "").replace(".json", "")
        with open(os.path.join(DATA_DIR, fname)) as f:
            data = json.load(f)
        sessions_data[session_key] = data
        print(f"  Loaded {fname} — {len(data.get('tickers', []))} tickers")

    if not sessions_data:
        print(f"  No session data found — skipping")
        return False

    # Collect all unique tickers
    all_tickers = {}
    for session_key, data in sessions_data.items():
        for t in data.get("tickers", []):
            all_tickers[t["ticker"]] = t

    print(f"  {len(all_tickers)} unique tickers across {len(sessions_data)} sessions")

    # Fetch final quotes
    quotes = fetch_quotes(list(all_tickers.keys()))
    print(f"  Fetched quotes for {len(quotes)} tickers")

    # Re-calculate outcomes per session with correct session windows
    session_results = {}
    eod_save = {"date": date_str, "generated": "backfill", "sessions": {}}

    for session_key, data in sessions_data.items():
        tickers_in_session = [t["ticker"] for t in data.get("tickers", [])]

        # Fetch session-specific highs using FIXED SESSION_WINDOWS
        try:
            s_highs, s_lows = fetch_session_highs(tickers_in_session, session_key, target_date)
        except Exception as e:
            print(f"  [!] Session highs failed for {session_key}: {e}")
            s_highs, s_lows = {}, {}

        # Fetch VWAP
        try:
            vwaps = fetch_real_vwap(tickers_in_session, session_key, target_date)
        except Exception as e:
            print(f"  [!] VWAP failed for {session_key}: {e}")
            vwaps = {}

        results = []
        for t in data.get("tickers", []):
            ticker = t["ticker"]
            quote  = quotes.get(ticker)
            if not quote:
                continue
            s_high    = s_highs.get(ticker)
            s_low     = s_lows.get(ticker)
            real_vwap = vwaps.get(ticker)
            entry_price = t.get("entry", 0)
            above_vwap  = (entry_price >= real_vwap) if real_vwap else None

            perf = calc_outcome(t, quote, session_high=s_high, session_low=s_low)
            if perf:
                perf["real_vwap"]  = real_vwap
                perf["above_vwap"] = above_vwap
                entry = {**t, "perf": perf, "outcome": perf["outcome"]}
                if perf.get("dumped"):
                    entry["dumped"] = True
                results.append(entry)

        session_results[session_key] = results
        eod_save["sessions"][session_key] = [
            {k: v for k, v in r.items() if k != "perf"}
            for r in results
        ]

        # Print session summary
        runners = [r for r in results if r["outcome"] in ("monster","big_runner","runner")]
        print(f"  {session_key}: {len(results)} tickers, {len(runners)} runners")

    # Overwrite EOD JSON
    eod_path = os.path.join(DATA_DIR, f"{date_str}_eod_results.json")
    with open(eod_path, "w") as f:
        json.dump(eod_save, f, indent=2)
    print(f"  ✅ Overwrote {eod_path}")

    return True

def main():
    # Get all dates that have EOD files
    eod_dates = sorted([
        fname.replace("_eod_results.json", "")
        for fname in os.listdir(DATA_DIR)
        if fname.endswith("_eod_results.json")
    ])

    print(f"Found {len(eod_dates)} EOD files to backfill:")
    for d in eod_dates:
        print(f"  {d}")

    success = 0
    for date_str in eod_dates:
        target = date.fromisoformat(date_str)
        if backfill_date(target):
            success += 1

    # Rebuild the EOD HTML with updated cumulative stats
    print(f"\n{'='*60}")
    print(f"Backfill complete: {success}/{len(eod_dates)} dates processed")
    print(f"Re-running today's EOD HTML render with updated cumulative stats...")

    # Load updated cumulative stats and re-render today's page
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.date()
    gen_time = now_et.strftime("%I:%M %p ET")

    # Find most recent EOD results for HTML render
    most_recent = sorted([
        fname for fname in os.listdir(DATA_DIR)
        if fname.endswith("_eod_results.json")
    ])[-1]
    most_recent_date = date.fromisoformat(most_recent.replace("_eod_results.json",""))

    with open(os.path.join(DATA_DIR, most_recent)) as f:
        latest_eod = json.load(f)

    # Rebuild session_results for render
    session_results = {}
    all_quotes = {}
    for session_key, tickers in latest_eod["sessions"].items():
        if isinstance(tickers, list):
            session_results[session_key] = [
                {**t, "perf": {
                    "close": safe(t.get("entry", 0)),
                    "high":  safe(t.get("entry", 0)),
                    "low":   safe(t.get("entry", 0)),
                    "pct_close": 0, "pct_high": 0,
                    "outcome": t.get("outcome","flat"),
                    "dumped": t.get("dumped", False),
                    "real_vwap": None, "above_vwap": None,
                }}
                for t in tickers
            ]

    cum_stats = load_cumulative()
    print(f"\nUpdated cumulative stats:")
    for tier in ("buy", "monitor"):
        d = cum_stats[tier]
        t = d["total"]
        print(f"  {tier}: {t} total, "
              f"{d['runner']}/{t} runners ({d['runner']/t*100:.1f}% if t else 0), "
              f"{d['big_runner']}/{t} big ({d['big_runner']/t*100:.1f}% if t else 0), "
              f"{d['monster']}/{t} monsters ({d['monster']/t*100:.1f}% if t else 0)")

if __name__ == "__main__":
    main()
