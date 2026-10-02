# AI providers and models

How to choose, switch and check the models behind the AI roles and the decision models.
Everything here is configuration in `.env`: switching a provider never needs a code change.
The settings are listed in [settings](../reference/settings.md).

**During the month run**, the veto role's model *is* book C's treatment, and the decision-model
setup is book B's. Changing either mid-run changes the experiment. Do it only as a logged
decision (see [incident](incident.md)), and before a session, not during one.

## LLM roles

| Role | Used by | When |
| --- | --- | --- |
| `veto` | book C's advisor | in the entry window, once per proposal |
| `review` | the nightly post-trade review (stored, never fed back) | after the daily report |
| `label` | teacher labels for decision-model calibration (`scripts/build_decision_labels.py`) | offline, paid: dry run unless `--confirm-spend` |
| `explain`, `research` | reserved (prompt and budget plumbing exist; nothing calls them yet) | - |

Every role is **off** until you set it. A role names a primary model, optional fallbacks and
an optional effort:

```bash
LLM_ROLE_VETO=openrouter:<vendor>/<model>:free      # provider:model, split at the first colon
LLM_ROLE_VETO_FALLBACKS=groq:llama-3.3-70b-versatile # comma-separated, tried in order
LLM_ROLE_VETO_EFFORT=low                             # low | medium | high | max (where supported)
```

Providers, and the key each needs:

| Provider | Model spec | Needs |
| --- | --- | --- |
| OpenAI | `openai:<model>` | `OPENAI_API_KEY` |
| OpenRouter | `openrouter:<vendor>/<model>`, `...:free` for free models | `OPENROUTER_API_KEY` |
| Groq | `groq:<model>` | `GROQ_API_KEY` |
| Anthropic (native SDK) | `anthropic:<model>`, e.g. `anthropic:claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| Ollama (local) | `ollama:<model>` | a running Ollama at `OLLAMA_BASE_URL` |
| Any OpenAI-compatible endpoint | `compat:<model>` | `LLM_COMPAT_BASE_URL` (+ `LLM_COMPAT_API_KEY`) |

An enabled role with an unknown provider or a missing key **fails startup** with exit code 2.
The error names the variable, never its value.

### Switching a model

1. Edit the role's line in `.env`, and add the provider's key if it is new.
2. If the model isn't priced in `src/config/llm_pricing.yaml`, add it (USD per million input
   and output tokens). An unpriced model's spend is recorded as unknown and **doesn't count
   toward the budgets**. Models ending in `:free` and every `ollama:` model cost nothing.
   OpenRouter's reported cost overrides the table.
3. Check it: `uv run python scripts/llm_check.py` makes one tiny call per enabled role through
   the real router and prints `role | provider:model | ok/fail | latency | tokens`.
4. Run `uv run python scripts/check_config.py`; it must be `[READY]`.

The next session uses the new model. Every call records the model that answered and the
prompt version, so the switch is visible in the AI Desk and the daily report.

### What happens when a model fails

The router tries the primary, then each fallback. A timeout (`LLM_TIMEOUT_S`), rate limit,
server error, refusal or invalid answer moves to the next model. After `LLM_BREAKER_FAILURES`
consecutive failures, a model is skipped for `LLM_BREAKER_COOLDOWN_S`. A 429 pauses that
provider until its reset time.

If every model fails, the role **abstains**: book C keeps the deterministic decision and
trades like A. Trading never waits on an LLM. Every attempt, skipped ones included, is an
`LLMCall` event with tokens, latency and cost.

### Budgets

| Setting | Default | Meaning |
| --- | --- | --- |
| `LLM_BUDGET_DAILY_INR` | 200 | all roles, per IST day (0 = unlimited) |
| `LLM_BUDGET_ROLE_DAILY_INR` | `{}` | per role, e.g. `{"veto": 50}` |
| `LLM_BUDGET_PER_DECISION_INR` | 10 | per decision |
| `USD_INR` | 88 | the conversion rate for costs (update it) |

A paid call over a cap is skipped (`budget_exceeded`) and the role abstains. Free models still
run. A restart rebuilds today's spend from the events, so budgets never reset mid-day.
Identical prompts reuse the stored reply (`LLM_CACHE_ENABLED`).

**Privacy:** prompts carry public facts only (the signal, features, typed events), never
positions or P&L. For OpenRouter, `LLM_OPENROUTER_DENY_DATA_COLLECTION=true` (the default)
asks for providers that don't retain data.

## Decision models (book B, announcement typing)

| Model | Where | Setting |
| --- | --- | --- |
| **Laya** (Convai Innovations, open source) | local, CPU | needs the `decision-local` extra; `DECISION_LAYA_ENABLED` (on), `DECISION_LAYA_CHECKPOINT` (`multilingual`, the fastest on CPU) |
| **Jev** (TypeSafe) | remote API | `TYPESAFE_API_KEY`, `TYPESAFE_MODEL` (pinned, `jev-1.13.0`) |

Laya answers first. A question escalates to Jev when Laya's calibrated confidence falls inside
the band `DECISION_ESCALATE_LOW`-`DECISION_ESCALATE_HIGH` (0.35-0.65), when the state is too
long for Laya, or when Laya fails. **Without a Jev key, escalated questions abstain.** An
unsure answer is never used as if sure. `DECISION_SHADOW_PCT` (20%) of Laya-only states also go
to Jev to measure agreement, unused.

- **Calibration:** `var/models/calibration.json`, fitted by
  `scripts/calibrate_decision_models.py` on a labelled set; see
  [decision-model-benchmark](../plan/decision-model-benchmark.md) and open question OQ-3 in
  [PROGRESS](../plan/PROGRESS.md).
- **Thresholds:** book B vetoes when the calibrated P(veto) reaches the book's threshold in
  `src/config/experiment.yaml` (0.6).
- **Check:** the AI Desk shows each model's latency, escalation rate and calibration status.
  The `decision_models_unavailable` alert means no model could load, and book B abstains.

Every answer is cached by state and questions, so replays reproduce book B without running a
model.
