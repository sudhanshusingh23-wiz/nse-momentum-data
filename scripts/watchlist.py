#!/usr/bin/env python3
"""
watchlist.py — Phase 1 store for the equity watchlist.

The memory layer for the L1-L8 momentum system. Holds candidate names between
the moment they surface and the moment capital is committed, and persists the
cross-run history that L7's decision grid needs but has nowhere to store.

Computes no scores. All momentum data arrives from L3 (`stock_select.py --json`).

Records carry three zones:
  machine   pipeline-owned, overwritten wholesale on every ingest
  human     user-owned, never touched by an ingest
  derived   computed on read, never stored

Stdlib only. Lives at the repo root next to artifacts/.

Usage:
  python watchlist.py init
  python watchlist.py add TITAN --price 3420 --thesis "..." --book core
  python watchlist.py ingest --json scan.json --symbols KAYNES,APARINDS --price KAYNES=5810
  python watchlist.py list [--book momentum] [--band Active] [--json]
  python watchlist.py show KAYNES
  python watchlist.py note KAYNES "Held the 5800 retest."
  python watchlist.py set KAYNES --status "Trigger set" --conviction high
  python watchlist.py archive KAYNES --reason momentum_decay
  python watchlist.py restore KAYNES
  python watchlist.py validate
"""

import argparse
import csv
import gzip
import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone

SCHEMA_VERSION = 1
DEFAULT_PATH = "watchlist.json"

BOOKS = ("momentum", "core")
ORIGINS = ("scan", "user")
STATUSES = ("Surfaced", "Watching", "Trigger set", "Accumulating", "Held", "Exited")

# Eviction ladder (spec section 9). Bands are computed here; the eviction
# *actions* they imply are Phase 4. Thresholds are provisional by design.
# Keyed on MS alone. The selector does not emit rank, and MS is the same
# percentile viewed differently, so the spec's rank language is dropped.
BANDS = [
    (89.0, "Active"),
    (78.0, "Active"),
    (55.0, "Cooling"),
]
ARCHIVE_BAND = "Archive candidate"

MS_FLOOR = 55.0          # L3 shortlist floor
# ADV is not in the selector output. It applies --adv-floor-cr internally as a
# gate, so anything in the shortlist has already cleared it. Re-checking a
# number we do not have would be theatre.

# Mirrors stock_select.py's shortlist record exactly. It emits MS/SMS/composite/
# cap/tier/unverified/entry/px — no rank, no ADV, no ATR extension. Anything the
# selector does not emit is not invented here.
MACHINE_FIELDS = (
    "ms", "sms", "composite", "sector", "cap_tier", "tier", "gates",
    "unverified", "entry_signal", "px", "regime_at_ingest",
    "data_tier", "ms_asof", "neutralized", "dropped_from_scan",
    "scan_appearances", "first_seen_asof", "last_seen_asof",
)


# ----------------------------------------------------------------- utilities

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today():
    return date.today().isoformat()


def _parse_date(s):
    if not s:
        return None
    return date.fromisoformat(str(s)[:10])


def _days_between(a, b):
    da, db = _parse_date(a), _parse_date(b)
    if not da or not db:
        return None
    return (db - da).days


def blank_record(symbol, book="momentum", origin="user"):
    """A record with every key present. Absent data is None, never missing."""
    return {
        "symbol": symbol.upper(),
        "name": None,
        "book": book,
        "origin": origin,
        "scan_confirmed": origin == "scan",
        "machine": {f: None for f in MACHINE_FIELDS} | {"gates": {}},
        "ms_history": [],
        "human": {
            "thesis": None,
            "conviction": None,
            "status": "Watching",
            "notes": [],
            "custom_levels": {},
            "tags": [],
            "date_added": _today(),
            "price_at_add": None,
        },
        "technical": {"tag": None, "note": None, "asof": None},
        "fundamental_findings": [],
        "price": {"last": None, "asof": None},
        "created_at": _now(),
        "updated_at": _now(),
    }


# --------------------------------------------------------------------- store

def load(path=DEFAULT_PATH):
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"{path} not found. Run `python watchlist.py init` first."
        )
    with open(path) as fh:
        data = json.load(fh)
    v = data.get("schema_version")
    if v != SCHEMA_VERSION:
        raise ValueError(
            f"{path} is schema v{v}, this tool speaks v{SCHEMA_VERSION}. "
            "Migrate before continuing."
        )
    return data


