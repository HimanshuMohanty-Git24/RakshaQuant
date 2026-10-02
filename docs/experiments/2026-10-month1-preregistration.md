# Pre-registration: month-1 paper experiment (`month1-2026-10`)

Written before the experiment starts; source: audit §Y and plan M8.6. The month-end report
(`docs/experiments/2026-10-month1.md`) will be judged against this document. Any change made
after the start is logged as an event and listed in that report.

## 1. Question

Does an AI advisor that can only veto deterministic trade proposals add value **net of its cost**,
compared with the same system without it? And is the deterministic system itself operationally
sound enough to trust its results?

The month **cannot** validate a trading edge (≈ 200 correlated trades against ≈ ₹190 of costs per
trade); it can only reject clearly bad strategies and test the system.

## 2. Design

| Item | Value |
|---|---|
| Market, product | NSE cash equities, **CNC swing, long-only** |
| Universe | NIFTY 50 constituents, snapshot pinned on the first session (date recorded) |
| Strategies | `momentum`, `mean_reversion` trade; `breakout`, `trend_following` are shadow-only (recorded, never traded) |
| Policy | Entry at the fill; stop = fill − 2·ATR, target = fill + 3·ATR, trailing 2·ATR on daily closes, time exit after 10 sessions, no partials |
| Entry window | 09:20–09:45 IST, one decision cycle per session on settled daily bars |
| Books (₹10,00,000 each, isolated) | **A**: no advisor (deterministic baseline). **B**: typed veto (Laya → Jev cascade), VETO iff calibrated P(veto) ≥ 0.6. **C**: LLM veto (role `veto`), VETO only with grounded evidence and confidence ≥ 0.6 |
| Shared by the books | Market data, features, signals, regime, announcements and typed events; each book has its own broker state, positions, risk state and kill switches |
| Shadow ledger | A counterfactual for every BUY signal (approved, vetoed, rejected or shadow), same exit rules, net of costs, with alpha vs NIFTY |
| Benchmarks | NIFTY 50 buy-and-hold; equal-weight universe; cash |
| Learning injection | **Off.** Nightly lessons are stored but never fed back (audit F-20) |
| Live orders | Off. Paper only (simulated broker with NSE costs, latency, spread and impact) |

### Schedule

- Start: the first trading session after the readiness gate (plan M12) passes, with the wallet
  reset and one clean replay day and one clean supervised paper day.
- Length: **20 trading sessions**. NSE 2026 holidays in the window: 2 Oct (Gandhi Jayanti),
  20 Oct (Dussehra), 8 Nov (Diwali Laxmi Pujan, a Sunday; Muhurat timings not yet notified, so
  the calendar keeps it closed), 10 Nov (Diwali-Balipratipada), 24 Nov (Guru Nanak Jayanti).
  Example: starting Mon 5 Oct, session 20 is Mon 2 Nov.
- Each session: auto-start 09:05 IST, decisions in the entry window, monitoring to the close,
  report at 15:45, exit at 15:50.

### Frozen configuration

Recorded at the start and checked every day; a mismatch is a protocol deviation:

| Artefact | Fingerprint at registration (2026-10-02) |
|---|---|
| `src/config/experiment.yaml` | git blob `5848be36` |
| Risk limits (`RiskLimits().limits_hash()`, defaults) | `0b4a8c9084ca` (each `RiskDecision` carries the hash in force) |
| Cost schedule, event rules, NSE calendar | as committed at the start tag |

Code and configuration on the trading path are frozen for the month. Only a P0 incident fix may
change them; every such change is logged and disclosed.

## 3. Hypotheses and pre-registered decision rules (audit §Y.3)

### H1 — Systems test (pass/fail). All must hold over the month:

1. zero unresolved P0 incidents;
2. ≥ 99% of scheduled decision cycles completed during market hours;
3. zero reconciliation drift (OMS vs broker);
4. modelled costs within ±5% of a hand-computed contract-note equivalent for a sample of ≥ 10
   trades;
5. 100% of closed trades explainable from their `decision_id`.

Failing H1 is the most likely and most useful outcome of month 1: fix, then rerun.

### H2 — AI value (per advisor: B and C, each against A)

Keep an advisor in the trading path **only if both** hold:

- **net AI value > 0**: (book equity − book A equity) − the book's cumulative AI spend, at the
  final session; and
- **veto precision**: the lower bound of the 95% Wilson interval of "vetoed signals whose
  counterfactual lost money (net)" is **above 50%**, with **≥ 30 settled vetoes**.

Otherwise remove it from the trading path (it may stay as an offline explainer). With fewer than 30
settled vetoes the rule is **inconclusive: extend** the experiment; do not conclude either way.
An advisor that abstained on most proposals (e.g. because no decision model or LLM was available) is
reported as such and is inconclusive.

### H3 — Edge (cannot be validated in a month)

- **Retire** a strategy whose shadow-ledger net expectancy is significantly negative: the 95%
  bootstrap interval of its per-trade net return lies entirely below zero (≥ 20 settled
  counterfactuals).
- **Drift check:** paper behaviour vs the backtest on the same days (per-trade returns and trade
  counts), reported, not judged.
- Expected result if the signals have no edge: **−1.5% to −4%** of capital per book.
- Capital-worthiness still requires the walk-forward gate on point-in-time data (n ≥ 200 out-of-
  sample trades, CI lower bound > 0) **and** a matching live-paper track record.

## 4. Metrics and estimation

All figures are computed from the event store by the daily report
(`src/evaluation/daily_report.py`); the month-end report uses the final day's month-to-date
values.

| Metric | Definition |
|---|---|
| Net AI value | (equity of the book − equity of book A) − the book's AI spend (₹, from `LLMCall` events; local decision models cost 0) |
| Veto precision | settled vetoed counterfactuals with net P&L < 0 ÷ settled vetoed counterfactuals; Wilson 95% CI |
| Loss avoided / gain forgone | the vetoed counterfactuals' net losses / net gains |
| Sharpe, Sortino | daily returns, √252; Sharpe with a seeded bootstrap 95% CI (2,000 resamples) |
| Max drawdown, Calmar | on daily closing equity |
| Expectancy | mean net P&L per closed trade (₹ and %) |
| Slippage | fill vs decision price and vs arrival price, in bps (adverse = positive) |
| Fallback / abstain rates | per advisor, of proposals reviewed |
| Escalation rate | decision-model calls escalated from Laya to Jev |

With about 20 daily observations, the confidence intervals will be wide; they are reported, not
hidden.

## 5. What will not be concluded

- That a strategy has an edge (H3 can only retire strategies or flag drift).
- That AI helps "in general": only whether *these* advisors, with *this* configuration, added net
  value in *this* month.
- Anything from a month in which H1 failed, beyond the failures themselves.

## 6. Known limitations, disclosed in advance

- Paper fills are modelled (latency, spread by ADV tier, square-root impact, participation cap),
  not real; calibration against real fills needs live trading.
- The universe is today's NIFTY 50 (survivorship bias for any backtest comparison).
- YFinance quotes lag the exchange by about 15 minutes; entries are delayed accordingly.
- The typed decision model (Laya) is uncalibrated until the owner-approved labelling run (PROGRESS
  OQ-3); until then most of Book B's answers abstain.
- NSE announcements come from the public RSS feed (PROGRESS OQ-2); feed gaps are recorded.
