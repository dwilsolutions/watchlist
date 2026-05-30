"""
evaluate_scan.py — Evaluate a scanner log against actual day_high prices.

Reads a scan-YYYY-MM-DD.jsonl file, fetches daily OHLC via yfinance for each
unique ticker, and writes an evaluated.json with success/failure per ticker.

Usage:
    python evaluate_scan.py path/to/scan-2026-05-20.jsonl

Outputs:
    evaluated-YYYY-MM-DD.json — full evaluated dataset
    summary-YYYY-MM-DD.json   — aggregate stats

Designed to run in GitHub Actions (no network restrictions, yfinance just works).
"""
import json
import sys
import os
from collections import defaultdict
from datetime import datetime
import yfinance as yf


SUCCESS_THRESHOLD_PCT = 5.0  # ran ≥5% from sighting = success
MIN_SIGHTING_PRICE    = 1.0  # match scanner's sh_price_o1 filter; exclude sub-$1 leakage


def load_log(path):
    """Read JSONL log, return list of records."""
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def dedupe_first_sightings(records):
    """Keep only earliest sighting per ticker."""
    seen = {}
    for r in records:
        sym = r["ticker"]
        if sym not in seen or r["time"] < seen[sym]["time"]:
            seen[sym] = r
    return list(seen.values())


def fetch_candle(ticker, date_str):
    """Fetch daily OHLC for one ticker on one date. Returns dict or None."""
    try:
        t = yf.Ticker(ticker)
        # yfinance start is inclusive, end is exclusive — pull a 2-day window for safety
        end_date = datetime.strptime(date_str, "%Y-%m-%d")
        end_str = (end_date.replace(day=end_date.day + 1)).strftime("%Y-%m-%d")
        h = t.history(start=date_str, end=end_str, auto_adjust=False)
        if h.empty:
            return None
        for idx, row in h.iterrows():
            if idx.strftime("%Y-%m-%d") == date_str:
                return {
                    "open":   float(row["Open"]),
                    "high":   float(row["High"]),
                    "low":    float(row["Low"]),
                    "close":  float(row["Close"]),
                    "volume": int(row["Volume"]),
                }
        return None
    except Exception as e:
        print(f"  fetch failed for {ticker}: {str(e)[:80]}")
        return None


def evaluate(records):
    """Run evaluation across all records. Returns (evaluated_list, failed_list, excluded_list).

    Excluded: tickers with sighting price below MIN_SIGHTING_PRICE — these match the
    scanner's sh_price_o1 filter intent (the scanner doesn't want them) but occasionally
    leak through when a stock dips below $1 mid-session after Finviz's snapshot.
    """
    evaluated = []
    failed = []
    excluded = []
    for i, r in enumerate(records):
        sym = r["ticker"]
        date_str = r["date"]
        sighting_price = r.get("price", 0) or 0

        if sighting_price <= 0:
            failed.append({"ticker": sym, "reason": "no sighting price"})
            continue

        # Match scanner's $1 floor — exclude leaked sub-$1 sightings
        if sighting_price < MIN_SIGHTING_PRICE:
            excluded.append({
                "ticker":         sym,
                "date":           date_str,
                "sighting_price": sighting_price,
                "reason":         f"price below ${MIN_SIGHTING_PRICE:.2f} floor",
            })
            continue

        candle = fetch_candle(sym, date_str)
        if candle is None:
            failed.append({"ticker": sym, "reason": "no yfinance data"})
            continue

        max_gain = (candle["high"] - sighting_price) / sighting_price * 100
        close_pct = (candle["close"] - sighting_price) / sighting_price * 100
        max_loss = (candle["low"] - sighting_price) / sighting_price * 100

        evaluated.append({
            **r,
            "day_open":  round(candle["open"], 4),
            "day_high":  round(candle["high"], 4),
            "day_low":   round(candle["low"], 4),
            "day_close": round(candle["close"], 4),
            "max_gain":  round(max_gain, 2),
            "max_loss":  round(max_loss, 2),
            "close_pct": round(close_pct, 2),
            "success":   1 if max_gain >= SUCCESS_THRESHOLD_PCT else 0,
        })

        if (i + 1) % 10 == 0:
            print(f"  ...{i + 1}/{len(records)} processed")

    return evaluated, failed


