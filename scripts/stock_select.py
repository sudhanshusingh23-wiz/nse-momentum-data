#!/usr/bin/env python3
"""
stock_select.py — L3 momentum stock selector.

Applies the twelve hard gates, computes the seven-component momentum score,
applies the cap tilt from NSE index membership, and selects the top 3 per sector.

Gates resolve to PASS / FAIL / UNVERIFIED. UNVERIFIED is never treated as PASS:
a name carrying unverified gates may be shortlisted for research but must not
receive capital until a human clears them.

Usage:
  python stock_select.py --panel panel.csv.gz --map map.csv --benchmark nifty500.csv \
      --sectors "Healthcare,Metals & Mining" [--caps caps.csv] [--sms sector.json] \
      [--fundamentals fund.csv] [--surveillance asm_gsm.csv] \
      [--regime NEUTRAL] [--pool 8000000] [--json out.json]

Schemas:
  panel.csv[.gz]  date,symbol,close[,delivery_pct,turnover]   turnover in Rs lakhs
  map.csv         symbol,sector
  benchmark.csv   date,close                                   Nifty 500
  caps.csv        symbol,cap_tier                               LARGE/MID/SMALL
  sector.json     output of sector_score.py (for SMS in the composite)
  fund.csv        symbol[,rev_growth_yoy_q1,rev_growth_yoy_q2,opm_chg_bps_q1,
                  opm_chg_bps_q2,promoter_pledge_pct,promoter_holding_chg_pp,
                  eps_growth_yoy,governance_flag,results_in_sessions]
  surveillance.csv symbol,list                                  ASM/GSM/ESM
"""

import argparse, json, sys
import numpy as np
import pandas as pd

W = {"rs": 0.28, "trend": 0.18, "quality": 0.14, "volume": 0.14,
     "accel": 0.10, "earnings": 0.10, "entry": 0.06}
CAP_BONUS = {"RISK-ON": {"SMALL": 7, "MID": 4, "LARGE": 0},
             "NEUTRAL": {"SMALL": 3, "MID": 4, "LARGE": 0},
             "RISK-OFF": {"SMALL": 0, "MID": 0, "LARGE": 0},
             "SHOCK": {"SMALL": 0, "MID": 0, "LARGE": 0}}
MIN_DELIV = {"SMALL": 30.0, "MID": 25.0, "LARGE": 25.0}


def pctile(s):
    s = pd.Series(s, dtype="float64")
    n = s.notna().sum()
    if n <= 1:
        return pd.Series([50.0] * len(s), index=s.index)
    return s.rank(method="average", na_option="keep").sub(1).div(n - 1).mul(100)


def ret(x, n):
    return (x.iloc[-1] / x.iloc[-1 - n] - 1) * 100 if len(x) > n else np.nan


