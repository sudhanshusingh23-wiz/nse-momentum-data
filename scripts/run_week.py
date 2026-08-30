#!/usr/bin/env python3
"""
run_week.py — run the full momentum chain end to end and report where it stops.

Sequences L1 -> L2 -> L3 -> L6 -> L4 -> L5 -> L7 -> L8. Each layer can legitimately
refuse (no qualifying sector, reward:risk REJECT, no clean setup, risk veto), and a
refusal is a RESULT, not an error: the run stops cleanly at that layer and says why.

This doubles as the flow test. If every layer reports either a decision or a
documented refusal, the wiring is sound.

Usage:
  python run_week.py --data ./artifacts --skills /mnt/skills/user --pool 8000000
  python run_week.py --data ./artifacts --skills ./skills --dry-run
"""

import argparse, json, os, subprocess, sys, tempfile

LAYERS = [
    ("L1", "market-regime-monitor", "regime_score.py"),
    ("L2", "sector-momentum-rotation", "sector_score.py"),
    ("L3", "momentum-stock-selector", "stock_select.py"),
    ("L6", "target-ladder-builder", "target_ladder.py"),
    ("L4", "position-sizer", "position_size.py"),
    ("L5", "entry-ladder-planner", "entry_plan.py"),
    ("L7", "weekly-rebalance-engine", "rebalance.py"),
    ("L8", "portfolio-risk-monitor", "risk_monitor.py"),
]


def _held_sectors(path):
    try:
        import csv
        with open(path) as f:
            return sorted({r["sector"] for r in csv.DictReader(f) if r.get("sector")})
    except Exception:
        return []


def script(skills, name, fn):
    p = os.path.join(skills, name, "scripts", fn)
    return p if os.path.exists(p) else None


