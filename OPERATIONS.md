# Operations Runbook — Delta Prospect System

Plain-English guide for running the system day to day. Commands assume you are
in the project folder with the virtual environment active (`venv\Scripts\activate`
on Windows). On Railway, run the Python commands from your own machine with
`DATABASE_URL` set to the Railway Postgres connection string.

---

## 1. How the pieces fit

| Piece | What it does | Where to look |
|---|---|---|
| API (`uvicorn api:app`) | Serves the dashboard and REST API | `/api/health`, `/api/health/live` |
| Worker (thread inside the API by default) | Runs refresh, enrichment, contact and AI jobs from the `background_jobs` table | Operations page → Background jobs, `/api/jobs` |
| Scheduler (inside the API) | Queues the daily ASX refresh (09:00 Sydney) and runs hourly alert checks | logs: "Automatic ASX refresh scheduled" |
| Migrations (`python migrations.py`) | Applies `schema.sql` once, then `migrations/NNN_*.sql` in order | `schema_migrations` table, `/api/health` → `migrations_pending` |

Only one scheduler and one worker should run per database. With one Railway
container that is automatic. If you ever run two API replicas, set
`RUN_WORKER=false` and `SCHEDULER_ENABLED=false` on all but one.

---

## 2. Daily checks (2 minutes)

Open **Operations** in the app (or `GET /api/ops/health`):

1. **Last successful refresh** — should be within 36 hours. Red = investigate (section 6).
2. **Market data fresh (36h)** — target ≥ 95%. Lower means the ASX detail pull is failing or rate-limited.
3. **Jobs** — nothing `failed` in the last 24h, no stale leases. A stale lease means the worker died mid-job; it will be re-queued automatically, but check why.
4. **Recent events** — anything `error` severity.
5. **AI spend** — within the monthly budget if one is set.

Then open **Review Queue** for the analyst work of the day.

---

## 3. Alerts

Hourly checks (`alerts.check_alert_conditions`) raise at most one event per type per 24h:

| Event | Meaning | First action |
|---|---|---|
| `scheduled_refresh_failed` | Last night's ASX refresh failed | Operations → Jobs → read the error. Usually ASX feed/rate limit. Re-run with the Dashboard "Refresh" button. |
| `refresh_stale` | No successful refresh for 48h | Is the API/worker running? `GET /api/health`. Check `AUTO_ASX_REFRESH_ENABLED`. |
| `jobs_stuck` | A running job's lease expired >10 min ago | Worker process dead or hung. Restart the service; the job is re-queued (attempts permitting). |
| `jobs_backlog` | Jobs queued >6h | Worker not running (`RUN_WORKER=false` without a separate `python worker.py`?). |
| `ai_budget_threshold` | ≥80% of `AI_MONTHLY_BUDGET_USD` used | Decide: raise budget or stop paid analysis for the month. |
| `job_failed` | A job failed permanently | Read the error on the Operations page. |

To receive them outside the app set `ALERT_WEBHOOK_URL` to a Slack/Teams incoming-webhook URL.
Assign a named person to watch alerts; an unwatched alert is not a process.

---

## 4. Backups and restore

**Take a backup** (local or Railway with `DATABASE_URL` set):

```bash
python scripts/backup_db.py                 # -> backups/delta_prospect_<timestamp>.dump
```

**Prove the backup restores** (quarterly, and before any risky change):

```bash
python scripts/backup_db.py --verify        # backs up, then restores into delta_prospect_restore and counts rows
```

**Restore for real** (never over the live DB name — restore into a new DB, check it, then repoint `DATABASE_URL`):

```bash
python scripts/backup_db.py --restore backups/delta_prospect_<timestamp>.dump --into delta_prospect_recovered
```

Recovery targets (proposed, confirm with the operator): lose at most 24 hours
of data, be back within 4 hours. A nightly backup meets the first; the
rehearsed restore meets the second. Both actions write an `ops_events` row so
the Operations page shows when they last happened.

Railway also keeps its own Postgres backups; this script is the one you control.

---

## 5. Deploying a change