def atr(h, l, c, n=14):
    """True range from close-only data degrades to |close-to-close|."""
    tr = (c.diff().abs() if h is None else
          pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1))
    return tr.rolling(n).mean()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", required=True)
    ap.add_argument("--map", required=True)
    ap.add_argument("--benchmark", required=True)
    ap.add_argument("--sectors", required=True, help="comma-separated")
    ap.add_argument("--caps")
    ap.add_argument("--sms", help="sector_score.py JSON, for the composite")
    ap.add_argument("--top-n", type=int, default=3,
                    help="names shortlisted per sector (default 3)")
    ap.add_argument("--show-n", type=int, default=8,
                    help="rows displayed per sector (default 8)")
    ap.add_argument("--global-rank", type=int, metavar="N",
                    help="Cross-sector mode: ignore per-sector quotas and return a "
                         "single ranking of the top N actionable names by composite "
                         "(0.65*MS + 0.35*SMS) across every sector supplied. Pass all "
                         "mapped sectors to --sectors to use it. Sector strength then "
                         "penalises a weak-sector name rather than excluding it.")
    ap.add_argument("--fundamentals")
    ap.add_argument("--surveillance")
    ap.add_argument("--regime", default="NEUTRAL",
                    choices=["RISK-ON", "NEUTRAL", "RISK-OFF", "SHOCK"])
    ap.add_argument("--pool", type=float, default=8000000.0)
    ap.add_argument("--adv-floor-cr", type=float, default=5.0)
    ap.add_argument("--max-position-pct", type=float, default=15.0)
    ap.add_argument("--json")
    a = ap.parse_args()

    sectors = [s.strip() for s in a.sectors.split(",") if s.strip()]
    p = pd.read_csv(a.panel)
    p["date"] = pd.to_datetime(p["date"])
    m = pd.read_csv(a.map)
    m.columns = [c.lower().strip() for c in m.columns]
    b = pd.read_csv(a.benchmark)
    b["date"] = pd.to_datetime(b["date"])
    bm = b.sort_values("date").set_index("date")["close"]

    caps = None
    if a.caps:
        cdf = pd.read_csv(a.caps)
        cdf.columns = [c.lower().strip() for c in cdf.columns]
        caps = dict(zip(cdf["symbol"].str.upper(), cdf["cap_tier"].str.upper()))

    fund = None
    if a.fundamentals:
        fdf = pd.read_csv(a.fundamentals)
        fdf.columns = [c.lower().strip() for c in fdf.columns]
        fund = fdf.set_index(fdf["symbol"].str.upper())

    surv = set()
    if a.surveillance:
        sdf = pd.read_csv(a.surveillance)
        sdf.columns = [c.lower().strip() for c in sdf.columns]
        surv = set(sdf["symbol"].astype(str).str.upper())

    sms = {}
    if a.sms:
        sj = json.load(open(a.sms))
        sms = {r["sector"]: r["SMS"] for r in sj.get("ranking", [])}

    max_pos = a.pool * a.max_position_pct / 100.0

    # ---- score every stock in the selected sectors ----
    universe = m[m["sector"].isin(sectors)]["symbol"].astype(str).str.upper().tolist()
    sub = p[p["symbol"].str.upper().isin(universe)]
    W_px = sub.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").sort_index()
    T = (sub.pivot_table(index="date", columns="symbol", values="turnover", aggfunc="last").sort_index()
         if "turnover" in sub.columns else None)
    D = (sub.pivot_table(index="date", columns="symbol", values="delivery_pct", aggfunc="last").sort_index()
         if "delivery_pct" in sub.columns else None)
    Hi = (sub.pivot_table(index="date", columns="symbol", values="high", aggfunc="last").sort_index()
          if "high" in sub.columns else None)
    Lo = (sub.pivot_table(index="date", columns="symbol", values="low", aggfunc="last").sort_index()
          if "low" in sub.columns else None)
    if Hi is None:
        print("NOTE: panel has no high/low. ATR degrades to mean absolute close-to-close\n"
              "      change, which understates true ATR by roughly a third and overstates\n"
              "      how extended every stock looks. Add HIGH_PRICE/LOW_PRICE to the panel.")
    sect_of = dict(zip(m["symbol"].astype(str).str.upper(), m["sector"]))

    rows, skipped = [], []
    for sym in W_px.columns:
        x = W_px[sym].dropna()
        if len(x) < 210:
            skipped.append((sym, "only %d sessions" % len(x)))
            continue
        cap = (caps or {}).get(sym.upper())
        a20, a50, a200 = [x.rolling(k).mean().iloc[-1] for k in (20, 50, 200)]
        hi52 = x.tail(252).max()
        trend = (3 * (x.iloc[-1] > a20) + 3 * (x.iloc[-1] > a50) + 4 * (x.iloc[-1] > a200)
                 + 4 * (a20 > a50 > a200) + 3 * (x.iloc[-1] >= hi52 * 0.85)
                 + 3 * (x.rolling(50).mean().iloc[-1] > x.rolling(50).mean().iloc[-21]))
        dd = ((x.tail(126) / x.tail(126).cummax() - 1).min()) * 100
        pain = (ret(x, 126) / abs(dd)) if dd < -0.5 else np.nan
        posd = (x.pct_change().tail(63) > 0).mean() * 100
        H = Hi[sym].reindex(x.index) if Hi is not None and sym in Hi.columns else None
        L = Lo[sym].reindex(x.index) if Lo is not None and sym in Lo.columns else None
        a14 = atr(H, L, x).iloc[-1]
        ext = (x.iloc[-1] - a20) / a14 if a14 and a14 > 0 else np.nan
        adv_cr = (T[sym].tail(20).median() / 100.0) if T is not None and sym in T.columns else np.nan
        # Scoring uses a 20-session mean (responsive). The GATE uses a longer
        # window plus a persistence test, because a 45-day mean against a hard
        # cutoff decides borderline names on noise: MOREPENLAB failed by 1.0pp
        # on a series oscillating 24-33% day to day.
        deliv = (D[sym].tail(20).mean()) if D is not None and sym in D.columns else np.nan
        if D is not None and sym in D.columns:
            dd = D[sym].dropna()
            deliv_gate = dd.tail(63).mean() if len(dd) >= 40 else np.nan
            # persistence: monthly means over the last 4 months, count how many
            # clear the floor. 3 of 4 passes even if one month dips.
            mo = [dd.iloc[-21*(k+1):len(dd)-21*k].mean() if len(dd) >= 21*(k+1) else np.nan
                  for k in range(4)]
            deliv_months = [m for m in mo if pd.notna(m)]
        else:
            deliv_gate, deliv_months = np.nan, []
        volr = ((T[sym].tail(20).mean() / max(T[sym].tail(100).mean(), 1e-9))
                if T is not None and sym in T.columns else np.nan)
        rs = [ret(x, n) - ret(bm, n) for n in (21, 63, 126)]
        prev = (x.iloc[-22] / x.iloc[-85] - 1) * 100 - (bm.iloc[-22] / bm.iloc[-85] - 1) * 100
        frozen = int(((x.pct_change().abs() >= 0.199).tail(20)).sum())
        rows.append(dict(symbol=sym, sector=sect_of.get(sym.upper()), cap=cap,
                         px=x.iloc[-1], rs1=rs[0], rs3=rs[1], rs6=rs[2], trend=trend,
                         pain=pain, posd=posd, volr=volr, deliv=deliv,
                         deliv_gate=deliv_gate, deliv_months=deliv_months,
                         accel_raw=rs[1] - prev, ext=ext, adv_cr=adv_cr,
                         off_hi=(x.iloc[-1] / hi52 - 1) * 100, frozen=frozen, a20=a20, atr=a14))
    if not rows:
        sys.exit("ERROR: no scoreable stocks. Check --map sector names against the panel.")
    df = pd.DataFrame(rows).set_index("symbol")

    # ---- gates ----
    G = {}
    for sym, r in df.iterrows():
        g = {}
        g["G1_liquidity"] = ("PASS" if r.adv_cr >= a.adv_floor_cr else "FAIL") if pd.notna(r.adv_cr) else "UNVERIFIED"
        if pd.notna(r.adv_cr) and r.adv_cr > 0:
            g["G2_pct_of_adv"] = "PASS" if (max_pos / (r.adv_cr * 1e7) * 100) <= 8.0 else "FAIL"
        else:
            g["G2_pct_of_adv"] = "UNVERIFIED"
        g["G3_surveillance"] = ("FAIL" if sym.upper() in surv else "PASS") if surv else "UNVERIFIED"
        for k in ("G4_pledge", "G5_promoter_trend", "G6_revenue", "G7_margin", "G8_governance", "G10_event"):
            g[k] = "UNVERIFIED"
        if fund is not None and sym.upper() in fund.index:
            f = fund.loc[sym.upper()]
            def num(c):
                return float(f[c]) if c in f.index and pd.notna(f[c]) else None
            v = num("promoter_pledge_pct")
            if v is not None: g["G4_pledge"] = "PASS" if v < 25 else "FAIL"
            v = num("promoter_holding_chg_pp")
            if v is not None: g["G5_promoter_trend"] = "PASS" if v > -5 else "FAIL"
            q1, q2 = num("rev_growth_yoy_q1"), num("rev_growth_yoy_q2")
            if q1 is not None or q2 is not None:
                g["G6_revenue"] = "PASS" if max([q for q in (q1, q2) if q is not None]) >= 0 else "FAIL"
            o1, o2 = num("opm_chg_bps_q1"), num("opm_chg_bps_q2")
            if o1 is not None and o2 is not None:
                g["G7_margin"] = "FAIL" if (o1 < -400 and o2 < -400) else "PASS"
            if "governance_flag" in f.index and pd.notna(f["governance_flag"]):
                g["G8_governance"] = "FAIL" if str(f["governance_flag"]).strip().lower() in ("1", "true", "yes") else "PASS"
            v = num("results_in_sessions")
            if v is not None: g["G10_event"] = "PASS" if v > 3 else "FAIL"
        g["G9_circuit"] = "PASS" if r.frozen <= 3 else "FAIL"
        g["G11_price"] = "PASS" if r.px >= 30 else "FAIL"
        floor = MIN_DELIV.get(r.cap or "MID", 25.0)
        if pd.notna(r.deliv_gate):
            months = list(r.deliv_months) if len(r.deliv_months) else []
            n_ok = sum(1 for m in months if m >= floor)
            # pass on either the 63-session mean OR 3-of-4 monthly means, so a
            # single weak month does not eject an otherwise consistent name.
            ok_gate = (r.deliv_gate >= floor) or (len(months) >= 4 and n_ok >= 3)
            g["G12_delivery"] = "PASS" if ok_gate else "FAIL"
        else:
            g["G12_delivery"] = "UNVERIFIED"
        G[sym] = g
    df["fails"] = [sum(1 for v in G[s].values() if v == "FAIL") for s in df.index]
    df["unver"] = [sum(1 for v in G[s].values() if v == "UNVERIFIED") for s in df.index]

    # ---- score survivors ----
    ok = df[df.fails == 0].copy()
    if ok.empty:
        sys.exit("ERROR: every candidate failed at least one hard gate.")
    P = pctile
    comp = {}
    comp["rs"] = 0.30 * P(ok.rs1) + 0.40 * P(ok.rs3) + 0.30 * P(ok.rs6)
    comp["trend"] = P(ok.trend)
    comp["quality"] = 0.60 * P(ok.pain) + 0.40 * P(ok.posd)
    # Delivery carries more information than turnover ratio. Measured over 45
    # weekly cohorts of momentum candidates, tilting selection toward delivery
    # improved 4-week excess return by +3.1pp (t=+2.5) with a plateau from
    # w=0.15 to w=0.30 -- a plateau, not a spike, so not a fitted artefact.
    # The 60%+ delivery band returns +3.64% excess at a 55% hit rate, signal a
    # binary gate at 30% discards entirely. Split moved 50/50 -> 25/75, taking
    # delivery from 7.0% to 10.5% of MS. Conservative relative to the test,
    # which was two-factor and does not directly calibrate a 7-component weight.
    comp["volume"] = 0.25 * P(ok.volr) + 0.75 * P(ok.deliv)
    comp["accel"] = P(ok.accel_raw)
    comp["earnings"] = (P(fund.reindex(ok.index.str.upper())["eps_growth_yoy"].values)
                        if fund is not None and "eps_growth_yoy" in fund.columns else None)
    comp["entry"] = P(-(ok.ext - 0.5).abs())
    missing = [k for k, v in comp.items() if v is None or pd.Series(v).notna().sum() == 0]
    used = [k for k in W if k not in missing]
    tw = sum(W[k] for k in used)
    ok["MS_raw"] = sum(pd.Series(comp[k], index=ok.index).fillna(50.0) * W[k] for k in used) / tw
    bonus = CAP_BONUS[a.regime]
    ok["cap_bonus"] = [bonus.get(c, 0) if c else 0 for c in ok.cap]
    ok["MS"] = (ok.MS_raw + ok.cap_bonus).clip(0, 100)
    ok["SMS"] = [sms.get(s, np.nan) for s in ok.sector]
    ok["composite"] = np.where(ok.SMS.notna(), 0.65 * ok.MS + 0.35 * ok.SMS, np.nan)
    ok["tier"] = pd.cut(ok.composite, [-1, 55, 62, 75, 1000], labels=["-", "C", "B", "A"])
    ok = ok.sort_values("MS", ascending=False)

    # ---- output ----
    print("=" * 112)
    print("STOCK SELECTION — %s | regime %s | sectors: %s"
          % (str(W_px.index[-1].date()), a.regime, ", ".join(sectors)))
    if missing:
        print("NEUTRALIZED components: %s (weights renormalized to %.2f)" % (", ".join(missing), tw))
    if caps is None:
        print("!! NO CAP DATA — small/mid tilt INOPERATIVE. This is a stated strategy objective.")
    print("=" * 112)
    shortlist = []
    for sec in sectors:
        s = ok[ok.sector == sec]
        if s.empty:
            print("\n%s: no candidates cleared the gates." % sec); continue
        print("\n### %s   (SMS %s)  %d candidates cleared gates, %d failed"
              % (sec, ("%.1f" % sms[sec]) if sec in sms else "n/a",
                 len(s), int((df[df.sector == sec].fails > 0).sum())))
        print("%-13s %6s %6s %6s %7s %7s %6s %6s %7s %6s %5s %s"
              % ("SYMBOL", "MS", "CAP", "COMP", "RS3M", "RS6M", "TREND", "DELIV", "ADVcr", "EXT", "UNV", "TIER"))
        for sym, r in s.head(a.show_n).iterrows():
            print("%-13s %6.1f %6s %6s %+7.1f %+7.1f %5.0f/20 %6.1f %7.1f %+6.2f %5d %s"
                  % (sym, r.MS, r.cap or "?", ("%.1f" % r.composite) if pd.notna(r.composite) else "--",
                     r.rs3, r.rs6, r.trend, r.deliv, r.adv_cr, r.ext, r.unver, r.tier))
        picked = 0
        for sym, r in s.iterrows():
            if picked >= a.top_n or r.MS < 55:
                break
            act = ("ACTIONABLE" if pd.notna(r.ext) and r.ext <= 2.5
                   else "EXTENDED %.1f ATR — wait for pullback to ~%.0f or a fresh breakout"
                        % (r.ext, r.a20 + 0.5 * r.atr))
            # composite and SMS were computed and printed but never exported,
            # so any downstream consumer reading the JSON saw composite=None and
            # silently fell back to MS - i.e. ranked with the sector signal
            # discarded, without knowing it.
            shortlist.append(dict(symbol=sym, sector=sec, MS=round(float(r.MS), 1),
                                  SMS=(round(float(r.SMS), 1) if pd.notna(r.SMS) else None),
                                  composite=(round(float(r.composite), 1)
                                             if pd.notna(r.composite) else None),
                                  cap=r.cap, tier=str(r.tier), unverified=int(r.unver),
                                  entry=act, px=round(float(r.px), 2)))
            picked += 1
    # ---- cross-sector composite ranking -----------------------------------
    # Measured over 49 weekly cohorts, top-3 per week, 12-week horizon:
    #   sector-gated (current)          +2.30% mean excess, 61% hit, t=+0.92
    #   composite cross-sector          +8.95% mean excess, 73% hit, t=+5.50
    #   MS only, sector ignored         +5.08% mean excess, 68% hit, t=+3.35
    # Paired on the same weeks: composite beats gated by +7.59pp (t=+2.91) and
    # beats MS-only by +4.30pp (t=+2.22). So the sector signal has real value --
    # discarding it costs -- but using it to EXCLUDE costs more than using it to
    # penalise. The gate threw away the good name in the 8th-ranked sector.
    # Caveat on the record: 49 weeks, all NEUTRAL regime. No drawdown tested.
    if a.global_rank:
        act_mask = ok.ext.notna() & (ok.ext <= 2.5) & (ok.MS >= 55)
        rank_key = ok.composite.where(ok.composite.notna(), ok.MS)
        g = ok[act_mask].assign(_k=rank_key[act_mask]).sort_values("_k", ascending=False)
        shortlist = []
        for sym, r in g.head(a.global_rank).iterrows():
            shortlist.append(dict(symbol=sym, sector=r.sector, MS=round(float(r.MS), 1),
                                  SMS=(round(float(r.SMS), 1) if pd.notna(r.SMS) else None),
                                  composite=(round(float(r.composite), 1)
                                             if pd.notna(r.composite) else None),
                                  cap=r.cap, tier=str(r.tier), unverified=int(r.unver),
                                  entry="ACTIONABLE", px=round(float(r.px), 2)))
        print("\n" + "-" * 112)
        print("CROSS-SECTOR RANKING — top %d by composite (0.65*MS + 0.35*SMS), "
              "sector used as a penalty, not a filter" % a.global_rank)
        blocked = ok[ok.ext.notna() & (ok.ext > 2.5) & (ok.MS >= 55)]
        if len(blocked):
            b = blocked.assign(_k=rank_key[blocked.index]).nlargest(3, "_k")
            print("  (excluded as EXTENDED >2.5 ATR: %s)"
                  % ", ".join("%s %.1f ATR" % (i, r.ext) for i, r in b.iterrows()))
        secs_hit = pd.Series([x["sector"] for x in shortlist]).value_counts()
        if len(secs_hit) and secs_hit.iloc[0] > max(2, a.global_rank // 2):
            print("  !! %d of %d names from '%s'. Cross-sector ranking applies no "
                  "sector cap; L8 enforces the 40%% sector limit."
                  % (secs_hit.iloc[0], len(shortlist), secs_hit.index[0]))

    print("\n" + "-" * 112)
    print("SHORTLIST")
    for s in shortlist:
        print("  %-13s %-22s MS %5.1f  %-6s tier %s  | %s"
              % (s["symbol"], s["sector"][:22], s["MS"], s["cap"] or "?", s["tier"], s["entry"]))
        if s["unverified"]:
            print("      %d UNVERIFIED gate(s) — research shortlist only, not funded until cleared"
                  % s["unverified"])
    if shortlist and caps:
        sm = sum(1 for s in shortlist if s["cap"] in ("SMALL", "MID"))
        need = int(np.ceil(0.55 * len(shortlist)))
        print("\nCOMPOSITION: %d/%d small+mid (need >= %d)  %s"
              % (sm, len(shortlist), need, "OK" if sm >= need else "BREACH — drop a large cap"))
    print("=" * 112)
    if a.json:
        json.dump({"asof": str(W_px.index[-1].date()), "regime": a.regime,
                   "shortlist": shortlist,
                   "gates": {s: G[s] for s in [x["symbol"] for x in shortlist]},
                   "neutralized": missing, "cap_data": caps is not None},
                  open(a.json, "w"), indent=2, default=str)
        print("JSON written to %s" % a.json)


if __name__ == "__main__":
    main()
