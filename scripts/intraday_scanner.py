"""
Intraday Scanner
Reads today's premarket.json as the base universe, fetches live prices via
yfinance, adds any new Finviz RVol 5x+ movers, and writes scanner.html.

Runs every 15 mins 9:30 AM–4 PM ET via GitHub Actions cron.
"""

import os, sys, json, time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf
import pandas as pd

# ── Config ─────────────────────────────────────────────────────────────────
ET          = ZoneInfo("America/New_York")
DATA_DIR    = os.path.join("docs", "data")
OUTPUT_DIR  = "docs"
TOKEN       = os.environ.get("FINVIZ_TOKEN", "")

# Signal thresholds
ENTRY_TRIGGER_PCT   = 0.00   # price >= entry level
ENTRY_APPROACH_PCT  = 0.05   # price within 5% below entry
VWAP_RECLAIM_BUFFER = 0.002  # price >= vwap * (1 - buffer)
RVOL_SPIKE_MIN      = 3.0    # minimum RVol to include new movers
NEW_MOVER_RVOL      = 5.0    # RVol threshold for Finviz new mover sweep

MARKET_HOLIDAYS = {
    date(2026, 1, 1),  date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3),  date(2026, 5, 25), date(2026, 7, 3),
    date(2026, 9, 7),  date(2026, 11, 26),date(2026, 11, 27),
    date(2026, 12, 25),
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def is_trading_day(d):
    return d.weekday() < 5 and d not in MARKET_HOLIDAYS

def now_et():
    return datetime.now(ET)

def fmt_time(dt):
    return dt.strftime("%-I:%M %p ET")

def load_wl_tickers():
    """Load all tickers from today's session JSONs."""
    sessions = ["premarket", "marketopen", "midday", "afterhours"]
    tickers = {}   # ticker → best scored row (highest score wins on dupe)
    for s in sessions:
        path = os.path.join(DATA_DIR, f"{s}.json")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            data = json.load(f)
        for t in data.get("tickers", []):
            sym = t.get("ticker", "")
            if not sym:
                continue
            if sym not in tickers or t.get("score", 0) > tickers[sym].get("score", 0):
                tickers[sym] = t
    return tickers

def fetch_live_data(symbols):
    """Fetch live price, volume, and intraday VWAP for a list of symbols."""
    if not symbols:
        return {}
    results = {}
    try:
        raw = yf.download(
            symbols,
            period="1d",
            interval="5m",
            progress=False,
            threads=True,
            auto_adjust=True,
        )
        # Handle single vs multi ticker response
        if isinstance(raw.columns, pd.MultiIndex):
            tickers_in = raw.columns.get_level_values(1).unique().tolist()
        else:
            tickers_in = symbols[:1]
            raw = pd.concat({symbols[0]: raw}, axis=1)
            raw.columns = pd.MultiIndex.from_tuples([(c, symbols[0]) for c in raw.columns])

        for sym in tickers_in:
            try:
                df = raw.xs(sym, axis=1, level=1).dropna(how="all")
                if df.empty:
                    continue
                close_series = df["Close"].dropna()
                vol_series   = df["Volume"].dropna()
                high_series  = df["High"].dropna()

                if close_series.empty:
                    continue

                price   = float(close_series.iloc[-1])
                volume  = int(vol_series.sum()) if not vol_series.empty else 0
                hod     = float(high_series.max()) if not high_series.empty else price

                # VWAP = sum(typical_price * volume) / sum(volume)
                typical = ((df["High"] + df["Low"] + df["Close"]) / 3).dropna()
                vol_clean = df["Volume"].dropna()
                if not typical.empty and vol_clean.sum() > 0:
                    vwap = float((typical * vol_clean).sum() / vol_clean.sum())
                else:
                    vwap = price

                results[sym] = {
                    "price":  price,
                    "volume": volume,
                    "hod":    hod,
                    "vwap":   vwap,
                }
            except Exception:
                continue
    except Exception as e:
        print(f"  [!] yfinance batch error: {e}")
    return results

