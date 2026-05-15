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
    total     = len(triggered) + len(watch) + len(ondeck)

    status_color = "#5cc98a" if market_status == "open" else "#e6a817"
    status_label = {"open": "MARKET OPEN", "pre": "PRE-MARKET", "after": "AFTER HOURS"}.get(market_status, "CLOSED")

    def row_html(t, meta, bucket, idx):
        sym        = t.get("ticker", "")
        company    = t.get("company", "")
        score      = t.get("score", 0)
        scan       = t.get("scan", "")
        entry_lbl  = t.get("entry_label", "")
        source     = t.get("source", "wl")
        flags      = t.get("flags", [])

        price      = meta.get("price", 0)
        vwap       = meta.get("vwap", 0)
        hod        = meta.get("hod", 0)
        pct        = meta.get("pct_from_entry", 0)
        signals    = meta.get("signals", [])
        above_vwap = meta.get("above_vwap", False)

        pct_str    = ("+{:.1f}%".format(pct * 100)) if pct >= 0 else ("{:.1f}%".format(pct * 100))
        pct_color  = "#5cc98a" if pct >= 0 else "#e05c5c"
        vwap_color = "#5cc98a" if above_vwap else "#e05c5c"
        vwap_lbl   = "above" if above_vwap else "below"

        bucket_color = {"triggered": "#5cc98a", "watch": "#e6a817", "ondeck": "#4a9eda"}.get(bucket, "#888")
        scan_short   = "LF" if "Low" in scan else ("MC" if "Mid" in scan else "LV")
        score_str    = " \xb7 {}".format(score) if score else ""

        sig_colors = ["#5cc98a", "#4a9eda", "#e6a817", "#c97dd4"]
        sigs_html  = "".join(
            '<span class="sig" style="color:{c};border-color:{c}">{s}</span>'.format(c=sig_colors[i % 4], s=s)
            for i, s in enumerate(signals)
        )
        if source == "live":
            sigs_html += '<span class="sig" style="color:#c97dd4;border-color:#c97dd4">LIVE</span>'

        flags_html = " \xb7 ".join(flags[:3]) if flags else ""

        row = '<div class="row" data-bucket="{bk}">'.format(bk=bucket)
        row += '<div class="row-main" onclick="toggleRow({idx})">'.format(idx=idx)
        row += '<div class="row-left" style="border-left-color:{bc}">'.format(bc=bucket_color)
        row += '<div class="row-sym">{}</div>'.format(sym)
        row += '<div class="row-meta">{}{}</div>'.format(scan_short, score_str)
        row += '</div>'
        row += '<div class="row-sigs">{}</div>'.format(sigs_html)
        row += '<div class="row-price">'
        row += '<div class="row-pv">${:.2f}</div>'.format(price)
        row += '<div class="row-sub" style="color:{c}">VWAP ${:.2f} {l}</div>'.format(vwap, l=vwap_lbl, c=vwap_color)
        row += '</div>'
        row += '<div class="row-pct">'
        row += '<div class="row-pv" style="color:{c}">{p}</div>'.format(c=pct_color, p=pct_str)
        row += '<div class="row-sub">vs entry</div>'
        row += '</div>'
        row += '</div>'
        row += '<div class="row-detail" id="rd-{idx}">'.format(idx=idx)
        row += '<div class="rd-inner">'
        row += '<div class="rd-item"><span class="rd-lbl">Entry</span><span class="rd-val">{}</span></div>'.format(entry_lbl)
        row += '<div class="rd-item"><span class="rd-lbl">HOD</span><span class="rd-val">${:.2f}</span></div>'.format(hod)
        if flags_html:
            row += '<div class="rd-item"><span class="rd-lbl">Flags</span><span class="rd-val rd-flags">{}</span></div>'.format(flags_html)
        row += '<div class="rd-links">'
        row += '<a class="rl info" href="https://finviz.com/chart.ashx?t={s}&ty=c&ta=0&p=i5&s=l" target="_blank">Chart &#8599;</a>'.format(s=sym)
        row += '<a class="rl" href="https://www.tradingview.com/chart/?symbol={s}" target="_blank">TV &#8599;</a>'.format(s=sym)
        row += '<a class="rl" href="https://finviz.com/quote.ashx?t={s}" target="_blank">Quote &#8599;</a>'.format(s=sym)
        row += '</div></div></div></div>'
        return row

    rows_html = ""
    idx = 0
    for t, meta in triggered:
        rows_html += row_html(t, meta, "triggered", idx); idx += 1
    for t, meta in watch:
        rows_html += row_html(t, meta, "watch",     idx); idx += 1
    for t, meta in ondeck:
        rows_html += row_html(t, meta, "ondeck",    idx); idx += 1

    if not rows_html:
        rows_html = '<div class="no-data">No tickers yet. Run premarket scorer first.</div>'

    css = (
        ":root{"
        "--bg:#0a0f0a;--bg2:#0f160f;--bg3:#141c14;--bd:#1e2a1e;"
        "--green:#5cc98a;--gold:#e6a817;--blue:#4a9eda;--red:#e05c5c;"
        "--muted:#4a5a4a;--text:#c8d8c8;"
        "--mono:'SF Mono','Fira Mono',monospace;"
        "--sans:'Inter','Helvetica Neue',sans-serif;}"
        "*{box-sizing:border-box;margin:0;padding:0;}"
        "html,body{height:100%;overflow:hidden;}"
        "body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;display:flex;flex-direction:column;}"
        ".hdr{background:var(--bg2);border-bottom:1px solid var(--bd);padding:10px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}"
        ".hdr-l{display:flex;align-items:center;gap:8px;}"
        ".hdr-brand{font-size:12px;color:var(--muted);}"
        ".hdr-name{font-size:14px;font-weight:700;color:var(--green);letter-spacing:0.03em;}"
        ".hdr-r{display:flex;align-items:center;gap:8px;}"
        ".status-dot{width:6px;height:6px;border-radius:50%;}"
        ".status-lbl{font-family:var(--mono);font-size:9px;letter-spacing:0.1em;}"
        ".pill{font-family:var(--mono);font-size:10px;color:var(--muted);background:var(--bg3);border:1px solid var(--bd);border-radius:20px;padding:2px 8px;}"
        ".help-btn{font-family:var(--mono);font-size:10px;padding:3px 10px;border-radius:20px;background:rgba(74,158,218,0.1);color:var(--blue);border:1px solid rgba(74,158,218,0.25);cursor:pointer;}"
        ".help-btn:hover{background:rgba(74,158,218,0.2);}"
        ".help-panel{display:none;background:var(--bg2);border-bottom:1px solid var(--bd);padding:14px 20px;flex-shrink:0;}"
        ".help-panel.open{display:block;}"
        ".help-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;}"
        ".help-item{display:flex;flex-direction:column;gap:3px;padding:8px 10px;background:var(--bg3);border-radius:6px;border:1px solid var(--bd);}"
        ".hk{font-size:11px;font-weight:700;font-family:var(--mono);}"
        ".hv{font-size:11px;color:var(--muted);line-height:1.4;}"
        ".strip{display:flex;border-bottom:1px solid var(--bd);flex-shrink:0;}"
        ".strip-item{flex:1;padding:8px 16px;display:flex;align-items:center;gap:8px;border-right:1px solid var(--bd);}"
        ".strip-item:last-child{border-right:none;}"
        ".strip-num{font-size:20px;font-weight:700;font-family:var(--mono);}"
        ".strip-lbl{font-size:10px;color:var(--muted);letter-spacing:0.06em;text-transform:uppercase;}"
        ".main{display:flex;flex:1;overflow:hidden;}"
        ".cat-panel{width:120px;flex-shrink:0;border-right:1px solid var(--bd);background:var(--bg2);display:flex;flex-direction:column;padding:8px 0;gap:2px;}"
        ".cat-item{padding:10px 14px;font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;transition:background .1s;display:flex;justify-content:space-between;align-items:center;}"
        ".cat-item:hover,.cat-item.active{background:var(--bg3);color:#fff;}"
        ".cat-item.triggered:hover,.cat-item.triggered.active{color:#5cc98a;}"
        ".cat-item.watching:hover,.cat-item.watching.active{color:#e6a817;}"
        ".cat-item.ondeck:hover,.cat-item.ondeck.active{color:#4a9eda;}"
        ".cat-cnt{font-size:10px;font-family:var(--mono);color:var(--muted);font-weight:400;}"
        ".list-panel{flex:1;overflow-y:auto;}"
        ".col-hdr{display:grid;grid-template-columns:120px 1fr 120px 90px;align-items:center;padding:6px 16px;border-bottom:1px solid var(--bd);background:var(--bg2);position:sticky;top:0;z-index:10;}"
        ".col-hdr span{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.07em;}"
        ".col-hdr span:nth-child(3),.col-hdr span:nth-child(4){text-align:right;}"
        ".row{border-bottom:1px solid var(--bd);}"
        ".row-main{display:grid;grid-template-columns:120px 1fr 120px 90px;align-items:center;padding:10px 16px;cursor:pointer;transition:background .1s;}"
        ".row-main:hover{background:var(--bg3);}"
        ".row-left{border-left:2px solid;padding-left:10px;}"
        ".row-sym{font-size:14px;font-weight:700;color:#fff;font-family:var(--mono);}"
        ".row-meta{font-size:10px;color:var(--muted);margin-top:1px;}"
        ".row-sigs{display:flex;flex-wrap:wrap;gap:4px;}"
        ".sig{font-size:9px;font-weight:700;letter-spacing:0.05em;padding:2px 6px;border-radius:8px;border:1px solid;}"
        ".row-price,.row-pct{text-align:right;}"
        ".row-pv{font-size:13px;font-weight:700;font-family:var(--mono);color:#fff;}"
        ".row-sub{font-size:10px;color:var(--muted);margin-top:1px;}"
        ".row-detail{display:none;border-top:1px solid var(--bd);background:var(--bg3);}"
        ".row-detail.open{display:block;}"
        ".rd-inner{display:flex;align-items:center;gap:16px;padding:10px 16px 10px 30px;flex-wrap:wrap;}"
        ".rd-item{display:flex;align-items:baseline;gap:6px;}"
        ".rd-lbl{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:0.06em;white-space:nowrap;}"
        ".rd-val{font-size:11px;color:var(--text);font-family:var(--mono);}"
        ".rd-flags{color:var(--muted);font-family:var(--sans);}"
        ".rd-links{display:flex;gap:6px;margin-left:auto;}"
        ".rl{font-family:var(--mono);font-size:10px;color:var(--muted);text-decoration:none;padding:3px 8px;border:1px solid var(--bd);border-radius:4px;}"
        ".rl:hover{color:var(--text);}"
        ".rl.info{color:var(--blue);border-color:rgba(74,158,218,0.3);}"
        ".no-data{padding:40px;text-align:center;color:var(--muted);font-size:12px;}"
        "@media(max-width:700px){"
        "html,body{overflow:auto;}"
        ".main{flex-direction:column;overflow:visible;}"
        ".cat-panel{width:100%;flex-direction:row;border-right:none;border-bottom:1px solid var(--bd);}"
        ".list-panel{overflow:visible;}"
        ".col-hdr,.row-main{grid-template-columns:100px 1fr 80px;}"
        ".row-pct{display:none;}}"
    )

    js = (
        "function toggleHelp(){var p=document.getElementById('help-panel');p.classList.toggle('open');}"
        "function selectCat(el,bucket){"
        "document.querySelectorAll('.cat-item').forEach(c=>c.classList.remove('active'));"
        "el.classList.add('active');"
        "document.querySelectorAll('.row').forEach(r=>{"
        "r.style.display=(bucket==='all'||r.dataset.bucket===bucket)?'block':'none';});"
        "}"
        "function toggleRow(idx){"
        "var d=document.getElementById('rd-'+idx);"
        "var was=d.classList.contains('open');"
        "document.querySelectorAll('.row-detail').forEach(x=>x.classList.remove('open'));"
        "if(!was)d.classList.add('open');}"
    )

    help_html = (
        '<div class="help-panel" id="help-panel"><div class="help-grid">'
        '<div class="help-item"><span class="hk" style="color:#5cc98a">Triggered</span><span class="hv">Price broke above the proposed entry. Actionable now.</span></div>'
        '<div class="help-item"><span class="hk" style="color:#e6a817">Watching</span><span class="hv">Within 5% of entry or above VWAP. Set an alert.</span></div>'
        '<div class="help-item"><span class="hk" style="color:#4a9eda">On Deck</span><span class="hv">On the morning WL, not yet moving. Watch for a volume spike.</span></div>'
        '<div class="help-item"><span class="hk">Entry Break</span><span class="hv">Price crossed the morning scorer entry level.</span></div>'
        '<div class="help-item"><span class="hk">Above VWAP</span><span class="hv">Holding above volume-weighted average price.</span></div>'
        '<div class="help-item"><span class="hk">Near HOD</span><span class="hv">Within 2% of the high of day.</span></div>'
        '<div class="help-item"><span class="hk">Live Mover</span><span class="hv">Not on morning list, found by live RVol sweep.</span></div>'
        '<div class="help-item"><span class="hk">LF / MC / LV</span><span class="hv">Low Float / Mid Cap / Live Mover scan type.</span></div>'
        '</div></div>'
    )

    out = "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n"
    out += "<meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n"
    out += "<meta http-equiv=\"refresh\" content=\"300\">\n"
    out += "<title>Watchlist \xb7 Scanner</title>\n"
    out += "<style>{}</style>\n</head>\n<body>\n".format(css)
    out += (
        "<div class=\"hdr\">"
        "<div class=\"hdr-l\"><span class=\"hdr-brand\">Watchlist \xb7</span>"
        "<span class=\"hdr-name\">Intraday Scanner</span></div>"
        "<div class=\"hdr-r\">"
        "<span class=\"status-dot\" style=\"background:{sc}\"></span>"
        "<span class=\"status-lbl\" style=\"color:{sc}\">{sl}</span>"
        "<span class=\"pill\">Updated {gt}</span>"
        "<span class=\"pill\">{tot} tickers</span>"
        "<button class=\"help-btn\" onclick=\"toggleHelp()\">? How to use</button>"
        "</div></div>\n"
    ).format(sc=status_color, sl=status_label, gt=gen_time, tot=total)
    out += help_html
    out += (
        "<div class=\"strip\">"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--green)\">{tr}</span><span class=\"strip-lbl\">Triggered</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--gold)\">{wt}</span><span class=\"strip-lbl\">Watching</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--blue)\">{od}</span><span class=\"strip-lbl\">On Deck</span></div>"
        "<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:#fff\">{tot}</span><span class=\"strip-lbl\">Universe</span></div>"
        "</div>\n"
    ).format(tr=len(triggered), wt=len(watch), od=len(ondeck), tot=total)
    out += (
        "<div class=\"main\">"
        "<div class=\"cat-panel\">"
        "<div class=\"cat-item active\" onclick=\"selectCat(this,'all')\">All<span class=\"cat-cnt\">{tot}</span></div>"
        "<div class=\"cat-item triggered\" onclick=\"selectCat(this,'triggered')\">Triggered<span class=\"cat-cnt\">{tr}</span></div>"
        "<div class=\"cat-item watching\" onclick=\"selectCat(this,'watch')\">Watching<span class=\"cat-cnt\">{wt}</span></div>"
        "<div class=\"cat-item ondeck\" onclick=\"selectCat(this,'ondeck')\">On Deck<span class=\"cat-cnt\">{od}</span></div>"
        "</div>"
        "<div class=\"list-panel\">"
        "<div class=\"col-hdr\"><span>Ticker</span><span>Signals</span><span>Price / VWAP</span><span>vs Entry</span></div>"
        "{rows}"
        "</div></div>\n"
        "<script>{js}</script>\n</body>\n</html>"
    ).format(tot=total, tr=len(triggered), wt=len(watch), od=len(ondeck), rows=rows_html, js=js)

    return out



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
