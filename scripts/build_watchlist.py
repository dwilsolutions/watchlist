"""
Swing Watchlist HTML Builder
Reads the scanner JSON output and renders swing-trading.html
"""

import os, sys, json, glob
from datetime import date

DATA_DIR = os.environ.get("SWING_DATA_DIR", "docs/data/swing")
OUT_DIR  = os.environ.get("SWING_OUT_DIR",  "docs")

# ── Styles ─────────────────────────────────────────────────────────────────────

RANK_CFG = {
    "hot":   ("#1e3d2a", "#6ee89a", "🔥 HOT"),
    "warm":  ("#3d2e1a", "#f5c46e", "⚡ WARM"),
    "watch": ("#1a2a3a", "#7ab4f5", "👁 WATCH"),
    "avoid": ("#3d1a1a", "#f57a7a", "✗ AVOID"),
}

SCAN_COLORS = {
    "Low Float": ("#1a2a3d", "#7ab4f5"),
    "Mid Float": ("#2a1e3d", "#b07af5"),
    "Manual":    ("#1a2a1a", "#7af5a0"),
}

FLAG_CFG = {
    "catalyst": ("#3d2e1a", "#f5c46e"),
    "gap":      ("#1a2a3d", "#7ab4f5"),
    "danger":   ("#3d1a1a", "#f57a7a"),
    "cont":     ("#1e3d2a", "#6ee89a"),
}

CSS = """
:root{--bg:#0c0e11;--bg2:#141618;--bg3:#1c1f23;--border:rgba(255,255,255,0.07);--text:#dde1e9;--muted:#656c7a;--green:#3a9c5f;--amber:#c07b1a;--red:#a33333;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.5;}
a{color:inherit;text-decoration:none;}
.hdr{background:var(--bg2);border-bottom:2px solid #3a9c5f;padding:16px 20px;display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.hdr-l h1{font-family:var(--sans);font-size:20px;font-weight:700;letter-spacing:-0.3px;}
.hdr-l h1 em{color:#3a9c5f;font-style:normal;}
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
.body{padding:18px 20px 48px;max-width:940px;margin:0 auto;}
.sec-lbl{font-size:10px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;margin:22px 0 9px;display:flex;align-items:center;gap:8px;}
.sec-lbl::after{content:'';flex:1;height:1px;background:var(--border);}
.cards{display:flex;flex-direction:column;gap:9px;}
.card{background:var(--bg2);border:0.5px solid var(--border);border-radius:10px;padding:13px 15px;border-left-width:3px;border-left-style:solid;}
.card.hot{border-left-color:#6ee89a;}.card.warm{border-left-color:#f5c46e;}.card.watch{border-left-color:#7ab4f5;}.card.avoid{border-left-color:#f57a7a;opacity:0.55;}
.card.manual-pin{border-top:1px solid #3a9c5f44;}
.r1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.tkr{font-family:var(--sans);font-size:15px;font-weight:700;}
.co{font-size:11px;color:var(--muted);flex:1;}
.scan-tag{font-size:10px;padding:2px 7px;border-radius:10px;border:0.5px solid;}
.rank-pill{font-size:11px;font-weight:500;padding:2px 9px;border-radius:20px;}
.score-bar-wrap{display:flex;align-items:center;gap:6px;margin-bottom:8px;}
.score-bar-bg{flex:1;height:4px;background:var(--bg3);border-radius:3px;overflow:hidden;}
.score-bar-fill{height:100%;border-radius:3px;background:#3a9c5f;}
.score-val{font-size:10px;color:var(--muted);min-width:28px;text-align:right;}
.flags{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px;}
.flag{font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500;}
.manual-badge{font-size:10px;padding:2px 7px;border-radius:10px;background:#1a2a1a;color:#7af5a0;font-weight:500;}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:5px;margin:8px 0;}
.st{background:var(--bg3);border-radius:6px;padding:5px 8px;}
.sl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}
.sv{font-size:12px;font-weight:500;color:var(--text);margin-top:1px;}
.score-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin:8px 0;}
.sc{background:var(--bg3);border-radius:6px;padding:4px 7px;}
.scl{font-size:9px;color:var(--muted);}
.scv{font-size:11px;font-weight:500;}
.scv.hit{color:#6ee89a;}.scv.miss{color:var(--muted);}
.news-line{font-size:10px;color:var(--muted);font-style:italic;border-top:0.5px solid var(--border);padding-top:5px;margin-top:4px;display:flex;align-items:center;gap:8px;overflow:hidden;}
.news-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;}
.news-btn{flex-shrink:0;font-size:10px;font-style:normal;padding:2px 8px;border-radius:20px;background:#1a2a3d;color:#7ab4f5;border:0.5px solid #7ab4f533;text-decoration:none;white-space:nowrap;}
.ch{font-size:12px;font-weight:500;}.pos{color:#5cc98a;}.neg{color:#e06060;}
.clink{font-size:11px;color:#5a8fd4;margin-left:auto;}
.footer{text-align:center;font-size:10px;color:var(--muted);padding:24px;border-top:1px solid var(--border);}
.empty{color:var(--muted);font-size:12px;padding:8px 0;}
"""

