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

import glob, gzip, json, os, shutil, subprocess, sys
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


def man_date_tag(ts):
    """YYYY-MM-DD tag for snapshot filenames."""
    return str(pd.Timestamp(ts).date())


def datestr_from(path, prefix):
    b = os.path.basename(path).replace(prefix, "").replace(".csv", "")
    return b if len(b) == 8 and b.isdigit() else None


MAINBOARD_SERIES = {"EQ", "BE", "BZ"}


def load_bhavcopy():
    files = sorted(glob.glob(os.path.join(ROOT, "data", "sec_bhavdata_full_*.csv")))
    rows, skipped = [], 0
    series_seen = {}
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
        # ---- INGEST FIX: keep all mainboard equity series, not just EQ -------
        # A `== "EQ"` filter silently discarded 25% of every daily file. Any
        # symbol moved to Trade-for-Trade (BE) or flagged non-compliant (BZ)
        # vanished from the panel with no error: STLTECH went BE on 2026-05-14
        # and was absent for four months while trading Rs 22 cr a day. 210 of
        # the 331 "stale" symbols were live on a non-EQ series.
        #
        # BE/BZ matter *more* than average, not less: that is where surveillance
        # sends a stock, so this is exactly the population the risk layer must
        # keep seeing — especially for a position already held.
        #
        # Deliberately excluded: GS/GB (government securities), IV/RR/E1
        # (InvITs, REITs), SM/ST (SME platform - thin floats, different lot
        # rules; revisit as an explicit decision, not an accident).
        if "SERIES" in df.columns:
            ser = df["SERIES"].astype(str).str.strip()
            df = df[ser.isin(MAINBOARD_SERIES)].copy()
            df["SERIES"] = ser[ser.isin(MAINBOARD_SERIES)]
            series_seen[ds] = ser.value_counts().to_dict()
        cols = ["SYMBOL", "CLOSE_PRICE", "PREV_CLOSE"]
        if "SERIES" in df.columns:
            cols.append("SERIES")
        # HIGH/LOW are needed for a true ATR. Without them ATR degrades to mean
        # absolute close-to-close change, which understates it by roughly 35% and
        # makes every stock look more extended than it is.
        for opt in ("HIGH_PRICE", "LOW_PRICE", "TURNOVER_LACS", "DELIV_PER"):
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
                          "HIGH_PRICE": "high", "LOW_PRICE": "low",
                          "DELIV_PER": "delivery_pct", "SERIES": "series"})
    d["symbol"] = d["symbol"].astype(str).str.strip()
    for c in ("close", "prev_close", "turnover", "delivery_pct", "high", "low"):
        if c in d.columns:
            d[c] = pd.to_numeric(d[c], errors="coerce")
    # BE/BZ rows carry no DELIV_PER (NSE prints "-"). Leave it NaN so gates read
    # UNVERIFIED rather than FAIL, and so delivery-weighted scoring neutralizes.
    return d.dropna(subset=["close"]), len(files), skipped, series_seen


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

    raw, n_files, n_skipped, series_seen = load_bhavcopy()
    print("bhavcopy: %d files, %d skipped, %d rows, %d symbols"
          % (n_files, n_skipped, len(raw), raw["symbol"].nunique()))
    if n_skipped:
        warnings.append("%d bhavcopy file(s) unparseable" % n_skipped)

    # ---- INGEST RECONCILIATION -------------------------------------------
    # Every row in each raw file is either kept or excluded for a named reason.
    # An unexplained delta means the parser is losing data, which is how a live
    # symbol can disappear for four months without a single error.
    if series_seen:
        latest_key = max(series_seen)
        counts = series_seen[latest_key]
        kept_n = sum(v for k, v in counts.items() if k in MAINBOARD_SERIES)
        excl = {k: v for k, v in counts.items() if k not in MAINBOARD_SERIES}
        total = sum(counts.values())
        print("series (%s): kept %d [%s] | excluded %d [%s]"
              % (latest_key, kept_n,
                 ", ".join("%s %d" % (k, counts[k]) for k in sorted(MAINBOARD_SERIES)
                           if k in counts),
                 total - kept_n,
                 ", ".join("%s %d" % (k, v) for k, v in sorted(excl.items()))))
        if kept_n + (total - kept_n) != total:
            warnings.append("series reconciliation failed for %s" % latest_key)
        # Known-and-deliberately-excluded. N1-N9 are non-convertible debentures
        # (M&MFIN N3 trades near Rs 2,337 while its equity is near Rs 300 - a
        # different instrument entirely). GS/GB gilts, IV/RR InvITs and REITs,
        # SM/ST the SME platform, W* warrants, Y1 when-issued.
        KNOWN_EXCLUDED = ({"GS", "GB", "IV", "RR", "E1", "SM", "ST", "GC", "GZ",
                           "MF", "NA", "W1", "W2", "W3", "Y1", "nan"}
                          | {"N%d" % i for i in range(1, 10)})
        unknown = set(excl) - KNOWN_EXCLUDED
        if unknown:
            warnings.append("unrecognised NSE series present and excluded: %s. "
                            "Decide explicitly whether these belong in the panel."
                            % ", ".join(sorted(unknown)))
        n_be = counts.get("BE", 0) + counts.get("BZ", 0)
        if n_be:
            print("   %d symbol(s) on BE/BZ (trade-for-trade / non-compliant). "
                  "These are now retained; NSE prints no delivery for them, so "
                  "G12 will read UNVERIFIED rather than FAIL." % n_be)

    Cadj, n_ev, n_sym = adjust(raw)
    print("corporate actions: %d events across %d symbols, back-adjusted" % (n_ev, n_sym))

    panel = Cadj.stack().reset_index()
    panel.columns = ["date", "symbol", "close"]
    panel = panel.dropna(subset=["close"])          # pandas 3.x stack() keeps NaN
    panel["date"] = pd.to_datetime(panel["date"], format="%Y%m%d")
    extra = raw.copy()
    extra["date"] = pd.to_datetime(extra["date"], format="%Y%m%d")
    keep = ["date", "symbol"] + [c for c in ("delivery_pct", "turnover", "high",
                                             "low", "series")
                                 if c in extra.columns]
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

    # ---- cap classification from index membership ----
    # SEBI's definition maps exactly onto NSE index membership, so no market-cap
    # or share-count data is needed: Nifty 100 = LARGE, Midcap 150 = MID,
    # Smallcap 250 = SMALL, and the three sum to the Nifty 500.
    CAP_FILES = [("ind_nifty100list.csv", "LARGE"),
                 ("ind_niftymidcap150list.csv", "MID"),
                 ("ind_niftysmallcap250list.csv", "SMALL")]
    cap_rows, cap_missing = [], []
    for fname, tier in CAP_FILES:
        fp = os.path.join(ROOT, "reference", fname)
        if not os.path.exists(fp):
            cap_missing.append(fname)
            continue
        cdf = pd.read_csv(fp)
        cdf.columns = [c.strip() for c in cdf.columns]
        scol = next((c for c in cdf.columns if c.lower() in ("symbol", "ticker")), None)
        if not scol:
            cap_missing.append(fname)
            continue
        for sym in cdf[scol].astype(str).str.strip().str.upper():
            cap_rows.append({"symbol": sym, "cap_tier": tier})
    if cap_rows:
        caps = pd.DataFrame(cap_rows).drop_duplicates("symbol", keep="first")
        caps.to_csv(os.path.join(ART, "caps.csv"), index=False)
        vc = caps["cap_tier"].value_counts().to_dict()
        print("caps: %d classified -> artifacts/caps.csv  (%s)"
              % (len(caps), ", ".join("%s %d" % (k, v) for k, v in sorted(vc.items()))))
        if len(caps) < 450:
            warnings.append("Only %d symbols cap-classified; expected ~500. The small/mid "
                            "tilt will be partial." % len(caps))
    else:
        warnings.append("No cap-tier lists in reference/. The small/mid-cap tilt cannot be "
                        "applied, and that is one of the strategy's stated objectives. "
                        "Run fetch_nse.py to pull the Nifty 100 / Midcap 150 / Smallcap 250 lists.")
        print("caps: NONE — small/mid tilt inoperative")
    if cap_missing:
        warnings.append("missing cap list(s): %s" % ", ".join(cap_missing))

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

    # ---- HARNESS FIX 1: staleness detection -------------------------------
    # A symbol can stop updating without anything failing: renamed, delisted,
    # restructured, or silently dropped by the ingest. Downstream this shows up
    # only as a NaN ADV, which every gate reads as UNVERIFIED and skips. STLTECH
    # stopped on 2026-05-13 and went unnoticed for four months.
    latest = panel["date"].max()
    lastseen = panel.groupby("symbol")["date"].max()
    stale = lastseen[lastseen < latest - pd.Timedelta(days=10)].sort_values()
    stale_out = os.path.join(ART, "stale_symbols.csv")
    if len(stale):
        sd = stale.reset_index()
        sd.columns = ["symbol", "last_session"]
        sd["last_session"] = pd.to_datetime(sd["last_session"])
        sd["days_stale"] = (pd.Timestamp(latest) - sd["last_session"]).dt.days
        sd.to_csv(stale_out, index=False)
        recent = sd[sd.days_stale <= 120]
        warnings.append(
            "%d symbol(s) stale (no data for >10 sessions); %d went stale in the "
            "last 120 days. See artifacts/stale_symbols.csv. A stale symbol yields "
            "NaN ADV and is silently skipped by every liquidity gate."
            % (len(sd), len(recent)))
        print("stale: %d symbols -> artifacts/stale_symbols.csv" % len(sd))
        for _, r in recent.head(10).iterrows():
            print("   %-14s last %s (%d days)"
                  % (r.symbol, pd.Timestamp(r.last_session).date(), r.days_stale))
    else:
        pd.DataFrame(columns=["symbol", "last_session", "days_stale"]).to_csv(
            stale_out, index=False)
        print("stale: none")

    # ---- HARNESS FIX 2: archive constituent lists point-in-time ------------
    # sector_map_v1.csv is built from the CURRENT Nifty 500 list. Any replay that
    # uses it gives the map foreknowledge of index additions, flattering recall.
    # Nothing can reconstruct past membership, so start archiving now: within a
    # few months a point-in-time replay becomes possible.
    snapdir = os.path.join(ROOT, "reference", "snapshots")
    os.makedirs(snapdir, exist_ok=True)
    n_snap = 0
    for fname in ("ind_nifty500list.csv", "ind_nifty100list.csv",
                  "ind_niftymidcap150list.csv", "ind_niftysmallcap250list.csv"):
        src = os.path.join(ROOT, "reference", fname)
        if not os.path.exists(src):
            continue
        dst = os.path.join(snapdir, "%s_%s" % (man_date_tag(latest), fname))
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
            n_snap += 1
    if n_snap:
        print("snapshots: %d constituent list(s) archived for %s"
              % (n_snap, man_date_tag(latest)))

    man = {
        "built_at_utc": datetime.now(timezone.utc).isoformat(),
        "bhavcopy_files": n_files,
        "index_files": n_idx_files if idx is not None else 0,
        "panel_sessions": int(panel["date"].nunique()),
        "panel_symbols": int(panel["symbol"].nunique()),
        "latest_session": str(panel["date"].max().date()),
        "corporate_action_events": n_ev,
        "symbols_cap_classified": len(cap_rows),
        "stale_symbols": int(len(stale)),
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
