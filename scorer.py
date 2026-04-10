"""
Watchlist Scorer
Pulls both Finviz Elite screeners, scores every ticker, renders a
self-contained HTML dashboard, and writes it to docs/ for GitHub Pages.

Session names and schedule:
  night      → "Pre-Market"   runs ~9 PM ET night before; date = next trading day
  premarket  → "Market Open"  runs ~8:30 AM ET; date = today
  midday     → "Midday"       runs ~12:30 PM ET; date = today
  powerhour  → "After Hours"  runs ~3:30 PM ET; date = today

Environment variables:
  FINVIZ_TOKEN — Finviz Elite API token (store as GitHub Secret)
"""

import os, sys, math, argparse, csv, io
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import urllib.request, urllib.error

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("FINVIZ_TOKEN", "")

SCREENERS = [
    ("Low Float", "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136&f=cap_smallunder,sh_curvol_o5000,sh_float_u20,sh_price_u10,sh_relvol_o2"),
    ("Mid Cap",   "v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,52,53,54,57,58,42,43,44,60,87,88,86,81,30,68,137,136&f=cap_smallunder,sh_curvol_o5000,sh_float_20to100x,sh_price_u20,sh_relvol_o3"),
]

# US market holidays 2026
MARKET_HOLIDAYS = {
    date(2026, 1, 1),  date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3),  date(2026, 5, 25), date(2026, 7, 3),
    date(2026, 9, 7),  date(2026, 11, 26),date(2026, 11, 27),
    date(2026, 12, 25),
}

SESSIONS = {
    "earlypremarket": ("Early Pre-Market", "First look · overnight gappers · ~4:00 AM ET"),
    "premarket":      ("Pre-Market",       "Confirmed setups · pre-Robinhood open · ~6:55 AM ET"),
    "marketopen":     ("Market Open",      "Pre-market momentum &amp; gap ups · ~8:30 AM ET"),
    "midday":         ("Midday",           "VWAP reclaims &amp; second entries · ~12:30 PM ET"),
    "afterhours":     ("After Hours",      "Power hour seeds &amp; HOD breakouts · ~3:30 PM ET"),
}

OUTPUT_DIR = "docs"

# ── Trading day logic ──────────────────────────────────────────────────────────

def is_trading_day(d):
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS

def next_trading_day(from_date):
    d = from_date + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d

def trading_date_for_session(session, now_et):
    today = now_et.date()
    if session in ("premarket", "earlypremarket"):
        # Night scan is always for the NEXT trading day
        return next_trading_day(today)
    else:
        # Same-day sessions: use today if it's a trading day
        return today if is_trading_day(today) else next_trading_day(today)

def fmt_trading_date(d):
    return d.strftime("%a %b %-d, %Y")   # e.g. "Mon Apr 6, 2026"

# ── Helpers ────────────────────────────────────────────────────────────────────

def pct(s):
    try:
        return float(str(s).replace("%", "").strip())
    except Exception:
        return float("nan")