def fetch_new_movers():
    """Pull Finviz live gainers with RVol 5x+ not already in WL."""
    if not TOKEN:
        return []
    import urllib.request, urllib.error, csv, io
    url = (
        f"https://elite.finviz.com/export.ashx?"
        f"v=152&c=0,1,2,3,4,5,6,65,66,61,67,64,63,25,59,60,87,88,86,81,30,68,137,136"
        f"&f=cap_smallunder,sh_curvol_o5000,sh_relvol_o5,ta_change_u"
        f"&auth={TOKEN}"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)
        movers = []
        for row in rows:
            try:
                sym   = row.get("Ticker", "").strip()
                price = float(row.get("Price", 0) or 0)
                rvol  = float(row.get("Relative Volume", 0) or 0)
                flt   = float(row.get("Shares Float", 0) or 0)
                if sym and price > 0 and rvol >= NEW_MOVER_RVOL:
                    movers.append({
                        "ticker":      sym,
                        "company":     row.get("Company", ""),
                        "sector":      row.get("Sector", ""),
                        "scan":        "Live Mover",
                        "score":       0,
                        "tier":        "monitor",
                        "entry":       round(price * 1.005, 2),
                        "entry_label": f"Momentum above ${price:.2f}",
                        "rvol":        rvol,
                        "float_m":     flt,
                        "source":      "live",
                    })
            except Exception:
                continue
        print(f"  [+] Finviz live movers: {len(movers)} new tickers")
        return movers
    except Exception as e:
        print(f"  [!] Finviz sweep failed: {e}")
        return []

def classify_signal(t, live):
    """
    Given a WL ticker dict and live price data, return signal classification.
    Returns one of: "triggered", "watch", "ondeck", "inactive"
    """
    sym   = t.get("ticker", "")
    entry = float(t.get("entry", 0) or 0)
    if sym not in live or entry <= 0:
        return "inactive", {}

    lv    = live[sym]
    price = lv["price"]
    vwap  = lv["vwap"]
    hod   = lv["hod"]

    pct_from_entry = (price - entry) / entry if entry else 0
    above_vwap     = price >= vwap * (1 - VWAP_RECLAIM_BUFFER)
    near_hod       = price >= hod * 0.97

    signals = []
    if price >= entry:
        signals.append("ENTRY BREAK")
    if above_vwap:
        signals.append("ABOVE VWAP")
    if near_hod:
        signals.append("NEAR HOD")

    meta = {
        "price":          price,
        "vwap":           vwap,
        "hod":            hod,
        "pct_from_entry": pct_from_entry,
        "above_vwap":     above_vwap,
        "signals":        signals,
    }

    if price >= entry:
        return "triggered", meta
    elif pct_from_entry >= -ENTRY_APPROACH_PCT:
        return "watch", meta
    elif above_vwap:
        return "watch", meta
    else:
        return "ondeck", meta

# ── HTML Rendering ──────────────────────────────────────────────────────────

SIGNAL_COLORS = {
    "ENTRY BREAK": "#5cc98a",
    "ABOVE VWAP":  "#4a9eda",
    "NEAR HOD":    "#e6a817",
    "LIVE MOVER":  "#c97dd4",
}

def signal_badge(s):
    color = SIGNAL_COLORS.get(s, "#888")
    return f'<span class="sig-badge" style="border-color:{color};color:{color}">{s}</span>'

