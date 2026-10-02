# Kill switches

How to stop trading, flatten, and resume, and what trips the switches on its own. The code is
`src/risk/kill_switch.py`; the codes are listed in [reason codes](../reference/risk.md).

## The model

Each book (A, B, C) has its own switches, in three scopes:

| Scope | Blocks | Tripped by |
| --- | --- | --- |
| **Global** | every entry of the book | the HALT file, the operator, daily loss, drawdown, a reject storm |
| **Broker** | every entry of the book | nothing yet: reserved for a live broker adapter (the paper broker can't fail this way) |
| **Strategy** (e.g. `momentum`) | that strategy's entries | the strategy's daily loss or loss streak, the operator |

Each switch is `ARMED`, `HALT_NEW` or `FLATTEN`:

- **`ARMED`:** trading normally.
- **`HALT_NEW`:** no new entries. Exits (stops, targets, time exits) keep working.
- **`FLATTEN`:** no new entries, and every open position in scope is closed with reduce-only
  market orders through the OMS. The protective stop is pulled first, so nothing is sold twice.

Switches are **persisted and latching**. A trip only ever escalates, and survives a restart
and the next day. **Only an operator's resume re-arms a switch.** The one exception: with
`RISK_REARM_ON_NEW_DAY=true` (off by default), daily-loss trips re-arm at the next IST day.
Drawdown, streak, HALT-file and operator trips never re-arm on their own.

Every change is a `KillSwitchChanged` event (who, why, when), and every operator action is
also a `ControlCommand` event.

## Halt now

Use whichever is quickest. All three take effect on the **next order check**, before any
further order is placed.

1. **The HALT file** works always, even with no console and with the CLI running. Create
   `var/paper/HALT` (`var/<environment>/HALT`); the content doesn't matter. Every book's
   global switch goes to `HALT_NEW`. To flatten everything, put the word `FLATTEN` in the
   file.

   ```powershell
   New-Item var\paper\HALT -ItemType File            # halt new entries
   Set-Content var\paper\HALT FLATTEN                 # halt and flatten
   ```

2. **The web console:** the **HALT** button in the top bar, with a reason. It halts every book;
   the Risk Center halts one. Halting works even in read-only mode.
3. **The API:** `POST /api/risk/halt {"reason": "...", "book": "A"}`. Leave out `book` to
   halt them all.

**Flatten** from the console needs the typed phrase `FLATTEN` and a reason (Risk Center, per
book). Flattening happens on the next risk tick (within 60 s).

## Automatic trips

The risk monitor checks every book every 60 s. Each breach is raised once per IST day:

| Breach | Code | Default action |
| --- | --- | --- |
| Mark-to-market loss vs start of day ≥ `RISK_DAILY_LOSS_LIMIT_PCT` (1%) | `PF_DAILY_LOSS_MTM` | global `FLATTEN` (`RISK_DAILY_LOSS_ACTION`) |
| Equity below its running peak by ≥ `RISK_MAX_DRAWDOWN_PCT` (5%) | `PF_DRAWDOWN` | global `FLATTEN` (`RISK_DRAWDOWN_ACTION`) |
| ≥ `RISK_REJECT_STORM_COUNT` rejects in `RISK_REJECT_STORM_WINDOW_S` | `SYS_REJECT_STORM` | global `HALT_NEW` |
| A strategy's loss today ≥ `RISK_STRATEGY_DAILY_LOSS_PCT` | `STR_DAILY_LOSS` | strategy `HALT_NEW` |
| A strategy's losing streak ≥ `RISK_STRATEGY_MAX_CONSEC_LOSSES` | `STR_CONSEC_LOSSES` | strategy `HALT_NEW` |

A trip raises a WARNING (halt) or CRITICAL (flatten) alert. The console shows it as a toast,
and the next daily report lists it.

## Before you resume

Find out why the switch tripped: the Risk Center lists each switch's reason, actor and time,
and the System screen's log tail shows the context. Then:

- **Daily loss or drawdown:** was the loss real (prices moved) or a data problem (a bad quote,
  stale marks)? A real loss on the limit is the limit working; leave the book halted for the
  day. A data problem is an [incident](incident.md).
- **Reject storm:** read the rejection reasons (Blotter, Rejections tab). Repeated broker
  rejections point to a bug or bad reference data, which is an incident.
- **Strategy streak or daily loss:** decide whether to keep trading the strategy. Resuming it
  acknowledges the streak (it counts from zero again); otherwise it would trip again at the
  next session.

## Resume

Resuming needs the web console. Remove the HALT file first: a global resume is refused while
it exists.

1. If a CLI session is running, stop it (Ctrl-C) and start the web console:
   `uv run python scripts/run_live_trading.py --mode web --no-auto-start`. With no session
   running, the console changes the stored switch state, which applies when the next session
   starts.
2. **Risk Center → RESUME** on the book. Type the phrase `RESUME` exactly (upper case) and
   give a reason.
3. A strategy switch uses the API with its scope:
   `POST /api/risk/resume {"confirm": true, "phrase": "RESUME", "reason": "...", "book": "A", "scope": "strategy", "name": "momentum"}`.
4. Check that the switch shows `ARMED` with actor `web`, and that the `ControlCommand` event
   says `applied`.

During the month run, every resume is part of the experiment's record. Prefer resuming before
the session or after the close, so that books stay comparable within a day.
