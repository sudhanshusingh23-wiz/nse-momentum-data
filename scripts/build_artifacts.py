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
    date_mismatch = []
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
        if "DATE1" in df.columns:
            pass  # read separately below; not carried into the panel
        if "SERIES" in df.columns:
            cols.append("SERIES")
        # HIGH/LOW are needed for a true ATR. Without them ATR degrades to mean
        # absolute close-to-close change, which understates it by roughly 35% and
        # makes every stock look more extended than it is.
        for opt in ("HIGH_PRICE", "LOW_PRICE", "TURNOVER_LACS", "DELIV_PER"):
            if opt in df.columns:
                cols.append(opt)
        # ---- PHANTOM-SESSION FIX --------------------------------------------
        # The date must come from DATE1 INSIDE the file, never from the
        # filename. NSE serves the PREVIOUS session's file when you request a
        # market-holiday URL, so a filename-derived date stamps a real session
        # with a date on which nothing traded. That injected ~60 flat sessions
        # into 2021-24: Republic Day 2023, Holi 2023 and Independence Day 2024
        # all carry full rows with 99.9% of closes identical to the prior day.
        #
        # It also caused a phantom corporate action: the 15-Jan-2026 holiday
        # file repeated 14-Jan data, and the duplicate fired a 5.07x split
        # factor on KOTAKBANK in the wrong direction.
        #
        # Flat sessions depress ATR and realised volatility, and shift every
        # rolling window. Reject the mismatch rather than trying to repair it.
        file_date = ds[4:] + ds[2:4] + ds[:2]       # DDMMYYYY -> YYYYMMDD
        sub = df[cols].copy()
        if "DATE1" in df.columns:
            inner = pd.to_datetime(df["DATE1"].astype(str).str.strip(),
                                   format="%d-%b-%Y", errors="coerce")
            if inner.notna().any():
                stamped = inner.dt.strftime("%Y%m%d")
                keep = stamped == file_date
                if not keep.all():
                    got = sorted(stamped[~keep].dropna().unique())[:1]
                    date_mismatch.append((os.path.basename(p), file_date,
                                          got[0] if got else "?",
                                          int((~keep).sum())))
                sub = sub[keep.values].copy()
                if sub.empty:
                    continue
        sub["date"] = file_date
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
    d = d.dropna(subset=["close"])
    # One row per symbol per session. Belt and braces after the DATE1 check.
    before = len(d)
    d = d.drop_duplicates(subset=["date", "symbol"], keep="last")
    n_dupes = before - len(d)
    return d, len(files), skipped, series_seen, date_mismatch, n_dupes