def save(data, path=DEFAULT_PATH):
    """Atomic write. The file is git-tracked; a truncated write is unacceptable."""
    data["updated_at"] = _now()
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def empty_store():
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _now(),
        "updated_at": _now(),
        "active": [],
        "archive": [],
    }


def find(data, symbol, where="active"):
    sym = symbol.upper()
    for r in data[where]:
        if r["symbol"] == sym:
            return r
    return None


# ------------------------------------------------------------------- derived

def derive(rec, asof_price_date=None):
    """Computed on read, never stored. Spec section 5.3."""
    m, h = rec["machine"], rec["human"]
    hist = rec["ms_history"]
    d = {}

    d["days_tracked"] = _days_between(h["date_added"], _today())

    last = rec["price"]["last"]
    add = h["price_at_add"]
    if last is not None and add:
        d["return_abs"] = round(last - add, 2)
        d["return_pct"] = round((last / add - 1) * 100, 2)
    else:
        d["return_abs"] = d["return_pct"] = None
    ba, bl = rec["price"].get("bench_at_add"), rec["price"].get("bench_last")
    if d["return_pct"] is not None and ba and bl:
        d["bench_pct"] = round((bl / ba - 1) * 100, 2)
        d["return_vs_nifty500"] = round(d["return_pct"] - d["bench_pct"], 2)
    else:
        d["bench_pct"] = d["return_vs_nifty500"] = None

    ms_vals = [e["ms"] for e in hist if e.get("ms") is not None]
    d["delta_ms_1w"] = round(ms_vals[-1] - ms_vals[-2], 1) if len(ms_vals) >= 2 else None
    d["delta_ms_2w"] = round(ms_vals[-1] - ms_vals[-3], 1) if len(ms_vals) >= 3 else None

    if ms_vals:
        best = max(ms_vals)
        d["best_ms"] = best
        idx = max(i for i, v in enumerate(ms_vals) if v == best)
        d["weeks_since_best"] = len(ms_vals) - 1 - idx
    else:
        d["best_ms"] = d["weeks_since_best"] = None

    d["band"] = band_for(m["ms"])
    d["scan_appearances"] = m.get("scan_appearances") or 0
    d["weeks_since_seen"] = (
        len([e for e in hist if e["asof"] > m["last_seen_asof"]])
        if m.get("last_seen_asof") else None)
    d["flags"] = flags(rec)
    d["recurring"] = d["scan_appearances"] >= 3
    d["gate_summary"] = gate_summary(m.get("gates") or {})
    return d


def band_for(ms):
    if ms is None:
        return None
    for floor, label in BANDS:
        if ms >= floor:
            return label
    return ARCHIVE_BAND


def gate_summary(gates):
    if not gates:
        return {"pass": 0, "fail": 0, "unverified": 0, "total": 0}
    vals = [str(v).upper() for v in gates.values()]
    return {
        "pass": vals.count("PASS"),
        "fail": vals.count("FAIL"),
        "unverified": vals.count("UNVERIFIED"),
        "total": len(vals),
    }


def flags(rec):
    """Context worth knowing, not permission to act.

    A watchlist is a place to watch things. Whether capital may be committed
    is L8's call, made at the moment of sizing against live portfolio heat —
    not something to precompute here from a week-old scan.
    """
    out = []
    g = gate_summary(rec["machine"].get("gates") or {})
    if g["fail"]:
        out.append(f"{g['fail']} gate(s) FAIL")
    if g["unverified"]:
        out.append(f"{g['unverified']} gate(s) unchecked")
    ms = rec["machine"]["ms"]
    if ms is not None and ms < MS_FLOOR:
        out.append(f"MS {ms}, below the selector's own shortlist floor")
    if rec["human"]["price_at_add"] is None:
        out.append("no price_at_add, so return is not tracked")
    return out


# ------------------------------------------------------------------ mutation