def run(cmd, label):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return None, "%s could not run: %s" % (label, e)
    if r.returncode != 0:
        return r.stdout, "exit %d: %s" % (r.returncode, (r.stderr or "").strip().splitlines()[-1:] or "")
    return r.stdout, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="./artifacts")
    ap.add_argument("--skills", default="/mnt/skills/user")
    ap.add_argument("--pool", type=float, default=8000000.0)
    ap.add_argument("--positions", help="positions.csv; omit for a fresh book")
    ap.add_argument("--scores", help="scores.csv for the rebalance grid")
    ap.add_argument("--map", help="symbol,sector map")
    ap.add_argument("--dry-run", action="store_true", help="check wiring only")
    ap.add_argument("--force-sectors", action="store_true",
                    help="TEST MODE. Bypass the two-week hysteresis confirmation and take "
                         "the top 3 by SMS directly, so L3-L8 can be exercised. Output is "
                         "NOT a tradeable plan and is labelled as such throughout.")
    ap.add_argument("--force-entries", action="store_true",
                    help="TEST MODE. Continue past a reward:risk REJECT so L4/L5 run. "
                         "Never use outside a wiring test.")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    D = a.data
    tmp = tempfile.mkdtemp(prefix="week_")
    F = lambda n: os.path.join(D, n)
    T = lambda n: os.path.join(tmp, n)

    print("=" * 78)
    if a.force_sectors or a.force_entries:
        print("***  TEST MODE — GATES BYPASSED. OUTPUT IS NOT A TRADEABLE PLAN.  ***")
    print("WEEKLY RUN — data %s | skills %s | pool Rs %s"
          % (D, a.skills, format(int(a.pool), ",")))
    print("=" * 78)

    # ---- preflight ----
    print("\nPREFLIGHT")
    missing_s = [(l, n) for l, n, f in LAYERS if not script(a.skills, n, f)]
    for l, n, f in LAYERS:
        p = script(a.skills, n, f)
        print("  %-3s %-28s %s" % (l, n, "OK" if p else "SCRIPT NOT FOUND"))
    need = ["breadth.json", "nifty500.csv", "panel.csv.gz"]
    opt = ["smallcap250.csv", "nifty50.csv", "vix.csv", "sector_indices.csv", "caps.csv"]
    for f in need:
        print("  data %-22s %s" % (f, "OK" if os.path.exists(F(f)) else "MISSING (required)"))
    for f in opt:
        print("  data %-22s %s" % (f, "OK" if os.path.exists(F(f)) else "absent (degraded)"))
    man = F("manifest.json")
    if os.path.exists(man):
        m = json.load(open(man))
        print("  manifest latest_session %s | warnings %d"
              % (m.get("latest_session"), len(m.get("warnings", []))))
        for w in m.get("warnings", []):
            print("     WARNING: %s" % w)
    else:
        print("  manifest.json absent — cannot verify data freshness. Do not trade on this run.")
    if missing_s:
        print("\nSTOP: %d skill script(s) not found. Install the skills or pass --skills."
              % len(missing_s))
        sys.exit(1)
    if a.dry_run:
        print("\nDry run complete: wiring checks passed.")
        sys.exit(0)

    stops = []

    # ---- L1 ----
    print("\n" + "-" * 78 + "\nL1 REGIME")
    cmd = [sys.executable, script(a.skills, "market-regime-monitor", "regime_score.py"),
           "--benchmark", F("nifty500.csv"), "--breadth-json", F("breadth.json"),
           "--pool", str(a.pool), "--json", T("regime.json")]
    for flag, f in (("--smallcap", "smallcap250.csv"), ("--largecap", "nifty50.csv"),
                    ("--vix", "vix.csv"), ("--largecap-index", "nifty50.csv")):
        if os.path.exists(F(f)):
            cmd += [flag, F(f)]
    out, err = run(cmd, "L1")
    if err or not os.path.exists(T("regime.json")):
        print("  FAILED: %s" % err); sys.exit(2)
    reg = json.load(open(T("regime.json")))
    state = reg["state_after_transition"]
    print("  state %s | score %.1f | deployment %d%% | heat cap %.1f%%%s"
          % (state, reg["composite_score"], reg["mandate"]["max_deployment_pct"],
             reg["mandate"]["heat_cap_pct"], "  [PARTIAL]" if reg.get("partial") else ""))
    if state in ("RISK-OFF", "SHOCK"):
        stops.append("L1: regime %s permits no new entries. Existing positions are managed "
                     "only; skip L3-L5 entirely." % state)

    # ---- L2 ----
    print("\n" + "-" * 78 + "\nL2 SECTORS")
    if not (a.map and os.path.exists(F("sector_indices.csv"))):
        print("  SKIPPED: needs --map and sector_indices.csv")
        sel = []
    else:
        cmd = [sys.executable, script(a.skills, "sector-momentum-rotation", "sector_score.py"),
               "--prices", F("panel.csv.gz"), "--map", a.map,
               "--sector-index", F("sector_indices.csv"), "--regime", state,
               "--pool", str(a.pool), "--json", T("sector.json")]
        out, err = run(cmd, "L2")
        if err:
            print("  FAILED: %s" % err); sys.exit(2)
        sj = json.load(open(T("sector.json")))
        sel = sj.get("selected", [])
        if a.force_sectors and not sel:
            sel = [r["sector"] for r in sj.get("ranking", [])[:3]]
            print("  !! TEST MODE: hysteresis bypassed. Taking top 3 by SMS: %s"
                  % ", ".join(sel))
            print("  !! These sectors have NOT confirmed. Nothing here is tradeable.")
        top = sj.get("ranking", [])[:5]
        for r in top:
            print("  %-24s SMS %5.1f  rank %d" % (r["sector"][:24], r["SMS"], r["rank"]))
        for h in sj.get("hysteresis_actions", [])[:4]:
            print("     %s: %s — %s" % (h["sector"][:20], h["action"], h["reason"][:44]))
        if not sel:
            stops.append("L2: no sector cleared the entry gates. Capital stays in cash. "
                         "This is intended behaviour, not a failure.")

    # ---- L3 ----
    print("\n" + "-" * 78 + "\nL3 STOCK SELECTION")
    short = []
    if not sel:
        print("  SKIPPED: no confirmed sectors")
    else:
        cmd = [sys.executable, script(a.skills, "momentum-stock-selector", "stock_select.py"),
               "--panel", F("panel.csv.gz"), "--map", a.map, "--benchmark", F("nifty500.csv"),
               "--sectors", ",".join(sel), "--sms", T("sector.json"), "--regime", state,
               "--pool", str(a.pool), "--json", T("select.json")]
        if os.path.exists(F("caps.csv")):
            cmd += ["--caps", F("caps.csv")]
        out, err = run(cmd, "L3")
        if err:
            print("  FAILED: %s" % err); sys.exit(2)
        short = json.load(open(T("select.json"))).get("shortlist", [])
        for s in short:
            print("  %-13s MS %5.1f  %-6s tier %s  %s"
                  % (s["symbol"], s["MS"], s.get("cap") or "?", s["tier"], s["entry"][:38]))
            if s.get("unverified"):
                print("     %d unverified gate(s) — research only, not funded" % s["unverified"])
        if not short:
            stops.append("L3: no stock cleared the MS>=55 floor in the selected sectors.")

    # ---- L6 -> L4 -> L5 per candidate ----
    print("\n" + "-" * 78 + "\nL6 TARGETS -> L4 SIZE -> L5 ENTRY")
    plans = []
    for s in short[:3]:
        sym, tier = s["symbol"], s["tier"] if s["tier"] in ("A", "B", "C") else "B"
        print("\n  %s (tier %s)" % (sym, tier))
        out, err = run([sys.executable, script(a.skills, "target-ladder-builder", "target_ladder.py"),
                        "--panel", F("panel.csv.gz"), "--symbol", sym, "--tier", tier,
                        "--json", T("tl_%s.json" % sym)], "L6")
        if err or not os.path.exists(T("tl_%s.json" % sym)):
            print("    L6 could not build a ladder: %s" % err); continue
        tl = json.load(open(T("tl_%s.json" % sym)))
        print("    L6: R %.2f (%.1f%%) | anchor %.2f | reward:risk %.2f -> %s"
              % (tl["R"], tl["R"] / tl["entry"] * 100, tl["anchor"],
                 tl.get("reward_to_risk", 0), tl.get("rr_verdict")))
        if tl.get("rr_verdict") == "REJECT" and not a.force_entries:
            print("    STOP: reward:risk REJECT. Not sized, not entered. Wait for a better entry.")
            continue
        if tl.get("rr_verdict") == "REJECT":
            print("    !! TEST MODE: continuing past a REJECT purely to exercise L4/L5.")
        out, err = run([sys.executable, script(a.skills, "position-sizer", "position_size.py"),
                        "--from-ladder", T("tl_%s.json" % sym), "--pool", str(a.pool),
                        "--regime", state, "--json", T("ps_%s.json" % sym)], "L4")
        if not os.path.exists(T("ps_%s.json" % sym)):
            print("    L4 declined to size (below minimum, or upstream reject).")
            continue
        ps = json.load(open(T("ps_%s.json" % sym)))
        print("    L4: %d shares Rs %s (%.1f%%) | bound by %s"
              % (ps["shares"], format(ps["value"], ","), ps["pct_of_pool"], ps["binding_constraint"]))
        out, err = run([sys.executable, script(a.skills, "entry-ladder-planner", "entry_plan.py"),
                        "--panel", F("panel.csv.gz"), "--symbol", sym,
                        "--shares", str(ps["shares"]), "--tier", tier, "--regime", state,
                        "--json", T("ep_%s.json" % sym)], "L5")
        if not os.path.exists(T("ep_%s.json" % sym)):
            print("    L5: no clean setup today — no entry planned.")
            continue
        ep = json.load(open(T("ep_%s.json" % sym)))
        if ep.get("setup") == "none":
            print("    L5: no clean setup today — no entry planned.")
            continue
        print("    L5: %s | T1 %d sh limit %.2f" % (ep["setup"], ep["shares"]["T1"], ep["t1_limit"]))
        plans.append({"symbol": sym, "shares": ps["shares"], "value": ps["value"],
                      "t1_limit": ep["t1_limit"], "setup": ep["setup"]})

    # ---- L7 rebalance ----
    print("\n" + "-" * 78 + "\nL7 REBALANCE")
    reb_orders, sector_arg = [], None
    if a.positions and os.path.exists(a.positions) and a.scores and os.path.exists(a.scores):
        sector_arg = ",".join("%s:%s" % (x, "TOP3" if x in sel else "EJECTED")
                              for x in sorted(set(list(sel) + _held_sectors(a.positions))))
        cmd = [sys.executable, script(a.skills, "weekly-rebalance-engine", "rebalance.py"),
               "--positions", a.positions, "--scores", a.scores,
               "--sectors", sector_arg, "--regime", state, "--pool", str(a.pool),
               "--json", T("rebal.json")]
        out, err = run(cmd, "L7")
        if os.path.exists(T("rebal.json")):
            rj = json.load(open(T("rebal.json")))
            reb_orders = rj.get("orders", [])
            for h in rj.get("holdings", []):
                if not str(h.get("action", "")).startswith("HOLD"):
                    print("  %-13s %-14s %s" % (h["symbol"], h["action"], str(h["why"])[:44]))
            for d in rj.get("deferred", []):
                print("  %-13s DEFERRED       %s" % (d["symbol"], str(d["defer"])[:44]))
            if not reb_orders:
                print("  no actions this week — every holding is HOLD or deferred")
        else:
            print("  L7 produced no plan: %s" % (err or "unknown"))
    elif a.positions:
        print("  --scores not supplied; cannot run the holding grid")
    else:
        print("  no positions file — fresh book, nothing to rebalance")

    # ---- L8 veto gate ----
    # EVERY risk-increasing action goes through the gate: new entries from the
    # L3-L5 chain AND adds generated by the L7 grid. Risk-reducing actions
    # (exits, trims, target bookings) are never vetoed - they lower exposure.
    print("\n" + "-" * 78 + "\nL8 RISK GATE")
    buy_side, sell_side = [], []
    for p in plans:
        buy_side.append({"symbol": p["symbol"], "action": "ENTRY", "value": p["value"],
                         "detail": "%d sh, T1 limit %.2f" % (p["shares"], p["t1_limit"])})
    for o in reb_orders:
        if o.get("side") == "BUY":
            val = o.get("value")
            if not val:
                val = int(a.pool * 0.0375)   # an add is 25% of a max-size position
                o["value"] = val
            buy_side.append({"symbol": o["symbol"], "action": o.get("action", "ADD"),
                             "value": val, "detail": "size via L4 at execution"})
        else:
            sell_side.append(o)

    vetoed, cleared, standing = {}, [], []
    if a.positions and os.path.exists(a.positions):
        pend = T("pending.csv")
        with open(pend, "w") as f:
            f.write("symbol,action,value\n")
            for b in buy_side:
                f.write("%s,%s,%d\n" % (b["symbol"], b["action"], b["value"]))
        cmd = [sys.executable, script(a.skills, "portfolio-risk-monitor", "risk_monitor.py"),
               "--positions", a.positions, "--panel", F("panel.csv.gz"),
               "--pool", str(a.pool), "--regime", state, "--pending", pend,
               "--json", T("risk.json")]
        out, err = run(cmd, "L8")
        if out:
            for line in out.splitlines():
                if line.startswith(("** VETO", "heat ")) or "BREACH" in line:
                    print("  " + line.strip())
        if os.path.exists(T("risk.json")):
            rk = json.load(open(T("risk.json")))
            for v in rk.get("vetoes", []):
                act = v["action"]
                if "ALL" in act.upper():
                    # A blanket restriction. If there are pending buys it vetoes them;
                    # if there are none it is a STANDING restriction that will block
                    # next week's buys too, and must still be reported.
                    if buy_side:
                        for b in buy_side:
                            vetoed.setdefault(b["symbol"], []).append(v["reason"])
                    else:
                        standing.append((act, v["reason"]))
                else:
                    for b in buy_side:
                        if b["symbol"] in act:
                            vetoed.setdefault(b["symbol"], []).append(v["reason"])
        cleared = [b for b in buy_side if b["symbol"] not in vetoed]
    else:
        if buy_side:
            print("  no positions file — cannot evaluate portfolio limits.")
            print("  Buy-side actions are UNGATED and must not be executed.")
        cleared = []

    # ---- reconciled plan ----
    print("\n" + "-" * 78 + "\nFINAL ORDER PLAN (post-veto)")
    if sell_side:
        print("  risk-reducing (never vetoed):")
        for o in sell_side:
            print("     SELL  %-13s %-12s qty %s" % (o["symbol"], o.get("action", ""), o.get("qty")))
    if cleared:
        print("  risk-increasing (CLEARED by L8):")
        for b in cleared:
            print("     BUY   %-13s %-12s Rs %-10s %s"
                  % (b["symbol"], b["action"], format(b["value"], ","), b["detail"]))
    if vetoed:
        print("  risk-increasing (VETOED — do not execute):")
        for sym, reasons in vetoed.items():
            print("     XXXX  %-13s %s" % (sym, reasons[0][:56]))
    if standing:
        print("  standing restrictions (no pending buys to refuse, but these block any):")
        for act, why in standing:
            print("     ----  %-13s %s" % (act[:13], why[:60]))
    if not sell_side and not cleared and not vetoed:
        print("  no orders.")

    # ---- summary ----
    print("\n" + "=" * 78)
    print("RESULT")
    n_sell, n_buy, n_veto = len(sell_side), len(cleared), len(vetoed)
    print("  %d sell order(s), %d cleared buy(s), %d vetoed, %d standing restriction(s)."
          % (n_sell, n_buy, n_veto, len(standing)))
    if n_veto:
        print("  L8 refused %d risk-increasing action(s). The veto stands." % n_veto)
    if standing and not n_veto:
        print("  No buys were proposed, but L8 has standing restrictions that would have")
        print("  blocked them. Resolve those before next week.")
    if not (n_sell or n_buy):
        print("  No actionable orders this week.")
    if stops:
        print("\n  Where the chain stopped, and why:")
        for s in stops:
            print("    - %s" % s)
    if a.force_sectors or a.force_entries:
        print("\n  *** TEST MODE was active. Gates were bypassed to exercise the wiring.")
        print("  *** Do not act on anything above. Re-run without the force flags for a")
        print("  *** real decision.")
    else:
        print("\n  A week with no orders is a normal outcome. Every layer above is designed")
        print("  to refuse, and a refusal is a decision.")
    print("=" * 78)
    print("artifacts written to %s" % tmp)


if __name__ == "__main__":
    main()