def safe(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except Exception:
        return default

def fetch_csv(label, filters):
    if not TOKEN:
        raise RuntimeError("FINVIZ_TOKEN not set.")
    url = f"https://elite.finviz.com/export.ashx?{filters}&auth={TOKEN}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://elite.finviz.com/screener.ashx",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8-sig")
    except urllib.error.HTTPError as e:
        print(f"  [!] HTTP {e.code} fetching {label}", file=sys.stderr)
        return []
    rows = list(csv.DictReader(io.StringIO(raw)))
    for r in rows:
        r["_scan_label"] = label
    print(f"  [+] {label}: {len(rows)} tickers")
    if rows:
        cols = list(rows[0].keys())
        has_gap = "Gap" in cols
        has_rvol = "Relative Volume" in cols
        has_sma = "20-Day Simple Moving Average" in cols
        print(f"  [+] Columns: {len(cols)} total | Gap={has_gap} | RVol={has_rvol} | SMA20={has_sma}")
        print(f"  [+] Column names: {cols}")
        print(f"  [+] First row sample: {dict(list(rows[0].items())[:10])}")
    return rows

# ── Scoring ────────────────────────────────────────────────────────────────────

def is_market_live(rows):
    """Returns False when key intraday fields are missing — e.g. weekend/closed."""
    if not rows: return False
    # Check Gap and Relative Volume fields are populated
    gaps  = [r.get("Gap", "") for r in rows[:5]]
    rvols = [r.get("Relative Volume", "") for r in rows[:5]]
    gap_ok  = any(str(g).strip() not in ("", "0", "0.00%", "0%") for g in gaps)
    rvol_ok = any(str(v).strip() not in ("", "0", "0.00") for v in rvols)
    return gap_ok or rvol_ok

def calc_stop(price, atr, entry):
    """ATR-based stop: 1.5x ATR with 6% floor and 10% ceiling."""
    if atr and atr > 0:
        atr_stop  = round(entry - (atr * 1.5), 2)
        ceiling   = round(entry * 0.90, 2)  # never more than 10% below entry
        floor     = round(entry * 0.94, 2)  # never tighter than 6% below entry
        return max(ceiling, min(floor, atr_stop))
    return round(entry * 0.94, 2)  # fallback: 6% fixed

def score_row(row):
    price        = safe(row.get("Price"))
    change       = pct(row.get("Change"))
    gap_raw      = row.get("Gap", "")
    gap          = pct(gap_raw)
    rvol         = safe(row.get("Relative Volume"))
    rsi          = safe(row.get("Relative Strength Index (14)"))
    float_m      = safe(row.get("Shares Float"))
    sector       = str(row.get("Sector", ""))
    sma20        = pct(row.get("20-Day Simple Moving Average"))
    sma50        = pct(row.get("50-Day Simple Moving Average"))
    sma200       = pct(row.get("200-Day Simple Moving Average"))
    hi52         = pct(row.get("52-Week High"))
    lo52         = pct(row.get("52-Week Low"))
    perf_week    = pct(row.get("Performance (Week)"))
    perf_month   = pct(row.get("Performance (Month)"))
    perf_quarter = pct(row.get("Performance (Quarter)"))
    chg_open     = pct(row.get("Change from Open"))
    news         = str(row.get("News Title", ""))
    h = safe(row.get("High")); l = safe(row.get("Low")); o = safe(row.get("Open"))
    vwap_proxy   = (h + l + o) / 3 if (h and l and o) else price
    real_vwap    = safe(row.get("_real_vwap"))
    above_vwap   = (price >= real_vwap) if real_vwap else (price >= vwap_proxy)
    vwap_ref     = real_vwap if real_vwap else vwap_proxy

    import math as _math
    gap_missing     = _math.isnan(gap)
    rvol_missing    = rvol == 0
    chgopen_missing = _math.isnan(chg_open)

    # Trend 30pts
    trend = 0
    if sma20  > 0: trend += 8
    if sma50  > 0: trend += 7
    if sma200 > 0: trend += 7
    if not _math.isnan(perf_week)  and perf_week  > 0: trend += 4
    if not _math.isnan(perf_month) and perf_month > 0: trend += 4
    trend = min(trend, 30)

    # Range 45pts
    room_to_high = abs(hi52) if hi52 < 0 else 0
    range_score  = 0
    if   room_to_high <= 30: range_score += 30
    elif room_to_high <= 50: range_score += 22
    elif room_to_high <= 70: range_score += 14
    else:                    range_score += 6
    if   lo52 > 50: range_score += 10
    elif lo52 > 20: range_score += 5
    if not _math.isnan(perf_quarter) and perf_quarter > 0: range_score += 5
    range_score = min(range_score, 45)

    # Volume 25pts — skip if intraday data unavailable
    vol_score = 0
    if not rvol_missing:
        if   rvol >= 50: vol_score += 12
        elif rvol >= 10: vol_score += 9
        elif rvol >= 3:  vol_score += 6
        elif rvol >= 2:  vol_score += 3
    gap_quality = (not gap_missing) and gap > 10 and (not rvol_missing) and rvol >= 2 and (not chgopen_missing) and chg_open > -5
    if gap_quality:
        vol_score += 7
    elif (not gap_missing) and gap > 5 and (not rvol_missing) and rvol >= 2:
        vol_score += 4
    if   50 <= rsi <= 75:                      vol_score += 6
    elif (40 <= rsi < 50) or (75 < rsi <= 85): vol_score += 3
    vol_score = min(vol_score, 25)

    base  = trend + range_score + vol_score
    bonus = 0
    flags = []

    continuation = (not _math.isnan(perf_week)) and perf_week > 30 and (not chgopen_missing) and chg_open > -10
    if continuation:
        flags.append(("CONTINUATION", "cont")); bonus += 5

    if gap_quality:
        flags.append(("GAP QUALITY", "gap"))

    news_lo = news.lower()
    if "fda" in news_lo or "fast track" in news_lo or "approval" in news_lo:
        flags.append(("CATALYST · FDA", "catalyst")); bonus += 3
    elif "8-k" in news_lo or "earnings" in news_lo:
        flags.append(("CATALYST · 8-K", "catalyst")); bonus += 3

    if "reverse" in news_lo and "split" in news_lo:
        flags.append(("REVERSE SPLIT", "danger")); bonus -= 15

    if not gap_missing:
        if gap < -20:
            flags.append(("CRASHED · GAP DOWN", "danger")); bonus -= 20
        elif gap < -10:
            bonus -= 10

    # Real VWAP signal (midday + powerhour) or Change from Open (night + premarket)
    if real_vwap:
        if above_vwap:
            bonus += 10  # above real VWAP — strong
        else:
            bonus -= 10  # below real VWAP — weak
    elif not chgopen_missing:
        if chg_open > 0:
            bonus += 5   # holding above open — strength proxy
        elif chg_open < -5:
            bonus -= 5   # fading hard from open — weakness proxy

    total = max(0, min(100, base + bonus))
    entry = round(vwap_ref * 1.005, 2) if not above_vwap else round(price * 1.005, 2)

    return {
        "ticker":      row.get("Ticker", ""),
        "company":     row.get("Company", ""),
        "sector":      sector,
        "scan":        row.get("_scan_label", ""),
        "price":       price,   "change":  change,
        "gap":         gap,     "rvol":    rvol,
        "rsi":         rsi,     "float_m": float_m,
        "trend":       trend,   "range_score": range_score, "vol_score": vol_score,
        "bonus":       bonus,   "total":   total,
        "flags":       flags,   "above_vwap": above_vwap,
        "vwap_proxy":  round(vwap_proxy, 3),
        "real_vwap":   round(real_vwap, 3) if real_vwap else None,
        "entry":       entry,
        "stop":        calc_stop(price, safe(row.get("Average True Range")), entry),
        "tp1":         round(price * 1.06, 2),   # 6% — tighter first target
        "tp2":         round(price * 1.12, 2),   # 12% — second target
        "tp3":         round(price * 1.20, 2),   # 20% — runner target
        "news":        news[:120],
        "perf_week":   perf_week, "perf_month": perf_month,
        "continuation": continuation, "gap_quality": gap_quality,
    }

def apply_sector_bonus(results):
    counts = {}
    for r in results:
        counts[r["sector"]] = counts.get(r["sector"], 0) + 1
    for r in results:
        if counts.get(r["sector"], 0) >= 2 and r["total"] >= 30:
            r["bonus"] += 8
            r["total"]  = min(100, r["total"] + 8)
            if not any(l == "SECTOR MOMENTUM" for l, _ in r["flags"]):
                r["flags"].append(("SECTOR MOMENTUM", "sector"))
    return results

# ── HTML ───────────────────────────────────────────────────────────────────────

FLAG_CFG = {
    "cont":     ("#1e3d2a", "#6ee89a"),
    "gap":      ("#1a2a3d", "#7ab4f5"),
    "sector":   ("#2a1e3d", "#b07af5"),
    "catalyst": ("#3d2e1a", "#f5c46e"),
    "danger":   ("#3d1a1a", "#f57a7a"),
    "vwap":     ("#3d3010", "#f5d96e"),
}

SCAN_COLORS = {
    "Low Float": ("#1a2a3d", "#7ab4f5"),
    "Mid Cap":   ("#2a1e3d", "#b07af5"),
}

def flag_html(label, kind):
    bg, tx = FLAG_CFG.get(kind, ("#222", "#aaa"))
    return f'<span class="flag" style="background:{bg};color:{tx}">{label}</span>'

def bar_html(label, val, mx, color):
    w = min(100, round(val / mx * 100))
    return (
        f'<div class="bar-row"><span class="bl">{label}</span>'
        f'<div class="bt"><div class="bf" style="width:{w}%;background:{color}"></div></div>'
        f'<span class="bv">{val}/{mx}</span></div>'
    )

def card_html(r):
    score    = r["total"]
    tier     = "buy" if score >= 65 else ("monitor" if score >= 40 else "avoid")
    scls     = "sbuy" if score >= 65 else ("smon" if score >= 40 else "savoid")
    t        = r["ticker"]
    gap_sign = "+" if r["gap"] >= 0 else ""
    chg_sign = "+" if r["change"] >= 0 else ""
    chg_cls  = "pos" if r["change"] >= 0 else "neg"
    scan_bg, scan_tx = SCAN_COLORS.get(r["scan"], ("#1c1f23", "#656c7a"))
    flags_out = "".join(flag_html(l, k) for l, k in r["flags"])
    entry_str = f'${r["entry"]}'

    return f"""<div class="card {tier}">
  <div class="r1">
    <span class="tkr">{t}</span>
    <span class="co">{r["company"][:24]}</span>
    <span class="scan-tag" style="background:{scan_bg};color:{scan_tx};border-color:{scan_tx}44">{r["scan"]}</span>
    <span class="score {scls}">{score}</span>
    <span class="ch {chg_cls}">{chg_sign}{r["change"]:.1f}%</span>
    <a class="clink" href="https://finviz.com/quote.ashx?t={t}" target="_blank">Chart ↗</a>
  </div>
  <div class="flags">{flags_out}</div>
  {bar_html("Trend",  r["trend"],       30, "#3a9c5f")}
  {bar_html("Range",  r["range_score"], 45, "#3266ad")}
  {bar_html("Volume", r["vol_score"],   25, "#c07b1a")}
  <div class="stats">
    <div class="st"><div class="sl">Price</div><div class="sv">${r["price"]:.2f}</div></div>
    <div class="st"><div class="sl">Gap</div><div class="sv">{gap_sign}{r["gap"]:.1f}%</div></div>
    <div class="st"><div class="sl">RVol</div><div class="sv">{r["rvol"]:.0f}x</div></div>
    <div class="st"><div class="sl">RSI</div><div class="sv">{r["rsi"]:.1f}</div></div>
    <div class="st"><div class="sl">Float</div><div class="sv">{r["float_m"]:.1f}M</div></div>
    <div class="st"><div class="sl">{"VWAP" if r.get("real_vwap") else "VWAP~"}</div><div class="sv" style="color:{"#6ee89a" if r["above_vwap"] else "#f57a7a"}">${r.get("real_vwap") or r["vwap_proxy"]}</div></div>
  </div>
  <div class="lvls">
    <span><span class="ll">Entry</span> <span class="lv">{entry_str}</span></span>
    <span class="sep">·</span><span><span class="ll">Stop</span> <span class="lv stop">${r["stop"]}</span></span>
    <span class="sep">·</span><span><span class="ll">TP1</span> <span class="lv">${r["tp1"]}</span></span>
    <span class="sep">·</span><span><span class="ll">TP2</span> <span class="lv">${r["tp2"]}</span></span>
    <span class="sep">·</span><span><span class="ll">TP3</span> <span class="lv">${r["tp3"]}</span></span>
  </div>
  <div class="news-line">{r["news"]}</div>
</div>"""

def chip_html(r):
    reasons  = ", ".join(l for l, _ in r["flags"]) or "weak setup"
    scan_bg, scan_tx = SCAN_COLORS.get(r["scan"], ("#1c1f23", "#656c7a"))
    return (
        f'<div class="chip">'
        f'<div class="chip-top"><span class="chip-tkr">{r["ticker"]}</span>'
        f'<span class="chip-scan" style="background:{scan_bg};color:{scan_tx}">{r["scan"]}</span></div>'
        f'<span class="chip-score">{r["total"]}</span>'
        f'<span class="chip-why">{reasons[:50]}</span></div>'
    )

CSS = """
:root{{--bg:#0c0e11;--bg2:#141618;--bg3:#1c1f23;--border:rgba(255,255,255,0.07);--text:#dde1e9;--muted:#656c7a;--green:#3a9c5f;--amber:#c07b1a;--red:#a33333;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif;--session-color:{session_color};}}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.5;}
a{color:inherit;text-decoration:none;}
.hdr{background:var(--bg2);border-bottom:2px solid var(--session-color);padding:16px 20px;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.hdr-l h1{font-family:var(--sans);font-size:20px;font-weight:700;letter-spacing:-0.3px;}
.hdr-l h1 em{color:var(--green);font-style:normal;}
.hdr-l .sub{font-size:11px;color:var(--muted);margin-top:3px;}
.hdr-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.pill{font-size:10px;padding:3px 9px;border-radius:20px;background:var(--bg3);color:var(--muted);border:1px solid var(--border);}
.legend{display:flex;gap:14px;padding:8px 20px;background:var(--bg2);border-bottom:1px solid var(--border);flex-wrap:wrap;align-items:center;}
.leg-label{font-size:10px;color:var(--muted);margin-right:4px;}
.leg-item{display:flex;align-items:center;gap:5px;font-size:10px;}
.leg-dot{width:8px;height:8px;border-radius:2px;}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;background:var(--border);border-bottom:1px solid var(--border);}
.sum-cell{background:var(--bg2);padding:12px 16px;text-align:center;}
.sum-n{font-family:var(--sans);font-size:26px;font-weight:700;}
.sum-l{font-size:10px;color:var(--muted);margin-top:2px;letter-spacing:0.06em;text-transform:uppercase;}
.c-g{color:var(--green);}.c-a{color:var(--amber);}.c-r{color:var(--red);}
.body{padding:18px 20px 48px;max-width:940px;margin:0 auto;}
.sec-lbl{font-size:10px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin:22px 0 9px;display:flex;align-items:center;gap:8px;}
.sec-lbl::after{content:'';flex:1;height:1px;background:var(--border);}
.cards{display:flex;flex-direction:column;gap:9px;}
.empty{color:var(--muted);font-size:12px;padding:8px 0;}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:13px 15px;border-left-width:3px;}
.card.buy{border-left-color:var(--green);}.card.monitor{border-left-color:var(--amber);}.card.avoid{border-left-color:var(--red);opacity:0.6;}
.r1{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin-bottom:9px;}
.tkr{font-family:var(--sans);font-size:16px;font-weight:700;min-width:46px;}
.co{font-size:11px;color:var(--muted);flex:1;min-width:60px;}
.scan-tag{font-size:10px;padding:2px 7px;border-radius:10px;border:1px solid;white-space:nowrap;}
.score{font-size:13px;font-weight:500;padding:2px 9px;border-radius:20px;}
.sbuy{background:#1e3d2a;color:#6ee89a;}.smon{background:#3d2e1a;color:#f5c46e;}.savoid{background:#3d1a1a;color:#f57a7a;}
.ch{font-size:12px;font-weight:500;}.pos{color:#5cc98a;}.neg{color:#e06060;}
.clink{font-size:11px;color:#5a8fd4;margin-left:auto;white-space:nowrap;}
.clink:hover{color:#7aaef5;}
.flags{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:9px;}
.flag{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500;white-space:nowrap;}
.bar-row{display:flex;align-items:center;gap:6px;margin-bottom:4px;}
.bl{font-size:10px;color:var(--muted);width:44px;text-align:right;flex-shrink:0;}
.bt{flex:1;height:5px;background:var(--bg3);border-radius:3px;overflow:hidden;}
.bf{height:100%;border-radius:3px;}
.bv{font-size:10px;color:var(--muted);width:30px;}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(76px,1fr));gap:5px;margin:9px 0;}
.st{background:var(--bg3);border-radius:6px;padding:5px 8px;}
.sl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}
.sv{font-size:12px;font-weight:500;color:var(--text);margin-top:1px;}
.lvls{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:7px;font-size:11px;}
.ll{color:var(--muted);}.lv{color:var(--text);font-weight:500;}.lv.stop{color:#e06060;}.sep{color:var(--border);}
.news-line{font-size:10px;color:var(--muted);font-style:italic;border-top:1px solid var(--border);padding-top:6px;margin-top:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.chips{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;}
.chip{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:8px 10px;display:flex;flex-direction:column;gap:2px;opacity:0.65;}
.chip-top{display:flex;align-items:center;gap:6px;margin-bottom:2px;}
.chip-tkr{font-family:var(--sans);font-size:13px;font-weight:700;}
.chip-scan{font-size:9px;padding:1px 5px;border-radius:8px;}
.chip-score{font-size:10px;color:var(--red);}
.chip-why{font-size:10px;color:var(--muted);}
.footer{text-align:center;font-size:10px;color:var(--muted);padding:24px;border-top:1px solid var(--border);}
.banner-closed{background:#3d2e1a;color:#f5c46e;font-size:11px;padding:10px 20px;text-align:center;border-bottom:1px solid #5a4010;}
"""

SESSION_COLORS = {
    "earlypremarket": "#1a4a8a",   # deep blue — night sky
    "premarket":      "#6b3fa0",   # purple — dawn
    "marketopen":     "#c07b1a",   # amber — sunrise
    "midday":         "#3a9c5f",   # green — midday
    "afterhours":     "#a33333",   # crimson — closing bell
}

def render_html(results, session, trading_date, label, note, gen_time_str, market_live=True):
    session_color = SESSION_COLORS.get(session, "#3a9c5f")
    buy     = [r for r in results if r["total"] >= 65]
    monitor = [r for r in results if 40 <= r["total"] < 65]
    avoid   = [r for r in results if r["total"] < 40]
    n_sec   = len({r["sector"] for r in results})
    total_n = len(results)
    lf_n    = len([r for r in results if r["scan"] == "Low Float"])
    mc_n    = len([r for r in results if r["scan"] == "Mid Cap"])
    td_str  = fmt_trading_date(trading_date)

    buy_out     = "".join(card_html(r) for r in buy)     or '<p class="empty">No setups reached Buy Watch threshold.</p>'
    monitor_out = "".join(card_html(r) for r in monitor) or '<p class="empty">No setups in Monitor range.</p>'
    avoid_out   = "".join(chip_html(r) for r in avoid)

    css = CSS.replace("{session_color}", session_color)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} · {td_str}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,400;0,500;1,400&family=Syne:wght@500;700&display=swap" rel="stylesheet">
