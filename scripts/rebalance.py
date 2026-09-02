#!/usr/bin/env python3
"""
rebalance.py — L7 weekly rebalance engine.

Evaluates the decision grid per holding, applies stability vetting and the churn
cap, and assembles a Monday order plan. Submit the result to risk_monitor.py
before acting: this script proposes, L8 disposes.

Usage:
  python rebalance.py --positions positions.csv --scores scores.csv \
      --sectors "Healthcare:TOP3,Metals & Mining:TOP3,Textiles:EJECTED" \
      --regime NEUTRAL --pool 8000000 [--churn-cap 2] [--json plan.json]

Schemas:
  positions.csv  symbol,sector,cap_tier,qty,cost,stop,days_held[,is_core,tranches,ms_entry]
  scores.csv     symbol,ms,ms_prev,ms_2w_ago[,price,r_now,target_hit,add_structure,ext_atr]
"""

import argparse, json, math, sys
import pandas as pd
import numpy as np

def truthy(v):
    """CSV columns with blanks parse as float, so 1 becomes 1.0 and a naive
    string comparison against "1" silently misses every flagged row."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return False
    s = str(v).strip().upper()
    if s in ("", "NAN", "0", "0.0", "FALSE", "NO", "N"):
        return False
    return s in ("1", "1.0", "TRUE", "YES", "Y", "T")


EXIT_MS = 42
TRIM_LO, TRIM_HI = 42, 55
ADD_MS = 70
ORPHAN_MS = 70
MIN_HOLD = 15
TIME_STOP = 25


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", required=True)
    ap.add_argument("--scores", required=True)
    ap.add_argument("--sectors", required=True, help='"Name:TOP3,Name:EJECTED"')
    ap.add_argument("--regime", default="NEUTRAL",
                    choices=["RISK-ON", "NEUTRAL", "RISK-OFF", "SHOCK"])
    ap.add_argument("--pool", type=float, default=8000000.0)
    ap.add_argument("--churn-cap", type=int, default=2)
    ap.add_argument("--json")
    a = ap.parse_args()

    sec_status = {}
    for part in a.sectors.split(","):
        if ":" in part:
            k, v = part.rsplit(":", 1)
            sec_status[k.strip()] = v.strip().upper()

    pos = pd.read_csv(a.positions)
    pos.columns = [c.lower().strip() for c in pos.columns]
    sc = pd.read_csv(a.scores)
    sc.columns = [c.lower().strip() for c in sc.columns]
    for d in (pos, sc):
        d["symbol"] = d["symbol"].astype(str).str.upper()
    d = pos.merge(sc, on="symbol", how="left", suffixes=("", "_s"))
    if d["ms"].isna().any():
        missing = d.loc[d["ms"].isna(), "symbol"].tolist()
        print("!! No score for: %s. These are PARTIAL — protective actions only.\n"
              % ", ".join(missing))

    rows = []
    for _, r in d.iterrows():
        ms = r.get("ms", np.nan)
        prev = r.get("ms_prev", np.nan)
        w2 = r.get("ms_2w_ago", np.nan)
        dm1 = ms - prev if pd.notna(ms) and pd.notna(prev) else np.nan
        dm2 = ms - w2 if pd.notna(ms) and pd.notna(w2) else np.nan
        dm1_prev = prev - w2 if pd.notna(prev) and pd.notna(w2) else np.nan
        stat = sec_status.get(str(r.get("sector", "")), "TOP3")
        core = bool(r.get("is_core", 0))
        held = int(r.get("days_held", 0) or 0)
        rnow = float(r.get("r_now", 0) or 0)
        partial = pd.isna(ms)
        row, act, why, defer = 12, "HOLD", "no rule fired", None

        if a.regime == "SHOCK" and not core:
            row, act, why = 2, "EXIT", "regime SHOCK, exit over <=3 sessions"
        elif pd.notna(ms) and ms < EXIT_MS:
            row, act, why = 3, "EXIT", "MS %.0f below %d" % (ms, EXIT_MS)
        elif (pd.notna(dm1) and pd.notna(dm1_prev) and dm1 <= -10 and dm1_prev <= -10
              and pd.notna(ms) and ms < 60):
            row, act, why = 4, "EXIT", "dMS <= -10 two weeks running, MS %.0f < 60" % ms
        elif stat == "EJECTED" and pd.notna(ms) and ms < ORPHAN_MS and not core:
            row, act, why = 5, "TRIM 50%", "sector ejected, MS %.0f < 70" % ms
        elif stat == "EJECTED" and pd.notna(ms) and ms >= ORPHAN_MS:
            row, act, why = 6, "HOLD (ORPHAN)", "sector ejected but MS %.0f — 3-week clock" % ms
        elif (held >= TIME_STOP and rnow < 0.5 and pd.notna(ms)
              and pd.notna(r.get("ms_entry", np.nan)) and ms < float(r["ms_entry"])):
            row, act, why = 7, "EXIT", ("time stop: %d sessions, %+.1fR, MS decayed %.0f->%.0f"
                                        % (held, rnow, float(r["ms_entry"]), ms))
        elif str(r.get("target_hit", "")).strip().upper() in ("T1", "T2", "T3") or truthy(r.get("target_hit")):
            row, act, why = 8, "BOOK %s" % str(r.get("target_hit")).upper(), "target reached"
        elif pd.notna(ms) and (TRIM_LO <= ms <= TRIM_HI or (pd.notna(dm2) and dm2 <= -15)):
            row, act, why = 9, "TRIM 33%", ("MS %.0f in trim band" % ms if TRIM_LO <= ms <= TRIM_HI
                                            else "dMS(2w) %.0f <= -15" % dm2)
        elif (pd.notna(ms) and ms >= ADD_MS and pd.notna(dm1) and dm1 >= 0 and stat == "TOP3"
              and truthy(r.get("add_structure"))):
            row, act, why = 10, "ADD", "MS %.0f, dMS %+.0f, sector TOP3, valid structure" % (ms, dm1)
            ext = r.get("ext_atr", np.nan)
            if pd.notna(ext) and float(ext) > 2.5:
                row, act, why = 12, "HOLD", "add blocked: %.1f ATR above 20 DMA (cap 2.5)" % float(ext)

        # ---- stability vetting ----
        if act.startswith("TRIM") or (act == "EXIT" and row in (4, 9)):
            if held < MIN_HOLD:
                defer = "minimum hold: only %d of %d sessions" % (held, MIN_HOLD)
                act, why = "HOLD", "suppressed — " + defer
        if partial and act in ("ADD",):
            defer = "PARTIAL score may not trigger an add"
            act, why = "HOLD", "suppressed — " + defer

        rows.append(dict(symbol=r["symbol"], sector=r.get("sector", ""), ms=ms, dm1=dm1, dm2=dm2,
                         sec=stat, r=rnow, held=held, row=row, action=act, why=why, defer=defer,
                         qty=int(r.get("qty", 0) or 0), price=float(r.get("price", r.get("cost", 0)) or 0)))
    # An empty positions file is a legitimate state, not an error: it is exactly
    # what the first run of a new book looks like. Without explicit columns the
    # DataFrame has no .action attribute and every downstream reference crashes.
    COLS = ["symbol", "sector", "ms", "dm1", "dm2", "sec", "r", "held",
            "row", "action", "why", "defer", "qty", "price"]
    g = pd.DataFrame(rows, columns=COLS) if not rows else pd.DataFrame(rows)

    # ---- churn cap: exits, then trims, then adds ----
    PRIORITY = {"EXIT": 0, "TRIM 50%": 1, "TRIM 33%": 1, "ADD": 2}
    counts = g[g["action"].isin(PRIORITY)].copy()
    counts["p"] = counts.action.map(PRIORITY)
    counts = counts.sort_values(["p", "ms"])
    allowed, deferred = [], []
    for i, (_, r) in enumerate(counts.iterrows()):
        (allowed if i < a.churn_cap else deferred).append(r["symbol"])
    for i, r in g.iterrows():
        if r["symbol"] in deferred:
            g.at[i, "defer"] = "churn cap (%d/week) — deferred to next Saturday" % a.churn_cap
            g.at[i, "action"] = "DEFER " + r["action"]

    print("=" * 104)
    print("WEEKLY REBALANCE — regime %s | churn cap %d | %d holdings"
          % (a.regime, a.churn_cap, len(g)))
    print("sectors: %s" % ", ".join("%s=%s" % (k, v) for k, v in sec_status.items()))
    print("=" * 104)
    print("%-13s %-18s %5s %6s %6s %9s %6s %5s %4s  %-14s %s"
          % ("SYMBOL", "SECTOR", "MS", "dMS1w", "dMS2w", "SECTOR", "R", "HELD", "ROW", "ACTION", "REASON"))
    print("-" * 104)
    for _, r in g.iterrows():
        f = lambda v, s="%+.0f": "--" if pd.isna(v) else s % v
        print("%-13s %-18s %5s %6s %6s %9s %6.1f %5d %4d  %-14s %s"
              % (r.symbol, str(r.sector)[:18], f(r.ms, "%.0f"), f(r.dm1), f(r.dm2),
                 r.sec, r.r, r.held, r.row, r.action, r.why[:34]))
    print("-" * 104)
    sup = g[g.defer.notna()]
    print("SUPPRESSED / DEFERRED")
    if len(sup):
        for _, r in sup.iterrows():
            print("   %-13s %s" % (r.symbol, r.defer))
    else:
        print("   none")
    print("-" * 104)
    print("ORDER PLAN FOR MONDAY")
    orders = []
    for _, r in g.iterrows():
        act = r.action
        if act.startswith("DEFER") or act.startswith("HOLD"):
            continue
        if act == "EXIT":
            q = r.qty
        elif act == "TRIM 50%":
            q = int(r.qty * 0.5)
        elif act == "TRIM 33%":
            q = int(r.qty * 0.33)
        elif act.startswith("BOOK"):
            q = int(r.qty * 0.25)
        elif act == "ADD":
            q = None
        else:
            continue
        side = "BUY" if act == "ADD" else "SELL"
        lim = r.price * (0.995 if side == "SELL" else 1.005) if r.price else None
        orders.append(dict(symbol=r.symbol, side=side, action=act, qty=q,
                           type="LIMIT", limit=None if lim is None else round(lim, 2)))
        print("   %-5s %-13s %-12s qty %-7s limit %s"
              % (side, r.symbol, act, q if q else "size via L4", ("%.2f" % lim) if lim else "-"))
    if not orders:
        print("   no orders — every holding is HOLD or deferred. This is a normal week.")
    print("-" * 104)
    print("NEXT STEP: submit this plan to portfolio-risk-monitor (L8) before placing anything.")
    print("           L7 proposes; L8 can veto and the veto stands.")
    print("=" * 104)

    if a.json:
        json.dump({"regime": a.regime, "sectors": sec_status,
                   "holdings": g.drop(columns=["price"]).to_dict("records"),
                   "orders": orders,
                   "deferred": sup[["symbol", "defer"]].to_dict("records")},
                  open(a.json, "w"), indent=2, default=str)
        print("JSON written to %s" % a.json)


if __name__ == "__main__":
    main()