def add_manual(data, symbol, book="momentum", price=None, thesis=None,
               conviction=None, name=None, tags=None, date_added=None):
    sym = symbol.upper()
    if find(data, sym):
        raise ValueError(f"{sym} is already on the active list.")
    arch = find(data, sym, "archive")
    rec = blank_record(sym, book=book, origin="user")
    rec["name"] = name
    rec["human"]["price_at_add"] = price
    rec["human"]["thesis"] = thesis
    rec["human"]["conviction"] = conviction
    rec["human"]["tags"] = list(tags or [])
    if date_added:
        rec["human"]["date_added"] = date_added
    if arch:
        # Carry history forward rather than starting clean. Spec R17 — the
        # point of archiving rather than deleting is that we remember.
        rec["ms_history"] = arch.get("ms_history", [])
        rec["human"]["notes"] = arch["human"].get("notes", [])
        rec["human"]["notes"].append({
            "date": _today(),
            "text": f"Re-added. Previously archived: {arch.get('archive_reason')}",
        })
        rec["readded_from_archive"] = arch.get("archived_at")
        data["archive"].remove(arch)
    data["active"].append(rec)
    return rec


def ingest(data, scan, symbols=None, prices=None, book="momentum", capture_all=False):
    """Merge L3 scan output into the store. Keyed on symbol.

    Machine zone is overwritten wholesale. Human zone is never touched.
    Additive — nothing is evicted (R15).

    `dropped_from_scan` is deliberately NOT set here: a selection run only
    emits the top --show-n per sector, so absence from it means nothing.
    Drop detection belongs to the Lane B refresh run (Phase 3).
    """
    prices = prices or {}
    shortlist = {s["symbol"].upper(): s for s in scan.get("shortlist", [])}
    # The selector's `shortlist` IS the top-n per sector; --show-n only affects
    # what it prints. Capturing the array captures the top 3 per sector.
    if capture_all:
        symbols = list(shortlist)
    symbols = symbols or []
    gates_all = scan.get("gates", {})
    asof = scan.get("asof")
    regime = scan.get("regime")
    neutralized = scan.get("neutralized") or []

    added, updated, missing = [], [], []

    for raw in symbols:
        sym = raw.upper()
        entry = shortlist.get(sym)
        if entry is None:
            missing.append(sym)
            continue

        rec = find(data, sym)
        is_new = rec is None
        if is_new:
            rec = blank_record(sym, book=book, origin="scan")
            # Surfaced, not Watching. The scan captured it; you have not yet
            # decided to track it. `adopt` is that decision (R30).
            rec["human"]["status"] = "Surfaced"
            if sym in prices:
                rec["human"]["price_at_add"] = prices[sym]
                rec["human"]["status"] = "Watching"
            data["active"].append(rec)
        else:
            # An existing USER name confirmed by a scan is now fundable-eligible.
            rec["scan_confirmed"] = True

        m = rec["machine"]
        m["ms"] = entry.get("MS")
        m["sms"] = entry.get("SMS")
        m["composite"] = entry.get("composite")
        m["sector"] = entry.get("sector")
        m["cap_tier"] = entry.get("cap")
        m["tier"] = entry.get("tier")
        m["unverified"] = entry.get("unverified")
        m["entry_signal"] = entry.get("entry")      # ACTIONABLE / EXTENDED / ...
        m["px"] = entry.get("px")                   # close, NOT price_at_add
        m["gates"] = gates_all.get(sym, {})
        # Recurrence is the signal this list exists to show. A name in the top
        # three for four straight weeks is a different animal from a one-off.
        if asof and asof != m.get("last_seen_asof"):
            m["scan_appearances"] = (m.get("scan_appearances") or 0) + 1
            m["first_seen_asof"] = m.get("first_seen_asof") or asof
            m["last_seen_asof"] = asof
        m["regime_at_ingest"] = regime
        m["data_tier"] = scan.get("data_tier", 1 if scan.get("cap_data") else 2)
        m["ms_asof"] = asof
        m["neutralized"] = neutralized
        append_ms_history(rec, asof, entry.get("MS"))
        rec["updated_at"] = _now()
        (added if is_new else updated).append(sym)

    return {"added": added, "updated": updated, "not_in_scan": missing,
            "asof": asof, "regime": regime}


def append_ms_history(rec, asof, ms):
    """Appended, never overwritten (R14). Re-running the same date replaces
    that date's entry rather than creating a duplicate."""
    if asof is None or ms is None:
        return
    hist = rec["ms_history"]
    for e in hist:
        if e["asof"] == asof:
            e["ms"] = ms
            return
    hist.append({"asof": asof, "ms": ms})
    hist.sort(key=lambda e: e["asof"])