1. Merge to `master` — CI must be green (tests, lint, build, docker build).
2. Railway builds the image and starts it. On start the app:
   - applies pending migrations (`AUTO_MIGRATE=true`),
   - seeds any **new** snapshot files (never overwrites analyst edits),
   - starts the worker thread and scheduler.
3. Check `/api/health` → `status: healthy`, `migrations_pending: 0`.
4. Rollback: redeploy the previous image. Migrations are additive; old code runs fine against the newer schema. If a migration must be undone, restore the pre-deploy backup into a new database (section 4).

Take a backup **before** deploying anything that includes a new migration file.

---

## 6. Common problems

**Refresh fails with "ASX listings feed could not be found / temporarily unavailable"**
The ASX CSV endpoint changed or is down. Existing data is untouched. Retry later; if it persists for 2 days, the URL in `asx_scraper.py` needs updating.

**Enrichment runs but finds nothing**
The ASX announcements API returns at most 5 recent items per company. Companies with no recent announcements are skipped (`skip` count). That is expected.

**Deep analysis says "budget reached"**
`AI_MONTHLY_BUDGET_USD` is set and the month's reservations + spend would exceed it. Raise it or wait. Spend is estimated from tokens × list price; reconcile monthly with the Anthropic invoice.

**Deep analysis result reused with "evidence unchanged"**
Nothing new to analyse (same signals and documents as last time, within `AI_CACHE_MAX_AGE_DAYS`). Use *force* (`POST .../deep-analysis/start?force=true`) if you really want a fresh run.

**A company is disqualified but has recovered**
It appears in Review Queue → "DQ review". Reinstate (returns to `qualified`) or Keep DQ. Nothing is reinstated automatically.

**Duplicate AI findings from the old time-based keys**
```bash
python scripts/dedupe_ai_signals.py            # report only
python scripts/dedupe_ai_signals.py --apply    # mark extras superseded + rescore
python scripts/dedupe_ai_signals.py --undo     # reverse
```

**Score looks wrong**
Open the company → "Why this score" shows v1, v2, pillar contributions, freshness and exclusions. `POST /api/prospects/{id}/rescore` recalculates. Switching the displayed policy: Operations → Scoring policy (run `python scripts/compare_score_policies.py` first and review the big movers).

---

## 7. Monthly routine

- [ ] Reconcile `GET /api/metrics/costs` against the provider invoice; note the difference.
- [ ] Review `GET /api/metrics/funnel`: stage counts, lost reasons, cohort lag. Sample sizes are small — describe, don't conclude.
- [ ] Review rejected signals (`verification_status = analyst_rejected`) for rule-engine patterns that keep misfiring.
- [ ] Re-run `python scripts/compare_score_policies.py --csv tmp/scores.csv`; if v2 is being considered, label the evaluation set (`python scripts/evaluate_ranking.py --export evaluation/evaluation_set.csv`) and check precision@20.
- [ ] `python scripts/backup_db.py --verify` (restore rehearsal).
- [ ] `pip list --outdated` / `npm outdated` in `frontend/`; update in a branch, let CI run.
- [ ] Check `python scripts/explain_hot_queries.py` if the Lead Matrix feels slow (>1s).

---

## 8. Environment variables that matter operationally

See `.env.example` for the full list. The ones to know:

| Variable | Default | Effect |
|---|---|---|
| `RUN_WORKER` | true | Run the job worker inside the API process |
| `SCHEDULER_ENABLED` | true | Queue the daily refresh + hourly alert checks |
| `AUTO_MIGRATE` | true | Apply pending migrations at startup (false → refuse to start if any pending) |
| `SNAPSHOT_SEED_MODE` | insert_missing | `overwrite` refreshes seeded rows (analyst-edited rows are never touched) |
| `AUTH_USER` / `AUTH_PASSWORD` | unset | Basic auth. Unset in production logs a loud warning; the Ops page shows a banner. |
| `AI_MONTHLY_BUDGET_USD` | 0 (no ceiling) | Hard stop for paid analysis |
| `ALERT_WEBHOOK_URL` | unset | Where warnings/errors are pushed |
| `LOG_FORMAT` | plain | `json` for structured logs (request id on every line; secrets scrubbed) |