<style>{css}</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-l">
    <h1>Watchlist · <em style="color:var(--session-color)">{label}</em></h1>
    <div class="sub">{note} · {total_n} tickers scored ({lf_n} low float · {mc_n} mid cap)</div>
  </div>
  <div class="hdr-r">
    <span class="pill">{td_str}</span>
    <span class="pill">Generated {gen_time_str}</span>
    <span class="pill">{n_sec} sectors</span>
  </div>
</div>
<div class="legend">
  <span class="leg-label">Screener:</span>
  <span class="leg-item"><span class="leg-dot" style="background:#7ab4f5"></span>Low Float (&lt;$10 · &lt;20M float · RVol 2x+)</span>
  <span class="leg-item"><span class="leg-dot" style="background:#b07af5"></span>Mid Cap (&lt;$20 · 20–100M float · RVol 3x+)</span>
</div>
{'<div class="banner-closed">⚠ Market closed or pre-market data unavailable — intraday scores (RVol, Gap, VWAP) are estimated from prior session. Scores will update when market opens.</div>' if not market_live else ''}
<div class="summary">
  <div class="sum-cell"><div class="sum-n c-g">{len(buy)}</div><div class="sum-l">Buy Watch ≥65</div></div>
  <div class="sum-cell"><div class="sum-n c-a">{len(monitor)}</div><div class="sum-l">Monitor 40–64</div></div>
  <div class="sum-cell"><div class="sum-n c-r">{len(avoid)}</div><div class="sum-l">Avoid &lt;40</div></div>
  <div class="sum-cell"><div class="sum-n">{total_n}</div><div class="sum-l">Total Scored</div></div>
