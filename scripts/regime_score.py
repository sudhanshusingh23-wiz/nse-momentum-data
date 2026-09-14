#!/usr/bin/env python3
"""
Market Regime Monitor (L1) — composite regime score for Indian equity momentum.

Computes a 0-100 regime score from six components, applies hard overrides,
maps to RISK-ON / NEUTRAL / RISK-OFF / SHOCK, and emits the deployment mandate.

Missing optional inputs are neutralized (score 50) and weights renormalized;
the run is then flagged PARTIAL. A PARTIAL read may justify de-risking but
never an increase in deployment.

Usage:
  python regime_score.py --benchmark benchmark.csv --universe universe.csv \
      [--smallcap smallcap.csv] [--largecap largecap.csv] [--vix vix.csv] \
      [--flows flows.csv] [--state state.json] [--asof YYYY-MM-DD] \
      [--pool 8000000] [--json out.json]

Schemas are documented in references/data-sources.md.
"""

import argparse, json, sys
import numpy as np
import pandas as pd

WEIGHTS = {
    "index_trend": 0.25,
    "participation": 0.25,
    "thrust": 0.15,
    "smallcap_health": 0.15,
    "volatility": 0.10,
    "flow": 0.10,
}


def band(x, points):
    """Piecewise-linear score. points = [(x0,s0),(x1,s1),...] ascending in x.
    Flat extrapolation outside the range."""
    xs = [p[0] for p in points]
    ss = [p[1] for p in points]
    if x <= xs[0]:
        return float(ss[0])
    if x >= xs[-1]:
        return float(ss[-1])
    return float(np.interp(x, xs, ss))


def load_series(path, name):
    if path is None:
        return None
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    if "date" not in cols or "close" not in cols:
        raise ValueError("%s must have columns: date, close" % name)
    df = df.rename(columns={cols["date"]: "date", cols["close"]: "close"})
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "close"]].dropna().sort_values("date").reset_index(drop=True)


def clip_asof(df, asof):
    if df is None or asof is None:
        return df
    return df[df["date"] <= asof].reset_index(drop=True)


# ---------------------------------------------------------------- components

def score_index_trend(bm):
    """Position vs 200 DMA (60%) + 200 DMA slope over 20 sessions (40%)."""
    if bm is None or len(bm) < 220:
        return None, {}
    c = bm["close"]
    dma200 = c.rolling(200).mean()
    pct = (c.iloc[-1] / dma200.iloc[-1] - 1) * 100
    slope = (dma200.iloc[-1] / dma200.iloc[-21] - 1) * 100
    a = band(pct, [(-8, 0), (-3, 30), (0, 45), (0.01, 55), (3, 70), (10, 90), (15, 85)])
    b = band(slope, [(-1.0, 0), (0, 40), (0.01, 55), (0.5, 75), (0.51, 90)])
    return 0.6 * a + 0.4 * b, {
        "pct_vs_200dma": round(float(pct), 2),
        "dma200_slope_20s_pct": round(float(slope), 3),
        "sub_position": round(a, 1), "sub_slope": round(b, 1),
    }


def breadth_pcts(uni):
    """Return (pct_above_200, pct_above_50, pct50_10_sessions_ago, n_used, n_dropped)."""
    if uni is None:
        return None
    wide = uni.pivot_table(index="date", columns="symbol", values="close", aggfunc="last")
    wide = wide.sort_index()
    if len(wide) < 210:
        return None
    d200 = wide.rolling(200).mean()
    d50 = wide.rolling(50).mean()
    above200 = (wide > d200)
    above50 = (wide > d50)
    valid200 = d200.notna() & wide.notna()
    valid50 = d50.notna() & wide.notna()

    def pct_at(mask, valid, i):
        v = valid.iloc[i]
        n = int(v.sum())
        if n == 0:
            return None, 0
        return float((mask.iloc[i] & v).sum()) / n * 100, n

    p200, n200 = pct_at(above200, valid200, -1)
    p50, n50 = pct_at(above50, valid50, -1)
    p50_prev, _ = pct_at(above50, valid50, -11)
    if p200 is None or p50 is None:
        return None
    n_total = wide.shape[1]
    return {
        "pct_above_200dma": round(p200, 2),
        "pct_above_50dma": round(p50, 2),
        "pct_above_50dma_10s_ago": None if p50_prev is None else round(p50_prev, 2),
        "n_with_200dma": n200, "n_symbols_total": n_total,
        "n_dropped_insufficient_history": n_total - n200,
    }


