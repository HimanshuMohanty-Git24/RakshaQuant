# Daily operations

How to run the paper session day to day, and what to check. All times are IST. For how the
system works, see [architecture](../architecture.md).

## One-time setup (before the month run)

1. **Install.** From the repo root:
   - `uv sync --extra web --extra decision-local`
   - `cd frontend && npm ci && npm run build`

   `decision-local` installs the local decision model (book B). Without it, book B abstains
   on every proposal.
2. **Configure `.env`** (start from `.env.example`). Set `ENVIRONMENT=paper`: other
   environments keep their own state, so the month's events all land in `var/paper/`. Also
   set Telegram if you want the daily summary and the dead-man alert, and the LLM roles you
   use (see [providers](providers.md)).
3. **Check:**
   - `uv run python scripts/check_config.py` must end `[READY]` with no warnings.
   - If any LLM role is enabled, `uv run python scripts/llm_check.py` must show `ok` for
     each one.
4. **Keep the machine awake** from 09:00 to 16:00 on weekdays. Set the power plan to never
   sleep while plugged in, and turn off automatic restarts for updates during market hours.
5. **Schedule it:** `scripts/install_windows_task.ps1`. It registers the weekday 09:05 start
   and the 10:00 and 13:00 dead-man checks, and changes nothing else. Run it once, in a
   PowerShell window as your user.
6. **Freeze the experiment.** After the first session, do not change any of the following until
   the month ends:
   - `src/config/experiment.yaml`;
   - any `RISK_*` limit;
   - the code under `src/strategies`, `src/risk`, `src/oms`, `src/brokers/simulated` and
     `src/decision`.

   The pre-registration
   ([docs/experiments/2026-10-month1-preregistration.md](../experiments/2026-10-month1-preregistration.md))
   records their fingerprints, and every risk decision records the limits hash. A P0 fix is
   the only exception; log it as described in [incident](incident.md).

## Every trading day

**Automatic.** At 09:05 Task Scheduler opens a console window running today's session behind
the web console (`run_live_trading.py --mode web --exit-after-session`). The window prints
the console URL once, with this launch's token. Open that URL; an old URL won't work. On a
holiday or a weekend the process logs `HolidaySkipped` and exits at once.

| Time | State | Check |
| --- | --- | --- |
| 09:05-09:15 | `PRE_OPEN` | Top bar shows PAPER, the session date and `PRE_OPEN`. No CRITICAL alert about reference data or history. A `history_lagging` warning is normal before Yahoo settles yesterday's close; those symbols just don't trade today. |
| 09:15 | `OPEN` | Data delay under a few minutes (YFinance is delayed). All kill switches `ARMED`. |
| 09:20-09:45 | `ENTRY_WINDOW` | One decision cycle. **Decisions** shows each signal with every book's disposition. Orders fill within a minute or two. |
| 09:45-15:30 | `MONITOR` | Exits only. Glance at the Command Center now and then: positions have stops, there are no new CRITICAL alerts, and the risk meters sit below their limits. |
| 10:00, 13:00 | - | The dead-man check sends Telegram only if the engine's heartbeat is over 3 minutes old. |
| 15:45 | `REPORT` | Telegram summary, and the daily report in `var/reports/<date>.md`. |
| 15:50 | `EXIT` | The session ends and the scheduled console shuts itself down (exit code 0). To browse the day afterwards, run the web console without a session (see below). |

**After the close**, read the day's report. If any of the following is wrong, start the
[incident runbook](incident.md):

- **Integrity:** every closed trade is explainable from its decision, with zero
  reconciliation drift.
- **Rejections:** risk rejections by reason code, and advisor vetoes.
- **Alerts:** no CRITICAL alerts.
- **Costs:** charges in line with the turnover.
- **AI:** spend within budget; the abstain and fallback rates are explained (a role or the
  decision model being down shows up here).

**Regenerate a report** at any time with
`uv run python scripts/daily_report.py --date YYYY-MM-DD`. Add `--send` to resend the
Telegram summary. Reports are built from the event store alone.

## Weekly

- **Back up** `var/paper/rakshaquant.db`, `var/tape/` and `var/reports/` to another disk,
  ideally while no session is running. The database is the experiment's record.
- **Free space:** check disk space; the tape grows by a few MB per day.
- **AI spend:** review the AI Desk's spend by role and model. Update `USD_INR` if the rate has
  moved: costs are computed in USD and converted.
- **Replay one recorded day** as a spot check:
  `uv run python scripts/replay_day.py --date YYYY-MM-DD --source var/paper/rakshaquant.db`.
  It must complete; it uses recordings and cached AI answers only.

## Calendar and dates

- `src/config/nse_calendar.json` covers **2026 only**. The system refuses to trade on a date
  it doesn't cover, so add 2027's holidays when NSE publishes them in December.
- Muhurat trading sessions stay closed until their timings are filled in.
- Session timings come from the calendar; nothing is hard-coded.

## Running by hand

| Purpose | Command |
| --- | --- |
| Today's session with the browser console | `uv run python scripts/run_live_trading.py --mode web` |
| Today's session in the terminal | `uv run python scripts/run_live_trading.py` |
| Web console without starting a session | `uv run python scripts/run_live_trading.py --mode web --no-auto-start` |
| A synthetic demo (its own `var/demo/`) | `uv run python scripts/run_live_trading.py --demo` (add `--mode web` for the browser) |

Only one process can hold an environment at a time; a second one exits with code 3. With the
CLI running, the HALT file is the only control. To use the web controls, stop the session
with Ctrl-C and start the web console instead.