def aggregate(evaluated):
    """Build summary stats."""
    if not evaluated:
        return {"n": 0}

    n = len(evaluated)
    wins = sum(1 for r in evaluated if r["success"])
    runs = [r["max_gain"] for r in evaluated]

    def grp(items, label, key_fn):
        groups = defaultdict(list)
        for r in items:
            k = key_fn(r)
            if k is not None:
                groups[k].append(r)
        return [{
            "factor":       label,
            "value":        k,
            "count":        len(g),
            "wins":         sum(1 for x in g if x["success"]),
            "success_rate": round(100 * sum(1 for x in g if x["success"]) / len(g), 1),
            "avg_max_gain": round(sum(x["max_gain"] for x in g) / len(g), 2),
            "best":         round(max(x["max_gain"] for x in g), 2),
        } for k, g in sorted(groups.items())]

    def score_bucket(r):
        s = r.get("score", 0) or 0
        if s < 50: return "0-49"
        if s < 70: return "50-69"
        if s < 85: return "70-84"
        return "85-100"

    def price_bucket(r):
        p = r.get("price", 0) or 0
        if p < 1: return "sub $1"
        if p < 5: return "$1-$5"
        if p < 20: return "$5-$20"
        return "$20+"

    def change_bucket(r):
        c = r.get("change_pct", 0) or 0
        if c < 5: return "<5%"
        if c < 10: return "5-10%"
        if c < 20: return "10-20%"
        if c < 30: return "20-30%"
        return "≥30%"

    def flag_rows():
        flag_groups = defaultdict(list)
        for r in evaluated:
            for f in r.get("flags", []):
                flag_groups[f].append(r)
        out = []
        for flag, g in sorted(flag_groups.items(), key=lambda x: -len(x[1])):
            out.append({
                "factor":       "flag",
                "value":        flag,
                "count":        len(g),
                "wins":         sum(1 for x in g if x["success"]),
                "success_rate": round(100 * sum(1 for x in g if x["success"]) / len(g), 1),
                "avg_max_gain": round(sum(x["max_gain"] for x in g) / len(g), 2),
                "best":         round(max(x["max_gain"] for x in g), 2),
            })
        return out

    return {
        "n_evaluated":           n,
        "wins":                  wins,
        "success_rate_pct":      round(100 * wins / n, 1),
        "avg_max_gain_pct":      round(sum(runs) / n, 2),
        "median_max_gain_pct":   round(sorted(runs)[n // 2], 2),
        "best_run_pct":          round(max(runs), 2),
        "worst_run_pct":         round(min(runs), 2),
        "by_bucket":             grp(evaluated, "bucket", lambda r: r["bucket"]),
        "by_score_range":        grp(evaluated, "score_range", score_bucket),
        "by_price_range":        grp(evaluated, "price_range", price_bucket),
        "by_change_pct":         grp(evaluated, "change_range", change_bucket),
        "by_market_session":     grp(evaluated, "session", lambda r: r.get("market")),
        "by_sector":             grp(evaluated, "sector", lambda r: r.get("sector") or "Unknown"),
        "by_flag":               flag_rows(),
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python evaluate_scan.py <scan-YYYY-MM-DD.jsonl>")
        sys.exit(1)

    log_path = sys.argv[1]
    records  = load_log(log_path)

    # Skip cleanly if log is empty (e.g., market holiday)
    if not records:
        print(f"No records in {log_path} — likely a holiday or non-trading day. Skipping.")
        return

    deduped  = dedupe_first_sightings(records)
    print(f"Loaded {len(records)} sightings, {len(deduped)} unique tickers")

    print(f"\nFetching daily OHLC for {len(deduped)} tickers...")
    evaluated, failed, excluded = evaluate(deduped)
    print(f"\nEvaluated: {len(evaluated)}, Failed: {len(failed)}, Excluded (sub-${MIN_SIGHTING_PRICE:.2f}): {len(excluded)}")

    if failed:
        print(f"Failed tickers: {[f['ticker'] for f in failed]}")
    if excluded:
        print(f"Excluded (price floor): {[e['ticker'] + ' @ $' + str(e['sighting_price']) for e in excluded]}")

    # Extract date from filename or first record
    date_str = deduped[0]["date"] if deduped else "unknown"

    # Don't write anything if no tickers survived evaluation (all failed/excluded)
    if not evaluated:
        print("No tickers evaluated successfully — skipping output.")
        return

    summary = aggregate(evaluated)

    eval_path = f"evaluated-{date_str}.json"
    sum_path  = f"summary-{date_str}.json"

    with open(eval_path, "w") as f:
        json.dump({
            "date":           date_str,
            "n_records":      len(deduped),
            "n_evaluated":    len(evaluated),
            "n_failed":       len(failed),
            "n_excluded":     len(excluded),
            "evaluated":      evaluated,
            "failed":         failed,
            "excluded":       excluded,
        }, f, indent=2)

    with open(sum_path, "w") as f:
        json.dump({
            "date":    date_str,
            "summary": summary,
        }, f, indent=2)

    print(f"\nWrote {eval_path} and {sum_path}")

    # Console summary
    if evaluated:
        print(f"\n{'=' * 60}")
        print(f"SUCCESS RATE: {summary['wins']}/{summary['n_evaluated']} = {summary['success_rate_pct']}%")
        print(f"  Avg max gain:    {summary['avg_max_gain_pct']:+.2f}%")
        print(f"  Median max gain: {summary['median_max_gain_pct']:+.2f}%")
        print(f"  Best run:        {summary['best_run_pct']:+.2f}%")
        print(f"  Worst run:       {summary['worst_run_pct']:+.2f}%")
        print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