def score_participation(b):
    if b is None:
        return None, {}
    p = b["pct_above_200dma"]
    s = band(p, [(25, 0), (40, 35), (55, 60), (70, 85), (85, 95)])
    return s, {"pct_above_200dma": p}


def score_thrust(b):
    if b is None:
        return None, {}
    p50 = b["pct_above_50dma"]
    prev = b["pct_above_50dma_10s_ago"]
    a = band(p50, [(20, 0), (35, 35), (50, 55), (65, 80), (75, 85)])
    if prev is None:
        return a, {"pct_above_50dma": p50, "delta_10s": None, "note": "delta unavailable"}
    d = p50 - prev
    bsc = band(d, [(-12, 0), (-4, 35), (-0.01, 45), (0.01, 60), (4, 65), (12, 85), (12.01, 92)])
    return 0.55 * a + 0.45 * bsc, {
        "pct_above_50dma": p50, "delta_10s_pp": round(d, 2),
        "sub_level": round(a, 1), "sub_delta": round(bsc, 1),
    }


def score_smallcap(sc, lc):
    if sc is None or lc is None:
        return None, {}
    m = pd.merge(sc, lc, on="date", suffixes=("_sc", "_lc"))
    if len(m) < 60:
        return None, {}
    ratio = m["close_sc"] / m["close_lc"]
    r50 = ratio.rolling(50).mean()
    if pd.isna(r50.iloc[-1]):
        return None, {}
    rvd = (ratio.iloc[-1] / r50.iloc[-1] - 1) * 100
    s = band(rvd, [(-4, 5), (-1, 35), (-0.01, 40), (0.01, 60), (1, 62), (4, 85), (4.01, 90)])
    return s, {"ratio_vs_50dma_pct": round(float(rvd), 2)}


def score_volatility(vix):
    if vix is None or len(vix) < 21:
        return None, {}
    lvl = float(vix["close"].iloc[-1])
    a = band(lvl, [(12, 85), (15, 70), (19, 45), (24, 20), (24.01, 5)])
    prev = float(vix["close"].iloc[-21])
    chg = (lvl / prev - 1) * 100 if prev else 0.0
    b = band(chg, [(-15, 85), (-0.01, 70), (0.01, 45), (20, 30), (20.01, 10)])
    return 0.7 * a + 0.3 * b, {"vix": round(lvl, 2), "vix_chg_20s_pct": round(chg, 2),
                               "sub_level": round(a, 1), "sub_trend": round(b, 1)}


def score_flow(fl):
    if fl is None or len(fl) < 21:
        return None, {}
    sub, det = [], {}
    if "fii_net_cr" in fl.columns:
        cum = float(fl["fii_net_cr"].tail(20).sum())
        a = band(cum, [(-20000, 5), (0, 45), (1, 55), (20000, 85), (20001, 90)])
        sub.append((a, 0.65)); det["fii_20s_cum_cr"] = round(cum, 1)
    if "usdinr" in fl.columns:
        chg = (float(fl["usdinr"].iloc[-1]) / float(fl["usdinr"].iloc[-21]) - 1) * 100
        b = 20.0 if chg > 1.5 else (80.0 if chg < -0.5 else 60.0)
        sub.append((b, 0.35)); det["usdinr_chg_20s_pct"] = round(chg, 2)
    if not sub:
        return None, {}
    tw = sum(w for _, w in sub)
    return sum(s * w for s, w in sub) / tw, det


# ---------------------------------------------------------------- overrides

