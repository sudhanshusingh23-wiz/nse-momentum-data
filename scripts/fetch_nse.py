#!/usr/bin/env python3
"""
fetch_nse.py — download one trading day of NSE end-of-day data.

Fetches:
  sec_bhavdata_full_DDMMYYYY.csv  -> data/       (prices, volume, DELIV_PER)
  ind_close_all_DDMMYYYY.csv      -> indices/    (Nifty 500, Smallcap 250, Nifty 50,
                                                  India VIX, all sector indices)
  ind_nifty500list.csv            -> reference/  (constituents; refreshed weekly)

NSE moves its file paths periodically, so every download tries a list of candidate
URLs and validates that what came back is actually a parseable CSV with the expected
columns. A silent 404 that leaves a stale panel in place is worse than a loud failure,
so an unrecoverable fetch exits non-zero and turns the workflow red.

Usage:
  python scripts/fetch_nse.py                 # today (IST)
  python scripts/fetch_nse.py --date 28-08-2026
  python scripts/fetch_nse.py --backfill 30   # last 30 calendar days, skips existing
"""

import argparse, io, os, sys, time
from datetime import datetime, timedelta, timezone

import requests

IST = timezone(timedelta(hours=5, minutes=30))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"),
    "Accept": "text/csv,application/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "DNT": "1",
}

# Candidates are tried in order. archives.nseindia.com is the host the upstream
# project uses successfully from GitHub Actions runners; the others are fallbacks.
BHAV_URLS = [
    "https://archives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv",
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{d}.csv",
]
LIST_HOSTS_NOTE = "Both hosts serve the same files; keep both as fallbacks."
# nsearchives is the host currently serving these; archives kept as a fallback.
INDEX_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_close_all_{d}.csv",
    "https://archives.nseindia.com/content/indices/ind_close_all_{d}.csv",
]
# Constituent lists. The three cap lists give SEBI cap classification exactly:
# Nifty 100 = LARGE (ranks 1-100), Midcap 150 = MID (101-250),
# Smallcap 250 = SMALL (251-500). They sum to the Nifty 500 with no gaps, so no
# market-cap data or share counts are needed anywhere in the system.
CONSTITUENT_LISTS = {
    "ind_nifty500list.csv":        ["Symbol", "Industry"],
    "ind_nifty100list.csv":        ["Symbol"],
    "ind_niftymidcap150list.csv":  ["Symbol"],
    "ind_niftysmallcap250list.csv": ["Symbol"],
}
LIST_URLS = [
    "https://archives.nseindia.com/content/indices/{name}",
    "https://nsearchives.nseindia.com/content/indices/{name}",
]


def looks_like_csv(text, must_have, min_rows):
    """Reject HTML error pages, empty files, and truncated downloads."""
    if not text or len(text) < 200:
        return False, "response too short (%d bytes)" % len(text or "")
    head = text[:400].lower()
    if "<html" in head or "<!doctype" in head:
        return False, "got an HTML page, not a CSV (path has probably moved)"
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < min_rows:
        return False, "only %d rows, expected at least %d" % (len(lines), min_rows)
    # Normalise BOTH sides. Uppercasing only the header silently rejects any
    # expected column written in mixed case, which is how the index file was
    # being discarded even though the download succeeded.
    def norm(x):
        return x.upper().replace(" ", "").replace("_", "").replace("-", "")
    header = norm(lines[0])
    missing = [c for c in must_have if norm(c) not in header]
    if missing:
        return False, "header missing expected column(s): %s" % ", ".join(missing)
    return True, "ok"


def fetch(urls, dstr, must_have, min_rows, label, session):
    tried = []
    for tmpl in urls:
        url = tmpl.format(d=dstr)
        for attempt in range(3):
            try:
                r = session.get(url, headers=HEADERS, timeout=60)
            except Exception as e:
                tried.append("%s -> %s" % (url, e))
                time.sleep(3)
                continue
            if r.status_code == 404:
                tried.append("%s -> 404" % url)
                break
            if r.status_code != 200:
                tried.append("%s -> HTTP %d" % (url, r.status_code))
                time.sleep(5)
                continue
            ok, why = looks_like_csv(r.text, must_have, min_rows)
            if ok:
                print("  %-10s OK  %s" % (label, url))
                return r.text
            tried.append("%s -> %s" % (url, why))
            break
    print("  %-10s FAILED" % label)
    for t in tried:
        print("      " + t)
    return None


