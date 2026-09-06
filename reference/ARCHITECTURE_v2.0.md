# Indian Equity Momentum System — Architecture v2.0

**Baseline date:** 4 September 2026
**Status:** First full end-to-end run completed. Zero positions taken. Zero trades closed. No backtest run.
**Purpose:** Fix what exists so that future changes are deliberate, attributable and measurable.

---

## 1. What this system is

A weekly, rules-based momentum book for Indian equities, tilted to small and mid caps. It answers three questions in strict order:

1. **Should we be deployed at all, and how much?** (regime)
2. **Where should capital go?** (sector rotation)
3. **Which names inside that, at what size, entered how?** (stock selection, sizing, entry)

The ordering is the design. A strong stock in a rejected sector does not get bought. A strong sector in a RISK-OFF regime does not get funded. Each layer can only *reduce* what the layer above permits — never expand it.

**Cadence:** decisions are made on Friday closes. Mid-week reads are permitted but are strictly read-only.

**Pool:** not a fixed constant. All outputs are computed against a ₹1,00,00,000 reference base so every rupee figure reads directly as a percentage, and are scaled to actual capital at execution time. Deployment is further capped by the regime.

---

## 2. Algorithm and approach

### 2.1 Data foundation

Everything derives from NSE bhavcopy and index files, assembled daily into a GitHub repository. No vendor feed, no intraday data, no fundamentals in the core loop.

| Input | Content | Coverage at v1 |
|---|---|---|
| `panel.csv.gz` | date, symbol, close, high, low, turnover, delivery_pct | 495 sessions, 2,998 symbols, from Sep 2024 |
| `sector_indices.csv` | daily closes for NSE sector indices | ~60 index series |
| `nifty500 / smallcap250 / nifty50 / vix` | benchmark closes | 477 rows each |
| `breadth.json` | point-in-time breadth snapshot | current date only |
| `caps.csv` | symbol → LARGE / MID / SMALL | Nifty 500 only (500 names) |
| `ind_nifty500list.csv` | NSE constituent list with `Industry` column | 500 names, 20 industries |

**Delivery percentage is the differentiating input.** It is specific to Indian markets and separates genuine accumulation from intraday churn. It appears in the stock score and in a hard gate.

### 2.2 Layer 1 — Regime (the gate)

A 0–100 composite scored on the Nifty 500, converted into a **mandate** rather than an opinion.

| Component | Weight | Measures |
|---|---|---|
| Index trend | 25% | Position vs 200 DMA + slope of that average |
| Participation | 25% | % of Nifty 500 above their own 200 DMA |
| Thrust | 15% | % above 50 DMA, and its 10-session change |
| Smallcap health | 15% | Smallcap 250 / Nifty 50 ratio vs its own 50 DMA |
| Volatility | 10% | India VIX level and 20-session trend |
| Flow | 10% | FII net flows — **currently unavailable, neutralized, weights renormalize to 0.90** |

**States:** score ≥ 65 → RISK-ON (subject to a gate requiring close > 200 DMA *and* pct200 > 55); 45–64 → NEUTRAL; < 45 → RISK-OFF. Floor override: pct200 < 40, or Nifty 500 below its 200 DMA on five consecutive closes, forces RISK-OFF minimum. SHOCK locks for 10 sessions.

**Mandate at NEUTRAL** (the state throughout v1 testing): 60% max deployment, 5–7 positions, mid-cap favoured with max 2 smallcap positions, tranches 1 and 2 only, 3.5% heat cap.

**Transition rules** are asymmetric — fast to de-risk, slow to re-risk. Three consecutive closes to upgrade; five-session minimum before leaving a lower state. *These are not currently being applied — see §7.*

A **PARTIAL** read (any component missing) may justify de-risking but never an increase in deployment.

### 2.3 Layer 2 — Sector rotation

All 28 sectors are scored every week. Nothing is pre-filtered.

**Universe construction (frozen as `sector_map_v1.csv`):**
- 21 Tier A sectors from NSE's `Industry` column, with `Diversified` dropped (3 names) and `Financial Services` split into PSU Bank / Private Bank / Financial Services ex-Bank
- 7 Tier B baskets for themes the Industry column cannot isolate: Defence, Railways & Wagons, Electronics Manufacturing, Renewables, Sugar & Agri-inputs, Hospitality & Travel, Logistics
- 574 symbol-rows, 533 unique symbols

**Price series:** official NSE index where one exists with ≥130 sessions (18 sectors); equal-weighted constituent basket otherwise (10 sectors).

**Sector Momentum Score (SMS)** — each component computed raw, then **percentile-ranked across all 28 sectors**, then weighted:

| # | Component | Weight | Definition |
|---|---|---|---|
| 1 | Absolute momentum | 30% | `0.25·R21 + 0.35·R63 + 0.30·R126 + 0.10·R252` |
| 2 | Risk-adjusted momentum | 20% | R126 ÷ annualised stdev over 126 sessions (stdev floored at 5%) |
| 3 | Breadth | 20% | `0.40·(%>50DMA) + 0.30·(%>200DMA) + 0.30·(% within 10% of 52wH)` |
| 4 | Trend integrity | 15% | Weekly structure test, needs ≥32 weekly bars |
| 5 | Acceleration | 10% | Change in 3-month rank |
| 6 | Participation | 5% | Volume/turnover concentration |

The four-horizon blend in component 1 centres on 3–6 months. R21 is included at reduced weight because for a 2–3 month hold the recent month carries information about whether the trend is live; R252 gets only 10% because a year-old move is largely priced.

**Percentile ranking is the key design choice and the key risk.** It makes the score mean "how does this compare to the alternatives available right now," which is the actual question since capital must go somewhere. The consequence: **in a broad decline, something still scores 90.** That is precisely why L1 sits above L2.

**Hysteresis** prevents churn on noise:
- Enter: rank ≤ 3 by SMS for **2 consecutive weekly scores**
- Exit: rank > 5
- Absolute gates: SMS ≥ 60 and breadth ≥ 50, regardless of rank

**Correlation guard:** pairwise correlation of daily returns over the trailing 12 weeks; ejects a sector if three slots are secretly one bet. If the same pair collapses three weeks running, that is a map defect, not a market event, and becomes a quarterly-review item.

**Allocation:** weights proportional to SMS across selected sectors, within a 25–45% band for three sectors, widening to 25–50% when only two qualify.

### 2.4 Layer 3 — Stock selection

Universe is every constituent of the confirmed sectors. Hard gates run **first**, as pass/fail. Survivors are then scored.

**Momentum Score (MS)** — percentile-ranked within the candidate pool:

| # | Component | Weight |
|---|---|---|
| 1 | Relative strength | 28% (0.30·RS1M + 0.40·RS3M + 0.30·RS6M) |
| 2 | Trend structure | 18% (scored /20) |
| 3 | Momentum quality | 14% (pain index + positive-day ratio) |
| 4 | Volume and participation | 14% (volume ratio + delivery %) |
| 5 | Acceleration | 10% |
| 6 | Earnings momentum | 10% — **unavailable, neutralized, weights renormalize to 0.90** |
| 7 | Entry proximity | 6% (penalises distance from an ideal 0.5 ATR extension) |

