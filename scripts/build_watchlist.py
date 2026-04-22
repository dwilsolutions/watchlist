"""
Swing Watchlist HTML Builder — Navy/Gold theme
Reads scanner JSON and renders swing-trading.html
"""

import os, sys, json, glob, math
from datetime import date, datetime

DATA_DIR = os.environ.get("SWING_DATA_DIR", "docs/data/swing")
OUT_DIR  = os.environ.get("SWING_OUT_DIR",  "docs")

# ── Rank config ────────────────────────────────────────────────────────────────

RANK_CFG = {
    "hot":   ("#1a3a0a","#c9a84c","🔥 HOT"),
    "warm":  ("#2a1a06","#fb923c","⚡ WARM"),
    "watch": ("#0a1830","#7ab4f5","👁 WATCH"),
    "avoid": ("#2a0a0a","#f87171","✗ AVOID"),
}
SCAN_COLORS = {
    "Low Float": ("#091830","#7ab4f5","rgba(122,180,245,0.2)"),
    "Mid Float": ("#1a0f28","#b07af5","rgba(176,122,245,0.2)"),
    "Manual":    ("#091820","#4ade80","rgba(74,222,128,0.2)"),
}
FLAG_CFG = {
    "catalyst": ("#2a1a06","#c9a84c"),
    "gap":      ("#091a2a","#7ab4f5"),
    "danger":   ("#2a0808","#f87171"),
}

# ── CSS ────────────────────────────────────────────────────────────────────────

