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

    BUCKET_STYLES = {
        "triggered": ("rgba(92,201,138,0.08)", "rgba(92,201,138,0.25)"),
        "watch":     ("rgba(230,168,23,0.06)", "rgba(230,168,23,0.2)"),
        "ondeck":    ("rgba(74,158,218,0.06)", "rgba(74,158,218,0.2)"),
    }

    all_tiles = []  # collect (sym, detail_html) for JS

    def tile(t, meta, bucket, tile_id):
        sym        = t.get("ticker", "")
        score      = t.get("score", 0)
        scan       = t.get("scan", "")
        source     = t.get("source", "wl")
        sector     = t.get("sector", "")
        entry_lbl  = t.get("entry_label", "")
        flags      = t.get("flags", [])
        fib_levels = t.get("fib_levels", [])
        gap        = t.get("gap", 0)
        rvol_scan  = t.get("rvol", 0)
        news_url   = t.get("news_url", "")

        price      = meta.get("price", 0)
        vwap       = meta.get("vwap", 0)
        pct        = meta.get("pct_from_entry", 0)
        hod        = meta.get("hod", 0)
        above_vwap = meta.get("above_vwap", False)
        signals    = meta.get("signals", [])

        pct_str    = ("+{:.1f}%".format(pct * 100)) if pct >= 0 else ("{:.1f}%".format(pct * 100))
        pct_color  = "#5cc98a" if pct >= 0 else "#e05c5c"
        vwap_color = "#5cc98a" if above_vwap else "#e05c5c"
        vwap_lbl   = "above" if above_vwap else "below"
        scan_short = "LF" if "Low" in scan else ("MC" if "Mid" in scan else "LV")
        score_str  = " \xb7 {}".format(score) if score else ""

        sig_colors = ["#5cc98a", "#4a9eda", "#e6a817", "#c97dd4"]
        sigs_html  = "".join(
            '<span class="sig" style="color:{c};border-color:{c}">{s}</span>'.format(c=sig_colors[i % 4], s=s)
            for i, s in enumerate(signals)
        )
        if source == "live":
            sigs_html += '<span class="sig" style="color:#c97dd4;border-color:#c97dd4">LIVE</span>'

        bg, bd = BUCKET_STYLES.get(bucket, ("rgba(255,255,255,0.03)", "#1e2a1e"))

        # Tile HTML
        t_out = []
        t_out.append('<div class="tile" data-id="{}" style="background:{};border-color:{}" onclick="openDetail(\'{}\')">'.format(tile_id, bg, bd, tile_id))
        t_out.append('<div class="tile-top"><span class="tile-sym">{}</span><span class="tile-scan">{}{}</span></div>'.format(sym, scan_short, score_str))
        t_out.append('<div class="tile-pct" style="color:{c}">{p}</div>'.format(c=pct_color, p=pct_str))
        t_out.append('<div class="tile-price">${:.2f}</div>'.format(price))
        t_out.append('<div class="tile-vwap" style="color:{c}">VWAP ${:.2f} {l}</div>'.format(vwap, l=vwap_lbl, c=vwap_color))
        if sigs_html:
            t_out.append('<div class="tile-sigs">{}</div>'.format(sigs_html))
        t_out.append('</div>')

        # Detail panel HTML
        d_out = []
        d_out.append('<div class="detail-panel" id="detail-{}" style="display:none">'.format(tile_id))
        d_out.append('<div class="dp-hdr">')
        d_out.append('<div class="dp-title"><span class="dp-sym">{}</span> <span class="dp-co">{}</span></div>'.format(sym, sector))
        d_out.append('<button class="dp-close" onclick="closeDetail(\'{}\')">&#10005;</button>'.format(tile_id))
        d_out.append('</div>')

        # Key metrics row
        d_out.append('<div class="dp-metrics">')
        d_out.append('<div class="dp-met"><div class="dp-ml">Price</div><div class="dp-mv">${:.2f}</div></div>'.format(price))
        d_out.append('<div class="dp-met"><div class="dp-ml">vs Entry</div><div class="dp-mv" style="color:{c}">{p}</div></div>'.format(c=pct_color, p=pct_str))
        d_out.append('<div class="dp-met"><div class="dp-ml">VWAP</div><div class="dp-mv" style="color:{c}">${:.2f} {l}</div></div>'.format(vwap, l=vwap_lbl, c=vwap_color))
        d_out.append('<div class="dp-met"><div class="dp-ml">HOD</div><div class="dp-mv">${:.2f}</div></div>'.format(hod))
        if gap:
            d_out.append('<div class="dp-met"><div class="dp-ml">Gap</div><div class="dp-mv">{:+.1f}%</div></div>'.format(gap))
        if rvol_scan:
            d_out.append('<div class="dp-met"><div class="dp-ml">RVol at scan</div><div class="dp-mv">{:.1f}x</div></div>'.format(rvol_scan))
        d_out.append('</div>')

        # Entry label
        if entry_lbl:
            d_out.append('<div class="dp-entry">{}</div>'.format(entry_lbl))

        # Fib targets
        if fib_levels:
            d_out.append('<div class="dp-fibs">')
            for name, lvl in fib_levels:
                d_out.append('<div class="dp-fib"><span class="dp-fl">{}</span><span class="dp-fv">${:.2f}</span></div>'.format(name, lvl))
            d_out.append('</div>')

        # Flags
        if flags:
            d_out.append('<div class="dp-flags">')
            for f in flags:
                d_out.append('<span class="dp-flag">{}</span>'.format(f))
            d_out.append('</div>')

        # Signals
        if sigs_html:
            d_out.append('<div class="dp-sigs">{}</div>'.format(sigs_html))

        # Links row
        d_out.append('<div class="dp-links">')
        if news_url:
            d_out.append('<a class="dp-link news" href="{}" target="_blank">&#128240; Read News &#8599;</a>'.format(news_url))
        else:
            d_out.append('<span class="dp-link news-ph">&#128240; News link coming soon</span>')
        d_out.append('<a class="dp-link chart" href="https://finviz.com/quote.ashx?t={s}" target="_blank">Finviz &#8599;</a>'.format(s=sym))
        d_out.append('</div>')

        d_out.append('</div>')
        return "".join(t_out), "".join(d_out)

    def section(label, color, items, bucket):
        if not items:
            return ""
        tiles_html = ""
        details_html = ""
        for i, (t, meta) in enumerate(items):
            tile_id = "{}-{}".format(bucket, i)
            t_html, d_html = tile(t, meta, bucket, tile_id)
            tiles_html  += t_html
            details_html += d_html
        return (
            '<div class="section" data-bucket="{bk}">'
            '<div class="sec-hdr" style="color:{c}">{l} <span class="sec-cnt">{n}</span></div>'
            '<div class="tiles">{t}</div>'
            '{d}'
            '</div>'
        ).format(bk=bucket, c=color, l=label, n=len(items), t=tiles_html, d=details_html)

    css = "".join([
        ":root{--bg:#0a0f0a;--bg2:#0f160f;--bg3:#141c14;--bd:#1e2a1e;",
        "--green:#5cc98a;--gold:#e6a817;--blue:#4a9eda;--red:#e05c5c;",
        "--muted:#4a5a4a;--text:#c8d8c8;",
        "--mono:'SF Mono','Fira Mono',monospace;--sans:'Inter','Helvetica Neue',sans-serif;}",
        "*{box-sizing:border-box;margin:0;padding:0;}",
        "html,body{height:100%;overflow:hidden;}",
        "body{background:var(--bg);color:var(--text);font-family:var(--sans);font-size:13px;display:flex;flex-direction:column;}",
        ".hdr{background:var(--bg2);border-bottom:1px solid var(--bd);padding:10px 16px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0;}",
        ".hdr-l{display:flex;align-items:center;gap:8px;}",
        ".hdr-brand{font-size:12px;color:var(--muted);}",
        ".hdr-name{font-size:14px;font-weight:700;color:var(--green);letter-spacing:0.03em;}",
        ".hdr-r{display:flex;align-items:center;gap:8px;}",
        ".status-dot{width:6px;height:6px;border-radius:50%;}",
        ".status-lbl{font-family:var(--mono);font-size:9px;letter-spacing:0.1em;}",
        ".pill{font-family:var(--mono);font-size:10px;color:var(--muted);background:var(--bg3);border:1px solid var(--bd);border-radius:20px;padding:2px 8px;}",
        ".help-btn{font-family:var(--mono);font-size:10px;padding:3px 10px;border-radius:20px;background:rgba(74,158,218,0.1);color:var(--blue);border:1px solid rgba(74,158,218,0.25);cursor:pointer;}",
        ".help-btn:hover{background:rgba(74,158,218,0.2);}",
        ".help-panel{display:none;background:var(--bg2);border-bottom:1px solid var(--bd);padding:14px 20px;flex-shrink:0;}",
        ".help-panel.open{display:block;}",
        ".help-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px;}",
        ".help-item{display:flex;flex-direction:column;gap:3px;padding:8px 10px;background:var(--bg3);border-radius:6px;border:1px solid var(--bd);}",
        ".hk{font-size:11px;font-weight:700;font-family:var(--mono);}",
        ".hv{font-size:11px;color:var(--muted);line-height:1.4;}",
        ".strip{display:flex;border-bottom:1px solid var(--bd);flex-shrink:0;}",
        ".strip-item{flex:1;padding:8px 16px;display:flex;align-items:center;gap:8px;border-right:1px solid var(--bd);}",
        ".strip-item:last-child{border-right:none;}",
        ".strip-num{font-size:20px;font-weight:700;font-family:var(--mono);}",
        ".strip-lbl{font-size:10px;color:var(--muted);letter-spacing:0.06em;text-transform:uppercase;}",
        # Layout
        ".main{display:flex;flex:1;overflow:hidden;}",
        ".sidebar{width:120px;flex-shrink:0;border-right:1px solid var(--bd);background:var(--bg2);display:flex;flex-direction:column;padding:8px 0;}",
        ".cat-item{padding:10px 12px;font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;transition:background .1s;display:flex;justify-content:space-between;align-items:center;}",
        ".cat-item:hover,.cat-item.active{background:var(--bg3);color:#fff;}",
        ".cat-item.triggered:hover,.cat-item.triggered.active{color:#5cc98a;}",
        ".cat-item.watching:hover,.cat-item.watching.active{color:#e6a817;}",
        ".cat-item.ondeck:hover,.cat-item.ondeck.active{color:#4a9eda;}",
        ".cat-cnt{font-size:10px;font-family:var(--mono);color:var(--muted);font-weight:400;}",
        ".content{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:20px;}",
        # Sections
        ".section{display:flex;flex-direction:column;gap:10px;}",
        ".sec-hdr{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;display:flex;align-items:center;gap:8px;}",
        ".sec-cnt{font-size:10px;font-weight:400;color:var(--muted);font-family:var(--mono);}",
        ".tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;}",
        # Tiles
        ".tile{border:1px solid;border-radius:8px;padding:11px 12px;display:flex;flex-direction:column;gap:5px;cursor:pointer;transition:filter .15s;}",
        ".tile:hover{filter:brightness(1.2);}",
        ".tile.active{outline:2px solid #fff;outline-offset:2px;}",
        ".tile-top{display:flex;justify-content:space-between;align-items:baseline;}",
        ".tile-sym{font-size:14px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".tile-scan{font-size:9px;color:var(--muted);}",
        ".tile-pct{font-size:22px;font-weight:700;font-family:var(--mono);line-height:1;}",
        ".tile-price{font-size:11px;color:var(--text);font-family:var(--mono);}",
        ".tile-vwap{font-size:10px;}",
        ".tile-sigs{display:flex;flex-wrap:wrap;gap:3px;margin-top:2px;}",
        ".sig{font-size:8px;font-weight:700;letter-spacing:0.05em;padding:1px 4px;border-radius:4px;border:1px solid;}",
        # Detail panel
        ".detail-panel{background:var(--bg3);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-top:4px;display:flex;flex-direction:column;gap:12px;}",
        ".dp-hdr{display:flex;justify-content:space-between;align-items:flex-start;}",
        ".dp-title{display:flex;align-items:baseline;gap:8px;}",
        ".dp-sym{font-size:20px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".dp-co{font-size:12px;color:var(--muted);}",
        ".dp-close{background:none;border:1px solid var(--bd);border-radius:4px;color:var(--muted);cursor:pointer;font-size:11px;padding:2px 7px;}",
        ".dp-close:hover{color:var(--text);border-color:var(--muted);}",
        ".dp-metrics{display:flex;flex-wrap:wrap;gap:8px;}",
        ".dp-met{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;min-width:100px;}",
        ".dp-ml{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:3px;}",
        ".dp-mv{font-size:14px;font-weight:700;font-family:var(--mono);color:#fff;}",
        ".dp-entry{font-size:11px;color:var(--muted);font-family:var(--mono);background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:7px 10px;}",
        ".dp-fibs{display:flex;flex-wrap:wrap;gap:6px;}",
        ".dp-fib{display:flex;gap:6px;background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:5px 10px;align-items:baseline;}",
        ".dp-fl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}",
        ".dp-fv{font-size:12px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".dp-flags{display:flex;flex-wrap:wrap;gap:5px;}",
        ".dp-flag{font-size:10px;color:#5cc98a;border:1px solid rgba(92,201,138,0.3);border-radius:4px;padding:2px 8px;}",
        ".dp-sigs{display:flex;flex-wrap:wrap;gap:5px;}",
        ".dp-links{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}",
        ".dp-link{font-family:var(--mono);font-size:11px;padding:6px 14px;border-radius:6px;text-decoration:none;font-weight:600;}",
        ".dp-link.news{background:rgba(92,201,138,0.12);color:var(--green);border:1px solid rgba(92,201,138,0.3);}",
        ".dp-link.news:hover{background:rgba(92,201,138,0.2);}",
        ".dp-link.chart{background:rgba(74,158,218,0.1);color:var(--blue);border:1px solid rgba(74,158,218,0.25);}",
        ".dp-link.chart:hover{background:rgba(74,158,218,0.2);}",
        ".news-ph{font-family:var(--mono);font-size:11px;color:var(--muted);padding:6px 14px;border-radius:6px;border:1px solid var(--bd);font-style:italic;}",
        ".no-data{padding:40px;text-align:center;color:var(--muted);font-size:12px;}",
        "@media(max-width:700px){html,body{overflow:auto;}.main{flex-direction:column;overflow:visible;}",
        ".sidebar{width:100%;flex-direction:row;border-right:none;border-bottom:1px solid var(--bd);}",
        ".content{overflow:visible;}.tiles{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));}}",
    ])

    js = "\n".join([
        "function toggleHelp(){document.getElementById('help-panel').classList.toggle('open');}",
        "function selectCat(el,bucket){",
        "  document.querySelectorAll('.cat-item').forEach(c=>c.classList.remove('active'));",
        "  el.classList.add('active');",
        "  document.querySelectorAll('.section').forEach(s=>{",
        "    s.style.display=(bucket==='all'||s.dataset.bucket===bucket)?'flex':'none';",
        "  });",
        "}",
        "function openDetail(id){",
        "  document.querySelectorAll('.tile').forEach(t=>t.classList.remove('active'));",
        "  document.querySelectorAll('.detail-panel').forEach(d=>d.style.display='none');",
        "  var t=document.querySelector('.tile[data-id=\"'+id+'\"]');",
        "  var d=document.getElementById('detail-'+id);",
        "  if(t)t.classList.add('active');",
        "  if(d){d.style.display='flex';d.scrollIntoView({behavior:'smooth',block:'nearest'});}",
        "}",
        "function closeDetail(id){",
        "  var t=document.querySelector('.tile[data-id=\"'+id+'\"]');",
        "  var d=document.getElementById('detail-'+id);",
        "  if(t)t.classList.remove('active');",
        "  if(d)d.style.display='none';",
        "}",
    ])

    help_items = [
        ('<span class="hk" style="color:#5cc98a">Triggered</span>', "Price broke above the proposed entry. Actionable now."),
        ('<span class="hk" style="color:#e6a817">Watching</span>', "Within 5% of entry or above VWAP. Set an alert."),
        ('<span class="hk" style="color:#4a9eda">On Deck</span>', "On the morning WL, not yet moving. Watch for a volume spike."),
        ('<span class="hk">Entry Break</span>', "Price crossed the morning scorer entry level."),
        ('<span class="hk">Above VWAP</span>', "Holding above volume-weighted average price."),
        ('<span class="hk">Near HOD</span>', "Within 2% of the high of day."),
        ('<span class="hk">Live Mover</span>', "Not on morning list, found by live RVol sweep."),
        ('<span class="hk">LF / MC / LV</span>', "Low Float / Mid Cap / Live Mover scan type."),
    ]
    help_html = '<div class="help-panel" id="help-panel"><div class="help-grid">'
    for title, desc in help_items:
        help_html += '<div class="help-item">{}<span class="hv">{}</span></div>'.format(title, desc)
    help_html += '</div></div>'

    content_html = (
        section("Triggered", "#5cc98a", triggered, "triggered") +
        section("Watching",  "#e6a817", watch,     "watch") +
        section("On Deck",   "#4a9eda", ondeck,    "ondeck")
    )
    if not content_html:
        content_html = '<div class="no-data">No tickers yet. Run premarket scorer first.</div>'

    out = []
    out.append("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
    out.append("<meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n")
    out.append("<meta http-equiv=\"refresh\" content=\"300\">\n")
    out.append("<title>Watchlist \xb7 Scanner</title>\n")
    out.append("<style>{}</style>\n</head>\n<body>\n".format(css))
    out.append("<div class=\"hdr\">")
    out.append("<div class=\"hdr-l\"><span class=\"hdr-brand\">Watchlist \xb7</span>")
    out.append("<span class=\"hdr-name\">Intraday Scanner</span></div>")
    out.append("<div class=\"hdr-r\">")
    out.append("<span class=\"status-dot\" style=\"background:{sc}\"></span>".format(sc=status_color))
    out.append("<span class=\"status-lbl\" style=\"color:{sc}\">{sl}</span>".format(sc=status_color, sl=status_label))
    out.append("<span class=\"pill\">Updated {gt}</span>".format(gt=gen_time))
    out.append("<span class=\"pill\">{tot} tickers</span>".format(tot=total))
    out.append("<button class=\"help-btn\" onclick=\"toggleHelp()\">? How to use</button>")
    out.append("</div></div>\n")
    out.append(help_html)
    out.append("<div class=\"strip\">")
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--green)\">{}</span><span class=\"strip-lbl\">Triggered</span></div>".format(len(triggered)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--gold)\">{}</span><span class=\"strip-lbl\">Watching</span></div>".format(len(watch)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:var(--blue)\">{}</span><span class=\"strip-lbl\">On Deck</span></div>".format(len(ondeck)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" style=\"color:#fff\">{}</span><span class=\"strip-lbl\">Universe</span></div>".format(total))
    out.append("</div>\n")
    out.append("<div class=\"main\">")
    out.append("<div class=\"sidebar\">")
    out.append("<div class=\"cat-item active\" onclick=\"selectCat(this,'all')\">All<span class=\"cat-cnt\">{}</span></div>".format(total))
    out.append("<div class=\"cat-item triggered\" onclick=\"selectCat(this,'triggered')\">Triggered<span class=\"cat-cnt\">{}</span></div>".format(len(triggered)))
    out.append("<div class=\"cat-item watching\" onclick=\"selectCat(this,'watch')\">Watching<span class=\"cat-cnt\">{}</span></div>".format(len(watch)))
    out.append("<div class=\"cat-item ondeck\" onclick=\"selectCat(this,'ondeck')\">On Deck<span class=\"cat-cnt\">{}</span></div>".format(len(ondeck)))
    out.append("</div>")
    out.append("<div class=\"content\">{}</div>".format(content_html))
    out.append("</div>\n")
    out.append("<script>{}</script>\n</body>\n</html>".format(js))
    return "".join(out)


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