**Cap-tier bonus** added after: NEUTRAL gives SMALL +3, MID +4, LARGE 0. RISK-ON gives SMALL +7. RISK-OFF and SHOCK give zero.

**Composite** = `0.65·MS + 0.35·SMS`, mapped to tiers A (>75) / B (62–75) / C (55–62). A name with no sector score gets no composite and no tier — correctly, since it has passed no rotation test.

**Extension check:** above 2.5 ATR from reference, a name is marked EXTENDED and is not actionable regardless of score.

**Composition rule:** ≥55% of the shortlist must be small or mid cap.

### 2.5 The sector-agnostic watchlist (added 4 Sep 2026)

Because the map covers only 533 of 2,998 panel symbols, a parallel pass runs L3 on everything **outside** the map that clears ADV ≥ ₹5 cr, price ≥ ₹30, and one year of history — 621 names at v1.

This is a **discovery feed, not a signal.** It runs as a separate invocation against separate files; the frozen map is never touched, so SMS percentiles and hysteresis are unaffected. Output carries no composite and no tier.

**MS from this pass is not comparable to the sector pass** — percentiles are computed within a 465-name pool versus 8–14. The watchlist names are on a harder curve.

---

## 3. The twelve hard gates

Gates are pass/fail and run before scoring. A FAIL removes the name. An **UNVERIFIED** is treated as distinct from PASS and blocks funding — the name may be shortlisted for research but must not be sized.

| Gate | Test | Source | Computable from panel? |
|---|---|---|---|
| G1 | Liquidity: ADV ≥ floor (₹5 cr default) | panel | ✅ |
| G2 | Max position ≤ 8% of ADV | panel | ✅ |
| G3 | Not in ASM stage 2+, GSM or ESM | NSE surveillance lists | ❌ |
| G4 | Promoter pledge < 25% | Shareholding pattern | ❌ |
| G5 | Promoter holding not down > 5pp in 2 quarters | Shareholding pattern | ❌ |
| G6 | Revenue growth ≥ 0% in one of last 2 quarters | Quarterly financials | ❌ |
| G7 | Operating margin not down > 400bps YoY twice running | Quarterly financials | ❌ |
| G8 | No auditor resignation, non-compliance or SEBI action in 12 months | Filings and news | ❌ |
| G9 | Circuit behaviour acceptable | panel | ✅ |
| G10 | Results not due within 3 sessions of entry | Corporate calendar | ❌ |
| G11 | Price ≥ ₹30 | panel | ✅ |
| G12 | Delivery ≥ 30% (SMALL) / 25% (MID, LARGE) | panel | ✅ |

**Why UNVERIFIED blocks rather than passes.** G3–G8 exist to catch the case where a chart looks superb *precisely because* something is wrong underneath — a pledge-funded ramp, a pre-announcement move, an operator-driven run in a name heading for surveillance. Those are the situations where a price-only system is most confident and most wrong. Treating "couldn't check" as "fine" inverts the gate's purpose.

**The intended workflow is score-first, verify-last.** Checking seven gates on ninety candidates is impossible; checking them on the two or three you would actually buy is a ten-minute job. G3 is the highest-value single check — ASM stage 2 forces trade-for-trade settlement and you discover it when you try to exit.

---

## 4. Position construction

Four layers convert a selected name into an order. Each can veto.

### 4.1 L4 — Target ladder (where you exit)

Three independent estimators produce a target anchor:
- **Volatility projection** — ATR × horizon × momentum strength multiplier
- **Measured move** — base height projected from the pivot
- **Structural** — nearest overhead resistance or 52-week high

The **median** is the anchor. **Dispersion** across the three is reported: below ~1.35 is agreement; above ~1.45 is DISPERSED, meaning a high reward:risk figure is inflated by estimator disagreement rather than genuine upside.

**Stop:** ATR-based, capped at 12% of entry. R = entry − stop.

**Reward:risk to anchor** must clear 2:1. Below that: MARGINAL or REJECT. The permitted fixes are *wait for a better entry* or *skip* — **never widen or tighten the stop to manufacture the ratio.** That is a sizing and timing problem, not a stop problem.

**Ladder:** T1 at +2R (book 25%), T2 at +4R (25%), T3 at +7R (25%), runner 25% on a trailing stop (10-week WMA or 3.5×ATR chandelier).

**Stop ladder:** breakeven at +1R → +0.5R after T1 → chandelier after T2. Stops move up only.

**Other exits:** time stop at 25 sessions if below +0.5R and momentum decayed; momentum decay exit at MS < 42 (trim 33% at MS 42–55); failed-breakout exit below pivot within 3 sessions on 1.2× volume; disaster stop at −18% intraday.

### 4.2 L5 — Position sizing (how much you own)

Size is the **minimum** of five constraints, and the binding one is always named:

| Constraint | Rule |
|---|---|
| Risk budget | Tier A 1.00% of pool, B 0.80%, C 0.55% (concentrated mode: A 1.20%) |
| Max position | 15% of pool (18% concentrated) |
| Liquidity | ≤ 8% of ADV |
| Heat headroom | Portfolio heat cap by regime (3.5% at NEUTRAL) |
| Sector cap | 40% of pool per sector |

In every v1 test the **risk budget bound first**, because ATR on these names runs 5–6.5% of price, pushing the stop to the 12% cap. High volatility is therefore priced through *size*, which is the correct mechanism.

### 4.3 L6 — Entry ladder (at what price, on what condition)

Classifies the setup into one of four types — range breakout, pullback in uptrend, momentum continuation, post-earnings continuation — and refuses if none fits. **"No setup" is a valid and frequent answer; a classification must not be manufactured to justify an entry.**

Tranches: T1 40% on trigger with a maximum-chase limit, T2 35% on confirmation, T3 25% on a press — **T3 withheld entirely at NEUTRAL.**

### 4.4 L7 / L8 — Rebalance and risk veto

L7 evaluates every holding against a decision grid (exit / trim / hold / add / promote to CORE) with stability vetting, and produces a Monday order plan. **L8 has veto authority over everything upstream** and governs portfolio heat, deployment, concentration, drawdown circuit breakers, correlation clusters and event exposure. L7 proposes; L8 disposes.

### 4.5 L9 — Trade journal

Post-trade forensics: expectancy in R, win rate, payoff ratio, profit factor, MFE/MAE, slippage, attribution by sector and cap tier. **Never exercised — no closed trades exist.**

---

## 5. Software architecture

### 5.1 Two systems, deliberately separate

| System | Holds | Updated by |
|---|---|---|
| **GitHub repo** `nse-momentum-data` | Data, reference files, state | Daily GitHub Action + manual commits |
| **Claude Skills** | All logic — SKILL.md, references, scripts | Manual upload via Settings → Capabilities → Skills |