CSS = """
:root{--bg:#030c1a;--bg2:#071428;--bg3:#0a1f3d;--border:rgba(201,168,76,0.12);
--text:#eef2ff;--muted:#6b7a99;--gold:#c9a84c;--mono:'DM Mono',monospace;--sans:'Syne',sans-serif;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;line-height:1.6;}
a{color:inherit;text-decoration:none;}
.hdr{background:var(--bg2);border-bottom:2px solid var(--gold);padding:16px 20px;
  display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:10px;}
.hdr-l h1{font-family:var(--sans);font-size:20px;font-weight:700;letter-spacing:-0.3px;}
.hdr-l h1 em{color:var(--gold);font-style:normal;}
.hdr-l .sub{font-size:11px;color:var(--muted);margin-top:3px;}
.hdr-r{display:flex;gap:8px;align-items:center;flex-wrap:wrap;}
.pill{font-size:10px;padding:3px 9px;border-radius:20px;background:var(--bg3);
  color:var(--muted);border:1px solid rgba(255,255,255,0.07);}
.legend{display:flex;gap:14px;padding:8px 20px;background:var(--bg2);
  border-bottom:1px solid rgba(255,255,255,0.06);flex-wrap:wrap;align-items:center;}
.leg-label{font-size:10px;color:var(--muted);margin-right:4px;}
.leg-item{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--muted);}
.leg-dot{width:8px;height:8px;border-radius:2px;}
.summary{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;
  background:rgba(255,255,255,0.05);border-bottom:1px solid rgba(255,255,255,0.06);}
.sum-cell{background:var(--bg2);padding:12px 16px;text-align:center;}
.sum-n{font-family:var(--sans);font-size:26px;font-weight:700;}
.sum-l{font-size:10px;color:var(--muted);margin-top:2px;text-transform:uppercase;letter-spacing:0.06em;}
.body{padding:18px 20px 48px;max-width:960px;margin:0 auto;}
.sec-lbl{font-size:10px;color:var(--muted);letter-spacing:0.1em;text-transform:uppercase;
  margin:22px 0 9px;display:flex;align-items:center;gap:8px;}
.sec-lbl::after{content:'';flex:1;height:1px;background:rgba(255,255,255,0.05);}
.cards{display:flex;flex-direction:column;gap:12px;}
/* Card */
.card{background:var(--bg2);border:1px solid var(--border);border-radius:12px;
  padding:14px 16px;border-left:3px solid;}
.card.hot{border-left-color:#c9a84c;}.card.warm{border-left-color:#fb923c;}
.card.watch{border-left-color:#7ab4f5;}.card.avoid{border-left-color:#f87171;opacity:0.6;}
.card.manual-pin{border-top:1px solid rgba(74,222,128,0.15);}
/* Row 1 */
.r1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;}
.tkr{font-family:var(--sans);font-size:15px;font-weight:700;}
.co{font-size:11px;color:var(--muted);flex:1;}
.scan-tag{font-size:10px;padding:2px 7px;border-radius:10px;border:1px solid;}
.rank-pill{font-size:11px;font-weight:600;padding:2px 10px;border-radius:20px;}
.manual-badge{font-size:10px;padding:2px 7px;border-radius:10px;
  background:#091820;color:#4ade80;border:1px solid rgba(74,222,128,0.2);}
.ch{font-size:12px;font-weight:500;}.pos{color:#4ade80;}.neg{color:#f87171;}
.clink{font-size:11px;color:#5a8fd4;margin-left:auto;}
/* Score bar */
.score-bar-wrap{display:flex;align-items:center;gap:6px;margin-bottom:8px;}
.score-bar-bg{flex:1;height:3px;background:rgba(255,255,255,0.05);border-radius:2px;overflow:hidden;}
.score-bar-fill{height:100%;border-radius:2px;background:var(--gold);}
.score-val{font-size:10px;color:var(--gold);min-width:28px;text-align:right;}
/* Flags */
.flags{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px;}
.flag{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:500;}
/* Stats */
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-bottom:12px;}
.st{background:var(--bg3);border-radius:6px;padding:5px 8px;}
.sl{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:0.05em;}
.sv{font-size:12px;font-weight:500;color:var(--text);margin-top:1px;}
/* Date rows */
.date-section{margin-bottom:12px;}
.date-group-label{font-size:9px;color:#3a4d66;letter-spacing:0.1em;text-transform:uppercase;
  padding-bottom:4px;border-bottom:1px solid rgba(255,255,255,0.04);margin-bottom:6px;}
.date-row{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px;}
.cdate{border-radius:8px;padding:9px 12px;}
.cd-label{font-size:9px;text-transform:uppercase;letter-spacing:0.09em;margin-bottom:3px;}
.cd-date{font-family:var(--sans);font-size:13px;font-weight:700;margin-bottom:2px;}
.cd-note{font-size:10px;}
.earn-warn{background:#1f1400;border:1px solid rgba(251,191,36,0.35);}
.earn-warn .cd-label{color:#fbbf24;}.earn-warn .cd-date{color:#fde68a;}.earn-warn .cd-note{color:#92762a;}
.earn-ok{background:#091424;border:1px solid rgba(255,255,255,0.05);}
.earn-ok .cd-label{color:#3a4d66;}.earn-ok .cd-date{color:#6b7a99;}.earn-ok .cd-note{color:#2a3550;}
.ma-active{background:#0a1a2a;border:1px solid rgba(96,165,250,0.25);}
.ma-active .cd-label{color:#60a5fa;}.ma-active .cd-date{color:#93c5fd;}.ma-active .cd-note{color:#2a4a6a;}
.fda-active{background:#180a20;border:1px solid rgba(192,132,252,0.25);}
.fda-active .cd-label{color:#c084fc;}.fda-active .cd-date{color:#e0aaff;}.fda-active .cd-note{color:#5a2a8a;}
.clinical-active{background:#1a0f0a;border:1px solid rgba(251,146,60,0.25);}
.clinical-active .cd-label{color:#fb923c;}.clinical-active .cd-date{color:#fdba74;}.clinical-active .cd-note{color:#6a3a1a;}
.eightk-new{background:#0d1f10;border:1px solid rgba(74,222,128,0.2);}
.eightk-new .cd-label{color:#4ade80;}.eightk-new .cd-date{color:#86efac;}.eightk-new .cd-note{color:#2d5c3a;}
.eightk-old{background:#091424;border:1px solid rgba(255,255,255,0.05);}
.eightk-old .cd-label{color:#3a4d66;}.eightk-old .cd-date{color:#6b7a99;}.eightk-old .cd-note{color:#2a3550;}
.cdate-none{background:#060f1a;border:1px solid rgba(255,255,255,0.03);}
.cdate-none .cd-label{color:#1e2d42;}.cdate-none .cd-date{color:#1e2d42;}.cdate-none .cd-note{color:#141e2e;}
/* Entry boxes */
.entry-section{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;margin-bottom:10px;}
.entry-box{border-radius:8px;padding:10px 12px;display:flex;flex-direction:column;gap:3px;}
.e-label{font-size:9px;text-transform:uppercase;letter-spacing:0.1em;}
.e-price{font-family:var(--sans);font-size:16px;font-weight:700;color:var(--text);}
.e-sub{font-size:10px;color:var(--muted);}
.e-size{font-size:10px;margin-top:2px;}
.buy1{background:#0a2010;border:1px solid rgba(74,222,128,0.3);}
.buy1 .e-label{color:#4ade80;}.buy1 .e-size{color:#4ade80;}
.buy2{background:#1a1600;border:1px solid rgba(250,204,21,0.2);}
.buy2 .e-label{color:#facc15;}.buy2 .e-size{color:#facc15;}
.buy3{background:#1a1000;border:1px solid rgba(251,146,60,0.2);}
.buy3 .e-label{color:#fb923c;}.buy3 .e-size{color:#fb923c;}
.stop{background:#1a0808;border:1px solid rgba(248,113,113,0.3);}
.stop .e-label{color:#f87171;}.stop .e-price{color:#fca5a5;}
.stop .e-sub{color:#6b3333;}.stop .e-size{color:#f87171;}
/* Targets */
.targets{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px;}
.target-box{background:var(--bg3);border-radius:8px;padding:10px 12px;
  border:1px solid rgba(255,255,255,0.04);}
.t-label{font-size:9px;text-transform:uppercase;letter-spacing:0.08em;color:#4d5d7a;margin-bottom:4px;}
.t-price{font-family:var(--sans);font-size:15px;font-weight:700;color:var(--gold);}
.t-pct{font-size:10px;color:#5a6a7a;margin-top:2px;}
/* Signal bars */
.sig-bars{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;}
.sig-bar-row{display:grid;grid-template-columns:100px 1fr 36px;align-items:center;gap:8px;}
.sig-bar-name{font-size:11px;color:#8a99b8;}
.sig-bar-bg{height:4px;background:#0a1f3d;border-radius:3px;overflow:hidden;}
.sig-bar-fill{height:100%;border-radius:3px;}
.bar-hit{background:#c9a84c;}.bar-partial{background:#3a4d66;}.bar-miss{background:transparent;}
.sig-bar-val{font-size:10px;text-align:right;}
.val-hit{color:#c9a84c;}.val-miss{color:#1e2d42;}.val-partial{color:#3a4d66;}
/* Section labels */
.sub-label{font-size:9px;color:#3a4d66;letter-spacing:0.12em;text-transform:uppercase;margin-bottom:6px;}
/* News */
.news-line{font-size:10px;color:var(--muted);font-style:italic;
  border-top:1px solid rgba(255,255,255,0.04);padding-top:8px;
  display:flex;align-items:center;gap:8px;overflow:hidden;}
.news-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;}
.news-btn{flex-shrink:0;font-size:10px;font-style:normal;padding:2px 8px;border-radius:20px;
  background:var(--bg3);color:var(--gold);border:1px solid rgba(201,168,76,0.3);text-decoration:none;}
.no-data{background:var(--bg2);border:1px solid rgba(255,255,255,0.05);border-radius:8px;
  padding:16px;color:var(--muted);font-size:12px;text-align:center;}
.footer{text-align:center;font-size:10px;color:var(--muted);
  padding:24px;border-top:1px solid rgba(255,255,255,0.05);}
"""

