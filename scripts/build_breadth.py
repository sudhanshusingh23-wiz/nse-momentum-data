#!/usr/bin/env python3
"""
build_breadth.py — compute Nifty 500 market breadth for the L1 regime monitor.

Produces the two inputs that carry 40% of the regime score:
  pct_above_200dma  (component 2, participation)
  pct_above_50dma + its 10-session delta  (component 3, thrust)

Handles denominator accounting explicitly and screens for unadjusted corporate
actions, which in an unadjusted bhavcopy panel can drag reported breadth down by
several percentage points and flip the RISK-ON gate.

Inputs (one of):
  --bhavcopy-dir DIR   directory of NSE sec_bhavdata_full / UDiFF CSVs (any nesting)
  --panel FILE         long-format CSV: date,symbol,close[,delivery_pct]

Plus:
  --constituents FILE  CSV with a symbol column (NSE Nifty 500 list export)

Usage:
  python build_breadth.py --bhavcopy-dir ./bhavcopy --constituents nifty500.csv \
      --out breadth.json --ca-report ca_flags.csv

Then feed the result to the regime scorer:
  python regime_score.py --benchmark nifty500.csv --breadth-json breadth.json ...
"""

import argparse, glob, json, os, sys
import numpy as np
import pandas as pd

MIN_HISTORY_200 = 200
MIN_HISTORY_50 = 50
CA_GAP_THRESHOLD = 0.35     # single-session move beyond +/-35% => likely unadjusted CA
CA_MARKET_TOLERANCE = 0.10  # unless the whole market moved this much (it never does)


def find_col(cols, *candidates):
    low = {c.lower().strip(): c for c in cols}
    for cand in candidates:
        if cand in low:
            return low[cand]
    return None


