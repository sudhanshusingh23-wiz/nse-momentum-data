#!/usr/bin/env python3
"""
build_artifacts.py — turn accumulated raw NSE files into the small, ready-to-use
artifacts that the Claude momentum skills consume.

Reads:
  data/sec_bhavdata_full_*.csv    raw daily bhavcopy
  indices/ind_close_all_*.csv     raw daily index closes
  reference/ind_nifty500list.csv  constituents

Writes to artifacts/:
  breadth.json          % of Nifty 500 above 200/50 DMA + 10-session delta  (L1 comp 2,3)
  nifty500.csv          benchmark close series                              (L1 comp 1)
  smallcap250.csv       Nifty Smallcap 250 close series                     (L1 comp 4)
  nifty50.csv           Nifty 50 close series  (divergence diagnostic ONLY, never the benchmark)
  vix.csv               India VIX close series                              (L1 comp 5)
  sector_indices.csv    long format: date,sector,close for all sector indices (L2 Tier A)
  panel.csv.gz          corporate-action-adjusted constituent panel          (L2 breadth/particip.)
  manifest.json         what was built, from how many files, and any warnings

Corporate actions are back-adjusted using NSE's own PREV_CLOSE, which is already
adjusted on the ex-date. This is the correct fix; dropping affected names would
bias breadth downward.
"""

import glob, gzip, json, os, subprocess, sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ART = os.path.join(ROOT, "artifacts")

# Index names as they appear in ind_close_all, mapped to output files.
INDEX_MAP = {
    "NIFTY 500": "nifty500.csv",
    "NIFTY SMALLCAP 250": "smallcap250.csv",
    "NIFTY 50": "nifty50.csv",
    "INDIA VIX": "vix.csv",
}
CA_LOW, CA_HIGH = 0.85, 1.18   # PREV_CLOSE/prior-close ratios outside this = corporate action


def datestr_from(path, prefix):
    b = os.path.basename(path).replace(prefix, "").replace(".csv", "")
    return b if len(b) == 8 and b.isdigit() else None


