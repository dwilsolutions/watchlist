#!/usr/bin/env python3
"""
QQQ Intraday Backtest — 0DTE ATM option proxy via underlying leverage model.

Tests 4 intraday entry strategies on QQQ 5-min bars (30/60-day windows):
  - VWAP pullback / reclaim
  - Opening Range Breakout (15-min)
  - Time-based entry (11:00 AM ET)
  - 9 EMA pullback

Plus a daily-bar EOD-bias baseline for longer windows (90/120/360 day).

Outputs:
  backtest_results.json — full data
  backtest_results.html — human-readable report

Usage:
  python scripts/backtest.py --ticker QQQ --windows 30,60,90,120,360 --output-dir .
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf

# ---------- Configuration ----------
OPTION_LEVERAGE = 50          # 0DTE ATM: ~50x leverage on underlying %
UNDERLYING_TARGET_PCT = 0.005 # +0.5% QQQ ≈ +25% option (scale-out point)
UNDERLYING_STOP_PCT = 0.003   # -0.3% QQQ ≈ -15% option (tight stop, theta-aware)
TRAIL_PCT = 0.003             # Runner trails 0.3% off peak
SCALE_OUT_FRAC = 0.5          # Take 50% off at first target
EOD_EXIT_TIME = "15:55"       # Force exit 5 min before close
ET = "US/Eastern"
MARKET_OPEN = "09:30"
MARKET_CLOSE = "16:00"


# ---------- Data loading ----------
def load_intraday(ticker: str, days: int, interval: str = "5m") -> pd.DataFrame:
    """Load intraday bars, regular-hours only, ET timezone."""
    end = datetime.now()
    start = end - timedelta(days=days)
    df = yf.download(
        ticker, start=start, end=end, interval=interval,
        progress=False, auto_adjust=False, prepost=False,
    )
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)
    df = df.between_time(MARKET_OPEN, MARKET_CLOSE)
    return df


def load_daily(ticker: str, days: int) -> pd.DataFrame:
    end = datetime.now()
    start = end - timedelta(days=days + 30)  # buffer for non-trading days
    df = yf.download(ticker, start=start, end=end, interval="1d",
                     progress=False, auto_adjust=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.tail(days)


# ---------- Signal generators ----------
def session_vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP that resets daily."""
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = tp * df["Volume"]
    g = df.groupby(df.index.date)
    return (pv.groupby(df.index.date).cumsum() /
            df["Volume"].groupby(df.index.date).cumsum())


def signal_vwap_pullback(df: pd.DataFrame) -> pd.Series:
    """Close was < VWAP within last 3 bars; current Close > VWAP."""
    vwap = session_vwap(df)
    below = (df["Close"] < vwap)
    was_below = below.rolling(3).sum().shift(1).fillna(0) > 0
    return (df["Close"] > vwap) & was_below


def signal_orb(df: pd.DataFrame, orb_minutes: int = 15) -> pd.Series:
    """First close above 9:30–9:45 high each day."""
    orb_cutoff = pd.Timestamp(f"09:{30 + orb_minutes:02d}").time()
    dates = df.index.date
    times = pd.Series(df.index.time, index=df.index)
    orb_window = df[times <= orb_cutoff]
    orb_high = orb_window.groupby(orb_window.index.date)["High"].max()
    orb_high_per_bar = pd.Series(dates, index=df.index).map(orb_high)
    breakout = (df["Close"] > orb_high_per_bar) & (times > orb_cutoff)
    # Keep only first breakout per day
    by_day = breakout.groupby(dates).cumsum()
    return breakout & (by_day == 1)


def signal_time_based(df: pd.DataFrame, hour: int = 11, minute: int = 0) -> pd.Series:
    """Trigger at exactly hour:minute ET each day."""
    return pd.Series(
        [t.hour == hour and t.minute == minute for t in df.index],
        index=df.index,
    )


def signal_ema_pullback(df: pd.DataFrame, period: int = 9) -> pd.Series:
    """Prior bar touched/broke below 9 EMA; current bar closes back above."""
    ema = df["Close"].ewm(span=period, adjust=False).mean()
    touched = (df["Low"].shift(1) <= ema.shift(1))
    return (df["Close"] > ema) & touched & (df["Close"].shift(1) <= ema.shift(1))


