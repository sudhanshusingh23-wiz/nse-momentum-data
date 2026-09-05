#!/usr/bin/env python3
"""Log what the breadth floor blocked, so the question becomes answerable.

Run every Friday, straight after sector_score.py. Appends one record per
sector that ranked top-3 but was blocked by the breadth floor, together with
the three names you would have bought inside it. Stores selections only -
forward returns are computed later from the panel, so nothing needs updating.

  python breadth_log.py --sector-json sector.json --panel panel.csv.gz \
      --map sector_map_v1.csv --benchmark nifty500.csv \
      --out state/breadth_counterfactual.jsonl

Reporting on what has accumulated:
  python breadth_log.py --report state/breadth_counterfactual.jsonl \
      --panel panel.csv.gz --benchmark nifty500.csv
"""
import pandas as pd, numpy as np, json, argparse, os

ADV_MIN, PX_MIN, HIST_MIN, FLOOR = 5.0, 30.0, 253, 50.0


def frames(panel, bench):
    p = pd.read_csv(panel); p['date'] = pd.to_datetime(p['date'])
    close = p.pivot_table(index='date', columns='symbol', values='close', aggfunc='last').sort_index()
    turn = p.pivot_table(index='date', columns='symbol', values='turnover', aggfunc='last').sort_index()
    n = pd.read_csv(bench); n['date'] = pd.to_datetime(n['date'])
    b = n.set_index('date')['close'].reindex(close.index).ffill()
    adv = turn.rolling(45, min_periods=30).mean() / 100.0
    rel = (close / close.shift(126) - 1).sub(b / b.shift(126) - 1, axis=0)
    hist = close.notna().cumsum()
    return close, b, adv, rel, hist


def log(a):
    j = json.load(open(a.sector_json))
    asof = pd.Timestamp(j['asof'])
    close, bench, adv, rel, hist = frames(a.panel, a.benchmark)
    if asof not in close.index:
        raise SystemExit("as-of %s not in panel" % asof.date())
    smap = pd.read_csv(a.map)
    members = smap.groupby('sector')['symbol'].apply(list).to_dict()
    selected = set(j.get('selected', []))
    recs = []
    for x in j['ranking']:
        br = x.get('breadth_raw')
        if x['rank'] > 3 or br is None or br >= FLOOR or x['sector'] in selected:
            continue
        mem = [s for s in members.get(x['sector'], []) if s in close.columns]
        elig = [s for s in mem
                if adv.loc[asof, s] >= ADV_MIN and close.loc[asof, s] >= PX_MIN
                and hist.loc[asof, s] >= HIST_MIN and pd.notna(rel.loc[asof, s])]
        top3 = sorted(elig, key=lambda s: -rel.loc[asof, s])[:3]
        recs.append(dict(week=str(asof.date()), sector=x['sector'], rank=x['rank'],
                         sms=x['SMS'], breadth=round(float(br), 1),
                         names=top3,
                         entry={s: round(float(close.loc[asof, s]), 2) for s in top3},
                         bench=round(float(bench.loc[asof]), 2)))
    os.makedirs(os.path.dirname(a.out) or '.', exist_ok=True)
    seen = set()
    if os.path.exists(a.out):
        for ln in open(a.out):
            try:
                r = json.loads(ln); seen.add((r['week'], r['sector']))
            except Exception:
                pass
    n_new = 0
    with open(a.out, 'a') as f:
        for r in recs:
            if (r['week'], r['sector']) in seen:
                continue
            f.write(json.dumps(r) + "\n"); n_new += 1
    print("%s: %d sector(s) blocked on breadth, %d newly logged -> %s"
          % (asof.date(), len(recs), n_new, a.out))
    for r in recs:
        print("   %-28s rank %d  breadth %.1f  ->  %s"
              % (r['sector'], r['rank'], r['breadth'], ", ".join(r['names'])))


def report(a):
    rows = [json.loads(l) for l in open(a.report) if l.strip()]
    if not rows:
        print("nothing logged yet"); return
    close, bench, _, _, _ = frames(a.panel, a.benchmark)
    last = close.index.max()
    out = []
    for r in rows:
        t = pd.Timestamp(r['week'])
        for h, lab in [(28, 'r4w'), (84, 'r12w')]:
            end = t + pd.Timedelta(days=h)
            if end > last:
                continue
            j = close.index.searchsorted(end, side='right') - 1
            t1 = close.index[j]
            bm = (bench.loc[t1] / bench.loc[t] - 1) * 100
            rets = [(close.loc[t1, s] / r['entry'][s] - 1) * 100 - bm
                    for s in r['names'] if s in close.columns and pd.notna(close.loc[t1, s])]
            if rets:
                out.append(dict(week=r['week'], sector=r['sector'], breadth=r['breadth'],
                                horizon=lab, excess=float(np.mean(rets))))
    d = pd.DataFrame(out)
    if d.empty:
        print("logged %d record(s); none matured yet" % len(rows)); return
    print("BREADTH COUNTERFACTUAL — %d blocked sector-weeks logged" % len(rows))
    print("(excess return of the top-3 names inside each blocked sector)\n")
    for h in ['r4w', 'r12w']:
        s = d[d.horizon == h]
        if s.empty:
            continue
        t = s.excess.mean() / (s.excess.std(ddof=1) / np.sqrt(len(s))) if len(s) > 2 else np.nan
        print("  %-5s n=%3d  mean %+6.2f%%  median %+6.2f%%  hit %3.0f%%  t=%+5.2f"
              % (h, len(s), s.excess.mean(), s.excess.median(),
                 100 * (s.excess > 0).mean(), t))
    n4 = (d.horizon == 'r4w').sum()
    print("\n  DECISION GATE: revisit the floor at n>=60 matured 4-week observations.")
    print("  Currently %d. %s" % (n4, "READY TO REVISIT." if n4 >= 60 else
                                  "Keep logging - roughly %d weeks to go at ~0.5/week."
                                  % max(0, int((60 - n4) / 0.5))))


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--sector-json'); ap.add_argument('--panel', required=True)
    ap.add_argument('--map'); ap.add_argument('--benchmark', required=True)
    ap.add_argument('--out', default='state/breadth_counterfactual.jsonl')
    ap.add_argument('--report')
    a = ap.parse_args()
    report(a) if a.report else log(a)
