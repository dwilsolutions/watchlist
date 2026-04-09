"""
End of Day Results Tracker
Runs at 4:30 PM ET — checks all sessions from today against final closing prices.
Renders a single daily results page for tuning the scoring system.

Usage:
    python results.py

Environment variables:
    FINVIZ_TOKEN — Finviz Elite API token
"""

import os, sys, json, math, csv, io
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import urllib.request, urllib.error

TOKEN      = os.environ.get("FINVIZ_TOKEN", "")
OUTPUT_DIR = "docs"
DATA_DIR   = os.path.join(OUTPUT_DIR, "data")

SESSIONS_ORDER = [
    ("night",     "Pre-Market"),
    ("premarket", "Market Open"),
    ("midday",    "Midday"),
    ("powerhour", "After Hours"),
]

COLS = "0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136,49"

# ── Helpers ────────────────────────────────────────────────────────────────────

def safe(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except Exception:
        return default

def fmt_date(d):
    return d.strftime("%a %b %-d, %Y")

# ── Session time windows (ET) ─────────────────────────────────────────────────

SESSION_WINDOWS = {
    "night":     ("04:00", "09:30"),  # Pre-Market window
    "premarket": ("09:30", "12:30"),  # Market Open window
    "midday":    ("12:30", "15:30"),  # Midday window
    "powerhour": ("16:00", "20:00"),  # After Hours window
}

# ── Fetch session-specific highs via yfinance ──────────────────────────────────

def fetch_session_highs(tickers, session_key, today):
    """Fetch the intraday high for each ticker within the session's time window."""
    import yfinance as yf
    import pandas as pd

    window = SESSION_WINDOWS.get(session_key)
    if not window:
        return {}

    start_time, end_time = window
    date_str = today.isoformat()

    # Build timezone-aware window
    et = ZoneInfo("America/New_York")
    start_dt = datetime.fromisoformat(f"{date_str}T{start_time}:00").replace(tzinfo=et)
    end_dt   = datetime.fromisoformat(f"{date_str}T{end_time}:00").replace(tzinfo=et)

    highs = {}
    lows  = {}

    print(f"  [+] Fetching session highs for {len(tickers)} tickers ({session_key} window {start_time}-{end_time} ET)")

    # Batch download 1-minute bars for all tickers
    try:
        data = yf.download(
            tickers,
            start=start_dt,
            end=end_dt,
            interval="1m",
            group_by="ticker",
            auto_adjust=True,
            progress=False,
            threads=True,
            prepost=True,  # include pre/post market data for PM and AH sessions
        )
        print(f"  [+] yfinance returned data shape: {data.shape if hasattr(data, 'shape') else 'unknown'}")
    except Exception as e:
        print(f"  [!] yfinance download error: {e}", file=sys.stderr)
        return {}, {}

    if data.empty:
        print(f"  [!] yfinance returned empty data for {session_key} window")
        return {}, {}

    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = data
            else:
                if ticker not in data.columns.get_level_values(0):
                    continue
                df = data[ticker]
            if df is None or df.empty:
                continue
            highs[ticker] = round(float(df["High"].max()), 2)
            lows[ticker]  = round(float(df["Low"].min()), 2)
        except Exception as e:
            print(f"  [!] Error processing {ticker}: {e}")
            continue

    print(f"  [+] Session highs fetched for {len(highs)}/{len(tickers)} tickers")
    return highs, lows

# ── Fetch closing prices via yfinance ─────────────────────────────────────────

def fetch_quotes(tickers):
    """Fetch end of day closing prices for all tickers."""
    import yfinance as yf
    quotes = {}
    try:
        data = yf.download(
            tickers,
            period="1d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
        for ticker in tickers:
            try:
                if len(tickers) == 1:
                    row = data.iloc[-1]
                else:
                    row = data.xs(ticker, axis=1, level=1).iloc[-1]
                quotes[ticker] = {
                    "Price": round(float(row["Close"]), 2),
                    "High":  round(float(row["High"]), 2),
                    "Low":   round(float(row["Low"]), 2),
                    "Open":  round(float(row["Open"]), 2),
                }
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] yfinance quotes error: {e}", file=sys.stderr)
    print(f"  [+] Closing quotes fetched for {len(quotes)} tickers")
    return quotes

# ── Load today's session JSONs ─────────────────────────────────────────────────

def load_today_sessions(today):
    sessions = {}
    if not os.path.exists(DATA_DIR):
        return sessions
    for session_key, label in SESSIONS_ORDER:
        fname = f"{today.isoformat()}_{session_key}.json"
        fpath = os.path.join(DATA_DIR, fname)
        if os.path.exists(fpath):
            with open(fpath) as f:
                sessions[session_key] = json.load(f)
            print(f"  [+] Loaded {fname} — {len(sessions[session_key]['tickers'])} tickers")
        else:
            print(f"  [-] No data for {session_key} today")
    return sessions

# ── Calculate performance ──────────────────────────────────────────────────────

def calc_outcome(t, quote, session_high=None, session_low=None):
    entry = t["entry"]
    stop  = t["stop"]
    tp1   = t["tp1"]
    tp2   = t["tp2"]
    tp3   = t["tp3"]

    close = safe(quote.get("Price")) if isinstance(quote, dict) else safe(quote)
    # Use session-specific high/low if available, fall back to full day
    high  = session_high if session_high else safe(quote.get("High"))
    low   = session_low  if session_low  else safe(quote.get("Low"))

    if not close or not entry:
        return None

    pct_close = round((close - entry) / entry * 100, 1) if entry else 0
    pct_high  = round((high  - entry) / entry * 100, 1) if entry else 0

    if low <= stop:
        outcome = "stopped"
    elif high >= tp3:
        outcome = "tp3"
    elif high >= tp2:
        outcome = "tp2"
    elif high >= tp1:
        outcome = "tp1"
    elif close > entry:
        outcome = "open_up"
    else:
        outcome = "open_down"

    return {
        "close":       round(close, 2),
        "high":        round(high, 2),
        "low":         round(low, 2),
        "pct_close":   pct_close,
        "pct_high":    pct_high,
        "outcome":     outcome,
        "session_window": SESSION_WINDOWS.get(t.get("session_key", ""), ("?","?")),
    }

# ── Cumulative stats ───────────────────────────────────────────────────────────

def load_cumulative():
    stats = {
        "buy":     {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "stopped": 0},
        "monitor": {"total": 0, "tp1": 0, "tp2": 0, "tp3": 0, "stopped": 0},
        "days":    0,
    }
    if not os.path.exists(DATA_DIR):
        return stats
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith("_eod_results.json"):
            continue
        with open(os.path.join(DATA_DIR, fname)) as f:
            data = json.load(f)
        stats["days"] += 1
        for s in data.get("sessions", {}).values():
            if isinstance(s, list):
                ticker_list = s
            else:
                ticker_list = s.get("tickers", [])
            for t in ticker_list:
                outcome = t.get("outcome", "")
                tier    = "buy" if t.get("tier") == "buy" else "monitor"
                stats[tier]["total"] += 1
                if outcome in ("tp1", "tp2", "tp3"):
                    stats[tier]["tp1"] += 1
                if outcome in ("tp2", "tp3"):
                    stats[tier]["tp2"] += 1
                if outcome == "tp3":
                    stats[tier]["tp3"] += 1
                if outcome == "stopped":
                    stats[tier]["stopped"] += 1
    return stats

# ── HTML ───────────────────────────────────────────────────────────────────────

OUTCOME_CFG = {
    "tp3":       ("🎯 TP3",      "#1e3d2a", "#6ee89a"),
    "tp2":       ("✅ TP2",      "#1e3d2a", "#6ee89a"),
    "tp1":       ("✅ TP1",      "#1e3d2a", "#6ee89a"),
    "stopped":   ("❌ Stopped",  "#3d1a1a", "#f57a7a"),
    "open_up":   ("📈 Up",       "#1a2a3d", "#7ab4f5"),
    "open_down": ("📉 Down",     "#3d2e1a", "#f5c46e"),
}

SCAN_COLORS = {
    "Low Float": ("#1a2a3d", "#7ab4f5"),
    "Mid Cap":   ("#2a1e3d", "#b07af5"),
}

CSS = """
:root{--bg:#0c0e11;--bg2:#141618;--bg3:#1c1f23;--border:rgba(255,255,255,0.07);--text:#dde1e9;--muted:#656c7a;--green:#3a9c5f;--amber:#c07b1a;--red:#a33333;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.5;}
a{color:inherit;text-decoration:none;}
.hdr{background:var(--bg2);border-bottom:1px solid var(--border);padding:16px 20px;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.hdr-l h1{font-family:var(--sans);font-size:20px;font-weight:700;letter-spacing:-0.3px;}
.hdr-l h1 em{color:var(--green);font-style:normal;}
.hdr-l .sub{font-size:11px;color:var(--muted);margin-top:3px;}
.hdr-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.pill{font-size:10px;padding:3px 9px;border-radius:20px;background:var(--bg3);color:var(--muted);border:1px solid var(--border);}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border-bottom:1px solid var(--border);}
.sum-cell{background:var(--bg2);padding:12px 16px;text-align:center;}
.sum-n{font-family:var(--sans);font-size:26px;font-weight:700;}
.sum-l{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:0.06em;text-transform:uppercase;}
.c-g{color:var(--green);}.c-a{color:var(--amber);}.c-r{color:var(--red);}
.body{padding:18px 20px 48px;max-width:940px;margin:0 auto;}
.cumulative{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:20px;}
.cum-title{font-family:var(--sans);font-size:14px;font-weight:500;margin-bottom:12px;}
.cum-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.cum-section{background:var(--bg3);border-radius:8px;padding:12px;}
.cum-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;margin-bottom:8px;}
.cum-row{display:flex;justify-content:space-between;font-size:12px;padding:3px 0;border-bottom:1px solid var(--border);}
.cum-row:last-child{border-bottom:none;}
.cum-val-g{font-weight:500;color:var(--green);}
.cum-val-r{font-weight:500;color:var(--red);}
.sec-lbl{font-size:10px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin:22px 0 9px;display:flex;align-items:center;gap:8px;}
.sec-lbl::after{content:'';flex:1;height:1px;background:var(--border);}
.session-block{margin-bottom:8px;}
.cards{display:flex;flex-direction:column;gap:7px;margin-bottom:16px;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:12px 15px;border-left-width:3px;}
.card.buy{border-left-color:var(--green);}
.card.monitor{border-left-color:var(--amber);}
.r1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.tkr{font-family:var(--sans);font-size:15px;font-weight:700;min-width:46px;}
.co{font-size:11px;color:var(--muted);flex:1;}
.scan-tag{font-size:10px;padding:2px 7px;border-radius:10px;border:1px solid;white-space:nowrap;}
.score-pill{font-size:11px;padding:2px 8px;border-radius:20px;background:var(--bg3);color:var(--muted);}
.outcome-pill{font-size:11px;font-weight:500;padding:2px 9px;border-radius:20px;white-space:nowrap;}
.pct{font-size:12px;font-weight:500;}
.pos{color:#5cc98a;}.neg{color:#e06060;}
.clink{font-size:11px;color:#5a8fd4;margin-left:auto;}
.levels{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:4px;margin-top:8px;}
.lv{background:var(--bg3);border-radius:6px;padding:4px 8px;}
.lv-l{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}
.lv-v{font-size:11px;font-weight:500;margin-top:1px;}
.hit{color:#6ee89a;}.miss{color:var(--muted);}.stp{color:#f57a7a;}
.empty{color:var(--muted);font-size:12px;padding:8px 0;}
.no-data{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;color:var(--muted);font-size:12px;text-align:center;}
.footer{text-align:center;font-size:10px;color:var(--muted);padding:24px;border-top:1px solid var(--border);}
"""

def card_html(t, perf):
    tier    = t["tier"]
    ticker  = t["ticker"]
    outcome = perf["outcome"]
    entry   = t["entry"]
    stop    = t["stop"]
    tp1, tp2, tp3 = t["tp1"], t["tp2"], t["tp3"]
    high    = perf["high"]
    close   = perf["close"]
    pct_h   = perf["pct_high"]
    pct_c   = perf["pct_close"]

    olabel, obg, otx = OUTCOME_CFG.get(outcome, ("—", "#1c1f23", "#656c7a"))
    scan_bg, scan_tx = SCAN_COLORS.get(t["scan"], ("#1c1f23", "#656c7a"))
    pc_cls  = "pos" if pct_c >= 0 else "neg"
    pc_sign = "+" if pct_c >= 0 else ""
    ph_cls  = "pos" if pct_h >= 0 else "neg"
    ph_sign = "+" if pct_h >= 0 else ""

    def lv_cls(target):
        if outcome == "stopped": return "stp"
        return "hit" if high >= target else "miss"

    stop_cls = "stp" if perf["low"] <= stop else "miss"

    return f"""<div class="card {tier}">
  <div class="r1">
    <span class="tkr">{ticker}</span>
    <span class="co">{t["company"][:20]}</span>
    <span class="scan-tag" style="background:{scan_bg};color:{scan_tx};border-color:{scan_tx}44">{t["scan"]}</span>
    <span class="score-pill">{t["score"]}</span>
    <span class="outcome-pill" style="background:{obg};color:{otx}">{olabel}</span>
    <span class="pct {pc_cls}">{pc_sign}{pct_c:.1f}% close</span>
    <span class="pct {ph_cls}" style="margin-left:4px">{ph_sign}{pct_h:.1f}% high</span>
    <a class="clink" href="https://finviz.com/quote.ashx?t={ticker}" target="_blank">Chart ↗</a>
  </div>
  <div class="levels">
    <div class="lv"><div class="lv-l">Entry</div><div class="lv-v">${entry}</div></div>
    <div class="lv"><div class="lv-l">Close</div><div class="lv-v {pc_cls}">${close}</div></div>
    <div class="lv"><div class="lv-l">Day High</div><div class="lv-v {ph_cls}">${high}</div></div>
    <div class="lv"><div class="lv-l">Stop</div><div class="lv-v {stop_cls}">${stop}</div></div>
    <div class="lv"><div class="lv-l">TP1</div><div class="lv-v {lv_cls(tp1)}">${tp1}</div></div>
    <div class="lv"><div class="lv-l">TP2</div><div class="lv-v {lv_cls(tp2)}">${tp2}</div></div>
    <div class="lv"><div class="lv-l">TP3</div><div class="lv-v {lv_cls(tp3)}">${tp3}</div></div>
  </div>
</div>"""

def cum_section_html(label, d):
    total   = d["total"]
    tp1_pct = round(d["tp1"]/total*100) if total else 0
    tp2_pct = round(d["tp2"]/total*100) if total else 0
    tp3_pct = round(d["tp3"]/total*100) if total else 0
    stp_pct = round(d["stopped"]/total*100) if total else 0
    return f"""<div class="cum-section">
  <div class="cum-label">{label}</div>
  <div class="cum-row"><span>Total tracked</span><span>{total}</span></div>
  <div class="cum-row"><span>Hit TP1+</span><span class="cum-val-g">{tp1_pct}%</span></div>
  <div class="cum-row"><span>Hit TP2+</span><span class="cum-val-g">{tp2_pct}%</span></div>
  <div class="cum-row"><span>Hit TP3</span><span class="cum-val-g">{tp3_pct}%</span></div>
  <div class="cum-row"><span>Stopped out</span><span class="cum-val-r">{stp_pct}%</span></div>
</div>"""

def render_html(today, session_results, all_quotes, cum_stats, gen_time):
    today_str = fmt_date(today)

    # Totals across all sessions today
    all_tickers  = [t for s in session_results.values() for t in s]
    total        = len(all_tickers)
    tp1_hits     = len([t for t in all_tickers if t["outcome"] in ("tp1","tp2","tp3")])
    stopped      = len([t for t in all_tickers if t["outcome"] == "stopped"])
    catch_rate   = f"{round(tp1_hits/total*100)}%" if total else "—"

    # Build session blocks
    session_html = ""
    for session_key, label in SESSIONS_ORDER:
        tickers = session_results.get(session_key, [])
        session_html += f'<div class="sec-lbl">{label}</div>'
        if not tickers:
            session_html += '<div class="no-data">No data for this session today</div>'
            continue
        buy_cards = "".join(card_html(t, t["perf"]) for t in tickers if t["tier"] == "buy")
        mon_cards = "".join(card_html(t, t["perf"]) for t in tickers if t["tier"] == "monitor")
        if not buy_cards and not mon_cards:
            session_html += '<div class="no-data">No Buy Watch or Monitor tickers for this session</div>'
        else:
            session_html += f'<div class="cards">{buy_cards}{mon_cards}</div>'

    # Cumulative block
    cum_html = f"""<div class="cumulative">
  <div class="cum-title">Cumulative Performance — {cum_stats["days"]} days tracked</div>
  <div class="cum-grid">
    {cum_section_html("Buy Watch ≥65", cum_stats["buy"])}
    {cum_section_html("Monitor 40–64", cum_stats["monitor"])}
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EOD Results · {today_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,400;0,500;1,400&family=Syne:wght@500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-l">
    <h1>Watchlist · <em>EOD Results</em></h1>
    <div class="sub">End of day outcomes · All sessions · Final closing prices</div>
  </div>
  <div class="hdr-r">
    <span class="pill">{today_str}</span>
    <span class="pill">Generated {gen_time}</span>
  </div>
</div>
<div class="summary">
  <div class="sum-cell"><div class="sum-n c-g">{tp1_hits}</div><div class="sum-l">Hit TP1+</div></div>
  <div class="sum-cell"><div class="sum-n c-r">{stopped}</div><div class="sum-l">Stopped Out</div></div>
  <div class="sum-cell"><div class="sum-n">{total - tp1_hits - stopped}</div><div class="sum-l">Flat / Mixed</div></div>
  <div class="sum-cell"><div class="sum-n c-g">{catch_rate}</div><div class="sum-l">Today Catch Rate</div></div>
</div>
<div class="body">
  {cum_html}
  {session_html}
</div>
<div class="footer">EOD Results · Final closing prices · For scoring system tuning only</div>
</body></html>"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    now_et   = datetime.now(ZoneInfo("America/New_York"))
    today    = now_et.date()
    gen_time = now_et.strftime("%I:%M %p ET")

    print(f"\nEOD Results tracker · {fmt_date(today)} · {gen_time}")

    # Load all today's session JSONs
    today_sessions = load_today_sessions(today)
    if not today_sessions:
        print("  [!] No session data found for today. Run scorer sessions first.")
        sys.exit(1)

    # Collect all unique tickers across sessions
    all_tickers = {}
    for session_key, data in today_sessions.items():
        for t in data["tickers"]:
            all_tickers[t["ticker"]] = t

    print(f"  [+] {len(all_tickers)} unique tickers across {len(today_sessions)} sessions")

    # Fetch final quotes for all tickers
    quotes = fetch_quotes(list(all_tickers.keys()))
    print(f"  [+] Fetched quotes for {len(quotes)} tickers")

    # Calculate outcomes per session using session-specific highs
    session_results = {}
    eod_save = {"date": today.isoformat(), "generated": gen_time, "sessions": {}}

    for session_key, data in today_sessions.items():
        # Fetch session-specific highs for this session's tickers
        tickers_in_session = [t["ticker"] for t in data["tickers"]]
        try:
            s_highs, s_lows = fetch_session_highs(tickers_in_session, session_key, today)
        except Exception as e:
            print(f"  [!] Session highs failed for {session_key}: {e} — falling back to full day")
            s_highs, s_lows = {}, {}

        results = []
        for t in data["tickers"]:
            ticker = t["ticker"]
            quote  = quotes.get(ticker)
            if not quote:
                continue
            s_high = s_highs.get(ticker)
            s_low  = s_lows.get(ticker)
            t_with_session = {**t, "session_key": session_key}
            perf = calc_outcome(t_with_session, quote, session_high=s_high, session_low=s_low)
            if perf:
                entry = {**t, "perf": perf, "outcome": perf["outcome"]}
                results.append(entry)
        session_results[session_key] = results
        eod_save["sessions"][session_key] = [
            {k: v for k, v in r.items() if k != "perf"}
            for r in results
        ]

    # Save EOD results JSON for cumulative tracking
    os.makedirs(DATA_DIR, exist_ok=True)
    eod_path = os.path.join(DATA_DIR, f"{today.isoformat()}_eod_results.json")
    with open(eod_path, "w") as f:
        json.dump(eod_save, f, indent=2)
    print(f"  [+] EOD JSON → {eod_path}")

    # Load cumulative stats
    cum_stats = load_cumulative()

    # Render HTML
    html = render_html(today, session_results, quotes, cum_stats, gen_time)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "eod_results.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"  [+] Results page → {out_path}")
    print(f"\nDone. URL: https://dwilsolutions.github.io/watchlist/eod_results.html\n")

if __name__ == "__main__":
    main()