def warm_up(session):
    """NSE sets cookies on the homepage; some paths need them."""
    try:
        session.get("https://www.nseindia.com", headers=HEADERS, timeout=30)
    except Exception:
        pass


def save(text, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print("      saved %s (%d KB)" % (os.path.relpath(path, ROOT), len(text) // 1024))


def do_date(d, session, force=False):
    dstr = d.strftime("%d%m%Y")
    if d.weekday() >= 5:
        print("%s is a weekend, skipping" % d.strftime("%d-%b-%Y"))
        return None
    print("%s" % d.strftime("%d-%b-%Y"))

    bhav_path = os.path.join(ROOT, "data", "sec_bhavdata_full_%s.csv" % dstr)
    idx_path = os.path.join(ROOT, "indices", "ind_close_all_%s.csv" % dstr)
    got_any = False

    if force or not os.path.exists(bhav_path):
        txt = fetch(BHAV_URLS, dstr, ["SYMBOL", "CLOSE_PRICE", "PREV_CLOSE", "DELIV_PER"],
                    500, "bhavcopy", session)
        if txt:
            save(txt, bhav_path)
            got_any = True
    else:
        print("  bhavcopy   already present")
        got_any = True

    if force or not os.path.exists(idx_path):
        txt = fetch(INDEX_URLS, dstr, ["Index Name", "Closing Index Value"],
                    30, "indices", session)
        if txt:
            save(txt, idx_path)
            got_any = True
    else:
        print("  indices    already present")
        got_any = True

    return got_any


def refresh_constituents(session):
    """Refresh the Nifty 500 list plus the three cap-tier lists, weekly."""
    print("constituent lists")
    for name, cols in CONSTITUENT_LISTS.items():
        path = os.path.join(ROOT, "reference", name)
        if os.path.exists(path):
            age = (time.time() - os.path.getmtime(path)) / 86400
            if age < 7:
                print("  %-32s %.1f days old, skipping" % (name, age))
                continue
        min_rows = 400 if "500" in name else 80
        urls = [u.replace("{name}", name) for u in LIST_URLS]
        txt = fetch(urls, "", cols, min_rows, name.replace("ind_", "")[:10], session)
        if txt:
            save(txt, path)
        elif os.path.exists(path):
            print("  WARNING: %s refresh failed; keeping the existing copy." % name)
        else:
            print("  WARNING: %s could not be fetched and no copy exists. Cap "
                  "classification will be unavailable and the small/mid tilt "
                  "will not be applied." % name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="DD-MM-YYYY. Defaults to today in IST.")
    ap.add_argument("--backfill", type=int, default=0,
                    help="Also fetch the last N calendar days, skipping files already present.")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--allow-empty", action="store_true",
                    help="Exit 0 even if nothing was downloaded (useful on holidays).")
    a = ap.parse_args()

    target = (datetime.strptime(a.date, "%d-%m-%Y").date() if a.date
              else datetime.now(IST).date())

    session = requests.Session()
    warm_up(session)

    refresh_constituents(session)

    dates = [target]
    if a.backfill:
        dates = [target - timedelta(days=i) for i in range(a.backfill, -1, -1)]

    results = [do_date(d, session, a.force) for d in dates]
    got = [r for r in results if r]

    if not got:
        msg = ("No files downloaded. Either it was a market holiday, or NSE changed its "
               "file paths again. Check the URLs printed above before assuming the market "
               "was closed.")
        print("\n" + msg)
        sys.exit(0 if a.allow_empty else 1)
    print("\nDone: %d date(s) with data." % len(got))


if __name__ == "__main__":
    main()