def scanner_card(t, meta, bucket):
    sym     = t.get("ticker", "")
    company = t.get("company", "")[:28]
    score   = t.get("score", 0)
    scan    = t.get("scan", "")
    entry   = float(t.get("entry", 0) or 0)
    source  = t.get("source", "wl")

    price         = meta.get("price", 0)
    vwap          = meta.get("vwap", 0)
    hod           = meta.get("hod", 0)
    pct_from_entry= meta.get("pct_from_entry", 0)
    signals       = meta.get("signals", [])

    price_str = f"${price:.2f}"
    entry_str = f"${entry:.2f}"
    vwap_str  = f"${vwap:.2f}"
    hod_str   = f"${hod:.2f}"

    if pct_from_entry >= 0:
        pct_cls   = "green"
        pct_str   = f"+{pct_from_entry*100:.1f}% above entry"
    else:
        pct_cls   = "red"
        pct_str   = f"{pct_from_entry*100:.1f}% below entry"

    scan_cls = "low-float" if "Low" in scan else ("mid-cap" if "Mid" in scan else "live")
    score_cls = "hot" if score >= 80 else ("warm" if score >= 65 else "watch")

    badge_html = "".join(signal_badge(s) for s in signals)
    if source == "live":
        badge_html += signal_badge("LIVE MOVER")

    bucket_cls = {"triggered": "card-triggered", "watch": "card-watch", "ondeck": "card-ondeck"}.get(bucket, "")

    return f'''<div class="card {bucket_cls}">
  <div class="card-top">
    <div class="card-left">
      <span class="sym">{sym}</span>
      <span class="co">{company}</span>
    </div>
    <div class="card-right">
      <span class="scan-pill {scan_cls}">{scan}</span>
      {f'<span class="score-pill {score_cls}">{score}</span>' if score else ''}
    </div>
  </div>
  <div class="signals">{badge_html}</div>
  <div class="metrics">
    <div class="met"><div class="met-l">Price</div><div class="met-v">{price_str}</div></div>
    <div class="met"><div class="met-l">Entry</div><div class="met-v {pct_cls}">{entry_str} <span class="pct">({pct_str})</span></div></div>
    <div class="met"><div class="met-l">VWAP</div><div class="met-v">{vwap_str}</div></div>
    <div class="met"><div class="met-l">HOD</div><div class="met-v">{hod_str}</div></div>
  </div>
</div>'''

