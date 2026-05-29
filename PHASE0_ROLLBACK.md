# Phase 0 Auto-DQ Rollback Instructions

Created: 2026-05-27. Last updated: 2026-05-28 (Gate 2 live).

## Baseline snapshots

| Checkpoint | SQL backup | CSV snapshot | State |
|---|---|---|---|
| Pre-Phase 0 | `prospect_matrix_pre_phase0_20260527-215836.sql` | `...csv` | 998 rows, 0 DQ'd |
| Post-Gate-1 | — | — | 46 DQ'd, 952 active |
| Pre-Gate-2 | `prospect_matrix_pre_gate2_20260528-213939.sql` | — | 46 DQ'd, 952 active |
| **Post-Gate-2 (current)** | `prospect_matrix_post_gate2_20260528-222304.sql` | `prospect_matrix_status_post_gate2_20260528-222304.csv` | **142 DQ'd, 854 active** |

To roll back to post-Gate-2 baseline (e.g. if a future scraper run goes wrong):
```bash
psql -U delta -h localhost -d delta_prospect -c "DROP TABLE prospect_matrix CASCADE;"
psql -U delta -h localhost -d delta_prospect -f backups/prospect_matrix_post_gate2_20260528-222304.sql
```

If Phase 0 work needs to be reverted, follow these steps.

---

## Code rollback (if scraper or other code is broken)

```bash
git checkout master
git branch -D phase-0-auto-dq        # delete local branch
```

Or if you want to preserve the work for inspection, just leave the branch alone and stop deploying it.

If the branch was already merged to master and is causing problems:
```bash
git revert <merge-commit-sha>
git push origin master
```

---

## Database rollback (if retroactive sweep DQ'd wrong prospects)

**Backup files (local):**
- `backups/prospect_matrix_pre_phase0_20260527-215836.sql` — full SQL dump, 493 KB
- `backups/prospect_matrix_status_pre_phase0_20260527-215836.csv` — ticker/company/status/dq_reason snapshot, 38 KB, 998 rows

**Full restore (nuclear — wipes all prospect_matrix changes since the backup):**
```bash
psql -U delta -h localhost -d delta_prospect -c "DROP TABLE prospect_matrix CASCADE;"
psql -U delta -h localhost -d delta_prospect -f backups/prospect_matrix_pre_phase0_20260527-215836.sql
```

WARNING: This nukes any prospect_matrix changes made AFTER 2026-05-27 21:58, including new ASX scrapes and manually updated statuses. Only use if the bad state is worse than losing recent changes.

**Safer — surgical status restore for specific tickers:**
```sql
UPDATE prospect_matrix
SET status = 'qualified', dq_reason = NULL
WHERE listing_id IN (
  SELECT id FROM asx_listings WHERE ticker IN ('TICKER1', 'TICKER2')
);
```

Cross-reference `backups/prospect_matrix_status_pre_phase0_20260527-215836.csv` to confirm original status values before running.

---

## Scraper rollback (if Phase 3 broke the scraper)

The Phase 0 prompt creates a timestamped backup of the scraper file. Restore with:
```bash
cp asx_scraper.py.backup.YYYYMMDD-HHMMSS asx_scraper.py
```

Confirm by running the smoke test from Phase 3a.iii.

---

## Full nuclear option

If everything is broken and nothing else works:
1. `git checkout master && git pull origin master`
2. Restore DB from `backups/prospect_matrix_pre_phase0_20260527-215836.sql` (see above)
3. Verify scraper is the master-branch version (not the Phase 0 modified version)
4. Confirm Delta Prospect System loads on deltaprospectmatrix.com with expected prospect counts
5. Tell Chris what broke

---

## Known-good baseline state (as of 2026-05-27)

- Git: master @ `416b7b8` (feat: DQ hazard-stripe banner)
- DB: 998 rows in prospect_matrix, 4 manually DQ'd (ORP, FZR, EFE, BMH)
- Snapshots loaded: A1M, BLU, ANO, INR (ORP snapshot deleted)
- Railway deployment: pulling from master