def load_bhavcopy_dir(d):
    files = []
    for ext in ("*.csv", "*.CSV"):
        files += glob.glob(os.path.join(d, "**", ext), recursive=True)
    if not files:
        sys.exit("ERROR: no CSV files found under %s" % d)
    frames, bad = [], []
    for f in sorted(files):
        try:
            df = pd.read_csv(f)
        except Exception as e:
            bad.append((f, str(e)))
            continue
        df.columns = [c.strip() for c in df.columns]
        sym = find_col(df.columns, "symbol", "tckrsymb")
        cls = find_col(df.columns, "close_price", "close", "clspric", "last_price")
        dt = find_col(df.columns, "date1", "date", "tradedt", "timestamp")
        ser = find_col(df.columns, "series", "sctysrs")
        dlv = find_col(df.columns, "deliv_per", "delivery_percentage")
        if not (sym and cls and dt):
            bad.append((f, "missing symbol/close/date columns"))
            continue
        keep = {sym: "symbol", cls: "close", dt: "date"}
        if dlv:
            keep[dlv] = "delivery_pct"
        sub = df[list(keep)].rename(columns=keep)
        if ser:
            sub = sub[df[ser].astype(str).str.strip().isin(["EQ", "BE"])]
        frames.append(sub)
    if bad:
        print("WARNING: %d file(s) skipped. First few:" % len(bad), file=sys.stderr)
        for f, why in bad[:5]:
            print("   %s -> %s" % (os.path.basename(f), why), file=sys.stderr)
    if not frames:
        sys.exit("ERROR: no parseable bhavcopy files. NSE changes its file format and URL "
                 "structure periodically; verify the download actually returned data.")
    return pd.concat(frames, ignore_index=True)


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--bhavcopy-dir")
    src.add_argument("--panel")
    ap.add_argument("--constituents", required=True)
    ap.add_argument("--asof")
    ap.add_argument("--out", default="breadth.json")
    ap.add_argument("--ca-report")
    ap.add_argument("--no-ca-screen", action="store_true")
    a = ap.parse_args()

    raw = load_bhavcopy_dir(a.bhavcopy_dir) if a.bhavcopy_dir else pd.read_csv(a.panel)
    raw.columns = [c.lower().strip() for c in raw.columns]
    for need in ("date", "symbol", "close"):
        if need not in raw.columns:
            sys.exit("ERROR: panel must contain columns date, symbol, close")
    # Date parsing must handle BOTH NSE's "04-Nov-2024" and ISO "2024-11-04".
    # dayfirst=True silently produces NaT on ISO dates, which drops most of the
    # panel and surfaces later as a confusing "not enough sessions" error.
    _d = pd.to_datetime(raw["date"], errors="coerce", format="ISO8601")
    if _d.isna().mean() > 0.5:
        _d = pd.to_datetime(raw["date"], errors="coerce", dayfirst=True)
    if _d.isna().mean() > 0.5:
        _d = pd.to_datetime(raw["date"], errors="coerce")
    bad = float(_d.isna().mean())
    if bad > 0.02:
        print("WARNING: %.1f%% of date values failed to parse. Check the date format "
              "in the panel." % (bad * 100), file=sys.stderr)
    raw["date"] = _d
    raw["close"] = pd.to_numeric(raw["close"], errors="coerce")
    raw["symbol"] = raw["symbol"].astype(str).str.strip().str.upper()
    raw = raw.dropna(subset=["date", "symbol", "close"])
    if a.asof:
        raw = raw[raw["date"] <= pd.to_datetime(a.asof)]

    cons = pd.read_csv(a.constituents)
    cons.columns = [c.lower().strip() for c in cons.columns]
    scol = find_col(cons.columns, "symbol", "ticker", "nse symbol", "security")
    if not scol:
        sys.exit("ERROR: constituents file needs a 'symbol' column")
    members = sorted(set(cons[scol].astype(str).str.strip().str.upper()))
    n_members = len(members)

    wide = raw.pivot_table(index="date", columns="symbol", values="close",
                           aggfunc="last").sort_index()
    present = [s for s in members if s in wide.columns]
    absent = [s for s in members if s not in wide.columns]
    wide = wide[present]

    if len(wide) < MIN_HISTORY_200 + 10:
        sys.exit("ERROR: only %d sessions in panel. Need >= %d for a 200 DMA plus the "
                 "10-session thrust delta." % (len(wide), MIN_HISTORY_200 + 10))

    # ---- corporate action screen ----
    ca_flagged, ca_rows = [], []
    if not a.no_ca_screen:
        rets = wide.pct_change()
        median_move = rets.median(axis=1).abs()
        for sym in wide.columns:
            r = rets[sym].iloc[-260:]
            hits = r[(r.abs() > CA_GAP_THRESHOLD)]
            for dt, val in hits.items():
                if median_move.get(dt, 0) < CA_MARKET_TOLERANCE:
                    ca_flagged.append(sym)
                    ca_rows.append({"symbol": sym, "date": str(pd.Timestamp(dt).date()),
                                    "move_pct": round(float(val) * 100, 1),
                                    "market_median_move_pct": round(float(median_move.get(dt, 0)) * 100, 2)})
                    break
        ca_flagged = sorted(set(ca_flagged))
        wide = wide.drop(columns=ca_flagged)

    # ---- breadth ----
    d200 = wide.rolling(MIN_HISTORY_200).mean()
    d50 = wide.rolling(MIN_HISTORY_50).mean()
    hi52 = wide.rolling(252, min_periods=200).max()
    last = wide.iloc[-1]

    v200 = d200.iloc[-1].notna() & last.notna()
    v50 = d50.iloc[-1].notna() & last.notna()
    vhi = hi52.iloc[-1].notna() & last.notna()
    n200, n50 = int(v200.sum()), int(v50.sum())
    if n200 == 0:
        sys.exit("ERROR: no symbol has 200 sessions of history. Panel is too short or misaligned.")

    pct200 = float(((last > d200.iloc[-1]) & v200).sum()) / n200 * 100
    pct50 = float(((last > d50.iloc[-1]) & v50).sum()) / n50 * 100
    near52 = (float(((last >= hi52.iloc[-1] * 0.90) & vhi).sum()) / int(vhi.sum()) * 100
              if int(vhi.sum()) else None)

    prev = wide.iloc[-11]
    v50p = d50.iloc[-11].notna() & prev.notna()
    n50p = int(v50p.sum())
    pct50_prev = (float(((prev > d50.iloc[-11]) & v50p).sum()) / n50p * 100) if n50p else None
    delta = round(pct50 - pct50_prev, 2) if pct50_prev is not None else None

    excluded_no_hist = n_members - len(absent) - n200 - len(ca_flagged)
    out = {
        "asof": str(wide.index[-1].date()),
        "universe": "NIFTY500",
        "pct_above_200dma": round(pct200, 2),
        "pct_above_50dma": round(pct50, 2),
        "pct_above_50dma_10s_ago": None if pct50_prev is None else round(pct50_prev, 2),
        "delta_10s_pp": delta,
        "pct_within_10pct_of_52w_high": None if near52 is None else round(near52, 2),
        "denominator_200dma": n200,
        "denominator_50dma": n50,
        "constituents_in_list": n_members,
        "not_found_in_panel": len(absent),
        "excluded_insufficient_history": max(0, excluded_no_hist),
        "excluded_corporate_action_flag": len(ca_flagged),
        "ca_flagged_symbols": ca_flagged[:40],
        "sessions_in_panel": int(len(wide) + len(ca_flagged) * 0),
        "method": "computed_from_panel",
    }

    coverage = n200 / n_members * 100
    warn = []
    if coverage < 90:
        warn.append("Coverage only %.1f%% of the constituent list (%d of %d). Breadth is "
                    "biased if the missing names are systematically weak or small."
                    % (coverage, n200, n_members))
    if len(ca_flagged) > 0.02 * n_members:
        warn.append("%d symbols (%.1f%%) flagged as likely unadjusted corporate actions. "
                    "Apply adjustments and re-run before trusting this figure."
                    % (len(ca_flagged), len(ca_flagged) / n_members * 100))
    if len(absent) > 0.05 * n_members:
        warn.append("%d constituents absent from the panel entirely — check symbol "
                    "conventions and the bhavcopy date range." % len(absent))
    out["warnings"] = warn

    print("=" * 70)
    print("NIFTY 500 BREADTH — %s" % out["asof"])
    print("=" * 70)
    print("  %% above 200 DMA : %6.2f%%   (%d of %d countable)" % (pct200, int(((last > d200.iloc[-1]) & v200).sum()), n200))
    print("  %% above  50 DMA : %6.2f%%   (%d of %d countable)" % (pct50, int(((last > d50.iloc[-1]) & v50).sum()), n50))
    print("  10-session delta: %s pp" % ("--" if delta is None else "%+.2f" % delta))
    if near52 is not None:
        print("  within 10%% of 52w high : %.2f%%" % near52)
    print("-" * 70)
    print("  constituent list        : %d" % n_members)
    print("  absent from panel       : %d" % len(absent))
    print("  insufficient history    : %d" % max(0, excluded_no_hist))
    print("  corporate-action flags  : %d" % len(ca_flagged))
    print("  countable denominator   : %d  (coverage %.1f%%)" % (n200, coverage))
    if warn:
        print("-" * 70)
        for w in warn:
            print("  WARNING: " + w)
    print("=" * 70)

    json.dump(out, open(a.out, "w"), indent=2)
    print("Written to %s" % a.out)
    if a.ca_report and ca_rows:
        pd.DataFrame(ca_rows).to_csv(a.ca_report, index=False)
        print("Corporate-action flags written to %s — review before trusting breadth."
              % a.ca_report)


if __name__ == "__main__":
    main()