# ---------- Trade simulation ----------
def simulate_trade(entry_idx: int, df: pd.DataFrame) -> dict | None:
    """
    Simulate one trade from entry_idx to EOD/stop/trail.
    Models scale-out + trail-the-runner. Long-only.
    """
    entry_time = df.index[entry_idx]
    entry_price = float(df["Close"].iloc[entry_idx])
    entry_date = entry_time.date()

    target_price = entry_price * (1 + UNDERLYING_TARGET_PCT)
    stop_price = entry_price * (1 - UNDERLYING_STOP_PCT)
    eod_cutoff = pd.Timestamp(EOD_EXIT_TIME).time()

    forward = df[(df.index.date == entry_date) & (df.index > entry_time)]
    if forward.empty:
        return None

    scaled = False
    scale_fill = None
    peak = entry_price

    for ts, row in forward.iterrows():
        hi, lo, close = float(row["High"]), float(row["Low"]), float(row["Close"])

        # Stop check first (conservative)
        if not scaled and lo <= stop_price:
            return _build_result(entry_time, entry_price, ts, stop_price,
                                 "stop", scaled=False)

        # Target hit → scale out, start trailing the runner
        if not scaled and hi >= target_price:
            scaled = True
            scale_fill = target_price
            peak = max(peak, hi)
            continue

        if scaled:
            peak = max(peak, hi)
            trail_stop = peak * (1 - TRAIL_PCT)
            if lo <= trail_stop:
                return _build_result(entry_time, entry_price, ts, trail_stop,
                                     "trail", scaled=True, scale_fill=scale_fill)

        if ts.time() >= eod_cutoff:
            return _build_result(entry_time, entry_price, ts, close,
                                 "eod", scaled=scaled, scale_fill=scale_fill)

    last = forward.iloc[-1]
    return _build_result(entry_time, entry_price, last.name, float(last["Close"]),
                         "eod", scaled=scaled, scale_fill=scale_fill)


def _build_result(entry_time, entry_price, exit_time, exit_price, reason,
                  scaled=False, scale_fill=None):
    if scaled and scale_fill is not None:
        scale_pct = (scale_fill - entry_price) / entry_price
        runner_pct = (exit_price - entry_price) / entry_price
        underlying_pct = SCALE_OUT_FRAC * scale_pct + (1 - SCALE_OUT_FRAC) * runner_pct
    else:
        underlying_pct = (exit_price - entry_price) / entry_price

    return {
        "entry_time": entry_time.isoformat(),
        "exit_time": exit_time.isoformat() if hasattr(exit_time, "isoformat") else str(exit_time),
        "entry_price": round(entry_price, 4),
        "exit_price": round(exit_price, 4),
        "scale_fill": round(scale_fill, 4) if scale_fill else None,
        "underlying_pct": round(underlying_pct * 100, 3),
        "option_pct_est": round(underlying_pct * OPTION_LEVERAGE * 100, 2),
        "exit_reason": reason,
        "scaled_out": bool(scaled),
    }


def run_strategy(df: pd.DataFrame, signal: pd.Series, name: str) -> list[dict]:
    """One trade per day max. Skip signals after 15:30 (no time to work)."""
    trades = []
    cutoff = pd.Timestamp("15:30").time()
    seen_dates = set()
    indices = np.where(signal.values)[0]

    for idx in indices:
        ts = df.index[idx]
        if ts.date() in seen_dates:
            continue
        if ts.time() >= cutoff:
            continue
        result = simulate_trade(idx, df)
        if result:
            result["strategy"] = name
            trades.append(result)
            seen_dates.add(ts.date())
    return trades


# ---------- Aggregation ----------
def aggregate_stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n_trades": 0}
    df = pd.DataFrame(trades)
    wins = df[df["option_pct_est"] > 0]
    losses = df[df["option_pct_est"] <= 0]
    pf = (wins["option_pct_est"].sum() / abs(losses["option_pct_est"].sum())
          if len(losses) and losses["option_pct_est"].sum() != 0 else None)
    return {
        "n_trades": int(len(df)),
        "win_rate": round(len(wins) / len(df) * 100, 1),
        "avg_option_pct": round(df["option_pct_est"].mean(), 2),
        "avg_win_pct": round(wins["option_pct_est"].mean(), 2) if len(wins) else 0,
        "avg_loss_pct": round(losses["option_pct_est"].mean(), 2) if len(losses) else 0,
        "best_pct": round(df["option_pct_est"].max(), 2),
        "worst_pct": round(df["option_pct_est"].min(), 2),
        "profit_factor": round(pf, 2) if pf is not None else None,
        "total_return_pct": round(df["option_pct_est"].sum(), 2),
        "avg_underlying_pct": round(df["underlying_pct"].mean(), 3),
        "scale_out_rate": round(df["scaled_out"].mean() * 100, 1),
    }