def apply_state(score, bm, vix, breadth, partial):
    ov = []
    state = None

    # SHOCK
    if bm is not None and len(bm) >= 6:
        r5 = (bm["close"].iloc[-1] / bm["close"].iloc[-6] - 1) * 100
    else:
        r5 = None
    vlvl = float(vix["close"].iloc[-1]) if vix is not None and len(vix) else None
    if r5 is not None and r5 <= -7:
        ov.append("SHOCK: Nifty 500 5-session return %.2f%% <= -7%%" % r5); state = "SHOCK"
    if vlvl is not None and vlvl > 28:
        ov.append("SHOCK: India VIX %.2f > 28" % vlvl); state = "SHOCK"

    # RISK-OFF floor
    below200_5 = False
    if bm is not None and len(bm) >= 205:
        d200 = bm["close"].rolling(200).mean()
        below200_5 = bool((bm["close"].iloc[-5:] < d200.iloc[-5:]).all())
    p200 = breadth["pct_above_200dma"] if breadth else None
    floor_off = False
    if p200 is not None and p200 < 40:
        ov.append("RISK-OFF floor: %% above 200DMA %.1f < 40" % p200); floor_off = True
    if below200_5:
        ov.append("RISK-OFF floor: Nifty 500 below 200 DMA on 5 consecutive closes"); floor_off = True

    if state != "SHOCK":
        base = "RISK-ON" if score >= 65 else ("NEUTRAL" if score >= 45 else "RISK-OFF")
        # RISK-ON gate
        if base == "RISK-ON":
            gate_ok = True
            if p200 is None or p200 <= 55:
                gate_ok = False
                ov.append("RISK-ON gate failed: %% above 200DMA %s (needs > 55)"
                          % ("unavailable" if p200 is None else "%.1f" % p200))
            if bm is not None and len(bm) >= 200:
                d200 = bm["close"].rolling(200).mean()
                if bm["close"].iloc[-1] <= d200.iloc[-1]:
                    gate_ok = False
                    ov.append("RISK-ON gate failed: Nifty 500 not above its 200 DMA")
            if not gate_ok:
                base = "NEUTRAL"
        if floor_off:
            base = "RISK-OFF"
        state = base

    if partial and state in ("RISK-ON",):
        ov.append("PARTIAL data: RISK-ON downgraded to NEUTRAL — incomplete reads "
                  "may not increase deployment")
        state = "NEUTRAL"
    return state, ov, r5


MANDATE = {
    "RISK-ON":  {"max_deployment_pct": 85, "positions": "6-9", "cap_tilt": "small/mid favoured; smallcap up to 60% of pool", "new_entries": "full tranche ladder", "heat_cap_pct": 6.0},
    "NEUTRAL":  {"max_deployment_pct": 60, "positions": "5-7", "cap_tilt": "mid favoured; max 2 smallcap positions", "new_entries": "tranches 1 and 2 only", "heat_cap_pct": 3.5},
    "RISK-OFF": {"max_deployment_pct": 30, "positions": "<=4", "cap_tilt": "large/mid only; no new smallcap", "new_entries": "none — manage existing only", "heat_cap_pct": 2.0},
    "SHOCK":    {"max_deployment_pct": 20, "positions": "<=2 (CORE only)", "cap_tilt": "any", "new_entries": "none — exit non-core within 3 sessions", "heat_cap_pct": 1.0},
}

ORDER = ["RISK-OFF", "NEUTRAL", "RISK-ON"]