# ── Card HTML ──────────────────────────────────────────────────────────────────

def flag_html(label, kind):
    bg, tx = FLAG_CFG.get(kind, ("#222", "#aaa"))
    return f'<span class="flag" style="background:{bg};color:{tx}">{label}</span>'

def card_html(t):
    rank              = t.get("rank", "watch")
    rank_bg, rank_tx, rank_lbl = RANK_CFG[rank]
    scan_bg, scan_tx  = SCAN_COLORS.get(t["scan"], ("#1c1f23", "#656c7a"))
    ticker            = t["ticker"]
    chg_sign          = "+" if t["change"] >= 0 else ""
    chg_cls           = "pos" if t["change"] >= 0 else "neg"
    score_pct         = min(100, round(t["score"] * 100))
    flags_html        = "".join(flag_html(l, k) for l, k in t["flags"])
    manual_html       = '<span class="manual-badge">📌 WATCHLIST</span>' if t.get("manual") else ""
    manual_cls        = " manual-pin" if t.get("manual") else ""

    # Score component grid
    comps = t.get("components", {})
    COMP_LABELS = {
        "fresh_8k":       "8-K",
        "merger_pivot":   "Catalyst",
        "previous_spike": "Prev Spike",
        "short_squeeze":  "Short Sq.",
        "high_rel_vol":   "RVol",
        "fda_binary":     "FDA",
    }
    comp_html = "".join(
        f'<div class="sc"><div class="scl">{COMP_LABELS.get(k, k)}</div>'
        f'<div class="scv {"hit" if v > 0 else "miss"}">{"✓" if v >= 1.0 else ("~" if v > 0 else "✗")}</div></div>'
        for k, v in comps.items()
    )

    # News line
    news     = t.get("news", "")
    news_url = t.get("news_url", "")
    if news_url and news_url.startswith("http"):
        news_html = f'<span class="news-txt">{news}</span><a class="news-btn" href="{news_url}" target="_blank">Read ↗</a>'
    else:
        news_html = f'<span class="news-txt">{news}</span>'

    short_str = f'{t["short_pct"]:.0f}%' if t.get("short_pct", 0) > 0 else "—"
    spike_str = f'+{t["spike_pct"]:.0f}%' if t.get("spike_pct", 0) > 0 else "—"

    return f"""<div class="card {rank}{manual_cls}">
  <div class="r1">
    <span class="tkr">{ticker}</span>
    <span class="co">{t["company"][:24]}</span>
    <span class="scan-tag" style="background:{scan_bg};color:{scan_tx};border-color:{scan_tx}44">{t["scan"]}</span>
    <span class="rank-pill" style="background:{rank_bg};color:{rank_tx}">{rank_lbl}</span>
    <span class="ch {chg_cls}">{chg_sign}{t["change"]:.1f}%</span>
    <a class="clink" href="https://finviz.com/quote.ashx?t={ticker}" target="_blank">Chart ↗</a>
  </div>
  <div class="score-bar-wrap">
    <div class="score-bar-bg"><div class="score-bar-fill" style="width:{score_pct}%"></div></div>
    <span class="score-val">{score_pct}%</span>
  </div>
  <div class="flags">{manual_html}{flags_html}</div>
  <div class="stats">
    <div class="st"><div class="sl">Price</div><div class="sv">${t["price"]:.2f}</div></div>
    <div class="st"><div class="sl">RVol</div><div class="sv">{t["rvol"]:.0f}x</div></div>
    <div class="st"><div class="sl">Float</div><div class="sv">{t["float_m"]:.1f}M</div></div>
    <div class="st"><div class="sl">Short Int</div><div class="sv">{short_str}</div></div>
    <div class="st"><div class="sl">90d Spike</div><div class="sv">{spike_str}</div></div>
  </div>
  <div class="score-grid">{comp_html}</div>
  <div class="news-line">{news_html}</div>
</div>"""

