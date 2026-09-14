#!/usr/bin/env python3
"""
Sector Momentum Rotation (L2) — SMS scoring, hysteresis, correlation guard, allocation.

Computes a six-component Sector Momentum Score across all mapped sectors,
percentile-ranks cross-sectionally, applies the asymmetric hysteresis bands,
runs the correlation guard, and emits allocation weights and budgets.

Usage:
  python sector_score.py --prices prices.csv --map map.csv \
      [--sector-index sector_index.csv] [--state state.json] \
      [--asof YYYY-MM-DD] [--regime RISK-ON] [--pool 8000000] \
      [--out-state new_state.json] [--json out.json]

Schemas documented in references/sms-scoring.md.
"""

import argparse, json, sys
import numpy as np
import pandas as pd

WEIGHTS = {"abs_mom": 0.30, "risk_adj": 0.20, "breadth": 0.20,
           "trend": 0.15, "accel": 0.10, "particip": 0.05}
DEPLOY = {"RISK-ON": 0.85, "NEUTRAL": 0.60, "RISK-OFF": 0.30, "SHOCK": 0.20}


def pctile(series):
    """Cross-sectional percentile rank, 0-100. NaNs preserved."""
    s = pd.Series(series, dtype="float64")
    n = s.notna().sum()
    if n <= 1:
        return pd.Series([50.0 if pd.notna(v) else np.nan for v in s], index=s.index)
    return s.rank(method="average", na_option="keep").sub(1).div(n - 1).mul(100)


def ret(series, n):
    if len(series) <= n:
        return np.nan
    a, b = series.iloc[-1], series.iloc[-1 - n]
    if b == 0 or pd.isna(a) or pd.isna(b):
        return np.nan
    return (a / b - 1) * 100


def build_sector_series(prices_wide, members):
    """Equal-weighted index from constituent closes, base 1000."""
    cols = [c for c in members if c in prices_wide.columns]
    if not cols:
        return None
    sub = prices_wide[cols].dropna(how="all")
    norm = sub.div(sub.ffill().bfill().iloc[0])
    idx = norm.mean(axis=1, skipna=True) * 1000
    return idx.dropna()