**No skill ever goes in the repo. No data ever goes in a skill.** Duplicating either across both systems is how the two copies drift.

### 5.2 Repository layout

| Path | Contents | Refresh |
|---|---|---|
| `artifacts/manifest.json` | Build metadata, `latest_session`, warnings | Daily ~19:08 IST |
| `artifacts/panel.csv.gz` | Master price panel, all symbols | Daily |
| `artifacts/sector_indices.csv` | NSE sector index closes | Daily |
| `artifacts/breadth.json` | Breadth snapshot (current date only) | Daily |
| `artifacts/caps.csv` | Cap-tier classification | Daily (Nifty 500 only) |
| `artifacts/nifty500.csv`, `smallcap250.csv`, `nifty50.csv`, `vix.csv` | Benchmark series | Daily |
| `reference/ind_nifty500list.csv` | NSE constituent list with Industry | On NSE rebalance |
| `reference/sector_map_v1.csv` | **Frozen** universe: symbol, sector, tier | Quarterly only |
| `reference/sector_index_map_v1.csv` | Sector → index mapping and fallback reasons | Quarterly only |
| `reference/SECTOR_MAP_NOTES.md` | Construction rules, weaknesses, revision log | Quarterly only |
| `state/sector_latest.json` | Prior week ranks, selections, entry-pending counters | Weekly (Friday) |
| `state/sector_state_YYYY-MM-DD.json` | Dated audit trail | Weekly, never overwritten |
| `state/regime_latest.json` | Prior regime state for transition smoothing | Weekly — **not yet present** |

### 5.3 Skills and scripts

| Layer | Skill | Script | Key inputs |
|---|---|---|---|
| L1 | `market-regime-monitor` | `regime_score.py` | benchmark, breadth-json *or* universe, smallcap, largecap, vix, state |
| L2 | `sector-momentum-rotation` | `sector_score.py` | prices, map, sector-index, state, regime |
| L3 | `momentum-stock-selector` | `stock_select.py` | panel, map, benchmark, sectors, sms, caps, regime, top-n, show-n |
| L4 | `target-ladder-builder` | `target_ladder.py` | panel, symbol, tier, entry, ms-now |
| L5 | `position-sizer` | `position_size.py` | from-ladder, tier, adv-cr, pool, regime, cap-tier |
| L6 | `entry-ladder-planner` | `entry_plan.py` | panel, symbol, shares, tier, regime |
| L7 | `weekly-rebalance-engine` | `rebalance.py` | positions, scores, sectors, regime, pool |
| L8 | `portfolio-risk-monitor` | `risk_monitor.py` | positions, panel, pending, pool, regime |
| L9 | `trade-journal-analytics` | `trade_journal.py` | closed trades |
| — | `momentum-backtest-harness` | `backtest.py` | full history — **never run** |

Research skills (`stock-scorecard`, `fundamental-deep-dive`, `earnings-analyzer`, `quick-technical-analysis`, `indian-equity-momentum-scan`, `live-market-screener`) sit **outside** the loop. They are for second opinions on a single named stock. **Their output never overrides the chain.**

### 5.4 Runtime transformations

Applied at run time, not stored, and stated in output each time:

1. **`volume = turnover / close`** — the panel carries turnover, the scorer expects volume
2. **Sector index series** built by mapping `sector_indices.csv` through `sector_index_map_v1.csv`
3. **Caps extension** — any map symbol not in the Nifty 500 is classified **SMALL**, since NSE ranks 1–100 LARGE, 101–250 MID, 251–500 SMALL, so anything outside is smaller than the 500th name. Verified deterministic: all 36 unclassified names are outside the index.
4. **Watchlist universe** — panel symbols outside the map passing ADV/price/history filters

### 5.5 Three entry points by intent

| Intent | Layers | Needs | Writes state? |
|---|---|---|---|
| **Market read** | L1 → L2 → L3 | Nothing | **Friday only** |
| **Trade plan** | L4 → L5 → L6 | A chosen symbol, pool | No |
| **Book** | L7 → L8 → L9 | `positions.csv` | No |

**The one rule that matters: only the Friday market read writes state.** Advancing entry-pending counters on a non-Friday manufactures a false confirmation, because the hysteresis rule is *two consecutive weekly closes*, not *two scores*.

### 5.6 Weekly operating sequence

1. **~19:10 IST Friday** — confirm `manifest.json` shows `latest_session` = today. If not, wait. **Never score a stale session.**
2. Fetch: panel, sector_indices, breadth, caps, four benchmarks, n500 list, frozen map, index map, both state files
3. Stage: build prices.csv, sector_index.csv, caps_full.csv, watchlist universe
4. Run L1 → L2 with prior state
5. Run L3 on confirmed sectors (top 10) + watchlist pass (top 10), kept visually separate
6. Verify gates manually on the two or three genuine candidates
7. Run L4 → L5 → L6 on anything that clears
8. Run L7 → L8 on the book
9. Commit new `sector_state_YYYY-MM-DD.json` and `sector_latest.json`

---

## 6. Limitations and trade-offs

Honest accounting of what this system cannot do, and what was traded away to get what it does.

### 6.1 Structural limitations

**The map sees 18% of the market.** 533 symbols out of 2,998. Of the 2,464 outside, **623 clear the system's own liquidity floor and 154 currently pass a momentum screen.** Names like DIACABS (₹321 cr ADV, +159% in six months) and HAPPYFORGE (₹20,877 cr market cap, +136% in a year) are invisible to the sector chain — not because they failed a gate, but because no sector contains them. For a strategy explicitly aimed at small and mid caps, excluding the smallest 2,400 listed companies is a serious contradiction. The watchlist is a mitigation, not a fix.

**Tier B baskets are hand-written and unverified.** Constituents came from a draft list and were checked only for being listed and trading — not for still representing their theme. Six sectors rest on this: EMS, Renewables, Railways, Sugar, Hospitality, Logistics. **Both currently confirmed sectors depend on it**, and the top two names in the entire scan (CYIENTDLM, AVALON) are visible only because someone typed "Electronics Manufacturing" into a basket. That is finding good names by luck of the draft, not by design.

**Percentile ranking guarantees a winner.** SMS is relative. Something always scores 90, including in a broad decline. L1 is the only thing preventing that being read as a buy signal, and L1 is itself running degraded.

**Thin baskets make breadth noisy.** Four sectors have fewer than 10 constituents (Media 5, Textiles 5, Renewables 8, Logistics 9). At 10 names, one stock crossing its 50 DMA moves breadth by 10 points and can flip a sector across the 50 entry gate.

**Equal weighting is a choice doing real work.** Capital Goods reads +14.6% equal-weighted versus +5.7% cap-weighted; EMS is carried by three names out of ten. For a small/mid book the equal-weighted lens is more honest, but it is a lens, not a measurement.

### 6.2 Data gaps