</div>
<div class="body">
  <div class="sec-lbl">Buy Watch — 65+</div>
  <div class="cards">{buy_out}</div>
  <div class="sec-lbl">Monitor — 40 to 64</div>
  <div class="cards">{monitor_out}</div>
  <div class="sec-lbl">Avoid — below 40</div>
  <div class="chips">{avoid_out}</div>
</div>
<div class="footer">Trend 30% · Range 45% · Volume 25% · Not financial advice · Always confirm on live chart before entry</div>
</body></html>"""

# ── Index ──────────────────────────────────────────────────────────────────────

SESSION_DISPLAY = {
    "night": "Pre-Market", "premarket": "Market Open",
    "midday": "Midday",    "powerhour": "After Hours",
}

def update_index(docs_dir):
    FIXED_FILES = {"premarket.html","marketopen.html","midday.html","afterhours.html"}
    files = sorted(
        [f for f in os.listdir(docs_dir) if f.endswith(".html") and f != "index.html" and f not in FIXED_FILES],
        reverse=True
    )
    rows = ""
    for f in files:
        name  = f.replace(".html", "")
        parts = name.split("_")
        if len(parts) >= 4:
            raw_date = "-".join(parts[:3])
            try:
                d = date.fromisoformat(raw_date)
                d_str = d.strftime("%a %b %-d, %Y")
            except Exception:
                d_str = raw_date
            sess_key = parts[3]
            sess_name = SESSION_DISPLAY.get(sess_key, sess_key.capitalize())
        else:
            d_str, sess_name = name, ""
        rows += (
            f'<a class="row" href="{f}">'
            f'<span class="rd">{d_str}</span>'
            f'<span class="rs">{sess_name}</span>'
            f'<span class="ra">View →</span></a>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watchlist Archive</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@500;700&display=swap" rel="stylesheet">
<style>
:root{{--bg:#0c0e11;--bg2:#141618;--border:rgba(255,255,255,0.07);--text:#dde1e9;--muted:#656c7a;--green:#3a9c5f;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif;}}
*{{box-sizing:border-box;margin:0;padding:0;}}
body{{background:var(--bg);color:var(--text);font-family:var(--mono);}}
.hdr{{padding:24px;border-bottom:1px solid var(--border);}}
.hdr h1{{font-family:var(--sans);font-size:22px;font-weight:700;}}
.hdr h1 em{{color:var(--green);font-style:normal;}}
.hdr p{{font-size:11px;color:var(--muted);margin-top:4px;}}
.list{{max-width:640px;margin:24px auto;padding:0 20px;display:flex;flex-direction:column;gap:6px;}}
.row{{display:flex;align-items:center;gap:12px;background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:12px 16px;text-decoration:none;color:var(--text);}}
.row:hover{{border-color:var(--green);}}
.rd{{font-size:13px;font-weight:500;min-width:130px;}}
.rs{{font-size:12px;color:var(--muted);flex:1;}}
.ra{{font-size:11px;color:var(--green);margin-left:auto;}}
</style>
</head>
<body>
<div class="hdr">
  <h1>Watchlist <em>Archive</em></h1>
  <p>All sessions · Low Float + Mid Cap combined · Scored and ranked</p>
</div>
<div class="list">
{rows or '<p style="color:#656c7a;padding:16px 0;font-size:12px;">No sessions yet.</p>'}
</div>
</body></html>"""
    with open(os.path.join(docs_dir, "index.html"), "w") as f:
        f.write(html)
    print("  [+] index.html updated")

