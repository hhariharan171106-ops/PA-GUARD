# PA Guard AI Agent Health Report

**Project:** `C:\Users\Admin\Desktop\PA_guard`  
**Verification date:** 2026-10-26  
**Database:** `C:\Users\Admin\Desktop\PA_guard\backend\pa_guard.db`  
**Scope:** Read-only verification of case data; isolated in-memory tests and synthetic model inference. No live case, assignment, document, audit history, model artifact, policy, or SLA setting was changed during this verification.

## Results at a glance

| Component | Result | Evidence |
|---|---|---|
| SLA calculations | **PASS** | Seven active deadlines match the reset target; none are missing or expired. Read-only operational-risk and threshold checks passed. Isolated recommendation endpoint and focused tests passed. |
| SLA explanation | **LIVE LLM** | `rag.explain_with_ollama` returned an actual Qwen explanation in 25.8 seconds against a 30-second configured timeout. It describes remaining time, risk, and next action, but does **not** include the calendar deadline date. |
| SLA risk ML model | **PASS** | The saved v2 artifact loaded and predicted for an isolated synthetic feature row: 8.06% probability, class 0/no breach, model `sla-breach-random-forest-v2`. It is unvalidated synthetic output. |
| Evidence agent | **NOT VERIFIED** | An isolated in-memory API call returned `NEEDS_REVIEW` and `needs_human_review=true`, with no unsupported claim of clinical sufficiency. The fuller document/policy test fixtures could not run because of Windows temp-directory permission errors. |
| Escalation agent | **PASS** | Isolated agent endpoint, Level 2 state/routing tests, and synthetic escalation threshold checks passed. No real notification was sent. |
| Recovery agent | **PASS** | Isolated preview endpoint and preview-safety test passed. Synthetic checks confirmed retry is blocked after the configured attempt limit. No recovery action or notification was executed. |
| Ollama generation | **PASS** | A harmless direct request to `qwen3.5:latest` returned actual text in 22.52 seconds. The application wrapper also returned actual generated text in 25.8 seconds. Both were below the 30-second timeout. |
| Frontend API connectivity | **PASS** | Vite root and the same-origin `/api/health` proxy returned HTTP 200; frontend TypeScript check passed. Authenticated case interaction in a browser was not tested. |

## Deadline reset verification

The configured database path was resolved from `backend/config.py` as `backend\pa_guard.db`; this check opened it in SQLite read-only mode. `PRAGMA integrity_check` returned `ok`.

The application consistently defines resolved cases as `APPROVED` or `DENIED`. The only authoritative case deadline column is `cases.deadline`. The prior reset report records T as `2026-10-26T15:31:56+00:00` and the target as `2026-11-02T15:31:56+00:00`.

- Active cases: **7**; all 7 have the exact target deadline.
- Resolved cases: **1**; `PA-1038` remains `APPROVED` with its original deadline, matching the pre-reset backup.
- Missing deadlines: **0**.
- Expired or invalid active deadlines: **0** as of this verification.
- The four stale active workflow summaries invalidated during the reset remain absent; no new active case workflow was run here.

At the captured reset instant T, the existing read-only `assess_case_operational_risk` implementation returned 168.0 hours for every active case. Its current categories were `MEDIUM` for PA-1039, PA-1040, PA-1041, PA-1042, PA-1048, and PA-1049, and `LOW` for PA-1050. These categories include existing blocker and assignment rules; they are deterministic operational risk, not ML output.

Read-only time-shift checks against PA-1050 used the actual configured SLA settings and did not change its record: 11 hours remaining returned `LOW` with no escalation; 9 hours remaining returned `CRITICAL` with escalation; a deadline just crossed returned `BREACHED` with escalation. Rule version: `sla-operational-risk-v1`.

## SLA agent and explanation

The Dashboard action is `apps/pa-guard/src/pages/index.tsx` → `pa-context.tsx::runSlaAgent` → `pa-api.ts::runSlaAgent` → `POST /api/agents/sla/run` → `backend/main.py::run_sla` → `backend/agents.py::run_sla_agent` → `run_sla_rag` LangGraph. The batch route writes recommendation and audit rows, so it was **not** called against the demo database. The focused isolated test exercised the per-case `/api/agents/sla/cases/{case_id}/recommend` route with an in-memory SQLite database and mocked model text.

The deterministic `agents.py::calculate_rules` path uses the stored deadline, configured warning/urgent thresholds, and unresolved evidence to produce `risk_level`, `remaining_time`, `rule_reason`, and `recommended_action`. `backend/sla_deterministic_risk.py::assess_case_operational_risk` separately derives operational urgency and escalation from the same deadline and current workflow blockers. Neither value is the ML prediction.