| Gap | Effect | Severity |
|---|---|---|
| **FII flow missing** | 10% of regime score neutralized, every read is PARTIAL | Medium |
| **Earnings momentum missing** | 10% of MS neutralized | Medium |
| **7 of 12 gates need manual lookup** | Nothing is fundable without human work | **High** |
| **`regime_latest.json` absent** | No transition smoothing; regime can upgrade without confirmation | Medium |
| **`caps.csv` Nifty 500 only** | Mitigated by the outside-500 → SMALL rule, but AVALON proved the exception: it is genuinely MID (₹12–14.6k cr) and was under-scored | Low |
| **21 symbols (4.2%) flagged unadjusted for corporate actions** | Contaminates breadth, which is 20% of SMS and 40% of regime | Medium |
| **`breadth.json` is a snapshot, not a series** | Past dates must be re-derived from the panel with `--universe` | Low |

### 6.3 Validation deficit — the most serious item

**The backtest has never been run.** Expectancy, drawdown distribution, churn rate, parameter sensitivity and walk-forward performance are all unknown. Every parameter in this document — the 2:1 reward:risk floor, the 12% stop cap, the 2-week hysteresis, the 30/20/20/15/10/5 SMS weights, the 8%-of-ADV cap — is the skill author's starting hypothesis, explicitly labelled as needing a plateau test.

**Implication:** size the first cycle as tuition, not investment. The backtest is optional before a small first position and mandatory before scaling.

### 6.4 Deliberate trade-offs

| Chose | Over | Why | Cost |
|---|---|---|---|
| Weekly cadence | Daily | Reduces noise and churn | Slower to react to breaks |
| Percentile ranking | Absolute thresholds | Comparable across market conditions | Always produces a leader |
| Hysteresis (2 weeks) | Immediate entry | Prevents whipsaw | **Structurally always one week late** |
| UNVERIFIED blocks | Assume-pass | Catches the pledge-funded ramp | Manual work every week |
| Sector-first | Stock-first | Rides the dominant flow | Misses strong stocks in weak sectors |
| Nifty 500 base | Full universe | Liquidity and classification available | Misses 82% of listed names |
| Equal-weight baskets | Cap-weighted | Suits a small/mid book | Three names can carry a sector |

### 6.5 Operational fragility

- **Manual state upload** is the weakest link. If a state file is missed, hysteresis silently restarts. This has already failed once (`regime_latest.json`).
- **Manual map construction caused a real incident.** On 1 Sep, two runs on identical data ranked Healthcare 1st and 4th purely because the map differed. The freeze fixed L2; L3 onward is not yet equivalently protected.
- **A known future discontinuity:** six NSE sector indices cross the 130-session threshold around November 2026 and the scripts will switch from baskets to official indices automatically. Given the +14.6% vs +5.7% Capital Goods gap, that will visibly reshuffle the ranking and **will look like a market event without being one.**

---

## 7. Improvement roadmap

Ordered by risk retired per unit of effort.

### Tier 1 — before or alongside first capital

| # | Item | Effort | Retires |
|---|---|---|---|
| 1 | Commit `state/regime_latest.json` | Minutes | Regime upgrading without confirmation |
| 2 | Extend `caps.csv` past the Nifty 500 in `build_artifacts.py` | One line | Under-scoring of Tier B names |
| 3 | Add NSE surveillance lists to the daily Action → **G3 computable** | Small | The gate most likely to trap capital |
| 4 | Add NSE corporate calendar → **G10 computable** | Small | Avoidable earnings-gap risk |
| 5 | Fix corporate-action adjustment for the 21 flagged symbols | Small | Breadth contamination across both layers |

### Tier 2 — before scaling beyond a token position

| # | Item | Effort | Retires |
|---|---|---|---|
| 6 | **Run the backtest.** Walk-forward, ablation, parameter plateaus | Large | Total ignorance of expectancy and drawdown |
| 7 | Verify Tier B basket constituents against current theme membership | Medium | Both confirmed sectors rest on unverified lists |
| 8 | Extend the Action to write state files automatically | Medium | Manual-upload fragility; removes Claude as a state dependency |
| 9 | Freeze L3+ inputs the way L2 was frozen (`caps`, surveillance list) | Small | Repeat of the 1 Sep map-divergence incident |

### Tier 3 — the structural fix

| # | Item | Effort | Retires |
|---|---|---|---|
| 10 | **Full-universe sector classification.** Third-party industry tags for ~620 liquid names outside the Nifty 500 → `sector_map_v2.csv` | Large | The 82% blind spot — the single largest weakness |
| 11 | Quarterly shareholding ingest → G4, G5 computable | Medium | Two more manual gates |
| 12 | Screener financials ingest → G6, G7 computable **and un-neutralizes earnings momentum** | Medium | Two gates plus 10% of MS |
| 13 | FII flow ingest | Medium | 10% of regime; ends PARTIAL reads |

G8 (governance) stays manual. It is a news-scan problem, rarely binding, and two minutes per stock before capital is cheap.

### 7.1 How to know if this is working

Track these from the first position, not retrospectively:

- **Weekly watchlist snapshots.** Save the sector-agnostic list each week. In 6–8 weeks it answers the architectural question empirically: **do sector-gated picks outperform sector-agnostic ones?** If they do not, L2 is costing opportunities rather than filtering risk, and the whole premise needs revisiting.
- **Sector changes per quarter.** Should run 2–4. Materially more means hysteresis is too loose; near zero means it is too tight.
- **Correlation guard firing frequency.** Same pair three weeks running is a map defect.
- **Binding-constraint distribution in L5.** Risk budget bound in 100% of v1 tests. If that persists, either the stop cap or the tier risk percentage is miscalibrated.
- **How often L6 returns NO SETUP.** It did so on every candidate in v1. If that continues for months, the setup definitions are too narrow for this market.

### 7.2 Scalability notes

- **Compute is not a constraint.** The full chain runs in well under a minute on a 22 MB panel.
- **The binding constraint on universe growth is classification, not compute.** Going from 533 to ~1,150 symbols is a data-sourcing problem.
- **Position count scales with regime**, not with capital — 5–7 at NEUTRAL regardless of pool. Growth in capital flows into position *size*, where the 8%-of-ADV cap becomes binding on thin names. At a ₹1 cr pool, liquidity binds nowhere; at ₹10 cr it would bind on most of the watchlist.
- **The manual gate check does not scale.** At 2–3 names a week it is fine. It is the reason Tier 3 items 11 and 12 matter more than they look.

---

## 8. Revision log

The full change log, with evidence and rationale for every decision, now lives in
**section 11** at the end of this document. It is the authoritative record.

---

# 9. v1.1 — Validation round (4 September 2026)

**Objective.** Test whether the v1 architecture actually selects what it claims to,
before committing capital. Three things came out of it: a data-layer defect that
invalidated much of the first day's analysis, one gate revised on evidence, and one
gate deliberately left alone for want of evidence.

**The headline result is a method finding, not a parameter finding.** Reasoning from
hand-picked winners produced a confident and wrong recommendation. The same question
run against 9,042 name-weeks with both sides scored overturned it. Every parameter
change from here should clear that bar.