# ── Date box HTML ──────────────────────────────────────────────────────────────

def date_box(label, value, note, css_class):
    return f"""<div class="cdate {css_class}">
  <div class="cd-label">{label}</div>
  <div class="cd-date">{value or "—"}</div>
  <div class="cd-note">{note or ""}</div>
</div>"""

def render_dates(dates):
    if not dates:
        return ""

    # Corporate row
    # Earnings
    earn_val  = dates.get("earnings")
    earn_days = dates.get("earnings_days")
    if earn_val and earn_days is not None:
        if earn_days <= 14:
            earn_box = date_box("⚠ Earnings", earn_val, f"{earn_days}d — plan your exit", "earn-warn")
        else:
            earn_box = date_box("Earnings", earn_val, f"{earn_days} days away", "earn-ok")
    else:
        earn_box = date_box("Earnings", "—", "Not scheduled", "cdate-none")

    # M&A
    ma_val = dates.get("ma_close")
    ma_box = date_box("M&A Closing", ma_val, "Deal expected to close", "ma-active") if ma_val \
             else date_box("M&A Closing", "—", "No deal detected", "cdate-none")

    # Investor day
    inv_val = dates.get("investor_day")
    inv_box = date_box("Investor Day", inv_val, "Scheduled event", "earn-ok") if inv_val \
              else date_box("Investor Day", "—", "None scheduled", "cdate-none")

    # Regulatory row
    # FDA
    fda_val = dates.get("fda")
    fda_box = date_box("FDA Decision", fda_val, "Binary event", "fda-active") if fda_val \
              else date_box("FDA Decision", "—", "No FDA event", "cdate-none")

    # Clinical trial
    clin_val = dates.get("clinical")
    clin_box = date_box("Trial Readout", clin_val, "Primary completion", "clinical-active") if clin_val \
               else date_box("Trial Readout", "—", "No trial found", "cdate-none")

    # 8-K filed
    eightk_val = dates.get("eightk_filed")
    if eightk_val:
        try:
            from datetime import date as ddate
            fd = ddate.fromisoformat(eightk_val[:10])
            days_ago = (ddate.today() - fd).days
            if days_ago <= 7:
                eightk_box = date_box("8-K Filed", f"{days_ago}d ago", "Catalyst is fresh", "eightk-new")
            else:
                eightk_box = date_box("8-K Filed", f"{days_ago}d ago", "Catalyst aging", "eightk-old")
        except:
            eightk_box = date_box("8-K Filed", eightk_val[:10], "", "eightk-old")
    else:
        eightk_box = date_box("8-K Filed", "—", "No recent 8-K", "cdate-none")

    return f"""<div class="date-section">
  <div class="date-group-label">Corporate</div>
  <div class="date-row">{earn_box}{ma_box}{inv_box}</div>
  <div class="date-group-label">Regulatory</div>
  <div class="date-row">{fda_box}{clin_box}{eightk_box}</div>
</div>"""