def daily_bias_test(ticker: str, days: int) -> dict:
    """Baseline: how often does QQQ close green from open? Simple buy-open/sell-close."""
    df = load_daily(ticker, days)
    if df.empty:
        return {"days": 0}
    o_to_c = (df["Close"] - df["Open"]) / df["Open"] * 100
    green = o_to_c > 0
    # Apply leverage model to open-close move
    option_pct = o_to_c * OPTION_LEVERAGE
    return {
        "days": int(len(df)),
        "green_pct": round(green.mean() * 100, 1),
        "avg_o_to_c_pct": round(o_to_c.mean(), 3),
        "median_o_to_c_pct": round(o_to_c.median(), 3),
        "best_day_underlying_pct": round(o_to_c.max(), 2),
        "worst_day_underlying_pct": round(o_to_c.min(), 2),
        "avg_option_pct_est": round(option_pct.mean(), 2),
        "best_day_option_pct_est": round(option_pct.max(), 2),
        "worst_day_option_pct_est": round(option_pct.min(), 2),
    }


# ---------- HTML report ----------
def write_html(results: dict, path: Path) -> None:
    ts = results["generated_at"]
    cfg = results["config"]
    ticker = results["ticker"]

    intraday_html = ""
    for window, strategies in results["intraday_strategies"].items():
        rows = ""
        for sname, data in strategies.items():
            s = data["stats"]
            if s["n_trades"] == 0:
                rows += f"<tr><td>{sname}</td><td colspan='9'>No trades</td></tr>"
                continue
            color = "#22c55e" if s["total_return_pct"] > 0 else "#ef4444"
            rows += (
                f"<tr>"
                f"<td><b>{sname}</b></td>"
                f"<td>{s['n_trades']}</td>"
                f"<td>{s['win_rate']}%</td>"
                f"<td>{s['avg_option_pct']}%</td>"
                f"<td>{s['avg_win_pct']}%</td>"
                f"<td>{s['avg_loss_pct']}%</td>"
                f"<td>{s['best_pct']}%</td>"
                f"<td>{s['worst_pct']}%</td>"
                f"<td>{s['profit_factor'] or '—'}</td>"
                f"<td style='color:{color};font-weight:600'>{s['total_return_pct']}%</td>"
                f"</tr>"
            )
        intraday_html += f"""
        <h3>Intraday — {window} window (5-min bars)</h3>
        <table>
          <thead><tr>
            <th>Strategy</th><th>N</th><th>Win%</th><th>Avg</th>
            <th>AvgW</th><th>AvgL</th><th>Best</th><th>Worst</th>
            <th>PF</th><th>Sum</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    daily_html = ""
    for window, d in results["daily_bias"].items():
        if d.get("days", 0) == 0:
            continue
        color = "#22c55e" if d["avg_option_pct_est"] > 0 else "#ef4444"
        daily_html += (
            f"<tr>"
            f"<td>{window}</td>"
            f"<td>{d['days']}</td>"
            f"<td>{d['green_pct']}%</td>"
            f"<td>{d['avg_o_to_c_pct']}%</td>"
            f"<td>{d['median_o_to_c_pct']}%</td>"
            f"<td>{d['best_day_underlying_pct']}%</td>"
            f"<td>{d['worst_day_underlying_pct']}%</td>"
            f"<td style='color:{color};font-weight:600'>{d['avg_option_pct_est']}%</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>{ticker} Backtest — {ts}</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; max-width: 1100px; margin: 0 auto; padding: 20px; }}
  h1 {{ color: #38bdf8; margin-bottom: 4px; }}
  h2 {{ color: #94a3b8; margin-top: 32px; }}
  h3 {{ color: #cbd5e1; margin-top: 24px; font-size: 16px; }}
  .ts {{ color: #64748b; font-size: 13px; margin-bottom: 16px; }}
  .config {{ background: #1e293b; padding: 12px 16px; border-radius: 8px; font-size: 13px; margin-bottom: 24px; }}
  .config span {{ color: #38bdf8; }}
  table {{ width: 100%; border-collapse: collapse; margin: 8px 0; font-size: 13px; }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ background: #1e293b; color: #94a3b8; font-weight: 500; text-transform: uppercase; font-size: 11px; }}
  tr:hover {{ background: #1e293b; }}
  .caveat {{ background: #422006; border-left: 3px solid #f59e0b; padding: 12px 16px; border-radius: 4px; font-size: 13px; margin: 20px 0; }}
</style>
</head><body>
<h1>{ticker} Backtest</h1>
<div class="ts">Generated {ts}</div>

<div class="config">
  <span>Target:</span> +{cfg['underlying_target_pct']}% underlying ≈ +{cfg['underlying_target_pct'] * cfg['option_leverage']:.0f}% option &nbsp;|&nbsp;
  <span>Stop:</span> -{cfg['underlying_stop_pct']}% &nbsp;|&nbsp;
  <span>Trail:</span> {cfg['trail_pct']}% &nbsp;|&nbsp;
  <span>Scale-out:</span> {int(cfg['scale_out_frac']*100)}% &nbsp;|&nbsp;
  <span>Leverage proxy:</span> {cfg['option_leverage']}x &nbsp;|&nbsp;
  <span>EOD exit:</span> {cfg['eod_exit_time']}
</div>

<div class="caveat">
  <b>⚠ Model limitations:</b> Symmetric leverage proxy — does not model theta decay (real 0DTE bleeds ~2%/hr on flat moves), IV changes, or bid-ask slippage. Real-world option P&L will be worse than shown, especially for trades that chop sideways. Use as relative comparison between strategies, not absolute expectations.
</div>

<h2>Intraday Entry Strategies (Option P&L est., long calls)</h2>
{intraday_html}

<h2>Daily Bias Baseline (Open → Close, no entry timing)</h2>
<table>
  <thead><tr>
    <th>Window</th><th>Days</th><th>Green%</th>
    <th>Avg O→C</th><th>Median O→C</th>
    <th>Best Day</th><th>Worst Day</th><th>Avg Opt%</th>
  </tr></thead>
  <tbody>{daily_html}</tbody>
</table>

<p style="color:#64748b;font-size:12px;margin-top:32px">
  Backtest run at {ts}. yfinance 5-min data limited to 60 days; longer windows use daily bars.
</p>
</body></html>"""
    path.write_text(html)


