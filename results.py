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
    ("earlypremarket", "Early Pre-Market"),
    ("premarket",      "Pre-Market"),
    ("marketopen",     "Market Open"),
    ("midday",         "Midday"),
    ("afterhours",     "After Hours"),
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
    # Maps session key → (window_start, window_end) ET
    # Window is used to find the HIGH within that session period
    # so run-and-dump stocks are correctly classified as runners.
    "earlypremarket": ("04:00", "06:55"),  # Early Pre-Market
    "premarket":      ("04:00", "09:30"),  # Pre-Market (covers full pre-market open)
    "night":          ("04:00", "09:30"),  # Legacy name for premarket
    "marketopen":     ("09:30", "12:30"),  # Market Open
    "midday":         ("12:30", "15:30"),  # Midday
    "afterhours":     ("15:30", "20:00"),  # After Hours
    "powerhour":      ("15:30", "20:00"),  # Legacy name for afterhours
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

# ── Calculate real VWAP via yfinance ──────────────────────────────────────────

def fetch_real_vwap(tickers, session_key, today):
    """Calculate true VWAP from 9:30 AM to session start time for each ticker."""
    import yfinance as yf

    et = ZoneInfo("America/New_York")
    date_str = today.isoformat()

    # VWAP always calculated from market open to session start
    vwap_end = {
        "earlypremarket": None,     # 4 AM — no meaningful VWAP yet
        "premarket":      None,     # 6:55 AM — no meaningful VWAP yet
        "marketopen":     None,     # no meaningful VWAP at open
        "midday":         "12:30",  # from open to midday
        "afterhours":     "15:30",  # from open to AH start
    }

    end_time = vwap_end.get(session_key)
    if not end_time:
        return {}  # no VWAP for earlypremarket/night/premarket sessions

    start_dt = datetime.fromisoformat(f"{date_str}T09:30:00").replace(tzinfo=et)
    end_dt   = datetime.fromisoformat(f"{date_str}T{end_time}:00").replace(tzinfo=et)

    if start_dt >= end_dt:
        return {}

    vwaps = {}
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
        )
        if data.empty:
            return {}

        for ticker in tickers:
            try:
                df = data if len(tickers) == 1 else (
                    data[ticker] if ticker in data.columns.get_level_values(0) else None
                )
                if df is None or df.empty:
                    continue
                typical = (df["High"] + df["Low"] + df["Close"]) / 3
                vwap = (typical * df["Volume"]).sum() / df["Volume"].sum()
                vwaps[ticker] = round(float(vwap), 3)
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] VWAP fetch error: {e}")

    print(f"  [+] Real VWAP calculated for {len(vwaps)} tickers ({session_key})")
    return vwaps

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

# Fallback names for backward compatibility with old naming convention
SESSION_LEGACY_NAMES = {
    "earlypremarket": [],
    "premarket":      ["night"],
    "marketopen":     ["premarket"],
    "midday":         ["midday"],
    "afterhours":     ["powerhour"],
}