# ── Entry plan HTML ────────────────────────────────────────────────────────────

def fmt_p(v):
    if v is None: return "—"
    try: return f"${float(v):.2f}"
    except: return "—"

def render_entries(entries):
    if not entries:
        return '<div class="no-data" style="margin-bottom:10px;">Entry data unavailable</div>'
    return f"""<div class="sub-label">Entry Plan</div>
<div class="entry-section">
  <div class="entry-box buy1">
    <div class="e-label">First Buy</div>
    <div class="e-price">{fmt_p(entries.get("entry1"))}</div>
    <div class="e-sub">{entries.get("entry1_note","")}</div>
    <div class="e-size">40% of position</div>
  </div>
  <div class="entry-box buy2">
    <div class="e-label">Add on Dip</div>
    <div class="e-price">{fmt_p(entries.get("entry2"))}</div>
    <div class="e-sub">{entries.get("entry2_note","")}</div>
    <div class="e-size">35% of position</div>
  </div>
  <div class="entry-box buy3">
    <div class="e-label">Add on Strength</div>
    <div class="e-price">{fmt_p(entries.get("entry3"))}</div>
    <div class="e-sub">{entries.get("entry3_note","")}</div>
    <div class="e-size">25% of position</div>
  </div>
  <div class="entry-box stop">
    <div class="e-label">Cut Loss Below</div>
    <div class="e-price">{fmt_p(entries.get("stop"))}</div>
    <div class="e-sub">{entries.get("stop_note","")}</div>
    <div class="e-size">Exit full position</div>
  </div>
</div>
<div class="sub-label">Price Targets</div>
<div class="targets">
  <div class="target-box">
    <div class="t-label">Target 1 — Sell 40%</div>
    <div class="t-price">{fmt_p(entries.get("target1"))}</div>
    <div class="t-pct">+{entries.get("target1_pct",0):.0f}% from first buy</div>
  </div>
  <div class="target-box">
    <div class="t-label">Target 2 — Sell 40%</div>
    <div class="t-price">{fmt_p(entries.get("target2"))}</div>
    <div class="t-pct">+{entries.get("target2_pct",0):.0f}% from first buy</div>
  </div>
  <div class="target-box">
    <div class="t-label">Target 3 — Sell rest</div>
    <div class="t-price">{fmt_p(entries.get("target3"))}</div>
    <div class="t-pct">+{entries.get("target3_pct",0):.0f}% from first buy</div>
  </div>
</div>"""

