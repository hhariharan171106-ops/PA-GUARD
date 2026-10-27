# PA Guard backend prototype

## SLA breach-risk model

### Shifted synthetic comparison and model card

Run `python backend/evaluate_sla_shifted.py` from the repository root to
reproduce the synthetic-only comparison with fixed seeds. It writes a separate
experimental artifact to `backend/models/sla_shifted_experiment.joblib` and
updates `docs/model_card.json` and `docs/MODEL_CARD.md`; it refuses to overwrite
that artifact. The shifted test set uses a separate generator seed and changes
the synthetic payer, service, and case-status mixes, duration noise, and
deadline range. This is a simulator stress test, not real-world validation.
The live endpoint continues to use its separately versioned v2 model.

`GET /api/sla-risk-model-card` returns the same synthetic model-card JSON for
review. SLA risk predictions in the frontend carry a **Synthetic model** badge.
Neither the experimental comparisons nor live synthetic predictions are
validated against real authorization outcomes.

`sla_risk_synthetic_data.py` generates reproducible **synthetic development
cases**. A stochastic simulated future resolution duration is compared with
the positive time remaining at prediction; the duration itself is excluded
from model features. This data is not real authorization history and cannot
establish real-world performance.

`train_sla_risk_model.py` is an **experimental evaluation pipeline**. It uses
stratified 60% train / 20% validation / 20% sealed test splits, fits
preprocessing on training rows only, selects a breach-recall-constrained
threshold on validation data, and reports a prior-only baseline, an
hours-remaining-only diagnostic, classification metrics, confusion matrix,
false positives/negatives, ROC-AUC, PR-AUC, Brier score, and calibration bins.
It writes an experimental v3 artifact and JSON/Markdown reports. The API
continues to load only the existing v2 model; experimental training does not
replace or activate the model served by the application.

Generate a reproducible CSV dataset (synthetic features and target only; no
simulated resolution duration):

```powershell
Set-Location 'C:\Users\Admin\Desktop\PA_guard'
& '.\.venv\Scripts\python.exe' '.\backend\sla_risk_synthetic_data.py' `
  --output '.\backend\reports\sla_risk_synthetic_data.csv' `
  --samples 6000 `
  --seed 20261018
```

Run the experiment and save a separate model plus evaluation reports:

```powershell
& '.\.venv\Scripts\python.exe' '.\backend\train_sla_risk_model.py'
```

The outputs are `backend\models\sla_breach_risk_v3_experimental.joblib`,
`backend\reports\sla_risk_evaluation.json`, and
`backend\reports\sla_risk_evaluation.md`. Training refuses to overwrite an
existing model artifact; choose a new `--output` filename to repeat training
without replacing it. Reports are regenerated at the specified report paths.
Review the full assumptions and limitations before interpreting the metrics.
Do not switch the live model based on simulator performance.

Because the default artifact was generated for this prototype, use a unique
artifact and report filename for subsequent runs:

```powershell
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
& '.\.venv\Scripts\python.exe' '.\backend\train_sla_risk_model.py' `
  --output ".\backend\models\sla_breach_risk_v3_experimental-$runId.joblib" `
  --json-report ".\backend\reports\sla_risk_evaluation-$runId.json" `
  --markdown-report ".\backend\reports\sla_risk_evaluation-$runId.md"
```

The existing `GET /api/cases/{case_id}/sla-risk-prediction` endpoint now returns
the predicted breach flag, estimated probability, model version, and global
feature-importance indicators while retaining its existing risk fields. It
returns `503` if the model artifact is missing and does not train on demand.
Predictions are available only for unresolved cases before their deadlines;
already-passed deadlines and resolved cases use deterministic SLA handling,
not a prospective ML estimate. ML estimates do not replace SLA policies,
trigger escalations, or make authorization decisions.

The frontend's **Agent conversation** page (`/agent-center`) runs the selected
case through the existing FastAPI routes for the SLA, evidence, escalation,
SLA-risk, and recovery reports. The SLA, evidence, escalation, and recovery
LangGraph agents are exposed individually through FastAPI and all four are
checked by `GET /api/agents/status`. Their summaries are presented as readable
chat messages with links to the existing human-review pages; the UI does not
render raw agent JSON. The local explanation model defaults to
`qwen3.5:latest`; set `OLLAMA_URL` and `OLLAMA_MODEL` before starting FastAPI to
override it. The status endpoint reports whether Ollama is reachable and the
configured model is installed. Each report also identifies whether its wording
came from Ollama or a deterministic fallback. Generation has a 90-second
timeout by default (`OLLAMA_GENERATION_TIMEOUT_SECONDS` can override it), and
generated summaries are capped at 64 tokens. Already-breached SLA cases use a
deterministic, human-readable explanation rather than waiting for an LLM to
restate a settled deadline status.
The default embedding model remains `nomic-embed-text`; if it is not installed,
policy retrieval uses the existing local hash-embedding fallback. The embedding
availability check is cached for the backend process so startup does not wait
on repeated requests for a missing embedding model.
Recovery in this briefing is explicitly preview-only and does not execute a
recovery action. The chat is an agent-report briefing, not an unrestricted
clinical question-answering system. The transcript exists in page memory only
and is cleared when navigating away or reloading.