## 9.1 The ingest defect

`load_bhavcopy()` filtered `SERIES == "EQ"`, silently discarding **25% of every daily
file**. Symbols moved to BE (trade-for-trade) or BZ (LODR non-compliant) vanished from
the panel with no error and no warning.

STLTECH moved EQ→BE on 14 May 2026 and was absent for four months while trading
₹22 cr a day. It surfaced only because a stale-symbol check was added for an unrelated
reason. Of 331 symbols flagged stale, **210 were still trading** — every one on a
non-EQ series.

| | Before | After |
|---|---|---|
| Panel rows | 1,098,583 | **1,221,277** (+11.2%) |
| Symbols | 2,998 | **3,161** |
| Corporate actions | 74 | 88 |
| Stale, within 120 days | 204 | **35** |

**Why BE/BZ matter more than average, not less.** That is where surveillance sends a
stock. Under the old filter a held position moving to BE would disappear from the panel
exactly when it became hard to exit — L7 and L8 would stop seeing it. The exclusion was
also doing G3's job by accident, invisibly and without recording it.

**Consequences beyond the fix.** Three conclusions drawn on the pre-fix panel were
wrong or unsupported: MTARTECH ranked #1 in Defence on `nan` data; DIACABS was cited as
the flagship blind-spot case while stale; and the claim that HFCL's absence was
depressing Telecom's breadth was false — HFCL had only two sessions missing.

**Deliberate exclusions, now recorded rather than accidental:** GS/GB (gilts), IV/RR/E1
(InvITs, REITs), N1–N9 (non-convertible debentures — M&MFIN N3 trades near ₹2,337
against equity near ₹300), SM/ST (SME platform, 434 rows/day; thin floats and different
lot rules — this is why NSE EMERGE listings have no panel match).

## 9.2 G12 delivery gate — revised

**Proposed and withdrawn:** scaling the delivery floor with ADV. Four winners
(KALYANKJIL +61%, MOREPENLAB +160%, BODALCHEM +105%, RATNAVEER +80%), all above
₹176 cr ADV, all blocked by G12, suggested the gate misreads intraday popularity as
operator activity.

**The sweep says otherwise.** 45 weekly cohorts, 9,042 name-weeks, momentum candidates
only, both sides scored:

| ADV bucket | Blocked excess | Admitted excess | Gate adds |
|---|---|---|---|
| ₹5–25 cr | −8.38% | +0.55% | +8.93 |
| ₹25–75 cr | −2.85% | +0.83% | +3.68 |
| ₹75–200 cr | +0.89% | +1.94% | +1.05 |
| >₹200 cr | −1.46% | +0.31% | +1.77 |

The gate adds value in **every** bucket. The liquidity interaction tested at t = −0.81 —
noise, and pointing the wrong way. The four cases were selected because they went up.

**What shipped instead.** Two changes, both from the sweep rather than the anecdotes:

1. **Gate window.** 20-session mean replaced by a 63-session mean, passing on *either*
   that mean clearing the floor *or* 3 of the last 4 monthly means clearing it. MOREPENLAB
   failed by 1.0pp on a series oscillating 24–33% — a coin flip on noise. Scoring still
   uses the responsive 20-day mean; only the gate changed.
2. **Delivery weight.** Volume component split moved 50/50 → 25/75, taking delivery from
   7.0% to 10.5% of MS. Tilting selection toward delivery improved 4-week excess by
   **+3.1pp (t = +2.5)**, hit rate 49% → 62%, with a **plateau from w=0.15 to w=0.30** —
   a plateau, not a spike. Set conservatively below the tested optimum because that test
   was two-factor and does not calibrate a seven-component weight.

The 60%+ delivery band returns +3.64% excess at a 55% hit rate. A binary gate at 30%
discards that entirely, which is the argument for treating delivery as a score input
rather than only a filter.

## 9.3 Breadth floor — tested, unchanged

The floor blocks any sector with breadth < 50 regardless of rank. Three cases pointed
both ways: Telecom rank 1 at breadth 25.6 (HFCL +20.9% missed), Realty rank 3 at 49.1
(LODHA +36.6% missed), Metals rank 2 blocked (VEDL −13.8% avoided).

**Result: no change, on insufficient evidence.** Stock-level excess of top-3 names
inside top-3 sectors, split by parent breadth, showed a negative edge at every threshold
— but t-statistics ran −0.81 to +0.71, and only **23 observations** fall below the floor
across all usable history.

Two things argue against acting:

- **Test 3 kills the mechanism.** The worst-performing band is the *highest* breadth
  (65+, n=74, −0.93%), the best are the two lowest (n=5 and n=8). That is noise, not a
  relationship.
- **The horizon was wrong.** The counterfactual log revealed the sign flips: blocked
  sectors' top names beat by **+1.90% at 4 weeks** and lose **−3.52% at 12 weeks**
  (26% hit rate). The holding period is 2–3 months, so the 4-week sweep measured a
  horizon nobody trades. This fits the mechanism the floor was built for.

Removing a risk control on t = 0.7 would repeat the G12 error in the opposite direction.

## 9.4 Harness defects closed

| Defect | Effect | Fix |
|---|---|---|
| Silent staleness | STLTECH missing 4 months, unnoticed | Flag any symbol trailing panel max; `stale_symbols.csv` + manifest warning |
| Look-ahead in replays | Map built from *current* Nifty 500 list | Archive constituent lists per build to `reference/snapshots/` |
| Returns from window start | BLUSPRING read +48.9%; actual from signal +12.4% | `eval_signal.py` measures from signal date, excludes stale and EXTENDED |

The signal-date fix alone moved sector-chain precision from mean +3.11% / median −1.32%
/ hit 47% to **mean +3.41% / median +0.41% / hit 52%**. The earlier negative median was
partly an artefact of counting stale names and unbuyable extended picks.

## 9.5 New files

| Path | Purpose | Cadence |
|---|---|---|
| `artifacts/stale_symbols.csv` | Symbols with no data for >10 sessions | Daily |
| `reference/snapshots/YYYY-MM-DD_*.csv` | Point-in-time constituent lists | Daily |
| `scripts/breadth_log.py` | Logs sectors blocked on breadth + the 3 names you'd have bought | Weekly |
| `state/breadth_counterfactual.jsonl` | Accumulating sample, 24 records backfilled | Weekly |
| `g12_sweep.py`, `breadth_sweep.py`, `eval_signal.py` | Analysis harness (not pipeline) | Ad hoc |

Manifest gains a `stale_symbols` count and warnings for unrecognised series.

## 9.6 What is now decided, and what is not

**Decided:** the ingest series filter, the G12 window and weight, and that the breadth
floor stays until there is evidence to move it.

**Open, in order:**

1. **L2 ablation** — the identical replay with sector gating off, L3 scoring the mapped
   universe directly. Still the question that decides the architecture. Note the
   watchlist currently beats the sector chain on every clean measure (+6.23% vs +3.41%
   mean 4-week excess, 57% vs 52% hit), which is a third independent signal pointing the
   same way.