def trend_integrity(wk):
    """Points out of 20 on weekly closes."""
    if len(wk) < 32:
        return np.nan
    w10 = wk.rolling(10).mean()
    w30 = wk.rolling(30).mean()
    if pd.isna(w30.iloc[-1]):
        return np.nan
    pts = 0.0
    if wk.iloc[-1] > w10.iloc[-1]:
        pts += 3
    if wk.iloc[-1] > w30.iloc[-1]:
        pts += 4
    if len(w30) > 8 and not pd.isna(w30.iloc[-9]) and w30.iloc[-1] > w30.iloc[-9]:
        pts += 4
    if w10.iloc[-1] > w30.iloc[-1]:
        pts += 3
    last12 = wk.iloc[-12:]
    hh = sum(1 for i in range(1, len(last12)) if last12.iloc[i] > last12.iloc[:i].max())
    pts += min(6.0, hh * 1.5)
    return pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prices", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--sector-index")
    ap.add_argument("--state")
    ap.add_argument("--asof")
    ap.add_argument("--regime", default="RISK-ON",
                    choices=["RISK-ON", "NEUTRAL", "RISK-OFF", "SHOCK"])
    ap.add_argument("--pool", type=float, default=8000000.0)
    ap.add_argument("--out-state")
    ap.add_argument("--json")
    a = ap.parse_args()

    asof = pd.to_datetime(a.asof) if a.asof else None

    px = pd.read_csv(a.prices)
    px.columns = [c.lower() for c in px.columns]
    px["date"] = pd.to_datetime(px["date"])
    if asof is not None:
        px = px[px["date"] <= asof]
    smap = pd.read_csv(a.map)
    smap.columns = [c.lower() for c in smap.columns]

    wide = px.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    has_vol = "volume" in px.columns
    volw = (px.pivot_table(index="date", columns="symbol", values="volume", aggfunc="last").sort_index()
            if has_vol else None)
    has_del = "delivery_pct" in px.columns
    delw = (px.pivot_table(index="date", columns="symbol", values="delivery_pct", aggfunc="last").sort_index()
            if has_del else None)

    sidx = None
    if a.sector_index:
        si = pd.read_csv(a.sector_index)
        si.columns = [c.lower() for c in si.columns]
        si["date"] = pd.to_datetime(si["date"])
        if asof is not None:
            si = si[si["date"] <= asof]
        sidx = si

    prior = json.load(open(a.state)) if a.state else None
    prior_ranks = (prior or {}).get("ranks", {})
    prior_sel = set((prior or {}).get("selected", []))
    pending = dict((prior or {}).get("entry_pending", {}))

    sectors = sorted(smap["sector"].unique())
    rows, series_store = [], {}
    series_src = {}
    notes = []

    for sec in sectors:
        members = smap.loc[smap["sector"] == sec, "symbol"].tolist()
        # price series: supplied index if available, else equal-weight basket
        # Prefer the official index, but fall back to the equal-weighted constituent
        # basket when the index series is too short. NSE launches new sector indices
        # regularly, and a 60-session index should not cost us a sector we have five
        # years of constituent data for.
        s, src = None, None
        if sidx is not None and (sidx["sector"] == sec).any():
            cand = sidx[sidx["sector"] == sec].set_index("date")["close"].sort_index()
            if len(cand) >= 130:
                s, src = cand, "official index"
            else:
                basket = build_sector_series(wide, members)
                if basket is not None and len(basket) >= 130:
                    s, src = basket, "equal-weight basket"
                    notes.append("%s: official index has only %d sessions, used "
                                 "equal-weight constituent basket instead" % (sec, len(cand)))
                else:
                    s, src = cand, "official index (short)"
        if s is None:
            s = build_sector_series(wide, members)
            src = "equal-weight basket"
        if s is None or len(s) < 130:
            notes.append("%s: insufficient price history (%s), excluded"
                         % (sec, "no data" if s is None else "%d sessions" % len(s)))
            continue
        series_src[sec] = src
        series_store[sec] = s

        r21, r63, r126, r252 = ret(s, 21), ret(s, 63), ret(s, 126), ret(s, 252)
        parts = [(r21, 0.25), (r63, 0.35), (r126, 0.30), (r252, 0.10)]
        avail = [(v, w) for v, w in parts if not pd.isna(v)]
        abs_mom = sum(v * w for v, w in avail) / sum(w for _, w in avail) if avail else np.nan

        dret = s.pct_change().dropna().iloc[-126:]
        vol = max(float(dret.std() * np.sqrt(252) * 100), 5.0) if len(dret) > 20 else np.nan
        risk_adj = (r126 / vol) if (not pd.isna(r126) and not pd.isna(vol)) else np.nan

        # breadth from constituents
        cols = [c for c in members if c in wide.columns]
        breadth = np.nan
        if cols and len(wide) >= 210:
            sub = wide[cols]
            d50, d200 = sub.rolling(50).mean(), sub.rolling(200).mean()
            hi52 = sub.rolling(252, min_periods=120).max()
            last = sub.iloc[-1]
            v50 = d50.iloc[-1].notna() & last.notna()
            v200 = d200.iloc[-1].notna() & last.notna()
            vhi = hi52.iloc[-1].notna() & last.notna()
            if v50.sum() and v200.sum() and vhi.sum():
                p50 = float(((last > d50.iloc[-1]) & v50).sum()) / int(v50.sum()) * 100
                p200 = float(((last > d200.iloc[-1]) & v200).sum()) / int(v200.sum()) * 100
                near = float(((last >= hi52.iloc[-1] * 0.90) & vhi).sum()) / int(vhi.sum()) * 100
                breadth = 0.40 * p50 + 0.30 * p200 + 0.30 * near

        trend = trend_integrity(s.resample("W-FRI").last().dropna())

        # participation
        particip = np.nan
        if volw is not None and cols:
            vc = [c for c in cols if c in volw.columns]
            if vc and len(volw) >= 100:
                tv = (wide[vc] * volw[vc]).sum(axis=1, skipna=True)
                m20, m100 = tv.tail(20).mean(), tv.tail(100).mean()
                if m100 > 0:
                    t = m20 / m100
                    if delw is not None:
                        dc = [c for c in cols if c in delw.columns]
                        if dc and len(delw) >= 100:
                            d20 = delw[dc].tail(20).mean(axis=1).mean()
                            d100 = delw[dc].tail(100).mean(axis=1).mean()
                            particip = 0.60 * t + 0.40 * (d20 / d100 if d100 else 1.0)
                        else:
                            particip = t
                    else:
                        particip = t

        rows.append({"sector": sec, "abs_mom_raw": abs_mom, "risk_adj_raw": risk_adj,
                     "breadth_raw": breadth, "trend_raw": trend, "particip_raw": particip,
                     "r63_raw": r63,
                     "r63_prev_raw": ret(s.iloc[:-21], 63) if len(s) > 84 else np.nan})

    if not rows:
        print("ERROR: no sectors could be scored.", file=sys.stderr)
        sys.exit(2)

    df = pd.DataFrame(rows).set_index("sector")

    # acceleration: change in cross-sectional rank of R63
    df["accel_raw"] = pctile(df["r63_raw"]) - pctile(df["r63_prev_raw"])

    comp_src = {"abs_mom": "abs_mom_raw", "risk_adj": "risk_adj_raw", "breadth": "breadth_raw",
                "trend": "trend_raw", "accel": "accel_raw", "particip": "particip_raw"}
    neutralized = []
    for k, src in comp_src.items():
        if df[src].notna().sum() == 0:
            neutralized.append(k)
            df[k] = np.nan
        else:
            df[k] = pctile(df[src])

    used = [k for k in WEIGHTS if k not in neutralized]
    tw = sum(WEIGHTS[k] for k in used)
    df["SMS"] = sum(df[k].fillna(50.0) * WEIGHTS[k] for k in used) / tw
    df = df.sort_values("SMS", ascending=False)
    df["rank"] = range(1, len(df) + 1)
    df["prev_rank"] = [prior_ranks.get(s) for s in df.index]
    df["d_rank"] = [(pr - r) if pr else None for pr, r in zip(df["prev_rank"], df["rank"])]

    # ---------------- hysteresis ----------------
    selected, actions, new_pending = [], [], {}
    regime_ok = a.regime in ("RISK-ON", "NEUTRAL")

    for sec, row in df.iterrows():
        sms, rk = row["SMS"], row["rank"]
        br = row["breadth_raw"]
        br_ok_ent = (pd.isna(br) or br >= 50)
        br_ok_ret = (pd.isna(br) or br >= 40)
        prev_sms = (prior or {}).get("sms", {}).get(sec)
        big_drop = prev_sms is not None and (prev_sms - sms) > 15

        if sec in prior_sel:
            if rk > 5:
                actions.append((sec, "EJECTED", "rank %d > 5" % rk))
            elif sms < 50:
                actions.append((sec, "EJECTED", "SMS %.1f < 50" % sms))
            elif not br_ok_ret:
                actions.append((sec, "EJECTED", "breadth %.1f < 40" % br))
            elif big_drop:
                actions.append((sec, "EJECTED", "SMS fell %.1f pts in one week" % (prev_sms - sms)))
            else:
                selected.append(sec)
                actions.append((sec, "RETAINED", "rank %d <= 5, SMS %.1f >= 50" % (rk, sms)))
        else:
            if rk <= 3 and sms >= 60 and br_ok_ent and regime_ok:
                cnt = pending.get(sec, 0) + 1
                if cnt >= 2:
                    selected.append(sec)
                    actions.append((sec, "NEW ENTRY", "rank %d for 2 consecutive weeks, SMS %.1f" % (rk, sms)))
                else:
                    new_pending[sec] = cnt
                    actions.append((sec, "ENTRY PENDING", "rank %d, week %d of 2" % (rk, cnt)))
            elif rk <= 3:
                why = []
                if sms < 60:
                    why.append("SMS %.1f < 60" % sms)
                if not br_ok_ent:
                    why.append("breadth %.1f < 50" % br)
                if not regime_ok:
                    why.append("regime %s blocks entry" % a.regime)
                actions.append((sec, "BLOCKED", "; ".join(why) or "gate failed"))

    # ---------------- correlation guard ----------------
    guard = []
    if len(selected) > 1:
        top6 = list(df.index[:6])
        rets = {}
        for s in set(selected) | set(top6):
            if s in series_store:
                rets[s] = series_store[s].pct_change().dropna().iloc[-60:]
        rdf = pd.DataFrame(rets).dropna()
        if len(rdf) >= 30:
            dropped = set()
            for i, s1 in enumerate(selected):
                for s2 in selected[i + 1:]:
                    if s1 in dropped or s2 in dropped or s1 not in rdf or s2 not in rdf:
                        continue
                    c = float(rdf[s1].corr(rdf[s2]))
                    if c > 0.80:
                        lo = s2 if df.loc[s1, "SMS"] >= df.loc[s2, "SMS"] else s1
                        dropped.add(lo)
                        guard.append("%s / %s corr %.2f > 0.80 — dropped %s" % (s1, s2, c, lo))
            if dropped:
                selected = [s for s in selected if s not in dropped]
                for sec, row in df.iterrows():
                    if len(selected) >= 3:
                        break
                    if sec in selected or sec in dropped:
                        continue
                    if row["rank"] <= 6 and row["SMS"] >= 60 and (pd.isna(row["breadth_raw"]) or row["breadth_raw"] >= 50) and regime_ok:
                        ok = all(sec not in rdf or s not in rdf or float(rdf[sec].corr(rdf[s])) <= 0.80 for s in selected)
                        if ok:
                            selected.append(sec)
                            guard.append("promoted %s (rank %d, SMS %.1f) to fill slot" % (sec, row["rank"], row["SMS"]))
    if not guard:
        guard.append("no action")

    selected = sorted(selected, key=lambda s: -df.loc[s, "SMS"])[:3]

    # ---------------- allocation ----------------
    alloc = {}
    alloc_note = None
    if selected:
        k = len(selected)
        tot = sum(df.loc[s, "SMS"] for s in selected)
        raww = {s: df.loc[s, "SMS"] / tot for s in selected}
        # The 0.25/0.45 band is defined for a 3-sector book. With fewer sectors a
        # 45% ceiling is arithmetically unreachable, so widen the band to the
        # nearest feasible one rather than letting the clamp silently do nothing.
        hi = max(0.45, 1.0 / k)
        lo = min(0.25, 1.0 / k)
        if k != 3:
            alloc_note = ("%d sector(s) selected — weight band widened to %.0f%%-%.0f%% "
                          "(the 25/45 band assumes 3 sectors)" % (k, lo * 100, hi * 100))
        # iterative clamp so the constraint survives renormalization
        w = dict(raww)
        for _ in range(20):
            w = {s: min(hi, max(lo, v)) for s, v in w.items()}
            tot_w = sum(w.values())
            if abs(tot_w - 1.0) < 1e-9:
                break
            w = {s: v / tot_w for s, v in w.items()}
        deployable = a.pool * DEPLOY[a.regime]
        for s in selected:
            alloc[s] = {"weight_pct": round(w[s] * 100, 1),
                        "budget_rupees": round(deployable * w[s]),
                        "raw_weight_pct": round(raww[s] * 100, 1)}

    # ---------------- output ----------------
    last_date = max(s.index[-1] for s in series_store.values())
    print("=" * 104)
    print("SECTOR MOMENTUM — as of %s | regime %s | %d sectors scored"
          % (str(pd.Timestamp(last_date).date()), a.regime, len(df)))
    if neutralized:
        print("NEUTRALIZED components: %s (weights renormalized to %.2f)" % (", ".join(neutralized), tw))
    if len(df) < 12:
        print("WARNING: only %d sectors scored. SMS is a cross-sectional percentile, so with a" % len(df))
        print("         small universe the scores spread across 0-100 regardless of absolute")
        print("         strength, and the SMS>=60 / >=50 gates lose their meaning. Score the full")
        print("         ~28-sector map before treating these gates as valid.")
    print("=" * 104)
    print("%-28s %6s %5s %6s %8s %8s %8s %7s %7s %8s"
          % ("SECTOR", "SMS", "RANK", "dRANK", "ABSMOM%", "RISKADJ", "BREADTH", "TREND", "ACCEL", "PARTICIP"))
    print("-" * 104)
    for sec, r in df.iterrows():
        mark = "*" if sec in selected else (" " if sec not in prior_sel else "x")
        def f(v, p=1):
            return "--" if pd.isna(v) else ("%%.%df" % p) % v
        print("%s%-27s %6.1f %5d %6s %8s %8s %8s %7s %7s %8s"
              % (mark, sec[:27], r["SMS"], r["rank"],
                 "--" if r["d_rank"] is None else "%+d" % r["d_rank"],
                 f(r["abs_mom_raw"]), f(r["risk_adj_raw"], 2), f(r["breadth_raw"]),
                 f(r["trend_raw"]), f(r["accel_raw"]), f(r["particip_raw"], 2)))
    print("-" * 104)
    print("  * = selected   x = was selected, now dropped")
    print("\nHYSTERESIS ACTIONS")
    for sec, act, why in actions:
        print("  %-28s %-15s %s" % (sec[:27], act, why))
    print("\nCORRELATION GUARD")
    for g in guard:
        print("  " + g)
    print("\nALLOCATION (pool Rs %s, %s deployment %.0f%%)"
          % (format(int(a.pool), ","), a.regime, DEPLOY[a.regime] * 100))
    if not selected:
        print("  NO SECTORS QUALIFY — capital stays in cash. This is intended behaviour")
        print("  when no sector clears the absolute entry gates.")
    for s in selected:
        print("  %-28s %5.1f%%   Rs %s" % (s[:27], alloc[s]["weight_pct"],
                                           format(alloc[s]["budget_rupees"], ",")))
    if alloc_note:
        print("  note: " + alloc_note)
    if selected and len(selected) < 3:
        print("  %d of 3 sector slots filled — remaining budget stays in cash." % len(selected))
    if notes:
        print("\nNOTES")
        for n in notes:
            print("  " + n)
    print("=" * 104)

    out = {
        "asof": str(pd.Timestamp(last_date).date()),
        "regime": a.regime,
        "neutralized_components": neutralized,
        "selected": selected,
        "allocation": alloc,
        "hysteresis_actions": [{"sector": s, "action": act, "reason": w} for s, act, w in actions],
        "correlation_guard": guard,
        "allocation_note": alloc_note,
        "ranking": [{"sector": s, "SMS": round(float(r["SMS"]), 2), "rank": int(r["rank"]),
                     "breadth_raw": None if pd.isna(r["breadth_raw"]) else round(float(r["breadth_raw"]), 1),
                     "abs_mom_raw": None if pd.isna(r["abs_mom_raw"]) else round(float(r["abs_mom_raw"]), 2)}
                    for s, r in df.iterrows()],
        "notes": notes,
    }
    if a.json:
        json.dump(out, open(a.json, "w"), indent=2)
        print("JSON written to %s" % a.json)

    new_state = {"week_ending": out["asof"],
                 "ranks": {s: int(df.loc[s, "rank"]) for s in df.index},
                 "sms": {s: round(float(df.loc[s, "SMS"]), 2) for s in df.index},
                 "selected": selected,
                 "entry_pending": new_pending}
    if a.out_state:
        json.dump(new_state, open(a.out_state, "w"), indent=2)
        print("State written to %s (feed as --state next week)" % a.out_state)


if __name__ == "__main__":
    main()