def adopt(data, symbol, price, thesis=None, conviction=None):
    """Turn a Surfaced name into a tracked one.

    This is the moment R30 is about: price_at_add is the price when *you*
    decide to track, not the close the scan happened to surface it at.
    """
    rec = find(data, symbol)
    if rec is None:
        raise ValueError(f"{symbol.upper()} is not on the active list.")
    if rec["human"]["status"] != "Surfaced":
        raise ValueError(
            f"{rec['symbol']} is already adopted (status {rec['human']['status']}, "
            f"added {rec['human']['date_added']}). Use `set` to change it.")
    rec["human"]["price_at_add"] = price
    rec["human"]["date_added"] = _today()
    rec["human"]["status"] = "Watching"
    if thesis:
        rec["human"]["thesis"] = thesis
    if conviction:
        rec["human"]["conviction"] = conviction
    rec["updated_at"] = _now()
    return rec


def archive(data, symbol, reason):
    rec = find(data, symbol)
    if rec is None:
        raise ValueError(f"{symbol.upper()} is not on the active list.")
    data["active"].remove(rec)
    rec["archive_reason"] = reason
    rec["archived_at"] = _today()
    data["archive"] = [r for r in data["archive"] if r["symbol"] != rec["symbol"]]
    data["archive"].append(rec)
    return rec


def restore(data, symbol):
    rec = find(data, symbol, "archive")
    if rec is None:
        raise ValueError(f"{symbol.upper()} is not in the archive.")
    data["archive"].remove(rec)
    rec.pop("archive_reason", None)
    rec.pop("archived_at", None)
    rec["updated_at"] = _now()
    data["active"].append(rec)
    return rec


def add_note(data, symbol, text):
    rec = find(data, symbol) or find(data, symbol, "archive")
    if rec is None:
        raise ValueError(f"{symbol.upper()} not found.")
    rec["human"]["notes"].append({"date": _today(), "text": text})
    rec["updated_at"] = _now()
    return rec


def set_human(data, symbol, **fields):
    """Human zone only. There is deliberately no path here to the machine zone."""
    rec = find(data, symbol)
    if rec is None:
        raise ValueError(f"{symbol.upper()} is not on the active list.")
    for k, v in fields.items():
        if v is None:
            continue
        if k not in rec["human"]:
            raise ValueError(f"'{k}' is not a human-zone field.")
        if k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        rec["human"][k] = v
    rec["updated_at"] = _now()
    return rec


# ------------------------------------------------------------ lane A: prices

def _open_maybe_gz(path):
    return gzip.open(path, "rt", newline="") if path.endswith(".gz") \
        else open(path, newline="")