On a case details page, documents are staged in the browser with an individual
category and are not uploaded when selected. The final
**Submit queued documents & run workflow** action sends the selected files in a
single multipart request to `POST /api/cases/{case_id}/documents/batch`. The
batch endpoint validates, stores, and extracts each file independently and
returns saved documents plus per-file failures for partial success. Each file
has a stable `submission_ids` multipart value, so retries return already-saved
documents instead of creating duplicates. The original single-file
`POST /api/cases/{case_id}/documents` endpoint remains available for existing
clients. After persistence, the page calls
`POST /api/agents/cases/{case_id}/workflow` on the saved documents only.
The workflow reuses the existing evidence, SLA, SLA-risk, and escalation
services, runs the existing experimental evidence-gap prediction, and runs
recovery only as a preview when a prior workflow failure is recorded. Its
result includes per-stage status, document/OCR state, an evidence and
escalation summary, and an LLM-generated explanation grounded in those results.
The evidence prediction remains explicitly experimental and cannot establish
clinical presence or absence. If Ollama is unavailable, the deterministic
fallback is labeled and the explanation stage is reported as failed. Failed
upload items can be retried without resending documents already saved; rerunning
a failed AI workflow does not upload documents again. The
**Retry failed AI stages** action calls
`POST /api/agents/cases/{case_id}/workflow/retry`; the most recent workflow
result is persisted in SQLite, completed stages are reused, and failed stages
are rerun. Dependent evidence prediction, recovery preview, and the LLM
explanation are refreshed when their inputs are rerun. This workflow only
produces recommendations: notifications, recovery actions, and clinical
authorization decisions remain subject to human approval.

The simulator and its metrics are development scaffolding only. Metrics measure
agreement with simulated outcomes, not performance on real cases. Before
production use, train and validate against representative, time-appropriate,
de-identified historical outcomes and assess probability calibration and
performance across operational groups. The model does not use patient
identifiers or clinical document text.

### Independent synthetic seed experiments

To compare separate 6,000-row synthetic datasets, use the multi-seed mode.
It creates a distinct experimental artifact and full JSON/Markdown report for
each seed, plus aggregate reports. It refuses to overwrite any matching output
file. Each run uses its seed for data generation and Random Forest randomness,
and derives separate train/test and train/validation split seeds. Each feature
ablation removes one of payer, service, case status, unresolved document
count, pending task count, and workflow failure count, then fits on training
data, selects a threshold on validation data, and evaluates once on test data.
The Random Forest and each baseline independently select their operating
threshold on that run's validation partition.

```powershell
Set-Location 'C:\Users\Admin\Desktop\PA_guard'
$runId = Get-Date -Format 'yyyyMMdd-HHmmss'
& '.\.venv\Scripts\python.exe' '.\backend\train_sla_risk_model.py' `
  --samples 6000 `
  --seeds 42 123 2027 `
  --run-id $runId