def transition(raw_state, prior):
    """Fast to de-risk, slow to re-risk."""
    if prior is None:
        return raw_state, {"note": "no prior state supplied — unconfirmed classification",
                           "sessions_in_state": 1, "pending_upgrade_count": 0}
    ps = prior.get("state")
    sis = int(prior.get("sessions_in_state", 1))
    pend = int(prior.get("pending_upgrade_count", 0))

    if ps == "SHOCK" and sis < 10 and raw_state != "SHOCK":
        return "SHOCK", {"note": "SHOCK locked for 10 sessions (%d elapsed)" % sis,
                         "sessions_in_state": sis + 1, "pending_upgrade_count": 0}
    if raw_state == "SHOCK":
        return "SHOCK", {"note": "SHOCK override", "sessions_in_state": 1 if ps != "SHOCK" else sis + 1,
                         "pending_upgrade_count": 0}
    if ps == "SHOCK":
        ps_idx = -1
    else:
        ps_idx = ORDER.index(ps) if ps in ORDER else 1
    rs_idx = ORDER.index(raw_state)

    if rs_idx < ps_idx:  # downgrade
        return raw_state, {"note": "downgrade applied immediately", "sessions_in_state": 1,
                           "pending_upgrade_count": 0}
    if rs_idx > ps_idx:  # upgrade candidate
        pend += 1
        if pend >= 3 and sis >= 5:
            return raw_state, {"note": "upgrade confirmed (3 closes, %d sessions in prior state)" % sis,
                               "sessions_in_state": 1, "pending_upgrade_count": 0}
        need_c = max(0, 3 - pend); need_s = max(0, 5 - sis)
        return ps, {"note": "upgrade to %s pending — needs %d more close(s), %d more session(s)"
                    % (raw_state, need_c, need_s),
                    "sessions_in_state": sis + 1, "pending_upgrade_count": pend}
    return raw_state, {"note": "state unchanged", "sessions_in_state": sis + 1,
                       "pending_upgrade_count": 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--benchmark", required=True,
                    help="Nifty 500 close series. NOT Nifty 50 / Sensex — see benchmark integrity.")
    ap.add_argument("--benchmark-name", default="NIFTY500",
                    help="Identifier for the benchmark series. Must be NIFTY500 unless "
                         "--allow-substitute-benchmark is set.")
    ap.add_argument("--allow-substitute-benchmark", action="store_true",
                    help="Force-run on a non-Nifty-500 benchmark. Output is marked DEGRADED "
                         "and the state can never be upgraded.")
    ap.add_argument("--universe", help="Long-format constituent panel for breadth. "
                                       "Omit if supplying --breadth-json.")
    ap.add_argument("--breadth-json", help="Pre-computed breadth from build_breadth.py.")
    ap.add_argument("--largecap-index", help="Optional Nifty 50 series, for the cap-tier "
                                             "divergence diagnostic only. Never used as the benchmark.")
    ap.add_argument("--smallcap"); ap.add_argument("--largecap")
    ap.add_argument("--vix"); ap.add_argument("--flows"); ap.add_argument("--state")
    ap.add_argument("--asof"); ap.add_argument("--pool", type=float)
    ap.add_argument("--json")
    a = ap.parse_args()

    asof = pd.to_datetime(a.asof) if a.asof else None
    bm = clip_asof(load_series(a.benchmark, "benchmark"), asof)
    sc = clip_asof(load_series(a.smallcap, "smallcap"), asof)
    lc = clip_asof(load_series(a.largecap, "largecap"), asof)
    vix = clip_asof(load_series(a.vix, "vix"), asof)

    # ---- benchmark integrity gate (see references/data-sources.md) ----
    bench_name = a.benchmark_name.upper().replace(" ", "").replace("_", "")
    degraded = False
    integrity_note = None
    if bench_name not in ("NIFTY500", "CNX500", "NIFTY500TRI"):
        if not a.allow_substitute_benchmark:
            print("ERROR: benchmark is '%s', not the Nifty 500.\n"
                  "  The regime layer gates a mid/small-cap book. A large-cap index is not a\n"
                  "  valid substitute: the two can sit on opposite sides of their own 200 DMA\n"
                  "  at the same time, which flips the state. Supply the Nifty 500 series, or\n"
                  "  pass --allow-substitute-benchmark to run DEGRADED."
                  % a.benchmark_name, file=sys.stderr)
            sys.exit(3)
        degraded = True
        integrity_note = ("DEGRADED: benchmark is '%s', not Nifty 500. Index-trend component "
                          "describes a different universe than the one being traded. State may "
                          "not be upgraded on this run." % a.benchmark_name)

    uni = None
    if a.universe:
        uni = pd.read_csv(a.universe)
        uni.columns = [c.lower() for c in uni.columns]
        uni["date"] = pd.to_datetime(uni["date"])
        if asof is not None:
            uni = uni[uni["date"] <= asof]
    elif not a.breadth_json:
        print("ERROR: supply --universe (constituent panel) or --breadth-json "
              "(from build_breadth.py). Breadth carries 40% of the score and is never "
              "estimated.", file=sys.stderr)
        sys.exit(4)

    fl = None
    if a.flows:
        fl = pd.read_csv(a.flows)
        fl.columns = [c.lower() for c in fl.columns]
        fl["date"] = pd.to_datetime(fl["date"])
        if asof is not None:
            fl = fl[fl["date"] <= asof]
        fl = fl.sort_values("date").reset_index(drop=True)

    if a.breadth_json:
        bj = json.load(open(a.breadth_json))
        univ = str(bj.get("universe", "")).upper().replace(" ", "")
        if univ and univ not in ("NIFTY500", "CNX500"):
            print("ERROR: breadth JSON universe is '%s', not NIFTY500. Breadth from a "
                  "different universe cannot be scored against these bands."
                  % bj.get("universe"), file=sys.stderr)
            sys.exit(5)
        b = {"pct_above_200dma": bj["pct_above_200dma"],
             "pct_above_50dma": bj["pct_above_50dma"],
             "pct_above_50dma_10s_ago": bj.get("pct_above_50dma_10s_ago"),
             "n_with_200dma": bj.get("denominator_200dma"),
             "n_symbols_total": bj.get("constituents_in_list"),
             "n_dropped_insufficient_history": bj.get("excluded_insufficient_history"),
             "source": bj.get("method", "breadth_json"),
             "breadth_warnings": bj.get("warnings", [])}
    else:
        b = breadth_pcts(uni)
    raw = {}
    raw["index_trend"] = score_index_trend(bm)
    raw["participation"] = score_participation(b)
    raw["thrust"] = score_thrust(b)
    raw["smallcap_health"] = score_smallcap(sc, lc)
    raw["volatility"] = score_volatility(vix)
    raw["flow"] = score_flow(fl)

    available = {k: v for k, v in raw.items() if v[0] is not None}
    missing = [k for k in raw if raw[k][0] is None]
    if not available:
        print("ERROR: no components could be computed. Check input schemas.", file=sys.stderr)
        sys.exit(2)

    tw = sum(WEIGHTS[k] for k in available)
    score = sum(available[k][0] * WEIGHTS[k] for k in available) / tw
    partial = len(missing) > 0

    # ---- cap-tier divergence diagnostic (never a substitute, only a flag) ----
    divergence = None
    if a.largecap_index:
        lc_series = clip_asof(load_series(a.largecap_index, "largecap-index"), asof)
        if lc_series is not None and len(lc_series) >= 200 and bm is not None and len(bm) >= 200:
            bm_pos = (bm["close"].iloc[-1] / bm["close"].rolling(200).mean().iloc[-1] - 1) * 100
            lc_pos = (lc_series["close"].iloc[-1] /
                      lc_series["close"].rolling(200).mean().iloc[-1] - 1) * 100
            gap = bm_pos - lc_pos
            divergence = {"broad_pct_vs_200dma": round(float(bm_pos), 2),
                          "largecap_pct_vs_200dma": round(float(lc_pos), 2),
                          "gap_pp": round(float(gap), 2)}
            if abs(gap) >= 3.0:
                divergence["flag"] = (
                    "CAP-TIER DIVERGENCE %+.1f pp. Broad market and large caps are on "
                    "materially different footings. Favourable for a small/mid book when "
                    "positive, a warning when negative. This is why the Nifty 50 may never "
                    "substitute for the Nifty 500 here." % gap)

    prior = json.load(open(a.state)) if a.state else None
    raw_state, overrides, r5 = apply_state(score, bm, vix, b, partial)
    if degraded:
        overrides.append(integrity_note)
        if raw_state == "RISK-ON":
            raw_state = "NEUTRAL"
    state, trans = transition(raw_state, prior)
    if degraded and prior:
        ps = prior.get("state")
        order = {"SHOCK": -1, "RISK-OFF": 0, "NEUTRAL": 1, "RISK-ON": 2}
        if ps in order and order.get(state, 1) > order[ps]:
            trans = {"note": "DEGRADED run may not upgrade state; held at %s" % ps,
                     "sessions_in_state": int(prior.get("sessions_in_state", 1)) + 1,
                     "pending_upgrade_count": 0}
            state = ps

    asof_date = str(bm["date"].iloc[-1].date()) if bm is not None else str(asof)
    out = {
        "asof": asof_date,
        "composite_score": round(score, 2),
        "state_raw": raw_state,
        "state_after_transition": state,
        "partial": partial,
        "degraded_benchmark": degraded,
        "benchmark_name": a.benchmark_name,
        "cap_tier_divergence": divergence,
        "components_missing": missing,
        "weights_renormalized_total": round(tw, 3),
        "components": {k: {"score": round(v[0], 2), "weight": WEIGHTS[k], "detail": v[1]}
                       for k, v in available.items()},
        "breadth": b,
        "overrides_fired": overrides,
        "transition": trans,
        "mandate": dict(MANDATE[state]),
        "nifty500_5session_return_pct": None if r5 is None else round(float(r5), 2),
    }
    if a.pool:
        out["mandate"]["max_deployment_rupees"] = round(a.pool * MANDATE[state]["max_deployment_pct"] / 100)
        out["mandate"]["heat_cap_rupees"] = round(a.pool * MANDATE[state]["heat_cap_pct"] / 100)

    print("=" * 68)
    print("MARKET REGIME — %s" % asof_date)
    print("=" * 68)
    print("State: %-9s   Score: %5.1f/100   %s"
          % (state, score, "PARTIAL" if partial else "complete"))
    if state != raw_state:
        print("  (raw classification %s held back by transition rules)" % raw_state)
    print("-" * 68)
    print("%-18s %7s %7s   %s" % ("COMPONENT", "WEIGHT", "SCORE", "KEY INPUT"))
    for k in WEIGHTS:
        if k in available:
            d = available[k][1]
            key = next(iter(d.items())) if d else ("", "")
            print("%-18s %6.0f%% %7.1f   %s=%s" % (k, WEIGHTS[k] * 100, available[k][0], key[0], key[1]))
        else:
            print("%-18s %6.0f%% %7s   neutralized" % (k, WEIGHTS[k] * 100, "--"))
    print("-" * 68)
    if overrides:
        print("OVERRIDES:")
        for o in overrides:
            print("  - " + o)
    else:
        print("OVERRIDES: none")
    if divergence:
        print("DIVERGENCE: broad %+.2f%% vs 200DMA | large-cap %+.2f%% | gap %+.2f pp"
              % (divergence["broad_pct_vs_200dma"], divergence["largecap_pct_vs_200dma"],
                 divergence["gap_pp"]))
        if "flag" in divergence:
            print("  " + divergence["flag"])
    if b and b.get("breadth_warnings"):
        for w in b["breadth_warnings"]:
            print("BREADTH WARNING: " + w)
    print("TRANSITION: " + trans["note"])
    print("-" * 68)
    m = out["mandate"]
    print("MANDATE")
    print("  Max deployment : %d%%%s" % (m["max_deployment_pct"],
          ("  (Rs %s)" % format(m["max_deployment_rupees"], ",")) if a.pool else ""))
    print("  Positions      : %s" % m["positions"])
    print("  Cap tilt       : %s" % m["cap_tilt"])
    print("  New entries    : %s" % m["new_entries"])
    print("  Heat cap       : %.1f%%" % m["heat_cap_pct"])
    if partial:
        print("\nNOTE: PARTIAL read (%s missing). May justify de-risking, never an "
              "increase in deployment." % ", ".join(missing))
    print("=" * 68)

    if a.json:
        json.dump(out, open(a.json, "w"), indent=2)
        print("JSON written to %s" % a.json)


if __name__ == "__main__":
    main()