def adjust(d):
    """Back-adjust for splits/bonuses using NSE's adjusted PREV_CLOSE.

    Returns the adjusted close AND the factor matrix. HIGH and LOW must be
    adjusted by the SAME factor: previously only close was adjusted, which left
    52 symbols with an adjusted close sitting beside a raw high/low - KOTAKBANK
    showed a close of 10,806 against a high of 2,139. Every ATR, stop, MAE and
    MFE computed on those rows was wrong.
    """
    C = d.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    P = d.pivot_table(index="date", columns="symbol", values="prev_close",
                      aggfunc="last").sort_index().reindex_like(C)
    R = P / C.shift(1)
    # only where this session AND the immediately preceding session both traded,
    # otherwise a data gap masquerades as a corporate action
    R = R.where(C.notna() & C.shift(1).notna() & P.notna())
    events = R.where((R < CA_LOW) | (R > CA_HIGH))

    # ---- SECOND DETECTOR: the close series itself ---------------------------
    # PREV_CLOSE is the primary signal, but NSE does not always publish an
    # ADJUSTED prev close on the ex-date. On KOTAKBANK's 1:5 split (14 Jan 2026)
    # it printed the raw 2132.60 beside a post-split close of 421.00, so
    # R = 1.00 and nothing fired. Catch that case directly: a single-session
    # move whose inverse ratio lands within 6% of a whole number >= 2 is a
    # split or bonus, not a price move. No stock halves or thirds in one
    # session and stays there.
    step = C / C.shift(1)
    step = step.where(C.notna() & C.shift(1).notna())
    inv = 1.0 / step

    # A split day carries the day's own price move as well as the ratio, so the
    # observed number is never exactly the split ratio: KOTAKBANK's 1:5 printed
    # 5.066, not 5.000. Snap to the nearest whole number and use THAT as the
    # factor - the residual is a genuine price move and must survive adjustment.
    # Tolerance is deliberately loose (15%) because the discriminator is not
    # the tolerance, it is the size of the move: NSE price bands mean a real
    # single-session fall past 35% is a corporate action, not a price.
    ratio_dn = inv.round()
    near_dn = (inv - ratio_dn).abs() / ratio_dn < 0.15
    implied = (1.0 / ratio_dn).where(near_dn & (ratio_dn >= 2) & (step < 0.65))

    # consolidation / reverse split: the ratio itself is a whole number
    ratio_up = step.round()
    near_up = (step - ratio_up).abs() / ratio_up < 0.15
    implied_up = ratio_up.where(near_up & (ratio_up >= 2) & (step > 1.55))

    from_close = implied.combine_first(implied_up)
    # only where PREV_CLOSE did not already catch it
    from_close = from_close.where(events.isna())
    n_from_close = int(from_close.notna().sum().sum())
    events = events.combine_first(from_close)

    n_events = int(events.notna().sum().sum())
    n_syms = int((events.notna().sum() > 0).sum())
    Rc = events.fillna(1.0)
    factor = Rc[::-1].cumprod()[::-1].shift(-1).fillna(1.0)   # product of SUBSEQUENT ratios
    return C * factor, factor, n_events, n_syms, n_from_close