2. **Breadth floor** — revisit at 60 matured observations (currently 23, ~0.5/week), or
   sooner via the backtest harness. **Test at 12 weeks, not 4.**
3. **Full-universe classification** — still deferred. TFCILTD (+90.4%) and SHILPAMED
   (+79.2%) were already in the watchlist universe and still never ranked, so the payoff
   is smaller than the SAKAR and BLUSPRING cases first suggested.

**Standing methodological rule.** No parameter changes on anecdotes. Every proposed
change is swept across the full universe, scored on what it admits *and* what it
excludes, tested for significance on weekly cohort means rather than raw n, and checked
for a plateau. A change that only works at one exact value is a fitted artefact.

---

# 10. v1.2 — The L2 ablation (4 September 2026)

**Objective.** Settle the question v1.1 left open: does the sector gate earn its place?
Three independent signals had pointed against it — eight blocked winners in the traces,
the watchlist outperforming the sector chain, and the breadth results — but none was
significant on its own.

**Result: the sector signal has real value; using it as a gate was the wrong way to
apply it.** This is the first change in the project made on a significant paired test.

## 10.1 The test

Three arms, 49 weekly cohorts, identical panel, identical gates, top 3 names per week.
The only difference is how sector information enters the selection.

| Arm | What it does |
|---|---|
| **Gated** | L3 restricted to the sectors L2 selected. The v1 system. |
| **Composite** | All 28 sectors scored; ranked by `0.65·MS + 0.35·SMS`. Sector penalises, does not exclude. |
| **MS only** | All 28 sectors scored; ranked on MS alone. Sector information discarded. |

### 12-week horizon (matches the 2–3 month holding period)

| Arm | Picks | Mean excess | Median | Hit | t |
|---|---|---|---|---|---|
| Gated | 93 | +2.30% | +3.94% | 61% | +0.92 |
| **Composite** | 111 | **+8.95%** | **+6.30%** | **73%** | **+5.50** |
| MS only | 111 | +5.08% | +4.56% | 68% | +3.35 |

**Paired on the same weeks:**
- composite − gated = **+7.59pp, t = +2.91** (significant)
- composite − MS-only = **+4.30pp, t = +2.22** (significant)

### 4-week horizon

| Arm | Mean | Median | Hit | t |
|---|---|---|---|---|
| Gated | +0.35% | −0.62% | 43% | +0.36 |
| Composite | +1.55% | +0.41% | 51% | +1.77 |
| MS only | +1.37% | +1.08% | 59% | +1.96 |

Nothing separates the arms at 4 weeks (paired t = 1.61 and 0.24). **The entire effect is
at the longer horizon** — the second time in this project that testing at the wrong
horizon would have produced the wrong answer (the first was the breadth floor, where the
sign flipped between 4 and 12 weeks).

## 10.2 What it means

The gate discards the good name in the 8th-ranked sector. The composite keeps it,
penalised. On this sample that difference is worth **~7.6 percentage points per quarter**.

Equally, discarding sector information entirely costs 4.3pp against the composite. So
sector strength is genuinely predictive — L2 is not useless, it was being used wrongly.

## 10.3 A bug that nearly produced the wrong conclusion

The first ablation reported "ungated wins, not significant" (t = 0.98). That arm was
supposed to be the composite. It was not.

`stock_select.py` computed `composite` and `SMS`, printed them in the table, and then
**never wrote them to the JSON**. Any downstream consumer reading the output saw
`composite: None` and silently fell back to MS — ranking with the sector signal
discarded, without knowing it.

The bug was found only because the middle path was requested as a separate test. Had it
not been, the recorded conclusion would have been "sector gating makes no measurable
difference," which is false in both directions: the gate is worse than composite, and
composite is better than ignoring sectors.

**Lesson for the change log: a silent `None` in an output contract is more dangerous
than a crash.** Exported fields need asserting, not assuming.

## 10.4 What changed in the code

| Change | File | Effect |
|---|---|---|
| `--global-rank N` mode | `stock_select.py` | Cross-sector ranking by composite. Opt-in; default gated path unchanged. |
| `composite` + `SMS` exported | `stock_select.py` | Fixes the silent-`None` contract bug |
| Concentration warning | `stock_select.py` | Cross-sector mode applies no sector cap; warns when the list concentrates |
| Documentation | `SKILL.md` | Records the three-arm result and both caveats |

**Usage.** Pass all mapped sectors to `--sectors` together with `--global-rank N`.
Extended names (>2.5 ATR) are excluded from the ranking and listed separately — this
differs from the gated path, which still gives them a shortlist slot. *That inconsistency
is open and should be aligned.*

## 10.5 Effect on the 4 September list

| Symbol | Sector | MS | SMS | Composite |
|---|---|---|---|---|
| AVALON | Electronics Mfg | 82.4 | 90.2 | **85.1** |
| LENSKART | Consumer Services | 90.1 | 70.0 | 83.1 |
| GLAXO | Healthcare | 90.3 | 68.6 | 82.7 |
| THELEELA | Consumer Services | 89.3 | 70.0 | 82.6 |
| DIVISLAB | Healthcare | 89.0 | 68.6 | 81.9 |
| OBEROIRLTY | Realty | 81.5 | 81.6 | 81.5 |

AVALON still leads: a merely-good MS in the strongest sector beats a great MS in a
mid-ranked one, which is the sector signal doing real work. LENSKART enters at rank 2
having been excluded entirely under the gate — it is one of the eight winners the traces
flagged as missed. Defence disappears from the top 10 despite being a confirmed sector;
its constituents do not survive cross-sector comparison.

## 10.6 Risks accepted

- **Regime.** 49 weeks, all NEUTRAL. Sector rotation exists to protect against a
  rotation-driven drawdown, and this sample contains none. If the regime turns, the gate
  may be the thing you want back. Do not treat this as settled.
- **Concentration.** 4 of 10 names came from Healthcare. Cross-sector ranking applies no
  sector cap of its own; L8's 40% limit is the only control, and that interaction was not
  part of the tested arm.
- **Look-ahead.** The map still uses the current Nifty 500 list. Snapshots began
  accumulating 4 Sep 2026; a genuinely point-in-time replay is months away.

## 10.7 Rollout

Run **both modes side by side** next Friday rather than switching outright. The gated
path is unchanged and still the default. Adopt cross-sector as default only after seeing
the divergence on live names that can be checked by hand.

---

# 11. Change log

Authoritative record of every decision, including proposals that were tested and
rejected. A log that shows only what shipped hides the reasoning that produced it.

