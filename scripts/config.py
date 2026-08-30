#!/usr/bin/env python3
"""
config.py — load strategy.yaml and resolve every percentage into rupees against
the current pool, plus apply the capital-scaling table.

No skill should hardcode a number or do its own percentage arithmetic. Import this.

    from config import load
    cfg = load()                      # uses pool.current from the yaml
    cfg = load(pool=10000000)         # override for what-if analysis

    cfg.rupees("sizing.position_pct.max")        -> 1200000
    cfg.get("stock.gates.g1_min_adv_cr")         -> 6.0 at a 1Cr pool (scaled)
    cfg.state("NEUTRAL")["max_deployment_rupees"]

Run directly to print a resolved summary:
    python config.py --pool 8000000
"""

import argparse, json, os, sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML required:  pip install pyyaml")

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(HERE, "..", "config", "strategy.yaml")


class Config(object):
    def __init__(self, raw, pool=None):
        self.raw = raw
        self.pool = float(pool if pool is not None else raw["pool"]["current"])
        self._scale = self._resolve_scaling()

    # ---- access -------------------------------------------------------
    def get(self, path, default=None):
        """Dotted lookup. Scaling-table keys are overridden by pool size."""
        if path in ("stock.gates.g1_min_adv_cr",):
            return self._scale["adv_floor_cr"]
        if path in ("stock.gates.g1_min_adv_cr_large_position",):
            return self._scale["adv_preferred_cr"]
        node = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def rupees(self, path, default=None):
        """Resolve a percentage-of-pool parameter into rupees."""
        v = self.get(path, default)
        if v is None:
            return None
        return int(round(self.pool * float(v) / 100.0))

    # ---- scaling ------------------------------------------------------
    def _resolve_scaling(self):
        rows = sorted(self.raw.get("scaling", []), key=lambda r: r["pool"])
        if not rows:
            return {"adv_floor_cr": 5, "adv_preferred_cr": 8, "max_smallcap_pct": 60}
        chosen = rows[0]
        for r in rows:
            if self.pool >= r["pool"]:
                chosen = r
        return chosen

    @property
    def scaling(self):
        return dict(self._scale)

    # ---- regime -------------------------------------------------------
    def state(self, name):
        s = dict(self.get("regime.states." + name, {}))
        if not s:
            raise KeyError("unknown regime state: %s" % name)
        s["max_deployment_rupees"] = int(round(self.pool * s["max_deployment_pct"] / 100.0))
        s["heat_cap_rupees"] = int(round(self.pool * s["heat_cap_pct"] / 100.0))
        # smallcap ceiling is the tighter of the regime cap and the scaling table
        s["max_smallcap_pct"] = min(s.get("max_smallcap_pct", 100),
                                    self._scale["max_smallcap_pct"])
        s["max_smallcap_rupees"] = int(round(self.pool * s["max_smallcap_pct"] / 100.0))
        return s

    # ---- sizing -------------------------------------------------------
    def tier(self, letter):
        t = dict(self.get("sizing.tiers." + letter, {}))
        if not t:
            raise KeyError("unknown tier: %s" % letter)
        t["risk_rupees"] = int(round(self.pool * t["risk_pct"] / 100.0))
        return t

    def position_bounds(self, mode="normal"):
        p = self.get("sizing.position_pct")
        mx = p["max_concentration_mode"] if mode == "concentration" else p["max"]
        return {"min_rupees": int(round(self.pool * p["min"] / 100.0)),
                "max_rupees": int(round(self.pool * mx / 100.0)),
                "min_pct": p["min"], "max_pct": mx}

    # ---- integrity ----------------------------------------------------
    def assert_target_blind(self):
        """The quarterly target must never be readable by a decision rule."""
        if self.get("objective.target_visible_to_rules", False):
            raise AssertionError(
                "objective.target_visible_to_rules is true. No rule may read P&L "
                "versus target — that is what turns a momentum system into a "
                "position-squaring machine. Set it back to false.")
        return True

    def summary(self):
        self.assert_target_blind()
        pb = self.position_bounds()
        out = {
            "pool": int(self.pool),
            "scaling_row": self._scale,
            "position_min_rupees": pb["min_rupees"],
            "position_max_rupees": pb["max_rupees"],
            "adv_floor_cr": self._scale["adv_floor_cr"],
            "tiers": {k: self.tier(k) for k in ("A", "B", "C")},
            "states": {k: self.state(k) for k in ("RISK-ON", "NEUTRAL", "RISK-OFF", "SHOCK")},
            "config_version": self.raw.get("version"),
        }
        return out


def load(path=None, pool=None):
    p = path or DEFAULT_PATH
    if not os.path.exists(p):
        sys.exit("strategy.yaml not found at %s" % p)
    with open(p) as f:
        raw = yaml.safe_load(f)
    cfg = Config(raw, pool=pool)
    cfg.assert_target_blind()
    return cfg


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=float)
    ap.add_argument("--path")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    c = load(a.path, a.pool)
    s = c.summary()
    if a.json:
        print(json.dumps(s, indent=2)); sys.exit(0)
    print("=" * 62)
    print("STRATEGY CONFIG v%s   pool Rs %s" % (s["config_version"], format(s["pool"], ",")))
    print("=" * 62)
    print("scaling row      : ADV floor Rs%dcr, preferred Rs%dcr, smallcap cap %d%%"
          % (s["adv_floor_cr"], s["scaling_row"]["adv_preferred_cr"],
             s["scaling_row"]["max_smallcap_pct"]))
    print("position bounds  : Rs %s to Rs %s"
          % (format(s["position_min_rupees"], ","), format(s["position_max_rupees"], ",")))
    print("-" * 62)
    print("%-8s %8s %12s   %s" % ("TIER", "RISK%", "RISK Rs", "TRANCHE 3"))
    for k, t in s["tiers"].items():
        print("%-8s %7.2f%% %12s   %s"
              % (k, t["risk_pct"], format(t["risk_rupees"], ","), t.get("tranche3")))
    print("-" * 62)
    print("%-10s %8s %14s %8s %12s" % ("STATE", "DEPLOY%", "DEPLOY Rs", "HEAT%", "SMALLCAP Rs"))
    for k, v in s["states"].items():
        print("%-10s %7d%% %14s %7.1f%% %12s"
              % (k, v["max_deployment_pct"], format(v["max_deployment_rupees"], ","),
                 v["heat_cap_pct"], format(v["max_smallcap_rupees"], ",")))
    print("=" * 62)
    print("target-blind check: PASSED (no rule can read P&L vs target)")