# ── Render ─────────────────────────────────────────────────────────────────────

def render_html(data):
    tickers  = data["tickers"]
    today    = data["date"]
    session  = data["session"]
    gen_time = data["generated"]

    session_label = "Pre-Market Scan" if session == "premarket" else "Mid-Morning Scan"

    hot   = [t for t in tickers if t.get("rank") == "hot"   and not t.get("manual")]
    warm  = [t for t in tickers if t.get("rank") == "warm"  and not t.get("manual")]
    watch = [t for t in tickers if t.get("rank") == "watch" and not t.get("manual")]
    manual = [t for t in tickers if t.get("manual")]
    total = len(tickers)

    hot_cards   = "".join(card_html(t) for t in hot)   or '<p class="empty">No HOT setups today.</p>'
    warm_cards  = "".join(card_html(t) for t in warm)  or '<p class="empty">No WARM setups today.</p>'
    watch_cards = "".join(card_html(t) for t in watch) or ""
    manual_cards = "".join(card_html(t) for t in manual)

    from datetime import datetime
    try:
        d = datetime.fromisoformat(today)
        today_fmt = d.strftime("%a %b %-d, %Y")
    except:
        today_fmt = today

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swing Watchlist · {today_fmt}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:ital,wght@0,400;0,500;1,400&family=Syne:wght@500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="hdr">
  <div class="hdr-l">
    <h1>Swing Watchlist · <em>{session_label}</em></h1>
    <div class="sub">Micro-cap momentum · 50-100% target moves · days to weeks</div>
  </div>
  <div class="hdr-r">
    <span class="pill">{today_fmt}</span>
    <span class="pill">Generated {gen_time}</span>
  </div>
</div>
<div class="legend">
  <span class="leg-label">Screener:</span>
  <span class="leg-item"><span class="leg-dot" style="background:#7ab4f5"></span>Low Float (&lt;$10 · &lt;20M · RVol 2x+)</span>
  <span class="leg-item"><span class="leg-dot" style="background:#b07af5"></span>Mid Float ($10-20 · 20-100M · RVol 3x+)</span>
  <span class="leg-item"><span class="leg-dot" style="background:#7af5a0"></span>Manual Watchlist</span>
</div>
<div class="summary">
  <div class="sum-cell"><div class="sum-n" style="color:#6ee89a">{len(hot)}</div><div class="sum-l">Hot Setups</div></div>
  <div class="sum-cell"><div class="sum-n" style="color:#f5c46e">{len(warm)}</div><div class="sum-l">Warm Setups</div></div>
  <div class="sum-cell"><div class="sum-n" style="color:#7ab4f5">{len(watch)}</div><div class="sum-l">Watch</div></div>
  <div class="sum-cell"><div class="sum-n">{total}</div><div class="sum-l">Total</div></div>
</div>
<div class="body">
  <div class="sec-lbl">Hot setups — score ≥70%</div>
  <div class="cards">{hot_cards}</div>
  <div class="sec-lbl">Warm setups — score ≥50%</div>
  <div class="cards">{warm_cards}</div>
  {"<div class='sec-lbl'>Watch — score ≥35%</div><div class='cards'>" + watch_cards + "</div>" if watch_cards else ""}
  <div class="sec-lbl">📌 Manual watchlist</div>
  <div class="cards">{manual_cards}</div>
</div>
<div class="footer">Micro-Cap Momentum Swing Scanner · Not financial advice · Always confirm on live chart before entry</div>
</body>
</html>"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    # Find most recent data file
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))
    if not files:
        print("No data files found. Run scanner.py first.")
        sys.exit(1)

    with open(files[-1]) as f:
        data = json.load(f)

    html = render_html(data)
    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, "swing-trading.html")
    with open(out_path, "w") as f:
        f.write(html)
    print(f"Written → {out_path}")

if __name__ == "__main__":
    main()
