# PA Guard

PA Guard is a prior-authorization operations prototype with a React/Vite frontend, FastAPI backend, SQLite persistence, workflow agents, and experimental ML components. It is a local demonstration system, not a production clinical or payer decision service.

## Run locally (Windows PowerShell)

From the repository root, start the backend:

```powershell
& '.\.venv\Scripts\python.exe' -m uvicorn main:app --app-dir '.\backend' --host 127.0.0.1 --port 8000 --reload
```

In a second terminal, start the frontend:

```powershell
Set-Location '.\apps\pa-guard'
npm.cmd run dev -- --host 127.0.0.1
```

The backend defaults to `backend/pa_guard.db`. Set `PA_GUARD_DB` before starting the backend to select a different SQLite file. Do not point a demo at a database until its identity and contents are confirmed. `GET /health` reports process health; it does not prove that an external OCR, Ollama, or payer service is available. Optional Ollama settings are `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_EMBED_MODEL`, and `OLLAMA_GENERATION_TIMEOUT_SECONDS`.

## Honest evaluation reports

- [SLA model card](docs/MODEL_CARD.md): **trained and evaluated on synthetic data; not validated on real prior-authorization outcomes.** It compares a majority baseline, deadline arithmetic, logistic regression, and random forest on a separately generated shifted synthetic set. Its model card endpoint is `/api/sla-risk-model-card`.
- [Evidence classifier evaluation](docs/EVIDENCE_EVALUATION.md): ten authored synthetic examples evaluated with stratified five-fold cross-validation. The classifier remains experimental and is not used for decisions. Its predicted evidence gaps do not verify evidence or satisfy policy requirements.

Reproduce the reports from the repository root:

```powershell
& '.\.venv\Scripts\python.exe' backend\evaluate_sla_shifted.py
& '.\.venv\Scripts\python.exe' backend\evaluate_evidence_classifier.py
```

The SLA evaluator writes a uniquely named artifact and refuses to overwrite an existing artifact at the selected path. Preserve generated artifacts and reports when rerunning.

## Demonstration limits and safeguards

- Model training and reported performance use synthetic data only; real-world generalization and calibration have not been established.
- Evidence classifier examples are hand-authored, tiny, and synthetic. Classifier output is experimental, is not used for decisions, and cannot verify document contents.
- Policy records marked `MOCK_DEMO` or otherwise unverified are not authoritative payer policy. An HTTPS source URL alone does not establish that its text supports each stored criterion.
- The stored `EVD-CMS-MRI` checklist remains unapproved and unverified. The linked CMS NCD 220.2 covers MRI coverage at a general level and does not substantiate the stored neurological-deficit or prior-conservative-therapy criteria as presently worded.
- OCR and extracted text can be incomplete or wrong; a document match does not establish clinical sufficiency or medical necessity.
- Submission responses marked `SUBMITTED_DEMO` are local simulations. No payer endpoint is contacted and no authorization is granted.
- Notifications and escalations must be interpreted by their explicit recorded/delivery status. A recorded event does not prove an external notification was delivered.
- A human reviewer remains responsible for evidence confirmation and authorization decisions. Existing approval steps must be completed; ML output cannot authorize, deny, or submit a case by itself.
- This prototype has not been security-hardened or validated for production deployment. Configure a restricted host, database access, and allowed frontend origins before any controlled demonstration beyond a local machine.

See [backend/README.md](backend/README.md) for API and workflow details. The frontend production build is `npm.cmd run build` from `apps/pa-guard`.