def _bench_series(path):
    """date -> close, from a two-column date,close file."""
    out = {}
    with _open_maybe_gz(path) as fh:
        for row in csv.DictReader(fh):
            try:
                out[row["date"][:10]] = float(row["close"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _on_or_before(series, when):
    """Last observation on or before `when`. Markets close; dates have holes."""
    keys = [k for k in series if k <= when]
    return (max(keys), series[max(keys)]) if keys else (None, None)


def refresh_prices(data, panel_path, benchmark_path=None):
    """Lane A. Touches price fields only — never MS, gates, tags or notes (R20).

    The panel is ~3M rows, so it is streamed once and only the symbols we
    actually track are retained.
    """
    want = {r["symbol"] for r in data["active"]}
    latest = {}
    with _open_maybe_gz(panel_path) as fh:
        for row in csv.DictReader(fh):
            sym = row.get("symbol")
            if sym not in want:
                continue
            d = (row.get("date") or "")[:10]
            if sym not in latest or d >= latest[sym][0]:
                try:
                    latest[sym] = (d, float(row["close"]))
                except (TypeError, ValueError):
                    continue

    bench = _bench_series(benchmark_path) if benchmark_path else {}
    b_asof, b_last = (max(bench), bench[max(bench)]) if bench else (None, None)

    updated, missing = [], []
    for rec in data["active"]:
        hit = latest.get(rec["symbol"])
        if hit is None:
            missing.append(rec["symbol"])
            continue
        d, px = hit
        rec["price"]["last"] = px
        rec["price"]["asof"] = d
        if bench:
            rec["price"]["bench_last"] = b_last
            rec["price"]["bench_asof"] = b_asof
            added = rec["human"]["date_added"]
            if added and rec["price"].get("bench_at_add") is None:
                _, bv = _on_or_before(bench, added)
                rec["price"]["bench_at_add"] = bv
        rec["updated_at"] = _now()
        updated.append(rec["symbol"])
    return {"updated": updated, "not_in_panel": missing,
            "asof": max((latest[s][0] for s in latest), default=None),
            "bench_asof": b_asof}


# ---------------------------------------------------------------- validation

def validate(data):
    problems = []
    seen = set()
    for r in data["active"]:
        s = r["symbol"]
        if s in seen:
            problems.append(f"{s}: duplicate on active list")
        seen.add(s)
        if s in {a["symbol"] for a in data["archive"]}:
            problems.append(f"{s}: present in both active and archive")
        if r["book"] not in BOOKS:
            problems.append(f"{s}: unknown book '{r['book']}'")
        if r["origin"] not in ORIGINS:
            problems.append(f"{s}: unknown origin '{r['origin']}'")
        if r["human"]["status"] not in STATUSES:
            problems.append(f"{s}: unknown status '{r['human']['status']}'")
        dates = [e["asof"] for e in r["ms_history"]]
        if dates != sorted(dates):
            problems.append(f"{s}: ms_history out of order")
        if len(dates) != len(set(dates)):
            problems.append(f"{s}: duplicate dates in ms_history")
    return problems


# ---------------------------------------------------------------------- view

def _fmt(v, width, dec=1):
    if v is None:
        return "-".rjust(width)
    if isinstance(v, float):
        return f"{v:.{dec}f}".rjust(width)
    return str(v).rjust(width)


def render_table(records):
    if not records:
        return "Nothing tracked yet. Add a name with `add` or `ingest`."
    hdr = (f"{'SYMBOL':<13}{'BK':<4}{'ORG':<5}{'MS':>6}{'BAND':>10}{'SIGNAL':>12}"
           f"{'GATES':>12}{'SEEN':>6}{'DAYS':>6}{'RET%':>8}  {'STATUS':<14}NOTE")
    lines = [hdr, "-" * len(hdr)]
    for r in records:
        d = derive(r)
        g = d["gate_summary"]
        gs = f"{g['pass']}P/{g['fail']}F/{g['unverified']}U" if g["total"] else "none"
        lines.append(
            f"{r['symbol']:<13}{r['book'][:3]:<4}{r['origin'][:4]:<5}"
            f"{_fmt(r['machine']['ms'], 6)}"
            f"{(d['band'] or '-'):>10}{(r['machine']['entry_signal'] or '-'):>12}{gs:>12}"
            f"{(('x' + str(d['scan_appearances'])) if d['scan_appearances'] else '-'):>6}"
            f"{_fmt(d['days_tracked'], 6)}{_fmt(d['return_pct'], 8, 2)}  "
            f"{r['human']['status']:<14}"
            f"{(str(len(d['flags'])) + ' flag' + ('' if len(d['flags']) == 1 else 's')) if d['flags'] else '-'}"
        )
    return "\n".join(lines)


def render_detail(rec):
    d = derive(rec)
    m, h = rec["machine"], rec["human"]
    out = [f"{rec['symbol']}  {rec['name'] or ''}".strip(),
           "=" * 60,
           f"book {rec['book']} | origin {rec['origin']} | "
           f"scan-confirmed {rec['scan_confirmed']} | tags {', '.join(h['tags']) or '-'}",
           "",
           "MACHINE ZONE" + (f"   (as of {m['ms_asof']})" if m["ms_asof"] else "   (no scan data)"),
           f"  MS {m['ms']}  composite {m['composite']}  band {d['band']}  tier {m['tier']}",
           f"  sector {m['sector']}  SMS {m['sms']}  cap {m['cap_tier']}",
           f"  signal {m['entry_signal']}  close at scan {m['px']}  "
           f"unverified gates {m['unverified']}",
           f"  seen in {m['scan_appearances'] or 0} scan(s), "
           f"first {m['first_seen_asof'] or '-'}, last {m['last_seen_asof'] or '-'}",
           f"  regime at ingest {m['regime_at_ingest']}  data tier {m['data_tier']}"]
    g = d["gate_summary"]
    if g["total"]:
        out.append(f"  gates: {g['pass']} PASS, {g['fail']} FAIL, {g['unverified']} UNVERIFIED")
        for k, v in (m["gates"] or {}).items():
            if str(v).upper() != "PASS":
                out.append(f"    !! {k}: {v}")
    else:
        out.append("  gates: no data")

    out += ["",
            "HUMAN ZONE",
            f"  status {h['status']}  conviction {h['conviction'] or '-'}",
            f"  added {h['date_added']} at {h['price_at_add'] or '-'}  "
            f"({d['days_tracked']} days)",
            f"  thesis: {h['thesis'] or '-'}"]
    if h["notes"]:
        out.append("  notes:")
        for n in h["notes"][-5:]:
            out.append(f"    {n['date']}  {n['text']}")

    out += ["",
            "DERIVED",
            f"  return since add {d['return_abs']} ({d['return_pct']}%)"
            + (f"   Nifty500 {d['bench_pct']}%   relative "
               f"{d['return_vs_nifty500']:+.2f}pp" if d['return_vs_nifty500'] is not None
               else "   vs Nifty500: not computed"),
            f"  last price {rec['price']['last']} as of {rec['price']['asof'] or '-'}",
            f"  dMS 1w {d['delta_ms_1w']}  dMS 2w {d['delta_ms_2w']}  "
            f"best MS {d['best_ms']} ({d['weeks_since_best']}w ago)",
            ]
    if d["flags"]:
        out.append("  worth knowing:")
        for b in d["flags"]:
            out.append(f"    - {b}")

    if rec["ms_history"]:
        out += ["", "MS HISTORY"]
        for e in rec["ms_history"][-8:]:
            out.append(f"  {e['asof']}  MS {e['ms']}")
    return "\n".join(out)


# ----------------------------------------------------------------------- cli

def _kvpairs(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"expected SYMBOL=VALUE, got '{it}'")
        k, v = it.split("=", 1)
        out[k.upper()] = float(v)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="Equity watchlist store (Phase 1)")
    ap.add_argument("--path", default=DEFAULT_PATH)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create an empty watchlist.json")

    p = sub.add_parser("add", help="add a name by hand (origin=user, USER tag)")
    p.add_argument("symbol")
    p.add_argument("--book", choices=BOOKS, default="momentum")
    p.add_argument("--price", type=float, help="price at the moment you decide to track")
    p.add_argument("--thesis")
    p.add_argument("--conviction")
    p.add_argument("--name")
    p.add_argument("--tags", help="comma separated")
    p.add_argument("--date-added")

    p = sub.add_parser("ingest", help="merge selected symbols from an L3 scan")
    p.add_argument("--json", required=True, help="stock_select.py --json output")
    p.add_argument("--symbols", help="comma separated")
    p.add_argument("--all", action="store_true",
                   help="capture the whole shortlist (= top 3 per sector)")
    p.add_argument("--book", choices=BOOKS, default="momentum")
    p.add_argument("--price", action="append", metavar="SYM=PRICE",
                   help="price at add, repeatable")

    p = sub.add_parser("list")
    p.add_argument("--book", choices=BOOKS)
    p.add_argument("--band")
    p.add_argument("--origin", choices=ORIGINS)
    p.add_argument("--archive", action="store_true")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("show")
    p.add_argument("symbol")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("note")
    p.add_argument("symbol")
    p.add_argument("text")

    p = sub.add_parser("set", help="edit human-zone fields only")
    p.add_argument("symbol")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--thesis")
    p.add_argument("--conviction")
    p.add_argument("--price-at-add", type=float, dest="price_at_add")

    p = sub.add_parser("adopt", help="decide to track a Surfaced name")
    p.add_argument("symbol")
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--thesis")
    p.add_argument("--conviction")

    p = sub.add_parser("archive")
    p.add_argument("symbol")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("restore")
    p.add_argument("symbol")

    p = sub.add_parser("prices", help="Lane A — refresh prices from the panel")
    p.add_argument("--panel", required=True)
    p.add_argument("--benchmark", help="date,close series for relative return")

    sub.add_parser("validate")

    a = ap.parse_args(argv)

    if a.cmd == "init":
        if os.path.exists(a.path):
            print(f"{a.path} already exists. Not overwriting.")
            return 1
        save(empty_store(), a.path)
        print(f"Created {a.path} (schema v{SCHEMA_VERSION}).")
        return 0

    data = load(a.path)

    if a.cmd == "add":
        tags = [t.strip() for t in a.tags.split(",")] if a.tags else None
        rec = add_manual(data, a.symbol, book=a.book, price=a.price,
                         thesis=a.thesis, conviction=a.conviction,
                         name=a.name, tags=tags, date_added=a.date_added)
        save(data, a.path)
        print(f"Added {rec['symbol']} to the {rec['book']} book.")
        if rec.get("readded_from_archive"):
            print(f"  Pulled back from the archive (archived {rec['readded_from_archive']}). "
                  f"{len(rec['ms_history'])} week(s) of MS history carried forward.")
        if a.price is None:
            print("  No price given, so return won't be tracked. "
                  "Add one later with `set SYMBOL --price-at-add N`.")
        return 0

    if a.cmd == "ingest":
        with open(a.json) as fh:
            scan = json.load(fh)
        if not a.all and not a.symbols:
            print("Give --symbols or --all."); return 1
        syms = [s.strip() for s in (a.symbols or "").split(",") if s.strip()]
        res = ingest(data, scan, syms, prices=_kvpairs(a.price), book=a.book,
                     capture_all=a.all)
        save(data, a.path)
        print(f"Scan as of {res['asof']} | regime {res['regime']}")
        if res["added"]:
            print(f"  surfaced: {', '.join(res['added'])}")
            print("  Adopt the ones you want to track: "
                  "`adopt SYMBOL --price N`")
        if res["updated"]:
            print(f"  updated: {', '.join(res['updated'])}")
        if res["not_in_scan"]:
            print(f"  ! not in this scan's shortlist: {', '.join(res['not_in_scan'])}")
            print("    Add by hand if you want them tracked — they'll carry the USER tag.")
        for s in res["added"] + res["updated"]:
            f = flags(find(data, s))
            gate_notes = [x for x in f if "gate" in x]
            if gate_notes:
                print(f"  {s}: {'; '.join(gate_notes)}")
        return 0

    if a.cmd == "list":
        recs = data["archive"] if a.archive else data["active"]
        if a.book:
            recs = [r for r in recs if r["book"] == a.book]
        if a.origin:
            recs = [r for r in recs if r["origin"] == a.origin]
        if a.band:
            recs = [r for r in recs if derive(r)["band"] == a.band]
        recs = sorted(recs, key=lambda r: (r["machine"]["ms"] is None,
                                           -(r["machine"]["ms"] or 0)))
        if a.json:
            print(json.dumps([r | {"derived": derive(r)} for r in recs], indent=2))
        else:
            print(render_table(recs))
            print(f"\n{len(recs)} name(s)."
                  f"  active {len(data['active'])} | archive {len(data['archive'])}")
        return 0

    if a.cmd == "show":
        rec = find(data, a.symbol) or find(data, a.symbol, "archive")
        if rec is None:
            print(f"{a.symbol.upper()} not found.")
            return 1
        print(json.dumps(rec | {"derived": derive(rec)}, indent=2)
              if a.json else render_detail(rec))
        return 0

    if a.cmd == "note":
        add_note(data, a.symbol, a.text)
        save(data, a.path)
        print(f"Note added to {a.symbol.upper()}.")
        return 0

    if a.cmd == "set":
        set_human(data, a.symbol, status=a.status, thesis=a.thesis,
                  conviction=a.conviction, price_at_add=a.price_at_add)
        save(data, a.path)
        print(f"Updated {a.symbol.upper()}.")
        return 0

    if a.cmd == "adopt":
        rec = adopt(data, a.symbol, a.price, a.thesis, a.conviction)
        save(data, a.path)
        print(f"Adopted {rec['symbol']} at {a.price}. Now tracking from today.")
        return 0

    if a.cmd == "archive":
        archive(data, a.symbol, a.reason)
        save(data, a.path)
        print(f"Archived {a.symbol.upper()} ({a.reason}). History retained.")
        return 0

    if a.cmd == "restore":
        restore(data, a.symbol)
        save(data, a.path)
        print(f"Restored {a.symbol.upper()} to the active list.")
        return 0

    if a.cmd == "prices":
        res = refresh_prices(data, a.panel, a.benchmark)
        save(data, a.path)
        print(f"Prices as of {res['asof']}"
              + (f" | benchmark {res['bench_asof']}" if res['bench_asof'] else ""))
        print(f"  updated {len(res['updated'])} name(s)")
        if res["not_in_panel"]:
            print(f"  ! not found in the panel: {', '.join(res['not_in_panel'])}")
        return 0

    if a.cmd == "validate":
        problems = validate(data)
        if not problems:
            print(f"OK. {len(data['active'])} active, {len(data['archive'])} archived.")
            return 0
        print(f"{len(problems)} problem(s):")
        for p_ in problems:
            print(f"  - {p_}")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
