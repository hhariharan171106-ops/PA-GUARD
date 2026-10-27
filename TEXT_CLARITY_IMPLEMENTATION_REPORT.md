# PA Guard Text Clarity Implementation Report

## What changed

- **AI and SLA explanations:** `backend/rag.py` now asks the existing Ollama calls for short, case-specific plain English, the reason, a next step, and uncertainty. It still uses the supplied case facts and same model. JSON-shaped output falls back to the existing factual explanation. `backend/case_workflow.py` presents the fallback as a short case summary that separates the synthetic risk estimate, deadline check, document review, and next step.
- **Evidence feedback:** `apps/pa-guard/src/pages/evidence.tsx` uses clearer review wording and distinguishes an AI explanation from a rule-based explanation.
- **Escalation and assignment:** `apps/pa-guard/src/lib/pa-context.tsx` confirms which case was sent for review. `apps/pa-guard/src/pages/case-allocation.tsx` reports the case ID, assignee, role, and saved assignment, and gives readable API errors.
- **Document upload:** `apps/pa-guard/src/pages/case-details.tsx` and `apps/pa-guard/src/lib/pa-context.tsx` replace OCR/HTTP jargon with file counts, readable outcomes, and retry guidance.
- **Shared errors/status labels:** `apps/pa-guard/src/lib/user-messages.ts` is used by `apps/pa-guard/src/lib/pa-api.ts`, `auth-context.tsx`, and relevant allocation flows. Focused coverage is in `apps/pa-guard/src/lib/user-messages.test.mjs`.
- **Reports/tests:** `TEXT_CLARITY_REVIEW.md`, this report, `backend/test_case_explanation.py`, and the updated fallback wording assertion in `backend/test_document_workflow.py`.

Additional copy edits already made during this turn were retained in `apps/pa-guard/src/components/case-table.tsx`, `apps/pa-guard/src/components/recovery-agent-panel.tsx`, `apps/pa-guard/src/components/system/error-boundary.tsx`, `apps/pa-guard/src/pages/_layout.tsx`, `agent-center.tsx`, `approvals.tsx`, `index.tsx`, and `submission-review.tsx`. No workflow, permission, API, database, policy, or SLA calculation was changed.

## Examples

- AI prompt: asks for a short explanation in simple English based on the case facts, instead of a generic or raw structured response.
- Case fallback: “Deterministic case analysis” with long technical lines → “Case summary” with separate risk estimate, deadline check, document review, and next step.
- Upload: “Document saved, but OCR failed” → “Document saved, but its text could not be read. Review the file and try again.”
- Assignment: generic success → names the case and staff member and confirms that the assignment was recorded.

## Validation and limitations

- `backend/test_case_explanation.py` and `backend/test_agent_readiness.py`: **29 passed**.
- Frontend TypeScript check: **passed**.
- Focused frontend utility checks: **passed** for API errors/status labels, audit details, allocation, and evidence permissions.
- Scoped whitespace check: **passed**. Repository-wide `git diff --check` reports a trailing-space line in the already-modified `apps/pa-guard/vite.config.ts`; that file is outside this text task and was left untouched.
- Frontend build could not finish: Vite/esbuild was blocked from starting a child process (`spawn EPERM`).
- `test_document_workflow.py` could not run in this environment because its isolated temporary document directory was denied access (`PermissionError`). No live Ollama response or browser session was used, so generated wording was checked through prompt/fallback tests, not by running the local model or UI.
- Remaining copy outside the narrowed demo paths, including specialized policy-management terminology, has not been reviewed or rewritten.