```

Outputs use seed-specific names under `backend\models` and `backend\reports`;
the aggregate report is named `sla_risk_multiseed_42_123_2027_<run-id>.*`. All such
results are synthetic simulator evaluations. Feature ablation measures
incremental predictive value only under the simulator and is not causal
evidence. The synthetic target is set to 1 when the generated future
resolution duration exceeds positive time remaining; that duration and the
target are excluded from model inputs. Partitions are disjoint, preprocessing
is fitted on training rows, thresholds are selected on validation rows, and
the test partition is reserved for final metrics. Recurring simulator
assumptions are shared across partitions, so this does not test temporal or
distribution-shift generalization.

The evidence-review feedback endpoints support collecting explicit human review
outcomes separately from experimental model predictions. New feedback is stored
as `PENDING` with outcome `UNKNOWN`; only the review endpoint can set a human
outcome. Training exports include only `CONFIRMED` records with `PRESENT` or
`MISSING` outcomes.

## Prototype security limitation

The existing evidence feedback endpoint's `reviewer_identifier` is still
supplied by its caller and is **not authenticated**. The new approval APIs have
prototype authentication, but the legacy case/document/evidence/agent APIs are
not globally protected. Treat the whole application as a controlled local
prototype and do not expose it to real case data. No OCR result, processing
error, or model prediction is automatically converted into a human label.

## Run the backend

## Approval workflow prototype

The additive startup migration creates `pa_staff`, `pa_sessions`,
`pa_case_assignments`, `pa_assignment_history`, `pa_notes`, `pa_reviews`, and
`pa_approval_policy`; it does not clear or recreate `pa_guard.db`. The approval
API uses PBKDF2 password hashes and expiring opaque bearer sessions. Local demo
accounts are seeded only when the staff table is empty:

| Role | Username | Password |
| --- | --- | --- |
| PA Specialist | `specialist` | `SpecialistDemo1` |
| PA Supervisor | `supervisor` | `SupervisorDemo1` |
| PA Manager | `manager` | `ManagerDemo1` |
| System Administrator | `admin` | `AdminDemo1` |

These credentials are for local synthetic demonstrations only. Current `/api`
routes require a bearer session except the health check and login endpoint.
Role and case-assignment checks run in backend middleware for both legacy and
new routes. This local prototype is not connected to an organizational
identity provider and is not cleared for production or operational patient use.

New statuses include `SPECIALIST_REVIEW`, `AWAITING_SUPERVISOR`,
`AWAITING_MANAGER`, `RETURNED_TO_SPECIALIST`, `UNDER_REVIEW`, `APPROVED`, and
`DENIED`. Existing case rows retain their existing statuses. With the default
policy, supervisor review forwards the case to `AWAITING_MANAGER`; it does not
approve the request. The manager decision API stores reason, old/new status,
authenticated actor, and timestamp together with an audit event. Finalized
cases reject further actions.

Allocation prioritizes overdue cases, then cases flagged by the existing SLA
model as predicted breaches, then deterministic at-risk status, before
deadline. If the model cannot return a prediction, deterministic status and
deadline provide the fallback. It then chooses an active, available staff
member in the required workflow role with the lowest open case count below the
configured limit. Repeated allocation preserves current assignments. Manual
reassignment checks role, availability, capacity, and preserves assignment
history. The approval screen displays
document metadata and OCR status; document text remains on the existing
case-detail route.

## Run the backend

From the repository root, when the existing v2 serving artifact is present:

```powershell
Set-Location 'C:\Users\Admin\Desktop\PA_guard'
$env:OLLAMA_URL = 'http://localhost:11434'
$env:OLLAMA_MODEL = 'qwen3.5:latest'
& '.\.venv\Scripts\python.exe' -m uvicorn main:app --app-dir '.\backend' --reload
```

Confirm the backend, all four LangGraph routes, and the local model are ready:

```powershell
Invoke-RestMethod 'http://127.0.0.1:8000/api/health'
Invoke-RestMethod 'http://127.0.0.1:8000/api/agents/status'
Invoke-RestMethod 'http://127.0.0.1:8000/api/cases' | Measure-Object
```

If the readiness response says the model is missing, install it in Ollama with
`ollama pull qwen3.5:latest`, then restart the backend. The app keeps the
deterministic fallback wording when generation is unavailable and labels that
fallback in the agent conversation.

If the live v2 artifact is absent, the prediction endpoint reports that the
model is unavailable. The experimental v3 artifact is deliberately not loaded
by the API.

Test the SLA model endpoint by creating a development case with a future
deadline, then requesting its prediction:

```powershell
$case = Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/cases' `
  -ContentType 'application/json' `
  -Body (@{
    patient_reference = 'SYNTHETIC-TEST'
    payer = 'Northstar Health'
    service = 'Lumbar MRI'
    deadline = (Get-Date).ToUniversalTime().AddDays(2).ToString('o')
    coordinator = 'Local test'
  } | ConvertTo-Json)

Invoke-RestMethod "http://127.0.0.1:8000/api/cases/$($case.id)/sla-risk-prediction"
```

## Exercise the endpoints

Submit pending feedback for an existing case using an evidence name listed in
an active approved requirement:

```powershell
$feedback = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/cases/PA-1042/evidence-review-feedback" `
  -ContentType "application/json" `
  -Body '{"required_evidence":"Clinical notes","ml_prediction":"PREDICTED_MISSING"}'
```

Confirm a human-reviewed outcome (or set `review_status` to `REJECTED`, omitting
`human_outcome`):

```powershell
Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:8000/api/evidence-review-feedback/$($feedback.id)/review" `
  -ContentType "application/json" `
  -Body '{"review_status":"CONFIRMED","human_outcome":"PRESENT","reviewer_identifier":"reviewer-17"}'
```

Retrieve case feedback and export eligible records:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/api/cases/PA-1042/evidence-review-feedback"
Invoke-RestMethod "http://127.0.0.1:8000/api/evidence-review-feedback/training-export?format=json"
Invoke-WebRequest "http://127.0.0.1:8000/api/evidence-review-feedback/training-export?format=csv" `
  -OutFile ".\evidence-review-training.csv"
```

Run the SLA, evidence prediction, and feedback API tests from the repository root:

```powershell
Set-Location 'C:\Users\Admin\Desktop\PA_guard\backend'
& '..\.venv\Scripts\python.exe' -m unittest `
  test_sla_risk_model `
  test_evidence_prediction_model `
  test_evidence_prediction_api `
  test_evidence_review_feedback_api
```