| Date | Ver | Change | Evidence | Status |
|---|---|---|---|---|
| 2026-09-04 | v1 | Baseline architecture: 28 sectors, 12 gates, 9 layers | First full end-to-end run | Shipped |
| 2026-09-04 | v1 | Sector-agnostic watchlist added | Map covers 533 of 2,998 symbols | Shipped |
| 2026-09-04 | v1 | Caps rule: outside Nifty 500 → SMALL | All 36 unclassified verified outside index | Shipped |
| 2026-09-04 | v1 | Shortlist expanded to top 10 | User request | Shipped |
| 2026-09-04 | v1.1 | **Ingest: keep BE/BZ series, not EQ only** | 25% of every daily file discarded; +122,694 rows, +163 symbols | Shipped |
| 2026-09-04 | v1.1 | Staleness detection + manifest warning | STLTECH missing 4 months unnoticed | Shipped |
| 2026-09-04 | v1.1 | Constituent snapshots archived per build | Replays had look-ahead on index membership | Shipped |
| 2026-09-04 | v1.1 | Signal-date evaluation harness | BLUSPRING read +48.9%; true value +12.4% | Shipped |
| 2026-09-04 | v1.1 | G12 window → 63-session mean or 3-of-4 months | MOREPENLAB failed by 1.0pp on 24–33% noise | Shipped |
| 2026-09-04 | v1.1 | Delivery weight 7.0% → 10.5% of MS | +3.1pp (t=+2.5), plateau w=0.15–0.30 | Shipped |
| 2026-09-04 | v1.1 | ~~G12 floor scaled by ADV~~ | Gate adds value in **every** bucket; interaction t=−0.81 | **Rejected** |
| 2026-09-04 | v1.1 | Breadth floor left at 50 | Edge negative at all thresholds but t = −0.81 to +0.71; n=23 | **Deferred** |
| 2026-09-04 | v1.2 | **`--global-rank`: sector as score input, not gate** | +7.59pp vs gated (t=+2.91), +4.30pp vs MS-only (t=+2.22) at 12 weeks | Shipped, opt-in |
| 2026-09-04 | v1.2 | `composite` + `SMS` exported to JSON | Silent `None` caused a wrong ablation result | Shipped |
| 2026-09-04 | v1.2 | Concentration warning in cross-sector mode | 4 of 10 names from one sector; no cap in tested arm | Shipped |
| 2026-09-06 | v2.0 | **Dual sector map.** v1 (NSE Industry, 28 sectors) and v2 (NSE index constituents, 18 sectors) run in parallel, reported separately | v1 ahead −1.53pp at 12wk but t=−1.08; no separation | Shipped, both live |
| 2026-09-06 | v2.0 | NSE sector index constituent fetch, 18 of 39 lists resolve | Price series and breadth described different populations, 10.4pp mean disagreement | Shipped |
| 2026-09-06 | v2.0 | `map_comparison.jsonl` weekly logger, 49 weeks backfilled | Neither map separates; decision needs sample | Shipped |
| 2026-09-06 | v2.0 | ~~v3: v2 minus 6 thematic indices~~ | Recovered 40% of v2 deficit at 12wk but worse at 4wk, med rank #47→#56 | **Rejected** |

## 11.1 Open items

| Item | Trigger to revisit | Blocked on |
|---|---|---|
| Breadth floor | 60 matured 12-week observations (currently 23, ~0.5/wk) | Sample accumulation, or 2022 backtest |
| Cross-sector as default | One live Friday running both modes side by side | Next run |
| Extended-name handling differs between modes | Before cross-sector becomes default | Small fix |
| Full-universe classification | After cross-sector adoption settles | Deferred — TFCILTD/SHILPAMED were visible and still didn't rank |
| Backtest across 2022, 2024 | Before scaling beyond a token position | Harness never run |
| G3 from series column | Next ingest change | BE/BZ now available as a free surveillance flag |
| 7 manual gates | Ongoing | Nothing is fundable without them |

## 11.2 Standing methodological rules

Adopted after a confident, wrong recommendation survived four hand-picked cases and
failed against 9,042 name-weeks.

1. **No parameter change on anecdotes.** Sweep the full universe.
2. **Score both sides.** What a rule admits *and* what it excludes.
3. **Significance on weekly cohort means**, not raw n — overlapping windows inflate n.
4. **Require a plateau.** A change that works only at one exact value is a fitted artefact.
5. **Test at the holding horizon.** Two findings reversed sign between 4 and 12 weeks.
6. **Assert exported fields.** A silent `None` is more dangerous than a crash.
7. **Record rejected proposals**, with the evidence that killed them.

---

# 12. v2.0 — The dual sector map (6 September 2026)

**Decision: run two sector maps in parallel, report both, converge later or not at all.**
Neither is authoritative. Neither has separated from the other on 49 weeks of evidence.

## 12.1 The defect that started it

The sector score was built from **two different populations**. For the 18 index-backed
sectors, absolute momentum and risk-adjusted momentum came from NSE's cap-weighted index,
while breadth was computed from the larger, equal-weighted Industry basket.

Measured over 126 sessions the two disagree by **10.4 percentage points on average**, and
by more than 10pp in 8 of 18 sectors:

| Sector | Index | Basket | Gap |
|---|---|---|---|
| Defence | +20.0% | +45.8% | **+25.8** |
| Automobile | +3.1% | +22.0% | +18.9 |
| Healthcare | +12.0% | +29.6% | +17.6 |
| Private Bank | −0.4% | +17.0% | +17.3 |
| Oil & Gas | −3.8% | +13.1% | +16.9 |
| FMCG | −7.8% | +8.5% | +16.4 |
| IT | +1.3% | +16.6% | +15.4 |
| Metals | +13.1% | +3.1% | −10.0 |

Defence is the clearest case: NIFTY INDIA DEFENCE is cap-weighted and dominated by BEL and
HAL, returning +20%; the 16-name equal-weighted basket returned +45.8%. **The momentum
components described BEL and HAL; the breadth component described sixteen names, thirteen
of which scored below MS 55.** That is why Defence could rank 2nd while its best buyable
constituent sat at #286 of 445.

This was diagnosed from a user observation, not from a test: *if a sector ranks #1, its top
three stocks should be among the strongest overall.* Measured across 426 sector-weeks, the
Spearman correlation between sector rank and its top-3 constituents' unified rank is only
**+0.39**. The intuition was right; the system did not meet it.

## 12.2 Bringing in the Nifty index constituents

`fetch_nse.py` now pulls NSE sector index constituent lists weekly into
`reference/sector_constituents/`. They use the same host, format and auth as the Nifty 500
list already fetched — `Company Name, Industry, Symbol, Series, ISIN`.

**18 of 39 attempted lists resolve.** The fetch is deliberately fail-soft: unknown filenames
are attempted, logged and skipped, so an unresolvable name never breaks a build.

| Resolved (18) | |
|---|---|
| **True sectors (12)** | auto, bank, it, fmcg, metal, pharma, realty, media, healthcare, consumerdurables, oilgas, psubank |
| **Cross-cutting themes (6)** | energy, infra, pse, cpse, commodities, consumption |

