# PA Guard SLA Deadline Reset Report

**Status: COMPLETE — update and verification finished.**

- Database: `C:\Users\Admin\Desktop\PA_guard\backend\pa_guard.db`
- SQLite-safe backup: `C:\Users\Admin\Desktop\PA_guard\backend\backups\pa_guard_before_deadline_reset_20261026T153156Z.sqlite3`
- Backup integrity: `ok`; case rows verified: `8`.
- Captured UTC timestamp (T): `2026-10-26T15:31:56+00:00`
- Target deadline (T + 168 hours): `2026-11-02T15:31:56+00:00`
- Active definition from source: status not in `APPROVED`, `DENIED`.
- Active cases proposed: `7`; resolved excluded: `1`; active missing deadlines: `0`.
- Existing non-ISO or timezone-less active deadlines to normalize by replacement: `2`.
- Active cached workflow summaries to invalidate in the same transaction: `4` (PA-1039, PA-1040, PA-1048, PA-1049).

| Case ID | Status | Previous deadline | Proposed deadline (UTC) |
|---|---|---|---|
| PA-1039 | NEW | 2026-10-12T10:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1040 | INTAKE REVIEW | 2026-10-11T16:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1041 | EVIDENCE REVIEW | 2026-10-10T11:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1042 | NEW | 2026-10-10T14:30:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1048 | NEW | 2026-10-26T03:26:00+05:30 | 2026-11-02T15:31:56+00:00 |
| PA-1049 | NEW | Oct 23, 6:30 PM | 2026-11-02T15:31:56+00:00 |
| PA-1050 | AWAITING_SUPERVISOR | Oct 24, 6:30 PM | 2026-11-02T15:31:56+00:00 |

Resolved cases excluded:

| Case ID | Status | Deadline (unchanged) |
|---|---|---|
| PA-1038 | APPROVED | 2026-10-09T17:00:00+00:00 |

The database write will proceed only if this dry-run plan matches the live records at transaction time. The intended transaction changes only `cases.deadline` for still-active listed cases, adds a `SYSTEM` demo-maintenance audit row per updated case, and removes only stale `case_workflow_state` cache rows for those active cases. Historical audit/recommendation records, case fields other than deadline, resolved cases, and all other tables remain unchanged.

## Execution and verification

**Status: COMPLETE — database verification passed.**

- Update timestamp (UTC): `2026-10-26T15:31:56+00:00`.
- Target: `2026-11-02T15:31:56+00:00` (exactly 168 hours after T).
- Eligible and updated: `7`; skipped: `0`; resolved excluded and unchanged: `1`; active cases with missing deadlines: `0`.
- Per-case demo-maintenance audit records added: `7` using actor `SYSTEM`; existing audit records preserved.
- Stale active workflow summaries invalidated: `4` (PA-1039, PA-1040, PA-1048, PA-1049); no other workflow state changed.
- All case fields other than `deadline` matched the pre-update backup; resolved case rows matched exactly.
- Post-commit SQLite integrity check: `ok`; all active deadlines equal the target.
- Source inspection confirms the case-list and case-detail API return `cases.deadline`; risk is computed from that field. Read-only operational-risk verification is recorded below; no live SLA batch was run.
- No application source, SLA threshold, policy, model artifact, case status, assignment, document, or prior audit record was changed.

| Case ID | Status | Previous deadline | Updated deadline (UTC) |
|---|---|---|---|
| PA-1039 | NEW | 2026-10-12T10:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1040 | INTAKE REVIEW | 2026-10-11T16:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1041 | EVIDENCE REVIEW | 2026-10-10T11:00:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1042 | NEW | 2026-10-10T14:30:00+00:00 | 2026-11-02T15:31:56+00:00 |
| PA-1048 | NEW | 2026-10-26T03:26:00+05:30 | 2026-11-02T15:31:56+00:00 |
| PA-1049 | NEW | Oct 23, 6:30 PM | 2026-11-02T15:31:56+00:00 |
| PA-1050 | AWAITING_SUPERVISOR | Oct 24, 6:30 PM | 2026-11-02T15:31:56+00:00 |

## Read-only operational-risk verification

Using captured timestamp T with the existing `assess_case_operational_risk` implementation and read-only SQLite connections:

| Case ID | Status | Remaining hours at T | Operational category |
|---|---|---:|---|
| PA-1038 | APPROVED | -406.5 | RESOLVED |
| PA-1039 | NEW | 168.0 | MEDIUM |
| PA-1040 | INTAKE REVIEW | 168.0 | MEDIUM |
| PA-1041 | EVIDENCE REVIEW | 168.0 | MEDIUM |
| PA-1042 | NEW | 168.0 | MEDIUM |
| PA-1048 | NEW | 168.0 | MEDIUM |
| PA-1049 | NEW | 168.0 | MEDIUM |
| PA-1050 | AWAITING_SUPERVISOR | 168.0 | LOW |

Every active case calculated to exactly 168.0 hours remaining at T. The resolved case remained `RESOLVED`. Existing risk thresholds and blocker adjustments were used unchanged. No SLA agent batch or ML inference was run.

## Execution notes

The first transaction attempt encountered an audit-row verification check error and rolled back before commit. Before retrying, read-only checks confirmed case rows and workflow-cache rows still matched the verified backup and no reset audit rows had been added. The corrected transaction then passed all in-transaction and post-commit checks.

## Files and data changed

- Database data: `backend/pa_guard.db` — seven active-case `deadline` values, seven associated `DEMO_SLA_DEADLINE_RESET` audit records, and four stale active `case_workflow_state` cache rows invalidated.
- Backup created: `backend/backups/pa_guard_before_deadline_reset_20261026T153156Z.sqlite3`.
- Report created: `SLA_DEADLINE_RESET_REPORT.md`.
- Application source/configuration files changed: none.