def render_html(buckets, gen_time, market_status):
    triggered = buckets.get("triggered", [])
    watch     = buckets.get("watch", [])
    ondeck    = buckets.get("ondeck", [])
    all_items = triggered + watch + ondeck
    total     = len(all_items)

    status_color = "#5cc98a" if market_status == "open" else "#e6a817"
    status_label = {"open": "MARKET OPEN", "pre": "PRE-MARKET", "after": "AFTER HOURS"}.get(market_status, "CLOSED")

    def sidebar_row(t, meta, bucket, idx, is_first):
        sym       = t.get("ticker", "")
        score     = t.get("score", 0)
        scan      = t.get("scan", "")
        price     = meta.get("price", 0)
        pct       = meta.get("pct_from_entry", 0)
        pct_str   = ("+{:.1f}%".format(pct*100)) if pct >= 0 else ("{:.1f}%".format(pct*100))
        pct_color = "#5cc98a" if pct >= 0 else "#e05c5c"
        scan_short= "LF" if "Low" in scan else ("MC" if "Mid" in scan else "LV")
        score_str = " \xb7 {}".format(score) if score else ""
        active_cls= " active" if is_first else ""
        return (
            '<div class="sr{}" data-idx="{}" onclick="selectTicker({})">'.format(active_cls, idx, idx) +
            '<div class="sr-top"><span class="sr-sym">{}</span><span class="sr-price">${:.2f}</span></div>'.format(sym, price) +
            '<div class="sr-bot"><span class="sr-meta">{}{}</span><span class="sr-pct" style="color:{}">{}</span></div>'.format(scan_short, score_str, pct_color, pct_str) +
            '</div>'
        )

    def detail_panel(t, meta, bucket, idx, is_first):
        sym        = t.get("ticker", "")
        company    = t.get("company", "")
        score      = t.get("score", 0)
        scan       = t.get("scan", "")
        entry      = float(t.get("entry", 0) or 0)
        entry_lbl  = t.get("entry_label", "Break above ${:.2f}".format(entry))
        source     = t.get("source", "wl")
        flags      = t.get("flags", [])

        price      = meta.get("price", 0)
        vwap       = meta.get("vwap", 0)
        hod        = meta.get("hod", 0)
        pct        = meta.get("pct_from_entry", 0)
        signals    = meta.get("signals", [])
        above_vwap = meta.get("above_vwap", False)

        pct_str    = ("+{:.1f}%".format(pct*100)) if pct >= 0 else ("{:.1f}%".format(pct*100))
        pct_color  = "#5cc98a" if pct >= 0 else "#e05c5c"
        vwap_lbl   = "above VWAP" if above_vwap else "below VWAP"
        vwap_color = "#5cc98a" if above_vwap else "#e05c5c"
        hod_pct    = (price - hod) / hod * 100 if hod else 0
        hod_lbl    = "at HOD" if abs(hod_pct) < 1 else "{:.1f}% from HOD".format(hod_pct)
        hod_color  = "#e6a817" if abs(hod_pct) < 2 else "#4a5a4a"

        bucket_label = {"triggered": "Entry Break", "watch": "Approaching Entry", "ondeck": "On Deck"}.get(bucket, "")
        bucket_color = {"triggered": "#5cc98a", "watch": "#e6a817", "ondeck": "#4a9eda"}.get(bucket, "#888")
        scan_cls     = "lf" if "Low" in scan else ("mc" if "Mid" in scan else "live")

        sig_colors   = ["#5cc98a", "#4a9eda", "#e6a817", "#c97dd4"]
        sigs_html    = "".join(
            '<span class="sig" style="color:{c};border-color:{c}">{s}</span>'.format(c=sig_colors[i % 4], s=s)
            for i, s in enumerate(signals)
        )
        if source == "live":
            sigs_html += '<span class="sig" style="color:#c97dd4;border-color:#c97dd4">LIVE MOVER</span>'

        flags_html = "".join('<span class="flag">{}</span>'.format(f) for f in flags[:4])

        score_html = '<span class="score-badge">{}</span>'.format(score) if score else ""
        flags_section = '<div class="dp-flags">{}</div>'.format(flags_html) if flags_html else ""

        active_cls = " active" if is_first else ""

        # Entry label — strip the dollar sign part for display
        if "$" in entry_lbl:
            entry_display = entry_lbl.split("$")[-1]
            entry_display = "${:.2f}".format(entry)
        else:
            entry_display = "${:.2f}".format(entry)

        return (
            '<div class="dp{}" id="dp-{}">'.format(active_cls, idx) +
            '<div class="dp-hdr">'
            '<div class="dp-title">'
            '<div class="dp-sym">{} <span class="scan-pill {}">{}</span></div>'.format(sym, scan_cls, scan) +
            '<div class="dp-co">{}</div>'.format(company) +
            '</div>'
            '<div class="dp-hdr-r">'
            '{}'.format(score_html) +
            '<span class="bucket-badge" style="color:{c};border-color:{c}">{}</span>'.format(bucket_label, c=bucket_color) +
            '<a class="chart-link info" href="https://finviz.com/chart.ashx?t={}&ty=c&ta=0&p=i5&s=l" target="_blank">Chart &#8599;</a>'.format(sym) +
            '<a class="chart-link" href="https://www.tradingview.com/chart/?symbol={}" target="_blank">TV &#8599;</a>'.format(sym) +
            '<a class="chart-link" href="https://finviz.com/quote.ashx?t={}" target="_blank">Quote &#8599;</a>'.format(sym) +
            '</div></div>'
            '<div class="dp-metrics">'
            '<div class="met-card"><div class="met-lbl">Live Price</div><div class="met-val">${:.2f}</div></div>'.format(price) +
            '<div class="met-card accent" style="--ac:{c}"><div class="met-lbl">vs Entry ({e})</div><div class="met-val" style="color:{c}">{p}</div></div>'.format(c=pct_color, e=entry_display, p=pct_str) +
            '<div class="met-card"><div class="met-lbl">VWAP</div><div class="met-val">${:.2f}</div><div class="met-sub" style="color:{}">{}</div></div>'.format(vwap, vwap_color, vwap_lbl) +
            '<div class="met-card"><div class="met-lbl">HOD</div><div class="met-val">${:.2f}</div><div class="met-sub" style="color:{}">{}</div></div>'.format(hod, hod_color, hod_lbl) +
            '</div>'
            '<div class="dp-signals">{}</div>'.format(sigs_html) +
            flags_section +
            '<div class="dp-entry">{}</div>'.format(entry_lbl) +
            '</div>'
        )

    def sidebar_section(label, color, items, bucket, id_offset):
        if not items:
            return ""
        rows = "".join(sidebar_row(t, meta, bucket, id_offset + i, id_offset == 0 and i == 0)
                       for i, (t, meta) in enumerate(items))
        return '<div class="sec-hdr" style="color:{}">{} <span class="sec-cnt">{}</span></div>{}'.format(
            color, label, len(items), rows)

    t_offset = 0
    w_offset = len(triggered)
    o_offset = len(triggered) + len(watch)

    sidebar_html = (
        sidebar_section("Triggered", "#5cc98a", triggered, "triggered", t_offset) +
        sidebar_section("Watching",  "#e6a817", watch,     "watch",     w_offset) +
        sidebar_section("On Deck",   "#4a9eda", ondeck,    "ondeck",    o_offset)
    )

    panels_html = ""
    for i, (t, meta) in enumerate(triggered):
        panels_html += detail_panel(t, meta, "triggered", i, i == 0)
    for i, (t, meta) in enumerate(watch):
        panels_html += detail_panel(t, meta, "watch", w_offset + i, w_offset + i == 0)
    for i, (t, meta) in enumerate(ondeck):
        panels_html += detail_panel(t, meta, "ondeck", o_offset + i, o_offset + i == 0)

    if not panels_html:
        panels_html = '<div class="no-data">No tickers in universe yet. Run premarket scorer first.</div>'

    css = """
:root {
  --bg:    #0a0f0a; --bg2: #0f160f; --bg3: #141c14; --bd: #1e2a1e;
  --green: #5cc98a; --gold: #e6a817; --blue: #4a9eda; --red: #e05c5c;
  --muted: #4a5a4a; --text: #c8d8c8;
  --mono: "SF Mono","Fira Mono",monospace;
  --sans: "Inter","Helvetica Neue",sans-serif;
}
* { box-sizing:border-box; margin:0; padding:0; }
html,body { height:100%; overflow:hidden; }
body { background:var(--bg); color:var(--text); font-family:var(--sans); font-size:13px; display:flex; flex-direction:column; }
.hdr { background:var(--bg2); border-bottom:1px solid var(--bd); padding:10px 16px; display:flex; align-items:center; justify-content:space-between; flex-shrink:0; }
.hdr-l { display:flex; align-items:center; gap:8px; }
.hdr-brand { font-size:12px; color:var(--muted); }
.hdr-name { font-size:14px; font-weight:700; color:var(--green); letter-spacing:0.03em; }
.hdr-r { display:flex; align-items:center; gap:8px; }
.status-dot { width:6px; height:6px; border-radius:50%; }
.status-lbl { font-family:var(--mono); font-size:9px; letter-spacing:0.1em; }
.pill { font-family:var(--mono); font-size:10px; color:var(--muted); background:var(--bg3); border:1px solid var(--bd); border-radius:20px; padding:2px 8px; }
.strip { display:flex; border-bottom:1px solid var(--bd); flex-shrink:0; }
.strip-item { flex:1; padding:8px 16px; display:flex; align-items:center; gap:8px; border-right:1px solid var(--bd); }
.strip-item:last-child { border-right:none; }
.strip-num { font-size:20px; font-weight:700; font-family:var(--mono); }
.strip-lbl { font-size:10px; color:var(--muted); letter-spacing:0.06em; text-transform:uppercase; }
.main { display:flex; flex:1; overflow:hidden; }
.sidebar { width:200px; flex-shrink:0; border-right:1px solid var(--bd); overflow-y:auto; background:var(--bg2); }
.sec-hdr { font-size:9px; font-weight:700; letter-spacing:0.1em; text-transform:uppercase; padding:10px 12px 6px; border-top:1px solid var(--bd); }
.sec-hdr:first-child { border-top:none; }
.sec-cnt { font-size:9px; color:var(--muted); font-weight:400; }
.sr { padding:9px 12px; border-bottom:1px solid var(--bd); cursor:pointer; transition:background .1s; }
.sr:hover { background:var(--bg3); }
.sr.active { background:var(--bg3); border-left:2px solid var(--green); }
.sr-top { display:flex; justify-content:space-between; align-items:baseline; }
.sr-sym { font-size:13px; font-weight:700; color:#fff; font-family:var(--mono); }
.sr-price { font-size:12px; font-weight:600; font-family:var(--mono); color:#fff; }
.sr-bot { display:flex; justify-content:space-between; margin-top:2px; }
.sr-meta { font-size:10px; color:var(--muted); }
.sr-pct { font-size:10px; font-weight:600; font-family:var(--mono); }
.detail { flex:1; overflow-y:auto; padding:20px 24px; }
.dp { display:none; flex-direction:column; gap:16px; }
.dp.active { display:flex; }
.dp-hdr { display:flex; justify-content:space-between; align-items:flex-start; }
.dp-sym { font-size:24px; font-weight:800; color:#fff; font-family:var(--mono); letter-spacing:0.03em; display:flex; align-items:center; gap:10px; }
.dp-co { font-size:12px; color:var(--muted); margin-top:3px; }
.dp-hdr-r { display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.score-badge { font-family:var(--mono); font-size:12px; font-weight:700; background:rgba(92,201,138,0.12); color:var(--green); border:1px solid rgba(92,201,138,0.25); border-radius:20px; padding:3px 10px; }
.bucket-badge { font-size:10px; font-weight:700; letter-spacing:0.05em; padding:3px 10px; border-radius:20px; border:1px solid; }
.chart-link { font-family:var(--mono); font-size:11px; color:var(--muted); text-decoration:none; padding:3px 8px; border:1px solid var(--bd); border-radius:4px; }
.chart-link:hover { color:var(--text); border-color:var(--muted); }
.chart-link.info { color:var(--blue); border-color:rgba(74,158,218,0.3); }
.chart-link.info:hover { color:#7dbee8; }.scan-pill { font-size:9px; font-weight:700; padding:2px 8px; border-radius:10px; letter-spacing:0.04em; vertical-align:middle; }
.scan-pill.lf { background:rgba(92,201,138,0.12); color:var(--green); }
.scan-pill.mc { background:rgba(74,158,218,0.12); color:var(--blue); }
.scan-pill.live { background:rgba(201,125,212,0.12); color:#c97dd4; }
.dp-metrics { display:grid; grid-template-columns:1fr 1fr 1fr 1fr; gap:10px; }
.met-card { background:var(--bg2); border:1px solid var(--bd); border-radius:8px; padding:12px 14px; }
.met-card.accent { border-color:rgba(92,201,138,0.2); background:rgba(92,201,138,0.04); }
.met-lbl { font-size:9px; color:var(--muted); text-transform:uppercase; letter-spacing:0.07em; margin-bottom:6px; }
.met-val { font-size:22px; font-weight:700; font-family:var(--mono); color:#fff; }
.met-sub { font-size:10px; margin-top:3px; }
.dp-signals { display:flex; flex-wrap:wrap; gap:6px; }
.sig { font-size:10px; font-weight:700; letter-spacing:0.06em; padding:3px 9px; border-radius:10px; border:1px solid; }
.dp-flags { display:flex; flex-wrap:wrap; gap:6px; }
.flag { font-size:10px; background:var(--bg3); color:var(--muted); border:1px solid var(--bd); border-radius:4px; padding:2px 8px; }
.dp-entry { font-size:12px; color:var(--muted); background:var(--bg3); border:1px solid var(--bd); border-radius:6px; padding:8px 12px; font-family:var(--mono); }
.no-data { padding:40px; text-align:center; color:var(--muted); font-size:12px; }.help-btn { font-family:var(--mono); font-size:10px; padding:3px 10px; border-radius:20px; background:rgba(74,158,218,0.1); color:var(--blue); border:1px solid rgba(74,158,218,0.25); cursor:pointer; margin-left:4px; }.help-btn:hover { background:rgba(74,158,218,0.2); }.help-panel { display:none; background:var(--bg2); border-bottom:1px solid var(--bd); padding:14px 20px; flex-shrink:0; }.help-panel.open { display:block; }.help-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:8px; }.help-item { display:flex; flex-direction:column; gap:3px; padding:8px 10px; background:var(--bg3); border-radius:6px; border:1px solid var(--bd); }.hk { font-size:11px; font-weight:700; font-family:var(--mono); }.hv { font-size:11px; color:var(--muted); line-height:1.4; }
@media(max-width:700px) {
  html,body { overflow:auto; }
  .main { flex-direction:column; overflow:visible; }
  .sidebar { width:100%; border-right:none; border-bottom:1px solid var(--bd); overflow-y:visible; }
  .detail { overflow:visible; }
  .dp-metrics { grid-template-columns:1fr 1fr; }
}"""

    js = """function toggleHelp() {var p = document.getElementById("help-panel");p.classList.toggle("open");}function selectTicker(idx) {
  document.querySelectorAll(".sr").forEach(r => r.classList.remove("active"));
  document.querySelectorAll(".dp").forEach(p => p.classList.remove("active"));
  var row = document.querySelector(".sr[data-idx='" + idx + "']");
  var panel = document.getElementById("dp-" + idx);
  if (row) row.classList.add("active");
  if (panel) panel.classList.add("active");
}"""

    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
        "<meta http-equiv=\"refresh\" content=\"300\">\n"
        "<title>Watchlist \xb7 Scanner</title>\n"
        "<style>{}</style>\n".format(css) +
        "</head>\n<body>\n"
        "<div class=\"hdr\">"
        "<div class=\"hdr-l\"><span class=\"hdr-brand\">Watchlist \xb7</span><span class=\"hdr-name\">Intraday Scanner</span></div>"
        "<div class=\"hdr-r\">"
        "<span class=\"status-dot\" style=\"background:{sc}\"></span>"
        "<span class=\"status-lbl\" style=\"color:{sc}\">{sl}</span>"
        "<span class=\"pill\">Updated {gt}</span>"
        "<span class=\"pill\">{tot} tickers</span>"
        "</div><button class=\"help-btn\" onclick=\"toggleHelp()\">? How to use</button></div></div>\n".format(sc=status_color, sl=status_label, gt=gen_time, tot=total) +
        "<div class=\"help-panel\" id=\"help-panel\"><div class=\"help-grid\"><div class=\"help-item\"><span class=\"hk\" style=\"color:#5cc98a\">Triggered</span><span class=\"hv\">Price broke above the proposed entry level. Actionable now.</span></div><div class=\"help-item\"><span class=\"hk\" style=\"color:#e6a817\">Watching</span><span class=\"hv\">Within 5% of entry or above VWAP. Set an alert.</span></div><div class=\"help-item\"><span class=\"hk\" style=\"color:#4a9eda\">On Deck</span><span class=\"hv\">On the morning WL but not yet moving. Watch for a volume spike.</span></div><div class=\"help-item\"><span class=\"hk\">Entry Break</span><span class=\"hv\">Price crossed above the morning scorer entry level.</span></div><div class=\"help-item\"><span class=\"hk\">Above VWAP</span><span class=\"hv\">Holding above volume-weighted average price — bullish intraday.</span></div><div class=\"help-item\"><span class=\"hk\">Near HOD</span><span class=\"hv\">Within 2% of high of day — momentum intact.</span></div><div class=\"help-item\"><span class=\"hk\">Live Mover</span><span class=\"hv\">Not on morning list — found by live RVol sweep. Extra caution.</span></div><div class=\"help-item\"><span class=\"hk\">LF / MC / LV</span><span class=\"hv\">Low Float / Mid Cap / Live Mover. LF moves fastest, highest risk/reward.</span></div></div></div>\n" +
        "<div class=\"strip\">"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--green)\">{}</span><span class=\"strip-lbl\">Triggered</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--gold)\">{}</span><span class=\"strip-lbl\">Watching</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--blue)\">{}</span><span class=\"strip-lbl\">On Deck</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:#fff\">{}</span><span class=\"strip-lbl\">Universe</span></div>"
        "</div>\n".format(len(triggered), len(watch), len(ondeck), total) +
        "<div class=\"main\">"
        "<div class=\"sidebar\">{}</div>"
        "<div class=\"detail\">{}</div>"
        "</div>\n".format(sidebar_html, panels_html) +
        "<script>{}</script>\n".format(js) +
        "</body>\n</html>"
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    now  = now_et()
    today = now.date()
    gen_time = now.strftime("%-I:%M %p ET")

    print(f"\nIntraday Scanner · {today} · {gen_time}")

    # Market status
    market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
    if now < market_open:
        market_status = "pre"
    elif now > market_close:
        market_status = "after"
    else:
        market_status = "open"

    # Load WL universe
    wl = load_wl_tickers()
    print(f"  [+] WL universe: {len(wl)} tickers")

    # Fetch live movers from Finviz and merge (skip if already in WL)
    new_movers = fetch_new_movers()
    added = 0
    for m in new_movers:
        sym = m["ticker"]
        if sym not in wl:
            wl[sym] = m
            added += 1
    print(f"  [+] New movers added: {added}")

    if not wl:
        print("  [!] No tickers to scan — exiting")
        return

    # Fetch live prices
    symbols = list(wl.keys())
    print(f"  [+] Fetching live data for {len(symbols)} tickers...")
    live = fetch_live_data(symbols)
    print(f"  [+] Got live data for {len(live)} tickers")

    # Classify each ticker
    buckets = {"triggered": [], "watch": [], "ondeck": [], "inactive": []}
    for sym, t in wl.items():
        bucket, meta = classify_signal(t, live)
        if bucket != "inactive":
            # Sort triggered by % above entry desc, others by score desc
            buckets[bucket].append((t, meta))

    # Sort each bucket
    buckets["triggered"].sort(key=lambda x: x[1].get("pct_from_entry", 0), reverse=True)
    buckets["watch"].sort(key=lambda x: x[1].get("pct_from_entry", 0), reverse=True)
    buckets["ondeck"].sort(key=lambda x: x[0].get("score", 0), reverse=True)

    print(f"  [+] Triggered: {len(buckets['triggered'])} | Watch: {len(buckets['watch'])} | On Deck: {len(buckets['ondeck'])}")

    # Render and write
    html = render_html(buckets, gen_time, market_status)

    # Build timestamp so git always has a diff
    ts = now.strftime("%Y-%m-%dT%H:%M:%S")
    html = html.replace("</head>", f"<!-- built {ts} --></head>", 1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, "scanner.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"  [+] Written → {out_path}")

    # Save JSON snapshot
    os.makedirs(DATA_DIR, exist_ok=True)
    snap = {
        "date":      today.isoformat(),
        "generated": gen_time,
        "status":    market_status,
        "triggered": [t[0]["ticker"] for t in buckets["triggered"]],
        "watch":     [t[0]["ticker"] for t in buckets["watch"]],
        "ondeck":    [t[0]["ticker"] for t in buckets["ondeck"]],
    }
    with open(os.path.join(DATA_DIR, "scanner.json"), "w") as f:
        json.dump(snap, f, indent=2)

    print(f"\nDone. URL: https://dwilsolutions.github.io/watchlist/scanner.html\n")

if __name__ == "__main__":
    main()