| Not resolved (21) | |
|---|---|
| Financial | privatebank, financialservices, financialservicesexbank, capitalmarkets |
| Thematic | indiadefence, indiarailwayspsu, indiatourism, indiadigital, indiamanufacturing, sugarethanol, housing, midsmallhealthcare |
| Industrial | capitalgoods, construction, chemicals, cement, power, telecommunications, transportationlogistics, consumerservices, servicessector |

The failures land precisely where the hand-written baskets are weakest — Defence, Railways,
Capital Goods, Chemicals. Retry with alternate names on future fetches; some may not publish
constituent files at all.

**The lists add almost no new data.** Union across all 18 is 205 symbols, of which 199 are
already in v1. Only six are new: DBCORP, HATHWAY, NAZARA, NETWORK18, PSB, TIPSMUSIC — five of
them Media names, which is why NSE's media list has 10 constituents against v1's 5.

**They are a redefinition, not an expansion:** the same stocks grouped more narrowly.

## 12.3 The two maps

| | **v1 — Industry column** | **v2 — NSE index constituents** |
|---|---|---|
| Source | `Industry` column of `ind_nifty500list.csv` | `reference/sector_constituents/*.csv` |
| Sectors | 28 | 18 |
| Symbols | 533 | 205 |
| Tier B baskets | 7 hand-written | none |
| Price/breadth consistency | **inconsistent** for 18 sectors | **consistent** |
| Cap bias | equal-weighted, small/mid friendly | index is cap-weighted |
| Coverage | broad | narrow, large-cap tilted |

`sector_map_v2.csv` and `sector_index_map_v2.csv` are frozen exactly as v1 is — quarterly
revisions only, recorded in the change log.

## 12.4 Head-to-head, 49 weeks

Same weeks, same unified stock ranks, same rule (top-3 sectors, top-3 constituents by
unified momentum rank). Only the map differs.

| Horizon | Map | Picks | Excess | Median | Hit | MAE | Med rank |
|---|---|---|---|---|---|---|---|
| 4 weeks | **v1** | 400 | **+1.49%** | +1.32% | **57%** | −9.07% | 55 |
| | v2 | 402 | +0.96% | +0.28% | 52% | **−7.83%** | 54 |
| 12 weeks | **v1** | 331 | **+5.39%** | +4.21% | 60% | −12.88% | 51 |
| | v2 | 333 | +3.84% | +3.94% | 60% | **−11.70%** | **47** |

**Paired: v2 − v1 = −0.47pp (t = −0.87) at 4 weeks, −1.53pp (t = −1.08) at 12 weeks.**
Verdict: **no separation.** v1 leads consistently; neither t-statistic is close to
significant.

**Back-to-back confirmation test** (name in top-3 picks two consecutive Fridays, last 15
weeks, return to 4 Sep):

| | v1 | v2 |
|---|---|---|
| Confirmations | 22 | 21 |
| Mean return | **+8.0%** | +5.5% |
| Median | **+1.6%** | −1.8% |
| Win rate | **59%** | **33%** |

Only 5 names overlap: WELCORP, LAURUSLABS, LLOYDSME, TORNTPHARM, GVT&D. **The two maps are
finding almost entirely different books.**

**The trade-off is consistent and worth stating plainly: v2 has shallower drawdowns at both
horizons and better median pick rank; v1 has higher returns and a much better win rate.** If
the binding constraint were drawdown rather than return, the answer could flip.

**Where each fails.** v2 caught the Pharma run cleanly — LAURUSLABS +30.2% confirmed 19 June,
SAILIFE +29.7%, GLAND +18.3%, five weeks ahead of v1 and at a 13% lower price. But it also
concentrated into the Adani complex through its Energy and Commodities baskets: ADANIGREEN
−12.4%, ADANIENSOL −5.4%, ADANIENT −3.3%, CGPOWER −5.4%. v1's advantage came from a spread of
smaller winners outside NSE index membership — WELSPUNLIV +25.5%, AVALON +22.8%,
CYIENTDLM +68.2%. Both were caught by GVT&D at −15.0%.

## 12.5 Rejected: v3

v2 minus the six cross-cutting themes, leaving 12 true sectors and 148 symbols. Tested to
check whether the Adani concentration was the whole story.

| Horizon | v1 | v2 | v3 |
|---|---|---|---|
| 4 weeks | **+1.49%** | +0.96% | +0.78% |
| 12 weeks | **+5.39%** | +3.84% | **+4.47%** |

The hypothesis was directionally right — v3 recovered ~40% of v2's 12-week deficit with the
best hit rate (62%) and shallowest drawdown (−11.32%). But it was the worst arm at 4 weeks,
median pick rank worsened from #47 to #56, and v3 beat v2 in only 27% of weeks. At 12 sectors
it also sits at the documented minimum for percentile validity. **Not adopted.**

## 12.6 Operating model

Both maps run every Friday. Outputs are reported **separately, never merged**.

1. L1 regime — unchanged, map-independent
2. L2 sector score — run twice, once per map
3. Unified stock ranking — computed once across all 1,154 liquid names, shared by both arms
4. L3 shortlist — produced per map
5. `map_compare_log.py` appends one record per week to `state/map_comparison.jsonl`

**What is tracked:** top-3 sectors and top-3 constituents per map, their unified ranks, and
forward excess return at 4 and 12 weeks recomputed from the panel at report time.

**Decision gate: 80 matured 4-week observations.** Currently 45 (backfilled). At ~1/week that
is roughly 8 months, or immediately once the backtest harness can run 2022 and 2024.

**Convergence is not assumed.** The two may stay permanently separate — v1 for return, v2 as a
cross-check whose steadier drawdown profile is informative in its own right. A merge should
only follow a significant result, not a tidy-up impulse.

## 12.7 Files

| Path | Status | Purpose |
|---|---|---|
| `scripts/fetch_nse.py` | **updated** | Fetches 39 sector constituent lists weekly, fail-soft |
| `reference/sector_map_v2.csv` | **new** | 18 sectors, 205 symbols, frozen |
| `reference/sector_index_map_v2.csv` | **new** | v2 sector → NSE index mapping |
| `reference/sector_constituents/*.csv` | **new, auto** | 18 resolved lists, refreshed weekly |
| `scripts/map_compare_log.py` | **new** | Weekly v1-vs-v2 logger and report |
| `state/map_comparison.jsonl` | **new** | 49 weeks backfilled, one record per week |
| `reference/ARCHITECTURE_v2.0.md` | **new** | This document |

## 12.8 What this does not fix

- **The +0.39 correlation stands.** Sector rank still predicts constituent rank only weakly.
  v2 makes price and breadth consistent but does not raise the correlation enough to matter.
- **21 sector lists remain unresolved**, including every sector where the hand-written v1
  baskets are weakest.
- **Cap-weighting versus equal-weighting is untested.** Using v2 membership but computing an
  *equal-weighted* series from those constituents would give consistency without the
  large-cap bias. Not yet run.
- **One regime.** All 49 weeks are NEUTRAL. Sector rotation exists to protect against a
  rotation-driven drawdown, and this sample contains none.
