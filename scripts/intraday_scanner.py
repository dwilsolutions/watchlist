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
    """
    Renders a static shell page. On load the browser fetches scanner.json
    for ticker/entry data, then polls Yahoo Finance every 60s for live prices.
    No page reload needed — tiles update in place.
    """
    triggered = buckets.get("triggered", [])
    watch     = buckets.get("watch", [])
    ondeck    = buckets.get("ondeck", [])
    total     = len(triggered) + len(watch) + len(ondeck)

    # Embed bucket membership so the page knows which section each ticker belongs to
    import json as _json
    def ticker_seed(t, meta, bucket):
        return {
            "ticker":      t.get("ticker", ""),
            "company":     t.get("company", ""),
            "scan":        t.get("scan", ""),
            "score":       t.get("score", 0),
            "entry":       float(t.get("entry", 0) or 0),
            "entry_label": t.get("entry_label", ""),
            "flags":       t.get("flags", []),
            "fib_levels":  t.get("fib_levels", []),
            "gap":         t.get("gap", 0),
            "rvol":        t.get("rvol", 0),
            "sector":      t.get("sector", ""),
            "news_url":    t.get("news_url", ""),
            "source":      t.get("source", "wl"),
            "bucket":      bucket,
            "vwap_static": meta.get("vwap", 0),
            "hod_static":  meta.get("hod", 0),
        }

    seed_data = (
        [ticker_seed(t, m, "triggered") for t, m in triggered] +
        [ticker_seed(t, m, "watch")     for t, m in watch] +
        [ticker_seed(t, m, "ondeck")    for t, m in ondeck]
    )
    seed_json = _json.dumps(seed_data).replace("</", "<\\/")

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
        ".status-dot{width:6px;height:6px;border-radius:50%;background:var(--green);}",
        ".status-lbl{font-family:var(--mono);font-size:9px;letter-spacing:0.1em;color:var(--green);}",
        ".pill{font-family:var(--mono);font-size:10px;color:var(--muted);background:var(--bg3);border:1px solid var(--bd);border-radius:20px;padding:2px 8px;}",
        ".live-badge{font-family:var(--mono);font-size:9px;padding:2px 8px;border-radius:20px;background:rgba(92,201,138,0.12);color:var(--green);border:1px solid rgba(92,201,138,0.3);letter-spacing:0.06em;}",
        ".last-update{font-family:var(--mono);font-size:10px;color:var(--muted);}",
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
        ".main{display:flex;flex:1;overflow:hidden;}",
        ".sidebar{width:120px;flex-shrink:0;border-right:1px solid var(--bd);background:var(--bg2);display:flex;flex-direction:column;padding:8px 0;}",
        ".cat-item{padding:10px 12px;font-size:12px;font-weight:600;color:var(--muted);cursor:pointer;transition:background .1s;display:flex;justify-content:space-between;align-items:center;}",
        ".cat-item:hover,.cat-item.active{background:var(--bg3);color:#fff;}",
        ".cat-item.triggered:hover,.cat-item.triggered.active{color:#5cc98a;}",
        ".cat-item.watching:hover,.cat-item.watching.active{color:#e6a817;}",
        ".cat-item.ondeck:hover,.cat-item.ondeck.active{color:#4a9eda;}",
        ".cat-cnt{font-size:10px;font-family:var(--mono);color:var(--muted);font-weight:400;}",
        ".content{flex:1;overflow-y:auto;padding:16px 20px;display:flex;flex-direction:column;gap:20px;}",
        ".section{display:flex;flex-direction:column;gap:10px;}",
        ".sec-hdr{font-size:10px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;display:flex;align-items:center;gap:8px;}",
        ".sec-cnt{font-size:10px;font-weight:400;color:var(--muted);font-family:var(--mono);}",
        ".tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px;}",
        ".tile{border:1px solid;border-radius:8px;padding:11px 12px;display:flex;flex-direction:column;gap:5px;cursor:pointer;transition:filter .15s;}",
        ".tile:hover{filter:brightness(1.2);}",
        ".tile.active{outline:2px solid rgba(255,255,255,0.4);outline-offset:2px;}",
        ".tile.stale{opacity:0.5;}",
        ".tile-top{display:flex;justify-content:space-between;align-items:baseline;}",
        ".tile-sym{font-size:14px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".tile-scan{font-size:9px;color:var(--muted);}",
        ".tile-pct{font-size:22px;font-weight:700;font-family:var(--mono);line-height:1;transition:color .3s;}",
        ".tile-price{font-size:11px;color:var(--text);font-family:var(--mono);}",
        ".tile-vwap{font-size:10px;}",
        ".tile-sigs{display:flex;flex-wrap:wrap;gap:3px;margin-top:2px;}",
        ".sig{font-size:8px;font-weight:700;letter-spacing:0.05em;padding:1px 4px;border-radius:4px;border:1px solid;}",
        ".detail-panel{background:var(--bg3);border:1px solid var(--bd);border-radius:10px;padding:16px;margin-top:4px;display:flex;flex-direction:column;gap:12px;}",
        ".dp-hdr{display:flex;justify-content:space-between;align-items:flex-start;}",
        ".dp-title{display:flex;align-items:baseline;gap:8px;}",
        ".dp-sym{font-size:20px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".dp-co{font-size:12px;color:var(--muted);}",
        ".dp-close{background:none;border:1px solid var(--bd);border-radius:4px;color:var(--muted);cursor:pointer;font-size:11px;padding:2px 7px;}",
        ".dp-close:hover{color:var(--text);}",
        ".dp-metrics{display:flex;flex-wrap:wrap;gap:8px;}",
        ".dp-met{background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:8px 12px;min-width:90px;}",
        ".dp-ml{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.07em;margin-bottom:3px;}",
        ".dp-mv{font-size:14px;font-weight:700;font-family:var(--mono);color:#fff;}",
        ".dp-entry{font-size:11px;color:var(--muted);font-family:var(--mono);background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:7px 10px;}",
        ".dp-fibs{display:flex;flex-wrap:wrap;gap:6px;}",
        ".dp-fib{display:flex;gap:6px;background:var(--bg2);border:1px solid var(--bd);border-radius:6px;padding:5px 10px;align-items:baseline;}",
        ".dp-fl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}",
        ".dp-fv{font-size:12px;font-weight:700;color:#fff;font-family:var(--mono);}",
        ".dp-flags{display:flex;flex-wrap:wrap;gap:5px;}",
        ".dp-flag{font-size:10px;color:#5cc98a;border:1px solid rgba(92,201,138,0.3);border-radius:4px;padding:2px 8px;}",
        ".dp-links{display:flex;gap:8px;flex-wrap:wrap;}",
        ".dp-link{font-family:var(--mono);font-size:11px;padding:6px 14px;border-radius:6px;text-decoration:none;font-weight:600;}",
        ".dp-link.news{background:rgba(92,201,138,0.12);color:var(--green);border:1px solid rgba(92,201,138,0.3);}",
        ".dp-link.news:hover{background:rgba(92,201,138,0.2);}",
        ".dp-link.finviz{background:rgba(74,158,218,0.1);color:var(--blue);border:1px solid rgba(74,158,218,0.25);}",
        ".dp-link.finviz:hover{background:rgba(74,158,218,0.2);}",
        ".news-ph{font-family:var(--mono);font-size:11px;color:var(--muted);padding:6px 14px;border-radius:6px;border:1px solid var(--bd);font-style:italic;}",
        ".loading{padding:40px;text-align:center;color:var(--muted);font-size:12px;}",
        ".error{padding:12px 16px;background:rgba(224,92,92,0.1);border:1px solid rgba(224,92,92,0.3);border-radius:6px;font-size:11px;color:var(--red);}",
        "@media(max-width:700px){html,body{overflow:auto;}.main{flex-direction:column;overflow:visible;}",
        ".sidebar{width:100%;flex-direction:row;border-right:none;border-bottom:1px solid var(--bd);}",
        ".content{overflow:visible;}.tiles{grid-template-columns:repeat(auto-fill,minmax(110px,1fr));}}",
    ])

    js = r"""
const SEED = """ + seed_json + r""";
const REFRESH_MS = 60000; // 60 seconds
const BUCKET_COLORS = {triggered:'rgba(92,201,138,0.08)',watch:'rgba(230,168,23,0.06)',ondeck:'rgba(74,158,218,0.06)'};
const BUCKET_BORDERS = {triggered:'rgba(92,201,138,0.25)',watch:'rgba(230,168,23,0.2)',ondeck:'rgba(74,158,218,0.2)'};
const SIG_COLORS = ['#5cc98a','#4a9eda','#e6a817','#c97dd4'];

let liveData = {}; // sym -> {price, vwap, pct, above_vwap, signals}
let activeTile = null;

// ── Build initial DOM from seed data ────────────────────────────────────────
function buildUI() {
  const sections = {triggered:[], watch:[], ondeck:[]};
  SEED.forEach(t => { if(sections[t.bucket]) sections[t.bucket].push(t); });

  const content = document.getElementById('content');
  const labels = {triggered:['Triggered','#5cc98a'], watch:['Watching','#e6a817'], ondeck:['On Deck','#4a9eda']};

  ['triggered','watch','ondeck'].forEach(bucket => {
    const items = sections[bucket];
    if(!items.length) return;
    const [label, color] = labels[bucket];
    const sec = document.createElement('div');
    sec.className = 'section';
    sec.dataset.bucket = bucket;
    sec.innerHTML = `<div class="sec-hdr" style="color:${color}">${label} <span class="sec-cnt">${items.length}</span></div>`;
    const tilesDiv = document.createElement('div');
    tilesDiv.className = 'tiles';
    items.forEach(t => {
      tilesDiv.appendChild(buildTile(t));
      const det = buildDetail(t);
      sec.appendChild(tilesDiv);
      sec.appendChild(det);
    });
    if(!sec.contains(tilesDiv)) sec.appendChild(tilesDiv);
    content.appendChild(sec);
  });

  updateCounts();
}

function buildTile(t) {
  const el = document.createElement('div');
  el.className = 'tile stale';
  el.id = 'tile-' + t.ticker;
  el.dataset.ticker = t.ticker;
  el.dataset.bucket = t.bucket;
  el.style.background = BUCKET_COLORS[t.bucket] || 'transparent';
  el.style.borderColor = BUCKET_BORDERS[t.bucket] || '#1e2a1e';
  el.style.border = '1px solid';
  el.onclick = () => openDetail(t.ticker);
  const scanShort = t.scan.includes('Low') ? 'LF' : (t.scan.includes('Mid') ? 'MC' : 'LV');
  const scoreStr = t.score ? ` \u00b7 ${t.score}` : '';
  el.innerHTML = `
    <div class="tile-top"><span class="tile-sym">${t.ticker}</span><span class="tile-scan">${scanShort}${scoreStr}</span></div>
    <div class="tile-pct" id="pct-${t.ticker}" style="color:var(--muted)">--</div>
    <div class="tile-price" id="price-${t.ticker}">--</div>
    <div class="tile-vwap" id="vwap-${t.ticker}" style="color:var(--muted)">VWAP --</div>
    <div class="tile-sigs" id="sigs-${t.ticker}"></div>`;
  return el;
}

function buildDetail(t) {
  const el = document.createElement('div');
  el.className = 'detail-panel';
  el.id = 'detail-' + t.ticker;
  el.style.display = 'none';

  const fibs = t.fib_levels.slice(0,4).map(([name,lvl]) =>
    `<div class="dp-fib"><span class="dp-fl">${name}</span><span class="dp-fv">$${Number(lvl).toFixed(2)}</span></div>`
  ).join('');

  const flags = t.flags.map(f => `<span class="dp-flag">${f}</span>`).join('');

  const newsLink = t.news_url
    ? `<a class="dp-link news" href="${t.news_url}" target="_blank">&#128240; Read News &#8599;</a>`
    : `<span class="news-ph">&#128240; News link coming soon</span>`;

  el.innerHTML = `
    <div class="dp-hdr">
      <div class="dp-title"><span class="dp-sym">${t.ticker}</span><span class="dp-co">${t.sector}</span></div>
      <button class="dp-close" onclick="closeDetail('${t.ticker}')">&#10005;</button>
    </div>
    <div class="dp-metrics" id="dp-metrics-${t.ticker}">
      <div class="dp-met"><div class="dp-ml">Price</div><div class="dp-mv" id="dp-price-${t.ticker}">--</div></div>
      <div class="dp-met"><div class="dp-ml">vs Entry</div><div class="dp-mv" id="dp-pct-${t.ticker}">--</div></div>
      <div class="dp-met"><div class="dp-ml">VWAP</div><div class="dp-mv" id="dp-vwap-${t.ticker}">--</div></div>
      <div class="dp-met"><div class="dp-ml">HOD</div><div class="dp-mv" id="dp-hod-${t.ticker}">$${Number(t.hod_static).toFixed(2)}</div></div>
      ${t.gap ? `<div class="dp-met"><div class="dp-ml">Gap</div><div class="dp-mv">${t.gap > 0 ? '+' : ''}${Number(t.gap).toFixed(1)}%</div></div>` : ''}
      ${t.rvol ? `<div class="dp-met"><div class="dp-ml">RVol at scan</div><div class="dp-mv">${Number(t.rvol).toFixed(1)}x</div></div>` : ''}
    </div>
    ${t.entry_label ? `<div class="dp-entry">${t.entry_label}</div>` : ''}
    ${fibs ? `<div class="dp-fibs">${fibs}</div>` : ''}
    ${flags ? `<div class="dp-flags">${flags}</div>` : ''}
    <div class="dp-links">
      ${newsLink}
      <a class="dp-link finviz" href="https://finviz.com/quote.ashx?t=${t.ticker}" target="_blank">Finviz &#8599;</a>
    </div>`;
  return el;
}

// ── Live price fetching via Yahoo Finance ────────────────────────────────────
async function fetchPrices() {
  const syms = SEED.map(t => t.ticker);
  if(!syms.length) return;

  // Yahoo Finance v8 chart endpoint — no API key needed, CORS allowed
  const chunk = syms.join(',');
  const url = `https://query1.finance.yahoo.com/v7/finance/quote?symbols=${chunk}&fields=regularMarketPrice,regularMarketVolume`;

  try {
    const res = await fetch(url);
    if(!res.ok) throw new Error('HTTP ' + res.status);
    const json = await res.json();
    const quotes = json?.quoteResponse?.result || [];
    quotes.forEach(q => {
      const sym = q.symbol;
      const price = q.regularMarketPrice || 0;
      const seed  = SEED.find(t => t.ticker === sym);
      if(!seed || !price) return;

      const entry = seed.entry || 0;
      const vwap  = seed.vwap_static || price; // use static VWAP as proxy
      const pct   = entry > 0 ? (price - entry) / entry : 0;
      const aboveVwap = price >= vwap;

      // Classify signals
      const signals = [];
      if(price >= entry && entry > 0) signals.push('ENTRY BREAK');
      if(aboveVwap) signals.push('ABOVE VWAP');
      if(seed.hod_static && price >= seed.hod_static * 0.98) signals.push('NEAR HOD');

      liveData[sym] = {price, vwap, pct, aboveVwap, signals};
      updateTile(sym, seed);
    });
    document.getElementById('last-update').textContent = 'Live · ' + new Date().toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit',hour12:true});
  } catch(e) {
    console.warn('Price fetch failed:', e);
    document.getElementById('last-update').textContent = 'Fetch failed · retrying...';
  }
}

function updateTile(sym, seed) {
  const d = liveData[sym];
  if(!d) return;
  const tile = document.getElementById('tile-' + sym);
  if(tile) tile.classList.remove('stale');

  const pctStr = (d.pct >= 0 ? '+' : '') + (d.pct * 100).toFixed(1) + '%';
  const pctColor = d.pct >= 0 ? '#5cc98a' : '#e05c5c';
  const vwapColor = d.aboveVwap ? '#5cc98a' : '#e05c5c';
  const vwapLbl = d.aboveVwap ? 'above' : 'below';

  const sig_colors = ['#5cc98a','#4a9eda','#e6a817','#c97dd4'];
  const sigsHtml = d.signals.map((s,i) =>
    `<span class="sig" style="color:${sig_colors[i%4]};border-color:${sig_colors[i%4]}">${s}</span>`
  ).join('') + (seed.source === 'live' ? '<span class="sig" style="color:#c97dd4;border-color:#c97dd4">LIVE</span>' : '');

  // Reclassify bucket based on live price
  const newBucket = d.pct >= 0 ? 'triggered' : (d.pct >= -0.05 || d.aboveVwap ? 'watch' : 'ondeck');
  if(tile) {
    tile.style.background = BUCKET_COLORS[newBucket];
    tile.style.borderColor = BUCKET_BORDERS[newBucket];
  }

  const pEl = document.getElementById('pct-' + sym);
  if(pEl){ pEl.textContent = pctStr; pEl.style.color = pctColor; }
  const prEl = document.getElementById('price-' + sym);
  if(prEl) prEl.textContent = '$' + d.price.toFixed(2);
  const vEl = document.getElementById('vwap-' + sym);
  if(vEl){ vEl.textContent = `VWAP $${d.vwap.toFixed(2)} ${vwapLbl}`; vEl.style.color = vwapColor; }
  const sEl = document.getElementById('sigs-' + sym);
  if(sEl) sEl.innerHTML = sigsHtml;

  // Update detail panel if open
  const dpPrice = document.getElementById('dp-price-' + sym);
  const dpPct   = document.getElementById('dp-pct-' + sym);
  const dpVwap  = document.getElementById('dp-vwap-' + sym);
  if(dpPrice) dpPrice.textContent = '$' + d.price.toFixed(2);
  if(dpPct){ dpPct.textContent = pctStr; dpPct.style.color = pctColor; }
  if(dpVwap){ dpVwap.textContent = `$${d.vwap.toFixed(2)} ${vwapLbl}`; dpVwap.style.color = vwapColor; }
}

// ── UI interactions ──────────────────────────────────────────────────────────
function openDetail(sym) {
  if(activeTile) {
    document.getElementById('tile-' + activeTile)?.classList.remove('active');
    document.getElementById('detail-' + activeTile).style.display = 'none';
  }
  if(activeTile === sym){ activeTile = null; return; }
  activeTile = sym;
  document.getElementById('tile-' + sym)?.classList.add('active');
  const det = document.getElementById('detail-' + sym);
  det.style.display = 'flex';
  det.scrollIntoView({behavior:'smooth', block:'nearest'});
}

function closeDetail(sym) {
  document.getElementById('tile-' + sym)?.classList.remove('active');
  document.getElementById('detail-' + sym).style.display = 'none';
  if(activeTile === sym) activeTile = null;
}

function selectCat(el, bucket) {
  document.querySelectorAll('.cat-item').forEach(c => c.classList.remove('active'));
  el.classList.add('active');
  document.querySelectorAll('.section').forEach(s => {
    s.style.display = (bucket === 'all' || s.dataset.bucket === bucket) ? 'flex' : 'none';
  });
}

function updateCounts() {
  const counts = {triggered:0, watch:0, ondeck:0};
  SEED.forEach(t => { if(counts[t.bucket] !== undefined) counts[t.bucket]++; });
  document.getElementById('cnt-all').textContent = SEED.length;
  document.getElementById('cnt-triggered').textContent = counts.triggered;
  document.getElementById('cnt-watch').textContent = counts.watch;
  document.getElementById('cnt-ondeck').textContent = counts.ondeck;
  document.getElementById('strip-triggered').textContent = counts.triggered;
  document.getElementById('strip-watch').textContent = counts.watch;
  document.getElementById('strip-ondeck').textContent = counts.ondeck;
  document.getElementById('strip-total').textContent = SEED.length;
}

function toggleHelp() { document.getElementById('help-panel').classList.toggle('open'); }

// ── Init ─────────────────────────────────────────────────────────────────────
buildUI();
fetchPrices();
setInterval(fetchPrices, REFRESH_MS);
"""

    help_items = [
        ('<span class="hk" style="color:#5cc98a">Triggered</span>', "Price broke above the proposed entry. Updates live every 60s."),
        ('<span class="hk" style="color:#e6a817">Watching</span>', "Within 5% of entry or above VWAP. Set an alert."),
        ('<span class="hk" style="color:#4a9eda">On Deck</span>', "On the morning WL, not yet moving."),
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

    out = []
    out.append("<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n")
    out.append("<meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\n")
    out.append("<title>Watchlist \xb7 Scanner</title>\n")
    out.append("<style>{}</style>\n</head>\n<body>\n".format(css))
    out.append("<div class=\"hdr\">")
    out.append("<div class=\"hdr-l\"><span class=\"hdr-brand\">Watchlist \xb7</span>")
    out.append("<span class=\"hdr-name\">Intraday Scanner</span></div>")
    out.append("<div class=\"hdr-r\">")
    out.append("<span class=\"status-dot\"></span>")
    out.append("<span class=\"status-lbl\">LIVE</span>")
    out.append("<span class=\"live-badge\">&#9679; 60s refresh</span>")
    out.append("<span class=\"last-update\" id=\"last-update\">Fetching...</span>")
    out.append("<span class=\"pill\">Built {gt}</span>".format(gt=gen_time))
    out.append("<button class=\"help-btn\" onclick=\"toggleHelp()\">? How to use</button>")
    out.append("</div></div>\n")
    out.append(help_html)
    out.append("<div class=\"strip\">")
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" id=\"strip-triggered\" style=\"color:var(--green)\">{}</span><span class=\"strip-lbl\">Triggered</span></div>".format(len(triggered)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" id=\"strip-watch\" style=\"color:var(--gold)\">{}</span><span class=\"strip-lbl\">Watching</span></div>".format(len(watch)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" id=\"strip-ondeck\" style=\"color:var(--blue)\">{}</span><span class=\"strip-lbl\">On Deck</span></div>".format(len(ondeck)))
    out.append("<div class=\"strip-item\"><span class=\"strip-num\" id=\"strip-total\" style=\"color:#fff\">{}</span><span class=\"strip-lbl\">Universe</span></div>".format(total))
    out.append("</div>\n")
    out.append("<div class=\"main\">")
    out.append("<div class=\"sidebar\">")
    out.append("<div class=\"cat-item active\" onclick=\"selectCat(this,'all')\">All<span class=\"cat-cnt\" id=\"cnt-all\">{}</span></div>".format(total))
    out.append("<div class=\"cat-item triggered\" onclick=\"selectCat(this,'triggered')\">Triggered<span class=\"cat-cnt\" id=\"cnt-triggered\">{}</span></div>".format(len(triggered)))
    out.append("<div class=\"cat-item watching\" onclick=\"selectCat(this,'watch')\">Watching<span class=\"cat-cnt\" id=\"cnt-watch\">{}</span></div>".format(len(watch)))
    out.append("<div class=\"cat-item ondeck\" onclick=\"selectCat(this,'ondeck')\">On Deck<span class=\"cat-cnt\" id=\"cnt-ondeck\">{}</span></div>".format(len(ondeck)))
    out.append("</div>")
    out.append("<div class=\"content\" id=\"content\"></div>")
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
    def ticker_snap(t, meta):
        return {
            "ticker":      t.get("ticker", ""),
            "company":     t.get("company", ""),
            "scan":        t.get("scan", ""),
            "score":       t.get("score", 0),
            "entry":       float(t.get("entry", 0) or 0),
            "entry_label": t.get("entry_label", ""),
            "flags":       t.get("flags", []),
            "fib_levels":  t.get("fib_levels", []),
            "gap":         t.get("gap", 0),
            "rvol":        t.get("rvol", 0),
            "sector":      t.get("sector", ""),
            "news_url":    t.get("news_url", ""),
            "source":      t.get("source", "wl"),
            "vwap_static": meta.get("vwap", 0),
            "hod_static":  meta.get("hod", 0),
        }

    snap = {
        "date":      today.isoformat(),
        "generated": gen_time,
        "status":    market_status,
        "triggered": [ticker_snap(t, meta) for t, meta in buckets["triggered"]],
        "watch":     [ticker_snap(t, meta) for t, meta in buckets["watch"]],
        "ondeck":    [ticker_snap(t, meta) for t, meta in buckets["ondeck"]],
    }
    with open(os.path.join(DATA_DIR, "scanner.json"), "w") as f:
        json.dump(snap, f, indent=2)

    print(f"\nDone. URL: https://dwilsolutions.github.io/watchlist/scanner.html\n")

if __name__ == "__main__":
    main()