The live model explanation wrapper returned this actual generated text for a synthetic case:

> The system indicates a low risk level for this case. The rule states that the deadline has 168 hours remaining with no pending evidence issues. You should continue your assigned workflow and monitor the timeline. Please note that any machine learning result is an estimate and requires human review before final action.

The response named remaining time, the rule-based risk, and a next step. It did not name the actual calendar deadline. Source inspection explains why: `rag.explain_with_ollama` forwards only `risk_level`, `remaining_time`, `rule_reason`, and `recommended_action`; `calculate_rules` does not put the absolute timestamp into those explanation facts. The UI displays the saved case deadline separately, and `agent-center.tsx::recommendationMessage` displays the recommendation, rationale, and remaining time. The explanation’s omission of the calendar date remains a user-facing limitation; no application logic was changed for this verification task.

## Experimental SLA risk ML model

`backend/models/sla_breach_risk_v2.joblib` exists (8,972,131 bytes). `SlaRiskModel._load()` successfully loaded it and validated the v2 model version, fitted `DictVectorizer` + `RandomForestClassifier` pipeline, seven expected features, and binary class labels `0` and `1`. `predict()` maps probabilities by the classifier’s actual `classes_`, uses a 0.35 breach decision threshold, and labels the result unreliable/synthetic.

A synthetic input (168 hours remaining; synthetic payer/service; `NEW`; zero unresolved documents, workflow failures, or pending tasks) produced:

- Model version: `sla-breach-random-forest-v2`
- Predicted class: **0 — no predicted breach**
- Breach probability: **0.0806** (8.06%)
- Decision threshold: **0.35**
- Risk band: `LOW`
- Reliability: **false** — trained on synthetic simulated outcomes and not validated against representative real authorization outcomes.

This is a genuine model inference on a synthetic feature row, not a prediction for a real case. The live read-only endpoint `GET /api/cases/{case_id}/sla-risk-prediction` was inspected; its implementation selects case fields and calls the model without writes. A local request for PA-1048 returned HTTP 401 (`Log in to access approval operations`) because no existing browser token was available. No login session was created, and no live-case probability is reported.

## Evidence, escalation, and recovery

**Evidence:** An isolated in-memory FastAPI call to `/api/agents/evidence/cases/PA-1042/review` returned HTTP 200, status `NEEDS_REVIEW`, `needs_human_review=true`, required documents `Clinical notes` and `Conservative treatment history`, and no found or possible evidence matches. The explanation was explicitly mocked for this test. It did not call a real LLM or prove an uploaded document’s OCR/policy match. Focused disk-backed evidence-policy tests could not complete because their temporary fixture failed with `PermissionError`; full document/policy safeguards therefore remain **not verified** in this run.

**Escalation:** The isolated agent endpoint and both `backend/test_level2_workflow.py` tests passed. These exercise role/state checks, escalation persistence, eligible Level 2 routing, manager allocation visibility, and supervisor queue visibility in isolated/in-memory state. A separate pure-rule synthetic check returned `LEVEL_2` / Manager / human approval at 9 hours under the current configured threshold, and `NONE` at 11 hours. No live case was escalated and no notification was sent.

**Recovery:** The isolated FastAPI recovery route returned `RECOMMENDATION_ONLY`. `test_recovery_preview_cannot_enter_execution` passed. A synthetic rule check confirmed a notification retry may be recommended initially but becomes disallowed after two same-error attempts; a preview remains `RECOMMENDATION_ONLY`, and `execute_recovery` takes no action from that state. The in-memory agent route also exercised its recovery/audit path. No external notification or destructive operation was executed.

## Connectivity and frontend display

- `GET http://127.0.0.1:8000/api/health`: HTTP 200.
- `GET http://127.0.0.1:5173/`: HTTP 200.
- `GET http://127.0.0.1:5173/api/health`: HTTP 200 through Vite’s `/api` proxy.
- `GET http://127.0.0.1:8000/api/cases/PA-1048/sla-risk-prediction`: HTTP 401 without an existing session; this is not a connectivity failure.
- `npm run typecheck` in `apps/pa-guard`: passed.

`apps/pa-guard/src/lib/pa-api.ts::mapRecommendation` maps the API risk, remaining time, explanation, recommended action, owner, and human-review fields. `apps/pa-guard/src/pages/agent-center.tsx::recommendationMessage` renders the rationale, rule status, remaining time, owner, priority, explanation source, and policy references. Type checking and same-origin proxy health passed; no browser sign-in or end-to-end UI click was performed.

