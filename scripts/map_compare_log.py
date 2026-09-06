#!/usr/bin/env python3
"""Log v1 (Industry column) vs v2 (NSE index constituents) side by side, weekly.

Both maps are kept live. Neither is authoritative. Each week this records the
top-3 sectors each map selected and the top-3 constituents of each by unified
momentum rank, so the two can be compared on real forward returns as the
sample accumulates.

Stores selections only. Returns are recomputed from the panel at report time,
so nothing needs updating retrospectively.

  log:     python map_compare_log.py --unified all_YYYY-MM-DD.json \
               --v1-rank bs2/rank_YYYY-MM-DD.json --v2-rank v2/rank_YYYY-MM-DD.json \
               --out state/map_comparison.jsonl
  report:  python map_compare_log.py --report state/map_comparison.jsonl \
               --panel panel.csv.gz --benchmark nifty500.csv
"""
import pandas as pd, numpy as np, json, argparse, os

TOPS, TOPN = 3, 3


def log(a):
    uni = json.load(open(a.unified))
    sl = uni['shortlist']
    pos = {y['symbol']: i + 1 for i, y in enumerate(sl)}
    ext = {y['symbol']: ('EXTENDED' in y.get('entry', '')) for y in sl}
    week = uni.get('asof') or os.path.basename(a.unified)[4:-5]
    rec = dict(week=str(week), pool=len(sl), arms={})
    for arm, rankfile, mapfile in [('v1', a.v1_rank, a.v1_map), ('v2', a.v2_rank, a.v2_map)]:
        if not rankfile or not os.path.exists(rankfile):
            continue
        m = pd.read_csv(mapfile)
        mem = m.groupby('sector')['symbol'].apply(list).to_dict()
        rj = json.load(open(rankfile))
        top = sorted(rj['ranking'], key=lambda z: z['rank'])[:TOPS]
        secs = []
        for x in top:
            cand = [s for s in mem.get(x['sector'], []) if s in pos and not ext.get(s, False)]
            cand = sorted(cand, key=lambda z: pos[z])[:TOPN]
            secs.append(dict(sector=x['sector'], rank=x['rank'], sms=x['SMS'],
                             breadth=x.get('breadth_raw'),
                             picks=[dict(symbol=s, uni_rank=pos[s]) for s in cand]))
        rec['arms'][arm] = dict(n_sectors=len(rj['ranking']), top=secs,
                                selected=rj.get('selected', []))
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    seen = set()
    if os.path.exists(a.out):
        for ln in open(a.out):
            try:
                seen.add(json.loads(ln)['week'])
            except Exception:
                pass
    if rec['week'] in seen:
        print("%s already logged" % rec['week']); return
    with open(a.out, 'a') as f:
        f.write(json.dumps(rec) + "\n")
    print("%s logged. pool %d" % (rec['week'], rec['pool']))
    for arm, v in rec['arms'].items():
        print("  %-3s %d sectors | %s" % (arm, v['n_sectors'],
              " | ".join("%s(#%d) %s" % (s['sector'][:18], s['rank'],
                         ",".join("%s#%d" % (p['symbol'], p['uni_rank']) for p in s['picks']))
                         for s in v['top'])))


def report(a):
    rows = [json.loads(l) for l in open(a.report) if l.strip()]
    if not rows:
        print("nothing logged yet"); return
    p = pd.read_csv(a.panel); p['date'] = pd.to_datetime(p['date'])
    C = p.pivot_table(index='date', columns='symbol', values='close', aggfunc='last').sort_index()
    LO = p.pivot_table(index='date', columns='symbol', values='low', aggfunc='last').sort_index()
    n = pd.read_csv(a.benchmark); n['date'] = pd.to_datetime(n['date'])
    B = n.set_index('date')['close'].reindex(C.index).ffill()
    last = C.index.max()
    out = []
    for r in rows:
        t = pd.Timestamp(r['week'])
        if t not in C.index:
            continue
        for h, lab in [(28, '4w'), (84, '12w')]:
            end = t + pd.Timedelta(days=h)
            if end > last:
                continue
            j = C.index.searchsorted(end, side='right') - 1
            t1 = C.index[j]
            bm = (B.loc[t1] / B.loc[t] - 1) * 100
            for arm, v in r['arms'].items():
                for s in v['top']:
                    for pk in s['picks']:
                        sym = pk['symbol']
                        if sym not in C.columns:
                            continue
                        p0, p1 = C.loc[t, sym], C.loc[t1, sym]
                        if pd.isna(p0) or pd.isna(p1) or p0 <= 0:
                            continue
                        out.append(dict(week=r['week'], arm=arm, h=lab, sym=sym,
                                        uni_rank=pk['uni_rank'],
                                        ex=(p1 / p0 - 1) * 100 - bm,
                                        mae=(LO.loc[t:t1, sym].min() / p0 - 1) * 100))
    d = pd.DataFrame(out)
    if d.empty:
        print("logged %d week(s); none matured yet" % len(rows)); return
    print("MAP COMPARISON — %d weeks logged, %s to %s\n"
          % (len(rows), rows[0]['week'], rows[-1]['week']))
    for h in ['4w', '12w']:
        s = d[d.h == h]
        if s.empty:
            continue
        print("  horizon %s" % h)
        print("    %-4s %6s %8s %8s %6s %8s %8s" %
              ('arm', 'picks', 'excess', 'median', 'hit', 'MAE', 'medRank'))
        store = {}
        for arm in ['v1', 'v2']:
            x = s[s.arm == arm]
            if x.empty:
                continue
            store[arm] = x.groupby('week').ex.mean()
            print("    %-4s %6d %+8.2f %+8.2f %5.0f%% %+8.2f %8.0f" %
                  (arm, len(x), x.ex.mean(), x.ex.median(),
                   100 * (x.ex > 0).mean(), x.mae.mean(), x.uni_rank.median()))
        if len(store) == 2:
            j = pd.concat(store, axis=1).dropna()
            diff = j['v2'] - j['v1']
            t = (diff.mean() / (diff.std(ddof=1) / np.sqrt(len(diff)))
                 if len(diff) > 2 else np.nan)
            verdict = ("no separation" if abs(t) < 2 else
                       "v2 better" if t > 0 else "v1 better")
            print("    paired v2-v1 %+.2fpp  t=%+.2f  n=%d weeks  -> %s\n"
                  % (diff.mean(), t, len(diff), verdict))
    n4 = d[d.h == '4w'].week.nunique()
    print("  DECISION GATE: revisit at 80 matured 4-week weeks. Currently %d." % n4)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--unified'); ap.add_argument('--v1-rank'); ap.add_argument('--v2-rank')
    ap.add_argument('--v1-map', default='sector_map_v1.csv')
    ap.add_argument('--v2-map', default='sector_map_v2.csv')
    ap.add_argument('--out', default='state/map_comparison.jsonl')
    ap.add_argument('--report'); ap.add_argument('--panel'); ap.add_argument('--benchmark')
    a = ap.parse_args()
    report(a) if a.report else log(a)