# ── Real VWAP via yfinance ────────────────────────────────────────────────────

def fetch_vwap(tickers, session, now_et):
    """Fetch real VWAP for each ticker. Only meaningful for midday and powerhour sessions."""
    # Night and premarket run before/at market open — no meaningful intraday VWAP yet
    if session in ("earlypremarket", "premarket", "marketopen"):
        return {}
    try:
        import yfinance as yf
        from zoneinfo import ZoneInfo as _ZI
        et = _ZI("America/New_York")
        today = now_et.date()
        start_dt = datetime(today.year, today.month, today.day, 9, 30, tzinfo=et)
        end_dt   = now_et  # up to current time

        if start_dt >= end_dt:
            return {}

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
        if data is None or data.empty:
            return {}

        vwaps = {}
        for ticker in tickers:
            try:
                df = data if len(tickers) == 1 else (
                    data[ticker] if ticker in data.columns.get_level_values(0) else None
                )
                if df is None or df.empty:
                    continue
                typical = (df["High"] + df["Low"] + df["Close"]) / 3
                vwap = (typical * df["Volume"]).cumsum().iloc[-1] / df["Volume"].cumsum().iloc[-1]
                vwaps[ticker] = round(float(vwap), 3)
            except Exception:
                continue
        print(f"  [+] Real VWAP calculated for {len(vwaps)}/{len(tickers)} tickers")
        return vwaps
    except Exception as e:
        print(f"  [!] VWAP fetch failed: {e} — skipping")
        return {}

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session", choices=list(SESSIONS.keys()), required=True)
    args = parser.parse_args()

    session     = args.session
    label, note = SESSIONS[session]
    now_et      = datetime.now(ZoneInfo("America/New_York"))
    trading_day = trading_date_for_session(session, now_et)
    gen_time    = now_et.strftime("%I:%M %p ET")

    print(f"\nWatchlist scorer · {label} · target date: {fmt_trading_date(trading_day)} · generated {gen_time}")

    all_rows = []
    for scan_label, filters in SCREENERS:
        all_rows.extend(fetch_csv(scan_label, filters))

    if not all_rows:
        print("No data. Check FINVIZ_TOKEN and screener filters.")
        sys.exit(1)

    seen, unique = set(), []
    for r in all_rows:
        t = r.get("Ticker", "")
        if t and t not in seen:
            seen.add(t); unique.append(r)
    print(f"  [+] {len(unique)} unique tickers after dedup")

    # Fetch real VWAP for midday and powerhour sessions
    tickers = [r.get("Ticker", "") for r in unique if r.get("Ticker")]
    real_vwaps = fetch_vwap(tickers, session, now_et)

    # Inject real VWAP into rows so score_row can use it
    for r in unique:
        t = r.get("Ticker", "")
        if t in real_vwaps:
            r["_real_vwap"] = real_vwaps[t]

    results = [score_row(r) for r in unique]
    results = apply_sector_bonus(results)
    results.sort(key=lambda x: x["total"], reverse=True)

    live     = is_market_live(unique)
    html     = render_html(results, session, trading_day, label, note, gen_time, market_live=live)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # 1. Dated archive copy — keeps history, powers index page
    filename = f"{trading_day.isoformat()}_{session}.html"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "w") as f:
        f.write(html)
    print(f"  [+] Written → {filepath}")

    # 2. Fixed permanent URL — WHOP embeds always point here, never changes
    FIXED_NAMES = {
        "earlypremarket": "earlypremarket.html",
        "premarket":      "premarket.html",
        "marketopen":     "marketopen.html",
        "midday":         "midday.html",
        "afterhours":     "afterhours.html",
    }
    fixed_name = FIXED_NAMES[session]
    fixed_path = os.path.join(OUTPUT_DIR, fixed_name)
    with open(fixed_path, "w") as f:
        f.write(html)
    print(f"  [+] Fixed URL → {fixed_path}")

    # 3. Save JSON data file for results tracker
    import json
    data_dir = os.path.join(OUTPUT_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)
    json_data = {
        "session": session,
        "label": label,
        "date": trading_day.isoformat(),
        "generated": gen_time,
        "tickers": [
            {
                "ticker": r["ticker"],
                "company": r["company"],
                "sector": r["sector"],
                "scan": r["scan"],
                "score": r["total"],
                "tier": "buy" if r["total"] >= 65 else ("monitor" if r["total"] >= 40 else "avoid"),
                "entry": r["entry"],
                "stop": r["stop"],
                "tp1": r["tp1"],
                "tp2": r["tp2"],
                "tp3": r["tp3"],
                "price_at_scan": r["price"],
                "gap": r["gap"],
                "rvol": r["rvol"],
                "flags": [f[0] for f in r["flags"]],
            }
            for r in results if r["total"] >= 40  # Buy Watch + Monitor only
        ]
    }
    json_path = os.path.join(data_dir, f"{trading_day.isoformat()}_{session}.json")
    with open(json_path, "w") as f:
        json.dump(json_data, f, indent=2)
    print(f"  [+] JSON data → {json_path}")

    update_index(OUTPUT_DIR)
    print(f"\nDone.")
    print(f"  Archive URL : https://YOUR_USERNAME.github.io/watchlist/{filename}")
    print(f"  WHOP embed  : https://YOUR_USERNAME.github.io/watchlist/{fixed_name}\n")

if __name__ == "__main__":
    main()