## Focused tests and limitations

- **21 passed** in the successful isolated run: all 12 `backend/test_case_explanation.py` tests; 7 selected `backend/test_agent_readiness.py` tests, including all four agent routes in isolated SQLite and recovery preview safety; both `backend/test_level2_workflow.py` tests.
- **Read-only rule checks passed** for current deadline calculations, threshold crossings, escalation recommendation levels, retry limits, and preview-only recovery. The demo database was opened read-only for these calculations.
- **Disk-backed test-fixture attempt did not complete:** 18 failed, 20 passed, 19 errors in the combined attempt. The setup/teardown failures include Windows `PermissionError: [WinError 5]` creating or cleaning test temporary directories and a test fixture’s temporary `documents` directory. Since fixture setup failed and contaminated later test state, those failures do not establish product assertion failures. The disk-backed SLA/evidence tests remain unverified.
- The combined attempt left generated test scratch under `backend\.ai-agent-verify-temp`; the environment rejected the cleanup operation. These are test-only temporary artifacts, not application database records. No production database file was used by those test fixtures.
- The model was loaded and inferred directly; model-training tests were intentionally not run.
- The live `/api/agents/sla/run` batch was not called because it records recommendation/audit rows. No tests, builds, or migrations touched the demo database.

## Files and data changed during this verification

- Updated `AI_AGENT_HEALTH_REPORT.md`.
- No PA Guard source, tests, configuration, model artifact, case rows, audit records, or database contents were changed during this verification. The earlier deadline-reset database change and its backup are documented separately in `SLA_DEADLINE_RESET_REPORT.md`.

## UI clarity update — Task and decision history

**Date:** 2026-10-26

### Root cause and data flow

The Case Details page renders the **Task and decision history** card in `apps/pa-guard/src/pages/case-details.tsx`. It receives audit events from `usePa().audit`; `backend/main.py::list_audit` returns `SELECT * FROM audit_events`, `apps/pa-guard/src/lib/pa-api.ts::getAudit` maps each `detail` field without parsing it, and the component previously interpolated `event.detail` directly into a paragraph.

`audit_events.detail` is a SQLite `TEXT` field. Source writers store different shapes: human notes and some workflow events are plain text; agent decisions and telemetry may be `json.dumps(...)` objects with fields such as `risk_level`, `next_action`, `reason`, policy references, or diagnostic values. Conversation-like message arrays are also supported by the formatter when present; they are not assumed to be the format of every event. The existing `formatAuditDetails` helper was used on the Audit page but not in this history card.

### Change

The history card now uses `formatTaskHistoryDetail` for presentation only. It displays recognized conversation arrays as separate entries with speaker and timestamp where provided, uses readable key/value labels for structured decisions, and falls back to the existing safe audit formatter for nested or unfamiliar objects. Plain text is preserved; malformed JSON remains visible as text; null/empty details receive a short empty-state label. Long messages and technical details are collapsed behind accessible “Show full details” disclosures. The card also shows a source label derived from the existing audit `decision_type` and `action`, while keeping the recorded action, actor, timestamp, and humanized decision type visible. AI recommendations are labeled as recommendations, not completed human decisions.

No audit rows, API response shape, backend behavior, workflow, or application logic was changed.

### Files changed for this UI update

- `apps/pa-guard/src/pages/case-details.tsx` — renders formatted history entries and expandable long details.
- `apps/pa-guard/src/lib/task-history-format.ts` — presentation formatter for conversation and mixed audit detail formats; delegates generic object formatting to the existing `audit-format.ts` helper.
- `apps/pa-guard/src/lib/task-history-format.test.mjs` — representative formatter fixtures.
- `AI_AGENT_HEALTH_REPORT.md` — this update.

### Validation

- `node --experimental-strip-types src/lib/task-history-format.test.mjs` — passed: conversation messages, structured decisions, nested objects/arrays, plain text, malformed JSON, null/missing fields, and source labels.
- `node --experimental-strip-types src/lib/audit-format.test.mjs` — passed: existing audit formatter regression checks.
- `npm run typecheck` in `apps/pa-guard` — passed.
- No live browser verification was performed. No case history was read or changed for this UI check.

### Remaining limitations

Conversation rendering recognizes common `messages`, `conversation`, `conversation_history`, `chat_history`, and `transcript` arrays, including role/content and wrapped `data.content` message shapes. Unknown schemas are shown through readable key/value fallback rather than assigned invented meanings. Malformed strings are preserved as provided, so genuinely malformed technical payloads may still look technical inside the expandable history entry.