def apply_factor(d, factor, col):
    """Apply the corporate-action factor matrix to a raw OHLC column."""
    M = d.pivot_table(index="date", columns="symbol", values=col,
                      aggfunc="last").sort_index().reindex_like(factor)
    return (M * factor).stack().rename(col)


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

    raw, n_files, n_skipped, series_seen, date_mismatch, n_dupes = load_bhavcopy()
    if date_mismatch:
        print("phantom sessions rejected: %d file(s) whose DATE1 did not match "
              "the filename date" % len(date_mismatch))
        for fn, want, got, nrows in date_mismatch[:8]:
            print("   %s: filename says %s, file contains %s (%d rows dropped)"
                  % (fn, want, got, nrows))
        if len(date_mismatch) > 8:
            print("   ... %d more" % (len(date_mismatch) - 8))
        pd.DataFrame(date_mismatch,
                     columns=["file", "filename_date", "file_date", "rows_dropped"]
                     ).to_csv(os.path.join(ART, "phantom_sessions.csv"), index=False)
        warnings.append("%d bhavcopy file(s) carried a different DATE1 to their "
                        "filename and were rejected - almost always NSE serving the "
                        "prior session for a market holiday. See "
                        "artifacts/phantom_sessions.csv." % len(date_mismatch))
    else:
        print("phantom sessions: none (every file's DATE1 matched its filename)")
    if n_dupes:
        print("duplicate (date, symbol) rows removed: %d" % n_dupes)
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

    Cadj, ca_factor, n_ev, n_sym, n_close_detected = adjust(raw)
    print("corporate actions: %d events across %d symbols, back-adjusted "
          "(%d found via the close series where PREV_CLOSE was unadjusted)"
          % (n_ev, n_sym, n_close_detected))

    panel = Cadj.stack().reset_index()
    panel.columns = ["date", "symbol", "close"]
    panel = panel.dropna(subset=["close"])          # pandas 3.x stack() keeps NaN
    panel["date"] = pd.to_datetime(panel["date"], format="%Y%m%d")
    extra = raw.copy()
    extra["date"] = pd.to_datetime(extra["date"], format="%Y%m%d")
    # high/low carry the SAME corporate-action factor as close. Merging them raw
    # leaves adjusted closes beside unadjusted highs, which silently corrupts ATR.
    hl = []
    for col in ("high", "low"):
        if col in raw.columns:
            adj = apply_factor(raw, ca_factor, col).reset_index()
            adj.columns = ["date", "symbol", col]
            adj["date"] = pd.to_datetime(adj["date"], format="%Y%m%d")
            hl.append(adj)
    keep = ["date", "symbol"] + [c for c in ("delivery_pct", "turnover", "series")
                                 if c in extra.columns]
    panel = panel.merge(extra[keep].drop_duplicates(["date", "symbol"]),
                        on=["date", "symbol"], how="left")
    for adj in hl:
        panel = panel.merge(adj.drop_duplicates(["date", "symbol"]),
                            on=["date", "symbol"], how="left")
    # assert consistency: close must sit inside the high/low band on every row
    if {"high", "low"}.issubset(panel.columns):
        band = panel.dropna(subset=["close", "high", "low"])
        off = band[(band["close"] > band["high"] * 1.02)
                   | (band["close"] < band["low"] * 0.98)]
        if len(off):
            warnings.append("%d row(s) across %d symbol(s) have close outside the "
                            "high/low band after adjustment - corporate-action "
                            "factors may be misaligned. See the ca_mismatch artifact."
                            % (len(off), off["symbol"].nunique()))
            off.to_csv(os.path.join(ART, "ca_mismatch.csv"), index=False)
        else:
            print("   OHLC consistency: close within high/low band on all %d rows"
                  % len(band))
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
        caps["cap_source"] = "nse_index"

        # ---- COVERAGE FIX ---------------------------------------------------
        # The three NSE lists cover the Nifty 500 and nothing else. Every other
        # symbol arrived downstream with no tier and was defaulted to SMALL,
        # which the RISK-OFF mandate ("large/mid only") then blocked. That is
        # 42% of the tradeable universe and 95% of the top 40 three-month
        # movers - TBZ, KABRAEXTRU, CYIENTDLM, INDSWFTLAB and the rest - locked
        # out for being un-indexed rather than for being small.
        #
        # We have no share counts, so real market cap cannot be computed. A
        # turnover proxy was tested against the known 500 and reached only 62%
        # accuracy (41% on MID), so it is NOT presented as a size judgement.
        # These names get the tier BROAD - a membership fact, not a size claim -
        # plus two liquidity columns the eligibility rule gates on directly:
        #
        #   adv_cr      45-session average turnover, Rs cr
        #   dlv_adv_cr  the same, multiplied by delivery% - the part that
        #               actually settles rather than being churned intraday
        #
        # dlv_adv_cr is the one that matters. Two names can show the same ADV
        # and differ tenfold in real liquidity: ITDC Rs 37cr at 25.5% delivery
        # is Rs 3.4cr of genuine flow; SJS Rs 42cr at 55.1% is Rs 22.7cr.
        known = set(caps["symbol"])
        # BOTH liquidity columns must use the SAME window, or dlv_adv_cr can
        # exceed adv_cr, which is arithmetically impossible: it is adv_cr times
        # a percentage. A first version computed adv_cr over 252 sessions and
        # dlv_adv_cr over 45, and names whose recent turnover ran above their
        # yearly average came out inverted - KABRAEXTRU printed ADV 5.4 beside
        # a delivery-adjusted 9.10. 45 sessions for both: it matches the window
        # every gate and sizing rule already uses.
        LIQ_WINDOW = 45
        try:
            _tv = (panel.pivot_table(index="date", columns="symbol",
                                     values="turnover", aggfunc="last")
                   .tail(LIQ_WINDOW))
            _dv = (panel.pivot_table(index="date", columns="symbol",
                                     values="delivery_pct", aggfunc="last")
                   .tail(LIQ_WINDOW).reindex(columns=_tv.columns))
            # Both averages must run over the SAME rows. NSE prints no
            # DELIV_PER for BE/BZ sessions, so delivery is NaN on some days.
            # Averaging turnover over 45 rows and delivered turnover over
            # whichever rows had delivery gives different denominators, and any
            # name whose delivery-bearing days were its busiest came out with
            # dlv_adv_cr ABOVE adv_cr. DIACABS had delivery on 7 of 45 days and
            # printed 124.78 against an ADV of 78.5.
            _mask_ok = _tv.notna() & _dv.notna()
            _n = float(max(len(_tv.index), 1))
            adv_45 = _tv.sum() / _n / 100.0
            # Delivered turnover summed over the window and divided by the FULL
            # session count, not by the days that happened to report delivery.
            # NSE prints no DELIV_PER for BE/BZ days, and averaging over only
            # the reporting days gave a figure that could exceed ADV outright:
            # DIACABS reported delivery on 7 of 45 sessions - its busiest - and
            # printed 124.78 against an ADV of 78.5.
            #
            # Treating a non-reporting day as zero delivered is also the right
            # economics: on a trade-for-trade day there is no delivery to count.
            # A name that spends half the window on BE therefore scores half the
            # delivered liquidity, which is exactly the penalty intended.
            # By construction dlv_adv_cr <= adv_cr for every row.
            dlv_adv = (_tv.where(_mask_ok) * _dv / 100.0).sum() / _n / 100.0
            _cover = _mask_ok.sum() / _n
        except Exception:
            adv_45 = pd.Series(dtype=float)
            dlv_adv = pd.Series(dtype=float)
        extra = []
        for sym in sorted(set(panel["symbol"]) - known):
            a = float(adv_45.get(sym, float("nan")))
            if a != a:
                continue
            da = float(dlv_adv.get(sym, float("nan")))
            cv = float(_cover.get(sym, float("nan")))
            extra.append({"symbol": sym, "cap_tier": "BROAD",
                          "cap_source": "not_in_nse_index", "adv_cr": round(a, 1),
                          "dlv_adv_cr": round(da, 2) if da == da else None,
                          "dlv_cover": round(cv, 2) if cv == cv else None})
        if extra:
            caps = pd.concat([caps, pd.DataFrame(extra)], ignore_index=True)
        caps["adv_cr"] = caps.get("adv_cr", pd.Series(dtype=float))
        miss = caps["adv_cr"].isna()
        if miss.any():
            caps.loc[miss, "adv_cr"] = caps.loc[miss, "symbol"].map(adv_45).round(1)
        # Eligibility floor for BROAD names. Set from evidence, not from exit
        # arithmetic: across 33 Fridays and 11,646 observations the 4-week
        # forward excess return rises monotonically with dlv_adv_cr, and the
        # Rs 6-10cr band is the WORST of six (+1.43%, 48% hit, negative median).
        # Above Rs 6cr beats below by +1.53pp at t = +2.89; moving the floor to
        # Rs 10cr lifts the kept-set return from +2.74% to +3.68%. Rs 20cr would
        # be better still but leaves only ~7% of the broad universe.
        BROAD_DLV_ADV_FLOOR = 10.0
        if "dlv_adv_cr" not in caps.columns:
            caps["dlv_adv_cr"] = pd.Series(dtype=float)
        caps.loc[caps["dlv_adv_cr"].isna(), "dlv_adv_cr"] = (
            caps.loc[caps["dlv_adv_cr"].isna(), "symbol"].map(dlv_adv).round(2))
        # dlv_adv_cr <= adv_cr must hold for every row, always.
        chk = caps.dropna(subset=["adv_cr", "dlv_adv_cr"])
        bad_liq = chk[chk["dlv_adv_cr"] > chk["adv_cr"] * 1.001]
        if len(bad_liq):
            warnings.append("%d row(s) in caps.csv have dlv_adv_cr > adv_cr, which is "
                            "impossible - the two columns are on different windows."
                            % len(bad_liq))
            print("   WARNING: %d caps rows have dlv_adv_cr > adv_cr" % len(bad_liq))
        else:
            print("   liquidity columns consistent: dlv_adv_cr <= adv_cr on all %d rows"
                  % len(chk))

        n_broad = int((caps["cap_tier"] == "BROAD").sum())
        elig = int(((caps["cap_tier"] == "BROAD")
                    & (caps["dlv_adv_cr"] >= BROAD_DLV_ADV_FLOOR)).sum())
        print("caps coverage: %d NSE-indexed + %d BROAD; %d BROAD clear the "
              "Rs %.0f cr delivery-adjusted ADV floor"
              % (len(known), n_broad, elig, BROAD_DLV_ADV_FLOOR))

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