# ── Signal bars HTML ───────────────────────────────────────────────────────────

SIG_LABELS = {
    "fresh_8k":       ("Fresh 8-K",    0.25),
    "merger_pivot":   ("Catalyst",     0.20),
    "previous_spike": ("Prev Spike",   0.20),
    "short_squeeze":  ("Short Squeeze",0.15),
    "high_rel_vol":   ("Volume",       0.10),
    "fda_binary":     ("FDA Event",    0.10),
}

def render_signal_bars(components):
    rows = ""
    for key, (label, weight) in SIG_LABELS.items():
        val = components.get(key, 0)
        contribution = round(val * weight * 100)
        if val >= 1.0:
            bar_cls, val_cls, width = "bar-hit", "val-hit", "100%"
            display = f"{contribution}%"
        elif val > 0:
            bar_cls, val_cls, width = "bar-partial", "val-partial", f"{int(val*100)}%"
            display = f"{contribution}%"
        else:
            bar_cls, val_cls, width = "bar-miss", "val-miss", "0%"
            display = "0%"
        rows += f"""<div class="sig-bar-row">
  <span class="sig-bar-name">{label}</span>
  <div class="sig-bar-bg"><div class="sig-bar-fill {bar_cls}" style="width:{width}"></div></div>
  <span class="sig-bar-val {val_cls}">{display}</span>
</div>"""
    return f'<div class="sub-label">Signal Breakdown</div><div class="sig-bars">{rows}</div>'

# ── Card HTML ──────────────────────────────────────────────────────────────────

def flag_html(label, kind):
    bg, tx = FLAG_CFG.get(kind, ("#1c1f23","#aaa"))
    return f'<span class="flag" style="background:{bg};color:{tx}">{label}</span>'

def card_html(t):
    rank = t.get("rank","watch")
    rank_bg, rank_tx, rank_lbl = RANK_CFG[rank]
    scan  = t.get("scan","Low Float")
    scan_bg, scan_tx, scan_border = SCAN_COLORS.get(scan, SCAN_COLORS["Low Float"])
    ticker = t["ticker"]
    chg    = t.get("change", 0)
    chg_sign = "+" if chg >= 0 else ""
    chg_cls  = "pos" if chg >= 0 else "neg"
    score_pct = min(100, round(t.get("score",0) * 100))
    flags_html = "".join(flag_html(l,k) for l,k in t.get("flags",[]))
    manual_html = '<span class="manual-badge">📌 WATCHLIST</span>' if t.get("manual") else ""
    manual_cls  = " manual-pin" if t.get("manual") else ""

    short_str = f'{t["short_pct"]:.0f}%' if t.get("short_pct",0) > 0 else "—"
    spike_str = f'+{t["spike_pct"]:.0f}%' if t.get("spike_pct",0) > 0 else "—"

    news     = t.get("news","")
    news_url = t.get("news_url","")
    if news_url and news_url.startswith("http"):
        news_line = f'<span class="news-txt">{news}</span><a class="news-btn" href="{news_url}" target="_blank">Read ↗</a>'
    else:
        news_line = f'<span class="news-txt">{news}</span>'

    return f"""<div class="card {rank}{manual_cls}">
  <div class="r1">
    <span class="tkr">{ticker}</span>
    <span class="co">{t.get("company","")[:26]}</span>
    <span class="scan-tag" style="background:{scan_bg};color:{scan_tx};border-color:{scan_border}">{scan}</span>
    <span class="rank-pill" style="background:{rank_bg};color:{rank_tx}">{rank_lbl}</span>
    {manual_html}
    <span class="ch {chg_cls}">{chg_sign}{chg:.1f}%</span>
    <a class="clink" href="https://finviz.com/quote.ashx?t={ticker}" target="_blank">Chart ↗</a>
  </div>
  <div class="score-bar-wrap">
    <div class="score-bar-bg"><div class="score-bar-fill" style="width:{score_pct}%"></div></div>
    <span class="score-val">{score_pct}%</span>
  </div>
  <div class="flags">{flags_html}</div>
  <div class="stats">
    <div class="st"><div class="sl">Price</div><div class="sv">{t.get("price_display", f'${t.get("price",0):.2f}')}</div></div>
    <div class="st"><div class="sl">RVol</div><div class="sv">{t.get("rvol_display", f'{t.get("rvol",0):.0f}x')}</div></div>
    <div class="st"><div class="sl">Float</div><div class="sv">{t.get("float_display", f'{t.get("float_m",0):.1f}M')}</div></div>
    <div class="st"><div class="sl">Short Int</div><div class="sv">{short_str}</div></div>
    <div class="st"><div class="sl">90d Spike</div><div class="sv">{spike_str}</div></div>
  </div>
  {render_dates(t.get("dates",{}))}
  {render_entries(t.get("entries"))}
  {render_signal_bars(t.get("components",{}))}
  <div class="news-line">{news_line}</div>
</div>"""

