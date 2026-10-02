# Incidents

What to do when something looks wrong. **If in doubt, halt first** (create `var/paper/HALT`;
see [kill switches](kill-switch.md)). Exits keep working while entries stop, and halting is
always reversible.

## Severity

| Level | Meaning | Response |
| --- | --- | --- |
| **P0** | An order, position or P&L could be wrong. Also: an order without a risk decision, lost or duplicated events, a control that does nothing, simulated prices able to trade, a secret in a log. | Halt. Collect evidence. Fix, test and replay before the next session. Log it (below). |
| **P1** | Degraded but safe: stale data (entries blocked), an AI provider or the decision model down (books abstain), a report or Telegram failure. | Note it. Fix outside market hours. |
| **P2** | Cosmetic: the console, wording, formatting. | Batch the fixes after the month run. |

## First response

1. **Halt** if positions could be at risk (HALT file).
2. **Don't edit the database.** `var/<env>/rakshaquant.db` is the experiment's append-only
   record. Copy it if you need to inspect it with other tools.
3. **Collect evidence:**
   - **Log:** `var/logs/rakshaquant-YYYYMMDD.log`, JSON lines, one file per IST day, secrets
     redacted. The web console's System screen shows its tail with filters.
   - **Lineage:** for an order or trade, open `/decisions/<decision_id>` in the console. It
     shows the signal, every book's outcome, the advisor, the risk checks, orders, fills and
     the exit.
   - **Report:** `uv run python scripts/daily_report.py --date YYYY-MM-DD` regenerates the
     day's report from the events, including integrity and reconciliation.
   - **Replay:** `uv run python scripts/replay_day.py --date YYYY-MM-DD --source var/paper/rakshaquant.db`
     re-runs the day from its recordings into a separate store.

## Symptoms

| Symptom | Likely cause | What to do |
| --- | --- | --- |
| Exits at start with **code 3** | Another process holds the environment: the scheduled run, a manual run, or a stuck process. | Find it (Task Manager, `Get-Process python`). Stop it only if it isn't today's session. |
| Exits at start with **code 2** | Configuration: a bad `.env` value, an enabled LLM role without its key, a `RISK_*` value out of bounds, or a date the calendar doesn't cover. | `uv run python scripts/check_config.py` names the setting; values are never printed. |
| Exits with **code 1** | A crash; the traceback is in the log. | Restarting is safe (below). A crash in the order path is a P0. |
| Exits at once with code 0 | A holiday or weekend (`HolidaySkipped`), or started after the day's `EXIT`. | Nothing to do. |
| **Dead-man** Telegram (no heartbeat for 3 min) | The process died, hung, or the machine slept or rebooted. | Check the console window and the log's last lines, then restart. |
| `FeedStale`, data delay rising | Yahoo is rate-limiting or down; polls back off up to 15 min. | Entries are blocked (`SYS_DATA_STALE`) while quotes are older than `RISK_MAX_QUOTE_AGE_S`; exits use the last quotes. Usually recovers by itself (`FeedRecovered`). |
| `history_lagging` at pre-open | Yahoo hasn't settled yesterday's daily close yet. | Those symbols are skipped today, never traded on a partial series. |
| `QuoteRejected` events | A quote failed validation (non-finite, outside the band, volume going backwards). | Dropped and never taped. Many for one symbol suggest bad reference data (bands). |
| `SYS_RECON_DRIFT` alert | The position book disagrees with the broker's state. | A P0 for the paper broker: it should never drift. Entries stay blocked until the next reconcile agrees. |
| An order stuck `UNKNOWN` | A submission whose outcome wasn't confirmed. | It's resolved by its tag (adopted) or rejected after two checks; entries in that symbol are blocked meanwhile (`SYS_UNKNOWN_ORDER`). |
| Book C trades exactly like A | The veto role is off, down, out of budget or refusing (`AdvisorFallback`). | AI Desk: model health and spend; `uv run python scripts/llm_check.py`. See [providers](providers.md). |
| Book B trades exactly like A | No decision model (`decision_models_unavailable` alert), or every answer unsure and no Jev key. | Check that the `decision-local` extra is installed and `DECISION_LAYA_ENABLED` is on. |
| A kill switch tripped | A limit was reached, or HALT, or an operator. | [Kill switches](kill-switch.md): find out why before resuming. |
| Console shows the auth screen | The token belongs to one launch. | Use the URL printed by this launch. |

## Restarting

Restarting is safe at any time:

- Positions, orders and cash are rebuilt from the events.
- Kill switches, start-of-day equity, loss streaks and breaches persist; a restart never
  resets a limit.
- The lifecycle joins the current state.
- A restart inside the entry window runs the decision cycle again; orders already placed
  carry deterministic ids and are not placed twice. A restart after 09:45 makes no new entries
  that day.

**While the process is down, nothing is monitored.** In paper trading the protective stops
live in the simulated broker, which only sees quotes while the process runs. After a restart,
a stop the price moved through fills at the first quote it sees, which can be worse than the
stop. Keep downtime short in market hours.

Start the scheduled task by hand with `schtasks /Run /TN "RakshaQuant paper session"`, or run
`uv run python scripts/run_live_trading.py --mode web`.

## After a P0

1. **Fix it** on the branch, with a test that fails before the fix.
2. **Run the quality gates**, then replay the affected day (`scripts/replay_day.py`) to check
   the fix against the recording.
3. **Log it** in the month's incident log, `docs/experiments/2026-10-month1-incidents.md`
   (create it at the first incident). Record:
   - the date and time;
   - what happened;
   - its impact on each book;
   - the fix's commit;
   - whether the day's data stays in the analysis.

   Every session records the commit it ran as `ProcessStarted.version`, so the event record
   shows when the fix took effect.
4. **Only P0 fixes** may change the trading-path code during the month run (strategies, risk,
   OMS, simulated broker, decision). Anything else waits until the run ends, so the month's
   data stays comparable with itself.