def load_today_sessions(today):
    sessions = {}
    if not os.path.exists(DATA_DIR):
        return sessions
    for session_key, label in SESSIONS_ORDER:
        # Try new name first, then legacy names
        candidates = [session_key] + SESSION_LEGACY_NAMES.get(session_key, [])
        loaded = False
        for name in candidates:
            fname = f"{today.isoformat()}_{name}.json"
            fpath = os.path.join(DATA_DIR, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    sessions[session_key] = json.load(f)
                print(f"  [+] Loaded {fname} — {len(sessions[session_key]['tickers'])} tickers")
                loaded = True
                break
        if not loaded:
            print(f"  [-] No data for {session_key} today")
    return sessions

# ── Calculate performance ──────────────────────────────────────────────────────

def calc_outcome(t, quote, session_high=None, session_low=None):
    entry = t.get("entry", 0)
    close = safe(quote.get("Price")) if isinstance(quote, dict) else safe(quote)
    high  = session_high if session_high else safe(quote.get("High"))
    low   = session_low  if session_low  else safe(quote.get("Low"))
    open_ = safe(quote.get("Open")) if isinstance(quote, dict) else 0

    if not close or not entry:
        return None

    # Use entry price as baseline for % move
    baseline  = entry if entry else close
    pct_close = round((close - baseline) / baseline * 100, 1) if baseline else 0
    pct_high  = round((high  - baseline) / baseline * 100, 1) if baseline else 0

    # Outcome is based on session HIGH from entry — not close.
    # This correctly counts run-and-dump: a stock that hit +30% then
    # closed -20% is still a runner (the high was achievable intraday).
    if pct_high >= 50:
        outcome = "monster"    # 50%+ from entry at any point
    elif pct_high >= 20:
        outcome = "big_runner" # 20%+ from entry at any point
    elif pct_high >= 10:
        outcome = "runner"     # 10%+ from entry at any point
    elif pct_high >= 5:
        outcome = "mover"      # 5%+ from entry at any point
    elif pct_high >= 1:
        outcome = "up"         # 1%+ move — small but positive
    else:
        outcome = "flat"       # never moved meaningfully above entry

    # Flag whether it dumped after running — high was much better than close
    dumped = (pct_high >= 10) and (pct_close < pct_high - 15)

    return {
        "close":     round(close, 2),
        "high":      round(high, 2),
        "low":       round(low, 2) if low else 0,
        "pct_close": pct_close,
        "pct_high":  pct_high,
        "outcome":   outcome,
        "dumped":    dumped,
    }

# ── Cumulative stats ───────────────────────────────────────────────────────────

def load_cumulative():
    stats = {
        "hot":   {"total": 0, "runner": 0, "big_runner": 0, "monster": 0, "dumped": 0},
        "warm":  {"total": 0, "runner": 0, "big_runner": 0, "monster": 0, "dumped": 0},
        "watch": {"total": 0, "runner": 0, "big_runner": 0, "monster": 0, "dumped": 0},
        "days":  0,
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
                rvol = t.get("rvol", 0) or 0
                if rvol >= 100:
                    tier = "hot"
                elif rvol >= 10:
                    tier = "warm"
                else:
                    tier = "watch"
                if tier not in stats:
                    tier = "watch"
                stats[tier]["total"] += 1
                if outcome in ("runner", "big_runner", "monster"):
                    stats[tier]["runner"] += 1
                if outcome in ("big_runner", "monster"):
                    stats[tier]["big_runner"] += 1
                if outcome == "monster":
                    stats[tier]["monster"] += 1
                if t.get("dumped"):
                    stats[tier]["dumped"] = stats[tier].get("dumped", 0) + 1
    return stats

# ── HTML ───────────────────────────────────────────────────────────────────────

OUTCOME_CFG = {
    "monster":    ("🎯 Monster 50%+", "#1e3d2a", "#6ee89a"),
    "big_runner": ("🔥 Big Run 20%+", "#1e3d2a", "#6ee89a"),
    "runner":     ("✅ Runner 10%+",  "#1a3320", "#5cc98a"),
    "mover":      ("📈 Mover 5%+",    "#1a2a3d", "#7ab4f5"),
    "up":         ("↑ Up",            "#1c1f23", "#656c7a"),
    "flat":       ("➖ Flat",          "#1c1f23", "#656c7a"),
}

SCAN_COLORS = {
    "Low Float": ("#1a2a3d", "#7ab4f5"),
    "Mid Cap":   ("#2a1e3d", "#b07af5"),
}

CSS = """
:root{--bg:#0c0e11;--bg2:#141618;--bg3:#1c1f23;--border:rgba(255,255,255,0.07);--text:#dde1e9;--muted:#656c7a;--green:#3a9c5f;--amber:#c07b1a;--red:#a33333;--mono:'DM Mono',monospace;--sans:'Inter',sans-serif;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html,body{min-height:100vh;}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.5;display:flex;flex-direction:column;}
a{color:inherit;text-decoration:none;}
.hdr{background:var(--bg2);border-bottom:1px solid var(--border);padding:16px 20px;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.hdr-l h1{font-family:var(--sans);font-size:20px;font-weight:700;letter-spacing:0;line-height:normal;}
.hdr-l h1 em{color:var(--green);font-style:normal;}
.hdr-l .sub{font-size:11px;color:var(--muted);margin-top:3px;}
.hdr-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.pill{font-size:10px;padding:3px 9px;border-radius:20px;background:var(--bg3);color:var(--muted);border:1px solid var(--border);}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border-bottom:1px solid var(--border);}
.sum-cell{background:var(--bg2);padding:12px 16px;text-align:center;}
.sum-n{font-family:var(--sans);font-size:26px;font-weight:700;}
.sum-l{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:0.06em;text-transform:uppercase;}
.c-g{color:var(--green);}.c-a{color:var(--amber);}.c-r{color:var(--red);}
.body{padding:18px 20px 48px;}
.cum-wrap{padding:16px 20px;}
.cumulative{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:0;}
.cum-title{font-family:var(--sans);font-size:14px;font-weight:500;margin-bottom:12px;}
.cum-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}
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

.layout{display:flex;min-height:calc(100vh - 200px);}
.sidenav{width:168px;flex-shrink:0;background:var(--bg2);
  border-right:1px solid var(--border);padding:16px 0;
  position:sticky;top:0;align-self:flex-start;
  height:calc(100vh - 200px);display:flex;flex-direction:column;}
.sidenav-label{font-size:9px;color:#2a3a52;letter-spacing:0.14em;
  text-transform:uppercase;padding:10px 16px 4px;}
.nav-item{display:flex;align-items:center;gap:10px;padding:10px 16px;
  cursor:pointer;border-left:2px solid transparent;color:var(--muted);
  transition:all .15s;user-select:none;}
.nav-item:hover{color:var(--text);background:rgba(255,255,255,0.03);}
.nav-item.active{color:var(--green);border-left-color:var(--green);
  background:rgba(58,156,95,0.06);}
.nav-icon{font-size:13px;width:18px;text-align:center;flex-shrink:0;}
.nav-label{font-size:12px;flex:1;}
.nav-cnt{font-size:10px;padding:1px 6px;border-radius:20px;
  background:rgba(255,255,255,0.06);color:var(--muted);}
.nav-item.active .nav-cnt{background:rgba(58,156,95,0.15);color:var(--green);}
.tab-content{display:none;flex:1;min-width:0;}
.tab-content.active{display:block;}
.empty{color:var(--muted);font-size:12px;padding:8px 0;}
.no-data{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:16px;color:var(--muted);font-size:12px;text-align:center;}
.footer{text-align:center;font-size:10px;color:var(--muted);padding:24px;border-top:1px solid var(--border);}
"""

def card_html(t, perf):
    tier    = t["tier"]
    ticker  = t["ticker"]
    outcome = perf["outcome"]
    entry   = t.get("entry", 0)
    high    = perf["high"]
    close   = perf["close"]
    pct_h   = perf["pct_high"]
    pct_c   = perf["pct_close"]

    dumped = perf.get("dumped", False)
    if dumped:
        outcome_display = outcome + "_dumped"
    else:
        outcome_display = outcome
    olabel, obg, otx = OUTCOME_CFG.get(outcome, ("—", "#1c1f23", "#656c7a"))
    if dumped:
        olabel = olabel + " · dumped"
    scan_bg, scan_tx = SCAN_COLORS.get(t["scan"], ("#1c1f23", "#656c7a"))
    pc_cls  = "pos" if pct_c >= 0 else "neg"
    pc_sign = "+" if pct_c >= 0 else ""
    ph_cls  = "pos" if pct_h >= 0 else "neg"
    ph_sign = "+" if pct_h >= 0 else ""

    real_vwap    = perf.get("real_vwap")
    above_vwap   = perf.get("above_vwap")
    vwap_str     = f"VWAP ${real_vwap:.3f}" if real_vwap else "VWAP —"
    vwap_color   = "#6ee89a" if above_vwap else "#f57a7a" if above_vwap is False else "#656c7a"
    vwap_label   = f"✓ {vwap_str}" if above_vwap else f"✗ {vwap_str}" if above_vwap is False else vwap_str

    return f"""<div class="card {tier}">
  <div class="r1">
    <span class="tkr">{ticker}</span>
    <span class="co">{t["company"][:20]}</span>
    <span class="scan-tag" style="background:{scan_bg};color:{scan_tx};border-color:{scan_tx}44">{t["scan"]}</span>
    <span class="score-pill">{t["score"]}</span>
    <span class="outcome-pill" style="background:{obg};color:{otx}">{olabel}</span>
    <span class="pct {pc_cls}">{pc_sign}{pct_c:.1f}% close</span>
    <span class="pct {ph_cls}" style="margin-left:4px">{ph_sign}{pct_h:.1f}% high</span>
    <span style="font-size:11px;color:{vwap_color};margin-left:4px">{vwap_label}</span>
    <a class="clink" href="https://finviz.com/quote.ashx?t={ticker}" target="_blank">Chart ↗</a>
  </div>
  <div class="levels">
    <div class="lv"><div class="lv-l">Entry</div><div class="lv-v">${t.get("entry","—")}</div></div>
    <div class="lv"><div class="lv-l">Close</div><div class="lv-v {pc_cls}">${close}</div></div>
    <div class="lv"><div class="lv-l">Session High</div><div class="lv-v {ph_cls}">${high} ({ph_sign}{pct_h:.1f}%)</div></div>
    <div class="lv"><div class="lv-l">Prev High</div><div class="lv-v">${t.get("prev_high","—")}</div></div>
    <div class="lv"><div class="lv-l">52W High</div><div class="lv-v">${t.get("hi52_price","—")}</div></div>
  </div>
</div>"""

def cum_section_html(label, d):
    total      = d["total"]
    run_pct    = round(d["runner"]/total*100) if total else 0
    big_pct    = round(d["big_runner"]/total*100) if total else 0
    mon_pct    = round(d["monster"]/total*100) if total else 0
    dump_pct   = round(d.get("dumped",0)/total*100) if total else 0
    dump_row   = f'<div class="cum-row"><span>Run &amp; Dump</span><span class="cum-val-g">{dump_pct}%</span></div>' if dump_pct else ""
    return f"""<div class="cum-section">
  <div class="cum-label">{label}</div>
  <div class="cum-row"><span>Total tracked</span><span>{total}</span></div>
  <div class="cum-row"><span>Runners 10%+</span><span class="cum-val-g">{run_pct}%</span></div>
  <div class="cum-row"><span>Big Runners 20%+</span><span class="cum-val-g">{big_pct}%</span></div>
  <div class="cum-row"><span>Monsters 50%+</span><span class="cum-val-g">{mon_pct}%</span></div>
  {dump_row}
</div>"""

def render_html(today, session_results, all_quotes, cum_stats, gen_time):
    today_str = fmt_date(today)

    # Totals across all sessions today
    all_tickers  = [t for s in session_results.values() for t in s]
    total        = len(all_tickers)
    runners      = len([t for t in all_tickers if t["outcome"] in ("runner","big_runner","monster")])
    big_runners  = len([t for t in all_tickers if t["outcome"] in ("big_runner","monster")])
    monsters     = len([t for t in all_tickers if t["outcome"] == "monster"])
    catch_rate   = f"{round(runners/total*100)}%" if total else "—"
    stopped      = 0  # no longer tracked

    # Build session blocks
    # Build nav items and tab content for each session
    nav_items_html = ""
    tabs_html = ""
    SESSION_ICONS = {
        "earlypremarket": "🌙",
        "premarket":      "🌅",
        "marketopen":     "🔔",
        "midday":         "☀️",
        "afterhours":     "🌆",
    }
    for idx, (session_key, label) in enumerate(SESSIONS_ORDER):
        tickers = session_results.get(session_key, [])
        count   = len(tickers)
        sess_runners = [t for t in tickers if t.get("outcome") in ("runner","big_runner","monster")]
        runner_txt = f"{len(sess_runners)} runner{'s' if len(sess_runners)!=1 else ''}" if sess_runners else "no runners"
        active_cls = " active" if idx == 0 else ""
        icon = SESSION_ICONS.get(session_key, "📋")

        nav_items_html += f'''<div class="nav-item{active_cls}" data-tab="sess-{session_key}">
  <span class="nav-icon">{icon}</span>
  <span class="nav-label">{label}</span>
  <span class="nav-cnt">{count}</span>
</div>'''

        if not tickers:
            body_inner = '<div class="no-data">No data for this session today</div>'
        else:
            all_cards = "".join(card_html(t, t["perf"]) for t in tickers)
            body_inner = f'<div class="sec-lbl">{label} · {runner_txt}</div><div class="cards">{all_cards}</div>' if all_cards else '<div class="no-data">No tickers tracked</div>'

        tabs_html += f'''<div class="tab-content{active_cls}" id="sess-{session_key}">
  <div class="body" style="padding:14px 20px 48px;">{body_inner}</div>
</div>'''

    session_html = f'''<div class="layout">
  <div class="sidenav">
    <div class="sidenav-label">Sessions</div>
    {nav_items_html}
  </div>
  {tabs_html}
</div>'''
    js_block = """<script>
document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    item.classList.add('active');
    document.getElementById(item.dataset.tab).classList.add('active');
    window.scrollTo(0, 0);
  });
});
</script>"""

    # Cumulative block — labels pre-built outside f-string (backslash in f-expr is a SyntaxError)
    _lbl_hot   = "🔥 HOT (rvol ≥100x)"
    _lbl_warm  = "⚡ WARM (rvol ≥10x)"
    _lbl_watch = "👁 WATCH"
    _cum_days  = cum_stats["days"]
    _sec_hot   = cum_section_html(_lbl_hot,   cum_stats["hot"])
    _sec_warm  = cum_section_html(_lbl_warm,  cum_stats["warm"])
    _sec_watch = cum_section_html(_lbl_watch, cum_stats["watch"])
    cum_html = (
        '<div class="cum-wrap"><div class="cumulative">\n'
        '  <div class="cum-title">Cumulative Performance — ' + str(_cum_days) + ' days tracked</div>\n'
        '  <div class="cum-grid">\n'
        '    ' + _sec_hot + '\n'
        '    ' + _sec_warm + '\n'
        '    ' + _sec_watch + '\n'
        '  </div>\n'
        '</div></div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>EOD Results · {today_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,400;0,500;1,400&family=Inter:wght@600;700;800&display=swap" rel="stylesheet">
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
  <div class="sum-cell"><div class="sum-n c-g">{monsters}</div><div class="sum-l">Monsters 50%+</div></div>
  <div class="sum-cell"><div class="sum-n c-g">{big_runners}</div><div class="sum-l">Big Runners 20%+</div></div>
  <div class="sum-cell"><div class="sum-n c-g">{runners}</div><div class="sum-l">Runners 10%+</div></div>
  <div class="sum-cell"><div class="sum-n c-g">{catch_rate}</div><div class="sum-l">Today Catch Rate</div></div>
</div>
{cum_html}
{session_html}
{js_block}
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

        # Fetch real VWAP for this session
        try:
            vwaps = fetch_real_vwap(tickers_in_session, session_key, today)
        except Exception as e:
            print(f"  [!] VWAP failed for {session_key}: {e}")
            vwaps = {}

        results = []
        for t in data["tickers"]:
            ticker = t["ticker"]
            quote  = quotes.get(ticker)
            if not quote:
                continue
            s_high = s_highs.get(ticker)
            s_low  = s_lows.get(ticker)
            real_vwap = vwaps.get(ticker)
            entry_price = t.get("entry", 0)
            above_vwap = (entry_price >= real_vwap) if real_vwap else None
            t_with_session = {**t, "session_key": session_key}
            perf = calc_outcome(t_with_session, quote, session_high=s_high, session_low=s_low)
            if perf:
                perf["real_vwap"] = real_vwap
                perf["above_vwap"] = above_vwap
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
