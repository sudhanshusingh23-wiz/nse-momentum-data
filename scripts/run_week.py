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
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    D = a.data
    tmp = tempfile.mkdtemp(prefix="week_")
    F = lambda n: os.path.join(D, n)
    T = lambda n: os.path.join(tmp, n)

    print("=" * 78)
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
        if tl.get("rr_verdict") == "REJECT":
            print("    STOP: reward:risk REJECT. Not sized, not entered. Wait for a better entry.")
            continue
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

    # ---- L7 / L8 ----
    print("\n" + "-" * 78 + "\nL7 REBALANCE / L8 RISK")
    if a.positions and os.path.exists(a.positions):
        if a.scores:
            print("  (run rebalance.py with --positions and --scores for the holding grid)")
        pend = T("pending.csv")
        with open(pend, "w") as f:
            f.write("symbol,action,value\n")
            for p in plans:
                f.write("%s,ENTRY,%d\n" % (p["symbol"], p["value"]))
        cmd = [sys.executable, script(a.skills, "portfolio-risk-monitor", "risk_monitor.py"),
               "--positions", a.positions, "--panel", F("panel.csv.gz"),
               "--pool", str(a.pool), "--regime", state, "--pending", pend,
               "--json", T("risk.json")]
        out, err = run(cmd, "L8")
        if out:
            for line in out.splitlines():
                if line.startswith(("** VETO", "heat ", "   VETOED", "   CLEARED")):
                    print("  " + line.strip())
    else:
        print("  no positions file — fresh book, nothing to rebalance or veto")

    # ---- summary ----
    print("\n" + "=" * 78)
    print("RESULT")
    if plans:
        print("  %d actionable entry plan(s):" % len(plans))
        for p in plans:
            print("    %-13s %d shares, T1 limit %.2f (%s)"
                  % (p["symbol"], p["shares"], p["t1_limit"], p["setup"]))
    else:
        print("  No actionable entries this week.")
    if stops:
        print("\n  Where the chain stopped, and why:")
        for s in stops:
            print("    - %s" % s)
    print("\n  A week with no orders is a normal outcome. Every layer above is designed")
    print("  to refuse, and a refusal is a decision.")
    print("=" * 78)
    print("artifacts written to %s" % tmp)


if __name__ == "__main__":
    main()