# ---------- Main ----------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker", default="QQQ")
    parser.add_argument("--windows", default="30,60,90,120,360")
    parser.add_argument("--output-dir", default=".")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    windows = [int(w) for w in args.windows.split(",")]
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Backtest: {ticker} over windows {windows}")
    results = {
        "ticker": ticker,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {
            "underlying_target_pct": UNDERLYING_TARGET_PCT * 100,
            "underlying_stop_pct": UNDERLYING_STOP_PCT * 100,
            "trail_pct": TRAIL_PCT * 100,
            "scale_out_frac": SCALE_OUT_FRAC,
            "option_leverage": OPTION_LEVERAGE,
            "eod_exit_time": EOD_EXIT_TIME,
        },
        "intraday_strategies": {},
        "daily_bias": {},
    }

    for window in windows:
        if window <= 60:
            print(f"  [{window}d] loading 5-min bars...")
            df = load_intraday(ticker, window, interval="5m")
            if df.empty:
                print(f"  [{window}d] no intraday data, skipping")
                continue
            print(f"  [{window}d] {len(df)} bars across {df.index.normalize().nunique()} days")
            strategies = {
                "vwap_pullback": signal_vwap_pullback(df),
                "orb_15min": signal_orb(df, 15),
                "time_1100am_et": signal_time_based(df, 11, 0),
                "ema9_pullback": signal_ema_pullback(df, 9),
            }
            window_results = {}
            for name, signal in strategies.items():
                trades = run_strategy(df, signal, name)
                window_results[name] = {
                    "stats": aggregate_stats(trades),
                    "trades": trades[-30:],
                }
                print(f"    {name}: {len(trades)} trades")
            results["intraday_strategies"][f"{window}d"] = window_results
        else:
            print(f"  [{window}d] beyond yfinance 5m limit — daily bias only")

    for window in windows:
        print(f"  [{window}d] daily-bias baseline...")
        results["daily_bias"][f"{window}d"] = daily_bias_test(ticker, window)

    json_path = out_dir / "backtest_results.json"
    json_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"Wrote {json_path}")

    html_path = out_dir / "backtest_results.html"
    write_html(results, html_path)
    print(f"Wrote {html_path}")


if __name__ == "__main__":
    main()
