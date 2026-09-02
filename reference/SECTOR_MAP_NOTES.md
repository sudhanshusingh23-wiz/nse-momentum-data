# Sector map v1 — frozen 28 August 2026

`sector_map_v1.csv` is the fixed universe for `sector-momentum-rotation`.
**Do not edit it between weekly runs.** Revisions belong at the quarterly review only,
and every revision gets a row in the log at the bottom of this file.

## Why it is frozen

SMS is a cross-sectional percentile computed across whatever sectors are in the map.
Change the map and every score moves even when no underlying price has changed.

Observed directly on 1 Sep 2026: two runs on the *identical* 28 Aug close, differing only
in the map, put Healthcare at rank 1 (SMS 80.8) and rank 4 (SMS 75.6). Healthcare's raw
values were byte-identical in both — abs momentum 9.6%, risk-adjusted 1.03, breadth 59.8,
trend 20/20. Only the comparison set differed.

The consequence that matters: **hysteresis is meaningless across a map change.** An
entry-pending counter recorded on one universe cannot be confirmed against another.
"Defence entered the top 3" and "Defence was added to the map" would be indistinguishable,
and only one of those is a signal.

## Construction

**Base — NSE `Industry` column** of `reference/ind_nifty500list.csv`, which is maintained by
NSE and refreshes with every index rebalance. 19 industries, of which:

- `Diversified` (3 names) dropped — too few constituents for a meaningful breadth reading.
- `Financial Services` (101 names) split three ways: **PSU Bank**, **Private Bank**, and
  **Financial Services ex-Bank**. The combined bucket was averaging a strong ex-bank complex
  against two weak bank groups and hiding both (rank 14 combined; 58.2 / 39.2 / 34.4 split).
  Bank membership is hand-assigned in the builder and is the one part of the base map that
  is not NSE-maintained — recheck it at each quarterly review.

**Tier B additions** — 7 baskets for themes the Industry column does not isolate:
Defence, Railways & Wagons, Electronics Manufacturing, Renewables, Sugar & Agri-inputs,
Hospitality & Travel, Logistics.

Constituents come from the draft lists in the skill's `references/sector-map.md`. They were
verified only as **still listed and trading in the panel** — not as currently representative
of the theme. Treat these six sectors' scores as indicative until the lists are properly
verified. (Defence and Railways use official NSE indices for price; only their breadth
comes from the draft baskets.)

## Price series per sector

See `sector_index_map_v1.csv`. Official NSE index where one exists with ≥130 sessions;
equal-weighted constituent basket otherwise. Six sectors fall back to baskets because NSE
launched their indices recently and they carry only ~55 sessions: Capital Goods,
Construction, Consumer Services, Power, Sugar & Agri-inputs, Telecommunication.

**Capital Goods is the judgment call to revisit.** NIFTY CAPITAL GOODS (55 sessions) falls
back to the equal-weighted basket of all 63 constituents. Same sector, same date:
cap-weighted manufacturing reads +5.7% absolute momentum, equal-weighted capital goods reads
+14.6%. The mid-caps inside the sector have far outrun the large-cap-weighted index. For a
small/mid book the equal-weighted read is the more honest one, but it is a choice, not a
measurement. Once NSE's index accumulates 130 sessions (around Nov 2026) the script will
switch to it automatically — **that switch will move the ranking and is not a market event.**

## Known weaknesses

- Four sectors have <10 constituents, so their breadth is noisy by construction:
  Logistics (9), Renewables (8), Media (5), Textiles (5).
- Construction Materials has no trend score — NIFTY CEMENT carries 132 sessions, short of
  the 32 weekly bars trend integrity requires.
- `artifacts/breadth.json` flags ~21 symbols as likely unadjusted corporate actions, which
  mildly contaminates breadth across the map.
- Overlap is intentional and acceptable (Bharat Forge sits in both Defence and Capital
  Goods). Watch for the correlation guard firing on the same pair three weeks running —
  per the skill that is a map problem, not a market one.

## Revision log

| Date | Version | Change | Reason |
|---|---|---|---|
| 2026-08-28 | v1 | Initial frozen map. 28 sectors, 574 symbol-rows. sha256[:16] `5ee9450989ae9870` | Baseline |