def load_bhavcopy():
    files = sorted(glob.glob(os.path.join(ROOT, "data", "sec_bhavdata_full_*.csv")))
    rows, skipped = [], 0
    for p in files:
        ds = datestr_from(p, "sec_bhavdata_full_")
        if not ds:
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            skipped += 1
            continue
        df.columns = [c.strip() for c in df.columns]
        if not {"SYMBOL", "CLOSE_PRICE", "PREV_CLOSE"}.issubset(df.columns):
            skipped += 1
            continue
        if "SERIES" in df.columns:
            df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
        cols = ["SYMBOL", "CLOSE_PRICE", "PREV_CLOSE"]
        for opt in ("TURNOVER_LACS", "DELIV_PER"):
            if opt in df.columns:
                cols.append(opt)
        sub = df[cols].copy()
        sub["date"] = ds[4:] + ds[2:4] + ds[:2]     # DDMMYYYY -> YYYYMMDD
        rows.append(sub)
    if not rows:
        sys.exit("ERROR: no parseable bhavcopy files in data/. Run fetch_nse.py first.")
    d = pd.concat(rows, ignore_index=True)
    d = d.rename(columns={"SYMBOL": "symbol", "CLOSE_PRICE": "close",
                          "PREV_CLOSE": "prev_close", "TURNOVER_LACS": "turnover",
                          "DELIV_PER": "delivery_pct"})
    d["symbol"] = d["symbol"].astype(str).str.strip()
    for c in ("close", "prev_close", "turnover", "delivery_pct"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    return d.dropna(subset=["close"]), len(files), skipped


def adjust(d):
    """Back-adjust for splits/bonuses using NSE's adjusted PREV_CLOSE."""
    C = d.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    P = d.pivot_table(index="date", columns="symbol", values="prev_close",
                      aggfunc="last").sort_index().reindex_like(C)
    R = P / C.shift(1)
    # only where this session AND the immediately preceding session both traded,
    # otherwise a data gap masquerades as a corporate action
    R = R.where(C.notna() & C.shift(1).notna() & P.notna())
    events = R.where((R < CA_LOW) | (R > CA_HIGH))
    n_events = int(events.notna().sum().sum())
    n_syms = int((events.notna().sum() > 0).sum())
    Rc = events.fillna(1.0)
    factor = Rc[::-1].cumprod()[::-1].shift(-1).fillna(1.0)   # product of SUBSEQUENT ratios
    return C * factor, n_events, n_syms


def load_indices():
    files = sorted(glob.glob(os.path.join(ROOT, "indices", "ind_close_all_*.csv")))
    rows = []
    for p in files:
        ds = datestr_from(p, "ind_close_all_")
        if not ds:
            continue
        try:
            df = pd.read_csv(p)
        except Exception:
            continue
        df.columns = [c.strip() for c in df.columns]
        name = next((c for c in df.columns if c.lower().startswith("index name")), None)
        close = next((c for c in df.columns if "closing" in c.lower()), None)
        if not (name and close):
            continue
        sub = df[[name, close]].copy()
        sub.columns = ["index_name", "close"]
        sub["date"] = ds[4:] + ds[2:4] + ds[:2]
        rows.append(sub)
    if not rows:
        return None, len(files)
    d = pd.concat(rows, ignore_index=True)
    d["index_name"] = d["index_name"].astype(str).str.strip().str.upper()
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d["date"] = pd.to_datetime(d["date"], format="%Y%m%d")
    return d.dropna(subset=["close"]), len(files)


def main():
    os.makedirs(ART, exist_ok=True)
    warnings = []
    print("=" * 68)
    print("BUILD ARTIFACTS")
    print("=" * 68)

    raw, n_files, n_skipped = load_bhavcopy()
    print("bhavcopy: %d files, %d skipped, %d rows, %d symbols"
          % (n_files, n_skipped, len(raw), raw["symbol"].nunique()))
    if n_skipped:
        warnings.append("%d bhavcopy file(s) unparseable" % n_skipped)

    Cadj, n_ev, n_sym = adjust(raw)
    print("corporate actions: %d events across %d symbols, back-adjusted" % (n_ev, n_sym))

    panel = Cadj.stack().reset_index()
    panel.columns = ["date", "symbol", "close"]
    panel = panel.dropna(subset=["close"])          # pandas 3.x stack() keeps NaN
    panel["date"] = pd.to_datetime(panel["date"], format="%Y%m%d")
    extra = raw.copy()
    extra["date"] = pd.to_datetime(extra["date"], format="%Y%m%d")
    keep = ["date", "symbol"] + [c for c in ("delivery_pct", "turnover") if c in extra.columns]
    panel = panel.merge(extra[keep].drop_duplicates(["date", "symbol"]),
                        on=["date", "symbol"], how="left")
    ppath = os.path.join(ART, "panel.csv.gz")
    panel.to_csv(ppath, index=False, compression="gzip")
    print("panel: %d rows, %d sessions -> artifacts/panel.csv.gz"
          % (len(panel), panel["date"].nunique()))

    idx, n_idx_files = load_indices()
    if idx is None:
        warnings.append("No index files found in indices/. Regime component 1 (25%) and "
                        "component 4 (15%) cannot be computed, and the L2 sector engine has "
                        "no Tier A index series.")
        print("indices: NONE FOUND — see warning")
    else:
        print("indices: %d files, %d distinct indices" % (n_idx_files, idx["index_name"].nunique()))
        for name, fname in INDEX_MAP.items():
            s = idx[idx["index_name"] == name][["date", "close"]].sort_values("date")
            if s.empty:
                warnings.append("index '%s' not present in ind_close_all" % name)
                continue
            s.to_csv(os.path.join(ART, fname), index=False)
            print("   %-22s %4d sessions -> artifacts/%s" % (name, len(s), fname))
        sect = idx[~idx["index_name"].isin(INDEX_MAP)][["date", "index_name", "close"]]
        sect = sect.rename(columns={"index_name": "sector"})
        sect.to_csv(os.path.join(ART, "sector_indices.csv"), index=False)
        print("   %-22s %4d rows -> artifacts/sector_indices.csv"
              % ("all other indices", len(sect)))

    # ---- breadth, via the skill's own script if vendored, else inline ----
    cons = os.path.join(ROOT, "reference", "ind_nifty500list.csv")
    bpath = os.path.join(ART, "breadth.json")
    if not os.path.exists(cons):
        warnings.append("reference/ind_nifty500list.csv missing; breadth not computed.")
        print("breadth: SKIPPED (no constituent list)")
    else:
        helper = os.path.join(ROOT, "scripts", "build_breadth.py")
        tmp = os.path.join(ART, "_panel_flat.csv")
        panel.to_csv(tmp, index=False)
        if os.path.exists(helper):
            rc = subprocess.call([sys.executable, helper, "--panel", tmp,
                                  "--constituents", cons, "--out", bpath,
                                  "--ca-report", os.path.join(ART, "ca_flags.csv")])
            if rc != 0:
                warnings.append("build_breadth.py exited %d" % rc)
        else:
            warnings.append("scripts/build_breadth.py not vendored into this repo; "
                            "copy it from the market-regime-monitor skill.")
            print("breadth: SKIPPED (build_breadth.py not found)")
        if os.path.exists(tmp):
            os.remove(tmp)

    man = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "bhavcopy_files": n_files,
        "index_files": n_idx_files if idx is not None else 0,
        "panel_sessions": int(panel["date"].nunique()),
        "panel_symbols": int(panel["symbol"].nunique()),
        "latest_session": str(panel["date"].max().date()),
        "corporate_action_events": n_ev,
        "warnings": warnings,
    }
    json.dump(man, open(os.path.join(ART, "manifest.json"), "w"), indent=2)
    print("-" * 68)
    print("latest session in panel: %s" % man["latest_session"])
    for w in warnings:
        print("WARNING: " + w)
    print("=" * 68)


if __name__ == "__main__":
    main()