# ── Page HTML ──────────────────────────────────────────────────────────────────

def render_html(data):
    tickers  = data["tickers"]
    today    = data["date"]
    session  = data["session"]
    gen_time = data["generated"]
    session_label = "Pre-Market Scan" if session == "premarket" else "Mid-Morning Scan"

    hot    = [t for t in tickers if t.get("rank")=="hot"   and not t.get("manual")]
    warm   = [t for t in tickers if t.get("rank")=="warm"  and not t.get("manual")]
    watch  = [t for t in tickers if t.get("rank")=="watch" and not t.get("manual")]
    manual = [t for t in tickers if t.get("manual")]
    avoid  = [t for t in tickers if t.get("rank")=="avoid" and not t.get("manual")]

    try:
        d = datetime.fromisoformat(today)
        today_fmt = d.strftime("%a %b %-d, %Y")
    except:
        today_fmt = today

    def section(label, items, empty_msg="No setups today."):
        cards = "".join(card_html(t) for t in items)
        return f"""<div class="sec-lbl">{label}</div>
<div class="cards">{cards if cards else f'<p style="color:var(--muted);font-size:12px;">{empty_msg}</p>'}</div>"""

    hot_section   = section("🔥 Hot setups — score ≥70%", hot, "No HOT setups today.")
    warm_section  = section("⚡ Warm setups — score ≥50%", warm, "No WARM setups today.")
    watch_section = section("👁 Watch — score ≥35%", watch, "") if watch else ""
    manual_section = section("📌 Manual watchlist", manual, "")
    avoid_section  = section("✗ Avoid", avoid, "") if avoid else ""

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
    <a class="pill" href="guide.html" style="color:#c9a84c;">How to Use ↗</a>
  </div>
</div>
<div class="legend">
  <span class="leg-label">Screener:</span>
  <span class="leg-item"><span class="leg-dot" style="background:#7ab4f5"></span>Low Float (&lt;$10 · &lt;20M · RVol 2x+)</span>
  <span class="leg-item"><span class="leg-dot" style="background:#b07af5"></span>Mid Float ($10-20 · 20-100M · RVol 3x+)</span>
  <span class="leg-item"><span class="leg-dot" style="background:#4ade80"></span>Manual Watchlist</span>
</div>
<div class="summary">
  <div class="sum-cell"><div class="sum-n" style="color:#c9a84c">{len(hot)}</div><div class="sum-l">Hot Setups</div></div>
  <div class="sum-cell"><div class="sum-n" style="color:#fb923c">{len(warm)}</div><div class="sum-l">Warm Setups</div></div>
  <div class="sum-cell"><div class="sum-n" style="color:#7ab4f5">{len(watch)}</div><div class="sum-l">Watch</div></div>
  <div class="sum-cell"><div class="sum-n">{len(tickers)}</div><div class="sum-l">Total</div></div>
</div>
<div class="body">
  {hot_section}
  {warm_section}
  {watch_section}
  {manual_section}
  {avoid_section}
</div>
<div class="footer">Micro-Cap Momentum Swing Scanner · Not financial advice · Always confirm on live chart before entry</div>
</body>
</html>"""

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "*.json")))
    if not files:
        print("No data files found. Run scanner.py first."); sys.exit(1)
    with open(files[-1]) as f:
        data = json.load(f)
    html = render_html(data)
    os.makedirs(OUT_DIR, exist_ok=True)
    out  = os.path.join(OUT_DIR, "swing-trading.html")
    with open(out,"w") as f: f.write(html)
    print(f"Written → {out}")

if __name__ == "__main__":
    main()
